# 飞牛 NAS (fnOS) Docker 部署与运维指南

本文档为 **雨课堂智能答题助手机器人 (Rainclass Assistant)** 在飞牛 NAS (fnOS / Debian 12) Docker 容器化环境下的标准部署、运维管理、健康检查、安全权限与灾备自愈指南。

---

## 1. 系统要求与环境规格

- **操作系统**：飞牛 NAS 操作系统 (fnOS，基于 Debian 12 Bookworm) 或标准 x86_64 Linux
- **容器运行时**：Docker Engine 24.0+，Docker Compose v2.20+
- **资源推荐配置**：
  - CPU 配额：上限 2.0 核（单账号空闲期 CPU 占用 < 2%）
  - 内存配额：上限 2048 MB（建议预留 1024MB 以上可用物理内存）
  - 共享内存（shm_size）：`1gb`（保障 Headless Chromium 多 Tab 与 Canvas 渲染不崩溃）
- **权限模型**：非 root 用户运行，默认映射飞牛 NAS 宿主用户 `uid=1000, gid=1001`。

---

## 2. 目录架构与文件组织

在 NAS 宿主机（如 `/home/lzk/rainclass-assistant`）的标准目录结构：

```text
/home/lzk/rainclass-assistant/
├── compose.yaml                # Docker Compose 编排描述文件
├── Dockerfile                  # 容器镜像构建脚本（多阶段、清华镜像源加速、内置字体与无头依赖）
├── .env                        # 部署环境变量（用户 UID/GID、运行模式、数据卷映射）
├── .env.example                # 环境变量配置模板
├── .dockerignore               # 镜像构建过滤清单（排除本地敏感密钥、会话与日志）
├── config.template.json        # 业务配置文件模板
├── requirements-worker.txt     # 无头 Worker 轻量 Python 依赖清单（剥离 Tkinter）
├── src/                        # 核心源代码目录
└── data/                       # 宿主机数据持久化目录（挂载至容器 /app/data）
    ├── config.json             # 生产配置文件（课堂 URL、AI 配置、延迟参数）
    ├── browser_state.json      # 浏览器登录态会话凭证（Cookie 与 LocalStorage）
    ├── records.db              # SQLite 答题两阶段持久化数据库（WAL 模式）
    ├── records.db-wal          # SQLite 预写日志文件
    ├── health.json             # 原子写入的轻量服务健康与心跳状态文件
    ├── .rainclass-assistant.lock # 单实例文件锁（防止双开竞态）
    └── logs/                   # 日志目录（脱敏日志与日志轮转）
```

---

## 3. 快速部署流程

### 步骤一：克隆/同步代码并准备配置文件

```bash
cd /home/lzk/rainclass-assistant

# 1. 复制并编辑环境变量
cp .env.example .env

# 2. 创建宿主机持久化数据目录
mkdir -p data

# 3. 复制并编辑业务配置
cp config.template.json data/config.json
```

### 步骤二：配置 `.env` 环境变量

```ini
# 宿主用户映射（飞牛 NAS 默认 lzk 用户为 1000:1001）
APP_UID=1000
APP_GID=1001

# 运行模式：observe (旁听观察), auto (全自动作答)
WORKER_MODE=observe

# 宿主机持久化数据目录路径
NAS_DATA_DIR=./data
```

### 步骤三：配置 `data/config.json` 业务参数

```json
{
  "mode": "observe",
  "classroom_url": "https://www.yuketang.cn/v2/web/studentLog/YOUR_CLASSROOM_ID",
  "ai_primary_model": "自定义",
  "custom_ai_base_url": "https://tokendance.space/gateway/v1",
  "custom_ai_api_key": "YOUR_API_KEY",
  "custom_ai_model": "deepseek-v4.1-flash",
  "submit_delay": 5,
  "confidence_threshold": 0.8
}
```

> [!NOTE]
> 在 `observe` 旁听模式下，系统仅进行页面与题目巡检监测，**严禁任何点击与提交操作，且不会向付费 AI 接口发送分析请求**，非常适合首阶段部署验收与登录态验证。

### 步骤四：导入登录会话凭据

雨课堂要求微信或短信验证登录，容器环境为纯无头（Headless）环境，无法弹窗人机交互，需导入有效登录会话：

- **方式 A（本地有头环境快速登录导出）**：
  在个人电脑执行：
  ```bash
  python -m src.worker --data-dir ./my_session --login
  ```
  完成扫码登录后，将生成的 `my_session/browser_state.json` 上传至 NAS 的 `data/browser_state.json`。

- **方式 B（从外部 JSON 文件校验导入）**：
  ```bash
  docker compose run --rm rainclass-worker --data-dir /app/data --import-session /path/to/exported_cookie.json
  ```
  该命令会自动校验 Cookie 的域名范围（`.yuketang.cn`）、必要字段与过期时间，通过后原子写入 `data/browser_state.json`。

### 步骤五：启动与管理容器

