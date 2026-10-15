# ARTEX-python · AI 自主渗透测试平台

> FastAPI + PostgreSQL + LangGraph 构建的**事件驱动型自主渗透测试引擎**。
> Planner（规划器）不断从探索图推断新意图 → Worker（工作者池）并发认领意图 → 执行工具 → 回写探索图 → 触发下一轮循环，全程无需人工干预。

---

## 目录

- [1. 这是什么](#1-这是什么)
- [2. 架构与目录结构](#2-架构与目录结构)
- [3. 安装要求](#3-安装要求)
- [4. 安装方法](#4-安装方法)
- [5. 配置 .env（重点）](#5-配置-env重点)
- [6. 初始化数据库](#6-初始化数据库)
- [7. 启动服务](#7-启动服务)
- [8. 登录与鉴权](#8-登录与鉴权)
- [9. 调用 API 做自主渗透](#9-调用-api-做自主渗透)
- [10. 真实工具接入（Route B）](#10-真实工具接入route-b)
- [11. 注意事项](#11-注意事项)
- [12. 避坑指南（我们踩过的坑）](#12-避坑指南我们踩过的坑)
- [13. 常见问题排查](#13-常见问题排查)
- [14. 运行测试](#14-运行测试)
- [15. 合法使用声明](#15-合法使用声明)

---

## 1. 这是什么

ARTEX-python 是一个**自主渗透测试（Auto-PenTest）平台**，核心思想是：

```
        ┌──────────────────────────────────────────────┐
        │              探索图（Exploration Graph）        │
        │   目标 / 意图 / 事实 / 发现 / 假设 / 行动 …      │
        └──────────────────────────────────────────────┘
                          ▲ 回写                │ 读取
                          │                     ▼
   ┌─────────┐   新意图   ┌─────────┐  认领   ┌──────────┐
   │ Planner │ ─────────► │  意图池  │ ──────► │  Worker  │
   │ (规划器) │            │(Todolist)│         │ (工作者池) │
   └─────────┘            └─────────┘          └──────────┘
      每个事件触发                          并发 4 个
      下一轮规划（debounce 2s）             认领+执行工具
```

- **Planner**：LangGraph 图，每 2 秒被唤醒一次，扫描探索图，推断出"接下来该做什么"（新意图）
- **Worker Pool**：4 个并发 LangGraph worker，通过 `SELECT ... FOR UPDATE SKIP LOCKED` 原子认领最高优先级的意图，执行工具，把结果写回探索图
- **EventBus + SSE**：所有图变更通过事件总线广播，前端通过 Server-Sent Events 实时渲染

它支持两种工具执行模式：
- **模拟模式**（默认）：不传 LLM，worker 用 `mock_recon_tool` 产生结构化假数据（端口、服务指纹），适合理解流程 / 跑通 pipeline
- **真实工具模式**（Route B）：接入真实 CLI 工具（nmap / masscan / sqlmap / http_probe），LLM 决策 `{"tool":"nmap","target":"...","args":"-sV"}` 后 `subprocess` 真执行，stdout 回填进探索图

---

## 2. 架构与目录结构

```
ARTEX-python/
├── src/
│   ├── main.py               # FastAPI 应用入口（src.main:app）+ 引擎 start/stop 路由
│   ├── config.py             # pydantic-settings，从 .env 读取配置
│   ├── api/
│   │   ├── dependencies.py   # JWT 鉴权、mock 账号
│   │   ├── routes/
│   │   │   ├── tasks.py      # /api/tasks 任务 + SSE 活动流
│   │   │   ├── dashboard.py  # /api/stats /api/companies 等聚合
│   │   │   ├── findings.py   # /api/tasks/{id}/findings 漏洞
│   │   │   └── traffic.py    # 流量代理路由
│   │   ├── stream_manager.py # SSE 连接管理
│   │   └── shutdown.py       # 引擎优雅停止
│   ├── core/
│   │   ├── engine.py         # 引擎编排（Planner + WorkerPool + EventBus）
│   │   ├── planner.py        # 规划器 LangGraph
│   │   ├── worker.py         # 工作者 LangGraph（含 Route B 真实工具接缝）
│   │   ├── worker_pool.py    # 4 并发 worker 池 + 原子认领
│   │   └── event_bus.py      # 内存事件总线
│   ├── db/
│   │   ├── models.py         # SQLAlchemy ORM
│   │   ├── schemas.py        # Pydantic 输入/输出模型
│   │   ├── repository.py     # AsyncGraphRepository（图读写）
│   │   └── session.py        # 异步引擎 + session factory
│   └── tools/
│       ├── real_tools.py     # ★ Route B：真实工具执行器（ToolExecutor）
│       ├── llm_gateway.py    # LLM 网关（OpenAI / Anthropic + 熔断）
│       ├── traffic_proxy.py  # mitmproxy 流量采集
│       └── notifier.py       # 通知分发
├── alembic/                  # 数据库迁移（initial + findings_goals）
├── web-demo/                 # 零依赖前端 SPA（index.html + app.js + styles.css）
├── tests/                    # pytest 测试套件（132 用例）
├── docker-compose.yml        # 一键部署（backend + postgres + mitmproxy）
├── Dockerfile
├── requirements.txt
└── .env.example              # 环境变量模板（复制为 .env 使用）
```

---

## 3. 安装要求

| 组件 | 最低版本 | 说明 |
|------|---------|------|
| **Python** | 3.11+ | 后端运行环境（`langgraph` / `pydantic` 依赖 3.11 语法） |
| **PostgreSQL** | 14+ | **必须**，且必须用 PostgreSQL（代码用了 `JSONB`、`ENUM`、`FOR UPDATE SKIP LOCKED` 等 PG 专有特性，**不能换成 SQLite / MySQL**） |
| **Docker** | 20+ | 仅"方式 B / Docker Compose"需要；本地跑后端不强求 |
| **Git** | 2.x | 拉取仓库需要 |

### 三种运行 PostgreSQL 的方式（任选其一）

1. **本机安装**（Windows / Linux / macOS 官方安装包）
2. **Docker 容器**（推荐，最干净）：
   ```powershell
   docker run -d --name artex-pg `
     -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres `
     -e POSTGRES_DB=artex_db -p 5432:5432 `
     -v pgdata:/var/lib/postgresql/data `
     postgres:16-alpine
   ```
3. **Docker Compose 全套**（见第 4 节方式 B，连数据库一起拉起来）

> ⚠️ 端口固定用 **5432**（`.env` 和 `docker-compose.yml` 都写死），不要改。

---

## 4. 安装方法

### 方式 A：本地开发（推荐，改代码即时生效）

#### A-1. 克隆并进入项目

```powershell
git clone https://github.com/YOUR-GITHUB/ARTEX-python.git
cd ARTEX-python
```

#### A-2. 创建虚拟环境 + 安装依赖

**Windows（PowerShell）：**
```powershell
python -m venv .venv
.\.venv\Scripts\activate            # 激活，提示符前会出现 (.venv)
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

**Linux / macOS（bash）：**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

> `requirements.txt` 里已包含全部生产依赖 + 测试依赖（`pytest`、`pytest-asyncio`），一次性装好。

#### A-3. 准备 PostgreSQL

任选上面 3 种方式之一。**Docker 容器**最省事：
```powershell
docker run -d --name artex-pg -e POSTGRES_USER=postgres `
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=artex_db `
  -p 5432:5432 -v pgdata:/var/lib/postgresql/data postgres:16-alpine
docker exec artex-pg pg_isready -U postgres
# 输出：/var/run/postgresql:5432 - accepting connections  即就绪
```

#### A-4. 配置 `.env`

```powershell
copy .env.example .env          # Windows
# cp .env.example .env          # Linux/macOS
```
按第 5 节修改 `DATABASE_URL`（本机要改成 `localhost`）。

#### A-5. 建表（数据库迁移）

```powershell
python -m alembic upgrade head
```
看到 `Running upgrade  -> initial` 和 `Running upgrade initial -> findings_goals` 即成功。

#### A-6. 启动后端 + 前端

见第 7 节。

---

### 方式 B：Docker Compose 一键部署（最省事，适合部署 / 快速体验）

```powershell
copy .env.example .env
# 按需修改 .env 里的 DB_PASS / JWT_SECRET_KEY
docker-compose up -d --build
```

一次起 3 个容器：

| 服务 | 端口 | 说明 |
|------|------|------|
| backend | 8000 | FastAPI + Swagger（`/docs`），启动时自动跑 alembic 迁移 |
| postgres | 5432 | PostgreSQL 16，数据存 `pgdata` 卷 |
| mitmproxy | 8081 / 8082 | 流量采集代理 / Web UI |

```powershell
docker-compose logs -f backend      # 看日志，等 migrations 完成
curl http://localhost:8000/api/health   # 验证
docker-compose down                # 停止（保留数据）
docker-compose down -v             # 停止并删数据卷（清库）
```

> 方式 B 里后端在容器内访问数据库用的 host 是 `postgres`（服务名），**不要**把它也写进本地 `.env` 给方式 A 用——本地要用 `localhost`。这是常见混淆点。

---

## 5. 配置 .env（重点）

`.env.example` 的默认值是**为 Docker Compose 场景**写的（数据库 host = `postgres`、账号 `artex_user`）。**本地开发必须改成指向你本机的 PG**：

```ini
# ===== 本地开发（方式 A）推荐配置 =====
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/artex_db
DB_ECHO=false
APP_NAME=ARTEX Python
DEBUG=true
JWT_SECRET_KEY=dev-secret-change-me
```

字段含义：

| 字段 | 必填 | 说明 |
|------|:---:|------|
| `DATABASE_URL` | ✅ | 完整连接串。本地用 `localhost:5432`；Docker 内用 `postgres:5432` |
| `DB_ECHO` | ❌ | `true` 时打印所有 SQL（调试用，生产关） |
| `APP_NAME` | ❌ | 应用名 |
| `DEBUG` | ❌ | `true` 时日志级别 INFO，方便看引擎循环 |
| `JWT_SECRET_KEY` | ✅ | 签 JWT 的密钥，**生产务必换成随机强字符串** |
| `GITHUB_REPO` / `GITHUB_TOKEN` 等 | ❌ | 仅 `supervisor.py` 自动更新用，本地可留空 |

> 代码已设 `Settings` 的 `model_config = {"extra": "ignore"}`，所以 `.env` 里多出来的 Docker 专用变量（`DB_USER`、`DOCKER_IMAGE` 等）**不会**导致本地启动失败——留着也没事。

---

## 6. 初始化数据库

```powershell
python -m alembic upgrade head        # 建表 + 建枚举
python -m alembic current             # 查看当前版本，应为 findings_goals (head)
python -m alembic downgrade -1        # 回退一级（慎用）
```

- 迁移包含两张 revision：`initial`（主 schema）+ `findings_goals`（漏洞 / 目标表）
- **首次建库**、或**改了模型**都要跑
- 建表失败时先确认 PG 在跑（`docker exec artex-pg pg_isready -U postgres`）

> 如果迁移报 `type "xxx" already exists`（枚举重复），通常是之前半路失败留下的脏库，直接删库重建：
> ```powershell
> docker exec artex-pg psql -U postgres -c "DROP DATABASE artex_db;"
> docker exec artex-pg psql -U postgres -c "CREATE DATABASE artex_db;"
> python -m alembic upgrade head
> ```

---

## 7. 启动服务

### 7.1 启动后端

```powershell
# 激活 venv 后：
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```
成功标志：
```
INFO:     Uvicorn running on http://0.0.0.0:8000
```
- **Swagger 文档**：http://localhost:8000/docs
- **健康检查**：http://localhost:8000/api/health → `{"status":"ok",...}`

### 7.2 启动前端（web-demo）

```powershell
cd web-demo
python -m http.server 5173
```
浏览器打开 **http://localhost:5173**，登录页账号 `admin / admin123`。

> 前端默认请求 `http://localhost:8000`。若后端换了端口，访问前端时加 `?api=http://localhost:你的端口`。

### 7.3 验证联调

浏览器 F12 → Console 无报错、能登录进仪表盘即前后端打通。
若登录报 `Failed to fetch`，基本是 **CORS** 问题，见第 13 节。

---

## 8. 登录与鉴权

内置 mock 账号（`src/api/dependencies.py`）：

| 用户名 | 密码 | 角色 |
|--------|------|------|
| `admin` | `admin123` | admin, user |
| `operator` | `operator123` | user |

获取 token：
```powershell
$login = curl -X POST http://localhost:8000/api/auth/login `
  -H "Content-Type: application/json" `
  -d '{"username":"admin","password":"admin123"}'
# 返回 {"access_token":"eyJ...","username":"admin"}
```
之后每个接口都要带：
```powershell
curl http://localhost:8000/api/tasks/ -H "Authorization: Bearer <token>"
```

---

## 9. 调用 API 做自主渗透

完整最小流程（PowerShell）：

```powershell
$base = "http://localhost:8000"

# 1) 登录拿 token
$login = Invoke-RestMethod -Method POST -Uri "$base/api/auth/login" `
  -ContentType "application/json" -Body '{"username":"admin","password":"admin123"}'
$h = @{ Authorization = "Bearer $($login.access_token)" }

# 2) 建目标公司（domain 是要渗透的目标）
$comp = Invoke-RestMethod -Method POST -Uri "$base/api/companies/" -Headers $h `
  -ContentType "application/json" -Body '{"name":"ACME","domain":"acme.example.com"}'

# 3) 建渗透任务（task_type: recon / scan / exploit / post_exploit / full）
$task = Invoke-RestMethod -Method POST -Uri "$base/api/tasks/" -Headers $h `
  -ContentType "application/json" `
  -Body (@{ company_id=$comp.id; task_type="full"; title="Demo"; priority=5 } | ConvertTo-Json)

# 4) 启动自主引擎（核心动作，之后引擎自动循环）
Invoke-RestMethod -Method POST -Uri "$base/api/engine/$($task.id)/start" -Headers $h

# 5) 实时观察（SSE，会一直流，Ctrl+C 结束）
Invoke-WebRequest -Uri "$base/api/tasks/$($task.id)/stream" -Headers $h

# 6) 拉快照（任务统计 / 探索图 / 漏洞）
Invoke-RestMethod -Uri "$base/api/tasks/$($task.id)/stats"   -Headers $h
Invoke-RestMethod -Uri "$base/api/tasks/$($task.id)/context" -Headers $h
Invoke-RestMethod -Uri "$base/api/tasks/$($task.id)/findings"-Headers $h

# 7) 停止引擎
Invoke-RestMethod -Method POST -Uri "$base/api/engine/$($task.id)/stop" -Headers $h
```

| `task_type` | 含义 |
|------------|------|
| `recon` | 只做侦察（域名/IP/端口/服务指纹） |
| `scan` | 加扫描（弱口令/漏洞指纹） |
| `exploit` | 加漏洞利用（模拟） |
| `post_exploit` | 加后渗透（横向/持久化） |
| `full` | 全链路（默认推荐） |

> 每个 task 同时只能有一个引擎，重复 `start` 返回 `already_running`；要重启先 `stop`。

---

## 10. 真实工具接入（Route B）

默认引擎跑的是**模拟工具**（`mock_recon_tool`，产假数据）。要跑**真实渗透工具**，已内置可插拔执行层 `src/tools/real_tools.py`：

| 工具 | 类型 | 说明 |
|------|------|------|
| `http_probe` | 内置（httpx） | 零安装、Windows 可用，默认探测目标 |
| `nmap` | 外部 CLI | 装到 PATH 后自动启用 |
| `masscan` | 外部 CLI | 同上 |
| `sqlmap` | 外部 CLI | 装好 `python -m sqlmap` 后自动识别 |

**启用 nmap（Windows）**：下载安装 [nmap](https://nmap.org/download/) 并确保 `nmap.exe` 在 PATH：
```powershell
nmap --version
```
装上后 LLM/规则决策出 `{"tool":"nmap","target":"10.0.0.0/24","args":"-sV -p80,443"}` 就会真实 `subprocess` 执行并回填 stdout。**未安装的外部工具会优雅降级**（返回 `status: unavailable`，引擎不会崩）。

**接 LLM 决策**（让大模型挑工具，而非规则回退），改 `src/main.py` 的 `start_engine`：
```python
from src.tools import create_openai_pool
pool = create_openai_pool(
    base_url="https://api.openai.com/v1",
    api_key=os.environ["OPENAI_API_KEY"],
    model="gpt-4o-mini",
)
engine = Engine(..., llm_pool=pool, tool_executor=ToolExecutor())
```
并在 system prompt 里约束"必须输出 JSON `{tool, target, args}`"。

---

## 11. 注意事项

1. **数据库只能用 PostgreSQL**：迁移与 ORM 依赖 `JSONB` / `ENUM` / `SKIP LOCKED`，换 SQLite/MySQL 会直接报错。
2. **CORS 端口**：前端跑 `:5173`，后端 CORS 白名单必须包含它。`src/main.py` 已默认放行 `3000 / 5173 / 5500`，若换了前端端口要手动加进 `allow_origins`。
3. **合法授权**：真实工具会产生真实流量，**只能在你拥有或已书面授权的目标上运行**（见第 15 节）。
4. **Windows vs Linux 工具差异**：HackingTool 那套 185 工具大多 Kali-only（无线 / 内核提权 / metasploit 模块），Windows 本机基本只有 nmap/sqlmap/masscan 可用；要全量工具请用 Kali Docker 容器。
5. **密钥不要提交**：`.env` 已在 `.gitignore` 里排除，只提交 `.env.example`。
6. **引擎停止才关 session**：`stop_engine` 会关闭该引擎专属的 DB session，正常流程用 `stop` 收尾。
7. **Python 3.11**：3.10 会因语法报错。

---

## 12. 避坑指南（我们踩过的坑）

按"症状 → 原因 → 修复"整理，供参考 / 复现排查：

| # | 症状 | 根因 | 修复 |
|---|------|------|------|
| 1 | `alembic upgrade` 报 `MissingGreenlet` | `alembic/env.py` 用同步 `connectable.connect()` 却配了 async 驱动 asyncpg | 重写 `env.py` 为官方异步模板（`create_async_engine` + `conn.run_sync`） |
| 2 | 迁移报 `type "asset_type" already exists`（删库也复现） | `initial.py` 先手动 `CREATE TYPE`，随后 `sa.Enum` 列又触发 SQLAlchemy 再建一次 | 删除手动 `op.execute("CREATE TYPE ...")`，只留 `sa.Enum` 列 |
| 3 | 启动报 `Extra inputs not permitted`（pydantic 校验失败） | `Settings` 默认 `extra="forbid"`，`.env` 里 Docker 专用变量触发校验 | `model_config = {"extra": "ignore"}` |
| 4 | 所有 DB 接口 500 `InvalidPasswordError: user "user"` | `src/db/session.py` 硬编码 `DATABASE_URL`，没读 `.env` | 改为 `DATABASE_URL = settings.database_url` |
| 5 | tasks 路由 500 `NameError: name 'request' is not defined` | 11 个端点体里用了 `request` 却没在签名声明 | 给相关函数加 `request: Request` 首参 |
| 6 | 建任务 500 `'TaskCreate' object has no attribute 'company_id'` | 路由读 `payload.company_id` 但 schema 没该字段（DB 列 NOT NULL） | `TaskCreate` 加 `company_id: UUID` |
| 7 | 登录页 `Failed to fetch` | 前端 `:5173` 跨域，后端 CORS 只放行 `:3000` | `allow_origins` 加入 `http://localhost:5173` |
| 8 | 引擎空转，intent 永远 pending（`claimed_by_worker_id` 为空） | `WorkerPool` 用全零 UUID 认领，而 `claim_intent` 按真实 task_id 过滤，匹配不到 | 给 `WorkerPool` 加 `task_id` 参数并传入 |
| 9 | `InvalidUpdateError: Expected dict, got <coroutine object>` | async LangGraph 节点被同步 lambda 包裹，没 await | `add_node` 改用 async node function |
| 10 | `UUID(uuid对象)` 崩溃 `'UUID' object has no attribute 'replace'` | worker 返回的 `result_node_id` 已是 UUID，又二次 `UUID(...)` | 判断 `isinstance(x, UUID)` 再转换 |
| 11 | activity 写库 FK 违约 `activities_intent_id_fkey` | `activities.intent_id` 外键指向 `exploration_nodes`，误传了 `intents.id` | 持久化时 `intent_id=None`，把 intent 关联放 `metadata_json` |
| 12 | 数据看不到进展，`stop` 后全回滚 | 引擎专属 session `autoflush/autocommit=False` 却从不 commit | worker/planner 周期末尾显式 `await repo.db.commit()` |

> 前 1~7 是"跑不起来"的阻塞项（已在代码里修好）；8~12 是"引擎自主循环"的深层接缝，也已在本次全部修好并回归验证（132 测试全绿）。

---

## 13. 常见问题排查

| 现象 | 排查 |
|------|------|
| 后端起不来 / `Connection refused 5432` | PG 没跑：`docker start artex-pg`；Docker Desktop 本身没开就先启动它 |
| `/api/health` 不通 | 后端没起：`uvicorn src.main:app --port 8000`；确认 8000 未被占用 |
| 接口返回 `401` | 忘了带 `Authorization: Bearer <token>` 请求头 |
| 登录 `Failed to fetch` | 多半 CORS：确认后端 `allow_origins` 含前端端口；浏览器硬刷新 `Ctrl+Shift+R`；检查代理软件是否拦截 localhost |
| `alembic` 命令找不到 | 先激活 venv（`.\.venv\Scripts\activate`）再跑 |
| `pip install` 卡住/失败 | 换国内镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| Docker 拉镜像慢 | 配置国内 registry-mirror（见 Docker Desktop → Settings → Docker Engine） |
| 端口冲突 | 5432/8000/5173 被占用时：`Get-NetTCPConnection -LocalPort 8000` 找 PID 杀掉 |

---

## 14. 运行测试

```powershell
python -m pytest               # 全部
python -m pytest tests/test_schemas.py -v   # 单文件
python -m pytest -k "engine"   # 按关键字
```
当前基线：**132 passed**（覆盖 schema、引擎、worker pool、LLM 网关、路由鉴权等）。

---

## 15. 合法使用声明

⚠️ **本项目包含真实攻击工具（nmap / sqlmap / masscan / mitmproxy）与自主渗透引擎，仅限授权场景使用。**

- 只在你**拥有**或**已获得书面授权**的目标、网络、系统上运行
- 遵守当地法律法规（如《网络安全法》《刑法》第 285/286 条）与目标方的服务条款
- 禁止用于未授权访问、破坏数据、DoS 等非法行为
- 使用者对运行行为负全责；项目方不对滥用后果负责

---

## License

（请按你的项目实际许可证补充，例如 MIT / Apache-2.0）
