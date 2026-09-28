// Rainclass Assistant Web Panel 前端交互逻辑

let autoScroll = true;
let sseSource = null;
let currentConfig = {};
let allLogLines = [];

// ==================== 初始化与事件绑定 ====================

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initControls();
  initLogStream();
  initDropzone();
  initForms();
  initQRLogin();

  // 初始加载
  fetchStatus();
  loadConfig();
  loadRecords();

  // 轮询状态机 (每 3 秒刷新一次)
  setInterval(fetchStatus, 3000);
});

// ==================== 标签页切换 ====================

function switchTab(tabId) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  document.querySelectorAll(".tab-pane").forEach((pane) => {
    pane.classList.toggle("active", pane.id === tabId);
  });

  if (tabId === "tab-records") {
    loadRecords();
  } else if (tabId === "tab-config") {
    loadConfig();
  }
}

function initTabs() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
}

// ==================== Toast 消息提示 ====================

function showToast(message, type = "info", timeout = 3500) {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(100%)";
    setTimeout(() => toast.remove(), 200);
  }, timeout);
}

// ==================== 状态机轮询 ====================

async function fetchStatus() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderStatus(data);
  } catch (err) {
    document.getElementById("badge-worker").className = "badge badge-red";
    document.getElementById("badge-worker").textContent = "API 断开";
  }
}

function renderStatus(data) {
  document.getElementById("last-refresh-time").textContent = `更新时间: ${new Date().toLocaleTimeString()}`;

  // 顶部徽标
  const workerBadge = document.getElementById("badge-worker");
  if (data.worker_running) {
    workerBadge.className = "badge badge-green";
    workerBadge.textContent = "Worker 运行中";
  } else {
    workerBadge.className = "badge badge-red";
    workerBadge.textContent = "Worker 已停止";
  }

  const modeBadge = document.getElementById("badge-mode");
  const actualMode = data.actual_mode || data.desired_mode;
  if (actualMode === "auto") {
    modeBadge.className = "badge badge-blue";
    modeBadge.textContent = "模式: 全自动作答 (Auto)";
  } else if (actualMode === "observe") {
    modeBadge.className = "badge badge-yellow";
    modeBadge.textContent = "模式: 旁听巡检 (Observe)";
  } else {
    modeBadge.className = "badge badge-gray";
    modeBadge.textContent = "模式: 已停机";
  }

  // 待应用配置提示
  const dirtyBadge = document.getElementById("badge-dirty");
  if (data.config_dirty) {
    dirtyBadge.style.display = "inline-flex";
    dirtyBadge.title = `修改未生效字段: ${data.config_dirty_fields.join(", ")}`;
  } else {
    dirtyBadge.style.display = "none";
  }

  // 心跳徽标
  const hbBadge = document.getElementById("badge-heartbeat");
  if (data.heartbeat_age_seconds !== null && data.heartbeat_age_seconds !== undefined) {
    hbBadge.textContent = `心跳: ${data.heartbeat_age_seconds}s 前`;
    hbBadge.className = data.heartbeat_age_seconds > 60 ? "badge badge-red" : "badge badge-gray";
  } else {
    hbBadge.textContent = "心跳: 无心跳";
  }

  // 看板卡片数据
  document.getElementById("stat-status-text").textContent = `${data.status} (${data.reason})`;
  document.getElementById("stat-desired-text").textContent = `期望模式: ${data.desired_mode || "stopped"}`;
  document.getElementById("stat-server-name").textContent = data.server_name || "雨课堂";

  const classroomInfo = data.classroom_url || (data.classroom_id ? `课堂 ID: ${data.classroom_id}` : "等待进入课堂");
  document.getElementById("stat-classroom-info").textContent = classroomInfo;

  // 会话状态
  const sess = data.session || {};
  const sessStatusEl = document.getElementById("stat-session-status");
  if (sess.valid) {
    sessStatusEl.textContent = "有效";
    sessStatusEl.style.color = "var(--status-green)";
  } else if (sess.exists) {
    sessStatusEl.textContent = "无效/已过期";
    sessStatusEl.style.color = "var(--status-red)";
  } else {
    sessStatusEl.textContent = "未导入";
    sessStatusEl.style.color = "var(--status-yellow)";
  }
  document.getElementById("stat-session-detail").textContent = `Cookie: ${sess.cookie_count || 0} 个 | ${sess.mtime || "无文件"}`;

  // 心跳时间与运行时间
  document.getElementById("stat-heartbeat-time").textContent = data.last_heartbeat_at || "--";
  const uptimeMinutes = Math.floor((data.uptime_seconds || 0) / 60);
  document.getElementById("stat-uptime-text").textContent = `运行: ${uptimeMinutes} 分钟 (${Math.round(data.uptime_seconds || 0)} 秒)`;

  // 异常提示
  const errBox = document.getElementById("alert-error-box");
  const errMsg = document.getElementById("alert-error-msg");
  if (data.error_message) {
    errBox.style.display = "block";
    errMsg.textContent = data.error_message;
  } else {
    errBox.style.display = "none";
  }

  // 会话卡片同步
  const sessBadge = document.getElementById("session-valid-badge");
  if (sess.valid) {
    sessBadge.className = "badge badge-green";
    sessBadge.textContent = "会话合法有效";
  } else if (sess.exists) {
    sessBadge.className = "badge badge-red";
    sessBadge.textContent = "会话无效或损坏";
  } else {
    sessBadge.className = "badge badge-yellow";
    sessBadge.textContent = "尚未导入会话文件";
  }
  document.getElementById("session-mtime").textContent = `更新时间: ${sess.mtime || "无"}`;
  document.getElementById("session-reason-text").textContent = sess.validation_reason || (sess.valid ? "Cookie 列表完备且属于 yuketang.cn 合法域名" : "无会话");
  document.getElementById("session-domains-text").textContent = (sess.domains && sess.domains.length) ? sess.domains.join(", ") : "无包含域名";
}

