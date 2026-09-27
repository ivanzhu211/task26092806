"""计划规则：校验、不可变、解锁链、每日周期、最低金额、多计划并行。"""
import pytest

from salesxp import (
    ActivityInput, ActivityStatus, ActivityType, PlanDef,
    SalesXPService, SubTaskDef, SubTaskPeriod,
)


@pytest.fixture
def svc():
    s = SalesXPService(":memory:", "Asia/Shanghai")
    yield s
    s.close()


def mk(aid, typ, ts, sp="s1", amount=0):
    return ActivityInput(aid, sp, typ, ts, amount_cents=amount)


def test_plan_validation(svc):
    base = dict(plan_id="P", start_date="2026-03-01", duration_days=1)
    st = SubTaskDef(ActivityType.CALL, 1, 100, SubTaskPeriod.ONCE)
    with pytest.raises(ValueError):
        svc.create_plan(PlanDef(**{**base, "duration_days": 0}, subtasks=(st,)))
    with pytest.raises(ValueError):
        svc.create_plan(PlanDef(**{**base, "duration_days": 31}, subtasks=(st,)))
    with pytest.raises(ValueError):
        svc.create_plan(PlanDef(**base, subtasks=()))  # 0个子任务
    with pytest.raises(ValueError):
        svc.create_plan(PlanDef(**base, subtasks=(st,) * 11))  # 11个子任务
    with pytest.raises(ValueError):
        svc.create_plan(PlanDef(**base, subtasks=(SubTaskDef(ActivityType.CALL, 1, 9, SubTaskPeriod.ONCE),)))
    with pytest.raises(ValueError):
        svc.create_plan(PlanDef(**base, subtasks=(SubTaskDef(ActivityType.CALL, 1, 1001, SubTaskPeriod.ONCE),)))
    with pytest.raises(ValueError):
        svc.create_plan(PlanDef(**base, subtasks=(SubTaskDef(ActivityType.CALL, 0, 100, SubTaskPeriod.ONCE),)))


def test_plan_immutable(svc):
    st = SubTaskDef(ActivityType.CALL, 1, 100, SubTaskPeriod.ONCE)
    svc.create_plan(PlanDef("P", "2026-03-01", 1, (st,)))
    with pytest.raises(ValueError):
        svc.create_plan(PlanDef("P", "2026-03-01", 1, (st,)))


def test_plan_applies_to_all_sales(svc):
    svc.create_plan(PlanDef("P", "2026-03-01", 1,
                            (SubTaskDef(ActivityType.CALL, 1, 100, SubTaskPeriod.ONCE),)))
    svc.submit_activities([mk("a1", ActivityType.CALL, "2026-03-01T10:00:00+08:00", sp="s1")])
    svc.submit_activities([mk("b1", ActivityType.CALL, "2026-03-01T10:00:00+08:00", sp="s2")])
    assert svc.get_xp("s1")[0] == 100
    assert svc.get_xp("s2")[0] == 100


def test_activity_before_plan_created_still_counts(svc):
    # 活动先于计划创建：进度只看发生时间
    svc.submit_activities([mk("a1", ActivityType.CALL, "2026-03-01T10:00:00+08:00")])
    svc.create_plan(PlanDef("P", "2026-03-01", 1,
                            (SubTaskDef(ActivityType.CALL, 1, 100, SubTaskPeriod.ONCE),)))
    assert svc.get_xp("s1")[0] == 100


def test_activity_outside_plan_period_not_counted(svc):
    svc.create_plan(PlanDef("P", "2026-03-02", 1,
                            (SubTaskDef(ActivityType.CALL, 1, 100, SubTaskPeriod.ONCE),)))
    svc.submit_activities([
        mk("before", ActivityType.CALL, "2026-03-01T23:59:59+08:00"),
        mk("after", ActivityType.CALL, "2026-03-03T00:01:00+08:00"),
    ])
    assert svc.get_xp("s1")[0] == 0


