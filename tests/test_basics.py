"""基础校验：批次大小、单条校验、重复提交、间隔与日上限。"""
import pytest

from sales_scoring import (
    ActivityStatus,
    ActivityType,
    ItemResult,
    PlanSpec,
    SubtaskPeriod,
    SubtaskSpec,
)
from conftest import act

SH = "Asia/Shanghai"


def test_batch_over_200_rejected_whole(make_engine):
    eng = make_engine()
    batch = [act(f"a{i}", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00")
             for i in range(201)]
    with pytest.raises(ValueError):
        eng.submit_batch(batch)
    # 整批拒收：一条都没进库
    with pytest.raises(KeyError):
        eng.get_activity_status("a0")


def test_batch_exactly_200_accepted(make_engine):
    eng = make_engine()
    batch = [act(f"a{i}", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00")
             for i in range(200)]
    res = eng.submit_batch(batch)
    assert all(o.result is ItemResult.ACCEPTED for o in res.outcomes)


def test_invalid_item_rejected_alone(make_engine):
    eng = make_engine()
    res = eng.submit_batch([
        act("ok", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
        act("bad-type", "sp", "Email", "2026-09-02T10:00:00+08:00"),
        act("no-tz", "sp", ActivityType.CALL, "2026-09-02T10:00:00"),
        act("neg-amount", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00", -1),
        act("", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
    ])
    by_id = {o.activity_id: o for o in res.outcomes}
    assert by_id["ok"].result is ItemResult.ACCEPTED
    assert by_id["bad-type"].result is ItemResult.REJECTED_INVALID
    assert by_id["no-tz"].result is ItemResult.REJECTED_INVALID
    assert by_id["neg-amount"].result is ItemResult.REJECTED_INVALID
    assert eng.get_activity_status("ok").status is ActivityStatus.VALID
    with pytest.raises(KeyError):
        eng.get_activity_status("bad-type")


def test_duplicate_same_content_idempotent(make_engine):
    eng = make_engine()
    a = act("dup", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00", 5, "n")
    r1 = eng.submit_batch([a])
    r2 = eng.submit_batch([a])
    assert r1.outcomes[0].result is ItemResult.ACCEPTED
    assert r2.outcomes[0].result is ItemResult.DUPLICATE_SAME
    # 同一批内重复也一样
    r3 = eng.submit_batch([act("d2", "sp", ActivityType.CALL, "2026-09-03T10:00:00+08:00"),
                           act("d2", "sp", ActivityType.CALL, "2026-09-03T10:00:00+08:00")])
    assert [o.result for o in r3.outcomes] == [ItemResult.ACCEPTED, ItemResult.DUPLICATE_SAME]


def test_duplicate_conflicting_rejected(make_engine):
    eng = make_engine()
    eng.submit_batch([act("c", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00", 1)])
    res = eng.submit_batch([act("c", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00", 2)])
    assert res.outcomes[0].result is ItemResult.REJECTED_CONFLICT
    # 先到的那条保持原样
    assert eng.get_activity_status("c").status is ActivityStatus.VALID


def test_interval_30_seconds(make_engine):
    eng = make_engine()
    eng.submit_batch([
        act("t0", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
        act("t29", "sp", ActivityType.CALL, "2026-09-02T10:00:29+08:00"),
        act("t30", "sp", ActivityType.CALL, "2026-09-02T10:00:30+08:00"),
    ])
    assert eng.get_activity_status("t0").status is ActivityStatus.VALID
    assert eng.get_activity_status("t29").status is ActivityStatus.INVALID_INTERVAL
    # 正好 30 秒算够
    assert eng.get_activity_status("t30").status is ActivityStatus.VALID


def test_invalid_activity_not_interval_anchor(make_engine):
    eng = make_engine()
    eng.submit_batch([
        act("a", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
        act("b", "sp", ActivityType.CALL, "2026-09-02T10:00:10+08:00"),  # 无效
        act("c", "sp", ActivityType.CALL, "2026-09-02T10:00:30+08:00"),  # 距 b 仅20秒，但 b 不算起点，距 a 正好30秒
    ])
    assert eng.get_activity_status("b").status is ActivityStatus.INVALID_INTERVAL
    assert eng.get_activity_status("c").status is ActivityStatus.VALID


def test_day_cap_500(make_engine):
    eng = make_engine()
    base = 1_788_000_000  # 2026-09-02 00:00:00 +08:00 附近，按 31 秒间隔排 501 条
    from datetime import datetime, timezone, timedelta
    start = datetime(2026, 9, 2, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    batch = []
    for i in range(501):
        ts = (start + timedelta(seconds=31 * i)).isoformat()
        batch.append(act(f"cap{i:03d}", "sp", ActivityType.CALL, ts))
    for i in range(0, 501, 200):
        eng.submit_batch(batch[i:i + 200])
    assert eng.get_activity_status("cap499").status is ActivityStatus.VALID
    st = eng.get_activity_status("cap500")
    assert st.status is ActivityStatus.INVALID_DAY_CAP


def test_same_instant_ordered_by_id(make_engine):
    # 同一时刻按编号排：id 大的排后面，距前一条 0 秒 -> 无效
    eng = make_engine()
    eng.submit_batch([
        act("b", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
        act("a", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
    ])
    assert eng.get_activity_status("a").status is ActivityStatus.VALID
    assert eng.get_activity_status("b").status is ActivityStatus.INVALID_INTERVAL


def test_plan_validation(make_engine):
    eng = make_engine()
    sub = SubtaskSpec(ActivityType.CALL, 1, 100, SubtaskPeriod.ONCE)
    with pytest.raises(ValueError):
        eng.create_plan(PlanSpec("p", "2026-09-01", 0, (sub,)))
    with pytest.raises(ValueError):
        eng.create_plan(PlanSpec("p", "2026-09-01", 31, (sub,)))
    with pytest.raises(ValueError):
        eng.create_plan(PlanSpec("p", "2026-09-01", 1, ()))
    with pytest.raises(ValueError):
        eng.create_plan(PlanSpec("p", "2026-09-01", 1, (sub,) * 11))
    with pytest.raises(ValueError):
        eng.create_plan(PlanSpec("p", "2026-09-01", 1,
                                 (SubtaskSpec(ActivityType.CALL, 1, 9, SubtaskPeriod.ONCE),)))
    with pytest.raises(ValueError):
        eng.create_plan(PlanSpec("p", "2026-09-01", 1,
                                 (SubtaskSpec(ActivityType.CALL, 1, 1001, SubtaskPeriod.ONCE),)))
    eng.create_plan(PlanSpec("p", "2026-09-01", 1, (sub,)))
    with pytest.raises(ValueError):
        eng.create_plan(PlanSpec("p", "2026-09-01", 1, (sub,)))  # 不可修改/重建


def test_bad_company_tz():
    from sales_scoring import ScoringEngine
    with pytest.raises(ValueError):
        ScoringEngine(":memory:", "Not/AZone")
