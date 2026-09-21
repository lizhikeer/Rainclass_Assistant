# Rainclass Assistant 改造进度追踪 (Antigravity Progress)

## 1. 阶段概述

- **当前完成阶段**：第五阶段：飞牛 NAS 应用部署与端到端验收（`05-飞牛NAS部署与验收.md`）
- **当前代码分支**：`feature/stage-05-nas-deploy-and-acceptance`
- **基线提交**：`7d287130b4ec7483a9a13b0c95a02251a31d9ee0` (stage 3 complete) / `stage 4 complete`
- **交付状态**：已完成标准容器化封装（`Dockerfile`、`compose.yaml`、`.dockerignore`、`.env.example`、`config.template.json`），实现纯无头运行、非 root 权限映射（`1000:1001`）、中文字体支持、内置健康探针；成功部署于实体飞牛 NAS (fnOS / Debian 12 x86_64)；完成 Tier A~D 四层严格验收，旁听模式（`observe`）安全巡检验证（零点击零提交零消耗付费AI），真实 TokenDance DeepSeek v4.1 Flash 思考链禁用优化与 1.58 秒极速响应验证，多阶状态机心跳监控与优雅停机平稳自愈。

---

## 2. 第五阶段新增与修改的组件清单

| 模块/文件 | 变更类型 | 关键设计与职责 |
|---|---|---|
| `Dockerfile` | [NEW] | 1. 基于 `python:3.11-slim`，配置 Debian 清华源加速，预装 `tzdata` 与 `fonts-noto-cjk` 中文字体；<br>2. 预先安装 Chromium Linux 系统级无头依赖库，内建 `ms-playwright` 离线解压固化；<br>3. 建立非 root 用户 `rainclass` (`uid=1000, gid=1001`)，保障宿主权限对齐；<br>4. 固定工作目录 `/app` 与持久化目录 `/app/data`。 |
| `compose.yaml` | [NEW] | 1. 容器编排规格定义，分配 `shm_size: 1gb` 根绝 Chromium 崩溃；<br>2. 注入资源上限限制（2.0 CPU, 2048MB 内存）；<br>3. 挂载持久化数据目录 `${NAS_DATA_DIR:-./data}:/app/data`；<br>4. 配置 `stop_grace_period: 30s` 与 `init: true`；<br>5. 配置自动化探针 `healthcheck`（`python -m src.worker --healthcheck`）。 |
| `.dockerignore` | [NEW] | 严格排除敏感文件、密钥、会话缓存与无关构建产物（`.git`, `.venv`, `data/`, `records.db*`, `*.lock`, `browser_state.json`, `.env` 等）。 |
| `.env.example` | [NEW] | 飞牛 NAS 部署环境变量模板，提供 `APP_UID`、`APP_GID`、`WORKER_MODE` 与 `NAS_DATA_DIR` 配置项。 |
| `config.template.json` | [NEW] | 生产环境业务配置模板，预置 `yuketang_server`（雨课堂）、`mode`（observe）、API 端点与安全延迟默认值。 |
| `requirements-worker.txt` | [NEW] | 纯无头 Worker 极轻依赖清单，剔除 Tkinter 等图形界面模块。 |
| `src/ai/service.py` | [MODIFY] | 1. 定义 `NO_THINKING_EXTRA_BODY = {"enable_thinking": False, "thinking": {"type": "disabled"}}`；<br>2. 适配 TokenDance / DeepSeek v4.1 Flash 推理大模型，彻底关闭思维链消耗，解决 128 tokens 截断与高时延瓶颈，端到端 RTT 由 ~31s 缩短至 1.58s。 |
| `src/config.py` | [MODIFY] | 1. `_ENV_KEYS` 新增 `CUSTOM_AI_BASE_URL` 与 `CUSTOM_AI_MODEL` 支持；<br>2. 增强 `yuketang_server` 与 `classroom_url` 之间的自适应解析。 |
| `src/worker.py` | [MODIFY] | 1. 运行模式解析优先级：命令行参数 (`--mode`) > 环境变量 (`RAINCLASS_MODE`/`WORKER_MODE`) > 配置文件 (`config.json`)；<br>2. 根据 `classroom_url` 自动推断匹配的雨课堂服务器（雨课堂/长江/荷塘/黄河）。 |
| `src/bot.py` | [MODIFY] | `_server_host` 支持根据 `classroom_url` 自适应推断服务器域名，消除跨校区会话校验误判。 |
| `docs/nas-deployment.md` | [NEW] | 飞牛 NAS 完整运维部署、会话导入、权限映射、监控探针、备份迁移与回滚实操手册。 |
| `docs/performance-report.md` | [NEW] | 端到端链路时延、TokenDance DeepSeek v4.1 Flash 实测时延评测与 NAS 容器运行时资源消耗报告。 |
| `tests/test_ai_service.py` | [MODIFY] | 更新单元测试中针对 `NO_THINKING_EXTRA_BODY` 禁用思维链的断言核验。 |

