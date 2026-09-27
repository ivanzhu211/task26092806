"""核心重算：对单个销售按发生时间重放全部已生效活动，
推导活动有效性、计划解锁、子任务完成周期与 XP 差额。

设计要点：每次提交/复核后，对受影响的销售全量重放其活动，
保证任意乱序补录、复核后的状态与"从头按时间顺序算一遍"完全一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import ActivityStatus

MIN_INTERVAL_SECONDS = 30
DAILY_VALID_CAP = 500

_REASONS = {
    ActivityStatus.VALID: "",
    ActivityStatus.INVALID_TOO_CLOSE: "距上一条有效活动不足30秒",
    ActivityStatus.INVALID_DAILY_CAP: "当天有效活动已达500条",
    ActivityStatus.PENDING_REVIEW: "大额成单待复核",
    ActivityStatus.REVIEW_REJECTED: "复核驳回，永不生效",
}


def reason_for(status: ActivityStatus) -> str:
    return _REASONS[status]


@dataclass
class _Act:
    activity_id: str
    epoch: int
    type: str
    amount_cents: int
    local_date: str
    status: ActivityStatus = ActivityStatus.VALID


def _load_activities(conn, salesperson: str, tz: ZoneInfo) -> list:
    rows = conn.execute(
        """
        SELECT activity_id, type, occurred_epoch, amount_cents
        FROM activities
        WHERE salesperson = ? AND review_state IN ('none', 'approved')
        ORDER BY occurred_epoch, activity_id
        """,
        (salesperson,),
    ).fetchall()
    acts = []
    for r in rows:
        local = datetime.fromtimestamp(r["occurred_epoch"], tz).date().isoformat()
        acts.append(
            _Act(
                activity_id=r["activity_id"],
                epoch=r["occurred_epoch"],
                type=r["type"],
                amount_cents=r["amount_cents"],
                local_date=local,
            )
        )
    return acts


def _mark_validity(acts: list) -> None:
    """就地标记每条活动的有效性。无效活动不作为间隔起点，也不计入日上限。"""
    last_valid_epoch = None
    day_counts = {}
    for a in acts:
        if last_valid_epoch is not None and a.epoch - last_valid_epoch < MIN_INTERVAL_SECONDS:
            a.status = ActivityStatus.INVALID_TOO_CLOSE
            continue
        if day_counts.get(a.local_date, 0) >= DAILY_VALID_CAP:
            a.status = ActivityStatus.INVALID_DAILY_CAP
            continue
        a.status = ActivityStatus.VALID
        last_valid_epoch = a.epoch
        day_counts[a.local_date] = day_counts.get(a.local_date, 0) + 1


def _plan_days(start_date: str, duration_days: int) -> list:
    d0 = date.fromisoformat(start_date)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(duration_days)]


def _evaluate_plan(plan, subtasks, valid_acts, tz, completions, states, salesperson):
    day_list = _plan_days(plan["start_date"], plan["duration_days"])
    day_set = set(day_list)
    plan_start_epoch = int(
        datetime.combine(date.fromisoformat(plan["start_date"]), datetime.min.time(), tz)
        .timestamp()
    )
    pool = [a for a in valid_acts if a.local_date in day_set]
    unlock_pos = None  # (epoch, activity_id)，只统计严格排在其后的活动
    for st in subtasks:
        seq = st["seq"]
        if seq == 1:
            unlocked_epoch = plan_start_epoch
            scope = pool
        elif unlock_pos is not None:
            unlocked_epoch = unlock_pos[0]
            scope = [a for a in pool if (a.epoch, a.activity_id) > unlock_pos]
        else:
            states.append((salesperson, plan["plan_id"], seq, None, 0))
            unlock_pos = None  # 后续子任务连锁锁定
            continue

        matching = [
            a
            for a in scope
            if a.type == st["type"]
            and (st["min_amount_cents"] is None or a.amount_cents >= st["min_amount_cents"])
        ]
        target = st["target_count"]
        trigger = None
        if st["period"] == "once":
            progress = len(matching)
            if progress >= target:
                trigger = matching[target - 1]
                completions[(plan["plan_id"], seq, "once")] = st["xp"]
        else:  # daily
            by_day = {}
            for a in matching:
                by_day[a.local_date] = by_day.get(a.local_date, 0) + 1
            progress = max(by_day.values(), default=0)
            first_trigger = None
            for d in day_list:
                if by_day.get(d, 0) >= target:
                    completions[(plan["plan_id"], seq, d)] = st["xp"]
                    if first_trigger is None:
                        day_acts = [a for a in matching if a.local_date == d]
                        first_trigger = day_acts[target - 1]
            trigger = first_trigger

        states.append((salesperson, plan["plan_id"], seq, unlocked_epoch, progress))
        unlock_pos = (trigger.epoch, trigger.activity_id) if trigger else None


def replay_salesperson(conn, salesperson: str, tz_name: str, mutation_seq: int) -> None:
    """重放单个销售的全部活动，更新活动状态、子任务进度快照与 XP 流水。

    调用方须在外层事务中调用，并提供本次变更的 mutation_seq。
    """
    tz = ZoneInfo(tz_name)
    acts = _load_activities(conn, salesperson, tz)
    _mark_validity(acts)

    conn.executemany(
        "UPDATE activities SET status = ?, reason = ? WHERE activity_id = ?",
        [
            (a.status.value, reason_for(a.status), a.activity_id)
            for a in acts
        ],
    )

    valid_acts = [a for a in acts if a.status is ActivityStatus.VALID]

    plans = conn.execute("SELECT * FROM plans ORDER BY plan_id").fetchall()
    completions = {}
    states = []
    for plan in plans:
        subtasks = conn.execute(
            "SELECT * FROM subtasks WHERE plan_id = ? ORDER BY seq",
            (plan["plan_id"],),
        ).fetchall()
        _evaluate_plan(plan, subtasks, valid_acts, tz, completions, states, salesperson)

    # 旧的完成集合：流水净额 > 0 的周期视为当前已完成
    old_complete = set()
    for r in conn.execute(
        """
        SELECT plan_id, subtask_seq, period_key, SUM(delta_xp) AS net
        FROM ledger WHERE salesperson = ?
        GROUP BY plan_id, subtask_seq, period_key
        """,
        (salesperson,),
    ).fetchall():
        if r["net"] > 0:
            old_complete.add((r["plan_id"], r["subtask_seq"], r["period_key"]))

    new_complete = set(completions)
    now = datetime.now(timezone.utc).isoformat()
    balance = conn.execute(
        "SELECT COALESCE(SUM(delta_xp), 0) AS b FROM ledger WHERE salesperson = ?",
        (salesperson,),
    ).fetchone()["b"]

    entries = []
    for key in sorted(new_complete - old_complete):
        entries.append((key, completions[key]))
    for key in sorted(old_complete - new_complete):
        entries.append((key, -_ledger_xp(conn, salesperson, key)))
    for (plan_id, seq, period_key), delta in entries:
        balance += delta
        conn.execute(
            """
            INSERT INTO ledger
                (salesperson, plan_id, subtask_seq, period_key,
                 delta_xp, balance_after, mutation_seq, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (salesperson, plan_id, seq, period_key, delta, balance, mutation_seq, now),
        )

    conn.execute("DELETE FROM subtask_state WHERE salesperson = ?", (salesperson,))
    conn.executemany(
        """
        INSERT INTO subtask_state
            (salesperson, plan_id, subtask_seq, unlocked_epoch, progress)
        VALUES (?, ?, ?, ?, ?)
        """,
        states,
    )


def _ledger_xp(conn, salesperson, key) -> int:
    plan_id, seq, period_key = key
    row = conn.execute(
        """
        SELECT SUM(delta_xp) AS net FROM ledger
        WHERE salesperson = ? AND plan_id = ? AND subtask_seq = ? AND period_key = ?
        """,
        (salesperson, plan_id, seq, period_key),
    ).fetchone()
    return int(row["net"])
