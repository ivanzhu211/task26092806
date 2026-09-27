"""对上层销售激励平台暴露的库 API。

同一时刻只有一个调用由上层保证；每次提交/复核在一个事务内完成，
并对受影响的销售做全量重放，保证状态强一致。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import db
from .level import level_for_xp
from .models import (
    ActivityInput,
    ActivityStatus,
    ActivityType,
    ActivityView,
    LedgerEntry,
    PlanDef,
    ReviewDecision,
    SubTaskPeriod,
    SubTaskProgress,
)
from .replay import MIN_INTERVAL_SECONDS, reason_for, replay_salesperson

MAX_BATCH = 200
REVIEW_THRESHOLD_CENTS = 2_000_000  # 2万美元


class SalesXPService:
    """主入口：初始化时给定 SQLite 路径与公司时区（IANA 名称）。"""

    def __init__(self, db_path: str, company_tz: str):
        self._tz_name = company_tz
        self._tz = ZoneInfo(company_tz)  # 非法时区名在此抛异常
        self._conn = db.connect(db_path)

    # ---------------- 计划 ----------------

    def create_plan(self, plan: PlanDef) -> None:
        """创建计划（创建后不可修改，重复 plan_id 拒绝）。"""
        if not 1 <= plan.duration_days <= 30:
            raise ValueError("duration_days 必须在 1..30")
        if not 1 <= len(plan.subtasks) <= 10:
            raise ValueError("子任务数量必须在 1..10")
        datetime.strptime(plan.start_date, "%Y-%m-%d")  # 校验日期格式
        for st in plan.subtasks:
            if st.target_count < 1:
                raise ValueError("target_count 必须 >= 1")
            if not 10 <= st.xp <= 1000:
                raise ValueError("XP 必须在 10..1000")
            if st.min_amount_cents is not None and st.min_amount_cents < 0:
                raise ValueError("min_amount_cents 不能为负")
        with self._conn:
            if self._conn.execute(
                "SELECT 1 FROM plans WHERE plan_id = ?", (plan.plan_id,)
            ).fetchone():
                raise ValueError(f"计划已存在，不可修改: {plan.plan_id}")
            self._conn.execute(
                "INSERT INTO plans (plan_id, start_date, duration_days) VALUES (?, ?, ?)",
                (plan.plan_id, plan.start_date, plan.duration_days),
            )
            self._conn.executemany(
                """
                INSERT INTO subtasks
                    (plan_id, seq, type, target_count, min_amount_cents, xp, period)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        plan.plan_id,
                        i + 1,
                        st.type.value,
                        st.target_count,
                        st.min_amount_cents,
                        st.xp,
                        st.period.value,
                    )
                    for i, st in enumerate(plan.subtasks)
                ],
            )
            # 进度不看计划创建时间：对已有活动的所有销售立即重放
            salespersons = [
                r[0] for r in self._conn.execute(
                    "SELECT DISTINCT salesperson FROM activities"
                ).fetchall()
            ]
            if salespersons:
                seq = db.next_mutation_seq(self._conn)
                for sp in salespersons:
                    replay_salesperson(self._conn, sp, self._tz_name, seq)

    # ---------------- 活动提交 ----------------

    def submit_activities(self, activities: list) -> dict:
        """批量提交活动。返回 {"batch_accepted": bool, "results": [...]}。

        超过 200 条整批拒收；单条不合格只拒该条；
        同编号同内容为幂等成功，同编号不同内容拒收后到的那条。
        """
        if len(activities) > MAX_BATCH:
            return {
                "batch_accepted": False,
                "results": [
                    {
                        "activity_id": getattr(a, "activity_id", None),
                        "accepted": False,
                        "reason": f"批次超过 {MAX_BATCH} 条，整批拒收",
                    }
                    for a in activities
                ],
            }

        results = []
        to_insert = []
        for a in activities:
            err = self._validate_item(a)
            if err:
                results.append({"activity_id": a.activity_id, "accepted": False, "reason": err})
                continue
            epoch = datetime.fromisoformat(a.occurred_at).timestamp()
            to_insert.append((a, epoch))
            results.append(None)  # 占位，后面统一填

        affected = set()
        with self._conn:
            insert_idx = 0
            for i, a in enumerate(activities):
                if results[i] is not None:
                    continue
                act, epoch = to_insert[insert_idx]
                insert_idx += 1
                existing = self._conn.execute(
                    "SELECT * FROM activities WHERE activity_id = ?", (act.activity_id,)
                ).fetchone()
                if existing is not None:
                    if self._same_content(existing, act, epoch):
                        results[i] = {
                            "activity_id": act.activity_id,
                            "accepted": True,
                            "reason": "重复提交，内容一致，幂等忽略",
                        }
                    else:
                        results[i] = {
                            "activity_id": act.activity_id,
                            "accepted": False,
                            "reason": "编号重复且内容不同，拒收",
                        }
                    continue
                needs_review = (
                    act.type == ActivityType.CLOSE
                    and act.amount_cents >= REVIEW_THRESHOLD_CENTS
                )
                review_state = "pending" if needs_review else "none"
                status = (
                    ActivityStatus.PENDING_REVIEW
                    if needs_review
                    else ActivityStatus.VALID
                )
                self._conn.execute(
                    """
                    INSERT INTO activities
                        (activity_id, salesperson, type, occurred_epoch, occurred_iso,
                         amount_cents, note, review_state, status, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        act.activity_id,
                        act.salesperson,
                        act.type.value,
                        epoch,
                        act.occurred_at,
                        act.amount_cents,
                        act.note,
                        review_state,
                        status.value,
                        reason_for(status),
                    ),
                )
                results[i] = {
                    "activity_id": act.activity_id,
                    "accepted": True,
                    "reason": "待复核" if needs_review else "已受理",
                }
                if not needs_review:
                    affected.add(act.salesperson)

            if affected:
                seq = db.next_mutation_seq(self._conn)
                for sp in affected:
                    replay_salesperson(self._conn, sp, self._tz_name, seq)

        return {"batch_accepted": True, "results": results}

    @staticmethod
    def _validate_item(a: ActivityInput) -> Optional[str]:
        if not a.activity_id:
            return "activity_id 不能为空"
        if not a.salesperson:
            return "salesperson 不能为空"
        if not isinstance(a.type, ActivityType):
            return f"非法活动类型: {a.type!r}"
        try:
            dt = datetime.fromisoformat(a.occurred_at)
        except (ValueError, TypeError):
            return f"occurred_at 不是合法 ISO 8601: {a.occurred_at!r}"
        if dt.tzinfo is None:
            return "occurred_at 必须带时区偏移"
        if not isinstance(a.amount_cents, int) or a.amount_cents < 0:
            return "amount_cents 必须是非负整数"
        return None

    @staticmethod
    def _same_content(row, act: ActivityInput, epoch: float) -> bool:
        return (
            row["salesperson"] == act.salesperson
            and row["type"] == act.type.value
            and row["occurred_epoch"] == epoch
            and row["amount_cents"] == act.amount_cents
            and (row["note"] or None) == (act.note or None)
        )

    # ---------------- 复核 ----------------

    def review_activity(self, activity_id: str, decision: ReviewDecision) -> None:
        """复核待审活动；结论作出后不可改。"""
        with self._conn:
            row = self._conn.execute(
                "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"活动不存在: {activity_id}")
            if row["review_state"] != "pending":
                raise ValueError("该活动不在待复核状态，复核结论不可更改")
            if decision == ReviewDecision.APPROVE:
                self._conn.execute(
                    "UPDATE activities SET review_state = 'approved' WHERE activity_id = ?",
                    (activity_id,),
                )
                seq = db.next_mutation_seq(self._conn)
                replay_salesperson(self._conn, row["salesperson"], self._tz_name, seq)
            else:
                self._conn.execute(
                    """
                    UPDATE activities
                    SET review_state = 'rejected', status = ?, reason = ?
                    WHERE activity_id = ?
                    """,
                    (
                        ActivityStatus.REVIEW_REJECTED.value,
                        reason_for(ActivityStatus.REVIEW_REJECTED),
                        activity_id,
                    ),
                )
                db.next_mutation_seq(self._conn)

    # ---------------- 查询 ----------------

    def get_activity(self, activity_id: str) -> Optional[ActivityView]:
        row = self._conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
        if row is None:
            return None
        return ActivityView(
            activity_id=row["activity_id"],
            salesperson=row["salesperson"],
            type=ActivityType(row["type"]),
            occurred_at=row["occurred_iso"],
            amount_cents=row["amount_cents"],
            note=row["note"],
            status=ActivityStatus(row["status"]),
            reason=row["reason"],
        )

    def get_progress(self, salesperson: str, plan_id: str) -> list:
        """返回该销售在该计划下各子任务的进度（list[SubTaskProgress]）。"""
        subtasks = self._conn.execute(
            "SELECT * FROM subtasks WHERE plan_id = ? ORDER BY seq", (plan_id,)
        ).fetchall()
        if not subtasks:
            raise KeyError(f"计划不存在: {plan_id}")
        plan = self._conn.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        plan_start_epoch = datetime.combine(
            date.fromisoformat(plan["start_date"]), datetime.min.time(), self._tz
        ).timestamp()
        states = {
            r["subtask_seq"]: r
            for r in self._conn.execute(
                "SELECT * FROM subtask_state WHERE salesperson = ? AND plan_id = ?",
                (salesperson, plan_id),
            ).fetchall()
        }
        completed = {}
        for r in self._conn.execute(
            """
            SELECT subtask_seq, period_key, SUM(delta_xp) AS net
            FROM ledger WHERE salesperson = ? AND plan_id = ?
            GROUP BY subtask_seq, period_key
            """,
            (salesperson, plan_id),
        ).fetchall():
            if r["net"] > 0:
                completed.setdefault(r["subtask_seq"], []).append(r["period_key"])

        out = []
        for st in subtasks:
            seq = st["seq"]
            s = states.get(seq)
            unlocked_at = None
            if s is None and seq == 1:
                # 该销售尚无活动（未重放过）：第1个子任务从计划开始就解锁
                unlocked_at = datetime.fromtimestamp(
                    plan_start_epoch, timezone.utc
                ).isoformat()
            elif s is not None and s["unlocked_epoch"] is not None:
                unlocked_at = datetime.fromtimestamp(
                    s["unlocked_epoch"], timezone.utc
                ).isoformat()
            periods = tuple(sorted(completed.get(seq, [])))
            out.append(
                SubTaskProgress(
                    subtask_seq=seq,
                    unlocked_at=unlocked_at,
                    progress=s["progress"] if s else 0,
                    target=st["target_count"],
                    completed_periods=periods,
                )
            )
        return out

    def get_xp(self, salesperson: str) -> tuple:
        """返回 (XP 总额, 等级)。"""
        xp = self._conn.execute(
            "SELECT COALESCE(SUM(delta_xp), 0) AS b FROM ledger WHERE salesperson = ?",
            (salesperson,),
        ).fetchone()["b"]
        return int(xp), level_for_xp(int(xp))

    def get_ledger(self, salesperson: str, plan_id: Optional[str] = None) -> list:
        """流水明细（list[LedgerEntry]，按写入顺序）。"""
        sql = "SELECT * FROM ledger WHERE salesperson = ?"
        args = [salesperson]
        if plan_id is not None:
            sql += " AND plan_id = ?"
            args.append(plan_id)
        sql += " ORDER BY ledger_id"
        return [
            LedgerEntry(
                ledger_id=r["ledger_id"],
                salesperson=r["salesperson"],
                plan_id=r["plan_id"],
                subtask_seq=r["subtask_seq"],
                period_key=r["period_key"],
                delta_xp=r["delta_xp"],
                balance_after=r["balance_after"],
                created_at=r["created_at"],
            )
            for r in self._conn.execute(sql, args).fetchall()
        ]

    def close(self) -> None:
        self._conn.close()
