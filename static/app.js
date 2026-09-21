/* 冷链告警抑制策略沙盒 —— 原生前端逻辑（无框架、无构建） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (_) { /* 非 JSON */ }
  if (!resp.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail
      : (detail ? JSON.stringify(detail) : `请求失败 ${resp.status}`));
  }
  return data;
}

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2600);
}

function setMsg(el, text, kind = "") {
  el.textContent = text;
  el.className = "msg" + (kind ? " " + kind : "");
  if (text) setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 5000);
}

const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};
const levelClass = (lv) => `level-${lv}`;
const verdictBadge = (v) => v === "pass"
  ? '<span class="badge pass">PASS 放行</span>'
  : '<span class="badge suppress">SUPPRESS 抑制</span>';

// ---------------------------------------------------------------- Tabs

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`#tab-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "rules") refreshRules();
    if (tab.dataset.tab === "receiver") refreshInbox();
    if (tab.dataset.tab === "compare") refreshVersionsForCompare();
  });
});

// ---------------------------------------------------------------- 概览 / 告警

async function refreshOverview() {
  const o = await api("/api/overview");
  $("#statTotal").textContent = o.alerts_total;
  $("#statPass").textContent = o.pass;
  $("#statSuppress").textContent = o.suppress;
  $("#statRules").textContent = o.rules_total;
  $("#autoForward").checked = o.auto_forward;
  const badge = $("#inboxBadge");
  badge.textContent = o.inbox_total;
  badge.classList.toggle("hidden", o.inbox_total === 0);
}

async function refreshAlerts() {
  const data = await api("/api/alerts");
  const tbody = $("#alertsTable tbody");
  tbody.innerHTML = "";
  const dmap = new Map(data.decisions.map((d) => [d.alert_id, d]));
  $("#alertsEmpty").style.display = data.alerts.length ? "none" : "block";

  for (const a of data.alerts) {
    const d = dmap.get(a.id);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">#${a.sequence}</td>
      <td class="mono">${esc(a.device_id)}</td>
      <td class="mono">${esc(a.batch_id) || "—"}</td>
      <td><span class="${levelClass(a.level)}">${esc(a.level)}</span></td>
      <td class="mono">${esc(a.alert_type) || "—"}</td>
      <td class="mono">${esc(fmtTime(a.alert_time))}</td>
      <td>${d ? verdictBadge(d.verdict) : '<span class="badge neutral">未评估</span>'}</td>
      <td>${d
        ? `<div class="verdict-cell"><span title="${esc(d.reason)}">${esc(shorten(d.reason, 46))}</span>
             <button class="link-btn" data-explain="${a.id}">查看命中解释 ▸</button></div>`
        : ""}</td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("[data-explain]").forEach((btn) => {
    btn.addEventListener("click", () => showExplain(dmap.get(Number(btn.dataset.explain)), aMeta(data.alerts, Number(btn.dataset.explain))));
  });
  await refreshOverview();
}

function aMeta(alerts, id) {
  return alerts.find((a) => a.id === id);
}

function shorten(s, n) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function showExplain(decision, alert) {
  $("#explainTitle").textContent = `#${decision.sequence} ${alert ? esc(alert.device_id) : ""} —— 命中解释`;
  const body = {
    verdict: decision.verdict,
    rule_id: decision.rule_id,
    rule_name: decision.rule_name || null,
    reason: decision.reason,
    cooldown_seconds: decision.cooldown_seconds,
    evaluated_at: decision.evaluated_at,
    explanation: decision.explanation,
  };
  $("#explainBody").textContent = JSON.stringify(body, null, 2);
  $("#explainModal").classList.remove("hidden");
}
$("#explainClose").addEventListener("click", () => $("#explainModal").classList.add("hidden"));
$("#explainModal").addEventListener("click", (e) => {
  if (e.target.id === "explainModal") $("#explainModal").classList.add("hidden");
});

$("#btnInject").addEventListener("click", async () => {
  let obj;
  try { obj = JSON.parse($("#injectJson").value); }
  catch (e) { return setMsg($("#injectMsg"), "JSON 解析失败：" + e.message, "err"); }
  try {
    const r = await api("/api/alerts/inject", { method: "POST", body: { alert: obj } });
    setMsg($("#injectMsg"),
      `#${r.sequence} 评估为 ${r.decision.verdict === "pass" ? "PASS 放行" : "SUPPRESS 抑制"}` +
      (r.forwarded ? "，已转发到接收端" : ""), "ok");
    await refreshAlerts();
  } catch (e) { setMsg($("#injectMsg"), e.message, "err"); }
});

$("#btnImportOpen").addEventListener("click", () => $("#importCard").classList.remove("hidden"));
$("#btnImportCancel").addEventListener("click", () => $("#importCard").classList.add("hidden"));

