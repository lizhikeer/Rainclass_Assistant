# Rainclass Assistant 改造进度追踪 (Antigravity Progress)

## 1. 阶段概述

- **当前完成阶段**：第八阶段：网页扫码登录接入与体验完善（`08-扫码登录接入`）
- **当前代码分支**：`feature/stage-08-web-qr-login`
- **基线提交**：`9cc556b` (stage 7 complete)
- **交付状态**：已完成雨课堂网页端扫码登录流程全栈集成；通过真实 Playwright Chromium 验证了雨课堂 4 大校区服务器（`www.yuketang.cn`、`changjiang.yuketang.cn`、`pro.yuketang.cn`、`huanghe.yuketang.cn`）在无头环境下的扫码登录能力与 DOM 结构；实现专用的临时隔离无头浏览器会话管理、二维码内存临时提取（`Cache-Control: no-store`，不落地磁盘）、超时倒计时与刷新、人机安全验证障碍感知、扫码成功后的原子会话写入及后台 Worker 自动平滑恢复；通过全量自动化测试（198/198 测试全部绿灯）与真实 Playwright UI 测试。

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

---

## 5. 第六阶段核心修复与真实浏览器验收成果

### 5.1 缺陷修复清单 (P1 / P2 审查问题闭环)

| 缺陷编号 | 影响模块 | 问题现象与安全隐患 | 修复方案与设计决策 |
|---|---|---|---|
| **P1-1** | `src/browser.py` | 域名校验仅做子串包含判断，攻击者可通过恶意域名（如 `not-yuketang.example`）绕过会话校验。 | 重构 `validate_session_data`，增加标签边界匹配，仅允许 `yuketang.cn` 根域及其合法二级/多级子域名；严格校验必须包含有效 Cookie 列表、有效期与结构完整性。 |
| **P1-2** | `src/bot.py` | 教师在同路由同 URL 下切换新题目时，旧题尚未完成的在途 AI 结果返回后误点击了新题目的选项并触发错误提交。 | 1. 引入 `TaskIdentity`，绑定账号、课号、题号、代际号、截止期与路由路径；<br>2. 轮询及答案就绪返回时双重核对题目标识；发现新题立即废弃旧任务并中止选项点击与提交；<br>3. 区分占位符动态渲染与真正切题，保障 DOM 渐进式加载稳健。 |
| **P1-3** | `src/bot.py` | 主观题/填空题跳过或无视觉能力跳过时，状态被误标记为 `CONFIRMED`，导致存储与历史统计虚假成功。 | 彻底解耦 `_finish_question`：主观/跳过题在 storage 中记录为 `STAGE_SKIPPED`（`submission_confirmed=0`），提交超时未决记录为 `STAGE_UNKNOWN`，绝不覆写为 `CONFIRMED`。在内存中标记为 completed 避免重复发题。 |
| **P2-1** | `src/ai/strategy.py` | AI 超过单调截止期（deadline）后返回的慢答案仍被 strategy 接受为有效结果，可能引发作答超时违规。 | 在 `fast_single`、`race_first_valid` 和 `consensus` 中全面引入 `time.monotonic()` 严格截止期检查，超期返回的结果一律标记为已弃用并丢弃。 |
| **P2-2** | `src/worker.py` | `--login` 和 `--import-session` 在空数据目录下因读取不存在的 `config.json` 崩溃；坏会话导致死循环启动浏览器。 | 1. 配置加载失败时自动回退安全默认值（`DEFAULTS`），保证会话导入与登录可在空目录零配置直接执行；<br>2. 增加 `browser_state.json` 文件 `mtime` 监控，相同损坏会话不重复拉起浏览器空转 10 秒。 |
| **P2-3** | `src/ai/service.py` | `extra_body` 参数直接硬编码思考禁用，锁定特定供应商参数，且与旧测试环境中的配置对象不兼容。 | 新增 `_resolve_extra_body` 方法，支持通过 `custom_ai_extra_body` 配置自定义参数；配置兼容字典、JSON 字符串及通用配置对象；默认保留推理速度优化，供应商完全解耦。 |
| **P2-4** | `Dockerfile` | 基础镜像依赖未锁定的 `python:3.11-slim`，且构建中依赖 `|| true` 掩盖依赖缺失，易导致构建结果不确定。 | 锁定 Debian 版本为 `python:3.11-slim-bookworm`；移除 `|| true`；改用显式判断，支持宿主预缓存与在线安装无缝兼容。 |

