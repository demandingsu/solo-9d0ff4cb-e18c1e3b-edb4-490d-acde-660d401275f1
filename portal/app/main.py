"""本地模拟门户（无需账号）。

查询接口模拟真实限制：按报告时间检索，单次最多返回 100 条，不能翻页。
管理接口用于演示/测试：造数、换批、注入查询失败、制造同秒超限。
"""
import random
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db
from .timeutil import fmt_ts, parse_ts

MAX_ITEMS = 100  # 单次查询最多返回条数


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="本地报告门户（模拟）", version="1.0.0", lifespan=lifespan)


def _parse_range(start: str, end: str) -> tuple[int, int]:
    try:
        s, e = parse_ts(start), parse_ts(end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"时间参数错误: {exc}") from exc
    if s >= e:
        raise HTTPException(status_code=400, detail="起始时间必须早于结束时间")
    return s, e


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/reports")
def query_reports(start: str, end: str):
    """按报告时间检索 [start, end)：返回总数、发布批次和最多 100 条记录。"""
    s, e = _parse_range(start, end)

    # 计划失败：计数归零时激活注入失败。
    # 故障注入：接下来若干次查询直接失败。
    with db.connect() as conn:
        fail_after = int(db.get_meta(conn, "fail_after"))
        if fail_after > 0:
            fail_after -= 1
            db.set_meta(conn, "fail_after", str(fail_after))
            if fail_after == 0:
                db.set_meta(conn, "fail_remaining", db.get_meta(conn, "fail_pending"))
                db.set_meta(conn, "fail_pending", "0")
        fail_remaining = int(db.get_meta(conn, "fail_remaining"))
        if fail_remaining > 0:
            db.set_meta(conn, "fail_remaining", str(fail_remaining - 1))
    if fail_remaining > 0:
        raise HTTPException(status_code=500, detail="门户查询失败（故障注入）")

    # 计划换批：计数归零时，本次响应起使用新批次。
    with db.connect() as conn:
        switch_after = int(db.get_meta(conn, "switch_after"))
        if switch_after > 0:
            switch_after -= 1
            db.set_meta(conn, "switch_after", str(switch_after))
            if switch_after == 0:
                db.switch_batch(conn)

    with db.connect() as conn:
        batch = db.get_meta(conn, "batch")
        rows = conn.execute(
            "SELECT report_no, sample_no, report_time, result, replaces_report_no"
            " FROM reports"
            " WHERE report_time >= ? AND report_time < ?"
            " ORDER BY report_time, report_no",
            (s, e),
        ).fetchall()

    items = rows[:MAX_ITEMS]
    return {
        "batch": batch,
        "total": len(rows),
        "returned": len(items),
        "items": [
            {
                "report_no": r["report_no"],
                "sample_no": r["sample_no"],
                "report_time": fmt_ts(r["report_time"]),
                "result": r["result"],
                "replaces_report_no": r["replaces_report_no"],
            }
            for r in items
        ],
    }


# ---------------- 管理接口（演示/测试用，无需账号） ----------------


class SeedBody(BaseModel):
    start: Optional[str] = None   # 与 end 搭配：在 [start, end) 内随机造数
    end: Optional[str] = None
    time: Optional[str] = None    # 单独使用：全部记录落在同一秒（制造同秒超限）
    count: int = Field(gt=0)
    seed: Optional[int] = None    # 随机种子，便于复现


@app.post("/admin/seed", status_code=201)
def seed_reports(body: SeedBody):
    if body.time is not None:
        try:
            fixed_ts = parse_ts(body.time)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"时间参数错误: {exc}") from exc
        range_ts = None
    elif body.start is not None and body.end is not None:
        range_ts = _parse_range(body.start, body.end)
        fixed_ts = None
    else:
        raise HTTPException(status_code=400, detail="需提供 time，或 start 和 end")

    rng = random.Random(body.seed)
    rows = []
    with db.connect() as conn:
        seq = int(db.get_meta(conn, "seq"))
        for _ in range(body.count):
            seq += 1
            ts = fixed_ts if range_ts is None else rng.randrange(range_ts[0], range_ts[1])
            rows.append(
                (f"RPT-{seq:06d}", f"SAM-{seq:06d}", ts, rng.choice(["合格", "不合格"]))
            )
        conn.executemany(
            "INSERT INTO reports(report_no, sample_no, report_time, result) VALUES(?,?,?,?)",
            rows,
        )
        db.set_meta(conn, "seq", str(seq))
    return {"inserted": body.count}


class CorrectBody(BaseModel):
    replaces: str                          # 被替代的原报告编号
    report_no: Optional[str] = None        # 更正报告编号，缺省自动生成
    sample_no: Optional[str] = None        # 缺省沿用原报告样品号
    result: Optional[str] = None           # 缺省沿用原报告结果
    time: Optional[str] = None             # 缺省沿用原报告时间；指定后可落在不同查询子区间
    allow_missing: bool = False            # True 时允许原报告不存在（用于造缺失引用）


