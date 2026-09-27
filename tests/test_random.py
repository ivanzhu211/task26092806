"""随机测试：同一组活动和复核结论，随机拆批、随机顺序多次提交，
最终状态必须与从头按时间顺序重算的参考结果完全一致；
每个子任务每个周期的流水净额只能是 0 或该子任务的 XP。"""
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sales_scoring import (
    ActivityInput,
    ActivityType,
    PlanSpec,
    ScoringEngine,
    SubtaskPeriod,
    SubtaskSpec,
)
from sales_scoring.levels import level_for_xp

REVIEW_THRESHOLD = 2_000_000
TYPES = list(ActivityType)


# ---------- 参考实现：把最终生效的活动从头按时间顺序算一遍 ----------

def _epoch(iso: str) -> int:
    return math.floor(datetime.fromisoformat(iso).timestamp())


def _needs_review(a: ActivityInput) -> bool:
    return a.type is ActivityType.CLOSE and a.amount_cents >= REVIEW_THRESHOLD


def reference_state(activities, decisions, plans, tz_name):
    """activities: {id: ActivityInput}；decisions: {id: bool}。
    返回 {sp: {"status": {id: str}, "plans": {pid: [...]}, "xp": int}}"""
    tz = ZoneInfo(tz_name)
    by_sp = defaultdict(list)
    for a in activities.values():
        by_sp[a.salesperson].append(a)
    out = {}
    for sp, acts in by_sp.items():
        status = {}
        in_force = []
        for a in acts:
            if _needs_review(a):
                d = decisions.get(a.activity_id)
                if d is True:
                    in_force.append(a)
                    status[a.activity_id] = None  # 由重放决定
                elif d is False:
                    status[a.activity_id] = "review_rejected"
                else:
                    status[a.activity_id] = "pending_review"
            else:
                in_force.append(a)
                status[a.activity_id] = None
        in_force.sort(key=lambda a: (_epoch(a.occurred_at), a.activity_id))
        valid = []
        last_at = None
        day_counts = defaultdict(int)
        for a in in_force:
            ep = _epoch(a.occurred_at)
            day = datetime.fromtimestamp(ep, tz).date()
            if last_at is not None and ep - last_at < 30:
                status[a.activity_id] = "invalid_interval"
            elif day_counts[day] >= 500:
                status[a.activity_id] = "invalid_day_cap"
            else:
                status[a.activity_id] = "valid"
                last_at = ep
                day_counts[day] += 1
                valid.append(a)
        plan_results = {}
        xp = 0
        for plan in plans:
            start = datetime.fromisoformat(plan.start_date).date()
            end = start + timedelta(days=plan.duration_days)
            subtask_results = []
            unlock_point = None
            chained = True
            for sub in plan.subtasks:
                if not chained:
                    subtask_results.append({"unlocked": False, "periods": set(),
                                            "progress": {}})
                    continue
                matching = [
                    a for a in valid
                    if a.type is sub.type
                    and (sub.min_amount_cents is None
                         or a.amount_cents >= sub.min_amount_cents)
                    and start <= datetime.fromtimestamp(
                        _epoch(a.occurred_at), tz).date() < end
                    and (unlock_point is None
                         or (_epoch(a.occurred_at), a.activity_id) > unlock_point)
                ]
                periods = set()
                if sub.period is SubtaskPeriod.ONCE:
                    progress = {"count": len(matching)}
                    if len(matching) >= sub.target_count:
                        periods.add("once")
                        first = matching[sub.target_count - 1]
                else:
                    by_day = defaultdict(list)
                    for a in matching:
                        by_day[datetime.fromtimestamp(
                            _epoch(a.occurred_at), tz).date()].append(a)
                    progress = {"days": {d.isoformat(): len(v)
                                         for d, v in sorted(by_day.items())}}
                    first = None
                    for day, day_acts in by_day.items():
                        if len(day_acts) >= sub.target_count:
                            periods.add(day.isoformat())
                            hit = day_acts[sub.target_count - 1]
                            if first is None:
                                first = hit
                if periods:
                    xp += sub.xp * len(periods)
                    unlock_point = (_epoch(first.occurred_at), first.activity_id)
                else:
                    chained = False
                subtask_results.append({"unlocked": True, "periods": periods,
                                        "progress": progress})
            plan_results[plan.plan_id] = subtask_results
        out[sp] = {"status": status, "plans": plan_results, "xp": xp}
    return out


# ---------- 场景生成 ----------