### 5.2 真实 Playwright Chromium 100 轮基准测量

- **测试工具**：`tests/benchmark_real_browser.py`
- **运行环境**：真实本地 Headless Chromium，真实 DOM 加载与交互，50ms AI 延时桩
- **执行轮数**：100 轮连续评测，**成功率 100.0%**
- **各阶段耗时分布**：
  - 题目检测与就绪 (`detect_to_ready_ms`): 平均 0.0ms | P95 0.0ms
  - 真实 Chromium 截图准备 (`ready_to_ai_start_ms`): 平均 95.5ms | P50 94.0ms | P95 109.0ms | P99 110.0ms | Max 110.0ms（SLA $\le 500\text{ms}$，**PASS**）
  - AI 桩推理延迟 (`ai_duration_ms`): 平均 57.4ms | P95 63.0ms | Max 78.0ms
  - 真实 DOM 选项查找与点击 (`ai_to_validated_ms`): 平均 47.6ms | P50 47.0ms | P95 62.0ms | P99 63.0ms | Max 63.0ms（SLA $\le 500\text{ms}$，**PASS**）
  - 真实提交点击与按钮消失确认 (`clicked_to_confirmed_ms`): 平均 38.6ms | P50 32.0ms | P95 47.0ms | Max 47.0ms
  - **端到端全链路耗时** (`total_end_to_end_ms`): 平均 248.4ms | P50 250.0ms | P95 250.0ms | P99 266.0ms | Max 266.0ms

### 5.3 测试验收证据

- 回归测试集：`tests/test_review_regressions.py` 覆盖 6 大审查问题，**6/6 PASS**。
- 全量自动化测试：`python -m unittest discover -s tests -p "test_*.py"`，**174/174 PASS（Ran 174 tests in 3.007s - OK）**。
- 独立诊断探针：`review_20260928_probes.py`，全部 6 项断言核验通过。

---

## 6. 第七阶段就绪清单 (Stage 7 Entry Readiness Checklist)

- [x] **缺陷闭环**：审查报告识别出的所有 P1/P2 缺陷已全部修复，回归测试与全量测试 100% 绿灯。
- [x] **真实浏览器验收**：Playwright Chromium 100 轮连续基准测试通过，元素操作与截图时延真实达标。
- [x] **文档对齐**：性能报告与 NAS 部署文档已更新至最新真实数据，移除过时配置。
- [x] **NAS 运行安全**：远端生产容器运行正常且不受本地阶段开发影响。
- [x] **阶段 7 规划就绪**：
  - 模仿 `hdu-grabber` 暗黑风格界面（主色调 `#0f1419`）
  - Caddy 统一网关：HTTPS `:40010`，`basic_auth`（用户名 `LzK`，密码 `LzK`）
  - 后端轻量 API：状态机监控、日志推流、配置修改、会话导入与远程重启

---

## 7. 第七阶段新增与修改的组件清单

