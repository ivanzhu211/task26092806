"""等级门槛：n级(2..50)门槛为 floor(100 * 1.5^(n-2))，按精确值算，封顶50级。"""
from __future__ import annotations

MAX_LEVEL = 50

# 精确计算：1.5^k = 3^k / 2^k，全部用整数，避免浮点误差。
_THRESHOLDS = [0] * (MAX_LEVEL + 1)  # _THRESHOLDS[n] 为 n 级门槛
for _n in range(2, MAX_LEVEL + 1):
    _k = _n - 2
    _THRESHOLDS[_n] = (100 * 3 ** _k) // (2 ** _k)


def level_threshold(n: int) -> int:
    """返回 n 级所需 XP 门槛（精确值）。n 范围 1..50。"""
    if not 1 <= n <= MAX_LEVEL:
        raise ValueError(f"level out of range: {n}")
    return _THRESHOLDS[n]


def level_for_xp(xp: int) -> int:
    """按 XP 总额计算等级，封顶 50。XP 为负时按 1 级。"""
    level = 1
    # 二分足够快，这里数据量小，线性也可；用二分。
    lo, hi = 1, MAX_LEVEL
    while lo <= hi:
        mid = (lo + hi) // 2
        if xp >= _THRESHOLDS[mid]:
            level = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return level
