"""等级门槛：精确整数运算，封顶 50 级。"""
from sales_scoring import ActivityType, PlanSpec, SubtaskPeriod, SubtaskSpec
from sales_scoring.levels import MAX_LEVEL, level_for_xp, level_threshold
from conftest import act


def test_low_thresholds():
    assert level_threshold(1) == 0
    assert level_threshold(2) == 100
    assert level_threshold(3) == 150
    assert level_threshold(4) == 225
    assert level_threshold(5) == 337  # floor(337.5)


def test_thresholds_exact_and_monotonic():
    prev = -1
    for n in range(1, MAX_LEVEL + 1):
        t = level_threshold(n)
        assert t > prev
        prev = t
    # 与精确分数比较：threshold(n) = floor(100 * 3^(n-2) / 2^(n-2))
    from fractions import Fraction
    for n in range(2, MAX_LEVEL + 1):
        exact = Fraction(100 * 3 ** (n - 2), 2 ** (n - 2))
        assert level_threshold(n) == exact.numerator // exact.denominator


def test_high_level_boundaries():
    # 50 级门槛：floor(100 * 1.5^48) = 28338733342
    t50 = level_threshold(50)
    assert t50 == 28338733342
    assert level_for_xp(t50 - 1) == 49
    assert level_for_xp(t50) == 50
    assert level_for_xp(t50 * 1000) == 50  # 封顶
    assert level_for_xp(0) == 1
    assert level_for_xp(-50) == 1  # 撤回导致负 XP 时最低 1 级


def test_level_for_xp_mid_levels():
    assert level_for_xp(99) == 1
    assert level_for_xp(100) == 2
    assert level_for_xp(149) == 2
    assert level_for_xp(150) == 3
    assert level_for_xp(224) == 3
    assert level_for_xp(225) == 4
    assert level_for_xp(336) == 4
    assert level_for_xp(337) == 5


def test_xp_total_drives_level(make_engine):
    """集成：XP 总额变化（含撤回）实时反映到等级。"""
    eng = make_engine()
    eng.create_plan(PlanSpec("p", "2026-09-01", 30, (
        SubtaskSpec(ActivityType.CALL, 1, 100, SubtaskPeriod.ONCE),
        SubtaskSpec(ActivityType.DEMO, 1, 50, SubtaskPeriod.ONCE),
    )))
    eng.submit_batch([
        act("c", "sp", ActivityType.CALL, "2026-09-02T10:00:00+08:00"),
        act("d", "sp", ActivityType.DEMO, "2026-09-02T11:00:00+08:00"),
    ])
    xp = eng.get_xp("sp")
    assert (xp.total_xp, xp.level) == (150, 3)
    # 撤回 Demo 的 50：d 因补录变得间隔不足
    eng.submit_batch([act("x", "sp", ActivityType.FOLLOW_UP,
                          "2026-09-02T10:59:50+08:00")])
    xp = eng.get_xp("sp")
    assert (xp.total_xp, xp.level) == (100, 2)
