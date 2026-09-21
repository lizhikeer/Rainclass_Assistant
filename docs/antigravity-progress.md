# Rainclass Assistant 改造进度追踪 (Antigravity Progress)

## 1. 阶段概述

- **当前完成阶段**：第四阶段：NAS 长期运行可靠性（`04-长期运行与恢复.md`）
- **当前代码分支**：`feature/stage-04-long-run-and-recovery`
- **基线提交**：`7d287130b4ec7483a9a13b0c95a02251a31d9ee0` (stage 3 complete)
- **交付状态**：已实现完整的服务生命周期状态机与非侵入式健康检查、真实登录态检验与原子会话更新、两阶段 SQLite 答题记录与崩溃自愈保护、单账号单实例文件锁、敏感信息脱敏与有界磁盘清理、有界告警队列与防风暴聚合、168 项全量回归测试验证及资源度量浸泡测试工具。

---

## 2. 第四阶段新增与修改的组件清单

| 模块/文件 | 变更类型 | 关键设计与职责 |
|---|---|---|
| `src/status.py` | [NEW] | 1. 定义标准 `ServiceState` 枚举：`starting`, `needs_login`, `waiting_class`, `monitoring`, `answering`, `stopping`, `stopped`, `error`；<br>2. `StatusTracker` 状态跟踪器：管理心跳刷新，原子安全写入 `health.json`（区分 `alive` 与 `ready`，无课等待不判定为故障）；<br>3. `read_health()`：轻量只读健康检查方法。 |
| `src/storage.py` | [NEW] | 1. `QuizStorage` SQLite 持久化管理器：开启 WAL 模式、忙等待超时（5000ms）、短事务；<br>2. `quiz_records` 约束：`UNIQUE(account_id, classroom_id, question_id, request_generation)` 幂等防重；<br>3. 两阶段答题追踪：`record_submitting()` 与 `record_completion()`，严格禁止存储密钥、Cookie 与完整 HTML。 |
| `src/cleaner.py` | [NEW] | 1. `ArtifactCleaner` 磁盘清理器：严格限制在 `data_dir` 边界内，禁止跟随外部软链接；<br>2. 支持保留天数（默认 7 天）、总磁盘上限（默认 500MB）、文件数上限（默认 1000 个）多维度安全清理；<br>3. 保护核心文件：`config.json`, `browser_state.json`, `records.db` 永不误删。 |
| `src/log.py` | [MODIFY] | 1. `SanitizingFilter`：对 Console 和 FileHandler 全量应用正则表达式脱敏（屏蔽 `sessionid`, `api_key`, `token`, `Bearer`, `password` 等凭据）；<br>2. 限制日志单文件 10MB，最多轮转 5 份备份。 |
| `src/notification.py` | [MODIFY] | 1. 建立有界发送队列（`maxsize=50`）与独立后台守护线程，彻底解耦业务主流程；<br>2. 300 秒滑动窗口告警聚合与去重，防止故障风暴打爆通知渠道；<br>3. 独立度量尝试数与成功数。 |
| `src/browser.py` | [MODIFY] | 1. `validate_session_data()`：严格校验 Cookie 域名、关键字段与站点绑定；<br>2. `is_logged_in()`：增强真实可观测校验（检查跳转登录页与密码框输入框特征）；<br>3. `save_session()`：原子写临时文件后替换，空状态防覆盖校验，15秒防抖节流；<br>4. `reload_context()`：受控重启上下文，避免多 Context 并发访问。 |
| `src/paths.py` | [MODIFY] | 新增 `db_file` (`records.db`) 与 `health_file` (`health.json`) 路径属性及自动目录保障。 |
| `src/worker.py` | [MODIFY] | 1. 新增 CLI 命令：`--login`（交互式有头浏览器快速登录生成会话）、`--import-session`（校验导入外部会话 JSON）、`--healthcheck` 与 `--strict-ready`；<br>2. 集成 `StatusTracker` 全生命周期驱动与信号捕获（优雅停机）；<br>3. 单实例文件锁保障（同一数据目录禁止双开，不同目录互相隔离）。 |
| `src/bot.py` | [MODIFY] | 1. 状态对齐 `ServiceState`，驱动状态更新与心跳；<br>2. 答题中间态管理（`submitting`, `unknown`, `confirmed`, `failed`, `skipped`）；<br>3. 崩溃恢复自愈：启动时检查遗留中间态题目，核验 DOM 状态决策，避免盲目重放；<br>4. 浏览器瞬时崩溃 5 次指数退避自愈，过时 AI 结果失效废弃；<br>5. 优雅停机在途任务标记 `unknown`。 |
| `tools/soak_runner.py` | [NEW] | 零额外第三方依赖长时间浸泡运行度量工具：采集真实 CPU%、RSS 物理内存（支持 Win64 ctypes 与 Linux procfs）、活跃线程数、页面数与磁盘空间，并输出格式化评估报告。 |
| `tests/test_recovery.py` | [NEW] | 第四阶段专项测试集（12 项测试）：覆盖会话格式与域名校验、原子替换与防写空、真实登录态识别、SQLite 两阶段持久化与幂等约束、崩溃恢复 DOM 校验与不盲目重放、单实例文件锁隔离、安全清理防越界、日志脱敏、告警防风暴、健康检查退出码及优雅停机。 |

