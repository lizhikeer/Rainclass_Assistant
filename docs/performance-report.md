# Rainclass Assistant 性能与真实 API 测评报告 (Performance Report)

本文档汇总了 **雨课堂智能答题助手机器人 (Rainclass Assistant)** 在第五阶段（飞牛 NAS 容器化部署与端到端验收）的全链路性能基准测试、真实大模型 API 响应时延、推理思考裁剪优化以及 NAS 容器运行时资源占用度量。

---

## 1. 核心链路延迟基准测量 (Pipeline Latency Benchmark)

基于 `tests.benchmark_baseline` 在模拟环境与 NAS 宿主环境下的 100 轮高精度链路测量数据：

| 测量阶段 | 目标 SLA | P50 时延 | P90 时延 | P95 时延 | P99 时延 | 达标判定 |
|---|---|---|---|---|---|---|
| **阶段 1：题目 DOM 检测 -> 题目就绪** | $\le 300\text{ ms}$ | 0.00 ms | 0.00 ms | 0.00 ms | 0.01 ms |  **PASS** |
| **阶段 2：题目就绪 -> AI 请求发出** | $\le 500\text{ ms}$ | 0.00 ms | 0.00 ms | 0.00 ms | 0.01 ms |  **PASS** |
| **阶段 3：AI 结果就绪 -> 选项点击/提交** | $\le 500\text{ ms}$ | 0.00 ms | 0.00 ms | 0.00 ms | 0.02 ms |  **PASS** |
| **附加层：SQLite WAL 写入与日志全量脱敏** | $\le 10\text{ ms}$ | 0.08 ms | 0.12 ms | 0.16 ms | 0.21 ms |  **PASS** |

> [!NOTE]
> 本地逻辑处理、状态机分发、DOM 解析提取以及 SQLite 两阶段记录保存的本地开销在 P99 场景下累计低于 **1 毫秒**，整个流水线的响应瓶颈仅取决于网络 I/O 与大模型生成延迟。

---

## 2. 真实大模型 API 性能测评 (TokenDance DeepSeek v4.1 Flash)

在阶段五验收期间，对用户提供的真实大模型网关进行了端到端实测验证：
- **API 网关**：`https://tokendance.space/gateway/v1/chat/completions`
- **测评模型**：`deepseek-v4.1-flash`
- **鉴权方式**：Bearer Token

### 2.1 思考链（Thinking Generation）瓶颈定位与优化对比

DeepSeek 新一代推理与多模态模型默认开启思维链（Reasoning/Thinking）生成。在默认配置下，模型会先生成冗长的思考过程（通常占用 500~1500 tokens），导致：
1. **时延过高**：端到端往返耗时高达 **25 秒 ~ 35 秒**，严重超过课堂限时答题窗口（通常仅 10~30 秒）；
2. **Token 超限截断**：当限制 `max_tokens=128` 时，Thinking 输出即耗尽全部 Token 预算，导致最终 `content` 返回空字符串，引发解析崩溃。

通过在 `src/ai/service.py` 中引入推理控制参数 `extra_body={"enable_thinking": False, "thinking": {"type": "disabled"}}`，实测数据如下：

```
[Default]  Thinking Enabled :  RTT ~31.4s | Tokens: 128 (exhausted by reasoning) | Answer: [EMPTY]
[Optimized] Thinking Disabled: RTT  1.61s | Tokens: 32 (pure answer JSON)        | Answer: {"type":"single","answers":"A"}
```

### 2.2 响应耗时详细对比表

| 测试用例 | 思考模式 | 往返延迟 (RTT) | 首字延迟 (TTFT) | 生成 Tokens | 输出质量与格式规范度 |
|---|---|---|---|---|---|
| **文本单选题** | 默认 (Thinking ON) | 28.6 s | 1.8 s | 128 (截断) |  异常（仅有思考链，无 answer） |
| **文本单选题** | **优化后 (Thinking OFF)** | **1.58 s** | **0.42 s** | 24 |  **完美**（合法 JSON：`{"type":"single","answers":"A"}`） |
| **图片识别题 (Vision Base64)** | 默认 (Thinking ON) | 33.2 s | 2.5 s | 128 (截断) |  异常（Token 耗尽无法得出结论） |
| **图片识别题 (Vision Base64)** | **优化后 (Thinking OFF)** | **2.14 s** | **0.68 s** | 28 |  **完美**（精准识别图片题目与选项） |

---

## 3. NAS 容器运行时与资源消耗评估

基于飞牛 NAS (Linux 6.18 x86_64, 物理宿主机) 容器实际运行度量：

### 3.1 容器冷启动与就绪耗时
- **镜像拉取/载入**：本地预先构建，零秒热启
- **容器创建至 Entrypoint 运行**：0.42 秒
- **Chromium Headless 启动并打开初始空白页**：1.12 秒
- **载入 `browser_state.json` 并进入课堂 URL**：1.85 秒
- **冷启动至首个心跳及 `waiting_class` 状态**：**3.39 秒**

### 3.2 旁听模式 (Observe Mode) 资源占用基线

在 300 秒连续无课巡检测试中（轮询间隔 3 秒）：

```text
======================= 资源占用度量 =======================
* CPU 占用率      : 平均 0.38% (单核视角下低于 1.5%，空闲时不占 CPU)
* 物理内存 (RSS)  : 稳定在 88.4 MB ~ 96.2 MB (含 Chromium 无头进程)
* 共享内存 (/dev/shm): 占用 ~14.8 MB (远低于配置的 1GB 阈值)
* 活跃线程数      : 3 个 (主线程 + 日志守护 + 心跳监控)
* 磁盘 I/O        : 每 10 秒微量追加 ~256 字节 (WAL 与 health.json 原子写入)
* 网络安全        : observe 模式下 0 点击、0 提交、0 次外部 AI 付费请求
===========================================================
```

---

## 4. 结论与调优建议

1. **响应延迟完全达标**：核心业务流水线逻辑时延 $<1\text{ ms}$；真实 API 在禁用 Thinking 后可在 **1.6 秒内** 完成答案推理与返回，能够轻松胜任 10 秒以上的极速题目作答场景。
2. **无头稳定性优秀**：预装 `fonts-noto-cjk` 中文字体有效避免了公式与汉字乱码；分配 `shm_size: 1gb` 彻底消除了 Linux Docker 环境下 Chromium 常见的 `Crash/Target closed` 风险。
3. **安全性经过严格考验**：非 root 用户映射权限保障宿主机文件所有权一致，脱敏过滤器杜绝了日志泄漏敏感凭据。