| 模块/文件 | 变更类型 | 关键设计与职责 |
|---|---|---|
| `requirements-worker.txt` | [MODIFY] | 增补轻量 Web 与异步框架依赖：`fastapi>=0.110.0`, `uvicorn>=0.28.0`, `python-multipart>=0.0.9`。 |
| `src/config.py` | [MODIFY] | 优化 `Config.save()`：基于全量已有数据与更新补丁的合并体 (`merged`) 执行校验，支持部分字段安全热更新；增加 `__contains__` 字典操作支持。 |
| `src/web/manager.py` | [NEW] | **无状态 Worker 单实例守护进程管理器**：<br>1. 基于 `subprocess.Popen` 单例启动、优雅停机（`SIGTERM` -> 3.0s 超时升级 `SIGKILL`）与重启；<br>2. 运行期望状态（`desired_mode`）独立持久化；<br>3. `LogRingBuffer`：环形内存日志（容量 1000 行），自动剔除 ANSI 控制字符，自动对 `sk-`、`sessionid`、Cookie 等敏感字段执行正则掩码脱敏，支持 SSE 订阅者分发；<br>4. 答题审计动态列兼容查询（自动适配 SQLite schema 字段）；<br>5. 安全会话导入（严格校验雨课堂域名与 Cookie 结构并原子覆盖写入）；<br>6. 配置脏状态检查（对比内存与磁盘配置）。 |
| `src/web/app.py` | [NEW] | **FastAPI 后端应用与 RESTful 控制路由**：<br>1. `GET /api/status`：获取当前实时状态、运行时长、配置变更提示；<br>2. `POST /api/control`：控制 Worker 动作（`start` / `stop` / `restart` / `set_mode`）；<br>3. `GET /api/config`：安全脱敏读取当前业务配置；<br>4. `POST /api/config`：保存业务配置（支持保持脱敏原有密钥）；<br>5. `POST /api/session`：接收文件上传或 JSON 粘贴导入新凭证；<br>6. `GET /api/records`：答题历史分页与成功率统计；<br>7. `GET /api/logs` & `GET /api/logs/stream`：实时获取或 SSE 持续推流脱敏控制台日志；<br>8. `POST /api/ai/test`：单次大模型 API 连通性测试；<br>9. `GET /api/health`：容器健康探针端点；<br>10. 挂载 `/static` 静态前端资源目录。 |
| `src/web/static/style.css` | [NEW] | **HDU-grabber 风格暗黑主题样式**：主色调 `#0f1419`，卡片 `#192734`，主按键与强调色 `#1d9bf0`，边框 `#2f3336`，内建徽章状态颜色体系（绿色运行、黄色警告、灰色离线）、代码字体与响应式网格断点。 |
| `src/web/static/index.html` | [NEW] | **现代化响应式单页管理界面**：包含导航栏（服务徽章与一键重启）、五大功能选项卡（仪表盘监控与快捷启停、配置中心与 AI 连通性测试、会话凭证导入与有效期诊断、答题历史审计表格、实时控制台日志与自动滚屏）。 |
| `src/web/static/app.js` | [NEW] | **前端轻量响应式交互引擎**：纯原生 ES6 编写，内建 3 秒轮询状态同步、SSE 实时流断线重连、防抖控制请求、二次确认弹窗、动态表格渲染、无外部繁重前端依赖包。 |
| `Caddyfile` | [NEW] | **生产级 HTTPS 反向代理网关配置**：监听 `:40010`，内建 `basic_auth`（用户名 `LzK`，密码 `LzK` bcrypt 哈希），支持 `${CADDY_TLS_DIRECTIVE}` 注入 NAS 通配证书或内部自签，反向代理应用并强制 `flush_interval -1` 保障 SSE 即时推送。 |
| `compose.panel.yaml` | [NEW] | **双容器生产部署编排定义**：编排 `rainclass-app` 与 `rainclass-caddy`，分配安全非 root 权限 `1000:1001`，1GB 共享内存，2.0 CPU / 2048M 内存限制，挂载 NAS 证书卷与应用数据持久卷。 |
| `.env.example` | [MODIFY] | 增补 `BASE_IMAGE`、`NAS_CERTS_DIR` 与 `CADDY_TLS_DIRECTIVE` 模板项。 |
| `tests/test_web_manager.py` | [NEW] | 针对 `ProcessManager` 的 6 项单元测试（脱敏规则、环形队列淘汰、启停幂等性、会话导入校验、数据库查询兼容性）。 |
| `tests/test_web_api.py` | [NEW] | 针对 FastAPI RESTful 接口的 8 项集成测试（状态、控制、配置掩码与保存、会话上传、答题记录、静态页面挂载）。 |
| `tests/test_web_ui_playwright.py` | [NEW] | 真实 Playwright Chromium 无头浏览器端到端 UI 测试（覆盖桌面 1280x800 与移动端 375x667、主题色 `#0f1419` 校验、全部 5 个 Tab 页面切换核验）。 |

