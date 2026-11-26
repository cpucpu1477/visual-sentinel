const KIND_LABELS = {
  black_screen: "画面过暗 / 黑屏",
  overexposed: "画面过曝",
  frozen: "画面冻结",
  scene_change: "画面剧烈变化",
  color_shift: "红绿颜色变化",
  value_change: "区域数值异常",
  occluded: "镜头遮挡 / 失焦",
  source_lost: "视频源中断",
  source_end: "视频播放结束",
  audio_loud: "环境声响超限",
  audio_spike: "突发异常声响",
};

const state = {
  config: null,
  running: false,
  source: null,
  stats: {},
  audio: {},
  ocr: {},
  alarms: [],
  sparks: { brightness: [], diff: [], red: [], audio: [] },
  soundAlert: true,
  audioCtx: null,
  ws: null,
  filter: "",
  lastFrames: -1,
  flashTimer: null,
  roiSelecting: false,
  roiStart: null,
  roiPreview: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function fmt(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(digits);
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = `请求失败 (${res.status})`;
    try {
      const data = await res.json();
      if (data && data.detail) detail = data.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : res.text();
}

/* ---------------- 初始化 ---------------- */

document.addEventListener("DOMContentLoaded", init);

async function init() {
  bindEvents();
  try {
    const [cfg, status, alarms, devices] = await Promise.all([
      api("/api/config"),
      api("/api/status"),
      api("/api/alarms?limit=100"),
      api("/api/audio/devices"),
    ]);
    state.config = cfg;
    state.alarms = alarms.alarms || [];
    state.ocr = status.ocr || {};
    fillAudioDevices(devices);
    applyConfigToForm(cfg);
    updateStatus(status);
    renderAlarms();
    connectWS();
    setInterval(refreshStatus, 4000);
    setInterval(updateClock, 1000);
    updateClock();
  } catch (err) {
    toast("初始化失败：" + err.message, "critical");
  }
}

function bindEvents() {
  $("#btnToggle").addEventListener("click", toggleMonitor);
  $("#btnQuit").addEventListener("click", quitApp);
  $("#btnScanCameras").addEventListener("click", scanCameras);
  $("#btnPickFile").addEventListener("click", pickFile);
  $("#btnClear").addEventListener("click", clearAlarms);
  $("#btnSaveSettings").addEventListener("click", saveSettings);
  $("#btnResetSettings").addEventListener("click", resetSettings);
  $("#btnSelectRoi").addEventListener("click", beginRoiSelection);
  $("#btnClearRoi").addEventListener("click", clearRoi);
  $("#btnCalibrate").addEventListener("click", calibrateBaselines);
  setupRoiCanvas();
  window.addEventListener("resize", drawRoi);

  $("#sourceType").addEventListener("change", () => {
    const isFile = $("#sourceType").value === "file";
    $("#fileGroup").classList.toggle("hidden", !isFile);
    $("#cameraGroup").classList.toggle("hidden", isFile);
  });

  $("#alarmFilter").addEventListener("change", () => {
    state.filter = $("#alarmFilter").value;
    renderAlarms();
  });

  $("#soundAlert").addEventListener("change", async () => {
    state.soundAlert = $("#soundAlert").checked;
    if (state.soundAlert) beep(false);
    try {
      await api("/api/config", {
        method: "PUT",
        body: JSON.stringify({ ui: { sound_alert: state.soundAlert } }),
      });
    } catch (_) {}
  });

  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      $("#tab-alarms").classList.toggle("hidden", btn.dataset.tab !== "alarms");
      $("#tab-settings").classList.toggle("hidden", btn.dataset.tab !== "settings");
    });
  });

  $$('input[type="range"][data-key]').forEach((el) => {
    el.addEventListener("input", () => syncKey(el.dataset.key, el));
  });
  $$('input[type="number"][data-key]').forEach((el) => {
    el.addEventListener("input", () => syncKey(el.dataset.key, el));
  });

  document.addEventListener(
    "click",
    () => {
      if (!state.audioCtx) {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (Ctx) state.audioCtx = new Ctx();
      }
      if (state.audioCtx && state.audioCtx.state === "suspended") {
        state.audioCtx.resume();
      }
    },
    { once: true }
  );
}

/* ---------------- 实时状态 ---------------- */

