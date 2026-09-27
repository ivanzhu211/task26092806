"""跨午夜与夏令时：按公司时区当地日历日统计，切换日就是 23/25 小时。"""
from sales_scoring import (
    ActivityStatus,
    ActivityType,
    PlanSpec,
    SubtaskPeriod,
    SubtaskSpec,
)
from conftest import act

NY = "America/New_York"


def _daily_plan(pid="p", start="2026-03-07", days=3, target=1):
    return PlanSpec(pid, start, days, (
        SubtaskSpec(ActivityType.CALL, target, 10, SubtaskPeriod.DAILY),
    ))


def test_cross_midnight_local_days(make_engine):
    """23:59 与次日 00:01 分属两个当地日，各记一个完成周期。"""
    eng = make_engine(tz=NY)
    eng.create_plan(_daily_plan())
    eng.submit_batch([
        act("m1", "sp", ActivityType.CALL, "2026-03-07T23:59:00-05:00"),
        act("m2", "sp", ActivityType.CALL, "2026-03-08T00:01:00-05:00"),
    ])
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[0].completed_periods == ["2026-03-07", "2026-03-08"]
    assert eng.get_xp("sp").total_xp == 20


def test_utc_same_day_local_different(make_engine):
    """UTC 同一天，在公司时区可能分属两天。"""
    eng = make_engine(tz=NY)
    eng.create_plan(_daily_plan())
    # 都是 UTC 2026-03-08，但纽约时间分属 03-07 和 03-08
    eng.submit_batch([
        act("u1", "sp", ActivityType.CALL, "2026-03-08T03:00:00+00:00"),  # 纽约 03-07 23:00
        act("u2", "sp", ActivityType.CALL, "2026-03-08T05:00:00+00:00"),  # 纽约 03-08 00:00
    ])
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[0].completed_periods == ["2026-03-07", "2026-03-08"]


def test_spring_forward_day_is_23_hours(make_engine):
    """2026-03-08 纽约拨快 1 小时（02:00 不存在），当天 23 小时。
    切换点两侧的活动都落在同一个当地日。"""
    eng = make_engine(tz=NY)
    eng.create_plan(_daily_plan(target=2))
    eng.submit_batch([
        act("before", "sp", ActivityType.CALL, "2026-03-08T01:59:00-05:00"),
        act("after", "sp", ActivityType.CALL, "2026-03-08T03:01:00-04:00"),
    ])
    assert eng.get_activity_status("before").status is ActivityStatus.VALID
    assert eng.get_activity_status("after").status is ActivityStatus.VALID
    prog = eng.get_plan_progress("sp", "p")
    # 两条都计入 2026-03-08，目标 2 条，当天达标
    assert prog.subtasks[0].progress["days"] == {"2026-03-08": 2}
    assert prog.subtasks[0].completed_periods == ["2026-03-08"]


def test_fall_back_day_is_25_hours(make_engine):
    """2026-11-01 纽约拨回 1 小时（01:30 出现两次），当天 25 小时。
    两个 01:30 实际相隔 1 小时，都是有效活动且同属一个当地日。"""
    eng = make_engine(tz=NY)
    eng.create_plan(_daily_plan(start="2026-11-01", days=1, target=2))
    eng.submit_batch([
        act("edt", "sp", ActivityType.CALL, "2026-11-01T01:30:00-04:00"),
        act("est", "sp", ActivityType.CALL, "2026-11-01T01:30:00-05:00"),
    ])
    assert eng.get_activity_status("edt").status is ActivityStatus.VALID
    assert eng.get_activity_status("est").status is ActivityStatus.VALID
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[0].progress["days"] == {"2026-11-01": 2}
    assert prog.subtasks[0].completed_periods == ["2026-11-01"]
    assert eng.get_xp("sp").total_xp == 10


def test_plan_window_by_local_date(make_engine):
    """计划窗口按当地日期：开始日当天计入，结束后不计。"""
    eng = make_engine(tz=NY)
    eng.create_plan(_daily_plan(start="2026-03-08", days=1))
    eng.submit_batch([
        act("prev", "sp", ActivityType.CALL, "2026-03-07T23:00:00-05:00"),  # 计划前
        act("in", "sp", ActivityType.CALL, "2026-03-08T12:00:00-04:00"),    # 计划内
        act("next", "sp", ActivityType.CALL, "2026-03-09T00:30:00-04:00"),  # 计划后
    ])
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[0].completed_periods == ["2026-03-08"]
    assert eng.get_xp("sp").total_xp == 10
