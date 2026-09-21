# 冷链告警抑制策略沙盒（Cold-Chain Alert Suppression Sandbox）

一个**完全本地运行**的 Web 沙盒：导入冷链告警 JSON、编写按 **设备 / 批次 / 级别 / 时间段** 匹配的抑制规则，系统按**显式优先级**与**冷却窗口**对连续告警逐条评估，输出每条告警的 **放行（PASS）/ 抑制（SUPPRESS）** 决定与命中规则解释；支持**逐条注入、撤销最近注入、策略变更前后对比**，并内置一个**模拟接收端**（webhook 收件箱）。

技术栈：**FastAPI + SQLite + 原生 HTML/CSS/JS**（无前端框架、无构建步骤、无需任何第三方账号）。

---

## 1. 一键启动

### 方式 A：Docker Compose（推荐）

```bash
docker compose up --build
```

启动后打开：

- 沙盒界面：<http://localhost:8000>
- 自动生成的 API 文档（Swagger UI）：<http://localhost:8000/docs>
- 内置模拟接收端：`POST http://localhost:8000/webhook`

SQLite 数据保存在 Docker volume `sandbox-data` 中，`docker compose down` 后数据仍在；如需全新开始：

```bash
docker compose down -v && docker compose up --build
```

### 方式 B：仅用 Docker

```bash
docker build -t coldchain-alert-sandbox .
docker run --rm -p 8000:8000 -v "$(pwd)/data:/app/data" coldchain-alert-sandbox
```

### 方式 C：本机 Python（3.10+）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 首次打开页面时，系统会自动载入一套冷链演示数据（4 条规则 + 9 条告警），可直接体验；
> 也可随时点击右上角「载入演示场景」重置为该状态。

---

## 2. 核心概念

### 规则的四个匹配维度（AND 关系，全部满足才算命中）

| 维度 | 写法 | 示例 |
| --- | --- | --- |
| 设备 `devices` | 逗号分隔；`前缀*` 通配；**空 = 任意** | `REEFER-*`、`FREEZER-01,FREEZER-02` |
| 批次 `batches` | 同上 | `B-2026-0901` |
| 级别 `levels` | 逗号分隔；空 = 任意 | `warning,critical` |
| 时间段 `time_window` | 24 小时制 `HH:MM-HH:MM`，**支持跨零点**；空 = 任意 | `22:00-06:00` |

级别标准值为 `info / warning / critical`；导入告警时兼容常见别名：`warn→warning`、`fatal/error→critical`、`notice→info`，以及中文「警告 / 严重 / 提示」，数字 `1/2/3` 分别表示 critical/warning/info。

时间段按**告警时间戳自身携带的当地钟点**匹配（`2026-09-21T22:30+08:00` 就按 22:30 匹配），与部署机器时区无关；**告警缺时间而规则设了时间窗时，判定为不命中**，避免误抑制。

### 显式优先级

- 规则按 `priority` 升序检查（**数字越小优先级越高**），**第一条命中的规则生效**，评估解释里会保留全部候选规则的命中/跳过原因；
- `action = pass` 是**放行白名单**，优先级排在抑制规则前面时，可以把特定告警从静默窗口里"捞出来"；
- 没有任何规则命中：默认 **PASS 放行**（沙盒采取"不配置即不拦截"的安全默认）。

### 动作与冷却窗口（`cooldown_sec`）

`action = suppress` 的规则分两种用法：

1. **纯抑制（冷却 = 0 秒）**：命中即抑制，适合计划维护窗口、已知批次静默；
2. **带冷却的抖动抑制（冷却 > 0 秒）**：同一**冷却维度**下——
   - 第一条命中告警 **PASS**（作为新事件通知）并建立冷却；
   - 冷却窗口内的重复告警 **SUPPRESS**，解释中给出距基准告警的已过秒数/剩余秒数；
   - 窗口外的命中告警 **PASS** 并重置冷却。

冷却维度 `cooldown_scope` 可选：

- `device_batch`（默认）：同设备 + 同批次；
- `device`：同设备（跨批次也算重复）；
- `global`：全局任意设备/批次。

对**乱序到达**（告警时间早于当前冷却基准）的迟到告警，不会错误重置冷却，而是保守抑制并在解释中标注。

### PASS 与接收端

PASS 表示"应当通知下游"。沙盒内置一个模拟接收端：