function connectWS() {
  if (state.ws && state.ws.readyState <= 1) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;

  ws.onclose = () => {
    setTimeout(connectWS, 2000);
  };
  ws.onerror = () => {
    try {
      ws.close();
    } catch (_) {}
  };
  ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (msg.type === "init") {
      state.alarms = msg.alarms || [];
      updateStatus(msg.status || {});
      renderAlarms();
    } else if (msg.type === "stats") {
      updateStatus({ running: msg.running, source: msg.source, stats: msg.stats });
    } else if (msg.type === "alarm" && msg.alarm) {
      onAlarm(msg.alarm);
    }
  };
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    updateStatus(status);
  } catch (_) {}
}

function updateStatus(status) {
  if (typeof status.running === "boolean") state.running = status.running;
  if (status.source) state.source = status.source;
  if (status.stats) state.stats = { ...state.stats, ...status.stats };
  if (status.audio) state.audio = status.audio;
  if (status.ocr) state.ocr = status.ocr;
  renderStatus();
  renderMetrics();
  drawRoi();
}

function renderStatus() {
  const badge = $("#statusBadge");
  const live = $("#liveTag");
  const frames = Number(state.stats.frames || 0);

  badge.className = "badge " + (state.running ? "live" : "idle");
  badge.innerHTML = `<i></i>${state.running ? "监控中" : "未接入"}`;

  live.className = "live-badge " + (state.running ? "on" : "off");
  live.innerHTML = `<i></i><b>${state.running ? "LIVE" : "OFF"}</b>`;

  $("#previewEmpty").classList.toggle("hidden", state.running || frames > 0);
  $("#sourceLabel").textContent = state.source ? state.source.label : "未选择视频源";
  $("#previewStatus").textContent = state.running
    ? `检测运行中 · ${state.source ? state.source.label : ""}`
    : frames > 0
    ? "监控已停止"
    : "待机";

  const toggle = $("#btnToggle");
  toggle.textContent = state.running ? "停止监控" : "开始监控";
  toggle.classList.toggle("primary", !state.running);

  ["#sourceType", "#cameraIndex", "#btnScanCameras", "#btnPickFile", "#loopPlay"].forEach((sel) => {
    const el = $(sel);
    if (el) el.disabled = state.running;
  });

  const audio = state.audio || {};
  const hint = $("#audioHint");
  if (audio.available) {
    hint.textContent = "麦克风：" + (audio.device || "已连接");
    hint.classList.remove("warn");
    hint.title = audio.device || "";
  } else {
    hint.textContent = audio.error || "音频未启动";
    hint.classList.add("warn");
    hint.title = audio.error || "";
  }

  updateRoiHint();
  updateOcrHint();
}

function updateRoiHint() {
  const el = $("#roiHint");
  if (!el) return;
  const roi = state.config && state.config.ocr ? state.config.ocr.roi : null;
  el.textContent = roi
    ? `已框选 ${(roi.w * 100).toFixed(0)}% × ${(roi.h * 100).toFixed(0)}%`
    : "未框选";
}

function updateOcrHint() {
  const el = $("#ocrHint");
  if (!el) return;
  const cfg = state.config && state.config.ocr;
  const ocr = state.ocr || {};
  const stats = state.stats || {};
  const baseline = stats.ocr_baseline !== undefined && stats.ocr_baseline !== null
    ? stats.ocr_baseline
    : ocr.baseline;

  if (!cfg || !cfg.enabled) {
    el.textContent = "未启用数值识别";
    el.classList.remove("warn");
    el.title = "";
    return;
  }
  if (ocr.error) {
    el.textContent = ocr.error;
    el.classList.add("warn");
    el.title = ocr.error;
    return;
  }
  if (!cfg.roi) {
    el.textContent = "请框选数值区域";
    el.classList.add("warn");
    return;
  }
  const baseText = baseline === null || baseline === undefined ? "待建立基准" : `基准 ${Number(baseline).toFixed(2)}`;
  if (stats.ocr_ok) {
    el.textContent = `${baseText} · 已识别`;
    el.classList.remove("warn");
    el.title = stats.ocr_text || "";
  } else if (stats.ocr_value !== null && stats.ocr_value !== undefined) {
    el.textContent = `${baseText} · 本次识别失败`;
    el.classList.add("warn");
    el.title = stats.ocr_text || "";
  } else {
    el.textContent = `${baseText} · 等待识别`;
    el.classList.remove("warn");
    el.title = "";
  }
}