$("#btnImportRun").addEventListener("click", async () => {
  let arr;
  try {
    arr = JSON.parse($("#importJson").value);
    if (!Array.isArray(arr)) throw new Error("顶层应为数组 [ {…}, {…} ]");
  } catch (e) { return setMsg($("#importMsg"), e.message, "err"); }
  try {
    const r = await api("/api/alerts/import", {
      method: "POST",
      body: { alerts: arr, auto_forward: $("#importForward").checked },
    });
    const errs = r.errors.length
      ? `；${r.errors.length} 条失败：` + r.errors.map((x) => `#${x.index} ${x.error}`).join("；")
      : "";
    setMsg($("#importMsg"),
      `成功导入 ${r.imported} 条${r.forwarded ? `，转发 ${r.forwarded} 条` : ""}${errs}`,
      r.errors.length ? "err" : "ok");
    $("#importCard").classList.toggle("hidden", r.errors.length === 0);
    await refreshAlerts();
  } catch (e) { setMsg($("#importMsg"), e.message, "err"); }
});

$("#btnUndo").addEventListener("click", async () => {
  try {
    const r = await api("/api/alerts/undo", { method: "POST" });
    toast(`已撤销 #${r.removed_sequence}（${r.device_id}）`);
    await refreshAlerts();
  } catch (e) { toast(e.message, true); }
});

$("#btnClearAlerts").addEventListener("click", async () => {
  if (!confirm("确定清空全部告警与转发记录？（规则与快照保留）")) return;
  await api("/api/alerts/clear", { method: "POST" });
  await refreshAlerts();
});

$("#btnReeval").addEventListener("click", async () => {
  await api("/api/evaluate", { method: "POST" });
  toast("已按当前规则重新评估全部告警");
  await refreshAlerts();
});

// ---------------------------------------------------------------- 规则

let editingRuleId = null;

function matchCell(value) {
  return value && value.trim() ? esc(value) : '<span class="any">任意 *</span>';
}

async function refreshRules() {
  const { rules } = await api("/api/rules");
  const tbody = $("#rulesTable tbody");
  tbody.innerHTML = "";
  $("#rulesEmpty").style.display = rules.length ? "none" : "block";
  for (const r of rules) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono">${r.priority}</td>
      <td><span class="badge ${r.enabled ? "enabled" : "disabled"}">${r.enabled ? "启用" : "停用"}</span></td>
      <td><b>${esc(r.name)}</b>${r.note ? `<div class="vmeta">${esc(r.note)}</div>` : ""}</td>
      <td><span class="badge ${r.action === "pass" ? "pass" : "suppress"}">${r.action}</span></td>
      <td class="match-cell">${matchCell(r.devices)}</td>
      <td class="match-cell">${matchCell(r.batches)}</td>
      <td class="match-cell">${matchCell(r.levels)}</td>
      <td class="match-cell">${matchCell(r.time_window)}</td>
      <td class="match-cell">${r.cooldown_sec > 0
        ? `${r.cooldown_sec}s<br><span class="vmeta">${scopeLabel(r.cooldown_scope)}</span>`
        : '<span class="any">—</span>'}</td>
      <td><div class="row-btns">
        <button class="link-btn" data-edit="${r.id}">编辑</button>
        <button class="link-btn" data-toggle="${r.id}">${r.enabled ? "停用" : "启用"}</button>
        <button class="link-btn danger" data-del="${r.id}">删除</button>
      </div></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => fillRuleForm(rules.find((x) => x.id === Number(b.dataset.edit)))));
  tbody.querySelectorAll("[data-toggle]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/api/rules/${b.dataset.toggle}/toggle`, { method: "PATCH" });
      await refreshRules(); await refreshAlerts();
    }));
  tbody.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("删除这条规则？所有告警将重新评估。")) return;
      await api(`/api/rules/${b.dataset.del}`, { method: "DELETE" });
      await refreshRules(); await refreshAlerts();
    }));
  await refreshVersionList();
}

function scopeLabel(s) {
  return { device_batch: "同设备+批次", device: "同设备", global: "全局" }[s] || s;
}

function fillRuleForm(r) {
  editingRuleId = r.id;
  $("#ruleFormTitle").textContent = `编辑规则 #${r.id}：${r.name}`;
  $("#rName").value = r.name;
  $("#rPriority").value = r.priority;
  $("#rAction").value = r.action;
  $("#rEnabled").checked = !!r.enabled;
  $("#rDevices").value = r.devices;
  $("#rBatches").value = r.batches;
  $("#rLevels").value = r.levels;
  $("#rTimeWindow").value = r.time_window;
  $("#rCooldown").value = r.cooldown_sec;
  $("#rScope").value = r.cooldown_scope;
  $("#rNote").value = r.note;
  $("#btnEditCancel").classList.remove("hidden");
  $("#rName").scrollIntoView({ behavior: "smooth", block: "center" });
}

