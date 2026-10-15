// ARTEX web UI — vanilla SPA, talks to the FastAPI backend.
// API base is same-origin by default (proxy /api -> :8000); override via ?api=
const params = new URLSearchParams(location.search);
const API = params.get("api") || (location.port === "8000" ? "" : "http://localhost:8000");

const state = {
  token: localStorage.getItem("artex_token") || "",
  user: localStorage.getItem("artex_user") || "",
  route: { name: "dashboard" },
  sse: null,
};

const $ = (sel) => document.querySelector(sel);
const view = $("#view");

// ---------- helpers ----------
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  setTimeout(() => (el.className = "toast"), 2600);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}
function fmtNum(n) {
  n = Number(n || 0);
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function statusPill(status) {
  return `<span class="pill ${esc(status)}">${esc(status)}</span>`;
}
function sevChip(sev) {
  return `<span class="sev ${esc(sev)}">${esc(sev)}</span>`;
}

// ---------- API ----------
async function http(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  const r = await fetch(API + "/api" + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) { logout(); throw new Error("未授权"); }
  if (r.status === 204) return null;
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || (method + " " + path + ": " + r.status));
  return data;
}

// ---------- auth ----------
async function login(user, pass) {
  const r = await fetch(API + "/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: user, password: pass }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || "登录失败");
  state.token = data.access_token;
  state.user = data.username;
  localStorage.setItem("artex_token", state.token);
  localStorage.setItem("artex_user", state.user);
  enterApp();
}
function logout() {
  state.token = "";
  localStorage.removeItem("artex_token");
  localStorage.removeItem("artex_user");
  if (state.sse) { state.sse.abort(); state.sse = null; }
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  location.hash = "";
}
function enterApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#user-chip").textContent = "👤 " + state.user;
  if (!location.hash) location.hash = "#/dashboard";
  router();
}

