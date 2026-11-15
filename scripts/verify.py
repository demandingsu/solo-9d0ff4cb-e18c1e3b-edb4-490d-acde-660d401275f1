#!/usr/bin/env python3
"""端到端验证脚本：对运行中的 API + 门户执行完整补采场景。

用法：
    python3 scripts/verify.py

环境变量：
    API_BASE           API 地址（默认 http://localhost:8000）
    PORTAL_BASE        门户地址（默认 http://localhost:8001）
    PORTAL_URL_FOR_API API 访问门户所用的 URL（默认 http://portal:8000，即 compose 网络）
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")
PORTAL_BASE = os.environ.get("PORTAL_BASE", "http://localhost:8001")
PORTAL_URL = os.environ.get("PORTAL_URL_FOR_API", "http://portal:8000")
# 可选：用于重启 API 服务的命令，如 "docker compose restart api"；设置后启用重启续采测试。
RESTART_CMD = os.environ.get("RESTART_CMD")

DAY_START = "2024-01-01T00:00:00Z"
DAY_END = "2024-01-02T00:00:00Z"
HOT_SECOND = "2024-01-01T12:00:00Z"
HOUR_START = "2024-03-01T00:00:00Z"
HOUR_END = "2024-03-01T01:00:00Z"

failures = []


def req(method, base, path, body=None, query=None):
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def check(name, cond, extra=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name} {extra}")
        failures.append(name)


def section(title):
    print(f"\n== {title} ==")


def main():
    section("0. 健康检查")
    code, _ = req("GET", API_BASE, "/healthz")
    check("API 健康", code == 200)
    code, _ = req("GET", PORTAL_BASE, "/healthz")
    check("门户健康", code == 200)
    if failures:
        return summary()

    section("1. 门户复位并造数（250 条 / 24 小时）")
    code, _ = req("POST", PORTAL_BASE, "/admin/reset")
    check("门户复位", code == 200)
    code, body = req(
        "POST", PORTAL_BASE, "/admin/seed",
        {"start": DAY_START, "end": DAY_END, "count": 250, "seed": 42},
    )
    check("造数 250 条", code == 201 and body.get("inserted") == 250, body)
    code, state = req("GET", PORTAL_BASE, "/admin/state")
    check("门户状态", code == 200 and state["report_count"] == 250 and state["batch"] == "BATCH-0001", state)

    section("2. 参数校验")
    code, _ = req("POST", API_BASE, "/tasks",
                  {"portal_url": PORTAL_URL, "start": DAY_END, "end": DAY_START})
    check("起点晚于终点返回 400", code == 400)
    code, _ = req("POST", API_BASE, "/tasks",
                  {"portal_url": PORTAL_URL, "start": "2024-01-01", "end": DAY_END})
    check("非法时间格式返回 400", code == 400)
    code, _ = req("POST", API_BASE, "/tasks",
                  {"portal_url": PORTAL_URL, "start": "2024-01-01T00:00:00.5Z", "end": DAY_END})
    check("非秒级精度返回 400", code == 400)

    section("3. 创建补采任务（含二分）")
    code, task = req("POST", API_BASE, "/tasks",
                     {"portal_url": PORTAL_URL, "start": DAY_START, "end": DAY_END})
    check("任务创建", code == 201, task)
    check("任务完成", task.get("status") == "completed", task.get("status"))
    check("采集 250 条", task["progress"]["records_collected"] == 250, task["progress"])
    check("区间全部完成", task["progress"]["intervals"]["pending"] == 0
          and task["progress"]["intervals"]["unfillable"] == 0, task["progress"])
    check("发生过二分", task["progress"]["intervals"]["done"] > 1, task["progress"])
    check("批次核对一致", task.get("start_batch") == "BATCH-0001"
          and task.get("last_batch") == "BATCH-0001", task)
    check("已发布", task.get("publication", {}).get("record_count") == 250
          and task["publication"]["batch"] == "BATCH-0001", task.get("publication"))
    task1_id = task["id"]

    section("4. 结果查询")
    code, results = req("GET", API_BASE, "/results",
                        query={"portal_url": PORTAL_URL, "start": DAY_START, "end": DAY_END})
    check("结果查询 200", code == 200, results)
    records = results.get("records", [])
    check("结果 250 条", results.get("record_count") == 250 and len(records) == 250)
    in_range = all(DAY_START <= r["report_time"] < DAY_END for r in records)
    check("记录均在 [start, end) 内", in_range)
    check("字段齐全", all({"report_no", "sample_no", "report_time", "result"} <= set(r)
                        for r in records))
    code, _ = req("GET", API_BASE, "/results",
                  query={"portal_url": PORTAL_URL, "start": "2025-01-01T00:00:00Z",
                         "end": "2025-01-02T00:00:00Z"})
    check("未发布区间返回 404", code == 404)

    section("5. 区间含起点不含终点")
    t = records[100]["report_time"]
    expected = sum(1 for r in records if r["report_time"] == t)
    end_of_second = _plus_one_second(t)
    code, task = req("POST", API_BASE, "/tasks",
                     {"portal_url": PORTAL_URL, "start": t, "end": end_of_second})
    check("单秒任务完成", code == 201 and task.get("status") == "completed", task)
    check("单秒采集条数正确", task["progress"]["records_collected"] == expected,
          f"expected={expected}")
    code, results = req("GET", API_BASE, "/results",
                        query={"portal_url": PORTAL_URL, "start": t, "end": end_of_second})
    check("单秒结果均为该秒", code == 200
          and all(r["report_time"] == t for r in results["records"]))

    section("6. 查询失败与按任务号续采")
    code, _ = req("POST", PORTAL_BASE, "/admin/fail", {"times": 2})
    check("注入 2 次失败", code == 200)
    code, task = req("POST", API_BASE, "/tasks",
                     {"portal_url": PORTAL_URL, "start": DAY_START, "end": DAY_END})
    check("任务失败", code == 201 and task.get("status") == "failed", task.get("status"))
    check("错误信息记录", bool(task.get("error")), task.get("error"))
    tid = task["id"]
    code, task = req("POST", API_BASE, f"/tasks/{tid}/resume")
    check("第一次续采仍失败（第 2 次注入）", code == 200 and task.get("status") == "failed",
          task.get("status"))
    code, task = req("POST", API_BASE, f"/tasks/{tid}/resume")
    check("第二次续采成功", code == 200 and task.get("status") == "completed", task.get("status"))
    check("数据仍然完整", task["progress"]["records_collected"] == 250, task["progress"])
    code, task = req("POST", API_BASE, f"/tasks/{tid}/resume")
    check("已完成任务续采返回 409", code == 409)

    section("7. 换批：清除本轮进度并暂停，续采从原区间重采")
    code, body = req("POST", PORTAL_BASE, "/admin/switch-batch", {"after_queries": 3})
    check("计划 3 次查询后换批", code == 200, body)
    code, task = req("POST", API_BASE, "/tasks",
                     {"portal_url": PORTAL_URL, "start": DAY_START, "end": DAY_END})
    check("任务因换批暂停", code == 201 and task.get("status") == "paused", task.get("status"))
    check("本轮进度已清除", task["progress"]["records_collected"] == 0
          and sum(task["progress"]["intervals"].values()) == 0, task["progress"])
    check("提示换批", "批次" in (task.get("error") or ""), task.get("error"))
    tid = task["id"]
    code, task = req("POST", API_BASE, f"/tasks/{tid}/resume")
    check("续采后完成", code == 200 and task.get("status") == "completed", task.get("status"))
    check("从原区间重采（第 2 轮）", task.get("round") == 2, task.get("round"))
    check("重采数据完整", task["progress"]["records_collected"] == 250, task["progress"])
    code, results = req("GET", API_BASE, "/results",
                        query={"portal_url": PORTAL_URL, "start": DAY_START, "end": DAY_END})
    check("结果已整体替换为新批次", code == 200 and results.get("batch") == "BATCH-0002"
          and results.get("record_count") == 250, results.get("batch"))

    section("8. 单秒超限：报告无法补齐的区间，保留旧结果")
    code, body = req("POST", PORTAL_BASE, "/admin/seed", {"time": HOT_SECOND, "count": 150})
    check("同秒造数 150 条", code == 201 and body.get("inserted") == 150, body)
    code, task = req("POST", API_BASE, "/tasks",
                     {"portal_url": PORTAL_URL, "start": DAY_START, "end": DAY_END})
    check("任务因单秒超限标记 incomplete", code == 201 and task.get("status") == "incomplete",
          task.get("status"))
    unfillable = task.get("unfillable_intervals", [])
    check("报告无法补齐的区间", len(unfillable) == 1
          and unfillable[0]["start"] == HOT_SECOND
          and unfillable[0]["end"] == _plus_one_second(HOT_SECOND)
          and unfillable[0]["total"] >= 150, unfillable)
    code, results = req("GET", API_BASE, "/results",
                        query={"portal_url": PORTAL_URL, "start": DAY_START, "end": DAY_END})
    check("旧结果保留（未整体替换）", code == 200
          and results.get("record_count") == 250
          and results.get("batch") == "BATCH-0002", results.get("record_count"))

    section("9. 服务重启后按任务号续采")
    if not RESTART_CMD:
        print("  [SKIP] 未设置 RESTART_CMD，跳过重启续采测试")
    else:
        code, body = req("POST", PORTAL_BASE, "/admin/seed",
                         {"start": HOUR_START, "end": HOUR_END, "count": 300, "seed": 7})
        check("造数 300 条 / 1 小时", code == 201 and body.get("inserted") == 300, body)
        # 第 4 次查询时注入 1 次失败：此前已有成功子区间落库。
        code, _ = req("POST", PORTAL_BASE, "/admin/fail", {"times": 1, "after_queries": 4})
        check("计划第 4 次查询失败", code == 200)
        code, task = req("POST", API_BASE, "/tasks",
                         {"portal_url": PORTAL_URL, "start": HOUR_START, "end": HOUR_END})
        check("采集中途失败", code == 201 and task.get("status") == "failed", task.get("status"))
        check("失败前已有记录落库", task["progress"]["records_collected"] > 0, task["progress"])
        check("仍有待采区间", task["progress"]["intervals"]["pending"] > 0, task["progress"])
        before = task["progress"]["records_collected"]
        tid = task["id"]

        subprocess.run(RESTART_CMD, shell=True, check=True)
        _wait_healthy()
        code, task = req("GET", API_BASE, f"/tasks/{tid}")
        check("重启后进度仍在", code == 200 and task.get("status") == "failed"
              and task["progress"]["records_collected"] == before, task.get("status"))
        code, task = req("POST", API_BASE, f"/tasks/{tid}/resume")
        check("重启后续采完成", code == 200 and task.get("status") == "completed",
              task.get("status"))
        check("续采数据完整", task["progress"]["records_collected"] == 300, task["progress"])
        code, results = req("GET", API_BASE, "/results",
                            query={"portal_url": PORTAL_URL, "start": HOUR_START, "end": HOUR_END})
        check("结果 300 条", code == 200 and results.get("record_count") == 300)

    section("10. 进度查询")
    code, task = req("GET", API_BASE, f"/tasks/{task1_id}")
    check("按任务号查询进度", code == 200 and task.get("id") == task1_id)
    code, tasks = req("GET", API_BASE, "/tasks")
    check("任务列表", code == 200 and len(tasks) >= 5)

    return summary()


def _plus_one_second(iso_ts):
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(iso_ts).astimezone(timezone.utc) + timedelta(seconds=1)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _wait_healthy(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = req("GET", API_BASE, "/healthz")
            if code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    print("  [FAIL] API 重启后未恢复")
    failures.append("API 重启后未恢复")
    print("\n" + f"失败 {len(failures)} 项: {failures}")
    sys.exit(1)


def summary():
    print("\n" + ("全部通过 ✔" if not failures else f"失败 {len(failures)} 项: {failures}"))
    if not failures and not RESTART_CMD:
        print("\n提示：设置 RESTART_CMD 可加测服务重启后续采，例如：")
        print("  RESTART_CMD='docker compose restart api' python3 scripts/verify.py")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
