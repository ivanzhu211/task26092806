"""复核场景：待复核不参与规则、通过/驳回、结论不可改、达标时刻提前。"""
import pytest

from sales_scoring import (
    ActivityStatus,
    ActivityType,
    PlanSpec,
    SubtaskPeriod,
    SubtaskSpec,
)
from conftest import act

BIG = 2_000_000   # 2 万美元，正好到复核线
SMALL = 1_999_999


def test_threshold_boundary(make_engine):
    """金额不低于 2 万美元的 Close 才待复核。"""
    eng = make_engine()
    eng.submit_batch([
        act("small", "sp", ActivityType.CLOSE, "2026-09-02T10:00:00+08:00", SMALL),
        act("big", "sp", ActivityType.CLOSE, "2026-09-02T10:01:00+08:00", BIG),
    ])
    assert eng.get_activity_status("small").status is ActivityStatus.VALID
    assert eng.get_activity_status("big").status is ActivityStatus.PENDING_REVIEW


def test_pending_not_counted(make_engine):
    """待复核期间不参与任何规则：不计数、不作间隔起点。"""
    eng = make_engine()
    eng.create_plan(PlanSpec("p", "2026-09-01", 30, (
        SubtaskSpec(ActivityType.CLOSE, 1, 100, SubtaskPeriod.ONCE),
    )))
    eng.submit_batch([
        act("big", "sp", ActivityType.CLOSE, "2026-09-02T10:00:00+08:00", BIG),
        act("c", "sp", ActivityType.CALL, "2026-09-02T10:00:10+08:00"),
    ])
    # 待复核的 big 不作间隔起点，c 有效
    assert eng.get_activity_status("c").status is ActivityStatus.VALID
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[0].progress["count"] == 0
    assert eng.get_xp("sp").total_xp == 0


def test_reject_never_effective(make_engine):
    eng = make_engine()
    eng.create_plan(PlanSpec("p", "2026-09-01", 30, (
        SubtaskSpec(ActivityType.CLOSE, 1, 100, SubtaskPeriod.ONCE),
    )))
    eng.submit_batch([act("big", "sp", ActivityType.CLOSE,
                          "2026-09-02T10:00:00+08:00", BIG)])
    eng.review_close("big", False)
    st = eng.get_activity_status("big")
    assert st.status is ActivityStatus.REVIEW_REJECTED
    assert eng.get_plan_progress("sp", "p").subtasks[0].progress["count"] == 0
    with pytest.raises(ValueError):
        eng.review_close("big", True)  # 结论不可改


def test_approve_counts_by_original_time(make_engine):
    """复核通过后按原发生时间参与规则，包括作间隔起点。"""
    eng = make_engine()
    eng.submit_batch([
        act("big", "sp", ActivityType.CLOSE, "2026-09-02T10:00:00+08:00", BIG),
        act("c", "sp", ActivityType.CALL, "2026-09-02T10:00:10+08:00"),
    ])
    assert eng.get_activity_status("c").status is ActivityStatus.VALID
    eng.review_close("big", True)
    assert eng.get_activity_status("big").status is ActivityStatus.VALID
    # big 生效后成为间隔起点，c 距它只有 10 秒 -> 变无效
    assert eng.get_activity_status("c").status is ActivityStatus.INVALID_INTERVAL
    with pytest.raises(ValueError):
        eng.review_close("big", False)  # 结论不可改


def test_review_unknown_or_non_pending(make_engine):
    eng = make_engine()
    eng.submit_batch([act("c", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00")])
    with pytest.raises(KeyError):
        eng.review_close("nope", True)
    with pytest.raises(ValueError):
        eng.review_close("c", True)  # 非待复核


def test_approve_moves_completion_earlier(make_engine):
    """验收场景：复核通过使前一个子任务的达标时刻提前，
    后一个子任务因此多计入活动。"""
    eng = make_engine()
    eng.create_plan(PlanSpec("p", "2026-09-01", 30, (
        SubtaskSpec(ActivityType.CLOSE, 1, 100, SubtaskPeriod.ONCE),
        SubtaskSpec(ActivityType.CALL, 2, 200, SubtaskPeriod.ONCE),
    )))
    eng.submit_batch([
        # 大额 Close 09:00，待复核
        act("big", "sp", ActivityType.CLOSE, "2026-09-02T09:00:00+08:00", BIG),
        # 小额 Close 10:00，直接生效
        act("small", "sp", ActivityType.CLOSE, "2026-09-02T10:00:00+08:00", SMALL),
        # 两通 Call：一通在 09:30（两笔 Close 之间），一通在 11:00
        act("call1", "sp", ActivityType.CALL, "2026-09-02T09:30:00+08:00"),
        act("call2", "sp", ActivityType.CALL, "2026-09-02T11:00:00+08:00"),
    ])
    # 复核前：子任务0 由 small(10:00) 达标，子任务1 只统计 10:00 之后的 Call
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[0].completed_periods == ["once"]
    assert prog.subtasks[1].progress["count"] == 1  # 只有 call2
    assert prog.subtasks[1].completed_periods == []
    assert eng.get_xp("sp").total_xp == 100

    # 复核通过：子任务0 达标时刻提前到 09:00，call1(09:30) 也计入子任务1
    eng.review_close("big", True)
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[1].progress["count"] == 2
    assert prog.subtasks[1].completed_periods == ["once"]
    assert eng.get_xp("sp").total_xp == 300
    # 子任务0 只是完成时刻提前，不重复记流水
    entries = [(e.subtask_index, e.amount) for e in eng.get_ledger("sp")]
    assert entries == [(0, 100), (1, 200)]