def test_min_amount_filter(svc):
    svc.create_plan(PlanDef("P", "2026-03-01", 1, (
        SubTaskDef(ActivityType.CLOSE, 1, 100, SubTaskPeriod.ONCE,
                   min_amount_cents=1_000_000),
    )))
    svc.submit_activities([
        mk("small", ActivityType.CLOSE, "2026-03-01T10:00:00+08:00", amount=999_999),
    ])
    assert svc.get_xp("s1")[0] == 0
    svc.submit_activities([
        mk("big", ActivityType.CLOSE, "2026-03-01T10:01:00+08:00", amount=1_000_000),
    ])
    assert svc.get_xp("s1")[0] == 100


def test_daily_subtask_each_day(svc):
    svc.create_plan(PlanDef("P", "2026-03-01", 3, (
        SubTaskDef(ActivityType.CALL, 1, 100, SubTaskPeriod.DAILY),
    )))
    svc.submit_activities([
        mk("d1", ActivityType.CALL, "2026-03-01T10:00:00+08:00"),
        mk("d2", ActivityType.CALL, "2026-03-03T10:00:00+08:00"),  # 03-02 没达标
    ])
    prog = svc.get_progress("s1", "P")[0]
    assert prog.completed_periods == ("2026-03-01", "2026-03-03")
    assert svc.get_xp("s1")[0] == 200


def test_unlock_chain_strictly_after(svc):
    svc.create_plan(PlanDef("P", "2026-03-01", 1, (
        SubTaskDef(ActivityType.CALL, 1, 100, SubTaskPeriod.ONCE),
        SubTaskDef(ActivityType.DEMO, 1, 200, SubTaskPeriod.ONCE),
    )))
    # demo 发生在触发子任务1的 call 之前 -> 不计入子任务2
    svc.submit_activities([
        mk("demo0", ActivityType.DEMO, "2026-03-01T09:59:00+08:00"),
        mk("call1", ActivityType.CALL, "2026-03-01T10:00:00+08:00"),
    ])
    prog = svc.get_progress("s1", "P")
    assert prog[0].completed_periods == ("once",)
    assert prog[1].progress == 0
    # 排在触发活动之后的 demo -> 计入
    svc.submit_activities([mk("demo1", ActivityType.DEMO, "2026-03-01T10:01:00+08:00")])
    assert svc.get_progress("s1", "P")[1].completed_periods == ("once",)


def test_locked_subtask_reports_none(svc):
    svc.create_plan(PlanDef("P", "2026-03-01", 1, (
        SubTaskDef(ActivityType.CALL, 5, 100, SubTaskPeriod.ONCE),
        SubTaskDef(ActivityType.DEMO, 1, 200, SubTaskPeriod.ONCE),
    )))
    svc.submit_activities([mk("a1", ActivityType.CALL, "2026-03-01T10:00:00+08:00")])
    prog = svc.get_progress("s1", "P")
    assert prog[0].unlocked_at is not None and prog[0].progress == 1
    assert prog[1].unlocked_at is None and prog[1].progress == 0


def test_multiple_plans_concurrent(svc):
    for i in range(5):
        svc.create_plan(PlanDef(f"P{i}", "2026-03-01", 1, (
            SubTaskDef(ActivityType.CALL, 1, 100, SubTaskPeriod.ONCE),
        )))
    svc.submit_activities([mk("a1", ActivityType.CALL, "2026-03-01T10:00:00+08:00")])
    # 一条活动同时计入多个计划
    assert svc.get_xp("s1")[0] == 500


def test_progress_for_salesperson_without_activities(svc):
    svc.create_plan(PlanDef("P", "2026-03-01", 1, (
        SubTaskDef(ActivityType.CALL, 1, 100, SubTaskPeriod.ONCE),
        SubTaskDef(ActivityType.DEMO, 1, 200, SubTaskPeriod.ONCE),
    )))
    prog = svc.get_progress("nobody", "P")
    # 第1个子任务从计划开始就解锁，其余未解锁
    assert prog[0].unlocked_at is not None and prog[0].progress == 0
    assert prog[1].unlocked_at is None and prog[1].progress == 0
    assert svc.get_xp("nobody") == (0, 1)


def test_progress_unknown_plan(svc):
    with pytest.raises(KeyError):
        svc.get_progress("s1", "nope")