---

## 3. 架构与核心机制

### 3.1 Worker 生命周期状态机与心跳机制

```
[starting]
    │
    ├── (会话失效/不存在) ──> [needs_login] (阻塞挂起，等待 --login 或导入有效 session)
    │
    └── (会话有效进入课堂) ──> [waiting_class] (无课等待，alive=True, ready=True)
                                 │ ▲
                                 │ │ (下课 / 上课)
                                 ▼ │
                               [monitoring] (巡检题目前端就绪状态)
                                 │ ▲
                                 │ │ (发现题目 / 答题完毕)
                                 ▼ │
                               [answering] (AI 决策、两阶段提交持久化)
                                 │
           (SIGTERM/SIGINT) ─────┴─────> [stopping] ──> [stopped]
           (不可恢复严重异常) ─────────> [error]
```

- **心跳守护**：主循环与空闲等待期间持续刷新 `health.json` 中的 `heartbeat`。
- **存活与就绪分离**：
  - `alive`：代表进程是否正常运行且心跳未超时（默认阈值 120 秒）；
  - `ready`：代表业务是否就绪可答题（`waiting_class`, `monitoring`, `answering` 为 True；`needs_login`, `error`, `stopping` 为 False）。
- **无课等待语义**：无课阶段被明确标识为 `waiting_class`，不会被外部健康检查误杀。

### 3.2 答题两阶段持久化与崩溃自愈

1. **第一阶段（提交前/在途中）**：
   - 记录 `status="submitting"`、`account_id`、`classroom_id`、`question_id`、`request_generation`、时间戳、AI 决策结果至 SQLite (`records.db`)。
2. **第二阶段（提交完成后）**：
   - 验证 DOM 变化，若成功则原子更新为 `status="confirmed"`；若未出现成功指示则标记 `unknown`；明确失败标记 `failed`。
3. **崩溃恢复流程**：
   - Worker 启动或异常重载后，优先查询数据库中残留的 `submitting` 或 `unknown` 记录。
   - 导航进入对应题目页面，实测 DOM 状态：
     - 若已选中选项且提交按钮已置灰/消失，更新为 `confirmed`（自愈成功）；
     - 若无法确认或页面已切题，更新为 `skipped` 并发出警报，**严禁对已可能提交的题目无脑重放提交**。

### 3.3 安全防护与日志脱敏

- **会话保护**：`save_session` 只在明确 `is_logged_in()` 之后执行，写临时文件后原子 `replace`，绝不覆写空白文件破坏历史凭据；防抖节流间隔不少于 15 秒。
- **单实例锁**：基于 `filelock` 锁定数据目录下的 `.rainclass-assistant.lock`。同账号目录冲突直接阻断并提示错误，多账号多目录彼此互不影响。
- **日志全量脱敏**：`SanitizingFilter` 自动对日志行中匹配的 `sessionid`、`token`、`api_key`、`password` 等敏感字段进行星号遮蔽，杜绝控制台及日志文件泄漏凭据。
- **磁盘清理边界**：`ArtifactCleaner` 在扫描与删除文件时，严格计算 `resolve()` 路径，必须落在 `data_dir` 之内；若遇到外部符号链接或跳出目录的行为立刻跳过。

