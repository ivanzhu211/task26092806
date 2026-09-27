# salesxp — 销售活动计分库

把销售的打电话、做演示、成单做成闯关任务：完成得 XP、升级。
纯 Python + SQLite 的库，由上层销售激励平台直接调用，无接口层、无外部服务依赖。

## 快速开始

```python
from salesxp import (
    ActivityInput, ActivityType, PlanDef, ReviewDecision,
    SalesXPService, SubTaskDef, SubTaskPeriod,
)

svc = SalesXPService("sales.db", "Asia/Shanghai")   # SQLite路径 + 公司时区(IANA)

svc.create_plan(PlanDef(
    plan_id="march", start_date="2026-03-01", duration_days=30,
    subtasks=(
        SubTaskDef(ActivityType.CALL, 50, 100, SubTaskPeriod.DAILY),
        SubTaskDef(ActivityType.CLOSE, 1, 500, SubTaskPeriod.ONCE,
                   min_amount_cents=1_000_000),
    ),
))

result = svc.submit_activities([
    ActivityInput("a-001", "alice", ActivityType.CALL,
                  "2026-03-01T09:30:00+08:00"),
])
# result["results"] 逐条给出 accepted / reason

svc.review_activity("a-002", ReviewDecision.APPROVE)   # 大额成单复核

svc.get_activity("a-001")        # 当前状态与原因
svc.get_progress("alice", "march")  # 各子任务解锁时刻/进度/已完成周期
svc.get_xp("alice")              # (XP总额, 等级)
svc.get_ledger("alice")          # XP流水明细（只追加）
```

## 规则要点

- **批量提交**：每批 ≤200 条，超过整批拒收；单条不合格只拒该条。
  同编号重复提交：内容一致幂等忽略，内容不同拒收后到的那条。
- **按天统计**：一律用初始化给定的公司时区的当地日历日（夏令时切换日即 23/25 小时）。
- **大额复核**：Close 且金额 ≥ 2,000,000 分进入待复核，期间不参与任何规则；
  通过后按原发生时间参与，驳回永不生效；结论不可改。
- **有效性**：同一销售的已生效活动按发生时间（同时刻按编号）排序，
  距上一条有效活动不足 30 秒的无效（正好 30 秒算够）；
  当天已有 500 条有效活动后的都无效。无效活动不作为间隔起点。
- **计划**：对所有销售生效，创建后不可修改；1..30 天，最多 10 个串联子任务。
  第 1 个子任务从计划开始解锁；后续子任务在前一个首次达标的那条活动之后解锁，
  只统计严格排在其后的活动。一条活动可计入多个子任务、多个计划。
- **XP**：一次性子任务达标得一次；每日子任务在计划期内每个达标日各得一次。
  流水只追加：周期由未完成变完成记加，反之记减；只是完成时刻变化不记。
- **等级**：1 级为 0；n 级（2..50）门槛为 floor(100 × 1.5^(n−2))，按精确分数计算，封顶 50 级。

## 一致性设计

每次提交/复核/建计划后，对受影响销售**全量重放**其活动（按发生时间排序），
重推有效性、解锁链、完成周期，再与流水中的完成集合做差，只记变化的 ±XP。
因此任意乱序补录、事后复核，最终状态都与"从头按时间顺序算一遍"完全一致。

## 查询说明

- `SubTaskProgress.progress`：一次性子任务为当前达标条数；每日子任务为单日最高计数。
- `SubTaskProgress.unlocked_at`：UTC ISO 字符串；`None` 表示未解锁。
- `SubTaskProgress.completed_periods`：`("once",)` 或已达标当地日期元组。

## 性能

单销售 2 万条活动、5 个计划并行时（`tests/test_perf.py` 实测）：
提交 200 条 ≈ 80ms，一次复核 ≈ 80ms，四个查询合计 < 1ms。
重放只触碰单个销售的数据，全库总量（50 万条）不影响单次操作耗时。

## 测试

```bash
python3 -m pytest tests/ -q
```

覆盖：批内/跨批乱序补录、补录撤回 XP、复核改变解锁链、跨午夜与夏令时、
高等级门槛、批量与校验规则；另有 40 个种子的随机差分测试
（随机拆批、乱序、重复提交、随机复核），与独立参考实现 `tests/reference.py` 全量对拍。