function renderMetrics() {
  const s = state.stats || {};
  const red = Number(s.red_ratio) || 0;
  const green = Number(s.green_ratio) || 0;
  const redRef = Number(s.red_ref) || 0;
  const greenRef = Number(s.green_ref) || 0;

  $("#mBrightness").textContent = fmt(s.brightness, 1);
  $("#mDiff").textContent = fmt(s.diff, 1);
  $("#mColor").textContent = `${(red * 100).toFixed(1)} / ${(green * 100).toFixed(1)}`;
  $("#mFps").textContent = Math.round(Number(s.fps) || 0);
  $("#mAudio").textContent = state.audio && state.audio.available ? fmt(s.audio_db, 1) : "—";
  $("#mOcr").textContent =
    s.ocr_value !== null && s.ocr_value !== undefined ? Number(s.ocr_value).toFixed(2) : "--";
  $("#frameHint").textContent = `已处理 ${Number(s.frames) || 0} 帧`;
  $("#colorHint").textContent = `基准 ${(redRef * 100).toFixed(1)} / ${(greenRef * 100).toFixed(1)}%`;

  $("#barBrightness").style.width = clamp((Number(s.brightness) || 0) / 255 * 100, 0, 100) + "%";
  $("#barDiff").style.width = clamp((Number(s.diff) || 0) / 60 * 100, 0, 100) + "%";
  $("#barColor").style.width = clamp(red * 100 / 30 * 100, 0, 100) + "%";
  $("#barAudio").style.width =
    state.audio && state.audio.available
      ? clamp(((Number(s.audio_db) || -80) + 80) / 80 * 100, 0, 100) + "%"
      : "0%";

  updateOcrHint();

  const frames = Number(s.frames) || 0;
  if (frames !== state.lastFrames) {
    state.lastFrames = frames;
    pushSpark("brightness", Number(s.brightness) || 0);
    pushSpark("diff", Number(s.diff) || 0);
    pushSpark("red", red * 100);
    pushSpark("audio", state.audio && state.audio.available ? Number(s.audio_db) || -80 : -80);
  }

  drawSpark($("#sparkBrightness"), state.sparks.brightness, "#38bdf8", [0, 255]);
  drawSpark($("#sparkDiff"), state.sparks.diff, "#a78bfa", [0, 60]);
  drawSpark($("#sparkColor"), state.sparks.red, "#f472b6", [0, 30]);
  drawSpark($("#sparkAudio"), state.sparks.audio, "#34d399", [-80, 0]);
}

function pushSpark(key, value) {
  const list = state.sparks[key];
  list.push(value);
  if (list.length > 90) list.shift();
}

