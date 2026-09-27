"""ScoringEngine：库的唯一入口，供上层销售激励平台调用。

上层保证同一时刻只有一个调用，因此每个写操作用一个事务完成，
完成后即落库，进程崩溃也只会丢失当前这次调用。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from . import db
from .levels import level_for_xp
from .models import (
    ActivityInput,
    ActivityStatus,
    ActivityStatusView,
    ActivityType,
    BatchItemOutcome,
    BatchResult,
    ItemResult,
    LedgerEntryView,
    PlanProgressView,
    PlanSpec,
    ReviewState,
    SubtaskPeriod,
    SubtaskProgressView,
    XpView,
)
from .recompute import recompute_salesperson

MAX_BATCH_SIZE = 200
REVIEW_THRESHOLD_CENTS = 2_000_000  # 2 万美元


def _parse_occurred_at(raw: object) -> int:
    """解析带时区偏移的 ISO 8601 时间，返回 epoch 秒。不合格抛 ValueError。"""
    if not isinstance(raw, str):
        raise ValueError("occurred_at 必须是 ISO 8601 字符串")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError("occurred_at 必须带时区偏移")
    return math.floor(dt.timestamp())


def _content_hash(activity_id: str, salesperson: str, type_: ActivityType,
                  occurred_at: int, amount_cents: int, note: str | None) -> str:
    payload = "\x1f".join([
        activity_id, salesperson, type_.value, str(occurred_at),
        str(amount_cents), "" if note is None else note,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_item(item: ActivityInput) -> tuple[ActivityType, int]:
    """校验单条输入，返回 (类型, epoch秒)。不合格抛 ValueError。"""
    if not isinstance(item.activity_id, str) or not item.activity_id:
        raise ValueError("activity_id 不能为空")
    if not isinstance(item.salesperson, str) or not item.salesperson:
        raise ValueError("salesperson 不能为空")
    try:
        type_ = item.type if isinstance(item.type, ActivityType) else ActivityType(item.type)
    except ValueError:
        raise ValueError(f"未知活动类型: {item.type!r}") from None
    occurred_at = _parse_occurred_at(item.occurred_at)
    if isinstance(item.amount_cents, bool) or not isinstance(item.amount_cents, int):
        raise ValueError("amount_cents 必须是整数（分）")
    if item.amount_cents < 0:
        raise ValueError("amount_cents 不能为负")
    if item.note is not None and not isinstance(item.note, str):
        raise ValueError("note 必须是字符串或 None")
    return type_, occurred_at


class ScoringEngine:
    def __init__(self, db_path: str, company_tz: str) -> None:
        try:
            self._tz = ZoneInfo(company_tz)
        except Exception as exc:
            raise ValueError(f"非法的公司时区: {company_tz!r}") from exc
        self.company_tz = company_tz
        self._conn = db.connect(db_path, company_tz)

    # ---------- 写操作 ----------

    def create_plan(self, spec: PlanSpec) -> str:
        """创建计划。计划对所有销售生效，创建后不可修改。"""
        if not isinstance(spec.plan_id, str) or not spec.plan_id:
            raise ValueError("plan_id 不能为空")
        try:
            start = date.fromisoformat(spec.start_date)
        except (TypeError, ValueError):
            raise ValueError(f"非法的开始日期: {spec.start_date!r}") from None
        if not 1 <= spec.duration_days <= 30:
            raise ValueError("持续天数必须在 1 到 30 之间")
        if not 1 <= len(spec.subtasks) <= 10:
            raise ValueError("子任务数量必须在 1 到 10 之间")
        for sub in spec.subtasks:
            if not isinstance(sub.type, ActivityType):
                try:
                    ActivityType(sub.type)
                except ValueError:
                    raise ValueError(f"未知子任务类型: {sub.type!r}") from None
            if sub.target_count < 1:
                raise ValueError("目标条数必须为正整数")
            if not 10 <= sub.xp <= 1000:
                raise ValueError("XP 必须在 10 到 1000 之间")
            if sub.min_amount_cents is not None and sub.min_amount_cents < 0:
                raise ValueError("单笔最低金额不能为负")
            period = sub.period if isinstance(sub.period, SubtaskPeriod) else SubtaskPeriod(sub.period)
            if period not in (SubtaskPeriod.ONCE, SubtaskPeriod.DAILY):
                raise ValueError(f"未知周期: {sub.period!r}")

        conn = self._conn
        if conn.execute("SELECT 1 FROM plans WHERE plan_id = ?",
                        (spec.plan_id,)).fetchone():
            raise ValueError(f"计划已存在且不可修改: {spec.plan_id}")
        try:
            conn.execute("BEGIN")
            seq = db.next_seq(conn)
            conn.execute(
                "INSERT INTO plans(plan_id, start_date, duration_days, created_seq)"
                " VALUES (?, ?, ?, ?)",
                (spec.plan_id, start.isoformat(), spec.duration_days, seq),
            )
            for idx, sub in enumerate(spec.subtasks):
                conn.execute(
                    "INSERT INTO subtasks(plan_id, idx, type, target_count,"
                    " min_amount_cents, xp, period) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (spec.plan_id, idx, ActivityType(sub.type).value, sub.target_count,
                     sub.min_amount_cents, sub.xp,
                     SubtaskPeriod(sub.period).value),
                )
            # 进度只看发生时间，不看计划创建时间：对所有已有销售补算
            salespersons = [
                r[0] for r in conn.execute("SELECT DISTINCT salesperson FROM activities")
            ]
            for sp in salespersons:
                recompute_salesperson(conn, self._tz, sp, f"plan:{spec.plan_id}", seq)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return spec.plan_id

    def submit_batch(self, activities: list[ActivityInput]) -> BatchResult:
        """提交一批活动。超过 200 条整批拒收（抛 ValueError）。"""
        if len(activities) > MAX_BATCH_SIZE:
            raise ValueError(f"一批最多 {MAX_BATCH_SIZE} 条，本批 {len(activities)} 条，整批拒收")
        result = BatchResult()
        if not activities:
            return result

        conn = self._conn
        try:
            conn.execute("BEGIN")
            seq = db.next_seq(conn)
            cause = f"batch:{seq}"
            affected: set[str] = set()
            seen_in_batch: dict[str, str] = {}  # activity_id -> content_hash
            for item in activities:
                activity_id = getattr(item, "activity_id", None)
                try:
                    type_, occurred_at = _validate_item(item)
                except ValueError as exc:
                    result.outcomes.append(BatchItemOutcome(
                        activity_id if isinstance(activity_id, str) else None,
                        ItemResult.REJECTED_INVALID, str(exc)))
                    continue
                content_hash = _content_hash(
                    item.activity_id, item.salesperson, type_,
                    occurred_at, item.amount_cents, item.note)
                existing_hash = seen_in_batch.get(item.activity_id)
                if existing_hash is None:
                    row = conn.execute(
                        "SELECT content_hash FROM activities WHERE activity_id = ?",
                        (item.activity_id,)).fetchone()
                    existing_hash = row["content_hash"] if row else None
                if existing_hash is not None:
                    if existing_hash == content_hash:
                        result.outcomes.append(BatchItemOutcome(
                            item.activity_id, ItemResult.DUPLICATE_SAME,
                            "同编号同内容，幂等忽略"))
                    else:
                        result.outcomes.append(BatchItemOutcome(
                            item.activity_id, ItemResult.REJECTED_CONFLICT,
                            "同编号内容不同，拒收"))
                    continue
                needs_review = (type_ is ActivityType.CLOSE
                                and item.amount_cents >= REVIEW_THRESHOLD_CENTS)
                review_state = ReviewState.PENDING if needs_review else ReviewState.NONE
                status = (ActivityStatus.PENDING_REVIEW if needs_review
                          else ActivityStatus.VALID)
                conn.execute(
                    "INSERT INTO activities(activity_id, salesperson, type, occurred_at,"
                    " occurred_iso, amount_cents, note, review_state, status,"
                    " status_reason, content_hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item.activity_id, item.salesperson, type_.value, occurred_at,
                     item.occurred_at, item.amount_cents, item.note,
                     review_state.value, status.value,
                     "大额成单待复核" if needs_review else None, content_hash),
                )
                seen_in_batch[item.activity_id] = content_hash
                affected.add(item.salesperson)
                result.outcomes.append(BatchItemOutcome(
                    item.activity_id, ItemResult.ACCEPTED,
                    "待复核" if needs_review else None))
            for sp in affected:
                recompute_salesperson(conn, self._tz, sp, cause, seq)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result

    def review_close(self, activity_id: str, approve: bool) -> None:
        """复核大额成单。结论作出后不能改。"""
        conn = self._conn
        row = conn.execute(
            "SELECT salesperson, review_state FROM activities WHERE activity_id = ?",
            (activity_id,)).fetchone()
        if row is None:
            raise KeyError(f"活动不存在: {activity_id}")
        if row["review_state"] != ReviewState.PENDING.value:
            raise ValueError(f"活动 {activity_id} 不在待复核状态，复核结论不可更改")
        try:
            conn.execute("BEGIN")
            seq = db.next_seq(conn)
            if approve:
                conn.execute(
                    "UPDATE activities SET review_state = ? WHERE activity_id = ?",
                    (ReviewState.APPROVED.value, activity_id))
            else:
                conn.execute(
                    "UPDATE activities SET review_state = ?, status = ?, status_reason = ?"
                    " WHERE activity_id = ?",
                    (ReviewState.REJECTED.value, ActivityStatus.REVIEW_REJECTED.value,
                     "复核驳回，永不生效", activity_id))
            recompute_salesperson(
                conn, self._tz, row["salesperson"], f"review:{activity_id}", seq)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ---------- 查询 ----------

    def get_activity_status(self, activity_id: str) -> ActivityStatusView:
        row = self._conn.execute(
            "SELECT status, status_reason FROM activities WHERE activity_id = ?",
            (activity_id,)).fetchone()
        if row is None:
            raise KeyError(f"活动不存在: {activity_id}")
        return ActivityStatusView(activity_id, ActivityStatus(row["status"]),
                                  row["status_reason"])

    def get_plan_progress(self, salesperson: str, plan_id: str) -> PlanProgressView:
        conn = self._conn
        subtasks = conn.execute(
            "SELECT idx FROM subtasks WHERE plan_id = ? ORDER BY idx", (plan_id,),
        ).fetchall()
        if not subtasks:
            raise KeyError(f"计划不存在: {plan_id}")
        states = {
            r["idx"]: r for r in conn.execute(
                "SELECT idx, unlocked, unlocked_at, progress_json FROM subtask_state"
                " WHERE salesperson = ? AND plan_id = ?", (salesperson, plan_id))
        }
        completions: dict[int, list[str]] = {}
        for r in conn.execute(
                "SELECT idx, period_key FROM completions"
                " WHERE salesperson = ? AND plan_id = ? ORDER BY period_key",
                (salesperson, plan_id)):
            completions.setdefault(r["idx"], []).append(r["period_key"])
        views: list[SubtaskProgressView] = []
        for sub in subtasks:
            idx = sub["idx"]
            st = states.get(idx)
            if st is None or not st["unlocked"]:
                views.append(SubtaskProgressView(idx, False, None, {}, []))
                continue
            unlocked_at = None
            if st["unlocked_at"] is not None:
                unlocked_at = datetime.fromtimestamp(
                    st["unlocked_at"], timezone.utc).isoformat()
            views.append(SubtaskProgressView(
                idx, True, unlocked_at, json.loads(st["progress_json"]),
                completions.get(idx, [])))
        return PlanProgressView(salesperson, plan_id, views)

    def get_xp(self, salesperson: str) -> XpView:
        row = self._conn.execute(
            "SELECT total_xp FROM xp_totals WHERE salesperson = ?",
            (salesperson,)).fetchone()
        total = row["total_xp"] if row else 0
        return XpView(salesperson, total, level_for_xp(total))

    def get_ledger(self, salesperson: str | None = None) -> list[LedgerEntryView]:
        if salesperson is None:
            rows = self._conn.execute(
                "SELECT * FROM xp_ledger ORDER BY ledger_id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM xp_ledger WHERE salesperson = ? ORDER BY ledger_id",
                (salesperson,)).fetchall()
        return [
            LedgerEntryView(
                r["ledger_id"], r["salesperson"], r["plan_id"], r["subtask_index"],
                r["period_key"], r["amount"], r["cause"], r["created_seq"])
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ScoringEngine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