@app.post("/admin/correct", status_code=201)
def add_correction(body: CorrectBody):
    """针对已存在报告造一份更正报告，replaces_report_no 指向原报告。

    指定 ``time`` 可让更正报告落在与原报告不同的查询子区间，验证补采 API
    跨子区间的更正链归并。门户不做更正链合法性校验，冲突由补采 API 检测。
    """
    try:
        fixed_ts = parse_ts(body.time) if body.time is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"时间参数错误: {exc}") from exc

    with db.connect() as conn:
        seq = int(db.get_meta(conn, "seq"))
        original = conn.execute(
            "SELECT * FROM reports WHERE report_no = ? ORDER BY id LIMIT 1",
            (body.replaces,),
        ).fetchone()
        if original is None and not body.allow_missing:
            raise HTTPException(status_code=404, detail=f"原报告不存在: {body.replaces}")
        report_no = body.report_no
        if report_no is None:
            seq += 1
            report_no = f"RPT-{seq:06d}"
            db.set_meta(conn, "seq", str(seq))
        sample_no = body.sample_no or (original["sample_no"] if original else f"SAM-{report_no}")
        result = body.result if body.result is not None else (
            original["result"] if original else "合格"
        )
        report_time = fixed_ts if fixed_ts is not None else (
            original["report_time"] if original else 0
        )
        conn.execute(
            "INSERT INTO reports(report_no, sample_no, report_time, result,"
            " replaces_report_no) VALUES(?,?,?,?,?)",
            (report_no, sample_no, report_time, result, body.replaces),
        )
    return {
        "report_no": report_no,
        "replaces_report_no": body.replaces,
        "report_time": fmt_ts(report_time),
    }


class RawReportBody(BaseModel):
    report_no: str
    sample_no: str = "SAM-MANUAL"
    time: str
    result: str = "合格"
    replaces_report_no: Optional[str] = None  # 空/缺省表示普通报告


@app.post("/admin/reports", status_code=201)
def add_raw_report(body: RawReportBody):
    """插入任意原始报告（允许重复编号、成环、缺失引用等异常数据）。

    用于构造补采 API 必须拒绝的更正链冲突；门户本身不校验引用合法性。
    """
    try:
        report_time = parse_ts(body.time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"时间参数错误: {exc}") from exc
    replaces = body.replaces_report_no.strip() if body.replaces_report_no else None
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO reports(report_no, sample_no, report_time, result,"
            " replaces_report_no) VALUES(?,?,?,?,?)",
            (body.report_no, body.sample_no, report_time, body.result, replaces),
        )
    return {
        "report_no": body.report_no,
        "replaces_report_no": replaces,
        "report_time": fmt_ts(report_time),
    }


class SwitchBatchBody(BaseModel):
    after_queries: int = Field(default=0, ge=0)  # >0：再响应 N 次查询后自动换批
@app.post("/admin/switch-batch")
def switch_batch(body: SwitchBatchBody):
    """换批：默认立即切换发布批次；after_queries>0 时按计划自动切换。"""
    with db.connect() as conn:
        if body.after_queries > 0:
            db.set_meta(conn, "switch_after", str(body.after_queries))
            batch = db.get_meta(conn, "batch")
        else:
            db.set_meta(conn, "switch_after", "0")
            batch = db.switch_batch(conn)
    return {"batch": batch, "after_queries": body.after_queries}


class FailBody(BaseModel):
    times: int = Field(ge=0)                    # 注入失败的次数，0 表示关闭
    after_queries: int = Field(default=0, ge=0)  # >0：再响应 N 次查询后才触发


@app.post("/admin/fail")
def inject_failure(body: FailBody):
    """注入查询失败：默认接下来的 times 次 /reports 查询返回 500；
    after_queries>0 时先正常响应 N 次再触发（模拟采集中途故障）。"""
    if body.after_queries > 0 and body.times == 0:
        raise HTTPException(status_code=400, detail="after_queries>0 时 times 必须大于 0")
    with db.connect() as conn:
        if body.after_queries > 0:
            db.set_meta(conn, "fail_after", str(body.after_queries))
            db.set_meta(conn, "fail_pending", str(body.times))
            db.set_meta(conn, "fail_remaining", "0")
        else:
            db.set_meta(conn, "fail_after", "0")
            db.set_meta(conn, "fail_pending", "0")
            db.set_meta(conn, "fail_remaining", str(body.times))
    return {"fail_remaining": body.times, "after_queries": body.after_queries}


@app.delete("/admin/reports/{report_no}")
def delete_report(report_no: str):
    """按编号删除报告（删除重复编号时一并删除同号记录），用于冲突修复演示。"""
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM reports WHERE report_no = ?", (report_no,))
    return {"deleted": cur.rowcount, "report_no": report_no}


@app.post("/admin/reset")
def reset():
    """清空报告数据并复位批次与故障注入。"""
    with db.connect() as conn:
        conn.execute("DELETE FROM reports")
        for key, value in db.DEFAULTS.items():
            db.set_meta(conn, key, value)
    return {"reset": True}


@app.get("/admin/state")
def admin_state():
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"]
        return {
            "batch": db.get_meta(conn, "batch"),
            "report_count": count,
            "fail_remaining": int(db.get_meta(conn, "fail_remaining")),
            "fail_after": int(db.get_meta(conn, "fail_after")),
            "switch_after": int(db.get_meta(conn, "switch_after")),
        }