function drawSpark(canvas, values, color, range) {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 200;
  const height = canvas.clientHeight || 30;
  if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const data = values.length >= 2 ? values : [range[0], values[0] ?? range[0]];
  const min = range ? range[0] : Math.min(...data);
  const max = range ? range[1] : Math.max(...data);
  const span = max - min || 1;
  const pad = 2;
  const stepX = width / (data.length - 1);
  const toY = (v) => height - pad - ((v - min) / span) * (height - pad * 2);

  ctx.beginPath();
  data.forEach((v, index) => {
    const x = index * stepX;
    if (index === 0) ctx.moveTo(x, toY(v));
    else ctx.lineTo(x, toY(v));
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();

  ctx.lineTo(width, height);
  ctx.lineTo(0, height);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, height);
  grad.addColorStop(0, color + "44");
  grad.addColorStop(1, color + "00");
  ctx.fillStyle = grad;
  ctx.fill();
}

function updateClock() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  $("#clock").textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

/* ---------------- 视频源控制 ---------------- */

async function toggleMonitor() {
  const btn = $("#btnToggle");
  btn.disabled = true;
  try {
    if (state.running) {
      await api("/api/monitor/stop", { method: "POST" });
      toast("监控已停止");
    } else {
      const type = $("#sourceType").value;
      let value;
      if (type === "camera") {
        value = parseInt($("#cameraIndex").value, 10) || 0;
      } else {
        value = $("#filePath").value.trim();
        if (!value) {
          toast("请先选择本地视频文件", "warning");
          return;
        }
      }
      const res = await api("/api/monitor/start", {
        method: "POST",
        body: JSON.stringify({ type, value, loop: $("#loopPlay").checked }),
      });
      updateStatus(res.status || {});
      toast(type === "camera" ? "摄像头已接入，开始检测" : "视频已开始播放，开始检测", "success");
      if (res.status && res.status.audio && res.status.audio.error) {
        toast("提示：" + res.status.audio.error, "warning");
      }
    }
    await refreshStatus();
  } catch (err) {
    toast(err.message, "critical");
  } finally {
    btn.disabled = false;
  }
}

async function scanCameras() {
  const btn = $("#btnScanCameras");
  btn.disabled = true;
  btn.textContent = "扫描中…";
  try {
    const data = await api("/api/cameras");
    const cameras = data.cameras || [];
    const select = $("#cameraIndex");
    const current = select.value;
    if (!cameras.length) {
      toast("未发现可用摄像头，可直接输入序号或改用视频文件", "warning");
      return;
    }
    select.innerHTML = "";
    cameras.forEach((cam) => {
      const option = document.createElement("option");
      option.value = cam.index;
      option.textContent = cam.name;
      select.appendChild(option);
    });
    if (cameras.some((cam) => String(cam.index) === current)) select.value = current;
    toast(`发现 ${cameras.length} 个可用摄像头`, "success");
  } catch (err) {
    toast(err.message, "critical");
  } finally {
    btn.disabled = false;
    btn.textContent = "扫描";
  }
}

async function pickFile() {
  try {
    const res = await api("/api/pick-file", { method: "POST" });
    if (res.path) {
      $("#filePath").value = res.path;
      toast("已选择：" + res.path.split(/[\\/]/).pop());
    } else if (res.error) {
      toast("打开文件选择器失败：" + res.error, "critical");
    }
  } catch (err) {
    toast(err.message, "critical");
  }
}

/* ---------------- 数值区域框选 ---------------- */

function contentRect() {
  const preview = $(".preview");
  const boxW = preview.clientWidth;
  const boxH = preview.clientHeight;
  let fw = Number(state.stats.frame_width) || 0;
  let fh = Number(state.stats.frame_height) || 0;
  if (!fw || !fh) {
    fw = 16;
    fh = 9;
  }
  const scale = Math.min(boxW / fw, boxH / fh);
  const w = fw * scale;
  const h = fh * scale;
  return { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h, boxW, boxH };
}

function normPoint(event) {
  const info = contentRect();
  const box = $(".preview").getBoundingClientRect();
  return {
    x: clamp((event.clientX - box.left - info.x) / info.w, 0, 1),
    y: clamp((event.clientY - box.top - info.y) / info.h, 0, 1),
  };
}

function drawRoi() {
  const canvas = $("#roiCanvas");
  if (!canvas) return;
  const info = contentRect();
  const dpr = window.devicePixelRatio || 1;
  const pixelW = Math.max(1, Math.round(info.boxW * dpr));
  const pixelH = Math.max(1, Math.round(info.boxH * dpr));
  if (canvas.width !== pixelW || canvas.height !== pixelH) {
    canvas.width = pixelW;
    canvas.height = pixelH;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, info.boxW, info.boxH);

  let roi = state.roiPreview;
  if (!roi && !state.roiSelecting) {
    roi = state.config && state.config.ocr ? state.config.ocr.roi : null;
  }
  if (!roi) return;

  const x = info.x + roi.x * info.w;
  const y = info.y + roi.y * info.h;
  const w = roi.w * info.w;
  const h = roi.h * info.h;

  ctx.fillStyle = "rgba(34, 211, 238, 0.12)";
  ctx.fillRect(x, y, w, h);
  ctx.strokeStyle = "#22d3ee";
  ctx.lineWidth = 2;
  ctx.setLineDash([6, 4]);
  ctx.strokeRect(x, y, w, h);
  ctx.setLineDash([]);
  ctx.fillStyle = "#22d3ee";
  ctx.font = "12px 'Microsoft YaHei', sans-serif";
  ctx.fillText("数值区域", x + 4, Math.max(12, y - 6));
}

function setupRoiCanvas() {
  const canvas = $("#roiCanvas");
  canvas.addEventListener("mousedown", (event) => {
    if (!state.roiSelecting) return;
    event.preventDefault();
    state.roiStart = normPoint(event);
    state.roiPreview = null;
  });
  window.addEventListener("mousemove", (event) => {
    if (!state.roiSelecting || !state.roiStart) return;
    const current = normPoint(event);
    const x = Math.min(state.roiStart.x, current.x);
    const y = Math.min(state.roiStart.y, current.y);
    const w = Math.abs(current.x - state.roiStart.x);
    const h = Math.abs(current.y - state.roiStart.y);
    state.roiPreview = { x, y, w, h };
    drawRoi();
  });
  window.addEventListener("mouseup", () => {
    if (!state.roiSelecting || !state.roiStart) return;
    state.roiStart = null;
    finishRoiSelection();
  });
}

function beginRoiSelection() {
  state.roiSelecting = true;
  state.roiStart = null;
  state.roiPreview = null;
  $("#roiCanvas").classList.add("selecting");
  drawRoi();
  toast("请在实时画面上拖动，框选要识别的数字区域", "info");
}

function finishRoiSelection() {
  $("#roiCanvas").classList.remove("selecting");
  state.roiSelecting = false;
  const roi = state.roiPreview;
  state.roiPreview = null;
  if (roi && roi.w > 0.01 && roi.h > 0.01) {
    saveRoi(roi);
  } else {
    toast("框选区域太小，请重新框选", "warning");
    drawRoi();
  }
}

async function saveRoi(roi) {
  try {
    state.config = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ ocr: { roi, enabled: true } }),
    });
    applyConfigToForm(state.config);
    toast("数值区域已保存，将自动识别该区域数字", "success");
  } catch (err) {
    toast(err.message, "critical");
  }
}

