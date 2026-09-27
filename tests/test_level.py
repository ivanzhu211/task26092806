"""等级门槛：精确计算，含高等级。"""
from salesxp.level import level_for_xp, level_threshold


def test_low_thresholds():
    assert level_threshold(1) == 0
    assert level_threshold(2) == 100
    assert level_threshold(3) == 150
    assert level_threshold(4) == 225
    assert level_threshold(5) == 337
    assert level_threshold(6) == 506   # floor(100*1.5^4)=floor(506.25)
    assert level_threshold(7) == 759   # floor(759.375)


def test_exact_boundary():
    for n in range(1, 51):
        t = level_threshold(n)
        assert level_for_xp(t) == n
        if n > 1:
            assert level_for_xp(t - 1) == n - 1


def test_high_levels_exact():
    # 用分数精确验证：threshold(n) = floor(100 * 3^(n-2) / 2^(n-2))
    from fractions import Fraction
    for n in (20, 30, 40, 49, 50):
        k = n - 2
        exact = Fraction(100 * 3 ** k, 2 ** k)
        assert level_threshold(n) == exact.numerator // exact.denominator
        # 浮点直接算在高等级会失真，这里确认整数算法与精确分数一致
        assert level_threshold(n) == int(exact)


def test_cap_at_50():
    assert level_for_xp(level_threshold(50)) == 50
    assert level_for_xp(level_threshold(50) * 1000) == 50


def test_negative_xp_level_1():
    assert level_for_xp(-100) == 1
