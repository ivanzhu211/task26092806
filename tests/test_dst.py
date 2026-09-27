"""跨午夜与夏令时：按公司时区的当地日历日统计，切换日23/25小时。"""
import pytest

from salesxp import (
    ActivityInput, ActivityStatus, ActivityType, PlanDef,
    SalesXPService, SubTaskDef, SubTaskPeriod,
)

NY = "America/New_York"


@pytest.fixture
def svc():
    s = SalesXPService(":memory:", NY)
    yield s
    s.close()


def call(aid, ts, sp="s1"):
    return ActivityInput(aid, sp, ActivityType.CALL, ts)


def plan(svc, start, days, target=1, period=SubTaskPeriod.DAILY):
    svc.create_plan(PlanDef(
        plan_id=f"P-{start}", start_date=start, duration_days=days,
        subtasks=(SubTaskDef(ActivityType.CALL, target, 100, period),),
    ))
    return f"P-{start}"


def test_cross_midnight_interval(svc):
    # 跨午夜但真实间隔只有20秒 -> 后一条无效
    svc.submit_activities([
        call("a1", "2026-03-01T23:59:50-05:00"),
        call("a2", "2026-03-02T00:00:10-05:00"),
    ])
    assert svc.get_activity("a1").status == ActivityStatus.VALID
    assert svc.get_activity("a2").status == ActivityStatus.INVALID_TOO_CLOSE


def test_company_tz_defines_day_not_offset(svc):
    # +08:00 的 03-01 01:00 在纽约还是 02-28 中午 -> 计入 02-28 的计划
    p = plan(svc, "2026-02-28", 1)
    svc.submit_activities([call("a1", "2026-03-01T01:00:00+08:00")])
    assert svc.get_progress("s1", p)[0].completed_periods == ("2026-02-28",)


def test_spring_forward_day_23h(svc):
    # 2026-03-08 春拨：01:59:50 EST 与 03:00:10 EDT 真实只隔20秒
    svc.submit_activities([
        call("a1", "2026-03-08T01:59:50-05:00"),
        call("a2", "2026-03-08T03:00:10-04:00"),
    ])
    assert svc.get_activity("a2").status == ActivityStatus.INVALID_TOO_CLOSE


def test_spring_forward_same_local_day(svc):
    # 切换点前后的活动同属当地 03-08，每日任务当天达标
    p = plan(svc, "2026-03-08", 1, target=2)
    svc.submit_activities([
        call("a1", "2026-03-08T01:30:00-05:00"),  # 切换前
        call("a2", "2026-03-08T03:30:00-04:00"),  # 切换后（真实隔30分钟）
    ])
    assert svc.get_progress("s1", p)[0].completed_periods == ("2026-03-08",)


def test_fall_back_day_25h(svc):
    # 2026-11-01 秋拨：两个"凌晨1点半"真实相隔1小时，同属当地 11-01
    p = plan(svc, "2026-11-01", 1, target=2)
    svc.submit_activities([
        call("a1", "2026-11-01T01:30:00-04:00"),  # EDT
        call("a2", "2026-11-01T01:30:00-05:00"),  # EST，真实晚1小时
    ])
    assert svc.get_activity("a2").status == ActivityStatus.VALID
    assert svc.get_progress("s1", p)[0].completed_periods == ("2026-11-01",)


def test_fall_back_interval_uses_real_elapsed(svc):
    # 秋拨日：01:59:50 EDT 与 01:00:10 EST 真实只隔20秒（表盘看似回拨）
    svc.submit_activities([
        call("a1", "2026-11-01T01:59:50-04:00"),
        call("a2", "2026-11-01T01:00:10-05:00"),
    ])
    assert svc.get_activity("a2").status == ActivityStatus.INVALID_TOO_CLOSE


def test_plan_last_day_boundary(svc):
    p = plan(svc, "2026-03-01", 2)  # 03-01, 03-02 两天
    svc.submit_activities([
        call("in", "2026-03-02T23:59:59-05:00"),   # 计划内最后一天
        call("out", "2026-03-03T00:00:31-05:00"),  # 计划外
    ])
    prog = svc.get_progress("s1", p)[0]
    assert prog.progress == 1
    assert prog.completed_periods == ("2026-03-02",)