// ==================== 快捷控制操作 ====================

function initControls() {
  document.getElementById("btn-start-observe").addEventListener("click", () => sendControl("start", "observe"));
  document.getElementById("btn-start-auto").addEventListener("click", () => {
    if (confirm("⚠️ 确认启动全自动答题模式吗？\n\n在此模式下，系统在课堂检测到题目将自动向真实 AI 提问并点击提交答案！")) {
      sendControl("start", "auto");
    }
  });
  document.getElementById("btn-stop-worker").addEventListener("click", () => {
    if (confirm("确认停止运行当前的 Worker 子进程吗？")) {
      sendControl("stop");
    }
  });
  document.getElementById("btn-restart-worker").addEventListener("click", () => sendControl("restart"));
  document.getElementById("btn-manual-refresh").addEventListener("click", () => {
    fetchStatus();
    showToast("状态已刷新", "info", 1500);
  });
}

async function sendControl(action, mode = "observe") {
  const btns = document.querySelectorAll(".btn-group button");
  btns.forEach((b) => (b.disabled = true));

  try {
    const res = await fetch("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, mode }),
    });
    const result = await res.json();
    if (result.success) {
      showToast(result.message || "操作已成功执行", "success");
    } else {
      showToast(result.message || "操作失败", "error");
    }
    if (result.status) {
      renderStatus(result.status);
    }
  } catch (err) {
    showToast(`控制命令发送异常: ${err}`, "error");
  } finally {
    btns.forEach((b) => (b.disabled = false));
  }
}

// ==================== 参数配置表单 ====================