def gen_scenario(rng):
    tz_name = rng.choice(["Asia/Shanghai", "America/New_York", "Australia/Sydney"])
    tz = ZoneInfo(tz_name)
    salespersons = ["sp1", "sp2"]
    n_plans = rng.randint(1, 2)
    plans = []
    for pi in range(n_plans):
        start = datetime(2026, 3, 5, tzinfo=tz).date() + timedelta(days=rng.randint(0, 3))
        subs = []
        for si in range(rng.randint(1, 3)):
            subs.append(SubtaskSpec(
                rng.choice(TYPES), rng.randint(1, 4), rng.choice([10, 50, 100, 200]),
                rng.choice([SubtaskPeriod.ONCE, SubtaskPeriod.DAILY]),
                rng.choice([None, None, 100_000, 500_000]),
            ))
        plans.append(PlanSpec(f"plan{pi}", start.isoformat(), rng.randint(3, 10),
                              tuple(subs)))
    # 活动：围绕若干簇中心生成，制造间隔冲突
    activities = {}
    decisions = {}
    base = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)
    centers = [base + timedelta(seconds=rng.randint(0, 8 * 86400))
               for _ in range(15)]
    offsets = [timezone.utc, timezone(timedelta(hours=8)),
               timezone(timedelta(hours=-5))]
    for i in range(rng.randint(60, 100)):
        center = rng.choice(centers)
        ts = center + timedelta(seconds=rng.randint(-90, 90))
        ts = ts.astimezone(rng.choice(offsets))
        type_ = rng.choice(TYPES)
        amount = rng.choice([0, 50_000, 300_000, 1_000_000, 2_500_000])
        a = ActivityInput(f"a{i:03d}", rng.choice(salespersons), type_,
                          ts.isoformat(), amount)
        activities[a.activity_id] = a
        if _needs_review(a):
            decisions[a.activity_id] = rng.random() < 0.6
    return tz_name, plans, activities, decisions


def run_scenario(tmp_path, name, tz_name, plans, activities, decisions, rng):
    """随机拆批、随机顺序提交；计划创建与复核插在随机位置。"""
    eng = ScoringEngine(str(tmp_path / name), tz_name)
    ops = []
    ids = list(activities)
    rng.shuffle(ids)
    # 随机拆批，部分活动重复提交（同内容，幂等）
    queue = list(ids)
    for aid in rng.sample(ids, k=len(ids) // 4):
        queue.insert(rng.randrange(len(queue) + 1), aid)
    while queue:
        take = min(len(queue), rng.randint(1, 12))
        batch = [activities[queue.pop()] for _ in range(take)]
        ops.append(("batch", batch))
    for plan in plans:
        ops.insert(rng.randrange(len(ops) + 1), ("plan", plan))
    for aid, approve in decisions.items():
        # 复核必须排在该活动最后一次提交之后
        positions = [i for i, op in enumerate(ops)
                     if op[0] == "batch" and any(a.activity_id == aid for a in op[1])]
        lo = max(positions) + 1 if positions else 0
        ops.insert(rng.randrange(lo, len(ops) + 1), ("review", (aid, approve)))
    for kind, payload in ops:
        if kind == "batch":
            eng.submit_batch(payload)
        elif kind == "plan":
            eng.create_plan(payload)
        else:
            eng.review_close(payload[0], payload[1])
    return eng


def snapshot(eng, activities, plans):
    snap = {"status": {}, "plans": {}, "xp": {}}
    for aid in activities:
        snap["status"][aid] = eng.get_activity_status(aid).status.value
    sps = sorted({a.salesperson for a in activities.values()})
    for sp in sps:
        xp = eng.get_xp(sp)
        snap["xp"][sp] = (xp.total_xp, xp.level)
        for plan in plans:
            prog = eng.get_plan_progress(sp, plan.plan_id)
            snap["plans"][(sp, plan.plan_id)] = [
                (s.unlocked, s.progress, sorted(s.completed_periods))
                for s in prog.subtasks
            ]
    return snap


def check_against_reference(eng, activities, decisions, plans, tz_name):
    ref = reference_state(activities, decisions, plans, tz_name)
    for sp, expected in ref.items():
        for aid, st in expected["status"].items():
            actual = eng.get_activity_status(aid).status.value
            assert actual == st, f"{aid}: engine={actual} ref={st}"
        xp = eng.get_xp(sp)
        assert xp.total_xp == expected["xp"], f"{sp} xp"
        assert xp.level == level_for_xp(expected["xp"])
        for plan in plans:
            prog = eng.get_plan_progress(sp, plan.plan_id)
            exp_subs = expected["plans"][plan.plan_id]
            for got, exp in zip(prog.subtasks, exp_subs):
                assert got.unlocked == exp["unlocked"]
                assert got.progress == exp["progress"]
                assert sorted(got.completed_periods) == sorted(exp["periods"])
    # 流水净额校验：每个子任务每个周期净额 ∈ {0, XP}，且与最终完成态一致
    for plan in plans:
        xp_of = {i: s.xp for i, s in enumerate(plan.subtasks)}
        nets = defaultdict(int)
        for e in eng.get_ledger():
            if e.plan_id == plan.plan_id:
                nets[(e.salesperson, e.subtask_index, e.period_key)] += e.amount
        for (sp, idx, key), net in nets.items():
            assert net in (0, xp_of[idx]), f"net {net} for {plan.plan_id}[{idx}]/{key}"
            prog = eng.get_plan_progress(sp, plan.plan_id)
            completed = key in prog.subtasks[idx].completed_periods
            assert (net == xp_of[idx]) == completed


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_eventual_consistency(tmp_path, seed):
    rng = random.Random(seed)
    tz_name, plans, activities, decisions = gen_scenario(rng)
    snaps = []
    for run in range(3):
        eng = run_scenario(tmp_path, f"run{run}.db", tz_name, plans,
                           activities, decisions, random.Random(seed * 100 + run))
        snaps.append(snapshot(eng, activities, plans))
        check_against_reference(eng, activities, decisions, plans, tz_name)
        eng.close()
    # 不同拆批/顺序的多次运行，最终状态一致
    assert snaps[0] == snaps[1] == snaps[2]
