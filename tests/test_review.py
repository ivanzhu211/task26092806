"""复核规则：大额成单待复核、通过/驳回、结论不可改、复核改变解锁链。"""
import pytest

from salesxp import (
    ActivityInput, ActivityStatus, ActivityType, PlanDef, ReviewDecision,
    SalesXPService, SubTaskDef, SubTaskPeriod,
)

BIG = 2_000_000   # 2万美元
SMALL = 1_999_999


@pytest.fixture
def svc():
    s = SalesXPService(":memory:", "Asia/Shanghai")
    yield s
    s.close()


def close(aid, sp, ts, amount):
    return ActivityInput(aid, sp, ActivityType.CLOSE, ts, amount_cents=amount)


def test_big_close_pending_review(svc):
    svc.submit_activities([close("big", "s1", "2026-03-01T10:00:00+08:00", BIG)])
    v = svc.get_activity("big")
    assert v.status == ActivityStatus.PENDING_REVIEW
    assert svc.get_xp("s1") == (0, 1)  # 待复核期间不参与任何规则


def test_small_close_no_review(svc):
    svc.submit_activities([close("small", "s1", "2026-03-01T10:00:00+08:00", SMALL)])
    assert svc.get_activity("small").status == ActivityStatus.VALID


def test_non_close_no_review(svc):
    svc.submit_activities([
        ActivityInput("c1", "s1", ActivityType.CALL, "2026-03-01T10:00:00+08:00",
                      amount_cents=BIG)
    ])
    assert svc.get_activity("c1").status == ActivityStatus.VALID


def test_review_reject_forever(svc):
    svc.submit_activities([close("big", "s1", "2026-03-01T10:00:00+08:00", BIG)])
    svc.review_activity("big", ReviewDecision.REJECT)
    assert svc.get_activity("big").status == ActivityStatus.REVIEW_REJECTED
    assert svc.get_xp("s1") == (0, 1)
    # 结论不可改
    with pytest.raises(ValueError):
        svc.review_activity("big", ReviewDecision.APPROVE)


def test_review_decision_immutable(svc):
    svc.submit_activities([close("big", "s1", "2026-03-01T10:00:00+08:00", BIG)])
    svc.review_activity("big", ReviewDecision.APPROVE)
    with pytest.raises(ValueError):
        svc.review_activity("big", ReviewDecision.REJECT)


def test_review_unknown_activity(svc):
    with pytest.raises(KeyError):
        svc.review_activity("nope", ReviewDecision.APPROVE)


def test_pending_not_counted_then_approved_counts(svc):
    """待复核期间不计数；通过后按原发生时间参与规则。"""
    svc.create_plan(PlanDef(
        plan_id="P1", start_date="2026-03-01", duration_days=1,
        subtasks=(SubTaskDef(ActivityType.CLOSE, 2, 100, SubTaskPeriod.ONCE),),
    ))
    svc.submit_activities([
        close("c1", "s1", "2026-03-01T10:00:00+08:00", 100),
        close("c2", "s1", "2026-03-01T10:01:00+08:00", BIG),
    ])
    assert svc.get_progress("s1", "P1")[0].progress == 1  # 待复核不计
    svc.review_activity("c2", ReviewDecision.APPROVE)
    assert svc.get_progress("s1", "P1")[0].progress == 2
    assert svc.get_xp("s1") == (100, 2)


def test_approve_moves_unlock_earlier(svc):
    """验收场景：复核通过使前一个子任务达标时刻提前，后一个子任务因此多计入活动。"""
    svc.create_plan(PlanDef(
        plan_id="P1", start_date="2026-03-01", duration_days=1,
        subtasks=(
            SubTaskDef(ActivityType.CLOSE, 1, 100, SubTaskPeriod.ONCE),
            SubTaskDef(ActivityType.CALL, 1, 200, SubTaskPeriod.ONCE),
        ),
    ))
    # 时间线：call1(10:00) < big(10:05,待复核) < call2(10:06) < small(10:10)
    svc.submit_activities([
        ActivityInput("call1", "s1", ActivityType.CALL, "2026-03-01T10:00:00+08:00"),
        close("big", "s1", "2026-03-01T10:05:00+08:00", BIG),
        ActivityInput("call2", "s1", ActivityType.CALL, "2026-03-01T10:06:00+08:00"),
        close("small", "s1", "2026-03-01T10:10:00+08:00", 100),
    ])
    # 待复核期间：子任务1由 small(10:10) 达标，call1/call2 都排在它之前 -> 子任务2进度0
    prog = svc.get_progress("s1", "P1")
    assert prog[0].completed_periods == ("once",)
    assert prog[1].progress == 0
    assert svc.get_xp("s1") == (100, 2)

    # 复核通过：big(10:05) 生效，子任务1达标时刻提前到10:05，
    # 排在 10:05 之后的 call2 因此被计入 -> 子任务2达标
    svc.review_activity("big", ReviewDecision.APPROVE)
    prog = svc.get_progress("s1", "P1")
    assert prog[1].progress == 1
    assert prog[1].completed_periods == ("once",)
    assert svc.get_xp("s1") == (300, 4)
