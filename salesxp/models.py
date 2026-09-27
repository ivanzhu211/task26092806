"""类型定义：活动、计划、子任务、状态枚举。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ActivityType(str, Enum):
    CALL = "Call"
    DEMO = "Demo"
    CLOSE = "Close"
    FOLLOW_UP = "FollowUp"


class ActivityStatus(str, Enum):
    PENDING_REVIEW = "pending_review"      # 大额成单待复核
    REVIEW_REJECTED = "review_rejected"    # 复核驳回，永不生效
    VALID = "valid"                        # 已生效
    INVALID_TOO_CLOSE = "invalid_too_close"        # 距上一条有效活动不足30秒
    INVALID_DAILY_CAP = "invalid_daily_cap"        # 当天有效活动已达500条


class SubTaskPeriod(str, Enum):
    ONCE = "once"
    DAILY = "daily"


class ReviewDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True)
class ActivityInput:
    """提交单条活动的输入。occurred_at 为带时区偏移的 ISO 8601 字符串。"""
    activity_id: str
    salesperson: str
    type: ActivityType
    occurred_at: str
    amount_cents: int = 0
    note: Optional[str] = None


@dataclass(frozen=True)
class SubTaskDef:
    type: ActivityType
    target_count: int
    xp: int
    period: SubTaskPeriod
    min_amount_cents: Optional[int] = None


@dataclass(frozen=True)
class PlanDef:
    plan_id: str
    start_date: str          # 公司时区下的当地日期 YYYY-MM-DD
    duration_days: int       # 1..30
    subtasks: tuple          # tuple[SubTaskDef, ...]，1..10 个，依次串联


@dataclass(frozen=True)
class ActivityView:
    """活动当前状态查询结果。"""
    activity_id: str
    salesperson: str
    type: ActivityType
    occurred_at: str
    amount_cents: int
    note: Optional[str]
    status: ActivityStatus
    reason: str


@dataclass(frozen=True)
class SubTaskProgress:
    """某销售在某计划下某子任务的进度。"""
    subtask_seq: int
    unlocked_at: Optional[str]   # 解锁时刻（UTC ISO），None 表示未解锁
    progress: int                # 当前计数（一次性：达标条数；每日：最近/当日语义见实现）
    target: int
    completed_periods: tuple     # 已完成周期：once -> ("once",)，daily -> ("YYYY-MM-DD", ...)


@dataclass(frozen=True)
class LedgerEntry:
    ledger_id: int
    salesperson: str
    plan_id: str
    subtask_seq: int
    period_key: str
    delta_xp: int
    balance_after: int
    created_at: str
