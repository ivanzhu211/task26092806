"""有效性与计划进度的重算（内部实现）。

任何改变某销售"已生效活动集合"的操作（新活动入库、复核结论、
新计划创建），都触发对该销售的全量重算：
1. 按 (occurred_at, activity_id) 排序重放，判定 30 秒间隔与每日 500 条上限；
2. 对每个计划逐个子任务串联计算解锁点与完成周期；
3. 与已存完成状态做 diff，只追加 XP 流水（加/减），并刷新进度快照。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import ActivityStatus, ReviewState

MIN_INTERVAL_SECONDS = 30
DAY_CAP = 500


def _local_date(tz: ZoneInfo, occurred_at: int) -> date:
    return datetime.fromtimestamp(occurred_at, tz).date()


def _load_valid_activities(
    conn: sqlite3.Connection, tz: ZoneInfo, salesperson: str
) -> list[sqlite3.Row]:
    """重放有效性规则，刷新 activities.status，返回有效活动列表。"""
    rows = conn.execute(
        """
        SELECT activity_id, type, occurred_at, amount_cents
        FROM activities
        WHERE salesperson = ? AND review_state IN (?, ?)
        ORDER BY occurred_at, activity_id
        """,
        (salesperson, ReviewState.NONE.value, ReviewState.APPROVED.value),
    ).fetchall()

    valid: list[sqlite3.Row] = []
    last_valid_at: int | None = None
    day_counts: dict[date, int] = {}
    for row in rows:
        status = ActivityStatus.VALID
        reason = None
        if last_valid_at is not None and row["occurred_at"] - last_valid_at < MIN_INTERVAL_SECONDS:
            status = ActivityStatus.INVALID_INTERVAL
            reason = "距上一条有效活动不足30秒"
        else:
            day = _local_date(tz, row["occurred_at"])
            if day_counts.get(day, 0) >= DAY_CAP:
                status = ActivityStatus.INVALID_DAY_CAP
                reason = "当天有效活动已达500条"
        if status is ActivityStatus.VALID:
            last_valid_at = row["occurred_at"]
            day = _local_date(tz, row["occurred_at"])
            day_counts[day] = day_counts.get(day, 0) + 1
            valid.append(row)
        conn.execute(
            "UPDATE activities SET status = ?, status_reason = ? WHERE activity_id = ?",
            (status.value, reason, row["activity_id"]),
        )
    return valid


def _compute_plan(
    valid: list[sqlite3.Row],
    subtasks: list[sqlite3.Row],
    tz: ZoneInfo,
    start_date: date,
    duration_days: int,
) -> list[dict]:
    """对单个计划串联计算每个子任务的解锁点、进度与完成周期。"""
    end_date = start_date + timedelta(days=duration_days)
    plan_start_at = int(datetime.combine(start_date, datetime.min.time(), tz).timestamp())

    results: list[dict] = []
    unlock_point: tuple[int, str] | None = None  # 排在该点之后的活动才计数
    chained = True  # 前一个子任务是否已首次达标
    prev_first_completion_at: int | None = None
    for idx, sub in enumerate(subtasks):
        if not chained:
            results.append({
                "idx": idx, "unlocked": False, "unlocked_at": None,
                "progress": {}, "periods": {},
            })
            continue
        min_amount = sub["min_amount_cents"]
        matching = [
            a for a in valid
            if a["type"] == sub["type"]
            and (min_amount is None or a["amount_cents"] >= min_amount)
            and start_date <= _local_date(tz, a["occurred_at"]) < end_date
            and (unlock_point is None
                 or (a["occurred_at"], a["activity_id"]) > unlock_point)
        ]
        target = sub["target_count"]
        periods: dict[str, sqlite3.Row] = {}
        if sub["period"] == "once":
            progress = {"count": len(matching)}
            if len(matching) >= target:
                periods["once"] = matching[target - 1]
        else:  # daily
            by_day: dict[date, list[sqlite3.Row]] = {}
            for a in matching:
                by_day.setdefault(_local_date(tz, a["occurred_at"]), []).append(a)
            progress = {"days": {d.isoformat(): len(v) for d, v in sorted(by_day.items())}}
            for day, acts in by_day.items():
                if len(acts) >= target:
                    periods[day.isoformat()] = acts[target - 1]
        if idx == 0:
            unlocked_at: int | None = plan_start_at
        else:
            # 子任务 i 在前一个子任务首次达标的那条活动之后解锁
            unlocked_at = prev_first_completion_at
        if periods:
            first_act = min(
                periods.values(),
                key=lambda a: (a["occurred_at"], a["activity_id"]),
            )
            unlock_point = (first_act["occurred_at"], first_act["activity_id"])
            prev_first_completion_at = first_act["occurred_at"]
        else:
            chained = False
            prev_first_completion_at = None
        results.append({
            "idx": idx,
            "unlocked": True,
            "unlocked_at": unlocked_at,
            "progress": progress,
            "periods": {k: (v["activity_id"], v["occurred_at"]) for k, v in periods.items()},
        })
    return results


def _apply_plan_diff(
    conn: sqlite3.Connection,
    salesperson: str,
    plan_id: str,
    subtasks: list[sqlite3.Row],
    results: list[dict],
    cause: str,
    seq: int,
) -> int:
    """完成状态 diff → 追加流水；刷新进度快照。返回 XP 净变化。"""
    xp_delta = 0
    for res, sub in zip(results, subtasks):
        idx = res["idx"]
        new_periods: dict[str, tuple[str, int]] = res["periods"]
        stored = {
            row["period_key"]: row
            for row in conn.execute(
                "SELECT period_key, completion_activity_id, completed_at "
                "FROM completions WHERE salesperson = ? AND plan_id = ? AND idx = ?",
                (salesperson, plan_id, idx),
            )
        }
        xp = sub["xp"]
        for key in sorted(new_periods):
            act_id, at = new_periods[key]
            if key not in stored:
                conn.execute(
                    "INSERT INTO completions VALUES (?, ?, ?, ?, ?, ?)",
                    (salesperson, plan_id, idx, key, act_id, at),
                )
                conn.execute(
                    "INSERT INTO xp_ledger"
                    "(salesperson, plan_id, subtask_index, period_key, amount, cause, created_seq)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (salesperson, plan_id, idx, key, xp, cause, seq),
                )
                xp_delta += xp
            elif stored[key]["completion_activity_id"] != act_id:
                # 只是完成时刻变了：更新记录，不记流水
                conn.execute(
                    "UPDATE completions SET completion_activity_id = ?, completed_at = ? "
                    "WHERE salesperson = ? AND plan_id = ? AND idx = ? AND period_key = ?",
                    (act_id, at, salesperson, plan_id, idx, key),
                )
        for key in sorted(stored):
            if key not in new_periods:
                conn.execute(
                    "DELETE FROM completions "
                    "WHERE salesperson = ? AND plan_id = ? AND idx = ? AND period_key = ?",
                    (salesperson, plan_id, idx, key),
                )
                conn.execute(
                    "INSERT INTO xp_ledger"
                    "(salesperson, plan_id, subtask_index, period_key, amount, cause, created_seq)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (salesperson, plan_id, idx, key, -xp, cause, seq),
                )
                xp_delta -= xp
        conn.execute(
            "INSERT INTO subtask_state(salesperson, plan_id, idx, unlocked, unlocked_at, progress_json)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(salesperson, plan_id, idx) DO UPDATE SET"
            " unlocked = excluded.unlocked, unlocked_at = excluded.unlocked_at,"
            " progress_json = excluded.progress_json",
            (
                salesperson, plan_id, idx,
                1 if res["unlocked"] else 0,
                res["unlocked_at"],
                json.dumps(res["progress"], sort_keys=True),
            ),
        )
    return xp_delta


def recompute_salesperson(
    conn: sqlite3.Connection,
    tz: ZoneInfo,
    salesperson: str,
    cause: str,
    seq: int,
) -> None:
    """对单个销售全量重算。调用方负责事务与提交。"""
    valid = _load_valid_activities(conn, tz, salesperson)

    plans = conn.execute(
        "SELECT plan_id, start_date, duration_days FROM plans ORDER BY created_seq"
    ).fetchall()
    total_delta = 0
    for plan in plans:
        subtasks = conn.execute(
            "SELECT idx, type, target_count, min_amount_cents, xp, period "
            "FROM subtasks WHERE plan_id = ? ORDER BY idx",
            (plan["plan_id"],),
        ).fetchall()
        results = _compute_plan(
            valid, subtasks, tz,
            date.fromisoformat(plan["start_date"]), plan["duration_days"],
        )
        total_delta += _apply_plan_diff(
            conn, salesperson, plan["plan_id"], subtasks, results, cause, seq,
        )
    if total_delta:
        conn.execute(
            "INSERT INTO xp_totals(salesperson, total_xp) VALUES (?, ?)"
            " ON CONFLICT(salesperson) DO UPDATE SET total_xp = total_xp + excluded.total_xp",
            (salesperson, total_delta),
        )
