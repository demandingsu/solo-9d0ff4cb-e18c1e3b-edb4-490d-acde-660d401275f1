# LIMS 历史报告补采系统

检验机构数据员把门户上的历史报告补录进 LIMS 的补采服务。门户按报告时间检索、
单次最多返回 100 条且不能翻页，本系统通过**时间区间二分**完成补采，支持失败续采、
换批重采、批次核对后的原子发布，以及**更正链归并**（门户另发的更正报告用
`replaces_report_no` 指向原报告，可跨查询子区间）。

## 组件

| 组件 | 说明 | 端口 |
| --- | --- | --- |
| `api` | 补采 API（FastAPI + SQLite）：补采、续采、进度、结果查询 | 8000 |
| `portal` | 本地模拟门户（FastAPI + SQLite，无需账号）：报告检索 + 故障/换批/造数管理接口 | 8001 |

```
├── docker-compose.yml      # 一键启动 api + portal
├── api/                    # 补采 API
│   └── app/{main,db,collector,chains,timeutil}.py
├── portal/                 # 本地模拟门户
│   └── app/{main,db,timeutil}.py
└── scripts/
    ├── verify.py             # 端到端验证脚本（基础场景，仅依赖标准库）
    └── verify_corrections.py # 更正链归并端到端验证
```

## 快速开始

```bash
docker compose up --build -d
```

启动后：

- API 文档：<http://localhost:8000/docs>
- 门户地址（API 容器视角）：`http://portal:8000`；宿主机视角：`http://localhost:8001`

## 端到端验证

```bash
# 基础场景（约 50 项断言）
python3 scripts/verify.py

# 更正链场景（跨子区间连续更正、四类冲突、旧结果可读）
python3 scripts/verify_corrections.py

# 加测“服务重启后按任务号续采”
RESTART_CMD='docker compose restart api' python3 scripts/verify.py
```

脚本覆盖：参数校验、区间二分、[start, end) 语义、查询失败续采、换批暂停重采、
单秒超限上报、旧结果保留、重启续采、进度与结果查询，以及更正链归并、冲突检测
（缺失引用、编号重复、一对多、成环）和原子发布。

## API 接口

### 创建补采任务（补采入口）

```bash
curl -X POST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"portal_url": "http://portal:8000",
       "start": "2024-01-01T00:00:00Z",
       "end":   "2024-01-02T00:00:00Z"}'
```

- 时间精确到秒（UTC），区间**含起点、不含终点**。
- 采集在请求内同步执行，响应即任务最终/当前状态；失败、暂停后可凭 `id` 续采。

### 续采

```bash
curl -X POST http://localhost:8000/tasks/1/resume
```

查询失败、服务重启、换批暂停（`paused`）、单秒超限（`incomplete`）后均可调用；
已完成任务返回 409。

### 进度查询

```bash
curl http://localhost:8000/tasks/1
```

返回状态、轮次、本轮区间统计（pending/done/unfillable）、已采记录数、
无法补齐的区间清单、开始/最近批次、发布信息等。

### 结果查询

```bash
curl 'http://localhost:8000/results?portal_url=http://portal:8000&start=2024-01-01T00:00:00Z&end=2024-01-02T00:00:00Z'
```

返回该门户该区间当前已发布的批次与全部记录；未发布返回 404。每条记录为一条
更正链的**末端**或未被更正的独立报告，并附 `replaced_report_nos`：

- 普通报告：`"replaced_report_nos": []`；
- 链末端：按**旧→新**列出被它替代的全部编号（不含末端自身），例如
  `RPT-A` 被 `RPT-B` 更正、`RPT-B` 又被 `RPT-C` 更正时，`RPT-C` 的链为
  `["RPT-A", "RPT-B"]`，而 `RPT-A`、`RPT-B` 不再出现在结果中。

更正报告演示流程：

```bash
# 1) 门户：对已采集的 RPT-000001 发更正报告，时间改到另一个子区间
curl -X POST http://localhost:8001/admin/correct -H 'Content-Type: application/json' \
  -d '{"replaces": "RPT-000001", "time": "2024-01-01T23:00:00Z"}'
# 2) API：重新补采同一区间，更正链在完整采集后归并并整体发布
curl -X POST http://localhost:8000/tasks -H 'Content-Type: application/json' \
  -d '{"portal_url": "http://portal:8000",
       "start": "2024-01-01T00:00:00Z", "end": "2024-01-02T00:00:00Z"}'
# 3) /results 中原报告消失，更正报告作为链末端附 replaced_report_nos
```

