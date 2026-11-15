"""补采核心逻辑：区间二分、进度持久化、换批处理、批次核对与原子发布。

采集在请求内同步执行，每个子区间的记录与进度在同一事务中落库，
因此查询失败或服务重启后都可以按任务号从断点续采。
"""
import threading

import httpx

from . import db
from .timeutil import fmt_ts, now_iso, parse_ts

MAX_ITEMS = 100          # 门户单次查询最多返回条数（不可翻页）
HTTP_TIMEOUT = 10.0

# 同一时刻只允许一个采集循环在跑，避免并发请求把同一任务跑乱。
collect_lock = threading.Lock()


class CollectError(Exception):
    """门户查询或响应校验失败。"""


def _query_portal(portal_url: str, start_ts: int, end_ts: int) -> dict:
    """查询门户 [start_ts, end_ts) 区间，返回 {total, batch, records}。"""
    try:
        resp = httpx.get(
            f"{portal_url}/reports",
            params={"start": fmt_ts(start_ts), "end": fmt_ts(end_ts)},
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise CollectError(f"门户查询异常: {exc}") from exc
    if resp.status_code != 200:
        raise CollectError(f"门户返回 HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
        total = int(data["total"])
        batch = str(data["batch"])
        items = list(data["items"])
    except (ValueError, KeyError, TypeError) as exc:
        raise CollectError(f"门户响应格式错误: {exc}") from exc

    records = []
    for item in items:
        try:
            ts = parse_ts(item["report_time"])
        except (KeyError, ValueError) as exc:
            raise CollectError(f"门户记录的报告时间非法: {item!r}") from exc
        if not (start_ts <= ts < end_ts):
            raise CollectError(f"门户返回了区间外的记录: {item!r}")
        records.append(
            {
                "report_no": str(item.get("report_no")),
                "sample_no": item.get("sample_no"),
                "report_time": ts,
                "result": item.get("result"),
            }
        )
    if total <= MAX_ITEMS and total != len(records):
        raise CollectError(f"门户总数({total})与返回条数({len(records)})不一致")
    return {"total": total, "batch": batch, "records": records}


def run_collection(task_id: int) -> None:
    """续采入口：根据任务当前状态恢复现场并跑采集循环。"""
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(task_id)
        if task["status"] == "completed":
            raise RuntimeError("任务已完成，无需续采")

        status = task["status"]
        round_no = task["round"]
        now = now_iso()

        if status == "paused":
            # 换批暂停：本轮进度已在暂停时清除，从原区间重新开一轮。
            round_no += 1
            conn.execute(
                "UPDATE tasks SET round = ?, start_batch = NULL, status = 'running',"
                " error = NULL, updated_at = ? WHERE id = ?",
                (round_no, now, task_id),
            )
            conn.execute(
                "INSERT INTO intervals(task_id, round, start_ts, end_ts) VALUES(?,?,?,?)",
                (task_id, round_no, task["start_ts"], task["end_ts"]),
            )
        elif status == "incomplete":
            # 重试单秒超限区间（门户数据可能已修复）。
            conn.execute(
                "UPDATE intervals SET status = 'pending', total = NULL, updated_at = ?"
                " WHERE task_id = ? AND round = ? AND status = 'unfillable'",
                (now, task_id, round_no),
            )
            conn.execute(
                "UPDATE tasks SET status = 'running', error = NULL, updated_at = ? WHERE id = ?",
                (now, task_id),
            )
        else:
            # pending / failed / running（崩溃残留）：沿用已保存的进度继续。
            conn.execute(
                "UPDATE tasks SET status = 'running', error = NULL, updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM intervals WHERE task_id = ? AND round = ?",
                (task_id, round_no),
            ).fetchone()["c"]
            if cnt == 0:
                conn.execute(
                    "INSERT INTO intervals(task_id, round, start_ts, end_ts) VALUES(?,?,?,?)",
                    (task_id, round_no, task["start_ts"], task["end_ts"]),
                )

    _collect_loop(task_id)


def _collect_loop(task_id: int) -> None:
    while True:
        with db.connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            interval = conn.execute(
                "SELECT * FROM intervals WHERE task_id = ? AND round = ? AND status = 'pending'"
                " ORDER BY start_ts LIMIT 1",
                (task_id, task["round"]),
            ).fetchone()
        if interval is None:
            break
        try:
            resp = _query_portal(task["portal_url"], interval["start_ts"], interval["end_ts"])
        except CollectError as exc:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                    (str(exc), now_iso(), task_id),
                )
            return
        if _handle_response(task_id, task["round"], interval, resp) == "paused":
            return
    _finalize(task_id)


def _handle_response(task_id: int, round_no: int, interval, resp: dict) -> str:
    """处理一次门户响应：批次核对、记录落库或区间二分。整个处理是一个事务。"""
    now = now_iso()
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        start_batch = task["start_batch"]

        if start_batch is None:
            # 本轮第一次响应：登记开始核对批次。
            conn.execute(
                "UPDATE tasks SET start_batch = ?, last_batch = ?, updated_at = ? WHERE id = ?",
                (resp["batch"], resp["batch"], now, task_id),
            )
        elif resp["batch"] != start_batch:
            # 换批：清除本轮进度并暂停，下次续采从原区间重采。
            conn.execute("DELETE FROM intervals WHERE task_id = ? AND round = ?", (task_id, round_no))
            conn.execute("DELETE FROM records WHERE task_id = ? AND round = ?", (task_id, round_no))
            conn.execute(
                "UPDATE tasks SET status = 'paused', last_batch = ?, error = ?, updated_at = ?"
                " WHERE id = ?",
                (
                    resp["batch"],
                    f"发布批次由 {start_batch} 变为 {resp['batch']}，本轮进度已清除，任务暂停",
                    now,
                    task_id,
                ),
            )
            return "paused"
        else:
            conn.execute(
                "UPDATE tasks SET last_batch = ?, updated_at = ? WHERE id = ?",
                (resp["batch"], now, task_id),
            )

        total = resp["total"]
        s, e = interval["start_ts"], interval["end_ts"]
        if total > MAX_ITEMS:
            if e - s <= 1:
                # 单秒仍超限：无法继续二分，记为无法补齐的区间。
                conn.execute(
                    "UPDATE intervals SET status = 'unfillable', total = ?, updated_at = ?"
                    " WHERE id = ?",
                    (total, now, interval["id"]),
                )
            else:
                mid = s + (e - s) // 2
                conn.execute("DELETE FROM intervals WHERE id = ?", (interval["id"],))
                conn.execute(
                    "INSERT INTO intervals(task_id, round, start_ts, end_ts) VALUES(?,?,?,?)",
                    (task_id, round_no, s, mid),
                )
                conn.execute(
                    "INSERT INTO intervals(task_id, round, start_ts, end_ts) VALUES(?,?,?,?)",
                    (task_id, round_no, mid, e),
                )
        else:
            # 成功子区间：记录与进度同一事务落库。
            conn.executemany(
                "INSERT INTO records(task_id, round, report_no, sample_no, report_time, result)"
                " VALUES(?,?,?,?,?,?)",
                [
                    (task_id, round_no, r["report_no"], r["sample_no"], r["report_time"], r["result"])
                    for r in resp["records"]
                ],
            )
            conn.execute(
                "UPDATE intervals SET status = 'done', total = ?, updated_at = ? WHERE id = ?",
                (total, now, interval["id"]),
            )
    return "ok"


def _finalize(task_id: int) -> None:
    """所有子区间处理完毕：检查覆盖完整性，做结束批次核对，原子发布。"""
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        round_no = task["round"]
        unfillable = conn.execute(
            "SELECT COUNT(*) AS c FROM intervals"
            " WHERE task_id = ? AND round = ? AND status = 'unfillable'",
            (task_id, round_no),
        ).fetchone()["c"]

    if unfillable:
        # 未完整覆盖原区间：不发布，保留旧结果。
        with db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'incomplete', error = ?, updated_at = ? WHERE id = ?",
                (f"存在 {unfillable} 个单秒超限区间无法补齐，未发布结果", now_iso(), task_id),
            )
        return

    # 结束核对：再次查询原区间，确认批次与开始核对一致。
    try:
        resp = _query_portal(task["portal_url"], task["start_ts"], task["end_ts"])
    except CollectError as exc:
        with db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (f"结束批次核对失败: {exc}", now_iso(), task_id),
            )
        return

    now = now_iso()
    with db.connect() as conn:
        if resp["batch"] != task["start_batch"]:
            # 结束核对发现换批：清除本轮进度并暂停。
            conn.execute("DELETE FROM intervals WHERE task_id = ? AND round = ?", (task_id, round_no))
            conn.execute("DELETE FROM records WHERE task_id = ? AND round = ?", (task_id, round_no))
            conn.execute(
                "UPDATE tasks SET status = 'paused', last_batch = ?, error = ?, updated_at = ?"
                " WHERE id = ?",
                (
                    resp["batch"],
                    f"结束核对时发现换批 {task['start_batch']} -> {resp['batch']}，"
                    "本轮进度已清除，任务暂停",
                    now,
                    task_id,
                ),
            )
            return

        # 完整覆盖且批次一致：整体替换该门户及区间的已发布结果（单事务，失败保留旧结果）。
        rows = conn.execute(
            "SELECT report_no, sample_no, report_time, result FROM records"
            " WHERE task_id = ? AND round = ? ORDER BY report_time, report_no",
            (task_id, round_no),
        ).fetchall()
        old = conn.execute(
            "SELECT id FROM publications WHERE portal_url = ? AND start_ts = ? AND end_ts = ?",
            (task["portal_url"], task["start_ts"], task["end_ts"]),
        ).fetchone()
        if old is not None:
            conn.execute("DELETE FROM published_records WHERE publication_id = ?", (old["id"],))
            conn.execute("DELETE FROM publications WHERE id = ?", (old["id"],))
        cur = conn.execute(
            "INSERT INTO publications(portal_url, start_ts, end_ts, batch, record_count,"
            " task_id, published_at) VALUES(?,?,?,?,?,?,?)",
            (
                task["portal_url"],
                task["start_ts"],
                task["end_ts"],
                resp["batch"],
                len(rows),
                task_id,
                now,
            ),
        )
        publication_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO published_records(publication_id, report_no, sample_no, report_time,"
            " result) VALUES(?,?,?,?,?)",
            [
                (publication_id, r["report_no"], r["sample_no"], r["report_time"], r["result"])
                for r in rows
            ],
        )
        conn.execute(
            "UPDATE tasks SET status = 'completed', last_batch = ?, error = NULL, updated_at = ?"
            " WHERE id = ?",
            (resp["batch"], now, task_id),
        )