async function clearRoi() {
  try {
    state.config = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ ocr: { roi: null } }),
    });
    applyConfigToForm(state.config);
    toast("已清除数值区域");
  } catch (err) {
    toast(err.message, "critical");
  }
}

async function calibrateBaselines() {
  try {
    const res = await api("/api/monitor/calibrate", { method: "POST" });
    if (res.config) state.config = res.config;
    applyConfigToForm(state.config);
    updateStatus(res.status || {});
    toast("已重新校准红绿基准与数值基准", "success");
  } catch (err) {
    toast(err.message, "critical");
  }
}

/* ---------------- 报警 ---------------- */

function onAlarm(alarm) {
  state.alarms.unshift(alarm);
  if (state.alarms.length > 200) state.alarms.length = 200;
  renderAlarms();
  flashPreview(alarm.level);
  beep(alarm.level === "critical");
  if (alarm.level === "critical") {
    toast(alarm.message, "critical");
  }
}

function renderAlarms() {
  const list = $("#alarmList");
  const items = state.alarms.filter((item) => !state.filter || item.level === state.filter);
  $("#alarmCount").textContent = state.alarms.length;
  $("#alarmEmpty").classList.toggle("hidden", items.length > 0);
  list.innerHTML = "";

  for (const alarm of items) {
    const li = document.createElement("li");
    li.className = "alarm-item " + alarm.level;

    if (alarm.snapshot) {
      const img = document.createElement("img");
      img.className = "thumb";
      img.loading = "lazy";
      img.src = "/snapshots/" + encodeURIComponent(alarm.snapshot);
      img.title = "查看报警截图";
      img.addEventListener("click", () => window.open(img.src, "_blank"));
      li.appendChild(img);
    } else {
      const placeholder = document.createElement("div");
      placeholder.className = "thumb placeholder";
      placeholder.textContent = "—";
      li.appendChild(placeholder);
    }

    const info = document.createElement("div");
    info.className = "alarm-info";

    const title = document.createElement("div");
    title.className = "alarm-title";
    const name = document.createElement("span");
    name.textContent = KIND_LABELS[alarm.kind] || alarm.kind;
    const tag = document.createElement("span");
    tag.className = "tag " + alarm.level;
    tag.textContent = alarm.level === "critical" ? "严重" : "警告";
    title.append(name, tag);

    const message = document.createElement("div");
    message.className = "alarm-msg";
    message.textContent = alarm.message;

    const meta = document.createElement("div");
    meta.className = "alarm-meta";
    meta.textContent = `${alarm.ts} · ${alarm.source || "未知来源"}`;

    info.append(title, message, meta);
    li.appendChild(info);
    list.appendChild(li);
  }
}

function flashPreview(level) {
  const el = $("#alarmFlash");
  el.className = "alarm-flash show " + (level === "critical" ? "" : "warning");
  clearTimeout(state.flashTimer);
  state.flashTimer = setTimeout(() => {
    el.className = "alarm-flash";
  }, 1800);
}