function resetRuleForm() {
  editingRuleId = null;
  $("#ruleFormTitle").textContent = "新建规则";
  ["rName", "rDevices", "rBatches", "rLevels", "rTimeWindow", "rNote"]
    .forEach((id) => ($("#" + id).value = ""));
  $("#rPriority").value = 100;
  $("#rAction").value = "suppress";
  $("#rEnabled").checked = true;
  $("#rCooldown").value = 0;
  $("#rScope").value = "device_batch";
  $("#btnEditCancel").classList.add("hidden");
}

$("#btnEditCancel").addEventListener("click", resetRuleForm);

$("#btnRuleSave").addEventListener("click", async () => {
  const body = {
    name: $("#rName").value.trim(),
    enabled: $("#rEnabled").checked,
    priority: Number($("#rPriority").value),
    action: $("#rAction").value,
    devices: $("#rDevices").value,
    batches: $("#rBatches").value,
    levels: $("#rLevels").value,
    time_window: $("#rTimeWindow").value,
    cooldown_sec: Number($("#rCooldown").value),
    cooldown_scope: $("#rScope").value,
    note: $("#rNote").value,
  };
  if (!body.name) return setMsg($("#ruleMsg"), "规则名称不能为空", "err");
  try {
    if (editingRuleId) {
      await api(`/api/rules/${editingRuleId}`, { method: "PUT", body });
      toast("规则已更新，全部告警已重新评估");
    } else {
      await api("/api/rules", { method: "POST", body });
      toast("规则已创建，全部告警已重新评估");
    }
    setMsg($("#ruleMsg"), "");
    resetRuleForm();
    await refreshRules();
    await refreshAlerts();
  } catch (e) { setMsg($("#ruleMsg"), e.message, "err"); }
});

// ---------------------------------------------------------------- 策略快照

async function refreshVersionList() {
  const { versions } = await api("/api/versions");
  const ul = $("#versionList");
  ul.innerHTML = "";
  if (!versions.length) {
    ul.innerHTML = '<li class="vmeta">暂无快照。调整规则前先保存一个快照，之后即可对比变更前后结果。</li>';
    return;
  }
  for (const v of versions) {
    const li = document.createElement("li");
    li.innerHTML = `
      <div><b>${esc(v.label)}</b> <span class="vmeta">#${v.id} · ${v.rule_count} 条规则 · ${esc(fmtTime(v.created_at))}</span></div>
      <button class="link-btn danger" data-delv="${v.id}">删除</button>`;
    ul.appendChild(li);
  }
  ul.querySelectorAll("[data-delv]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/api/versions/${b.dataset.delv}`, { method: "DELETE" });
      refreshVersionList();
    }));
}

$("#btnVersionSave").addEventListener("click", async () => {
  const label = $("#versionLabel").value.trim() || `快照 ${new Date().toLocaleString()}`;
  try {
    await api("/api/versions", { method: "POST", body: { label } });
    $("#versionLabel").value = "";
    toast("快照已保存");
    refreshVersionList();
  } catch (e) { toast(e.message, true); }
});

// ---------------------------------------------------------------- 前后对比

async function refreshVersionsForCompare() {
  const { versions } = await api("/api/versions");
  const sel = $("#baseVersion");
  sel.innerHTML = "";
  for (const v of versions) {
    const opt = document.createElement("option");
    opt.value = v.id;
    opt.textContent = `#${v.id} ${v.label}（${v.rule_count} 条规则，${fmtTime(v.created_at)}）`;
    sel.appendChild(opt);
  }
  if (!versions.length) {
    const opt = document.createElement("option");
    opt.textContent = "（暂无快照，请先到规则页保存）";
    sel.appendChild(opt);
  }
}

$("#btnCompare").addEventListener("click", async () => {
  if (!$("#baseVersion").value) return setMsg($("#compareMsg"), "请先保存策略快照", "err");
  try {
    const r = await api("/api/compare", {
      method: "POST",
      body: { base_version_id: Number($("#baseVersion").value) },
    });
    renderCompare(r);
  } catch (e) { setMsg($("#compareMsg"), e.message, "err"); }
});

