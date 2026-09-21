# Rainclass Assistant 改造进度追踪 (Antigravity Progress)

## 1. 阶段概述

- **当前完成阶段**：第三阶段：模型响应与提前决策（`03-AI快速响应策略.md`）
- **当前代码分支**：`feature/stage-03-ai-response-strategy`
- **基线提交**：`a90c7152011b7dfb7a95610ecffb66ba3a789128` (stage 2 complete)
- **交付状态**：已实现三种 AI 响应决策策略、题图安全复用与重定向凭据隔离、单调时钟总预算控制、有界并发调度、结构化决策指标统计、156 项全量测试验证与独立 `provider-benchmark` 评测工具。

---

## 2. 实际新增与修改的组件

| 模块/文件 | 变更类型 | 关键设计与职责 |
|---|---|---|
| `src/config.py` | [MODIFY] | 新增 `ai_strategy`（默认 `fast_single`，可选 `race_first_valid`/`consensus`）、`ai_primary_model`、`ai_backup_model`、`ai_backup_delay_ms`、`ai_consensus_quorum`、`ai_consensus_mode`、`ai_consensus_tie_breaker`、`ai_total_budget_seconds`（默认 20.0s）、`submit_time_margin_seconds`（默认 3.0s）、`ai_max_concurrent_requests`（默认 4）及其完整边界与枚举校验。 |
| `src/ai/image.py` | [NEW] | 1. 严格 RFC 6265 Cookie 域名/Path/Secure 匹配，仅向雨课堂白名单站点提供会话凭据；<br>2. 重定向跟踪保护：发生跨站或离开白名单重定向时彻底剥离 Cookie 与凭据，杜绝凭据外泄；<br>3. 敏感 URL Query 与异常日志脱敏（遮蔽 token/key/sessionid 等）。 |
| `src/ai/models.py` | [NEW] | 1. 定义 `EndpointConfig`、`ModelCallResult`、`RoundDecision` 数据模型；<br>2. 规范化答案解析器与有效性校验：支持单选/多选/判断题标准判定，过滤 unknown/fill/sub 及错误报错文本；<br>3. 格式有效率（`valid_rate`）清晰界定与格式化导出。 |
| `src/ai/strategy.py` | [NEW] | 核心策略执行器 `StrategyRunner`：<br>1. **fast_single**：主模型先行，成功立刻返回；失败且预算充足时单次尝试备用模型；<br>2. **race_first_valid**：主备双模型竞速，首胜锁定且迟到不可改写；支持备用模型延迟启动，主模型在延迟内成功则自动跳过备用模型以节省 API 费用；<br>3. **consensus**：多模型共识，支持 Quorum 与严格多数（$\lfloor N/2 \rfloor + 1$）提前决策返回；超时/平票支持确定性配置优先级仲裁（绝不用 `random.choice`）或冲突跳过（`skip`）；<br>4. 基于有界线程池控制全局在途请求上限，支持切题/过期任务前置中断。 |
| `src/ai/service.py` | [MODIFY] | 1. 深度集成 `StrategyRunner`，接管 `submit_answer` 策略编排与端点解析；<br>2. `_download_and_save` 接入 `image.py` 安全下载能力；<br>3. 连接池复用：缓存 OpenAI 兼容客户端并在 `shutdown` 时显式安全释放；<br>4. 完全向下兼容原有 `多AI作答` 逻辑与所有历史单元测试。 |
| `src/ai/benchmark.py` | [NEW] | 独立 CLI 基准评测工具（`python -m src.ai.benchmark`）：支持受限样本数（1~10）对用户配置的实际端点进行评测，输出 P50/P95 耗时、格式有效率及 Token 消耗统计，采用纯合成测试图绝不泄露敏感课堂数据。 |
| `src/timing.py` | [MODIFY] | `QuizTimingTracker` 扩展支持 `set_ai_metrics`，记录策略名称、胜出模型、有效结果率、备用触发标志与迟到模型数，同步写入 `metrics.jsonl` 与控制台高可读耗时摘要。 |
| `src/bot.py` | [MODIFY] | 1. 统一基于页面倒计时与 `submit_time_margin_seconds` 计算单调绝对截止时间（`deadline`）；<br>2. 实现 `_prepare_question_image_b64`：每轮仅准备一份完整题图（URL 安全下载，失败时在页面线程直接截图兜底），同轮模型完全复用；<br>3. 答案完成时自动将 `last_decision` 关联至当前计时跟踪器。 |
| `src/browser.py` | [MODIFY] | 新增 `get_all_cookies` 接口，暴露完整的 cookie 属性（domain, path, secure）供安全下载器匹配。 |
| `tests/test_ai_strategies.py` | [NEW] | 第三阶段专项测试集（19 项测试）：全面验证 fast_single 快胜/备用回退/预算不足跳过、race 竞速首胜/延迟启动跳过省费、consensus quorum 提前返回/严格多数/确定性平票/平票跳过、跨域重定向凭据剥离、URL 与日志脱敏、有效答案归一化、代际失效任务拦截。 |
| `tests/test_config.py` | [MODIFY] | 新增 `test_ai_strategy_defaults_and_validation`，覆盖阶段三全部新配置字段的默认值、枚举约束与越界校验。 |