// ---------- router ----------
function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = h.split("/");
  return { name: name || "dashboard", arg: rest[0] };
}
async function router() {
  const r = parseRoute();
  state.route = r;
  document.querySelectorAll(".nav-item").forEach((n) =>
    n.classList.toggle("active", n.dataset.route === r.name)
  );
  try {
    if (r.name === "dashboard") return renderDashboard();
    if (r.name === "tasks") return r.arg ? renderTaskDetail(r.arg) : renderTasks();
    if (r.name === "findings") return renderFindings();
    renderDashboard();
  } catch (e) {
    view.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", router);

// ---------- dashboard ----------
async function renderDashboard() {
  $("#page-title").textContent = "仪表盘";
  view.innerHTML = `<div class="empty">加载中…</div>`;
  const [stats, usage, recent] = await Promise.all([
    http("/stats"),
    http("/usage"),
    http("/activities/recent").catch(() => ({ items: [] })),
  ]);

  const sev = stats.findings || {};
  const tok = usage.total || {};
  const daily = (usage.daily || []).slice(-14);
  const maxTok = Math.max(1, ...daily.map((d) => d.input_tokens + d.output_tokens + d.cache_read_tokens + d.cache_write_tokens));

  view.innerHTML = `
    <div class="grid cols-4" style="margin-bottom:16px">
      ${statCard("任务总数", stats.tasks_total, `${stats.tasks_running} 运行中 · ${stats.tasks_done} 完成`)}
      ${statCard("资产", stats.assets_total, `关联资产`)}
      ${statCard("漏洞", stats.findings_total, `严重 ${sev.critical||0} · 高危 ${sev.high||0}`)}
      ${statCard("活动", stats.activities_total, `待处理意图 ${stats.intents_pending||0}`)}
    </div>
    <div class="grid cols-2">
      <div class="card">
        <h3>漏洞分布（按严重度）</h3>
        ${sevBars(sev)}
      </div>
      <div class="card">
        <h3>Token 消耗（近 ${daily.length} 天）</h3>
        ${daily.length ? dailyBars(daily, maxTok) : `<div class="empty">暂无数据</div>`}
        <div class="legend">
          <span>输入 ${fmtNum(tok.input_tokens)}</span>
          <span>输出 ${fmtNum(tok.output_tokens)}</span>
          <span>缓存读 ${fmtNum(tok.cache_read_tokens)}</span>
        </div>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <h3>最近活动</h3>
      <div class="feed" id="dash-feed">${recent.items?.length ? recent.items.map(feedItem).join("") : `<div class="empty">暂无活动</div>`}</div>
    </div>`;
  connectSse(null);
}

function statCard(label, value, sub) {
  return `<div class="card stat-card"><div class="stat-value">${esc(value ?? 0)}</div><div class="stat-label">${esc(label)}</div><div class="muted tiny">${esc(sub || "")}</div></div>`;
}
function sevBars(sev) {
  const items = [["critical", sev.critical], ["high", sev.high], ["medium", sev.medium], ["low", sev.low]];
  const max = Math.max(1, ...items.map((i) => i[1] || 0));
  return `<div class="bars">${items.map(([k, v]) => `
    <div class="bar"><div class="bar-fill" style="height:${((v || 0) / max) * 100}%;background:var(--${k === "critical" ? "red" : k === "high" ? "orange" : k === "medium" ? "yellow" : "primary"})"></div><div class="bar-label">${k} ${v || 0}</div></div>`).join("")}
  </div>`;
}
function dailyBars(daily, max) {
  return `<div class="bars">${daily.map((d) => {
    const total = d.input_tokens + d.output_tokens + d.cache_read_tokens + d.cache_write_tokens;
    return `<div class="bar" title="${esc(d.date)}: ${total}"><div class="bar-fill" style="height:${(total / max) * 100}%"></div><div class="bar-label">${esc(d.date.slice(5))}</div></div>`;
  }).join("")}</div>`;
}
function feedItem(a) {
  const kind = a.activity_type || a.kind || "activity";
  const text = a.agent_thought || a.title || (a.tool_output ? JSON.stringify(a.tool_output) : "") || kind;
  return `<div class="feed-item"><span class="t">${fmtTime(a.created_at || a.ts)}</span><span class="k ${esc(kind)}">${esc(kind)}</span><span>${esc(text).slice(0, 160)}</span></div>`;
}


// ---------- tasks ----------
async function renderTasks() {
  $("#page-title").textContent = "任务";
  view.innerHTML = `
    <div class="section-head">
      <div class="toolbar">
        <input id="task-search" placeholder="搜索任务…" />
        <select id="task-status">
          <option value="">全部状态</option>
          <option value="running">running</option>
          <option value="paused">paused</option>
          <option value="completed">completed</option>
          <option value="failed">failed</option>
        </select>
      </div>
      <button class="btn primary" id="new-task-btn">+ 新建任务</button>
    </div>
    <div class="card"><div id="task-table"><div class="empty">加载中…</div></div></div>`;

  const load = async () => {
    const q = $("#task-search").value.trim();
    const status = $("#task-status").value;
    let path = "/tasks/?limit=200";
    if (status) path += "&status_filter=" + encodeURIComponent(status);
    const data = await http(path);
    let items = data.items || [];
    if (q) items = items.filter((t) => (t.title + (t.description || "")).toLowerCase().includes(q.toLowerCase()));
    $("#task-table").innerHTML = items.length ? `
      <table class="table">
        <thead><tr><th>标题</th><th>类型</th><th>状态</th><th>优先级</th><th>迭代</th><th>创建时间</th></tr></thead>
        <tbody>${items.map((t) => `
          <tr class="row-link" data-id="${t.id}">
            <td>${esc(t.title)}</td>
            <td><span class="badge">${esc(t.task_type)}</span></td>
            <td>${statusPill(t.status)}</td>
            <td>${esc(t.priority)}</td>
            <td>${esc(t.current_iteration)}</td>
            <td class="muted tiny">${fmtTime(t.created_at)}</td>
          </tr>`).join("")}
        </tbody></table>` : `<div class="empty">暂无任务</div>`;
    document.querySelectorAll("#task-table tr.row-link").forEach((tr) =>
      tr.addEventListener("click", () => (location.hash = "#/tasks/" + tr.dataset.id))
    );
  };
  $("#task-search").addEventListener("input", debounce(load, 250));
  $("#task-status").addEventListener("change", load);
  $("#new-task-btn").addEventListener("click", newTaskModal);
  await load();
}

async function newTaskModal() {
  let companies = { items: [] };
  try { companies = await http("/companies/"); } catch {}
  const cOpts = (companies.items || []).map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  openModal(`
    <h2>新建任务</h2>
    <label>公司<select id="nt-company">${cOpts || `<option value="">（无公司，请先创建）</option>`}</select></label>
    <label>标题<input id="nt-title" placeholder="Acme 官网渗透" /></label>
    <label>类型<select id="nt-type">
      <option value="recon">recon</option><option value="scan">scan</option>
      <option value="exploit">exploit</option><option value="full">full</option>
    </select></label>
    <label>目标 (goal)<textarea id="nt-goal" placeholder="拿到后台管理权限…"></textarea></label>
    <label>描述<textarea id="nt-desc"></textarea></label>
    <div class="modal-actions">
      <button class="btn ghost" data-close>取消</button>
      <button class="btn primary" id="nt-save">创建</button>
    </div>`, async () => {
    const company_id = $("#nt-company").value;
    const title = $("#nt-title").value.trim();
    if (!company_id) return toast("请选择公司", true);
    if (!title) return toast("请填写标题", true);
    await http("/tasks/", { method: "POST", body: {
      company_id, title, task_type: $("#nt-type").value,
      goal: $("#nt-goal").value, description: $("#nt-desc").value,
    }});
    toast("任务已创建");
    router();
  });
}


// ---------- task detail ----------
async function renderTaskDetail(id) {
  $("#page-title").textContent = "任务详情";
  view.innerHTML = `<div class="empty">加载中…</div>`;
  const [task, graph, findings, goals, intents] = await Promise.all([
    http("/tasks/" + id),
    http(`/tasks/${id}/graph`).catch(() => ({ nodes: [], edges: [] })),
    http(`/tasks/${id}/findings`).catch(() => ({ items: [], by_severity: {} })),
    http(`/tasks/${id}/goals`).catch(() => ({ items: [], goals_total: 0, goals_met: 0 })),
    http(`/tasks/${id}/intents`).catch(() => ({ items: [] })),
  ]);

  view.innerHTML = `
    <div class="section-head">
      <div>
        <button class="btn ghost sm" onclick="location.hash='#/tasks'">← 返回</button>
        <h2 style="display:inline;margin-left:8px">${esc(task.title)}</h2>
        ${statusPill(task.status)}
      </div>
      <div class="toolbar">
        <button class="btn" id="td-start">启动引擎</button>
        <button class="btn" id="td-stop">停止引擎</button>
        <button class="btn primary" id="td-add-finding">+ 漏洞</button>
      </div>
    </div>
    <div class="grid cols-4" style="margin-bottom:16px">
      ${statCard("目标达成", `${goals.goals_met||0}/${goals.goals_total||0}`, "goals met")}
      ${statCard("漏洞", findings.items?.length || 0, `严重 ${findings.by_severity?.critical||0}`)}
      ${statCard("意图", intents.items?.length || 0, `共 ${intents.total||0}`)}
      ${statCard("图节点", graph.nodes?.length || 0, `${graph.edges?.length||0} 条边`)}
    </div>
    <div class="grid cols-2">
      <div class="card">
        <h3>探索图</h3>
        <div class="graph-wrap"><svg class="graph-svg" id="graph-svg"></svg></div>
        <div class="legend">
          <span><span class="dot" style="background:var(--green)"></span>goal</span>
          <span><span class="dot" style="background:var(--primary)"></span>intent</span>
          <span><span class="dot" style="background:var(--purple)"></span>fact</span>
          <span><span class="dot" style="background:var(--red)"></span>finding</span>
          <span><span class="dot" style="background:var(--yellow)"></span>clue</span>
        </div>
      </div>
      <div class="card">
        <h3>实时活动流</h3>
        <div class="feed" id="td-feed"><div class="empty">等待事件…</div></div>
      </div>
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card"><h3>目标</h3>${goalsList(goals.items)}</div>
      <div class="card"><h3>漏洞</h3>${findingsList(findings.items)}</div>
    </div>`;

  drawGraph(graph);
  connectSse(id);

  $("#td-start").addEventListener("click", async () => { await http(`/engine/${id}/start`, { method: "POST", body: {} }); toast("引擎已启动"); });
  $("#td-stop").addEventListener("click", async () => { await http(`/engine/${id}/stop`, { method: "POST", body: {} }); toast("引擎已停止"); });
  $("#td-add-finding").addEventListener("click", () => addFindingModal(id));
}

function goalsList(items) {
  if (!items?.length) return `<div class="empty">暂无目标</div>`;
  return items.map((g) => `<div class="kv"><span class="k">${statusPill(g.status)}</span><span>${esc(g.title)}</span></div>`).join("");
}
function findingsList(items) {
  if (!items?.length) return `<div class="empty">暂无漏洞</div>`;
  return items.map((f) => `<div class="kv"><span class="k">${sevChip(f.severity)}</span><span>${esc(f.title)}</span><span class="muted tiny">${esc(f.affected_asset || "")}</span></div>`).join("");
}

// ---------- graph (force-ish radial layout) ----------
function drawGraph(graph) {
  const svg = $("#graph-svg");
  if (!svg) return;
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  const W = svg.clientWidth || 600, H = svg.clientHeight || 440;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!nodes.length) { svg.innerHTML = `<text x="${W/2}" y="${H/2}" fill="#8b98ab" text-anchor="middle">暂无图数据</text>`; return; }

  // radial layout: distribute nodes on a circle by type grouping
  const colors = { goal: "#2ecc71", intent: "#4f8cff", fact: "#9b59b6", finding: "#e74c3c", clue: "#f1c40f", action: "#e67e22" };
  const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 50;
  const pos = {};
  nodes.forEach((n, i) => {
    const ang = (i / nodes.length) * Math.PI * 2;
    pos[n.id] = { x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang) };
  });

  let html = "";
  edges.forEach((e) => {
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b) return;
    html += `<line class="gedge" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"><title>${esc(e.anchor_type)}</title></line>`;
  });
  nodes.forEach((n) => {
    const p = pos[n.id];
    const c = colors[n.node_type] || "#8b98ab";
    const label = (n.title || "").slice(0, 10);
    html += `<g class="gnode"><circle cx="${p.x}" cy="${p.y}" r="12" fill="${c}" fill-opacity="0.25" stroke="${c}"></circle>
      <text x="${p.x}" y="${p.y + 26}" text-anchor="middle">${esc(label)}</text>
      <title>${esc(n.node_type)}: ${esc(n.title)}</title></g>`;
  });
  svg.innerHTML = html;
}


