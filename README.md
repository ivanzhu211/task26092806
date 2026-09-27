# sales_scoring：销售活动计分库

Python + SQLite 的销售活动计分模块，以库的形式嵌入上层销售激励平台，
无接口层。活动、计划定义、进度与 XP 流水全部持久化在单个 SQLite 文件中。

## 快速开始

```python
from sales_scoring import (
    ScoringEngine, ActivityInput, ActivityType,
    PlanSpec, SubtaskSpec, SubtaskPeriod,
)

eng = ScoringEngine("scoring.db", "Asia/Shanghai")   # 公司时区（IANA 名称）

eng.create_plan(PlanSpec(
    plan_id="march", start_date="2026-03-01", duration_days=30,
    subtasks=(
        SubtaskSpec(ActivityType.CALL, 5, 100, SubtaskPeriod.ONCE),
        SubtaskSpec(ActivityType.DEMO, 2, 200, SubtaskPeriod.DAILY,
                    min_amount_cents=None),
    ),
))

result = eng.submit_batch([
    ActivityInput("a1", "alice", ActivityType.CALL,
                  "2026-03-02T10:00:00+08:00", 0),
])
eng.review_close("some-big-close-id", approve=True)

eng.get_activity_status("a1")        # 每条活动的当前状态与原因
eng.get_plan_progress("alice", "march")  # 各子任务解锁时刻/进度/已完成周期
eng.get_xp("alice")                  # XP 总额与等级
eng.get_ledger("alice")              # XP 流水明细
```

## API 一览

- `ScoringEngine(db_path, company_tz)`：打开（或创建）库文件；同一库文件
  再次打开时公司时区必须一致。
- `create_plan(spec) -> plan_id`：创建计划，对所有销售生效，创建后不可修改；
  进度只看活动发生时间，与计划创建时间无关。
- `submit_batch(activities) -> BatchResult`：一批最多 200 条，超过整批拒收
  （抛 `ValueError`）；单条不合格只拒该条（`ItemResult.REJECTED_INVALID`）；
  同编号同内容幂等忽略（`DUPLICATE_SAME`），内容不同拒收后到的那条
  （`REJECTED_CONFLICT`）。
- `review_close(activity_id, approve)`：复核金额 ≥ 2 万美元的 Close；
  结论作出后不可更改（重复复核抛 `ValueError`）。
- 查询：`get_activity_status` / `get_plan_progress` / `get_xp` / `get_ledger`。

## 关键规则实现

- 按天统计一律用公司时区的当地日历日（`zoneinfo`，夏令时切换日即 23/25 小时）。
- 大额 Close 待复核期间不参与任何规则；通过后按原发生时间参与；驳回永不生效。
- 同一销售的已生效活动按 `(发生时间, 编号)` 排序重放：距上一条**有效**活动
  不足 30 秒的无效（正好 30 秒算够）；当天满 500 条有效后其余无效；
  无效活动不作为间隔起点。
- 子任务串联：第 1 个从计划开始解锁，之后每个在前一个**首次达标的那条活动**
  之后解锁，只统计排在其后的活动；一条活动可计入多个子任务、多个计划。
- 任何改变已生效活动集合的操作（补录、复核、新建计划）都会对该销售全量重算，
  XP 流水只追加：某子任务某周期完成态翻转时记一笔加/减，仅完成时刻变化不记。
- 等级：n 级门槛 = `floor(100 * 1.5^(n-2))`，精确整数运算，封顶 50 级。

## 测试

```bash
python3 -m pytest tests/ -q
```

覆盖：批内/跨批乱序补录、补录导致 XP 撤回、复核使达标时刻提前、跨午夜与
夏令时、高等级门槛，以及随机化一致性测试（随机拆批乱序提交的最终状态
与从头重算完全一致，流水净额 ∈ {0, XP}）。