## 门户接口（模拟，无需账号）

| 接口 | 说明 |
| --- | --- |
| `GET /reports?start=&end=` | 返回 `{batch, total, items}`，`items` 最多 100 条，含 `replaces_report_no` |
| `POST /admin/seed` | 造数：`{start,end,count,seed}` 区间内随机；`{time,count}` 同秒超限 |
| `POST /admin/correct` | 造更正报告：`{replaces, time?}`，`replaces_report_no` 指向原报告；指定 `time` 可落在不同子区间 |
| `POST /admin/reports` | 插入任意原始报告（可造重复编号、成环、缺失引用等冲突数据） |
| `DELETE /admin/reports/{report_no}` | 按编号删除报告（用于冲突修复演示） |
| `POST /admin/switch-batch` | 换批：`{}` 立即换；`{"after_queries": N}` 第 N 次查询起生效 |
| `POST /admin/fail` | 注入失败：`{"times": N}` 接下来 N 次查询 500；`after_queries` 可延迟触发 |
| `POST /admin/reset` | 清空数据并复位批次/故障注入 |
| `GET /admin/state` | 查看批次、数据量、故障与换批计划 |

## 核心设计

- **半开区间二分**：原始区间 `[start, end)`；门户返回 `total > 100` 时把区间对半分
  继续查询，直到每个子区间 `total ≤ 100` 才落库记录。区间宽度降到 1 秒仍超限时，
  记为**无法补齐的区间**（`unfillable`），在任务详情中上报。
- **进度持久化**：每个子区间的记录与进度在**同一事务**落库，因此查询失败或服务
  重启（含采集中途崩溃）后，可按任务号从断点续采，不重复、不丢失。
- **换批处理**：本轮首次响应登记**开始核对批次**；任一后续响应批次不同即判定换批
  ——清除本轮全部进度并暂停（`paused`），下次续采轮次 +1、从原区间重新采集。
- **批次核对与原子发布**：所有子区间完成后再次查询原区间做**结束核对**；只有
  完整覆盖原区间（无 unfillable）且开始、结束批次一致，才在**单个事务**里删除并
  替换该门户及区间的已发布结果——任何失败都保留旧结果。
- **更正链归并**：门户的更正报告通过 `replaces_report_no` 指向原报告，引用随
  子区间记录一起暂存（两者可落在不同查询子区间）。完整采集后，在同一门户及
  UTC 原区间内解析连续更正：每条链只发布**末端**报告并附旧→新的被替代编号链，
  未被更正的报告链为空、照常有效。出现以下任一冲突时任务标记 `failed` 且错误
  信息点名冲突编号，**不发布、不替换旧结果**，修复门户数据后可凭任务号续采：
  - 缺失引用：被指向的原报告不在本次采集结果内；
  - 报告编号重复；
  - 同一报告被多份更正报告替代（一对多）；
  - 更正关系成环（含自引用）。

  报告、编号链与发布批次在**同一事务**写入，`/results` 只会读到完整的新版本或
  完整的旧版本，不会混读。
- **任务状态机**：`pending → running → completed / failed / paused / incomplete`。
  `failed`（查询失败）、`paused`（换批）、`incomplete`（单秒超限）均可续采；
  重启残留的 `running` 任务也可直接续采。

## 环境变量

| 变量 | 服务 | 默认 | 说明 |
| --- | --- | --- | --- |
| `DB_PATH` | api | `/data/api.db` | SQLite 路径（compose 已挂卷持久化） |
| `DB_PATH` | portal | `/data/portal.db` | 同上 |

## 本地开发（不用 Docker）

```bash
pip install -r api/requirements.txt -r portal/requirements.txt
DB_PATH=/tmp/portal.db uvicorn app.main:app --port 8001 --app-dir portal &
DB_PATH=/tmp/api.db    uvicorn app.main:app --port 8000 --app-dir api &
API_BASE=http://localhost:8000 PORTAL_BASE=http://localhost:8001 \
PORTAL_URL_FOR_API=http://localhost:8001 python3 scripts/verify.py
```
