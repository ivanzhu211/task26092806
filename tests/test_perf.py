"""性能冒烟：单销售2万条活动、5个计划，提交200条/一次复核 < 2s，查询 < 100ms。
（全库50万条按 25 个销售 x 2万条 的规模在慢速文件库上抽验，此处用内存库验证单销售规模。）
"""
import time
from datetime import datetime, timedelta, timezone

import pytest

from salesxp import (
    ActivityInput, ActivityType, PlanDef, ReviewDecision,
    SalesXPService, SubTaskDef, SubTaskPeriod,
)


def _build_service(n_activities=20_000, n_plans=5):
    svc = SalesXPService(":memory:", "Asia/Shanghai")
    for i in range(n_plans):
        svc.create_plan(PlanDef(
            f"P{i}", "2026-03-01", 30,
            tuple(
                SubTaskDef(ActivityType.CALL, 50 + j * 10, 100, SubTaskPeriod.DAILY)
                for j in range(3)
            ),
        ))
    base = datetime(2026, 3, 1, tzinfo=timezone(timedelta(hours=8)))
    acts = []
    for i in range(n_activities):
        ts = (base + timedelta(seconds=31 * i)).isoformat()
        acts.append(ActivityInput(f"a{i:06d}", "s1", ActivityType.CALL, ts))
    t0 = time.perf_counter()
    for off in range(0, n_activities, 200):
        svc.submit_activities(acts[off:off + 200])
    build_s = time.perf_counter() - t0
    return svc, build_s


def test_submit_200_under_2s():
    svc, build_s = _build_service()
    print(f"\n构建2万条(100批)耗时: {build_s:.2f}s")
    base = datetime(2026, 4, 1, tzinfo=timezone(timedelta(hours=8)))
    batch = [
        ActivityInput(f"new{i:03d}", "s1", ActivityType.CALL,
                      (base + timedelta(seconds=31 * i)).isoformat())
        for i in range(200)
    ]
    t0 = time.perf_counter()
    svc.submit_activities(batch)
    elapsed = time.perf_counter() - t0
    print(f"已有2万条时提交200条: {elapsed:.3f}s")
    assert elapsed < 2.0
    svc.close()


def test_review_under_2s():
    svc, _ = _build_service()
    svc.submit_activities([
        ActivityInput("big", "s1", ActivityType.CLOSE,
                      "2026-03-01T00:00:00+08:00", amount_cents=5_000_000)
    ])
    t0 = time.perf_counter()
    svc.review_activity("big", ReviewDecision.APPROVE)
    elapsed = time.perf_counter() - t0
    print(f"已有2万条时一次复核: {elapsed:.3f}s")
    assert elapsed < 2.0
    svc.close()


def test_queries_under_100ms():
    svc, _ = _build_service()
    t0 = time.perf_counter()
    svc.get_xp("s1")
    svc.get_progress("s1", "P0")
    svc.get_ledger("s1")
    svc.get_activity("a000100")
    elapsed = time.perf_counter() - t0
    print(f"四个查询合计: {elapsed * 1000:.1f}ms")
    assert elapsed < 0.1
    svc.close()