function initForms() {
  document.getElementById("form-config").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-save-config");
    btn.disabled = true;

    const form = e.target;
    const settings = {
      yuketang_server: form.yuketang_server.value,
      classroom_url: form.classroom_url.value.trim(),
      submit_delay: parseInt(form.submit_delay.value, 10) || 0,
      classroom_poll_interval_ms: parseInt(form.classroom_poll_interval_ms.value, 10) || 200,
      auto_sign_in: form.auto_sign_in.value === "true",
      start_time: form.start_time.value.trim() || "00:00",
      end_time: form.end_time.value.trim() || "23:59",
      ai_strategy: form.ai_strategy.value,
      ai_primary_model: form.ai_primary_model.value,
      custom_ai_base_url: form.custom_ai_base_url.value.trim(),
      custom_ai_model: form.custom_ai_model.value.trim(),
    };

    // 密钥及 extra_body 处理
    const keyVal = form.custom_ai_api_key.value.trim();
    if (keyVal) {
      settings.custom_ai_api_key = keyVal;
    }
    const extraBody = form.custom_ai_extra_body.value.trim();
    if (extraBody) {
      settings.custom_ai_extra_body = extraBody;
    }

    const clearKeys = [];
    if (document.getElementById("chk-clear-custom_ai_api_key").checked) {
      clearKeys.push("custom_ai_api_key");
    }

    try {
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings, clear_keys: clearKeys }),
      });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast(data.message || "配置已成功保存", "success");
        loadConfig();
        fetchStatus();
      } else {
        const msg = data.errors ? data.errors.join("; ") : data.message;
        showToast(`保存失败: ${msg}`, "error");
      }
    } catch (err) {
      showToast(`网络提交异常: ${err}`, "error");
    } finally {
      btn.disabled = false;
    }
  });

  // AI 连通性测试
  document.getElementById("btn-test-ai").addEventListener("click", async () => {
    const btn = document.getElementById("btn-test-ai");
    const resultBox = document.getElementById("ai-test-result");
    btn.disabled = true;
    btn.textContent = "正在测试...";
    resultBox.style.display = "block";
    resultBox.style.backgroundColor = "var(--bg-tertiary)";
    resultBox.style.color = "var(--text-secondary)";
    resultBox.textContent = "正在向大模型网关发送单次标准测试请求，请稍候...";

    try {
      const res = await fetch("/api/ai/test", { method: "POST" });
      const data = await res.json();
      if (data.success && data.is_valid_json) {
        resultBox.style.backgroundColor = "rgba(23, 191, 99, 0.1)";
        resultBox.style.color = "var(--status-green)";
        resultBox.textContent = `✅ 连通性正常！耗时: ${data.duration_ms}ms\n模型返回: ${data.raw_response}\n格式解析: 合法 ${data.parsed?.type} 题，提取答案: [${data.parsed?.answers}]`;
      } else if (data.success) {
        resultBox.style.backgroundColor = "rgba(255, 173, 31, 0.1)";
        resultBox.style.color = "var(--status-yellow)";
        resultBox.textContent = `⚠️ 接口连通但返回非标准 JSON 格式 (耗时: ${data.duration_ms}ms):\n${data.raw_response}`;
      } else {
        resultBox.style.backgroundColor = "rgba(224, 36, 94, 0.1)";
        resultBox.style.color = "var(--status-red)";
        resultBox.textContent = `❌ 测试调用失败 (耗时: ${data.duration_ms}ms):\n${data.error}`;
      }
    } catch (err) {
      resultBox.style.backgroundColor = "rgba(224, 36, 94, 0.1)";
      resultBox.style.color = "var(--status-red)";
      resultBox.textContent = `❌ 请求异常: ${err}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "🧪 测试连通性";
    }
  });
}

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    if (!res.ok) return;
    const data = await res.json();
    currentConfig = data.config || {};
    const flags = data.flags || {};

    const form = document.getElementById("form-config");
    form.yuketang_server.value = currentConfig.yuketang_server || "雨课堂";
    form.classroom_url.value = currentConfig.classroom_url || "";
    form.submit_delay.value = currentConfig.submit_delay !== undefined ? currentConfig.submit_delay : 5;
    form.classroom_poll_interval_ms.value = currentConfig.classroom_poll_interval_ms || 200;
    form.auto_sign_in.value = currentConfig.auto_sign_in ? "true" : "false";
    form.start_time.value = currentConfig.start_time || "00:00";
    form.end_time.value = currentConfig.end_time || "23:59";
    form.ai_strategy.value = currentConfig.ai_strategy || "fast_single";
    form.ai_primary_model.value = currentConfig.ai_primary_model || "自定义";
    form.custom_ai_base_url.value = currentConfig.custom_ai_base_url || "";
    form.custom_ai_model.value = currentConfig.custom_ai_model || "";
    form.custom_ai_extra_body.value = currentConfig.custom_ai_extra_body
      ? typeof currentConfig.custom_ai_extra_body === "object"
        ? JSON.stringify(currentConfig.custom_ai_extra_body, null, 2)
        : currentConfig.custom_ai_extra_body
      : "";

    // 密钥状态徽标
    const keyFlag = document.getElementById("flag-custom_ai_api_key");
    if (flags.custom_ai_api_key_configured) {
      keyFlag.className = "badge badge-green";
      keyFlag.textContent = "已配置 (已安全加密存储)";
    } else {
      keyFlag.className = "badge badge-gray";
      keyFlag.textContent = "未配置";
    }
    form.custom_ai_api_key.value = "";
    document.getElementById("chk-clear-custom_ai_api_key").checked = false;
  } catch (err) {
    showToast("无法加载配置数据", "error");
  }
}

// ==================== 会话拖拽与导入 ====================

function initDropzone() {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-session");

  ["dragenter", "dragover"].forEach((name) => {
    dropzone.addEventListener(name, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach((name) => {
    dropzone.addEventListener(name, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    });
  });

  dropzone.addEventListener("drop", (e) => {
    const files = e.dataTransfer.files;
    if (files.length > 0) {
      uploadSessionFile(files[0]);
    }
  });

  fileInput.addEventListener("change", (e) => {
    if (e.target.files.length > 0) {
      uploadSessionFile(e.target.files[0]);
    }
  });

  document.getElementById("btn-import-paste").addEventListener("click", async () => {
    const text = document.getElementById("paste-session-text").value.trim();
    if (!text) {
      showToast("请先在文本框中粘贴会话 JSON 内容", "warning");
      return;
    }
    try {
      const parsed = JSON.parse(text);
      const res = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed),
      });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast("会话导入校验成功", "success");
        document.getElementById("paste-session-text").value = "";
        fetchStatus();
      } else {
        showToast(`导入失败: ${data.message || data.detail}`, "error");
      }
    } catch (e) {
      showToast(`无效的 JSON 格式: ${e}`, "error");
    }
  });
}

async function uploadSessionFile(file) {
  const formData = new FormData();
  formData.append("file", file);

  showToast(`正在上传并校验 ${file.name}...`, "info", 2000);
  try {
    const res = await fetch("/api/session", {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast("文件校验并导入成功", "success");
      fetchStatus();
    } else {
      showToast(`导入失败: ${data.message || data.detail}`, "error");
    }
  } catch (err) {
    showToast(`文件上传网络失败: ${err}`, "error");
  }
}

// ==================== 答题记录与聚合指标 ====================

async function loadRecords() {
  try {
    const res = await fetch("/api/records?limit=50");
    if (!res.ok) return;
    const data = await res.json();

    // 统计面板更新
    const stats = data.stats || {};
    document.getElementById("sum-confirmed").textContent = stats.confirmed || 0;
    document.getElementById("sum-skipped").textContent = stats.skipped || 0;
    document.getElementById("sum-failed").textContent = stats.failed || 0;
    document.getElementById("sum-p95").textContent = stats.p95_ms !== undefined ? `${stats.p95_ms} ms` : "-- ms";

    // 表格渲染
    const tbody = document.getElementById("records-tbody");
    const records = data.records || [];
    if (records.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--text-muted); padding: 30px;">暂无答题记录（当 Worker 监测到题目并执行作答后将在此呈现）</td></tr>`;
      return;
    }

    tbody.innerHTML = records
      .map((r) => {
        let stageBadge = `<span class="badge badge-gray">${r.stage}</span>`;
        if (r.stage === "confirmed") {
          stageBadge = `<span class="badge badge-green">已确认提交</span>`;
        } else if (r.stage === "skipped") {
          stageBadge = `<span class="badge badge-yellow">主动跳过</span>`;
        } else if (r.stage === "failed") {
          stageBadge = `<span class="badge badge-red">作答失败</span>`;
        } else if (r.stage === "unknown") {
          stageBadge = `<span class="badge badge-purple">待核对</span>`;
        }

        const qid = (r.question_id || "").split(":").pop() || "--";
        const shortQid = qid.length > 12 ? `${qid.substring(0, 8)}...` : qid;

        return `
        <tr>
          <td>#${r.id}</td>
          <td style="white-space: nowrap; font-size: 12px; color: var(--text-secondary);">${r.created_at || "--"}</td>
          <td title="${r.question_id || ''}" style="font-family: var(--font-mono); font-size: 12px;">${shortQid}</td>
          <td>${stageBadge}</td>
          <td style="font-weight: bold; color: var(--accent-blue);">${r.submitted_answer || "--"}</td>
          <td style="font-size: 12px;">${r.ai_model || "--"}</td>
          <td style="font-family: var(--font-mono); font-size: 12px;">${r.ready_to_ai_start_ms !== null ? r.ready_to_ai_start_ms + "ms" : "--"}</td>
          <td style="font-family: var(--font-mono); font-size: 12px;">${r.ai_duration_ms !== null ? r.ai_duration_ms + "ms" : "--"}</td>
          <td style="font-family: var(--font-mono); font-size: 12px;">${r.clicked_to_confirmed_ms !== null ? r.clicked_to_confirmed_ms + "ms" : "--"}</td>
          <td style="font-family: var(--font-mono); font-size: 12px; font-weight: bold;">${r.total_end_to_end_ms !== null ? r.total_end_to_end_ms + "ms" : "--"}</td>
        </tr>
      `;
      })
      .join("");
  } catch (err) {
    showToast("获取答题记录失败", "error");
  }
}

