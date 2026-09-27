"""SQLite 连接与建表。所有状态都落在这一个库里。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    salesperson TEXT NOT NULL,
    type TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,       -- 发生时刻，epoch 秒（UTC）
    occurred_iso TEXT NOT NULL,         -- 原始输入（含时区偏移）
    amount_cents INTEGER NOT NULL,
    note TEXT,
    review_state TEXT NOT NULL,         -- none/pending/approved/rejected
    status TEXT NOT NULL,               -- valid/invalid_*/pending_review/review_rejected
    status_reason TEXT,
    content_hash TEXT NOT NULL          -- 同编号内容一致性判断用
);
CREATE INDEX IF NOT EXISTS idx_acts_sp
    ON activities(salesperson, occurred_at, activity_id);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,           -- 公司时区当地日期 YYYY-MM-DD
    duration_days INTEGER NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subtasks (
    plan_id TEXT NOT NULL,
    idx INTEGER NOT NULL,               -- 0 起，依次串联
    type TEXT NOT NULL,
    target_count INTEGER NOT NULL,
    min_amount_cents INTEGER,
    xp INTEGER NOT NULL,
    period TEXT NOT NULL,               -- once/daily
    PRIMARY KEY (plan_id, idx)
);

CREATE TABLE IF NOT EXISTS subtask_state (
    salesperson TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    unlocked INTEGER NOT NULL,          -- 0/1
    unlocked_at INTEGER,                -- epoch 秒，未解锁为 NULL
    progress_json TEXT NOT NULL,        -- 当前进度快照
    PRIMARY KEY (salesperson, plan_id, idx)
);

CREATE TABLE IF NOT EXISTS completions (
    salesperson TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    period_key TEXT NOT NULL,           -- 'once' 或当地日期 'YYYY-MM-DD'
    completion_activity_id TEXT NOT NULL,
    completed_at INTEGER NOT NULL,      -- 达标那条活动的 occurred_at
    PRIMARY KEY (salesperson, plan_id, idx, period_key)
);

CREATE TABLE IF NOT EXISTS xp_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    salesperson TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    subtask_index INTEGER NOT NULL,
    period_key TEXT NOT NULL,
    amount INTEGER NOT NULL,            -- 正为加，负为减
    cause TEXT NOT NULL,                -- 触发来源（批次号或复核）
    created_seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_sp ON xp_ledger(salesperson);

CREATE TABLE IF NOT EXISTS xp_totals (
    salesperson TEXT PRIMARY KEY,
    total_xp INTEGER NOT NULL
);
"""


def connect(db_path: str, company_tz: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'company_tz'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('company_tz', ?)",
            (company_tz,),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('seq', '0')"
        )
        conn.commit()
    elif row["value"] != company_tz:
        conn.close()
        raise ValueError(
            f"数据库已用公司时区 {row['value']} 初始化，"
            f"不能改用 {company_tz}"
        )
    return conn


def next_seq(conn: sqlite3.Connection) -> int:
    """单调递增序号，用于流水与计划的创建顺序。"""
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'seq'")
    row = conn.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
    return int(row["value"])
