# Rainclass Assistant 改造进度追踪 (Antigravity Progress)

## 1. 阶段概述

- **当前完成阶段**：第二阶段：优先消除程序自身等待与提交提速（`02-新题检测与提交提速.md`）
- **当前代码分支**：`feature/stage-02-detect-and-submit-speed`
- **基线提交**：`1372bcce03a8844994338267f971110d001c7936` (master, 2026-09-17)
- **交付状态**：已完成全部核心功能实现并通过真实 Chromium 夹具测试、137 项全量回归测试与 100 轮性能基准测试。

---

## 2. 实际新增与修改的组件

| 模块/文件 | 变更类型 | 关键设计与职责 |
|---|---|---|
| `src/config.py` | [MODIFY] | 新增 `classroom_poll_interval_ms`（默认 200ms，范围 50~5000ms）与 `save_exercise_html`（默认 False）；定义高频检测毫秒配置优先于旧秒级 `quiz_refresh_interval` 的优先级。 |
| `src/bot.py` | [MODIFY] | 1. 引入 `_classroom_poll_interval()` 毫秒调度；<br>2. 修复新题提示点击后多条件快速退出（同页出现/路由切换/新标签页/关闭立即退出，彻底消除 3 秒停顿）；<br>3. 实现 AI Future 极速调度（在途期间以 40ms 轮询检测，完成即刻处理）；<br>4. 跨标签页下课深扫节流至 2 秒一次，避免高频打扰 DOM；<br>5. 维护 `_request_generation`（题目代际号），在切题/换题时递增并在答案返回及提交前严格校验，防旧答案误交；<br>6. 优化 `_question_id` 中的 JS 提取器，在 DOM 克隆中剥离倒计时与提交按钮，使倒计时每秒跳变和选项选中态不引发题目标识突变；<br>7. 支持 `submit_delay=0` 并保留 `can` 态确认；多选选项点击失败逆序安全回滚；<br>8. 默认关闭普通运行中每题完整 HTML 落盘，Playwright 读取严格保留在主线程。 |
| `tests/test_fast_path.py` | [NEW] | 第二阶段专用测试集（共 12 项测试）：覆盖毫秒配置优先级与边界、同页/新页提示点击毫秒级退出、真实 Chromium 下本地 HTML 夹具倒计时/选中态不变性、题干变化敏感性、代际防切题机制、多选失败回滚、缺项拒绝、submit_delay=0、HTML 节流与观察模式零动作。 |
| `tests/benchmark_baseline.py` | [MODIFY] | 扩展为第二阶段性能基准评估工具：支持 100 轮评估、单选/多选/按钮延迟对比，自动输出 P50/P90/P95/Max/Mean 与指标达标状态。 |
| `tests/test_bot_pages.py` | [MODIFY] | 在 `test_exercise_html_is_saved_once_per_continuous_visit` 测试中显式声明开启 `save_exercise_html=True`，保持向前兼容。 |

---

## 3. 状态机、配置契约与优先级规则

### 3.1 课堂检测间隔优先级
```text
IF classroom_poll_interval_ms is set AND 50 <= classroom_poll_interval_ms <= 5000:
    interval = classroom_poll_interval_ms / 1000.0 (秒)
ELSE:
    interval = quiz_refresh_interval (秒，默认 1s)
```
- **首页发现循环**：保持独立的 `check_interval`（默认 60s），完全与课堂高频轮询隔离，绝不高频刷新主页。

### 3.2 题目代际号与防切题契约
- **代际号递增触发条件**：
  1. 页面 URL 发生变化（离开或切换 exercise/slide）；
  2. 切换当前操控的 Page 实例；
  3. 识别到的稳定题目标识（`question_id`）发生替换。
- **答案处理与提交双重校验**：
  - AI 答案返回时：`_request_generation == _answer_generation`，否则立即丢弃旧答案；
  - 点击提交前：再次检查代际号与提交按钮可见性，确保收题/切题时不发生误交。

---

## 4. 测试与验证结果

### 4.1 单元与回归测试
- **虚拟环境 (`.venv`)**：
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
  ```
  **结果**：137 项测试全部通过（`Ran 137 tests in 2.029s - OK`）。
- **系统原生 Python (无额外桌面依赖)**：
  ```powershell
  python -m unittest discover -s tests -p "test_*.py"
  ```
  **结果**：137 项测试全部通过（`Ran 137 tests in 5.166s - OK`）。

### 4.2 第二阶段性能验收目标实测对比 (100 轮受控基准)

#### 运行命令
```powershell
python -m tests.benchmark_baseline --rounds 100 --ai-delay 0.05 --quiz-type single
python -m tests.benchmark_baseline --rounds 100 --ai-delay 0.05 --quiz-type multi
python -m tests.benchmark_baseline --rounds 50 --ai-delay 0.05 --btn-delay 50
```

#### 测量报告与指标达标情况

| 分段阶段 (Stage) | P50 (ms) | P90 (ms) | P95 (ms) | Max (ms) | Mean (ms) | 目标值 (Target) | 达标判定 |
|---|---|---|---|---|---|---|---|
| **1. 题目检测->就绪 (单选)** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | P95 <= 300ms | **PASS** |
| **1. 题目检测->就绪 (多选)** | 0.00 | 0.00 | 0.00 | 16.00 | 0.16 | P95 <= 300ms | **PASS** |
| **2. 就绪->发起请求 (单选)** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | P95 <= 500ms | **PASS** |
| **2. 就绪->发起请求 (多选)** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | P95 <= 500ms | **PASS** |
| **3. AI 推理往返 (固定模拟延迟 50ms)** | 78.00 | 78.00 | 78.00 | 79.00 | 69.84 | - (模型延迟) | - |
| **4. 答案生效校验 (单选)** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | - | - |
| **4. 答案生效校验 (按钮延迟 50ms 场景)** | 0.00 | 31.00 | 31.00 | 32.00 | 4.06 | - (DOM 生效) | - |
| **5. 点击提交按钮 (单选)** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | P95 <= 500ms | **PASS** |
| **5. 点击提交按钮 (多选)** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | P95 <= 500ms | **PASS** |
| **6. 确认提交生效** | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | - | - |
| **7. 端到端总耗时 (E2E, 含 50ms AI)** | 78.00 | 78.00 | 78.00 | 79.00 | 69.84 | - | - |

> [!TIP]
> **优化前后对比**：
> - **点击新题提示后的同页延迟**：由固定的 **3000ms** 死等彻底降至 **< 40ms**（减少 98.7% 程序内部阻塞）。
> - **题目检测轮询**：由原版的整 1 秒离散采样降至 **200ms**（响应速度提升 5 倍），且主页发现保持独立的 60 秒慢速，不增加额外网络负荷。
> - **AI 结果返回后的分发**：由原版的阻塞休眠改为在途 40ms 快速探针，返回后在下一事件片即刻派发。
> - **提交延迟**：在 `submit_delay=0` 下，确认 `can` 态即刻点击提交，开销为亚毫秒级（0.00ms）。

---

## 5. 配置迁移与回滚说明

- **兼容性**：保留原有 `quiz_refresh_interval` 配置读取与默认回退，原有 `config.json` 无需强制手工修改即可平滑升级。
- **回滚操作**：若需回退本阶段改动，可切换回 `feature/stage-01-worker-baseline` 分支：
  ```bash
  git checkout feature/stage-01-worker-baseline
  ```

---

## 6. 第三阶段入口

- 下一阶段目标：**AI 视觉与多模型竞速**
- 下一阶段提示词文件：`Antigravity分阶段提示词/03-AI视觉与多模型竞速.md`。
