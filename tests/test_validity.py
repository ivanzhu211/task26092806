"""有效性规则：30秒间隔、每日500条上限、乱序补录、补录撤回XP。"""
import pytest

from salesxp import (
    ActivityInput, ActivityStatus, ActivityType, PlanDef,
    SalesXPService, SubTaskDef, SubTaskPeriod,
)

TZ = "Asia/Shanghai"


@pytest.fixture
def svc():
    s = SalesXPService(":memory:", TZ)
    yield s
    s.close()


def call(aid, sp, ts):
    return ActivityInput(aid, sp, ActivityType.CALL, ts)


def test_interval_exactly_30s_ok(svc):
    svc.submit_activities([
        call("a1", "s1", "2026-03-01T10:00:00+08:00"),
        call("a2", "s1", "2026-03-01T10:00:30+08:00"),  # 正好30秒，算够
    ])
    assert svc.get_activity("a2").status == ActivityStatus.VALID


def test_interval_29s_invalid(svc):
    svc.submit_activities([
        call("a1", "s1", "2026-03-01T10:00:00+08:00"),
        call("a2", "s1", "2026-03-01T10:00:29+08:00"),
    ])
    assert svc.get_activity("a2").status == ActivityStatus.INVALID_TOO_CLOSE


def test_invalid_activity_not_interval_anchor(svc):
    # a2 无效（距 a1 不足30秒），a3 的间隔应从 a1 算起
    svc.submit_activities([
        call("a1", "s1", "2026-03-01T10:00:00+08:00"),
        call("a2", "s1", "2026-03-01T10:00:10+08:00"),
        call("a3", "s1", "2026-03-01T10:00:31+08:00"),
    ])
    assert svc.get_activity("a2").status == ActivityStatus.INVALID_TOO_CLOSE
    assert svc.get_activity("a3").status == ActivityStatus.VALID


def test_same_instant_ordered_by_id(svc):
    # 同一时刻按编号排序：b1 排在 a1 后，间隔0秒 -> b1 无效
    svc.submit_activities([
        call("b1", "s1", "2026-03-01T10:00:00+08:00"),
        call("a1", "s1", "2026-03-01T10:00:00+08:00"),
    ])
    assert svc.get_activity("a1").status == ActivityStatus.VALID
    assert svc.get_activity("b1").status == ActivityStatus.INVALID_TOO_CLOSE


def test_out_of_order_within_batch(svc):
    # 批内乱序：提交顺序与发生时间相反，结果应与按时间排序一致
    svc.submit_activities([
        call("a3", "s1", "2026-03-01T10:02:00+08:00"),
        call("a1", "s1", "2026-03-01T10:00:00+08:00"),
        call("a2", "s1", "2026-03-01T10:00:10+08:00"),  # 距 a1 不足30秒
    ])
    assert svc.get_activity("a1").status == ActivityStatus.VALID
    assert svc.get_activity("a2").status == ActivityStatus.INVALID_TOO_CLOSE
    assert svc.get_activity("a3").status == ActivityStatus.VALID


def test_out_of_order_across_batches(svc):
    svc.submit_activities([call("a2", "s1", "2026-03-01T10:00:20+08:00")])
    assert svc.get_activity("a2").status == ActivityStatus.VALID
    # 跨批补录一条更早的，使 a2 变成间隔不足
    svc.submit_activities([call("a1", "s1", "2026-03-01T10:00:00+08:00")])
    assert svc.get_activity("a1").status == ActivityStatus.VALID
    assert svc.get_activity("a2").status == ActivityStatus.INVALID_TOO_CLOSE


def test_backfill_revokes_xp(svc):
    """补录一条更早的活动 -> 原本的 Demo 变无效 -> 撤回已发XP。"""
    svc.create_plan(PlanDef(
        plan_id="P1", start_date="2026-03-01", duration_days=1,
        subtasks=(SubTaskDef(ActivityType.DEMO, 1, 100, SubTaskPeriod.ONCE),),
    ))
    svc.submit_activities([
        call("a1", "s1", "2026-03-01T10:00:00+08:00"),
        ActivityInput("a3", "s1", ActivityType.DEMO, "2026-03-01T10:00:40+08:00"),
    ])
    assert svc.get_xp("s1") == (100, 2)

    # 补录 a2（10:00:30，自身有效），使 Demo a3 距 a2 只有10秒 -> a3 无效 -> 子任务不再达标
    svc.submit_activities([call("a2", "s1", "2026-03-01T10:00:30+08:00")])
    assert svc.get_activity("a2").status == ActivityStatus.VALID
    assert svc.get_activity("a3").status == ActivityStatus.INVALID_TOO_CLOSE
    assert svc.get_xp("s1") == (0, 1)
    ledger = svc.get_ledger("s1")
    assert [e.delta_xp for e in ledger] == [100, -100]
    assert ledger[-1].balance_after == 0


def test_daily_cap_500(svc):
    # 当天 501 条间隔足够的活动，第 501 条无效
    acts = []
    base = 8 * 3600  # 当地 08:00 起，每 31 秒一条，501 条约 4.3 小时
    for i in range(501):
        t = base + i * 31
        ts = f"2026-03-01T{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}+08:00"
        acts.append(call(f"c{i:03d}", "s1", ts))
    for chunk in (acts[:200], acts[200:400], acts[400:]):
        svc.submit_activities(chunk)
    assert svc.get_activity("c499").status == ActivityStatus.VALID
    assert svc.get_activity("c500").status == ActivityStatus.INVALID_DAILY_CAP
    # 无效的不占名额：跨午夜后第一条有效
    svc.submit_activities([call("next-day", "s1", "2026-03-02T00:00:00+08:00")])
    assert svc.get_activity("next-day").status == ActivityStatus.VALID


def test_salespersons_isolated(svc):
    svc.submit_activities([call("a1", "s1", "2026-03-01T10:00:00+08:00")])
    svc.submit_activities([call("b1", "s2", "2026-03-01T10:00:10+08:00")])
    # 不同销售互不影响间隔
    assert svc.get_activity("b1").status == ActivityStatus.VALID
