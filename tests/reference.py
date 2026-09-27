"""独立的参考实现：直接按业务规则从最终生效活动集合从头算一遍。
刻意不 import 库内部逻辑，用于随机差分测试。
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

REVIEW_THRESHOLD = 2_000_000


def _epoch(iso):
    return datetime.fromisoformat(iso).timestamp()


def compute(activities, review_decisions, plans, tz_name):
    """activities: list[ActivityInput]（已按先到先算去重）
    review_decisions: dict activity_id -> 'approve'/'reject'
    plans: list[PlanDef]
    返回 {salesperson: {"status": {aid: status}, "xp": int,
          "completed": {(plan_id, seq, period_key)}, "progress": {(plan_id, seq): int,
          "unlocked": {(plan_id, seq): epoch|None}}}
    """
    tz = ZoneInfo(tz_name)
    by_sales = {}
    for a in activities:
        by_sales.setdefault(a.salesperson, []).append(a)

    result = {}
    for sp, acts in by_sales.items():
        status = {}
        effective = []
        for a in acts:
            needs_review = a.type.value == "Close" and a.amount_cents >= REVIEW_THRESHOLD
            dec = review_decisions.get(a.activity_id)
            if needs_review:
                if dec == "reject":
                    status[a.activity_id] = "review_rejected"
                    continue
                if dec != "approve":
                    status[a.activity_id] = "pending_review"
                    continue
            effective.append(a)

        effective.sort(key=lambda a: (_epoch(a.occurred_at), a.activity_id))
        valid = []
        last_epoch = None
        day_count = {}
        for a in effective:
            ep = _epoch(a.occurred_at)
            ld = datetime.fromtimestamp(ep, tz).date().isoformat()
            if last_epoch is not None and ep - last_epoch < 30:
                status[a.activity_id] = "invalid_too_close"
                continue
            if day_count.get(ld, 0) >= 500:
                status[a.activity_id] = "invalid_daily_cap"
                continue
            status[a.activity_id] = "valid"
            last_epoch = ep
            day_count[ld] = day_count.get(ld, 0) + 1
            valid.append((ep, a.activity_id, a.type.value, a.amount_cents, ld))

        completed = set()
        progress = {}
        unlocked = {}
        xp = 0
        for plan in plans:
            d0 = date.fromisoformat(plan.start_date)
            days = [(d0 + timedelta(days=i)).isoformat()
                    for i in range(plan.duration_days)]
            pool = [v for v in valid if v[4] in set(days)]
            unlock_pos = None
            for i, st in enumerate(plan.subtasks):
                seq = i + 1
                key = (plan.plan_id, seq)
                if seq == 1:
                    unlocked[key] = "plan_start"
                    scope = pool
                elif unlock_pos is not None:
                    unlocked[key] = unlock_pos[0]
                    scope = [v for v in pool if (v[0], v[1]) > unlock_pos]
                else:
                    unlocked[key] = None
                    progress[key] = 0
                    unlock_pos = None
                    continue
                matching = [v for v in scope
                            if v[2] == st.type.value
                            and (st.min_amount_cents is None
                                 or v[3] >= st.min_amount_cents)]
                trigger = None
                if st.period.value == "once":
                    progress[key] = len(matching)
                    if len(matching) >= st.target_count:
                        trigger = matching[st.target_count - 1]
                        completed.add((plan.plan_id, seq, "once"))
                        xp += st.xp
                else:
                    by_day = {}
                    for v in matching:
                        by_day[v[4]] = by_day.get(v[4], 0) + 1
                    progress[key] = max(by_day.values(), default=0)
                    first = None
                    for d in days:
                        if by_day.get(d, 0) >= st.target_count:
                            completed.add((plan.plan_id, seq, d))
                            xp += st.xp
                            if first is None:
                                day_acts = [v for v in matching if v[4] == d]
                                first = day_acts[st.target_count - 1]
                    trigger = first
                unlock_pos = (trigger[0], trigger[1]) if trigger else None

        result[sp] = {
            "status": status,
            "xp": xp,
            "completed": completed,
            "progress": progress,
            "unlocked": unlocked,
        }
    return result