---

## 8. 第七阶段验收成果与测试证据

- **全量自动化测试**：`python -m unittest discover -s tests -p "test_*.py"`，**189/189 PASS（Ran 189 tests in 11.540s - OK，0 失败，0 错误）**。
- **UI 真实浏览器渲染核验**：Playwright Headless Chromium 测试全部通过，验证了 HDU 风格暗黑调背景色（`rgb(15, 20, 25)` / `#0f1419`）、5 大选项卡切换平滑、桌面与移动端响应式布局均正常无破损。
- **安全与权限边界核验**：
  - Web API 密钥输出均为掩码（`sk-***`），避免前端泄露；
  - 会话导入严格执行 `validate_session_data` 校验（拒绝非法域名与畸形 Cookie）；
  - Caddy 网关实施 HTTP Basic Auth 保护（用户名 `LzK`，密码 `LzK`），密码在配置文件中经 bcrypt 强哈希固化。

---

## 9. 第八阶段完成清单 (Stage 8 Completion Checklist)

- [x] **真实站点无头扫码能力验证**：通过真实 Playwright Chromium 对雨课堂四大服务器（雨课堂/长江/荷塘/黄河）登录页实际元素抽检，证实 `#qrcode-box img.logma` 均能稳定提供完整扫码二维码。
- [x] **QRLoginManager 专用会话管理**：
  - 启动前自动检查并优雅暂停运行中的 Worker，杜绝同账号双开与会话覆盖竞态；
  - 采用 `threading.RLock` 与单会话锁机制，严格禁止多实例重复启动；
  - 二维码仅在内存中以 Base64 Data URL 临时持有并推送到前端，绝不写入公开静态目录；
  - HTTP 接口严格配置 `Cache-Control: no-store, no-cache, must-revalidate`；
  - 实时感知人机安全验证障碍（如腾讯滑块或 hCaptcha），及时提示并优雅回退至本地 CLI 导入；
  - 180 秒严格超时与主动取消机制，退出时安全销毁 Playwright 浏览器上下文；
  - 扫码成功后使用 `validate_session_data` 校验，临时文件 + `os.replace` 原子写入 `browser_state.json`；
  - 自动平滑恢复 Worker 至登录前期望的运行模式（`observe` / `auto`）。
- [x] **前端 Web 体验完善**：
  - 在 HDU 风格管理面板“会话管理”选项卡首屏嵌入“📱 网页扫码登录”模块；
  - 支持快捷选择雨课堂 4 大校区站点；
  - 实时倒计时（180s）、二维码过期遮罩与一键刷新、主动取消登录按键；
  - 成功/失败/需本地验证等状态图标与 Toast 动态提示；
  - 保留原有拖拽与粘贴 JSON 作为可靠备用回退。
- [x] **自动化测试与端到端 UI 验证**：
  - 新增 `tests/test_web_qr_login.py`（9 项单元与 API 集成测试全部通过）；
  - 更新 `tests/test_web_ui_playwright.py`（真实无头 Chromium UI 渲染与 Tab 切换测试全部通过）；
  - 全量自动化测试：**198/198 PASS（Ran 198 tests in 9.561s - OK）**，0 失败，0 错误。

---

## 10. 第八阶段新增与修改的组件清单