function renderCompare(r) {
  setMsg($("#compareMsg"),
    `基线：${r.base_version.label}（${fmtTime(r.base_version.created_at)}）`, "ok");
  const s = r.summary;
  $("#compareSummary").classList.remove("hidden");
  $("#compareSummary").innerHTML = `
    <span>告警总数：<b>${s.alerts}</b></span>
    <span>变更前：<b style="color:var(--pass)">${s.before_pass} 放行</b> / <b style="color:var(--suppress)">${s.before_suppress} 抑制</b></span>
    <span>变更后：<b style="color:var(--pass)">${s.after_pass} 放行</b> / <b style="color:var(--suppress)">${s.after_suppress} 抑制</b></span>
    <span>结果发生变化的告警：<b class="${s.changed ? "arrow-change" : "arrow-same"}">${s.changed} 条</b></span>`;

  $("#compareCard").classList.remove("hidden");
  const tbody = $("#compareTable tbody");
  tbody.innerHTML = "";
  for (const row of r.rows) {
    const tr = document.createElement("tr");
    if (row.changed) tr.className = "row-changed";
    tr.innerHTML = `
      <td class="mono">#${row.sequence}</td>
      <td class="mono">${esc(row.device_id)}</td>
      <td class="mono">${esc(row.batch_id) || "—"}</td>
      <td><span class="${levelClass(row.level)}">${esc(row.level)}</span></td>
      <td>${verdictCell(row.before)}</td>
      <td>${verdictCell(row.after)}</td>
      <td>${row.changed
        ? '<span class="arrow-change">● 变化</span>'
        : '<span class="arrow-same">○ 一致</span>'}</td>`;
    tbody.appendChild(tr);
  }
}

function verdictCell(v) {
  return `<div class="verdict-cell">
    ${verdictBadge(v.verdict)}
    <span class="rule-name" title="${esc(v.reason)}">${esc(v.rule_name ? "规则：" + v.rule_name : shorten(v.reason, 40))}</span>
  </div>`;
}

// ---------------------------------------------------------------- 接收端

async function refreshInbox() {
  const { events } = await api("/api/receiver/inbox");
  const box = $("#inboxList");
  box.innerHTML = "";
  $("#inboxEmpty").style.display = events.length ? "none" : "block";
  for (const ev of events) {
    const p = ev.payload || {};
    const div = document.createElement("div");
    div.className = "inbox-event";
    div.innerHTML = `
      <div class="ev-head">
        <span class="ev-title">#${ev.id} ${ev.source === "webhook" ? "外部 POST" : "策略转发"}
          ${p.sequence !== undefined ? `· 告警 #${esc(p.sequence)}` : ""}</span>
        <span>${esc(fmtTime(ev.received_at))}</span>
      </div>
      <div>${p.device_id ? `<span class="mono">${esc(p.device_id)}</span> · ` : ""}
        ${p.level ? `<span class="${levelClass(p.level)}">${esc(p.level)}</span> · ` : ""}
        ${esc(p.message || p.alert_type || "(无消息)")}</div>
      <pre>${esc(JSON.stringify(p, null, 2))}</pre>`;
    box.appendChild(div);
  }
  refreshOverview();
}

$("#btnForwardPending").addEventListener("click", async () => {
  const r = await api("/api/receiver/forward-pending", { method: "POST" });
  setMsg($("#receiverMsg"), r.delivered ? `已投递 ${r.delivered} 条 PASS 告警` : "没有待投递的 PASS 告警",
    r.delivered ? "ok" : "");
  refreshInbox();
});
$("#btnClearInbox").addEventListener("click", async () => {
  await api("/api/receiver/clear", { method: "POST" });
  refreshInbox();
});

// ---------------------------------------------------------------- 设置 / 演示

$("#autoForward").addEventListener("change", async (e) => {
  await api(`/api/settings/auto-forward?enabled=${e.target.checked}`, { method: "POST" });
  toast(e.target.checked ? "已开启注入即转发" : "已关闭注入即转发");
});

$("#btnDemo").addEventListener("click", async () => {
  if (!confirm("载入演示场景会清空当前所有告警、规则、快照与收件箱，是否继续？")) return;
  try {
    const r = await api("/api/demo/load", { method: "POST" });
    toast(`演示数据已载入：${r.rules} 条规则、${r.alerts} 条告警，${r.forwarded_to_receiver} 条 PASS 已进入接收端`);
    resetRuleForm();
    await refreshAlerts();
    await refreshRules();
    await refreshInbox();
    await refreshVersionsForCompare();
  } catch (e) { toast(e.message, true); }
});

// ---------------------------------------------------------------- 初始化

(async function init() {
  await refreshAlerts();
  const { versions } = await api("/api/versions");
  if (versions.length === 0 && $("#statTotal").textContent === "0") {
    // 首次进入：静默载入演示场景，打开即可体验
    await api("/api/demo/load", { method: "POST" });
    await refreshAlerts();
  }
  await refreshRules();
  await refreshVersionsForCompare();
})();