---

## 3. 三种策略核心机制与参数配置速查

```json
{
    "ai_strategy": "fast_single",          // 可选: "fast_single" (默认), "race_first_valid", "consensus"
    "ai_primary_model": "豆包AI",           // 主模型（豆包AI / Gemini AI / 自定义 / INI 节点名）
    "ai_backup_model": "",                 // 备用模型（为空时不启用备用模型）
    "ai_backup_delay_ms": 0,               // race_first_valid 备用模型启动延迟（毫秒），0 表示同时并发
    "ai_consensus_quorum": 2,              // consensus 达成法定票数立即提前决策的阈值 (M 张相同有效票)
    "ai_consensus_mode": "quorum",         // consensus 决策模式: "quorum" (达到 quorum 票) / "strict_majority" (超过一半)
    "ai_consensus_tie_breaker": "priority",// 平票裁决策略: "priority" (按配置顺序确定性仲裁) / "skip" (冲突跳过)
    "ai_total_budget_seconds": 20.0,       // 题目无有效倒计时时的整题单调预算上限（秒）
    "submit_time_margin_seconds": 3.0,     // 预留给选项点击与提交确认的安全时间余量（秒）
    "ai_max_concurrent_requests": 4        // 全局在途 AI 请求的最大并发上限
}
```

### 3.1 策略行为规则

1. **fast_single（单模型优先，默认推荐）**：
   - 首先启动 `ai_primary_model`。
   - 收到合法可提交答案（单选/多选/判断）立即胜出返回。
   - 主模型返回错误/格式非法时，计算剩余单调预算：若 `剩余时间 > submit_time_margin_seconds + 1.0s`，且配置了 `ai_backup_model`，则按配置超时触发一次备用模型；若预算不足则跳过备用，绝不超时死等或随机乱猜。
2. **race_first_valid（双模型竞速，速度最快）**：
   - 主模型在 $T=0$ 启动。
   - 若 `ai_backup_delay_ms == 0`，备用模型同时启动；若 `delay > 0`，备用模型延后启动。
   - **省费保护机制**：在延迟等待期间，若主模型已返回有效答案，或者剩余预算已不足安全余量，系统立即取消备用模型启动，避免浪费 API 调用额度。
   - 任何一方率先返回有效答案即锁定胜出，迟到结果记录至 `late_results_count` 且绝不改写已决策答案。
3. **consensus（多模型共识，准确度最高）**：
   - 同时向候选端点发起并发请求。
   - **提前决策机制**：
     - 若 `mode == "quorum"`：任意有效答案累计票数达到 `ai_consensus_quorum` 时立即提前返回，无需等待剩余慢模型。
     - 若 `mode == "strict_majority"`：任意答案达到 $\lfloor N/2 \rfloor + 1$ 票时立即提前决策。
   - 截止或模型全部返回后仍平票时：
     - 若 `tie_breaker == "priority"`：按配置文件的端点先后顺序确定性裁决（由排在最前的端点投出的答案胜出，完全可复现，绝不用 `random.choice`）。
     - 若 `tie_breaker == "skip"`：放弃猜测，记录冲突跳过。

---

## 4. 测试与验证结果

### 4.1 全量回归测试结果
- **虚拟环境 (`.venv`)**：
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
  ```
  **结果**：156 项测试全部通过（`Ran 156 tests in 4.154s - OK`）。
- **系统原生 Python**：
  ```powershell
  python -m unittest discover -s tests -p "test_*.py"
  ```
  **结果**：156 项测试全部通过（`Ran 156 tests in 4.613s - OK`）。

### 4.2 基准评测命令验证 (CLI `provider-benchmark`)
```powershell
.\.venv\Scripts\python.exe -m src.ai.benchmark --help
```
输出完整帮助信息，支持 `--config`, `--models-ini`, `--samples`, `--timeout` 等参数，采用内置 1x1 合成测试卡进行非侵入式测量。

---

## 5. 实际使用建议与最佳实践

1. **默认日常使用**：
   保持 `ai_strategy: "fast_single"`，配置主力视觉模型（如豆包AI），无需配置备用模型。开销最小、单次请求耗时最快。
2. **极速抢答场景（如限时极短的随堂测试）**：
   可启用 `ai_strategy: "race_first_valid"`，设置 `ai_backup_delay_ms: 300`（或 0）。注意：竞速策略可能同时产生两份 API 账单，请在有成本预算的前提下使用。
3. **关键高分考试场景**：
   可启用 `ai_strategy: "consensus"`，在 `model_visible.ini` 中配置 3~5 个不同 Provider 的视觉模型，设置 `ai_consensus_quorum: 2` 与 `ai_consensus_tie_breaker: "priority"`。多模型互验减少幻觉，且达到 2 票即提前返回，不被最慢模型拖住。