| 模块/文件 | 变更类型 | 关键设计与职责 |
|---|---|---|
| `src/web/login_manager.py` | [NEW] | **雨课堂网页扫码登录生命周期管理器 (QRLoginManager)**：<br>1. 线程安全（`threading.RLock`）的独立登录任务守护；<br>2. 专用 Headless Chromium 实例隔离运行，自动定位 `#qrcode-box img.logma`；<br>3. 二维码纯内存安全提取，绝不落地磁盘；<br>4. 智能识别扫码成功、超时过期、人机滑块验证等多种状态；<br>5. 登录成功后原子更新 `browser_state.json` 并自动恢复 Worker 期望运行模式。 |
| `src/web/app.py` | [MODIFY] | **新增扫码登录 RESTful 控制端点**：<br>1. `POST /api/login/qr/start`：启动扫码登录任务（响应头注入 `Cache-Control: no-store`）；<br>2. `GET /api/login/qr/status`：获取当前扫码状态与临时二维码数据；<br>3. `POST /api/login/qr/refresh`：重新请求最新二维码；<br>4. `POST /api/login/qr/cancel`：用户主动取消登录流程。 |
| `src/web/manager.py` | [MODIFY] | 为 `ProcessManager` 增加 `desired_mode` 属性与 `get_desired_mode()` 访问方法，与 `login_manager` 形成优雅契约。 |
| `src/web/static/index.html` | [MODIFY] | 在“会话管理”选项卡首屏新增“📱 网页扫码登录”卡片，包含服务器选择下拉框、开始/刷新/取消控制按钮、二维码展示容器、过期遮罩与倒计时指示器。 |
| `src/web/static/app.js` | [MODIFY] | 增补网页扫码登录前端控制逻辑（`initQRLogin`、`startQRLogin`、`startQRPolling`、`renderQRStatus`、`refreshQRLogin`、`cancelQRLogin`），支持 1.5 秒动态轮询、断线容错与状态机驱动渲染。 |
| `tests/test_web_qr_login.py` | [NEW] | 9 项针对 `QRLoginManager` 与 FastAPI 路由的单元测试与接口集成测试。 |
| `tests/test_web_ui_playwright.py` | [MODIFY] | 增补对“会话管理”页面扫码登录卡片、4 所高校服务器选项与开始按键的真实 Chromium 渲染核验。 |

---

## 11. 阶段 6 至 8 全流程改造总览与成果汇总

经过第 6、7、8 三个关键阶段的严密实施与工程闭环，Rainclass Assistant 已成功完成由底层核心修复到上层 Web 统一管理的全面跃升：

1. **阶段 6（修复与真实浏览器验收）**：
   - 彻底闭环所有 P1/P2 安全缺陷（精确域名边界防御、同路由切题旧答案阻断、题目终态与统计解耦、AI 绝对截止期丢弃、extra_body 解耦自适应与 Dockerfile 标签固化）；
   - 完成真实 Playwright Chromium 100 轮连续基准测评（成功率 100.0%，端到端 P95 250ms）。
2. **阶段 7（HDU 风格 Web 面板与 Caddy 网关）**：
   - 实现了基于 FastAPI + HDU 暗黑风格（`#0f1419`）的完整单页应用；
   - 搭建 Caddy 2 HTTPS 反向代理（`:40010`，Basic Auth `LzK` / `LzK`，SSE 实时日志流推）；
   - 提供双容器编排配置 `compose.panel.yaml`，完全复用现有 NAS 数据目录与非 root 权限。
3. **阶段 8（网页扫码登录与体验完善）**：
   - 实现了无头环境下的雨课堂网页扫码登录全流程闭环；
   - 免除本地 PC CLI 导出并上传的繁琐操作，在手机端即可直接完成微信/雨课堂 APP 扫码登录；
   - 具备内存提取、互斥防重、人机验证感知、超时自愈与原子会话更新机制；
   - 全项目 198 项自动化测试全部 100% 绿灯通过。



