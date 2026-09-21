# Rainclass Assistant 改造进度追踪 (Antigravity Progress)

## 1. 阶段概述

- **当前完成阶段**：第一阶段：后台入口与性能基线（`01-后台入口与性能基线.md`）
- **当前代码分支**：`feature/stage-01-worker-baseline`
- **基线提交**：`1372bcce03a8844994338267f971110d001c7936` (master, 2026-09-17)
- **交付状态**：已完成全部实现并通过本地受控测试与全量回归测试。

---

## 2. 实际新增与修改的组件

| 模块/文件 | 变更类型 | 关键设计与职责 |
|---|---|---|
| `src/log.py` | [NEW] | 提取原 `main.py` 中的 `_MaxLogLengthFilter`、`_bot_log_namer` 与 `QueueListener` 异步日志体系，解除 GUI / CustomTkinter 依赖；增加控制台编码容错（避免 Windows GBK 下特殊字符报错）。 |
| `src/paths.py` | [NEW] | 统一管理数据根目录（`data_dir`，支持 `--data-dir` 和 `RAINCLASS_DATA_DIR`），规范 `config.json`、`browser_state.json`、`.rainclass-assistant.lock`、`log/`、`debug/` 及 `metrics/` 路径；提供静态资源确定性检索。 |
| `src/config.py` | [MODIFY] | 新增 `ConfigError` 异常与严格加载方法 `Config.load_strict()`；增加 `mode`（`observe` / `auto`）与 `auto_sign_in`（解耦签到与答题）；强化字段类型与取值范围校验。 |
| `src/browser.py` | [MODIFY] | 新增 `auto_install: bool = False` 参数控制，后台启动时验证已有 Chromium 而不静默下载；新增 `validate_session()` 与强化的 `has_session` 校验，拦截空文件与非法 JSON。 |
| `src/timing.py` | [NEW] | 实现 `QuizTimingTracker`，使用 `time.monotonic()` 结构化记录 7 个核心节点耗时（`question_detected`, `question_ready`, `ai_request_started`, `ai_response_received`, `answer_validated`, `submit_clicked`, `submit_confirmed`）；脱敏记录匿名 ID 并同步追加至 `quiz_timings.jsonl`。 |
| `src/bot.py` | [MODIFY] | 引入 `BotState` 状态机；支持 `mode: "auto" | "observe"`（观察模式严格禁止签到、选项点击、提交与真实 AI 请求）；将签到控制独立为 `auto_sign_in`；会话缺失/失效时进入 `needs_login` 状态并安全停止，不再失控轮询；打通各环节结构化事件计时。 |
| `src/worker.py` | [NEW] | 纯命令行无 GUI 后台独立入口（`python -m src.worker`），支持 `--config`、`--data-dir`、`--mode`、`--check`、`--wait-for-session`；注册 SIGINT/SIGTERM 优雅停机；定义退出码契约（0: 正常, 1: 错误, 2: `needs_login`）。 |
| `tests/fixtures/` | [NEW] | 提供离线本地 HTML 夹具：`login_success.html`、`classroom_home.html`、`exercise_single.html`。 |
| `tests/test_worker.py` | [NEW] | 新增 8 项专门测试：验证无 GUI 依赖、配置缺失/损坏报错、会话缺失/损坏报错、observe 模式零动作行为、auto 模式分段耗时落盘等。 |
| `tests/benchmark_baseline.py` | [NEW] | 本地受控分段耗时基准评测工具，支持自定义轮数与模拟延迟，输出 P50/P90/P95/Max 分布报告。 |
| `main.py` | [MODIFY] | 复用 `src.log` 日志底层，保留原有别名与 GUI 兼容性。 |
| `tests/test_ai_service.py` | [MODIFY] | 日志过滤器导入改为 `from src.log import _MaxLogLengthFilter`，使 AI 模块单元测试脱离桌面 UI 依赖。 |

---

## 3. 状态机、退出码与配置契约

### 3.1 运行状态机 (`BotState`)
- `IDLE`: 初始化完成，尚未启动
- `NEEDS_LOGIN`: 会话文件缺失、损坏或无法通过登录验证，输出修复指引
- `WAITING_FOR_CLASS`: 登录有效，正在时间窗口内检测/等待课程
- `IN_CLASSROOM`: 已进入课堂主循环，正在监听习题
- `STOPPED`: 收到停止信号后正常收尾
- `ERROR`: 启动失败或运行中发生致命未捕获异常

### 3.2 进程退出码
- **`0` (EXIT_OK)**: 正常退出（通过 SIGINT/SIGTERM 信号优雅停机，或 `--check` 自检通过）
- **`1` (EXIT_ERROR)**: 异常退出（配置语法/类型错误、单实例锁冲突、浏览器底层致命异常）
- **`2` (EXIT_NEEDS_LOGIN)**: 需要导入登录态（`browser_state.json` 不存在或已失效，容器/守护进程可据此判定无需失控重启）

