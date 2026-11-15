# LIMS 历史报告补采系统

检验机构数据员把门户上的历史报告补录进 LIMS 的补采服务。门户按报告时间检索、
单次最多返回 100 条且不能翻页，本系统通过**时间区间二分**完成补采，支持失败续采、
换批重采和批次核对后的原子发布。

## 组件

| 组件 | 说明 | 端口 |
| --- | --- | --- |
| `api` | 补采 API（FastAPI + SQLite）：补采、续采、进度、结果查询 | 8000 |
| `portal` | 本地模拟门户（FastAPI + SQLite，无需账号）：报告检索 + 故障/换批/造数管理接口 | 8001 |

```
├── docker-compose.yml      # 一键启动 api + portal
├── api/                    # 补采 API
│   └── app/{main,db,collector,timeutil}.py
├── portal/                 # 本地模拟门户
│   └── app/{main,db,timeutil}.py
└── scripts/verify.py       # 端到端验证脚本（仅依赖标准库）
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

# 加测“服务重启后按任务号续采”
RESTART_CMD='docker compose restart api' python3 scripts/verify.py
```

脚本覆盖：参数校验、区间二分、[start, end) 语义、查询失败续采、换批暂停重采、
单秒超限上报、旧结果保留、重启续采、进度与结果查询。

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

返回该门户该区间当前已发布的批次与全部记录；未发布返回 404。

## 门户接口（模拟，无需账号）

| 接口 | 说明 |
| --- | --- |
| `GET /reports?start=&end=` | 返回 `{batch, total, items}`，`items` 最多 100 条 |
| `POST /admin/seed` | 造数：`{start,end,count,seed}` 区间内随机；`{time,count}` 同秒超限 |
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
