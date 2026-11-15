"""SQLite 持久化层：任务、区间进度、暂存记录、已发布结果。"""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/api.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_url  TEXT NOT NULL,
    start_ts    INTEGER NOT NULL,          -- 原始区间起点（含），epoch 秒
    end_ts      INTEGER NOT NULL,          -- 原始区间终点（不含），epoch 秒
    status      TEXT NOT NULL,             -- pending/running/paused/failed/incomplete/completed
    round       INTEGER NOT NULL DEFAULT 1,-- 采集轮次，换批重采时 +1
    start_batch TEXT,                      -- 本轮开始核对到的发布批次
    last_batch  TEXT,                      -- 最近一次响应中的发布批次
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intervals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id),
    round      INTEGER NOT NULL,
    start_ts   INTEGER NOT NULL,           -- 子区间起点（含）
    end_ts     INTEGER NOT NULL,           -- 子区间终点（不含）
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending/done/unfillable
    total      INTEGER,                    -- 门户返回的区间内总数
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_intervals_task ON intervals(task_id, round, status);

CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    round       INTEGER NOT NULL,
    report_no   TEXT NOT NULL,             -- 报告编号
    sample_no   TEXT,                      -- 样品号
    report_time INTEGER NOT NULL,          -- 报告时间，epoch 秒
    result      TEXT                       -- 结果
);
CREATE INDEX IF NOT EXISTS idx_records_task ON records(task_id, round);

CREATE TABLE IF NOT EXISTS publications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_url   TEXT NOT NULL,
    start_ts     INTEGER NOT NULL,
    end_ts       INTEGER NOT NULL,
    batch        TEXT NOT NULL,            -- 发布时核对一致的批次
    record_count INTEGER NOT NULL,
    task_id      INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(portal_url, start_ts, end_ts)   -- 同一门户同一区间只有一份已发布结果
);

CREATE TABLE IF NOT EXISTS published_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    report_no      TEXT NOT NULL,
    sample_no      TEXT,
    report_time    INTEGER NOT NULL,
    result         TEXT
);
CREATE INDEX IF NOT EXISTS idx_published_records_pub ON published_records(publication_id);
"""


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    """一个 with 块即一个事务：正常结束提交，异常回滚。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
