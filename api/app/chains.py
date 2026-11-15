"""更正链归并：把带 ``replaces_report_no`` 引用的报告解析为连续更正链。

输入为本次完整采集到的全部报告（同一门户、同一 UTC 原区间，可能来自不同
查询子区间）。解析成功时返回可发布的末端记录：

- 每条更正链只发布**末端**报告，并附带被替代的编号链（旧→新，不含末端自身）；
- 未被任何更正报告指向的报告视为有效，照常发布，其编号链为空。

以下冲突直接抛出 :class:`ChainConflictError`，消息中指出冲突编号，
调用方必须把任务标记为失败且**不得替换旧发布结果**：

- 报告编号重复；
- 更正引用的原报告在本次采集中不存在（缺失引用）；
- 同一报告被多份更正报告引用（一对多）；
- 更正关系成环。
"""
from __future__ import annotations

from typing import Optional


class ChainConflictError(Exception):
    """更正链存在冲突，任务不能发布。"""


def build_terminal_records(records: list[dict]) -> list[dict]:
    """把完整采集的记录列表归并为待发布的末端记录。

    每条输入记录至少包含 ``report_no``，可含 ``replaces_report_no``、
    ``sample_no``、``report_time``、``result``。输出保持输入中的相对顺序，
    并为每条末端记录增加 ``replaced_chain``（旧→新的被替代编号列表）。
    """
    by_no: dict[str, dict] = {}
    duplicates: list[str] = []
    edges: dict[str, Optional[str]] = {}

    for rec in records:
        no = rec["report_no"]
        if no in by_no:
            if no not in duplicates:
                duplicates.append(no)
            # 重复编号无法确定唯一节点，后续不再覆盖。
            continue
        by_no[no] = rec
        parent = rec.get("replaces_report_no")
        edges[no] = parent if parent else None

    # 缺失引用：被指向的原报告不在本次采集结果内。
    missing: list[str] = []
    incoming: dict[str, list[str]] = {}
    for child, parent in edges.items():
        if parent is None:
            continue
        if parent not in by_no:
            missing.append(f"{child} -> {parent}")
        else:
            incoming.setdefault(parent, []).append(child)

    # 一对多：同一原报告被多份更正报告引用。
    multi = [
        f"{parent} <- {', '.join(children)}"
        for parent, children in incoming.items()
        if len(children) > 1
    ]

    # 成环检测：每个节点至多一条出边，沿引用链行走即可发现环。
    cycle_nodes: set[str] = set()
    resolved: set[str] = set()
    for start in by_no:
        if start in resolved:
            continue
        path: dict[str, int] = {}
        cur: Optional[str] = start
        while cur is not None and cur in by_no and cur not in resolved:
            if cur in path:
                ring_start = path[cur]
                cycle_nodes.update(
                    no for no, idx in path.items() if idx >= ring_start
                )
                break
            path[cur] = len(path)
            cur = edges.get(cur)
        resolved.update(path)

    errors = []
    if duplicates:
        errors.append(f"报告编号重复: {', '.join(duplicates)}")
    if missing:
        errors.append(f"缺失更正引用（原报告不在本次采集区间内）: {', '.join(missing)}")
    if multi:
        errors.append(f"同一报告被多份更正报告替代: {', '.join(multi)}")
    if cycle_nodes:
        errors.append(f"更正链成环: {', '.join(sorted(cycle_nodes))}")
    if errors:
        raise ChainConflictError("；".join(errors))

    # 无冲突：没有任何入边的节点即为链末端（含未被更正的独立报告）。
    terminals = []
    for rec in records:
        no = rec["report_no"]
        if no in incoming:
            continue  # 已被更正，不发布
        chain = []
        cur = edges.get(no)
        seen = set()
        while cur is not None and cur in by_no and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = edges.get(cur)
        chain.reverse()  # 行走方向是新→旧，发布时按旧→新排列
        terminals.append({**rec, "replaced_chain": chain})
    return terminals