// ==================== SSE 实时控制台日志 ====================

function initLogStream() {
  const terminal = document.getElementById("terminal-box");
  const sseBadge = document.getElementById("badge-sse-status");

  // 初始拉取最近历史日志
  fetch("/api/logs?limit=100")
    .then((r) => r.json())
    .then((d) => {
      allLogLines = d.lines || [];
      renderLogs();
    });

  // 建立 SSE 连接
  connectSSE();

  document.getElementById("btn-toggle-scroll").addEventListener("click", () => {
    autoScroll = !autoScroll;
    const btn = document.getElementById("btn-toggle-scroll");
    btn.textContent = autoScroll ? "⏸️ 暂停滚动" : "▶️ 恢复滚动";
    btn.className = autoScroll ? "btn btn-outline" : "btn btn-warning";
  });

  document.getElementById("btn-clear-logs").addEventListener("click", () => {
    allLogLines = [];
    renderLogs();
  });

  document.getElementById("log-filter-level").addEventListener("change", renderLogs);
  document.getElementById("log-filter-keyword").addEventListener("input", renderLogs);
}

function connectSSE() {
  const sseBadge = document.getElementById("badge-sse-status");
  if (sseSource) {
    sseSource.close();
  }

  sseSource = new EventSource("/api/logs/stream");

  sseSource.onopen = () => {
    sseBadge.className = "badge badge-green";
    sseBadge.textContent = "已连接实时流";
  };

  sseSource.onmessage = (event) => {
    try {
      const entry = JSON.parse(event.data);
      if (entry && entry.message) {
        allLogLines.push(entry);
        if (allLogLines.length > 1000) {
          allLogLines.shift();
        }
        appendLogEntry(entry);
      }
    } catch (e) {}
  };

  sseSource.onerror = () => {
    sseBadge.className = "badge badge-red";
    sseBadge.textContent = "连接断开 (正在重连...)";
    sseSource.close();
    setTimeout(connectSSE, 4000);
  };
}