```bash
# 1. 构建镜像（首次构建）
docker compose build

# 2. 后台启动容器
docker compose up -d

# 3. 查看实时日志
docker compose logs -f

# 4. 查看容器健康状态
docker compose ps
```

---

## 4. 运行模式与行为特征

| 模式名称 | `WORKER_MODE` | 题目巡检 | AI 请求 | 自动勾选/点击 | 自动提交 | 适用场景 |
|---|---|---|---|---|---|---|
| **旁听模式** (默认推荐) | `observe` |  启用 |  **关闭**（零费用） |  **关闭**（零触碰） |  **关闭**（零触碰） | 部署验收、排查网络、验证登录态 |
| **辅助模式** | `assist` |  启用 |  启用 |  启用（可预览） |  **关闭**（人工确认） | 关键考试、双人核验 |
| **自动模式** | `auto` |  启用 |  启用 |  启用 |  启用 | 日常课堂、自动应答 |

> [!TIP]
> 运行模式具有严格的生效优先级：
> **命令行参数 (`--mode`) > 环境变量 (`WORKER_MODE` / `RAINCLASS_MODE`) > 配置文件 (`config.json`)**。
> 在 Compose 中配置环境变量可快速覆盖配置，无需频繁修改持久化目录中的 `config.json`。

---

## 5. 健康检查与运维监控

### 5.1 容器内置探针 (Healthcheck)

Compose 中已配置自动化健康检查探针：
```yaml
healthcheck:
  test: ["CMD", "python", "-m", "src.worker", "--data-dir", "/app/data", "--healthcheck"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 30s
```

探针行为特性：
- **存活检验 (Liveness)**：只要主程序心跳正常刷新（心跳超时阈值 120 秒），退出码为 `0`；
- **非侵入式判定**：探针仅读取 `health.json`，不加文件锁、不发起网络请求、不干扰浏览器会话；
- **无课等待语义识别**：当处于无课期（`status="waiting_class"`）时，视为正常业务状态，绝不误杀容器。

### 5.2 状态文件 `health.json` 规范

宿主机可直接读取 `data/health.json` 查看实时运行状态：
```json
{
  "status": "waiting_class",
  "alive": true,
  "ready": true,
  "reason": "无课等待中",
  "account_id": "default",
  "server_name": "fnOS-Rainclass",
  "classroom_id": "33446237",
  "classroom_url": "https://www.yuketang.cn/v2/web/studentLog/33446237...",
  "active_question_id": "",
  "error_message": "",
  "uptime_seconds": 1240.5,
  "heartbeat_age_seconds": 1.2,
  "last_heartbeat_at": "2026-09-21 23:50:00"
}
```

### 5.3 常见故障排查与自愈

1. **容器状态显示 `(unhealthy)`**：
   - 检查 `docker compose logs --tail=100` 查看异常栈；
   - 检查 `data/health.json` 中的 `error_message` 与 `heartbeat_age_seconds`；
   - 若处于 `needs_login` 状态，说明登录态失效，需重新导入 `browser_state.json`。
2. **浏览器启动失败 (`browser closed / crash`)**：
   - 检查 Compose 中的 `shm_size: "1gb"` 是否生效；
   - 确保宿主机 `/dev/shm` 空间充足。
3. **多实例锁冲突**：
   - 若容器意外重启导致遗留锁，Worker 具备超时竞争锁检测；若确有残留，可安全删除 `data/.rainclass-assistant.lock`。

---

## 6. 数据备份、迁移与回滚策略

### 6.1 备份方案
日常维护仅需对 `data/` 目录进行冷备或热备：
- **关键凭据**：`data/config.json` 与 `data/browser_state.json`（恢复业务核心）；
- **历史记录**：`data/records.db` 与 `data/records.db-wal`（答题审计日志与幂等数据）；
- **建议脚本**：使用飞牛 NAS 的定时任务每日归档 `data/` 目录。

### 6.2 优雅停机与容灾重启
容器配置了 `stop_grace_period: 30s` 与 `init: true`（Tini 进程管理器）：
- 收到 `SIGTERM` 信号后，Worker 会安全退出主循环，完成正在进行的两阶段事务更新；
- 刷新 `health.json` 状态为 `stopped`，优雅关闭 Chromium 进程，杜绝僵尸进程。

### 6.3 快速回滚
若需要回滚至上一版本：
```bash
# 停止当前服务
docker compose down

# 切换 Git 历史稳定分支/标签
git checkout feature/stage-04-long-run-and-recovery

# 重新构建并启动（原有 data/ 目录完全向下兼容）
docker compose build
docker compose up -d
```

---

## 7. 双容器 Web 管理面板与 Caddy HTTPS 网关部署 (Stage 7 HDU 风格面板)

在阶段 7 中，系统升级支持 HDU-grabber 风格暗黑 Web 管理面板（主色调 `#0f1419`），通过 Caddy 反向代理对外暴露安全 HTTPS 访问端口（`:40010`），内建 HTTP Basic Auth 访问认证与 SSE 实时脱敏日志流。

