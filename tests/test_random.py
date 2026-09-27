"""随机差分测试：随机拆批、乱序、重复提交、随机复核，
最终状态必须与参考实现（从头按时间顺序算一遍）完全一致。"""
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from salesxp import (
    ActivityInput, ActivityType, PlanDef, ReviewDecision,
    SalesXPService, SubTaskDef, SubTaskPeriod,
)
from salesxp.level import level_for_xp

from reference import compute

TZ = "America/New_York"
TYPES = list(ActivityType)


def _iso(rng, epoch):
    """同一时刻随机选一种偏移格式输出。"""
    choice = rng.random()
    if choice < 0.5:
        return datetime.fromtimestamp(epoch, ZoneInfo(TZ)).isoformat()
    if choice < 0.75:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    return datetime.fromtimestamp(epoch, ZoneInfo("Asia/Shanghai")).isoformat()


def gen_scenario(seed):
    rng = random.Random(seed)
    # 时间窗覆盖 2026-03-08 春拨
    t0 = datetime(2026, 3, 6, 0, 0, tzinfo=ZoneInfo(TZ)).timestamp()
    t1 = datetime(2026, 3, 12, 0, 0, tzinfo=ZoneInfo(TZ)).timestamp()

    plans = []
    for pi in range(rng.randint(1, 3)):
        start_off = rng.randint(0, 4)
        start = datetime.fromtimestamp(t0 + start_off * 86400, ZoneInfo(TZ)).date().isoformat()
        subs = []
        for si in range(rng.randint(1, 4)):
            subs.append(SubTaskDef(
                type=rng.choice(TYPES),
                target_count=rng.randint(1, 4),
                xp=rng.choice([10, 50, 100, 250, 500, 1000]),
                period=rng.choice([SubTaskPeriod.ONCE, SubTaskPeriod.DAILY]),
                min_amount_cents=rng.choice([None, None, 100_000, 1_000_000]),
            ))
        plans.append(PlanDef(f"plan{pi}", start, rng.randint(1, 5), tuple(subs)))

    salespersons = [f"s{i}" for i in range(rng.randint(1, 3))]
    activities = []
    n = rng.randint(30, 120)
    for i in range(n):
        # 30% 概率贴着上一条（触发30秒规则）
        if activities and rng.random() < 0.3:
            prev = activities[-1]
            ep = datetime.fromisoformat(prev.occurred_at).timestamp() + rng.randint(0, 90)
        else:
            ep = t0 + rng.random() * (t1 - t0)
        typ = rng.choice(TYPES)
        amount = 0
        if typ == ActivityType.CLOSE:
            amount = rng.choice([0, 500_000, 1_999_999, 2_000_000, 5_000_000])
        activities.append(ActivityInput(
            f"act{i}", rng.choice(salespersons), typ, _iso(rng, ep), amount,
        ))
    return plans, activities


def run_scenario(seed):
    plans, activities = gen_scenario(seed)
    svc = SalesXPService(":memory:", TZ)
    for p in plans:
        svc.create_plan(p)

    rng = random.Random(seed + 1_000_000)
    # 随机拆批、乱序；混入重复提交（同内容幂等 / 不同内容拒收）
    queue = list(activities)
    rng.shuffle(queue)
    first_wins = {}
    batches = []
    while queue:
        batch = []
        for _ in range(min(len(queue), rng.randint(1, 40))):
            a = queue.pop()
            batch.append(a)
            if a.activity_id not in first_wins:
                first_wins[a.activity_id] = a
        # 随机重复提交
        if first_wins and rng.random() < 0.4:
            dup = rng.choice(list(first_wins.values()))
            batch.append(dup)
        if first_wins and rng.random() < 0.2:
            dup = rng.choice(list(first_wins.values()))
            batch.append(ActivityInput(dup.activity_id, dup.salesperson, dup.type,
                                       dup.occurred_at, dup.amount_cents + 1))
        batches.append(batch)

    review_decisions = {}
    pending = []
    for batch in batches:
        svc.submit_activities(batch)
        for a in batch:
            orig = first_wins.get(a.activity_id)
            if (orig is a and a.type == ActivityType.CLOSE
                    and a.amount_cents >= 2_000_000
                    and a.activity_id not in review_decisions
                    and a.activity_id not in pending):
                pending.append(a.activity_id)
        # 随机复核一部分待审活动
        for aid in list(pending):
            if rng.random() < 0.5:
                dec = rng.choice([ReviewDecision.APPROVE, ReviewDecision.REJECT])
                svc.review_activity(aid, dec)
                review_decisions[aid] = dec.value
                pending.remove(aid)
    # 剩余待审随机留一部分不审（终态为待复核）
    for aid in list(pending):
        if rng.random() < 0.7:
            dec = rng.choice([ReviewDecision.APPROVE, ReviewDecision.REJECT])
            svc.review_activity(aid, dec)
            review_decisions[aid] = dec.value

    ref = compute(list(first_wins.values()), review_decisions, plans, TZ)

    for sp, expected in ref.items():
        # 1) 活动状态
        for aid, st in expected["status"].items():
            assert svc.get_activity(aid).status.value == st, \
                f"seed={seed} {aid}: {svc.get_activity(aid).status.value} != {st}"
        # 2) XP 与等级
        xp, level = svc.get_xp(sp)
        assert xp == expected["xp"], f"seed={seed} {sp} xp {xp} != {expected['xp']}"
        assert level == level_for_xp(expected["xp"])
        # 3) 子任务进度与完成周期
        for plan in plans:
            prog = svc.get_progress(sp, plan.plan_id)
            got_completed = set()
            for p in prog:
                key = (plan.plan_id, p.subtask_seq)
                assert p.progress == expected["progress"][key], \
                    f"seed={seed} {sp} {key} progress {p.progress} != {expected['progress'][key]}"
                assert (p.unlocked_at is None) == (expected["unlocked"][key] is None)
                for pk in p.completed_periods:
                    got_completed.add((plan.plan_id, p.subtask_seq, pk))
            want_completed = {c for c in expected["completed"] if c[0] == plan.plan_id}
            assert got_completed == want_completed, \
                f"seed={seed} {sp} {plan.plan_id} completed differ"

        # 4) 流水净额只能是 0 或该子任务 XP
        subtask_xp = {(pl.plan_id, i + 1): st.xp
                      for pl in plans for i, st in enumerate(pl.subtasks)}
        nets = {}
        for e in svc.get_ledger(sp):
            k = (e.plan_id, e.subtask_seq, e.period_key)
            nets[k] = nets.get(k, 0) + e.delta_xp
        for (pid, seq, pk), net in nets.items():
            assert net in (0, subtask_xp[(pid, seq)]), \
                f"seed={seed} {sp} {(pid, seq, pk)} net={net}"
        # 5) 流水净额集合与参考完成集合一致
        assert {k for k, v in nets.items() if v > 0} == {
            c for c in expected["completed"]
        }, f"seed={seed} {sp} ledger nets differ from reference"

    # 6) 同一次提交/复核的流水里无一加一减互相抵消
    rows = svc._conn.execute(
        "SELECT mutation_seq, salesperson, plan_id, subtask_seq, period_key, COUNT(*), "
        "SUM(delta_xp) FROM ledger "
        "GROUP BY mutation_seq, salesperson, plan_id, subtask_seq, period_key"
    ).fetchall()
    for r in rows:
        assert r[5] == 1, f"seed={seed} 同一变更内同周期多笔流水: {dict(r)}"
    svc.close()


@pytest.mark.parametrize("seed", range(40))
def test_random(seed):
    run_scenario(seed)
