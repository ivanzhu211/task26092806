"""SQLite 连接与 schema。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id     TEXT PRIMARY KEY,
    salesperson     TEXT NOT NULL,
    type            TEXT NOT NULL,
    occurred_epoch  INTEGER NOT NULL,   -- UTC 秒
    occurred_iso    TEXT NOT NULL,      -- 原始带偏移的 ISO 字符串
    amount_cents    INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    review_state    TEXT NOT NULL DEFAULT 'none',  -- none/pending/approved/rejected
    status          TEXT NOT NULL,      -- 当前计算状态
    reason          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_activities_sales_time
    ON activities (salesperson, occurred_epoch, activity_id);

CREATE TABLE IF NOT EXISTS plans (
    plan_id        TEXT PRIMARY KEY,
    start_date     TEXT NOT NULL,       -- 公司时区当地日期 YYYY-MM-DD
    duration_days  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subtasks (
    plan_id           TEXT NOT NULL,
    seq               INTEGER NOT NULL,  -- 1..10，依次串联
    type              TEXT NOT NULL,
    target_count      INTEGER NOT NULL,
    min_amount_cents  INTEGER,
    xp                INTEGER NOT NULL,
    period            TEXT NOT NULL,     -- once/daily
    PRIMARY KEY (plan_id, seq)
);

CREATE TABLE IF NOT EXISTS ledger (
    ledger_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    salesperson    TEXT NOT NULL,
    plan_id        TEXT NOT NULL,
    subtask_seq    INTEGER NOT NULL,
    period_key     TEXT NOT NULL,        -- 'once' 或当地日期
    delta_xp       INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,     -- 该销售 XP 总额（本笔之后）
    mutation_seq   INTEGER NOT NULL,     -- 第几次提交/复核
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_sales ON ledger (salesperson, ledger_id);

CREATE TABLE IF NOT EXISTS subtask_state (
    salesperson     TEXT NOT NULL,
    plan_id         TEXT NOT NULL,
    subtask_seq     INTEGER NOT NULL,
    unlocked_epoch  INTEGER,             -- NULL 表示未解锁
    progress        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (salesperson, plan_id, subtask_seq)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并初始化 schema。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('mutation_seq', 0)"
    )
    conn.commit()
    return conn


def next_mutation_seq(conn: sqlite3.Connection) -> int:
    """递增并返回全局变更序号（每次提交/复核一次）。"""
    conn.execute(
        "UPDATE meta SET value = value + 1 WHERE key = 'mutation_seq'"
    )
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'mutation_seq'"
    ).fetchone()
    return int(row["value"])