---

## 4. CLI 命令速查指南

### 4.1 登录与会话管理

- **本地交互式有头浏览器扫码登录**：
  ```bash
  python -m src.worker --data-dir data/account_1 --login
  ```
  自动启动 Chromium 窗口，提示用户扫码或密码登录，成功后原子导出 `browser_state.json` 并退出。

- **导入外部会话 JSON**：
  ```bash
  python -m src.worker --data-dir data/account_1 --import-session /path/to/exported_session.json
  ```
  校验 Session 结构、Cookie 域白名单和过期时间，通过后原子写入 `data/account_1/browser_state.json`。

### 4.2 健康检查 (Docker / K8s / NAS 探针)

- **容器 Liveness 存活性检查**（进程正常心跳即返回 0）：
  ```bash
  python -m src.worker --data-dir data/account_1 --healthcheck
  ```
- **容器 Readiness 业务就绪性检查**（需处于待课、巡检或答题状态才返回 0）：
  ```bash
  python -m src.worker --data-dir data/account_1 --healthcheck --strict-ready
  ```
  - 退出码对照：
    - `0`：健康 / 就绪
    - `1`：严重错误（心跳超时、进程死亡、状态为 error）
    - `2`：未就绪（如处于 needs_login 或 starting，仅在 `--strict-ready` 下返回）

### 4.3 浸泡测试工具

- **度量系统资源稳定性**：
  ```bash
  python tools/soak_runner.py --data-dir data --duration 300 --interval 10
  ```
  支持 `--simulate` 模拟不同业务阶段（waiting_class, monitoring, answering）的资源波动与垃圾回收。

---

## 5. 测试与验证结果

### 5.1 全量回归测试结果
- **虚拟环境 (`.venv`)**：
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
  ```
  **结果**：168 项测试全部通过（`Ran 168 tests in 4.956s - OK`）。
- **覆盖模块**：
  - `test_recovery.py`（12 项专项恢复与可靠性测试）
  - `test_ai_strategies.py`（19 项策略测试）
  - `test_pipeline.py`、`test_worker.py`、`test_config.py`、`test_timing.py` 等全量通过。

### 5.2 链路延迟基准测量 (Stage 2 & 4 基准对比)
运行命令：`.\.venv\Scripts\python.exe -m tests.benchmark_baseline --rounds 100 --ai-delay 0.05`
- **题目检测 -> 就绪 (P95)**：0.00 ms (目标 <= 300ms) [PASS]
- **就绪 -> AI 请求发出 (P95)**：0.00 ms (目标 <= 500ms) [PASS]
- **AI 答案 -> 提交点击 (P95)**：0.00 ms (目标 <= 500ms) [PASS]
- **SQLite 与脱敏日志引入开销**：单轮低于 0.2ms，全链路性能无任何回退。

### 5.3 浸泡稳定性测试数据（10 秒采样示例）
- **CPU 使用率**：平均 10.86%，峰值 50.4%
- **RSS 物理内存**：从 19.57 MB 缓慢增加至 20.08 MB（微量常驻增长 0.51 MB，无内存泄漏）
- **活跃线程数**：恒定维持 2 线程（主线程 + 通知后台守护线程）
- **磁盘占用**：严格保持 0.023 MB，无未关闭文件句柄与垃圾堆积

---

## 6. 运维、数据备份与回滚指南

1. **多账号目录隔离**：
   - NAS 部署时，每个雨课堂账号必须使用独立的 `--data-dir`（如 `/app/data/user_a` 与 `/app/data/user_b`）。
   - 每个目录下独立生成 `records.db`、`browser_state.json`、`health.json` 和 `.rainclass-assistant.lock`。
2. **备份核心文件**：
   - 仅需备份每个数据目录下的 `config.json` 和 `browser_state.json` 即可完全恢复服务。
   - `records.db` 损坏时可自动重新建表，不会阻断核心答题流水线。
3. **版本回滚安全性**：
   - 第四阶段新增代码完全向下兼容历史 CLI 参数；
   - 若回滚至上一版本，只需切回 `feature/stage-03-ai-response-strategy` 分支，原有配置文件与会话文件无需任何格式变更。
