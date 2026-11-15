"""门户 SQLite 存储：报告数据 + 运行状态（批次、故障注入、换批计划）。"""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/portal.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_no   TEXT NOT NULL,             -- 报告编号
    sample_no   TEXT NOT NULL,             -- 样品号
    report_time INTEGER NOT NULL,          -- 报告时间，epoch 秒
    result      TEXT NOT NULL              -- 结果
);
CREATE INDEX IF NOT EXISTS idx_reports_time ON reports(report_time);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULTS = {
    "batch": "BATCH-0001",     # 当前发布批次
    "fail_remaining": "0",     # 接下来多少次查询注入失败
    "fail_after": "0",         # 再响应多少次查询后触发注入失败（0=不计划）
    "fail_pending": "0",       # 触发时注入的失败次数
    "switch_after": "0",       # 再响应多少次查询后自动换批（0=不计划）
    "seq": "0",                # 报告编号序列
}


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)", (key, value)
            )


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_meta(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"]


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def switch_batch(conn) -> str:
    current = get_meta(conn, "batch")
    number = int(current.split("-")[1]) + 1
    new_batch = f"BATCH-{number:04d}"
    set_meta(conn, "batch", new_batch)
    return new_batch
