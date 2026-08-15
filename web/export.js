"use strict";

// ---------- 全局状态 ----------
const state = {
  gid: null,
  groups: [],
  members: [],          // 当前群成员列表
  lastExportToken: null, // 最近一次导出的下载 token
  activeTab: "member",   // 'member' | 'daily'
};

// ---------- DOM ----------
const $ = (id) => document.getElementById(id);
const elGroup = $("group-select");
const elStatus = $("status");
const elTabMemberBtn = document.querySelector('.tab[data-tab="member"]');
const elTabDailyBtn = document.querySelector('.tab[data-tab="daily"]');
const elTabMember = $("tab-member");
const elTabDaily = $("tab-daily");

// 成员导出 Tab
const elMemberSelectM = $("member-select-member");
const elSinceM = $("since-member");
const elUntilM = $("until-member");
const elFmtRadios = document.querySelectorAll('input[name="fmt"]');
const elCompactCheck = $("compact-check");
const elCompactWrap = $("compact-wrap");
const elExportMemberBtn = $("export-member-btn");

// 日报导出 Tab
const elMemberSelectD = $("member-select-daily");
const elDateDaily = $("date-daily");
const elExportDailyBtn = $("export-daily-btn");

// 结果区
const elResultArea = $("result-area");
const elResultMeta = $("result-meta");
const elDownloadBtn = $("download-btn");
const elPreviewTable = $("preview-table");
const elPreviewCount = $("preview-count");

// ---------- 工具 ----------
function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      if (data && data.error) msg = data.error;
    } catch (_) { /* ignore */ }
    throw new Error(msg);
  }
  return resp.json();
}

function setStatus(msg, isError) {
  elStatus.textContent = msg || "";
  elStatus.style.color = isError ? "#dc3545" : "#888";
}

function clearResult() {
  elResultArea.hidden = true;
  elResultMeta.innerHTML = "";
  elPreviewTable.innerHTML = "";
  elPreviewCount.textContent = "0";
  state.lastExportToken = null;
}

function showError(msg) {
  elResultArea.hidden = false;
  elResultMeta.innerHTML = `<div class="result-error">❌ ${escapeHtml(msg)}</div>`;
  elDownloadBtn.disabled = true;
  elPreviewTable.innerHTML = "";
  elPreviewCount.textContent = "0";
  state.lastExportToken = null;
}

function metaItem(label, value, highlight) {
  const cls = highlight ? "meta-value meta-highlight" : "meta-value";
  return `<span class="meta-item"><span class="meta-label">${escapeHtml(label)}：</span><span class="${cls}">${escapeHtml(value)}</span></span>`;
}

// ---------- Tab 切换 ----------
function switchTab(tab) {
  state.activeTab = tab;
  elTabMemberBtn.classList.toggle("active", tab === "member");
  elTabDailyBtn.classList.toggle("active", tab === "daily");
  elTabMember.classList.toggle("active", tab === "member");
  elTabDaily.classList.toggle("active", tab === "daily");
}
elTabMemberBtn.onclick = () => switchTab("member");
elTabDailyBtn.onclick = () => switchTab("daily");

// 格式联动：CSV 时禁用紧凑模式
function updateCompactState() {
  const fmt = document.querySelector('input[name="fmt"]:checked').value;
  const isCsv = fmt === "csv";
  elCompactCheck.disabled = isCsv;
  elCompactWrap.style.opacity = isCsv ? "0.5" : "1";
  if (isCsv) elCompactCheck.checked = false;
}
elFmtRadios.forEach(r => r.addEventListener("change", updateCompactState));

// ---------- 数据加载 ----------
async function loadGroups() {
  try {
    state.groups = await api("/api/groups");
    elGroup.innerHTML = state.groups.map(g =>
      `<option value="${g.gid}">${escapeHtml(g.name)} (${g.msg_count})</option>`).join("");
  } catch (e) {
    setStatus("加载群列表失败：" + e.message, true);
  }
}

async function loadMembers(gid) {
  try {
    state.members = await api(`/api/members?gid=${gid}`);
    const opts = state.members.map(m =>
      `<option value="${escapeHtml(m.sender_name)}">${escapeHtml(m.sender_name)} (${m.msg_count})</option>`).join("");
    // 成员导出：必选，无"全部"项
    elMemberSelectM.innerHTML = opts || '<option value="">（无成员）</option>';
    // 日报导出：可选，留空 = 仅上下文
    elMemberSelectD.innerHTML = '<option value="">（不指定，仅上下文）</option>' + opts;
  } catch (e) {
    elMemberSelectM.innerHTML = '<option value="">（加载失败）</option>';
    elMemberSelectD.innerHTML = '<option value="">（加载失败）</option>';
  }
}

async function selectGroup(gid) {
  state.gid = gid;
  clearResult();
  setStatus("加载成员…");
  await loadMembers(gid);
  setStatus("");
}