### 7.1 架构与端口规划

```text
       [浏览器/移动端] 
             │ HTTPS :40010 (Basic Auth: LzK / LzK)
             ▼
    ┌──────────────────────────────────────────────┐
    │  rainclass-caddy (Caddy 2 官方镜像)           │
    │  - 端口: 40010 -> 8000                        │
    │  - 证书: /certs 真实证书或 internal 自签名     │
    │  - SSE 禁用缓冲: flush_interval -1            │
    └──────────────────────┬───────────────────────┘
                           │ 内网 HTTP (rainclass-app:8000)
    ┌──────────────────────▼───────────────────────┐
    │  rainclass-app (FastAPI Web Panel + Worker)  │
    │  - Web 静态资源 (HDU 暗黑控制台)              │
    │  - RESTful API (状态/控制/配置/记录/日志)     │
    │  - 单实例 Worker 子进程生命周期守护           │
    │  - 持久化挂载: ./data -> /app/data           │
    └──────────────────────────────────────────────┘
```

### 7.2 环境变量配置 (`.env`)

在 `.env` 中按需指定 Caddy 证书路径与指令：

```ini
# 宿主用户映射（飞牛 NAS 默认 lzk 用户为 1000:1001）
APP_UID=1000
APP_GID=1001

# 基础镜像与运行模式
BASE_IMAGE=python:3.11-slim-bookworm
WORKER_MODE=observe

# 宿主机持久化数据目录路径
NAS_DATA_DIR=./data

# Caddy HTTPS 证书映射（飞牛 NAS 证书目录，若未配置证书则使用 internal 自签）
NAS_CERTS_DIR=/vol4/docker/xray/certs
# 若使用已有域名证书：
# CADDY_TLS_DIRECTIVE="tls /certs/91666.icu.crt /certs/91666.icu.key"
# 若使用 Caddy 内部自签（默认）：
CADDY_TLS_DIRECTIVE="tls internal"
```

### 7.3 编排启动与管理

```bash
# 1. 使用面板专用编排文件启动双容器服务
docker compose -f compose.panel.yaml up -d --build

# 2. 查看双容器运行状态
docker compose -f compose.panel.yaml ps

# 3. 跟踪查看网关与应用日志
docker compose -f compose.panel.yaml logs -f
```

### 7.4 访问与安全凭据

- **访问地址**：`https://<NAS_IP_OR_DOMAIN>:40010`（如 `https://91666.icu:40010`）
- **Basic Auth 账号**：`LzK`
- **Basic Auth 密码**：`LzK`
- **安全说明**：
  - Basic Auth 密码在 Caddyfile 中存储为标准 bcrypt 哈希（`$2b$10$...`）；
  - Web API 严禁对外暴露真实 API Key 与 Cookie 内容（自动 `sk-***` 掩码脱敏）；
  - 不挂载 Docker Socket，无远程任意代码执行风险；
  - Worker 作为内部托管单进程运行，避免双开写竞态。

### 7.5 从单容器 Worker 平滑迁移到双容器面板

1. 保持现有 `./data` 目录完全不变（包含已有 `config.json`、`browser_state.json` 与 `records.db`）；
2. 停止原单容器 Worker：`docker compose down`；
3. 启动双容器面板：`docker compose -f compose.panel.yaml up -d --build`；
4. 浏览器访问 `https://YOUR_NAS_IP:40010`，通过面板即时查看运行状态、切换模式、或在线导入新凭据；
5. 若需回滚单容器，仅需 `docker compose -f compose.panel.yaml down` 并 `docker compose up -d` 即可秒级切回。

### 7.6 网页扫码登录实操指引 (Stage 8 新增)

在双容器 Web 面板中，已支持全流程 Web 扫码登录闭环，**免除在本地电脑运行命令行导出会话并手动上传的步骤**：

1. 浏览器打开管理面板并登录（`https://<NAS_IP>:40010`，用户名 `LzK`，密码 `LzK`）；
2. 导航至 **「会话管理」** 选项卡；
3. 在顶部的 **「📱 网页扫码登录」** 卡片中：
   - 下拉选择您所在的雨课堂校区服务器（默认支持 *雨课堂*、*长江雨课堂*、*荷塘雨课堂*、*黄河雨课堂*）；
   - 点击 **「🚀 开始扫码登录」** 按钮；
4. 系统将在后台启动隔离无头 Chromium 浏览器，加载雨课堂登录页面并在界面右侧实时呈现二维码；
5. 使用手机微信或雨课堂 APP 扫描屏幕上的二维码并点击授权登录；
6. 扫码成功后，系统将自动校验凭据合法性、原子覆盖写入 `data/browser_state.json`，并自动平滑恢复后台 Worker 运行；
7. **异常与回退**：若因雨课堂环境风控出现滑动人机验证码，界面将明确提示“需本地命令行登录”，此时可随时使用下方的文件拖拽上传作为兜底保障。


