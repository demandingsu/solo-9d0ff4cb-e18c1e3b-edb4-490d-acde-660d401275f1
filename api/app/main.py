"""LIMS 历史报告补采 API。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import collector, db
from .timeutil import fmt_ts, now_iso, parse_ts


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="LIMS 历史报告补采 API", version="1.0.0", lifespan=lifespan)


class TaskCreate(BaseModel):
    portal_url: str
    start: str  # 精确到秒的 UTC 时间，含起点
    end: str    # 精确到秒的 UTC 时间，不含终点


def _parse_range(start: str, end: str) -> tuple[int, int]:
    try:
        s, e = parse_ts(start), parse_ts(end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"时间参数错误: {exc}") from exc
    if s >= e:
        raise HTTPException(status_code=400, detail="起始时间必须早于结束时间")
    return s, e


def _run_locked(task_id: int) -> None:
    if not collector.collect_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有补采任务正在执行，请稍后重试")
    try:
        collector.run_collection(task_id)
    finally:
        collector.collect_lock.release()


def _task_detail(task_id: int) -> dict:
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        counts = {"pending": 0, "done": 0, "unfillable": 0}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS c FROM intervals WHERE task_id = ? AND round = ?"
            " GROUP BY status",
            (task_id, task["round"]),
        ):
            counts[row["status"]] = row["c"]
        records_collected = conn.execute(
            "SELECT COUNT(*) AS c FROM records WHERE task_id = ? AND round = ?",
            (task_id, task["round"]),
        ).fetchone()["c"]
        unfillable = [
            {"start": fmt_ts(r["start_ts"]), "end": fmt_ts(r["end_ts"]), "total": r["total"]}
            for r in conn.execute(
                "SELECT * FROM intervals WHERE task_id = ? AND round = ? AND status = 'unfillable'"
                " ORDER BY start_ts",
                (task_id, task["round"]),
            )
        ]
        pub = conn.execute(
            "SELECT * FROM publications WHERE portal_url = ? AND start_ts = ? AND end_ts = ?",
            (task["portal_url"], task["start_ts"], task["end_ts"]),
        ).fetchone()
    return {
        "id": task["id"],
        "portal_url": task["portal_url"],
        "start": fmt_ts(task["start_ts"]),
        "end": fmt_ts(task["end_ts"]),
        "status": task["status"],
        "round": task["round"],
        "start_batch": task["start_batch"],
        "last_batch": task["last_batch"],
        "error": task["error"],
        "progress": {
            "intervals": counts,
            "records_collected": records_collected,
        },
        "unfillable_intervals": unfillable,
        "publication": (
            {
                "batch": pub["batch"],
                "record_count": pub["record_count"],
                "published_at": pub["published_at"],
                "task_id": pub["task_id"],
            }
            if pub
            else None
        ),
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/tasks", status_code=201)
def create_task(body: TaskCreate):
    """创建补采任务并立即采集；失败/暂停后可凭返回的任务号续采。"""
    s, e = _parse_range(body.start, body.end)
    portal_url = body.portal_url.rstrip("/")
    if not portal_url:
        raise HTTPException(status_code=400, detail="portal_url 不能为空")
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks(portal_url, start_ts, end_ts, status, created_at, updated_at)"
            " VALUES(?,?,?,'pending',?,?)",
            (portal_url, s, e, now_iso(), now_iso()),
        )
        task_id = cur.lastrowid
    _run_locked(task_id)
    return _task_detail(task_id)


@app.post("/tasks/{task_id}/resume")
def resume_task(task_id: int):
    """按任务号续采：查询失败、服务重启、换批暂停或单秒超限后均可调用。"""
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] == "completed":
        raise HTTPException(status_code=409, detail="任务已完成，无需续采")
    _run_locked(task_id)
    return _task_detail(task_id)


@app.get("/tasks/{task_id}")
def get_task(task_id: int):
    """查询任务进度。"""
    return _task_detail(task_id)


@app.get("/tasks")
def list_tasks():
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    return [
        {
            "id": r["id"],
            "portal_url": r["portal_url"],
            "start": fmt_ts(r["start_ts"]),
            "end": fmt_ts(r["end_ts"]),
            "status": r["status"],
            "round": r["round"],
            "error": r["error"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


@app.get("/results")
def get_results(portal_url: str, start: str, end: str):
    """查询某门户某区间已发布的结果。"""
    s, e = _parse_range(start, end)
    with db.connect() as conn:
        pub = conn.execute(
            "SELECT * FROM publications WHERE portal_url = ? AND start_ts = ? AND end_ts = ?",
            (portal_url.rstrip("/"), s, e),
        ).fetchone()
        if pub is None:
            raise HTTPException(status_code=404, detail="该门户及区间暂无已发布结果")
        rows = conn.execute(
            "SELECT report_no, sample_no, report_time, result FROM published_records"
            " WHERE publication_id = ? ORDER BY report_time, report_no",
            (pub["id"],),
        ).fetchall()
    return {
        "portal_url": pub["portal_url"],
        "start": fmt_ts(pub["start_ts"]),
        "end": fmt_ts(pub["end_ts"]),
        "batch": pub["batch"],
        "record_count": pub["record_count"],
        "published_at": pub["published_at"],
        "task_id": pub["task_id"],
        "records": [
            {
                "report_no": r["report_no"],
                "sample_no": r["sample_no"],
                "report_time": fmt_ts(r["report_time"]),
                "result": r["result"],
            }
            for r in rows
        ],
    }
