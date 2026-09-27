"""数据类型定义：输入、输出与枚举。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ActivityType(str, enum.Enum):
    CALL = "Call"
    DEMO = "Demo"
    CLOSE = "Close"
    FOLLOW_UP = "FollowUp"


class SubtaskPeriod(str, enum.Enum):
    ONCE = "once"
    DAILY = "daily"


class ReviewState(str, enum.Enum):
    NONE = "none"          # 不需要复核
    PENDING = "pending"    # 待复核
    APPROVED = "approved"  # 复核通过
    REJECTED = "rejected"  # 复核驳回


class ActivityStatus(str, enum.Enum):
    VALID = "valid"                  # 有效
    INVALID_INTERVAL = "invalid_interval"  # 距上一条有效活动不足30秒
    INVALID_DAY_CAP = "invalid_day_cap"    # 当天有效活动已达500条
    PENDING_REVIEW = "pending_review"      # 大额成单待复核
    REVIEW_REJECTED = "review_rejected"    # 复核驳回，永不生效


class ItemResult(str, enum.Enum):
    ACCEPTED = "accepted"              # 新活动，已接收
    DUPLICATE_SAME = "duplicate_same"  # 同编号同内容，幂等忽略
    REJECTED_CONFLICT = "rejected_conflict"  # 同编号内容不同
    REJECTED_INVALID = "rejected_invalid"    # 字段不合格


@dataclass(frozen=True)
class ActivityInput:
    """提交单条活动的输入。occurred_at 为带时区偏移的 ISO 8601 字符串。"""
    activity_id: str
    salesperson: str
    type: ActivityType
    occurred_at: str
    amount_cents: int
    note: str | None = None


@dataclass(frozen=True)
class SubtaskSpec:
    type: ActivityType
    target_count: int
    xp: int
    period: SubtaskPeriod
    min_amount_cents: int | None = None


@dataclass(frozen=True)
class PlanSpec:
    plan_id: str
    start_date: str  # 公司时区当地日期，YYYY-MM-DD
    duration_days: int  # 1..30
    subtasks: tuple[SubtaskSpec, ...]  # 1..10 个，依次串联


@dataclass
class BatchItemOutcome:
    activity_id: str | None
    result: ItemResult
    reason: str | None = None


@dataclass
class BatchResult:
    outcomes: list[BatchItemOutcome] = field(default_factory=list)


@dataclass
class ActivityStatusView:
    activity_id: str
    status: ActivityStatus
    reason: str | None


@dataclass
class SubtaskProgressView:
    index: int
    unlocked: bool
    unlocked_at: str | None          # ISO 时间（UTC），未解锁为 None
    progress: dict                   # once: {"count": n}; daily: {"days": {date: n}}
    completed_periods: list[str]     # "once" 或当地日期字符串


@dataclass
class PlanProgressView:
    salesperson: str
    plan_id: str
    subtasks: list[SubtaskProgressView]


@dataclass
class XpView:
    salesperson: str
    total_xp: int
    level: int


@dataclass
class LedgerEntryView:
    ledger_id: int
    salesperson: str
    plan_id: str
    subtask_index: int
    period_key: str
    amount: int
    cause: str   # 触发来源说明（批次或复核）
    created_seq: int