function beep(critical) {
  if (!state.soundAlert) return;
  const ctx = state.audioCtx;
  if (!ctx) return;
  if (ctx.state === "suspended") ctx.resume();
  const now = ctx.currentTime;
  const tones = critical ? [880, 660, 880] : [620];
  tones.forEach((freq, index) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const start = now + index * 0.17;
    osc.type = "triangle";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(critical ? 0.16 : 0.09, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.15);
    osc.connect(gain).connect(ctx.destination);
    osc.start(start);
    osc.stop(start + 0.17);
  });
}

async function clearAlarms() {
  if (!confirm("确定清空全部报警记录？")) return;
  try {
    await api("/api/alarms", { method: "DELETE" });
    state.alarms = [];
    renderAlarms();
    toast("报警记录已清空", "success");
  } catch (err) {
    toast(err.message, "critical");
  }
}

/* ---------------- 参数设置 ---------------- */

function fillAudioDevices(data) {
  const select = $("#audioDevice");
  select.innerHTML = "";
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "自动（系统默认麦克风）";
  select.appendChild(auto);
  (data.devices || []).forEach((dev) => {
    const option = document.createElement("option");
    option.value = dev.index;
    option.textContent = `${dev.name}（${dev.channels} 通道）`;
    select.appendChild(option);
  });
  if (!(data.devices || []).length && data.error) {
    auto.textContent = "未检测到音频输入设备";
  }
}

function applyConfigToForm(cfg) {
  state.config = cfg;
  $$("input[data-check]").forEach((el) => {
    const [section, key] = el.dataset.check.split(".");
    el.checked = Boolean(cfg[section] && cfg[section][key]);
  });
  $$("input[data-key]").forEach((el) => {
    const [section, key] = el.dataset.key.split(".");
    const value = cfg[section] ? cfg[section][key] : undefined;
    if (value === undefined) return;
    el.value = value === null ? "" : value;
  });
  const device = $("#audioDevice");
  if (cfg.audio) {
    device.value = cfg.audio.device === null || cfg.audio.device === undefined ? "" : String(cfg.audio.device);
    if (device.selectedIndex < 0) device.selectedIndex = 0;
  }
  state.soundAlert = Boolean(cfg.ui && cfg.ui.sound_alert);
  $("#soundAlert").checked = state.soundAlert;
  updateRoiHint();
  updateOcrHint();
  drawRoi();
}

function syncKey(key, source) {
  const elements = $$(`[data-key="${key}"]`);
  const value = source.value;
  if (value === "") return;
  elements.forEach((el) => {
    if (el !== source) el.value = value;
  });
}

async function saveSettings() {
  const patch = { video: {}, audio: {}, ui: {} };
  $$("input[data-check]").forEach((el) => {
    const [section, key] = el.dataset.check.split(".");
    patch[section][key] = el.checked;
  });
  const seen = new Set();
  $$('input[type="range"][data-key]').forEach((el) => {
    if (seen.has(el.dataset.key)) return;
    seen.add(el.dataset.key);
    const [section, key] = el.dataset.key.split(".");
    const value = parseFloat(el.value);
    if (!Number.isNaN(value)) patch[section][key] = value;
  });
  const device = $("#audioDevice").value;
  patch.audio.device = device === "" ? null : parseInt(device, 10);

  const baselineEl = $('[data-key="ocr.baseline"]');
  if (baselineEl) {
    const raw = baselineEl.value.trim();
    if (raw === "") {
      patch.ocr.baseline = null;
    } else {
      const parsed = parseFloat(raw);
      if (!Number.isNaN(parsed)) patch.ocr.baseline = parsed;
    }
  }

  try {
    state.config = await api("/api/config", { method: "PUT", body: JSON.stringify(patch) });
    applyConfigToForm(state.config);
    toast("参数已保存并立即生效", "success");
  } catch (err) {
    toast(err.message, "critical");
  }
}

async function resetSettings() {
  if (!confirm("恢复全部默认参数？")) return;
  try {
    state.config = await api("/api/config/reset", { method: "POST" });
    applyConfigToForm(state.config);
    toast("已恢复默认参数", "success");
  } catch (err) {
    toast(err.message, "critical");
  }
}

/* ---------------- 退出 / 提示 ---------------- */

async function quitApp() {
  if (!confirm("确定退出监控软件？")) return;
  try {
    await api("/api/shutdown", { method: "POST" });
  } catch (_) {}
  document.body.classList.add("closed");
  setTimeout(() => {
    try {
      window.close();
    } catch (_) {}
  }, 400);
}

let toastTimer = null;

function toast(message, level = "info") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show ${level}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.className = "toast";
  }, 3400);
}