// ---------- 成员发言导出 ----------
async function doExportMember() {
  if (!state.gid) {
    setStatus("请先选择群", true);
    return;
  }
  const senderName = elMemberSelectM.value;
  if (!senderName) {
    setStatus("请选择成员", true);
    return;
  }
  const fmt = document.querySelector('input[name="fmt"]:checked').value;
  const compact = elCompactCheck.checked;
  const since = elSinceM.value;
  const until = elUntilM.value;

  const body = { gid: state.gid, sender_name: senderName, fmt, compact };
  if (since) body.since = since;
  if (until) body.until = until;

  elExportMemberBtn.disabled = true;
  elExportMemberBtn.textContent = "导出中…";
  setStatus("导出中…");
  try {
    const data = await api("/api/export/member", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderMemberResult(data);
    setStatus("");
  } catch (e) {
    showError(e.message);
    setStatus("导出失败", true);
  } finally {
    elExportMemberBtn.disabled = false;
    elExportMemberBtn.textContent = "导出";
  }
}

function renderMemberResult(data) {
  elResultArea.hidden = false;
  state.lastExportToken = data.download_token;
  elDownloadBtn.disabled = false;

  const parts = [
    metaItem("群", data.group_name || "(未知)"),
    metaItem("条数", String(data.count)),
    metaItem("起始", data.start_cst || "—"),
    metaItem("结束", data.end_cst || "—"),
    metaItem("文件", data.path),
  ];
  elResultMeta.innerHTML = parts.join("");

  // 预览表格
  elPreviewCount.textContent = String(data.preview_total);
  const preview = data.preview || [];
  if (!preview.length) {
    elPreviewTable.innerHTML = '<tr><td class="result-empty">无数据</td></tr>';
    return;
  }
  // JSON 导出每条只有 sender_name / text
  elPreviewTable.innerHTML =
    `<thead><tr><th class="col-sender_name">发言者</th><th class="col-text">正文</th></tr></thead>` +
    `<tbody>${preview.map(m =>
      `<tr><td class="col-sender_name">${escapeHtml(m.sender_name || "")}</td>` +
      `<td class="col-text">${escapeHtml(m.text || "")}</td></tr>`).join("")}</tbody>`;
}

// ---------- 日报导出 ----------
async function doExportDaily() {
  if (!state.gid) {
    setStatus("请先选择群", true);
    return;
  }
  const dateStr = elDateDaily.value;
  if (!dateStr) {
    setStatus("请选择日期", true);
    return;
  }
  const senderName = elMemberSelectD.value || null;

  const body = { gid: state.gid, date_str: dateStr };
  if (senderName) body.sender_name = senderName;

  elExportDailyBtn.disabled = true;
  elExportDailyBtn.textContent = "导出中…";
  setStatus("导出中…");
  try {
    const data = await api("/api/export/daily", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderDailyResult(data);
    setStatus("");
  } catch (e) {
    showError(e.message);
    setStatus("导出失败", true);
  } finally {
    elExportDailyBtn.disabled = false;
    elExportDailyBtn.textContent = "导出日报";
  }
}

function renderDailyResult(data) {
  elResultArea.hidden = false;
  state.lastExportToken = data.download_token;
  elDownloadBtn.disabled = false;

  const targetName = data.target_sender_name || "(未指定)";
  const parts = [
    metaItem("群", data.group_name || "(未知)"),
    metaItem("日期", data.date),
    metaItem("目标成员", targetName, data.target_count > 0),
    metaItem("当日总数", String(data.count)),
    metaItem("目标发言", String(data.target_count)),
    metaItem("上下文", String(data.context_count)),
    metaItem("文件", data.path),
  ];
  elResultMeta.innerHTML = parts.join("");

  elPreviewCount.textContent = String(data.preview_total);
  const preview = data.preview || [];
  if (!preview.length) {
    elPreviewTable.innerHTML = '<tr><td class="result-empty">当日无消息</td></tr>';
    return;
  }
  // CSV 预览每行有 created_at / sender_name / text / is_target
  elPreviewTable.innerHTML =
    `<thead><tr>` +
    `<th class="col-created_at">时间</th>` +
    `<th class="col-sender_name">发言者</th>` +
    `<th class="col-text">正文</th>` +
    `<th class="col-is-target">目标</th>` +
    `</tr></thead>` +
    `<tbody>${preview.map(r => {
      const isTarget = String(r.is_target) === "1";
      return `<tr class="${isTarget ? "is-target" : ""}">` +
        `<td class="col-created_at">${escapeHtml(r.created_at || "")}</td>` +
        `<td class="col-sender_name">${escapeHtml(r.sender_name || "")}</td>` +
        `<td class="col-text">${escapeHtml(r.text || "")}</td>` +
        `<td class="col-is-target">${isTarget ? "★" : ""}</td>` +
        `</tr>`;
    }).join("")}</tbody>`;
}

// ---------- 下载 ----------
elDownloadBtn.onclick = () => {
  if (!state.lastExportToken) return;
  // 用 a 标签触发下载（带 filename 头），避免导航影响
  const a = document.createElement("a");
  a.href = `/api/export/download?token=${encodeURIComponent(state.lastExportToken)}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
};

// ---------- 事件绑定 ----------
elGroup.onchange = () => selectGroup(parseInt(elGroup.value, 10));
elExportMemberBtn.onclick = doExportMember;
elExportDailyBtn.onclick = doExportDaily;

// ---------- 初始化 ----------
async function init() {
  updateCompactState();
  await loadGroups();
  if (state.groups.length) {
    await selectGroup(state.groups[0].gid);
  } else {
    setStatus("数据库中没有群");
  }
}

init();