### 3.3 核心配置项
```json
{
  "mode": "observe",          // 运行模式："observe" (观察模式) 或 "auto" (自动答题)
  "auto_sign_in": true,       // 是否自动签到（与是否答题解耦）
  "headless_mode": true,      // 后台 Worker 固定为 true
  "submit_delay": 1,          // 提交延迟（秒，快速配置可设为 0）
  "check_interval": 60,       // 课程巡检间隔（秒）
  "quiz_refresh_interval": 1  // 课堂内题目刷新间隔（秒）
}
```

---

## 4. 测试与验证结果

### 4.1 单元与回归测试
- **执行命令 1 (虚拟环境)**：
  ```bash
  .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
  ```
  **结果**：125 项测试全部通过（`Ran 125 tests in 1.199s - OK`）。
- **执行命令 2 (系统原生 Python，无 customtkinter 依赖)**：
  ```bash
  python -m unittest discover -s tests -p "test_*.py"
  ```
  **结果**：125 项测试全部通过（`Ran 125 tests in 1.597s - OK`），成功解除对桌面 GUI 库的导入依赖。

### 4.2 观察模式 (`observe`) 验证证据
- 在 `tests/test_worker.py::test_observe_mode_strictly_prohibits_actions` 中实测证明：
  - 签到按钮点击次数：`0`
  - 选项点击次数：`0`
  - 提交按钮点击次数：`0`
  - 真实 AI 请求发起次数：`0`
  - 页面观察、HTML 保存与日志正常运作。

### 4.3 第一阶段分段耗时基线测量输出
运行受控基准命令：
```bash
python -m tests.benchmark_baseline --rounds 50 --ai-delay 0.05
```
**测量结果**：
```text
================================================================================
第一阶段性能基线测量报告（本地模拟流水线，共 50 轮）
运行条件：模拟 AI 附加延迟 = 50.0ms, submit_delay = 0
================================================================================
分段阶段 (Stage)               | P50 (ms)  | P90 (ms)  | P95 (ms)  | Max (ms)  | Mean (ms)
--------------------------------------------------------------------------------
1. 题目检测->就绪 (Detect->Ready) | 0.00      | 0.00      | 0.00      | 0.00      | 0.00     
2. 就绪->发起请求 (Ready->AI Req) | 0.00      | 0.00      | 0.00      | 0.00      | 0.00     
3. AI 推理往返 (AI Latency)       | 63.00     | 78.00     | 79.00     | 94.00     | 66.80    
4. 答案生效校验 (AI->Validated)   | 0.00      | 0.00      | 0.00      | 0.00      | 0.00     
5. 点击提交按钮 (Validated->Click)| 0.00      | 0.00      | 0.00      | 0.00      | 0.00     
6. 确认提交生效 (Click->Confirm)  | 0.00      | 0.00      | 0.00      | 0.00      | 0.00     
7. 端到端总耗时 (Total E2E)       | 63.00     | 78.00     | 79.00     | 94.00     | 66.80    
================================================================================
```
*注：本地纯代码层与 DOM 桩调度耗时均为亚毫秒级（<1ms），基线测量表明程序自身调度开销极小。真实课堂耗时瓶颈在于网络轮询、平台消息到达与远程 AI 视觉模型推理。*

---

## 5. 配置迁移与回滚说明

- **兼容性**：原有桌面端启动入口 `main.py` 完整保留并正常工作；`Config` 在普通模式下仍保留对缺失配置文件的默认值回退能力；`tests/test_ai_service.py` 仍可被标准测试加载器直接发现。
- **回滚操作**：若需回退本阶段改动，可直接切回 `master` 分支（`git checkout master`），无需删除或重置任何历史数据。

---

## 6. 剩余问题与第二阶段入口

### 6.1 剩余问题与优化点（由第二阶段承接）
1. **课堂内轮询间隔**：目前 `_run_classroom_loop()` 中的 `quiz_refresh_interval` 为秒级整数（最小 1 秒），对新题发现存在最大 1 秒的固有离散延迟。
2. **同页与新标签页等待延迟**：点击“你有新的课堂习题”后，当前实现对新标签页等待最长达 3 秒；即使题目直接在原页呈现，也未提前结束该等待。
3. **提交等待参数**：默认配置 `submit_delay` 为 1 秒，需在安全确认选项生效后允许配置为 0 并即时触发。
4. **HTML 诊断保存**：目前每道题均无条件调用 `page.content()` 落盘完整 HTML，需增加按需开关，避免在答题关键链路上产生无谓的 I/O 阻塞。

### 6.2 下一阶段入口
- 下一阶段提示词：`Antigravity分阶段提示词/02-新题检测与提交提速.md`。