// ---------- findings ----------
async function renderFindings() {
  $("#page-title").textContent = "漏洞";
  view.innerHTML = `<div class="empty">加载中…</div>`;
  const tasks = await http("/tasks/?limit=200");
  let all = [];
  await Promise.all((tasks.items || []).map(async (t) => {
    try {
      const f = await http(`/tasks/${t.id}/findings?limit=200`);
      (f.items || []).forEach((x) => all.push({ ...x, _task: t.title, _taskId: t.id }));
    } catch {}
  }));
  all.sort((a, b) => sevRank(b.severity) - sevRank(a.severity));

  const counts = all.reduce((m, f) => ((m[f.severity] = (m[f.severity] || 0) + 1), m), {});
  view.innerHTML = `
    <div class="grid cols-4" style="margin-bottom:16px">
      ${statCard("全部漏洞", all.length, "")}
      ${statCard("严重", counts.critical || 0, "critical")}
      ${statCard("高危", counts.high || 0, "high")}
      ${statCard("中危", counts.medium || 0, "medium")}
    </div>
    <div class="card">
      ${all.length ? `<table class="table">
        <thead><tr><th>严重度</th><th>标题</th><th>类型</th><th>资产</th><th>任务</th><th>状态</th><th>时间</th></tr></thead>
        <tbody>${all.map((f) => `
          <tr class="row-link" data-id="${f._taskId}">
            <td>${sevChip(f.severity)}</td>
            <td>${esc(f.title)}</td>
            <td><span class="badge">${esc(f.vuln_type || "—")}</span></td>
            <td class="muted tiny">${esc(f.affected_asset || "—")}</td>
            <td class="muted tiny">${esc(f._task)}</td>
            <td>${statusPill(f.status)}</td>
            <td class="muted tiny">${fmtTime(f.created_at)}</td>
          </tr>`).join("")}</tbody></table>` : `<div class="empty">暂无漏洞</div>`}
    </div>`;
  document.querySelectorAll(".row-link").forEach((tr) =>
    tr.addEventListener("click", () => (location.hash = "#/tasks/" + tr.dataset.id))
  );
}
function sevRank(s) { return { critical: 4, high: 3, medium: 2, low: 1, info: 0 }[s] || 0; }