function renderLogs() {
  const terminal = document.getElementById("terminal-box");
  const levelFilter = document.getElementById("log-filter-level").value;
  const keyword = document.getElementById("log-filter-keyword").value.toLowerCase().trim();

  terminal.innerHTML = "";
  const filtered = allLogLines.filter((l) => {
    if (levelFilter && l.level !== levelFilter) return false;
    if (keyword && !l.message.toLowerCase().includes(keyword)) return false;
    return true;
  });

  filtered.forEach((entry) => appendLogEntry(entry, false));
  if (autoScroll) {
    terminal.scrollTop = terminal.scrollHeight;
  }
}

function appendLogEntry(entry, doScroll = true) {
  const terminal = document.getElementById("terminal-box");
  const levelFilter = document.getElementById("log-filter-level").value;
  const keyword = document.getElementById("log-filter-keyword").value.toLowerCase().trim();

  if (levelFilter && entry.level !== levelFilter) return;
  if (keyword && !entry.message.toLowerCase().includes(keyword)) return;

  const div = document.createElement("div");
  div.className = `terminal-line level-${entry.level || "INFO"}`;

  const timeSpan = document.createElement("span");
  timeSpan.className = "terminal-time";
  timeSpan.textContent = `[${entry.timestamp.split(" ")[1] || entry.timestamp}]`;

  div.appendChild(timeSpan);
  div.appendChild(document.createTextNode(entry.message));
  terminal.appendChild(div);

  if (doScroll && autoScroll) {
    terminal.scrollTop = terminal.scrollHeight;
  }
}

// ==================== 网页扫码登录交互 ====================

