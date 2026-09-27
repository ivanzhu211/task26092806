"""补录场景：批内乱序、跨批乱序、补录导致有效性变化并撤回 XP。"""
from sales_scoring import (
    ActivityStatus,
    ActivityType,
    PlanSpec,
    SubtaskPeriod,
    SubtaskSpec,
)
from conftest import act


def _plan_calls2():
    return PlanSpec("p", "2026-09-01", 30, (
        SubtaskSpec(ActivityType.CALL, 2, 100, SubtaskPeriod.ONCE),
    ))


def test_out_of_order_within_batch(make_engine):
    """批内乱序：后发生的先出现，判定结果应与按时间顺序一致。"""
    eng = make_engine()
    eng.create_plan(_plan_calls2())
    eng.submit_batch([
        act("late", "sp", ActivityType.CALL, "2026-09-02T10:01:00+08:00"),
        act("early", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
    ])
    assert eng.get_activity_status("early").status is ActivityStatus.VALID
    assert eng.get_activity_status("late").status is ActivityStatus.VALID
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[0].completed_periods == ["once"]
    assert eng.get_xp("sp").total_xp == 100


def test_out_of_order_across_batches(make_engine):
    """跨批乱序：第一批到的晚活动先占坑，补录早活动后重新判定。"""
    eng = make_engine()
    eng.create_plan(_plan_calls2())
    # 先到的是 10:00:20
    eng.submit_batch([act("b", "sp", ActivityType.CALL, "2026-09-02T10:00:20+08:00")])
    assert eng.get_activity_status("b").status is ActivityStatus.VALID
    # 补录 10:00:00：b 距它只有 20 秒，变成无效
    eng.submit_batch([act("a", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00")])
    assert eng.get_activity_status("a").status is ActivityStatus.VALID
    assert eng.get_activity_status("b").status is ActivityStatus.INVALID_INTERVAL
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[0].progress["count"] == 1
    assert prog.subtasks[0].completed_periods == []
    assert eng.get_xp("sp").total_xp == 0


def test_backfill_revokes_xp(make_engine):
    """补录一条更早的其他类型活动，使原本有效的 Call 变成间隔不足，
    子任务由完成变回未完成，撤回已发的 XP。"""
    eng = make_engine()
    eng.create_plan(_plan_calls2())
    eng.submit_batch([
        act("c1", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
        act("c2", "sp", ActivityType.CALL, "2026-09-02T10:00:40+08:00"),
    ])
    assert eng.get_xp("sp").total_xp == 100
    assert eng.get_plan_progress("sp", "p").subtasks[0].completed_periods == ["once"]

    # 补录 10:00:35 的 Demo：c2 距它只有 5 秒 -> 无效 -> Call 只剩 1 条
    eng.submit_batch([act("d1", "sp", ActivityType.DEMO, "2026-09-02T10:00:35+08:00")])
    assert eng.get_activity_status("d1").status is ActivityStatus.VALID
    assert eng.get_activity_status("c2").status is ActivityStatus.INVALID_INTERVAL
    prog = eng.get_plan_progress("sp", "p")
    assert prog.subtasks[0].progress["count"] == 1
    assert prog.subtasks[0].completed_periods == []
    assert eng.get_xp("sp").total_xp == 0

    # 流水：一加一减两笔，净额 0
    ledger = eng.get_ledger("sp")
    assert [e.amount for e in ledger] == [100, -100]
    assert all(e.period_key == "once" for e in ledger)


def test_backfill_restores_xp_again(make_engine):
    """撤回之后再补一条更早的有效 Call，子任务重新完成，再次加 XP。"""
    eng = make_engine()
    eng.create_plan(_plan_calls2())
    eng.submit_batch([
        act("c1", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
        act("c2", "sp", ActivityType.CALL, "2026-09-02T10:00:40+08:00"),
    ])
    eng.submit_batch([act("d1", "sp", ActivityType.DEMO, "2026-09-02T10:00:35+08:00")])
    assert eng.get_xp("sp").total_xp == 0
    # 再补一条 09:59:00 的 Call：有效 Call 变回 2 条
    eng.submit_batch([act("c0", "sp", ActivityType.CALL, "2026-09-02T09:59:00+08:00")])
    assert eng.get_xp("sp").total_xp == 100
    ledger = eng.get_ledger("sp")
    assert [e.amount for e in ledger] == [100, -100, 100]


def test_completion_time_change_no_ledger(make_engine):
    """只是完成时刻变了（仍完成），不记流水。"""
    eng = make_engine()
    eng.create_plan(_plan_calls2())
    eng.submit_batch([
        act("c1", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
        act("c2", "sp", ActivityType.CALL, "2026-09-02T10:01:00+08:00"),
    ])
    assert eng.get_xp("sp").total_xp == 100
    # 补一条 10:00:31 的 Call：达标那条从 c2 提前到 c3，但周期仍是完成态
    eng.submit_batch([act("c3", "sp", ActivityType.CALL, "2026-09-02T10:00:31+08:00")])
    assert eng.get_xp("sp").total_xp == 100
    ledger = eng.get_ledger("sp")
    assert len(ledger) == 1  # 只有最初那一笔