async function addFindingModal(taskId) {
  openModal(`
    <h2>登记漏洞</h2>
    <label>标题<input id="nf-title" placeholder="SQL 注入 / IDOR 越权…" /></label>
    <label>严重度<select id="nf-sev">
      <option value="critical">critical</option><option value="high" selected>high</option>
      <option value="medium">medium</option><option value="low">low</option><option value="info">info</option>
    </select></label>
    <label>类型<input id="nf-type" placeholder="sqli / idor / rce" /></label>
    <label>受影响资产<input id="nf-asset" placeholder="https://admin.acme.com/…" /></label>
    <label>证据<textarea id="nf-ev"></textarea></label>
    <div class="modal-actions">
      <button class="btn ghost" data-close>取消</button>
      <button class="btn primary" id="nf-save">保存</button>
    </div>`, async () => {
    const title = $("#nf-title").value.trim();
    if (!title) return toast("请填写标题", true);
    await http(`/tasks/${taskId}/findings`, { method: "POST", body: {
      title, severity: $("#nf-sev").value, vuln_type: $("#nf-type").value,
      affected_asset: $("#nf-asset").value, evidence: $("#nf-ev").value,
    }});
    toast("漏洞已登记");
    router();
  });
}


// ---------- modal + misc ----------
function openModal(html, onSave) {
  closeModal();
  const back = document.createElement("div");
  back.className = "modal-back";
  back.id = "modal-back";
  back.innerHTML = `<div class="modal">${html}</div>`;
  document.body.appendChild(back);
  back.addEventListener("click", (e) => { if (e.target === back) closeModal(); });
  back.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeModal));
  const save = back.querySelector("#nt-save, #nf-save");
  if (save && onSave) save.addEventListener("click", onSave);
}
function closeModal() { document.getElementById("modal-back")?.remove(); }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ---------- SSE ----------
function connectSse(taskId) {
  if (state.sse) { state.sse.abort(); state.sse = null; }
  const dot = $("#conn-dot");
  const feed = $(taskId ? "#td-feed" : "#dash-feed");
  (async () => {
    try {
      const path = taskId ? `/tasks/${taskId}/stream` : "/tasks/stream";
      const resp = await fetch(API + "/api" + path, { headers: { Authorization: "Bearer " + state.token } });
      if (!resp.ok || !resp.body) { if (dot) dot.className = "conn-dot off"; return; }
      if (dot) dot.className = "conn-dot on";
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          const evt = chunk.split("\n").find((l) => l.startsWith("event:"))?.slice(6).trim();
          const data = chunk.split("\n").find((l) => l.startsWith("data:"))?.slice(5).trim();
          if (evt === "heartbeat" || !data) continue;
          if (feed) {
            const empty = feed.querySelector(".empty"); if (empty) empty.remove();
            const div = document.createElement("div");
            div.className = "feed-item";
            div.innerHTML = `<span class="t">${fmtTime(new Date().toISOString())}</span><span class="k ${esc(evt)}">${esc(evt)}</span><span>${esc(data).slice(0, 160)}</span>`;
            feed.prepend(div);
            while (feed.children.length > 100) feed.lastChild.remove();
          }
        }
      }
    } catch (e) {
      if (dot) dot.className = "conn-dot off";
    }
  })();
}

// ---------- bootstrap ----------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-err").textContent = "";
  try { await login($("#li-user").value, $("#li-pass").value); }
  catch (err) { $("#login-err").textContent = err.message; }
});
$("#logout-btn").addEventListener("click", logout);
setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString(); }, 1000);
$("#clock").textContent = new Date().toLocaleTimeString();
if (state.token) enterApp();