let qrPollInterval = null;

function initQRLogin() {
  const btnStart = document.getElementById("btn-start-qr");
  const btnRefresh = document.getElementById("btn-refresh-qr");
  const btnMaskRefresh = document.getElementById("btn-mask-refresh");
  const btnCancel = document.getElementById("btn-cancel-qr");

  if (btnStart) btnStart.addEventListener("click", startQRLogin);
  if (btnRefresh) btnRefresh.addEventListener("click", refreshQRLogin);
  if (btnMaskRefresh) btnMaskRefresh.addEventListener("click", refreshQRLogin);
  if (btnCancel) btnCancel.addEventListener("click", cancelQRLogin);

  // 初始检查是否有活跃登录任务
  checkInitialQRStatus();
}

async function checkInitialQRStatus() {
  try {
    const res = await fetch("/api/login/qr/status");
    if (!res.ok) return;
    const data = await res.json();
    if (data.active) {
      startQRPolling();
      renderQRStatus(data);
    }
  } catch (e) {}
}

async function startQRLogin() {
  const server = document.getElementById("qr-server-select").value;
  const btnStart = document.getElementById("btn-start-qr");
  btnStart.disabled = true;
  btnStart.textContent = "⏳ 正在发起登录...";

  try {
    const res = await fetch("/api/login/qr/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ server }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || data.message || `HTTP ${res.status}`);
    }

    showToast("已启动隔离无头浏览器，正在获取登录二维码...", "info");
    startQRPolling();
  } catch (err) {
    showToast(err.message || "启动扫码登录失败", "error");
  } finally {
    btnStart.disabled = false;
    btnStart.textContent = "🚀 开始扫码登录";
  }
}

function startQRPolling() {
  if (qrPollInterval) clearInterval(qrPollInterval);
  pollQRStatusOnce();
  qrPollInterval = setInterval(pollQRStatusOnce, 1500);
}

function stopQRPolling() {
  if (qrPollInterval) {
    clearInterval(qrPollInterval);
    qrPollInterval = null;
  }
}

async function pollQRStatusOnce() {
  try {
    const res = await fetch("/api/login/qr/status");
    if (!res.ok) return;
    const data = await res.json();
    renderQRStatus(data);
  } catch (err) {
    console.error("轮询扫码状态失败:", err);
  }
}