- 顶栏「注入即转发」开启时，逐条注入产生的 PASS 会自动投递到接收端；
- 也可用「重放所有未投递的 PASS」手动投递（已投递的会按告警去重，不会重复）；
- 接收端同时是一个真实 HTTP 端点 `POST /webhook`，可直接用 curl / 外部平台投递测试：

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"device_id":"FREEZER-01","level":"critical","message":"直发测试"}'
```

**撤销最近注入**会级联删除该告警在接收端产生的转发记录，方便反复试验。

---

## 3. 操作流程（界面四个标签页）

1. **① 告警与决定**：粘贴单条 JSON 逐条注入（立即评估并可自动转发）、批量导入数组、撤销最近注入、清空告警；评估流水展示每条告警的 PASS/SUPPRESS 徽章，点击「查看命中解释」可看到完整的规则匹配链（每条候选规则为何命中/跳过、冷却基准与剩余秒数）。
2. **② 抑制规则**：表单新建/编辑规则（优先级、动作、四维匹配、冷却与维度），支持启停与删除；任何规则变更都会**立即用新规则重算全部告警**。在调整规则前，可在本页「策略快照」把当前全量规则冻结为一个命名版本。
3. **③ 策略前后对比**：选择一个历史快照作为"变更前"基线，与"当前规则"在**同一份告警**上分别评估，逐条列出变更前/变更后的决定、命中规则与原因，并统计放行/抑制数量变化与差异条数。
4. **④ 模拟接收端**：查看策略转发与外部 POST 的收件箱（最新在前、完整 JSON 留档），支持手动重放与清空。

---

## 4. 告警 JSON 格式

最小只需设备标识：`{"device_id": "FREEZER-01"}`。字段支持多种别名：

| 内部字段 | 可接受的键名 |
| --- | --- |
| `device_id` | `device_id` / `deviceId` / `device` / `device_code` / `设备` |
| `batch_id` | `batch_id` / `batchId` / `batch` / `lot` / `批次` |
| `level` | `level` / `severity` / `级别`（值兼容中英文别名与数字） |
| `alert_type` | `alert_type` / `alertType` / `type` / `event` / `code` |
| `message` | `message` / `msg` / `detail` / `description` / `描述` |
| `alert_time` | `alert_time` / `alertTime` / `time` / `timestamp` / `occurred_at` / `ts`（ISO8601、`Z` 结尾、常见空格格式、epoch 秒/毫秒均可） |

完整原始 JSON 会原样保留在数据库与解释中。可参考 [`examples/alerts.sample.json`](examples/alerts.sample.json)。

---

## 5. HTTP API 摘要

所有接口前缀 `/api`（Swagger 文档见 `/docs`）：

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /api/overview` | 告警/决定/规则/收件箱计数与开关状态 |
| `GET /api/alerts` | 全部告警 + 最近一次评估的决定与解释 |
| `POST /api/alerts/inject` | 逐条注入：`{"alert": {...}, "auto_forward": true}` |
| `POST /api/alerts/import` | 批量导入：`{"alerts": [{...}], "auto_forward": false}`（坏行收集为错误，不影响其他行） |
| `POST /api/alerts/undo` | 撤销最近一次注入（级联清理转发记录并重算） |
| `POST /api/alerts/clear` | 清空告警与转发记录（规则/快照保留） |
| `POST /api/evaluate` | 手动触发全量重评 |
| `GET/POST /api/rules`、`PUT/PATCH/DELETE /api/rules/{id}` | 规则 CRUD；`PATCH .../toggle` 快速启停 |
| `GET/POST /api/versions`、`DELETE /api/versions/{id}` | 策略快照管理 |
| `POST /api/compare` | 快照 vs 当前规则的逐条对比，body：`{"base_version_id": 1}`（省略则取最早快照） |
| `GET /api/receiver/inbox` | 接收端收件箱 |
| `POST /api/receiver/forward-pending` | 重放所有未投递的 PASS（自动去重） |
| `POST /api/receiver/clear` | 清空收件箱 |
| `POST /api/demo/load` | 重置并载入演示场景 |
| `POST /webhook` | 模拟接收端，接收任意 JSON |

快速试用：

```bash
# 注入一条（时间在冷却窗口内的第二条会被抑制）
curl -X POST http://localhost:8000/api/alerts/inject \
  -H 'Content-Type: application/json' \
  -d '{"alert":{"device_id":"FREEZER-02","batch_id":"B-1","level":"warning",
      "alert_type":"TEMP_DRIFT","alert_time":"2026-09-21T14:05:00+08:00"}}'

# 撤销最近注入
curl -X POST http://localhost:8000/api/alerts/undo
```

---

## 6. 数据持久化与配置

| 项目 | 说明 |
| --- | --- |
| 数据库 | SQLite 单文件，默认 `./data/sandbox.db`（容器内为 `/app/data/sandbox.db`，已挂 volume） |
| 环境变量 `SANDBOX_DB` | 覆盖数据库文件路径 |
| 并发模型 | 单文件 SQLite（WAL 模式）+ 进程内读写锁，评估与决策落库原子执行；沙盒/单机用途安全 |
| 外网依赖 | **无**。镜像基于 `python:3.12-slim`，运行时不访问任何外部服务 |

---

## 7. 运行测试

无需额外依赖（标准库 `unittest`）：

```bash
python3 -m unittest discover -s tests -v
```

测试包含 20 个策略引擎纯函数用例（优先级、四维匹配、跨零点时间窗、冷却边界、乱序到达、冷却维度、解释链等）与 8 个服务层集成用例（注入/转发/撤销/批量导入容错/快照对比/规则 CRUD/webhook/去重）。

---

## 8. 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 入口：API 路由 + /webhook + 静态前端挂载
│   ├── db.py            # SQLite 连接、建表、事务与锁
│   ├── normalize.py     # 级别别名 / 时间解析 / 时间窗校验与匹配
│   ├── engine.py        # 纯函数评估引擎：优先级 + 四维匹配 + 冷却状态机
│   ├── schemas.py       # Pydantic 请求模型与字段校验
│   ├── services.py      # 服务层：注入/撤销/重评/转发/快照对比/演示数据
│   └── routers/api.py   # 全部 /api HTTP 路由
├── static/
│   ├── index.html       # 四标签页界面
│   ├── styles.css
│   └── app.js           # 原生 JS，无框架无构建
├── tests/               # unittest 引擎与服务层测试
├── examples/
│   └── alerts.sample.json
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
