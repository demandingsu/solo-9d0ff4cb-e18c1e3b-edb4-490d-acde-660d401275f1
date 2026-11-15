#!/usr/bin/env python3
"""更正链归并端到端验证：跨子区间连续更正、链末端发布、四类冲突与旧结果可读。

用法：
    API_BASE=http://localhost:8000 PORTAL_BASE=http://localhost:8001 \
    PORTAL_URL_FOR_API=http://localhost:8001 python3 scripts/verify_corrections.py
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")
PORTAL_BASE = os.environ.get("PORTAL_BASE", "http://localhost:8001")
PORTAL_URL = os.environ.get("PORTAL_URL_FOR_API", "http://portal:8000")

DAY_START = "2024-01-01T00:00:00Z"
DAY_END = "2024-01-02T00:00:00Z"
SMALL_START = "2024-02-01T00:00:00Z"
SMALL_END = "2024-02-01T01:00:00Z"

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


def create_task(start, end):
    return req("POST", API_BASE, "/tasks",
               {"portal_url": PORTAL_URL, "start": start, "end": end})


def get_results(start, end):
    return req("GET", API_BASE, "/results",
               query={"portal_url": PORTAL_URL, "start": start, "end": end})


def plus_second(iso_ts, seconds=1):
    from datetime import datetime, timedelta

    dt = datetime.fromisoformat(iso_ts) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def expect_conflict(section_name, start, end, keywords):
    """跑一次任务，预期因更正链冲突失败，且旧发布保持可读。"""
    code, old = get_results(start, end)
    check(f"{section_name}：旧结果先发布可读", code == 200 and old["record_count"] >= 1)
    old_count = old["record_count"]
    code, task = create_task(start, end)
    check(f"{section_name}：任务失败", code == 201 and task.get("status") == "failed", task.get("status"))
    err = task.get("error") or ""
    check(f"{section_name}：错误指出冲突", all(k in err for k in keywords), err)
    code, kept = get_results(start, end)
    check(f"{section_name}：旧发布结果保持可读",
          code == 200 and kept["record_count"] == old_count
          and kept["batch"] == old["batch"],
          f"{code} {kept.get('record_count')} vs {old_count}")
    return task


def main():
    section("0. 健康检查与复位")
    code, _ = req("GET", API_BASE, "/healthz")
    check("API 健康", code == 200)
    code, _ = req("GET", PORTAL_BASE, "/healthz")
    check("门户健康", code == 200)
    if failures:
        return summary()
    req("POST", PORTAL_BASE, "/admin/reset")
    code, body = req("POST", PORTAL_BASE, "/admin/seed",
                     {"start": DAY_START, "end": DAY_END, "count": 250, "seed": 42})
    check("造数 250 条", code == 201 and body.get("inserted") == 250, body)

    section("1. 无更正报告：全部视为有效")
    code, task = create_task(DAY_START, DAY_END)
    check("任务完成", code == 201 and task.get("status") == "completed", task.get("status"))
    code, results = get_results(DAY_START, DAY_END)
    check("发布 250 条", code == 200 and results["record_count"] == 250)
    check("编号链均为空", all(r["replaced_report_nos"] == [] for r in results["records"]))
    base_batch = results["batch"]

    section("2. 跨查询子区间的两跳连续更正链")
    first_no = results["records"][0]["report_no"]
    mid_no = results["records"][100]["report_no"]
    first_time = results["records"][0]["report_time"]
    mid_time = results["records"][100]["report_time"]
    # 第一跳：first_no 的更正报告，时间改到当天很靠后（与原报告落在不同子区间）。
    c1_time = "2024-01-01T23:00:00Z"
    code, c1 = req("POST", PORTAL_BASE, "/admin/correct",
                   {"replaces": first_no, "time": c1_time, "result": "更正1"})
    check("造第一跳更正", code == 201 and c1["replaces_report_no"] == first_no, c1)
    c1_no = c1["report_no"]
    # 第二跳：再更正上一份更正报告，时间再换一个子区间。
    c2_time = "2024-01-01T23:30:00Z"
    code, c2 = req("POST", PORTAL_BASE, "/admin/correct",
                   {"replaces": c1_no, "time": c2_time, "result": "更正2"})
    check("造第二跳更正", code == 201 and c2["replaces_report_no"] == c1_no, c2)
    c2_no = c2["report_no"]
    check("更正报告时间确实跨子区间", c1_time != first_time and c2_time != c1_time)
    # 单跳链：mid_no 被更正，更正报告时间改到另一秒。
    c3_time = plus_second(mid_time, 60)
    code, c3 = req("POST", PORTAL_BASE, "/admin/correct",
                   {"replaces": mid_no, "time": c3_time})
    check("造单跳更正", code == 201 and c3["replaces_report_no"] == mid_no, c3)
    c3_no = c3["report_no"]

    code, task = create_task(DAY_START, DAY_END)
    check("含更正任务完成", code == 201 and task.get("status") == "completed", task.get("error"))
    code, results = get_results(DAY_START, DAY_END)
    # 250 原报告 + 3 份更正，归并掉 3 个被替代编号 -> 250 个末端（独立报告 + 链末端）。
    check("仅发布链末端与独立报告（253-3=250）", code == 200 and results["record_count"] == 250,
          results.get("record_count"))
    by_no = {r["report_no"]: r for r in results["records"]}
    check("两跳链末端发布", c2_no in by_no and first_no not in by_no and c1_no not in by_no)
    check("两跳链编号链旧→新",
          by_no.get(c2_no, {}).get("replaced_report_nos") == [first_no, c1_no],
          by_no.get(c2_no))
    check("单跳链末端发布且链为 [原报告]",
          c3_no in by_no and mid_no not in by_no
          and by_no[c3_no]["replaced_report_nos"] == [mid_no], by_no.get(c3_no))
    check("未更正报告链为空", all(
        r["replaced_report_nos"] == []
        for r in results["records"] if r["report_no"] not in (c2_no, c3_no)
    ))
    check("批次随新发布一致", results["batch"] == base_batch)
    # 被替代报告不再出现在任何记录里。
    check("被替代编号不重复出现",
          all(no not in by_no for no in (first_no, c1_no, mid_no)))

    section("3. 冲突场景小区间基线（20 条）")
    code, body = req("POST", PORTAL_BASE, "/admin/seed",
                     {"start": SMALL_START, "end": SMALL_END, "count": 20, "seed": 7})
    check("小区间造数 20 条", code == 201 and body.get("inserted") == 20, body)
    code, task = create_task(SMALL_START, SMALL_END)
    check("小区间基线任务完成", code == 201 and task.get("status") == "completed", task.get("status"))
    code, base = get_results(SMALL_START, SMALL_END)
    check("小区间旧结果可读", code == 200 and base["record_count"] == 20)
    base_nos = [r["report_no"] for r in base["records"]]
    t0 = SMALL_START

    section("4. 缺失引用：更正指向区间内不存在的报告")
    code, raw = req("POST", PORTAL_BASE, "/admin/reports", {
        "report_no": "MISS-1", "time": plus_second(t0, 30),
        "replaces_report_no": "GHOST-999",
    })
    check("造缺失引用", code == 201 and raw["replaces_report_no"] == "GHOST-999", raw)
    expect_conflict("缺失引用", SMALL_START, SMALL_END, ["缺失", "MISS-1", "GHOST-999"])
    # 修复（删除问题报告）后续采应成功并重新发布。
    code, _ = req("DELETE", PORTAL_BASE, "/admin/reports/MISS-1")
    check("删除冲突报告", code == 200)
    code, task = create_task(SMALL_START, SMALL_END)
    check("修复后续采完成", code == 201 and task.get("status") == "completed", task.get("error"))
    code, fixed = get_results(SMALL_START, SMALL_END)
    check("修复后恢复 20 条", code == 200 and fixed["record_count"] == 20)

    section("5. 报告编号重复")
    dup_no = base_nos[0]
    code, _ = req("POST", PORTAL_BASE, "/admin/reports",
                  {"report_no": dup_no, "time": plus_second(t0, 40)})
    check("造重复编号", code == 201)
    expect_conflict("编号重复", SMALL_START, SMALL_END, ["重复", dup_no])
    code, _ = req("DELETE", PORTAL_BASE, f"/admin/reports/{urllib.parse.quote(dup_no)}")
    check("删除重复（含同号两条，随后补回一条）", code == 200)
    req("POST", PORTAL_BASE, "/admin/reports",
        {"report_no": dup_no, "sample_no": "SAM-DUP", "time": plus_second(t0, 40)})

    section("6. 同一报告被多份更正替代")
    target = base_nos[1]
    code, _ = req("POST", PORTAL_BASE, "/admin/correct",
                  {"replaces": target, "time": plus_second(t0, 41)})
    check("第一份更正", code == 201)
    code, _ = req("POST", PORTAL_BASE, "/admin/reports", {
        "report_no": "MULTI-2", "time": plus_second(t0, 42),
        "replaces_report_no": target,
    })
    check("第二份更正（同目标）", code == 201)
    expect_conflict("一对多", SMALL_START, SMALL_END, ["多份", target])

    section("7. 更正链成环")
    req("POST", PORTAL_BASE, "/admin/reports",
        {"report_no": "CYC-A", "time": plus_second(t0, 50), "replaces_report_no": "CYC-C"})
    req("POST", PORTAL_BASE, "/admin/reports",
        {"report_no": "CYC-B", "time": plus_second(t0, 51), "replaces_report_no": "CYC-A"})
    code, _ = req("POST", PORTAL_BASE, "/admin/reports",
                  {"report_no": "CYC-C", "time": plus_second(t0, 52),
                   "replaces_report_no": "CYC-B"})
    check("造成环 A->C, B->A, C->B", code == 201)
    expect_conflict("成环", SMALL_START, SMALL_END, ["环", "CYC-A", "CYC-B", "CYC-C"])

    section("8. 自引用环（单节点）")
    req("POST", PORTAL_BASE, "/admin/reports",
        {"report_no": "SELF-1", "time": plus_second(t0, 60),
         "replaces_report_no": "SELF-1"})
    expect_conflict("自引用", SMALL_START, SMALL_END, ["环", "SELF-1"])

    section("9. 冲突全量修复后重新发布")
    # MULTI-2 删除后，target 仍被第一份合法更正替代，因此是 19 个独立报告 + 1 个链末端。
    for no in ("MULTI-2", "CYC-A", "CYC-B", "CYC-C", "SELF-1"):
        req("DELETE", PORTAL_BASE, f"/admin/reports/{urllib.parse.quote(no)}")
    code, task = create_task(SMALL_START, SMALL_END)
    check("清理后任务完成", code == 201 and task.get("status") == "completed", task.get("error"))
    code, results = get_results(SMALL_START, SMALL_END)
    check("重新发布 20 个末端", code == 200 and results["record_count"] == 20)
    check("恰剩一条合法单跳链，其余链为空",
          len([r for r in results["records"] if r["replaced_report_nos"]]) == 1
          and len([r for r in results["records"] if not r["replaced_report_nos"]]) == 19,
          [(r["report_no"], r["replaced_report_nos"])
           for r in results["records"] if r["replaced_report_nos"]])
    # 再删掉最后一份更正，恢复为 20 条无链独立报告。
    code, _ = req("GET", PORTAL_BASE, "/admin/state")
    last_correct = next(r["report_no"] for r in results["records"] if r["replaced_report_nos"])
    req("DELETE", PORTAL_BASE, f"/admin/reports/{urllib.parse.quote(last_correct)}")
    code, task = create_task(SMALL_START, SMALL_END)
    check("删除更正后任务完成", code == 201 and task.get("status") == "completed", task.get("error"))
    code, results = get_results(SMALL_START, SMALL_END)
    check("恢复 20 条且链全为空",
          code == 200 and results["record_count"] == 20
          and all(r["replaced_report_nos"] == [] for r in results["records"]))

    return summary()


def summary():
    print("\n" + ("全部通过 ✔" if not failures else f"失败 {len(failures)} 项: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