function renderQRStatus(data) {
  const badge = document.getElementById("qr-global-badge");
  const boxIdle = document.getElementById("qr-box-idle");
  const boxLoading = document.getElementById("qr-box-loading");
  const boxActive = document.getElementById("qr-box-active");
  const boxResult = document.getElementById("qr-box-result");

  const btnStart = document.getElementById("btn-start-qr");
  const btnRefresh = document.getElementById("btn-refresh-qr");
  const btnCancel = document.getElementById("btn-cancel-qr");

  const qrImg = document.getElementById("qr-code-img");
  const maskExpired = document.getElementById("qr-mask-expired");
  const statusMsg = document.getElementById("qr-status-msg");
  const timerMsg = document.getElementById("qr-timer-msg");

  const state = data.state || "idle";

  if (state === "idle") {
    stopQRPolling();
    badge.className = "badge badge-gray";
    badge.textContent = "未开启";
    boxIdle.style.display = "block";
    boxLoading.style.display = "none";
    boxActive.style.display = "none";
    boxResult.style.display = "none";
    btnStart.style.display = "block";
    btnRefresh.style.display = "none";
    btnCancel.style.display = "none";
    return;
  }

  if (state === "starting") {
    badge.className = "badge badge-blue";
    badge.textContent = "启动中...";
    boxIdle.style.display = "none";
    boxLoading.style.display = "block";
    boxActive.style.display = "none";
    boxResult.style.display = "none";
    btnStart.style.display = "none";
    btnRefresh.style.display = "none";
    btnCancel.style.display = "block";
    return;
  }

  if (state === "waiting_scan" || state === "scanned") {
    badge.className = state === "scanned" ? "badge badge-blue" : "badge badge-yellow";
    badge.textContent = state === "scanned" ? "已扫码确认中" : "等待扫码";

    boxIdle.style.display = "none";
    boxLoading.style.display = "none";
    boxActive.style.display = "block";
    boxResult.style.display = "none";

    btnStart.style.display = "none";
    btnRefresh.style.display = "block";
    btnCancel.style.display = "block";

    if (data.qr_image) {
      qrImg.src = data.qr_image;
    }
    maskExpired.style.display = "none";

    statusMsg.textContent = state === "scanned" 
      ? "📲 检测到扫码，正在确认登录并持久化凭据..." 
      : (data.message || "请使用微信或雨课堂 APP 扫码");

    timerMsg.textContent = `剩余有效时间: ${data.expires_in || 0}s`;
    return;
  }

  if (state === "expired") {
    badge.className = "badge badge-red";
    badge.textContent = "二维码已过期";

    boxIdle.style.display = "none";
    boxLoading.style.display = "none";
    boxActive.style.display = "block";
    boxResult.style.display = "none";

    maskExpired.style.display = "flex";
    statusMsg.textContent = "二维码已过期，请刷新重新获取";
    timerMsg.textContent = "有效时间: 0s";

    btnStart.style.display = "none";
    btnRefresh.style.display = "block";
    btnCancel.style.display = "block";
    return;
  }

  if (state === "success") {
    stopQRPolling();
    badge.className = "badge badge-green";
    badge.textContent = "登录成功";

    boxIdle.style.display = "none";
    boxLoading.style.display = "none";
    boxActive.style.display = "none";
    boxResult.style.display = "block";

    document.getElementById("qr-result-icon").textContent = "✅";
    document.getElementById("qr-result-title").textContent = "登录成功！";
    document.getElementById("qr-result-desc").textContent = data.message || "凭证已原子保存，系统正在运行中。";

    btnStart.style.display = "block";
    btnRefresh.style.display = "none";
    btnCancel.style.display = "none";

    showToast("雨课堂账号登录成功！凭据已保存", "success");
    fetchStatus();
    return;
  }

  if (state === "needs_manual") {
    stopQRPolling();
    badge.className = "badge badge-yellow";
    badge.textContent = "需本地验证";

    boxIdle.style.display = "none";
    boxLoading.style.display = "none";
    boxActive.style.display = "none";
    boxResult.style.display = "block";

    document.getElementById("qr-result-icon").textContent = "⚠️";
    document.getElementById("qr-result-title").textContent = "需本地命令行登录";
    document.getElementById("qr-result-desc").textContent = data.error || "雨课堂触发了安全人机验证码，请在本地电脑运行命令行完成首次登录后导入会话。";

    btnStart.style.display = "block";
    btnRefresh.style.display = "none";
    btnCancel.style.display = "none";

    showToast("雨课堂触发安全人机验证，请使用本地 CLI 导入", "warning", 6000);
    return;
  }

  if (state === "failed") {
    stopQRPolling();
    badge.className = "badge badge-red";
    badge.textContent = "登录失败";

    boxIdle.style.display = "none";
    boxLoading.style.display = "none";
    boxActive.style.display = "none";
    boxResult.style.display = "block";

    document.getElementById("qr-result-icon").textContent = "❌";
    document.getElementById("qr-result-title").textContent = "登录未成功";
    document.getElementById("qr-result-desc").textContent = data.error || data.message || "登录流程异常退出。";

    btnStart.style.display = "block";
    btnRefresh.style.display = "none";
    btnCancel.style.display = "none";

    showToast(data.error || "登录失败", "error");
    return;
  }

  if (state === "cancelled") {
    stopQRPolling();
    badge.className = "badge badge-gray";
    badge.textContent = "已取消";

    boxIdle.style.display = "block";
    boxLoading.style.display = "none";
    boxActive.style.display = "none";
    boxResult.style.display = "none";

    btnStart.style.display = "block";
    btnRefresh.style.display = "none";
    btnCancel.style.display = "none";
    return;
  }
}

async function refreshQRLogin() {
  try {
    const res = await fetch("/api/login/qr/refresh", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.message || `HTTP ${res.status}`);
    showToast("正在重新请求最新二维码...", "info");
    pollQRStatusOnce();
  } catch (err) {
    showToast(err.message || "刷新二维码失败", "error");
  }
}

async function cancelQRLogin() {
  try {
    const res = await fetch("/api/login/qr/cancel", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.message || `HTTP ${res.status}`);
    showToast("已取消扫码登录流程", "info");
    pollQRStatusOnce();
  } catch (err) {
    showToast(err.message || "取消登录失败", "error");
  }
}

