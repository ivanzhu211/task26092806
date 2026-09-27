"""提交规则：批量上限、单条校验、重复编号。"""
import pytest

from salesxp import ActivityInput, ActivityType, SalesXPService


@pytest.fixture
def svc():
    s = SalesXPService(":memory:", "Asia/Shanghai")
    yield s
    s.close()


def mk(aid, sp="s1", ts="2026-03-01T10:00:00+08:00", **kw):
    return ActivityInput(aid, sp, ActivityType.CALL, ts, **kw)


def test_batch_over_200_rejected_whole(svc):
    acts = [mk(f"a{i}", ts=f"2026-03-01T10:{i % 60:02d}:00+08:00") for i in range(201)]
    res = svc.submit_activities(acts)
    assert res["batch_accepted"] is False
    assert all(not r["accepted"] for r in res["results"])
    assert svc.get_activity("a0") is None  # 整批拒收，一条都没进


def test_batch_exactly_200_ok(svc):
    acts = [mk(f"a{i}", ts=f"2026-03-0{1 + i // 50}T10:{i % 50:02d}:00+08:00") for i in range(200)]
    res = svc.submit_activities(acts)
    assert res["batch_accepted"] is True
    assert all(r["accepted"] for r in res["results"])


def test_single_invalid_only_rejects_that_one(svc):
    res = svc.submit_activities([
        mk("good1", ts="2026-03-01T10:00:00+08:00"),
        mk("bad", ts="not-a-date"),
        mk("good2", ts="2026-03-01T10:01:00+08:00"),
    ])
    assert res["batch_accepted"] is True
    by_id = {r["activity_id"]: r for r in res["results"]}
    assert by_id["good1"]["accepted"] and by_id["good2"]["accepted"]
    assert not by_id["bad"]["accepted"]
    assert svc.get_activity("bad") is None


def test_invalid_items(svc):
    res = svc.submit_activities([
        ActivityInput("", "s1", ActivityType.CALL, "2026-03-01T10:00:00+08:00"),   # 空编号
        ActivityInput("x1", "", ActivityType.CALL, "2026-03-01T10:00:00+08:00"),   # 空销售
        ActivityInput("x2", "s1", ActivityType.CALL, "2026-03-01T10:00:00"),       # 无时区
        ActivityInput("x3", "s1", ActivityType.CALL, "2026-03-01T10:00:00+08:00", amount_cents=-1),
        ActivityInput("x4", "s1", "Call", "2026-03-01T10:00:00+08:00"),            # 类型不是枚举
    ])
    assert all(not r["accepted"] for r in res["results"])


def test_duplicate_same_content_idempotent(svc):
    a = mk("dup", amount_cents=500, note="hi")
    svc.submit_activities([a])
    res = svc.submit_activities([a])
    assert res["results"][0]["accepted"] is True
    # 仍然只有一条
    assert svc.get_activity("dup").amount_cents == 500


def test_duplicate_different_content_rejected(svc):
    svc.submit_activities([mk("dup2", amount_cents=500)])
    res = svc.submit_activities([mk("dup2", amount_cents=600)])
    assert res["results"][0]["accepted"] is False
    assert svc.get_activity("dup2").amount_cents == 500  # 保留先到的那条


def test_duplicate_within_same_batch(svc):
    res = svc.submit_activities([
        mk("inb", amount_cents=1),
        mk("inb", amount_cents=2),  # 同批内冲突，拒后到
    ])
    assert res["results"][0]["accepted"] is True
    assert res["results"][1]["accepted"] is False
    assert svc.get_activity("inb").amount_cents == 1
