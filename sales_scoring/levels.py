"""等级门槛计算（精确整数运算）。

n 级（2..50）门槛 = floor(100 * 1.5^(n-2)) = (100 * 3^(n-2)) // 2^(n-2)。
1 级门槛为 0。封顶 50 级。
"""

MAX_LEVEL = 50

_THRESHOLDS: list[int] | None = None


def _build_thresholds() -> list[int]:
    # 下标即等级，0 号位不用
    table = [0] * (MAX_LEVEL + 1)
    for n in range(2, MAX_LEVEL + 1):
        e = n - 2
        table[n] = (100 * 3**e) // (2**e)
    return table


def level_threshold(level: int) -> int:
    """level 级所需的最低 XP。level 取值 1..50。"""
    global _THRESHOLDS
    if not 1 <= level <= MAX_LEVEL:
        raise ValueError(f"level out of range: {level}")
    if _THRESHOLDS is None:
        _THRESHOLDS = _build_thresholds()
    return _THRESHOLDS[level]


def level_for_xp(total_xp: int) -> int:
    """按 XP 总额计算等级，封顶 50，最低 1。"""
    level = 1
    while level < MAX_LEVEL and total_xp >= level_threshold(level + 1):
        level += 1
    return level