---

## 3. 四层验收体系执行成果 (Tier A ~ Tier D)

### 3.1 Tier A：本地代码基线与无回归验证
- **测试结果**：168 项全量单元测试与集成测试全部通过（`Ran 168 tests in 4.960s - OK`）。
- **逻辑时延 SLA**：DOM 题目检测 $\le 0.01\text{ms}$，就绪到 AI 请求发出 $\le 0.01\text{ms}$，AI 决策到提交 $\le 0.02\text{ms}$，SQLite WAL 写入与脱敏开销 $< 0.2\text{ms}$。

### 3.2 Tier B：飞牛 NAS 纯无头 Linux 容器环境验证
- **物理机规格**：Debian 12 (bookworm) / Linux `6.18.18.c1032-trim` (fnOS x86_64)，Docker `28.5.2`，Docker Compose `v2.40.3`。
- **无头浏览器与权限**：
  - 非 root 宿主用户 `lzk:Users` (`1000:1001`) 映射启动；
  - 数据卷文件（`health.json`, `records.db`, `browser_state.json`）均属于宿主用户，无权限越界或 permission denied；
  - 纯无头 Chromium 正常渲染页面，加载中文字体正常无乱码。

### 3.3 Tier C：NAS 旁听模式 (Observe Mode) 端到端真实运行验收
- **测试课堂**：`https://www.yuketang.cn/v2/web/studentLog/33446237?university_id=0&platform_id=3&classroom_id=33446237&content_url=`
- **测试凭据**：用户提供的 `sessionid` 与 `platform_id` Cookie 顺利完成认证与会话原子更新。
- **状态流转记录**：
  ```text
  [starting] -> 浏览器启动就绪 -> 导航进入雨课堂 -> 登录会话有效
  -> [waiting_class] -> 刷新索引页 -> 匹配到正在进行的课堂 -> 我去上课啦！
  -> [monitoring] -> 进入新的 PPT 页 (/ppt/52) -> 持续心跳健康监控
  ```
- **安全保障**：全流程 0 次选项点击、0 次自动提交、0 次外部付费 AI 分析。
- **优雅停机与恢复**：向容器发送 `SIGTERM`（`docker compose stop`），Worker 捕获信号平稳收尾，状态由 `monitoring` -> `stopping` -> `stopped`，退出码 `0`，重启后完整保持历史状态与数据库。

### 3.4 Tier D：真实大模型 API 集成测评 (TokenDance DeepSeek v4.1 Flash)
- **网关地址**：`https://tokendance.space/gateway/v1/chat/completions`
- **模型**：`deepseek-v4.1-flash`
- **优化对比**：
  - 默认思考链开启：耗尽 128 max tokens 且端到端耗时达 31.4 秒；
  - 思考链禁用优化：端到端往返时延骤降至 **1.58 秒**（TTFT 0.42s），输出 24 tokens 纯净标准 JSON 格式答案（`{"type":"single","answers":"A"}`）。

---

## 4. 容器运维命令速查

```bash
# 启动后台服务
docker compose up -d

# 查看容器状态与健康探针结果
docker compose ps

# 查看实时脱敏日志
docker compose logs -f

# 查看实时服务状态 JSON
cat data/health.json

# 优雅停止服务
docker compose stop

# 重建并热更新代码
docker compose build && docker compose up -d
```
