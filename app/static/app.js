// KnowledgeDesk web interface – plain JavaScript, no build step.
"use strict";

// ================================================================== basics
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => Number(n || 0).toLocaleString();
const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many}`;
const ic = (name, cls = "") => `<svg class="ic ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const BADGE_ICON = { ok: "check", bad: "alert", info: "info" };
const badge = (state, text) => `<span class="badge ${state}">${state === "busy" ? '<span class="spinner"></span>' : ic(BADGE_ICON[state] || "info")}${esc(text)}</span>`;
function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts).toLocaleDateString();
}

let token = null;
try { token = localStorage.getItem("kd_token"); } catch {}
let me = null;
let departments = [];
let appStatus = {};
let pagePoll = null;

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  let res;
  try { res = await fetch(path, { ...opts, headers }); }
  catch { throw new Error("Can't reach KnowledgeDesk. Check that it's running, then try again."); }
  if (res.status === 401 && path !== "/api/login") { logout(); throw new Error("Please log in again."); }
  if (!res.ok) {
    let msg = `Something went wrong (${res.status}).`;
    try { const d = (await res.json()).detail; if (d) msg = typeof d === "string" ? d : (d[0]?.msg || JSON.stringify(d)); } catch {}
    throw new Error(msg);
  }
  return res;
}
const getJSON = async (p) => (await api(p)).json();

function toast(message, bad = false) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : ""); t.setAttribute("role", bad ? "alert" : "status");
  t.innerHTML = `${ic(bad ? "error" : "check")}<span>${esc(message)}</span>`;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3500);
}

function openDialog(html) {
  const d = $("#dialog");
  $("#dialog-form").innerHTML = html;
  if (!d.open) d.showModal();
  return $("#dialog-form");
}
function closeDialog() { if ($("#dialog").open) $("#dialog").close(); }
function confirmBox({ title, text, confirm = "OK", danger = false }) {
  return new Promise((resolve) => {
    const f = openDialog(`<h2>${esc(title)}</h2><p class="muted">${esc(text)}</p>
      <div class="dialog-actions"><button class="btn" type="button" data-no>Cancel</button>
      <button class="btn ${danger ? "danger solid" : "primary"}" type="button" data-yes>${esc(confirm)}</button></div>`);
    $("[data-no]", f).onclick = () => { closeDialog(); resolve(false); };
    $("[data-yes]", f).onclick = () => { closeDialog(); resolve(true); };
    $("#dialog").addEventListener("cancel", () => resolve(false), { once: true });
  });
}

async function busy(button, label, fn) {
  const old = button.innerHTML; button.disabled = true;
  button.innerHTML = `<span class="spinner"></span>${esc(label)}`;
  try { return await fn(); } finally { button.disabled = false; button.innerHTML = old; }
}
function debounce(fn, ms = 300) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ---------- who can find this: Everyone OR departments
function visibilityPicker(selected = [], everyone = false, title = "Who can find this?") {
  const boxes = departments.map((d) => `<label class="check"><input type="checkbox" value="${d.id}" ${selected.includes(d.id) ? "checked" : ""}> ${esc(d.name)}</label>`).join("");
  return `<div class="vis-block"><span class="label">${esc(title)}</span>
    <label class="check"><input type="checkbox" class="vis-everyone" ${everyone ? "checked" : ""}> <b>Everyone in the company</b></label>
    ${boxes ? `<div class="muted small">or only these departments:</div><div class="vis-depts ${everyone ? "off" : ""}">${boxes}</div>` :
      '<div class="muted small">To limit it to certain teams, first create departments under People.</div>'}</div>`;
}
document.addEventListener("change", (e) => {
  const block = e.target.closest?.(".vis-block");
  if (!block) return;
  const everyone = $(".vis-everyone", block);
  const depts = $$(".vis-depts input", block);
  if (e.target === everyone && everyone.checked) depts.forEach((d) => (d.checked = false));
  if (e.target !== everyone && e.target.checked) everyone.checked = false;
  $(".vis-depts", block)?.classList.toggle("off", everyone.checked);
});
function readVisibility(root) {
  const el = root.classList?.contains("vis-block") ? root : $(".vis-block", root);
  return { company_wide: $(".vis-everyone", el).checked, department_ids: $$(".vis-depts input:checked", el).map((x) => Number(x.value)) };
}
function visibilityError(v) { return !v.company_wide && !v.department_ids.length ? "Choose who can find it: Everyone, or at least one department." : ""; }
function visibilityPills(item) {
  if (item.company_wide) return '<span class="pill">Everyone</span>';
  return (item.departments || []).map((d) => `<span class="pill">${esc(d.name)}</span>`).join("") || '<span class="pill">Admins only</span>';
}
async function refreshDepartments() { departments = await getJSON("/api/departments"); }

// ---------- what can be searched
const KIND_ICON = { file: "file", documents: "file", interview: "mic", whatsapp: "chat", email: "mail", calendar: "calendar", web: "globe", slack: "ask" };
const KIND_NAMES = { email: ["email", "emails"], documents: ["document", "documents"], calendar: ["calendar event", "calendar events"],
  slack: ["Slack message", "Slack messages"], web: ["web page", "web pages"], whatsapp: ["WhatsApp chat", "WhatsApp chats"], interview: ["interview", "interviews"] };
function knowledgeChips(ready) {
  return Object.entries(KIND_NAMES).filter(([k]) => ready[k]).map(([k, [one, many]]) =>
    `<span class="stat-chip">${ic(KIND_ICON[k])}<span><b>${num(ready[k])}</b> ${ready[k] === 1 ? one : many}</span></span>`).join("");
}
function docBadge(d) {
  if (d.status === "ready") return badge("ok", "Searchable");
  if (d.status === "error") return badge("bad", "Couldn't read");
  return badge("busy", "Preparing for search");
}

// ================================================================== start, login, navigation
const ADMIN_PAGES = [["home", "Home", "home"], ["ask", "Ask", "ask"], ["sources", "Sources", "sources"], ["people", "People", "people"],
  ["activity", "Activity", "activity"], ["settings", "Settings", "settings"]];
const MEMBER_PAGES = [["ask", "Ask", "ask"], ["library", "Library", "library"]];
const LOADERS = {};

function showView(id) { $$("body > section").forEach((s) => s.classList.add("hidden")); $(id).classList.remove("hidden"); }
function logout() { token = null; try { localStorage.removeItem("kd_token"); } catch {} me = null; showView("#login-view"); }
function initials(name) { return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join(""); }

async function boot() {
  try { appStatus = await (await fetch("/api/status")).json(); } catch { appStatus = { company: "", license: {} }; }
  $("#login-company").textContent = appStatus.company || "";
  $("#company-name").textContent = appStatus.company || "";
  $("#app-version").textContent = appStatus.version ? `Version ${appStatus.version}` : "";
  if (!token) return showView("#login-view");
  try { me = await getJSON("/api/me"); } catch { return showView("#login-view"); }
  await refreshDepartments();
  $("#user-name").innerHTML = `<span class="avatar">${esc(initials(me.name || me.email))}</span>
    <span class="who"><b>${esc(me.name || me.email)}</b><span>${me.role === "admin" ? "Admin" : esc(me.email)}</span></span>`;
  const pages = me.role === "admin" ? ADMIN_PAGES : MEMBER_PAGES;
  $("#nav").innerHTML = pages.map(([id, label, icon]) => `<a href="#${id}" data-page="${id}">${ic(icon)}<span>${label}</span></a>`).join("");
  updateBanner();
  showView("#app-view");
  const connected = new URLSearchParams(location.search).get("connected");
  if (connected !== null) {
    window.history.replaceState(null, "", "/#sources");
    toast("Account connected. Importing now – you can follow the progress on this page.");
  }
  route();
}
function updateBanner() {
  const lic = appStatus.license || {};
  const banner = $("#banner");
  let html = "";
  if (!appStatus.llm_configured && me?.role === "admin") html = `${ic("alert")}<span>Answers are off until a Claude API key is added. <a href="#settings">Add it in Settings</a></span>`;
  else if (lic.message && lic.state !== "unchecked") html = `${ic("alert")}<span>${esc(lic.message)}</span>`;
  banner.innerHTML = html; banner.classList.toggle("hidden", !html);
}

function route() {
  if (!me) return;
  clearTimeout(pagePoll);
  const pages = me.role === "admin" ? ADMIN_PAGES : MEMBER_PAGES;
  let name = location.hash.slice(1);
  if (!pages.some(([id]) => id === name)) name = pages[0][0];
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.id !== `page-${name}`));
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === name));
  LOADERS[name]?.().catch((err) => toast(err.message, true));
}
window.addEventListener("hashchange", route);
function poll(fn, ms) { clearTimeout(pagePoll); pagePoll = setTimeout(() => fn().catch(() => {}), ms); }

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  $("#login-error").textContent = "";
  await busy($("button", f), "Logging in…", async () => {
    try {
      const r = await (await api("/api/login", { method: "POST", json: { email: f.email.value, password: f.password.value } })).json();
      token = r.token; try { localStorage.setItem("kd_token", token); } catch {}
      f.password.value = "";
      await boot();
    } catch (err) { $("#login-error").textContent = err.message; }
  });
});
$("#logout").addEventListener("click", logout);
$("#user-name").addEventListener("click", () => {
  const f = openDialog(`<h2>My account</h2><p class="muted">${esc(me.name || "")} · ${esc(me.email)}</p>
    <label>Current password <input name="current" type="password" autocomplete="current-password" required></label>
    <label>New password (at least 8 characters) <input name="next" type="password" autocomplete="new-password" required minlength="8"></label>
    <p class="error" role="alert"></p>
    <div class="dialog-actions"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="submit">Change password</button></div>`);
  $("[data-close]", f).onclick = closeDialog;
  f.onsubmit = (e) => {
    e.preventDefault();
    busy($("button[type=submit]", f), "Saving…", async () => {
      try { await api("/api/me/password", { method: "POST", json: { current_password: f.current.value, new_password: f.next.value } }); closeDialog(); toast("Password changed."); }
      catch (err) { $(".error", f).textContent = err.message; }
    });
  };
});

// ================================================================== Home (admins)
LOADERS.home = async function loadHome() {
  const first = (me.name || "").split(" ")[0];
  $("#home-title").textContent = first ? `Hi ${first}` : "Home";
  const [setup, health, overview] = await Promise.all([getJSON("/api/admin/setup"), getJSON("/api/admin/health"), getJSON("/api/overview")]);

  const issues = health.checks.filter((c) => !["ok", "info"].includes(c.state));
  const bad = issues.some((c) => c.state === "bad");
  $("#home-health").innerHTML = !issues.length
    ? `<div class="health-banner ok">${ic("check")}<b>Everything is working.</b></div>`
    : `<div class="health-banner ${bad ? "bad" : "warn"}">${ic(bad ? "error" : "clock")}<div>
        <b>${bad ? "Some things need your attention" : "Almost ready"}</b>
        <ul>${issues.map((c) => `<li><b>${esc(c.label)}:</b> ${esc(c.detail)}</li>`).join("")}</ul>
        <a href="#settings">Open the health check</a></div></div>`;

  const steps = [
    [setup.has_sources, "Connect your company's information", "Email, calendar, Drive, your website, documents.", "#sources", "Connect sources"],
    [setup.has_team, "Add your team", "Create departments and add the people who will ask questions.", "#people", "Add people"],
    [setup.has_asked, "Ask your first question", "See how KnowledgeDesk answers from your own information.", "#ask", "Ask a question"],
  ];
  const done = steps.filter((s) => s[0]).length;
  const nextIndex = steps.findIndex((s) => !s[0]);
  $("#home-setup").innerHTML = done === steps.length ? "" : `<div class="card"><div class="toolbar"><h2>Get set up</h2>
      <span class="muted small">${done} of ${steps.length} done</span></div>
    <ol class="steps">${steps.map(([ok, title, text, href, label], i) => `<li class="${ok ? "done" : ""}">
      <span class="step-dot">${ok ? ic("tick") : i + 1}</span>
      <div><div class="step-title"><b>${title}</b></div><div class="muted small">${text}</div></div>
      ${ok ? badge("ok", "Done") : `<a class="btn ${nextIndex === i ? "primary" : ""}" href="${href}">${label}</a>`}</li>`).join("")}</ol></div>`;

  $("#home-kb").innerHTML = `<h2>What can be searched</h2>
    ${overview.total ? `<div class="chips">${knowledgeChips(overview.ready)}</div>` : '<p class="muted">Nothing yet.</p>'}
    ${overview.importing ? `<p class="status-detail">${badge("busy", "Preparing for search")} ${plural(overview.importing, "item", "items")} in line${overview.eta ? `, about ${esc(overview.eta)} left` : ""}.</p>` : ""}
    <div class="row">${overview.total ? '<a class="btn primary" href="#ask">Ask a question</a>' : ""}<a class="btn" href="#sources">Manage sources</a></div>`;
  $("#home-activity").innerHTML = `<h2>Questions</h2>
    <div class="grid2"><div><div class="big-number">${num(setup.questions_week)}</div><div class="muted small">asked this week</div></div>
    <div><div class="big-number">${num(setup.gaps)}</div><div class="muted small">without a good answer (30 days)</div></div></div>
    <div class="row"><a class="btn" href="#activity">See all questions</a></div>`;
  if (overview.importing || issues.some((c) => c.state === "busy")) poll(LOADERS.home, 8000);
};

// ================================================================== Ask
const chat = { history: [], busy: false };
const EXAMPLES = {
  calendar: "What meetings do I have this week?", email: "What were the latest emails about prices?",
  documents: "What are our main procedures?", web: "What services do we offer?",
  interview: "How do we handle a new customer order?", whatsapp: "What was decided in the team chat recently?",
  slack: "What was discussed in Slack this week?",
};
LOADERS.ask = async function loadAsk() {
  if (!$("#dept-filter").dataset.filled) {
    const mine = me.role === "admin" ? departments : me.departments;
    $("#dept-filter").innerHTML = `<option value="">Everything I can see</option>` +
      mine.map((d) => `<option value="${d.id}">${esc(d.name)} only</option>`).join("");
    $("#dept-filter").dataset.filled = "1";
  }
  if (!chat.history.length) await drawWelcome();
  $("#question").focus();
};
async function drawWelcome() {
  let o = { ready: {}, total: 0 };
  try { o = await getJSON("/api/overview"); } catch {}
  const examples = Object.keys(EXAMPLES).filter((k) => o.ready[k]).map((k) => EXAMPLES[k]).slice(0, 4);
  $("#messages").innerHTML = `<div class="welcome">
    <h2>What would you like to know?</h2>
    ${o.total ? `<p class="muted">Answers come only from your company's own information, with sources.</p>
      <div class="chips">${knowledgeChips(o.ready)}</div>
      <div class="examples">${examples.map((x) => `<button type="button">${esc(x)}</button>`).join("")}</div>`
    : me.role === "admin" ? `<p class="muted">Nothing can be searched yet.</p><a class="btn primary" href="#sources">Connect your first source</a>`
    : `<p class="muted">Nothing can be searched yet. Your admin is still adding the company's information.</p>`}</div>`;
  $$(".examples button").forEach((b) => b.onclick = () => { $("#question").value = b.textContent; $("#ask-form").requestSubmit(); });
}

function renderAnswer(text, sources) {
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\[(\d+)\]/g, (m, n) => {
      const s = sources[Number(n) - 1];
      return s ? `<span class="cite" title="${esc(s.title + (s.location ? " – " + s.location : ""))}">[${n}]</span>` : m;
    });
  const out = []; let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*•]\s+(.*)/); const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (bullet || numbered) {
      const type = bullet ? "ul" : "ol";
      if (!list || list.type !== type) { if (list) out.push(`</${list.type}>`); out.push(`<${type}>`); list = { type }; }
      out.push(`<li>${inline((bullet || numbered)[1])}</li>`); continue;
    }
    if (list) { out.push(`</${list.type}>`); list = null; }
    if (!line.trim()) continue;
    const h = line.match(/^#{1,4}\s+(.*)/);
    out.push(h ? `<p><b>${inline(h[1])}</b></p>` : `<p>${inline(line)}</p>`);
  }
  if (list) out.push(`</${list.type}>`);
  return out.join("");
}
function addMessage(cls, html) {
  $(".welcome")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${cls}`; div.dir = "auto"; div.innerHTML = html;
  $("#messages").append(div);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return div;
}
const qBox = $("#question");
qBox.addEventListener("input", () => { qBox.style.height = "auto"; qBox.style.height = qBox.scrollHeight + "px"; });
qBox.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask-form").requestSubmit(); } });
$("#new-chat").addEventListener("click", () => { chat.history = []; drawWelcome(); qBox.focus(); });

$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = qBox.value.trim();
  if (!question || chat.busy) return;
  chat.busy = true; $("#ask-btn").disabled = true;
  qBox.value = ""; qBox.style.height = "auto";
  addMessage("user", esc(question));
  const bot = addMessage("bot", `<div class="thinking"><span class="spinner"></span><span>Searching your company's information…</span></div>`);
  const dept = $("#dept-filter").value;
  let text = "", sources = [], qid = null, failed = "";
  try {
    const res = await api("/api/ask", { method: "POST", json: { question, history: chat.history, department_id: dept ? Number(dept) : null } });
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buf.slice(0, nl)); buf = buf.slice(nl + 1);
        if (ev.type === "sources") { sources = ev.sources; const t = $(".thinking span:last-child", bot); if (t && sources.length) t.textContent = `Found ${plural(sources.length, "relevant passage", "relevant passages")}. Writing the answer…`; }
        if (ev.type === "text") { text += ev.text; bot.innerHTML = renderAnswer(text, sources); }
        if (ev.type === "error") failed = ev.message;
        if (ev.type === "done") qid = ev.question_id;
      }
      $("#messages").scrollTop = $("#messages").scrollHeight;
    }
  } catch (err) { failed = err.message; }
  chat.busy = false; $("#ask-btn").disabled = false;
  if (failed && !text) {
    bot.innerHTML = `<p class="state-text bad">${ic("alert")} ${esc(failed)}</p><button class="btn small" type="button">${ic("refresh")}Try again</button>`;
    $("button", bot).onclick = () => { bot.previousElementSibling?.remove(); bot.remove(); qBox.value = question; $("#ask-form").requestSubmit(); };
    return;
  }
  chat.history.push({ role: "user", content: question }, { role: "assistant", content: text });
  const cited = sources.filter((s) => text.includes(`[${s.n}]`));
  if (cited.length) {
    bot.insertAdjacentHTML("beforeend", `<div class="sources"><b>Sources</b><ol>${cited.map((s) => `<li value="${s.n}">${ic(KIND_ICON[s.kind] || "file")}
      ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>` : `<a href="#" data-doc="${s.document_id}">${esc(s.title)}</a>`}
      ${s.location ? `<span class="muted">(${esc(s.location)})</span>` : ""}</li>`).join("")}</ol></div>`);
  }
  if (qid) {
    bot.insertAdjacentHTML("beforeend", `<div class="feedback">Was this helpful? <button type="button" data-v="1" aria-label="Helpful">${ic("up")}</button><button type="button" data-v="-1" aria-label="Not helpful">${ic("down")}</button></div>`);
    $$(".feedback button", bot).forEach((b) => b.onclick = async () => {
      $$(".feedback button", bot).forEach((x) => x.classList.toggle("chosen", x === b));
      try { await api(`/api/questions/${qid}/feedback`, { method: "POST", json: { value: Number(b.dataset.v) } }); toast("Thanks – your feedback helps improve the answers."); }
      catch (err) { toast(err.message, true); }
    });
  }
  qBox.focus();
});

document.addEventListener("click", async (e) => {
  const a = e.target.closest("a[data-doc]");
  if (!a) return;
  e.preventDefault();
  try {
    const res = await api(`/api/documents/${a.dataset.doc}/file`);
    const url = URL.createObjectURL(await res.blob());
    const name = (res.headers.get("content-disposition") || "").match(/filename\*?=(?:UTF-8'')?"?([^";]+)/i)?.[1];
    const link = Object.assign(document.createElement("a"), { href: url, target: "_blank", rel: "noopener" });
    if (name && !/\.(pdf|txt|html?|png|jpe?g)$/i.test(decodeURIComponent(name))) link.download = decodeURIComponent(name);
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (err) { toast(err.message, true); }
});

// ================================================================== document lists
function docRow(d, { manage = false } = {}) {
  const when = new Date(d.updated_at).toLocaleDateString();
  const link = d.metadata?.url || d.metadata?.link;
  return `<div class="list-row"><div>
      <div class="title" dir="auto">${ic(KIND_ICON[d.kind] || "file")}${link ? `<a href="${esc(link)}" target="_blank" rel="noopener">${esc(d.title)}</a>`
        : `<a href="#" data-doc="${d.id}">${esc(d.title)}</a>`}</div>
      <div class="meta">${when} · ${visibilityPills(d)} ${manage ? docBadge(d) : ""}
        ${manage && d.error ? `<div class="state-text bad small">${esc(d.error)}</div>` : ""}</div></div>
    ${manage ? `<div class="actions">${d.status === "error" ? `<button type="button" class="btn link" data-retry="${d.id}">Try again</button>` : ""}
      <button type="button" class="btn link danger" data-del-doc="${d.id}" data-title="${esc(d.title)}">Delete</button></div>` : "<div></div>"}</div>`;
}
async function pagedDocs(container, params, { manage = false, pageSize = 25, empty = "Nothing here yet." } = {}) {
  const state = container._state || (container._state = { offset: 0 });
  if (params.reset) state.offset = 0;
  const q = new URLSearchParams({ ...params.query, limit: pageSize, offset: state.offset });
  const res = await api(`/api/documents?${q}`);
  const rows = await res.json(); const total = Number(res.headers.get("X-Total-Count") || rows.length);
  container.innerHTML = rows.length ? rows.map((d) => docRow(d, { manage })).join("") +
    (total > pageSize ? `<div class="pager"><span>${num(state.offset + 1)}–${num(state.offset + rows.length)} of ${num(total)}</span><span class="row tight">
      <button type="button" class="btn small" data-prev ${state.offset ? "" : "disabled"}>Newer</button>
      <button type="button" class="btn small" data-next ${state.offset + pageSize < total ? "" : "disabled"}>Older</button></span></div>` : "")
    : `<div class="empty">${esc(empty)}</div>`;
  const again = () => pagedDocs(container, { query: params.query }, { manage, pageSize, empty });
  $("[data-prev]", container)?.addEventListener("click", () => { state.offset = Math.max(0, state.offset - pageSize); again(); });
  $("[data-next]", container)?.addEventListener("click", () => { state.offset += pageSize; again(); });
  $$("[data-del-doc]", container).forEach((b) => b.onclick = async () => {
    if (!await confirmBox({ title: "Delete this file?", text: `"${b.dataset.title}" will no longer be searchable.`, confirm: "Delete", danger: true })) return;
    try { await api(`/api/documents/${b.dataset.delDoc}`, { method: "DELETE" }); toast("Deleted."); again(); } catch (err) { toast(err.message, true); }
  });
  $$("[data-retry]", container).forEach((b) => b.onclick = async () => {
    await api(`/api/documents/${b.dataset.retry}/reindex`, { method: "POST" }); toast("Trying again."); again();
  });
  return { rows, total };
}

LOADERS.library = async () => pagedDocs($("#lib-list"), { reset: true, query: { q: $("#lib-search").value, status: "ready" } },
  { empty: "Nothing to show yet. Your admin is still adding the company's information." });
$("#lib-search").addEventListener("input", debounce(() => LOADERS.library()));

// ================================================================== Sources
const PROVIDER_TYPES = { google: { mail: "gmail", calendar: "gcal", files: "gdrive" }, microsoft: { mail: "ms_mail", calendar: "ms_cal", files: "ms_drive" },
  slack: { messages: "slack" } };
const PROVIDER_NAME = { google: "Google", microsoft: "Microsoft", slack: "Slack" };
const PROVIDER_ACCOUNT = { google: "account", microsoft: "account", slack: "workspace" };
const PROVIDER_SERVICES = {
  google: [["mail", "Gmail", "all mail except spam, trash and promotions"], ["calendar", "Google Calendar", ""], ["files", "Google Drive", ""]],
  microsoft: [["mail", "Outlook mail", "all mail in Inbox and Sent"], ["calendar", "Outlook calendar", ""], ["files", "OneDrive", ""]],
  slack: [["messages", "Slack channels", "public channels, plus private channels you add the KnowledgeDesk app to"]],
};
const SERVICE_INFO = {
  gmail: ["mail", "Gmail", "email", "emails"], ms_mail: ["mail", "Outlook mail", "email", "emails"],
  gcal: ["calendar", "Google Calendar", "event", "events"], ms_cal: ["calendar", "Outlook calendar", "event", "events"],
  gdrive: ["folder", "Google Drive", "file", "files"], ms_drive: ["folder", "OneDrive", "file", "files"],
  imap: ["mail", "Mailbox", "email", "emails"], ics: ["calendar", "Calendar", "event", "events"], website: ["globe", "Website", "page", "pages"],
  slack: ["ask", "Channels", "message", "messages"],
};
const CALENDAR_TYPES = ["gcal", "ms_cal", "ics", "slack"]; // counted in events / messages, not documents
let conns = [];
let overviewNow = { importing: 0 };
let oauthState = { configured: {}, redirect_url: "" };
let editing = false; // a form or panel is open: refreshes must not redraw it

function progressBar(done, total) {
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return `<div class="progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct}%"></span></div>`;
}
// Clear, honest status for one source: connected? importing? ready for search? any problem?
function sourceStatus(k) {
  const [, , one, many] = SERVICE_INFO[k.type] || ["", "", "item", "items"];
  const ready = CALENDAR_TYPES.includes(k.type) ? k.events : k.ready;
  const checked = k.last_sync ? `<span class="muted small">Checked ${ago(k.last_sync)}</span>` : "";
  let note = k.last_note ? `<div class="status-detail">${esc(k.last_note)}</div>` : "";
  if (k.type === "slack" && k.channels != null) note += `<div class="status-detail">Reading ${plural(k.channels, "channel", "channels")}.${k.private_not_added
    ? ` ${plural(k.private_not_added, "private channel isn't", "private channels aren't")} included – in Slack, open the channel → its name → Integrations → Add apps → KnowledgeDesk.` : ""}</div>`;
  const searchable = ready ? `${plural(ready, one, many)} searchable` : "";
  if (k.last_error && !k.syncing)
    return `<div class="status-line">${badge("bad", "Needs attention")} ${checked}</div><div class="status-detail bad">${esc(k.last_error)}</div>
      ${ready ? `<div class="status-detail">${searchable} already.</div>` : ""}${note}`;
  const p = k.progress;
  if (k.syncing || !k.last_sync || k.more) {
    const what = p && p.total ? `Importing ${num(p.done)} of ${plural(p.total, one, many)}` : `Importing… ${plural(k.documents, one, many)} so far`;
    return `<div class="status-line">${badge("ok", "Connected")} <span class="state-text busy small">${what}</span></div>
      ${p && p.total ? progressBar(p.done, p.total) : ""}${searchable ? `<div class="status-detail">${searchable} so far.</div>` : ""}${note}`;
  }
  if (k.indexing) {
    const total = k.documents;
    const done = k.ready;
    return `<div class="status-line">${badge("ok", "Connected")} <span class="state-text busy small">Preparing for search: ${num(done)} of ${plural(total, one, many)} ready</span></div>
      ${progressBar(done, total)}<div class="status-detail">Items are prepared one by one${overviewNow.importing > k.indexing ? ` – ${num(overviewNow.importing)} items in line in total` : ""}${overviewNow.eta ? `, about ${esc(overviewNow.eta)} left` : ""}. You can already ask about the ready ones.</div>${note}`;
  }
  return `<div class="status-line">${badge("ok", "Connected")} <span class="state-text ok small">${searchable || "Nothing to import yet"}</span> ${checked}</div>${note}`;
}

LOADERS.sources = async function loadSources() {
  await refreshDepartments();
  try { oauthState = await getJSON("/api/oauth/status"); } catch {}
  editing = false;
  $$(".pc-panel").forEach((el) => (el.innerHTML = ""));
  $("#web-visibility").innerHTML = visibilityPicker([], true, "Who can find its pages?");
  $("#upload-visibility").innerHTML = visibilityPicker([], false);
  resetConnForm();
  await refreshSources(true);
};

const TILES = [
  ["gmail", "Gmail", "mail", "google"], ["gcal", "Google Calendar", "calendar", "google"], ["gdrive", "Google Drive", "folder", "google"],
  ["ms_mail", "Outlook mail", "mail", "microsoft"], ["ms_cal", "Outlook calendar", "calendar", "microsoft"], ["ms_drive", "OneDrive", "folder", "microsoft"],
  ["slack", "Slack", "ask", "slack"], ["website", "Websites", "globe", "web-card"], ["whatsapp", "WhatsApp chats", "chat", "whatsapp-card"],
  ["uploads", "Uploaded files", "upload", "files-card"],
];
function tileState(type) {
  const ks = conns.filter((k) => k.type === type);
  if (type === "whatsapp" || type === "uploads") {
    const n = type === "whatsapp" ? (overviewNow.ready.whatsapp || 0) : null;
    if (type === "whatsapp") return n ? ["ok", "Imported", plural(n, "chat", "chats")] : ["", "Not added", ""];
    return ["", "", ""];
  }
  if (!ks.length) return ["", "Not connected", ""];
  const count = ks.reduce((a, k) => a + (CALENDAR_TYPES.includes(k.type) ? k.events : k.ready), 0);
  const [, , one, many] = SERVICE_INFO[type];
  if (ks.some((k) => k.last_error && !k.syncing)) return ["bad", "Needs attention", count ? plural(count, one, many) : ""];
  if (ks.some((k) => k.syncing || !k.last_sync || k.more || k.indexing)) return ["busy", "Importing", count ? `${plural(count, one, many)} so far` : ""];
  return ["ok", "Connected", plural(count, one, many)];
}
function drawTiles(uploads) {
  $("#overview-tiles").innerHTML = TILES.map(([type, label, icon, target]) => {
    let [state, text, count] = tileState(type);
    if (type === "uploads") [state, text, count] = uploads ? ["ok", "Added", plural(uploads, "file", "files")] : ["", "None yet", ""];
    return `<button type="button" class="tile ${state ? "" : "off"}" data-tile="${type}" data-target="${target}">
      <span class="icon-tile">${ic(icon)}</span><span><b>${label}</b>${text ? badge(state, text) : ""}${count ? `<div class="count">${count}</div>` : ""}</span></button>`;
  }).join("");
  $$("#overview-tiles .tile").forEach((t) => t.onclick = () => {
    const el = document.getElementById(t.dataset.target) || $(`.provider-card[data-provider=${t.dataset.target}]`);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}
$("#find-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = e.target.q.value.trim();
  if (!q) return;
  const f = openDialog(`<h2>Find an item</h2>
    <p class="muted small">Searches the titles and the text of everything imported. If something isn't here, it hasn't been imported (yet).</p>
    <div class="search-box" style="width:100%">${ic("search")}<input type="search" data-q dir="auto" value="${esc(q)}"></div>
    <div data-list style="margin-top:8px"></div>
    <div class="dialog-actions"><button class="btn" type="button" data-close>Close</button></div>`);
  const list = $("[data-list]", f);
  const load = () => pagedDocs(list, { reset: true, query: { q: $("[data-q]", f).value } }, { manage: true, pageSize: 15,
    empty: "Nothing found. It may not be imported yet – check the status of its source on this page." });
  $("[data-q]", f).addEventListener("input", debounce(load));
  $("[data-close]", f).onclick = closeDialog;
  await load();
});

async function refreshSources(withFiles = false) {
  conns = await getJSON("/api/connectors");
  overviewNow = await getJSON("/api/overview");
  const o = overviewNow;
  try {
    const up = await api("/api/documents?source=uploads&limit=1");
    drawTiles(Number(up.headers.get("X-Total-Count") || 0));
  } catch { drawTiles(0); }
  $("#wa-status").innerHTML = o.ready.whatsapp ? `${badge("ok", "Imported")} <span class="muted small">${plural(o.ready.whatsapp, "chat", "chats")} searchable – listed under Files and recordings below.</span>`
    : `${badge("", "No chats yet")}`;
  const importing = o.importing || conns.some((k) => k.syncing || !k.last_sync || k.more);
  $("#kb-summary").innerHTML = `<div class="toolbar"><h3>Searchable now</h3>${o.total ? '<a class="btn primary small" href="#ask">Ask a question</a>' : ""}</div>
    ${o.total ? `<div class="chips">${knowledgeChips(o.ready)}</div>` : '<p class="muted">Nothing yet. Connect an account, add your website, or upload files below.</p>'}
    ${importing ? `<p class="status-detail">${badge("busy", "Working")} ${o.importing ? `${plural(o.importing, "item is", "items are")} being prepared for search${o.eta ? ` (about ${esc(o.eta)} left)` : ""}.` : "Importing from your sources."} New items become searchable automatically – you can already ask questions.</p>` : ""}`;
  if (!editing) { drawProvider("google"); drawProvider("microsoft"); drawProvider("slack"); drawWebsites(); drawManual(); }
  if (withFiles || o.importing || refreshSources.wasImporting) await loadFiles(false);
  refreshSources.wasImporting = o.importing > 0;
  if (importing || conns.some((k) => k.indexing)) poll(() => refreshSources(), 4000);
}

function drawProvider(provider) {
  const card = $(`.provider-card[data-provider=${provider}]`);
  const types = Object.values(PROVIDER_TYPES[provider]);
  const accounts = {};
  conns.filter((k) => types.includes(k.type)).forEach((k) => { (accounts[k.config.account_id] ||= []).push(k); });
  const has = Object.keys(accounts).length > 0;

  const noun = PROVIDER_ACCOUNT[provider];
  $(".pc-connect", card).innerHTML = `<button class="btn ${has ? "" : "primary"}" data-open-connect>${has ? ic("plus") : ""}${has ? `Connect another ${PROVIDER_NAME[provider]} ${noun}` : `Connect ${PROVIDER_NAME[provider]} ${noun}`}</button>`;
  $("[data-open-connect]", card).onclick = () => oauthState.configured[provider] ? openConnectPanel(provider) : setupNeededDialog(provider);

  $(".pc-accounts", card).innerHTML = Object.entries(accounts).map(([accountId, list]) => {
    const first = list[0];
    const anyBusy = list.some((k) => k.syncing);
    const rows = Object.entries(PROVIDER_TYPES[provider]).map(([svc, type]) => {
      const k = list.find((x) => x.type === type);
      const [icon, label] = SERVICE_INFO[type];
      const name = `<div class="svc-name"><span class="icon-tile sm">${ic(icon)}</span>${label}</div>`;
      if (!k) return `<div class="svc">${name}<div class="status-line">${badge("", "Not connected")}</div>
        <div><button class="btn small" data-add-svc="${svc}" data-account="${accountId}">${ic("plus")}Connect</button></div></div>`;
      return `<div class="svc">${name}<div>${sourceStatus(k)}</div>
        <div>${k.documents ? `<button class="btn link" data-items="${k.id}">View items</button>` : ""}</div></div>`;
    }).join("");
    return `<div class="account">
      <div class="acc-head">
        <div><b dir="auto">${esc(first.config.username || "Connected account")}</b>
          <div class="muted small">Who can find this: ${visibilityPills(first)} <button class="btn link small" data-edit-vis="${accountId}">Change</button></div></div>
        <div class="acc-actions">
          <button class="btn small" data-sync-account="${accountId}" ${anyBusy ? "disabled" : ""}>${anyBusy ? '<span class="spinner"></span>Importing' : `${ic("refresh")}Check for new items`}</button>
          <button class="btn link danger" data-disconnect="${accountId}">Disconnect</button>
        </div>
      </div>${rows}<div class="vis-slot"></div></div>`;
  }).join("");

  $$("[data-items]", card).forEach((b) => b.onclick = () => itemsDialog(conns.find((k) => k.id === Number(b.dataset.items))));
  $$("[data-edit-vis]", card).forEach((b) => b.onclick = () => {
    const acc = b.closest(".account"); const list = accounts[b.dataset.editVis];
    editing = true;
    $(".vis-slot", acc).innerHTML = `<div class="panel" style="margin:0 16px 16px">${visibilityPicker(list[0].departments.map((d) => d.id), list[0].company_wide)}
      <div class="row"><button class="btn primary" data-save-vis>Save</button><button class="btn" data-cancel-vis>Cancel</button></div><p class="error"></p></div>`;
    $("[data-cancel-vis]", acc).onclick = () => { editing = false; refreshSources(); };
    $("[data-save-vis]", acc).onclick = (ev) => busy(ev.target, "Saving…", async () => {
      const v = readVisibility($(".panel", acc));
      const err = visibilityError(v);
      if (err) { $(".panel .error", acc).textContent = err; return; }
      for (const k of list) await api(`/api/connectors/${k.id}`, { method: "PUT", json: { type: k.type, name: k.name, config: {}, enabled: k.enabled, ...v } });
      editing = false; toast("Saved. This applies to everything already imported."); refreshSources();
    });
  });
  $$("[data-sync-account]", card).forEach((b) => b.onclick = async () => {
    b.disabled = true; b.innerHTML = '<span class="spinner"></span>Importing';
    for (const k of accounts[b.dataset.syncAccount]) await api(`/api/connectors/${k.id}/sync`, { method: "POST" });
    setTimeout(() => refreshSources(), 800);
  });
  $$("[data-disconnect]", card).forEach((b) => b.onclick = async () => {
    const list = accounts[b.dataset.disconnect];
    const ok = await confirmBox({ title: `Disconnect ${list[0].config.username || "this " + noun}?`, confirm: "Disconnect", danger: true,
      text: `Everything imported from this ${noun} will be removed from KnowledgeDesk, and its sign-in will be forgotten. Nothing is deleted in the ${noun} itself.` });
    if (!ok) return;
    await busy(b, "Disconnecting…", async () => {
      try { for (const k of list) await api(`/api/connectors/${k.id}`, { method: "DELETE" }); toast("Disconnected."); } catch (err) { toast(err.message, true); }
    });
    refreshSources();
  });
  $$("[data-add-svc]", card).forEach((b) => b.onclick = () => {
    const list = accounts[b.dataset.account];
    startSignIn(provider, [b.dataset.addSvc], { department_ids: list[0].departments.map((d) => d.id), company_wide: list[0].company_wide }, b);
  });
}

function setupNeededDialog(provider) {
  const f = openDialog(`<h2>${PROVIDER_NAME[provider]} sign-in isn't set up yet</h2>
    <p class="muted">To connect ${PROVIDER_NAME[provider]} ${PROVIDER_ACCOUNT[provider]}s with one click, ${PROVIDER_NAME[provider]} needs to know about this installation.
      It's a one-time setup (about 30 minutes, free) done by whoever installed KnowledgeDesk – see "Setting up one-click sign-in" in the README.</p>
    <p class="small">Return address to register:<br><code>${esc(oauthState.redirect_url)}</code></p>
    <div class="dialog-actions"><button class="btn primary" type="button" data-close>OK</button></div>`);
  $("[data-close]", f).onclick = closeDialog;
}

function openConnectPanel(provider) {
  editing = true;
  const slot = $(`.provider-card[data-provider=${provider}] .pc-panel`);
  slot.innerHTML = `<div class="panel">
    <span class="label">What should KnowledgeDesk read?</span>
    ${PROVIDER_SERVICES[provider].map(([value, label, hint]) => `<label class="check"><input type="checkbox" name="svc" value="${value}" checked> ${label}
      ${hint ? `<span class="muted small">(${hint})</span>` : ""}</label>`).join("<br>")}
    ${visibilityPicker([], false)}
    <p class="muted small">You'll sign in on ${PROVIDER_NAME[provider]}'s own page. KnowledgeDesk only reads – it never sends, posts, changes or deletes anything.
      ${provider === "google" ? "If Google says the app isn't verified, click <b>Continue</b>, then tick <b>all</b> the boxes on the next page." : ""}</p>
    <div class="row"><button class="btn primary" data-go>Continue to ${PROVIDER_NAME[provider]}</button><button class="btn" data-cancel>Cancel</button></div>
    <p class="error" role="alert"></p></div>`;
  $("[data-cancel]", slot).onclick = () => { editing = false; slot.innerHTML = ""; refreshSources(); };
  $("[data-go]", slot).onclick = (e) => {
    const services = $$("input[name=svc]:checked", slot).map((x) => x.value);
    const v = readVisibility(slot);
    const err = !services.length ? "Tick at least one thing to read." : visibilityError(v);
    if (err) { $(".error", slot).textContent = err; return; }
    startSignIn(provider, services, v, e.target, $(".error", slot));
  };
}
async function startSignIn(provider, services, visibility, button, errorEl) {
  const old = button.innerHTML;
  button.disabled = true; button.innerHTML = `<span class="spinner"></span>Opening ${PROVIDER_NAME[provider]}…`;
  try {
    const r = await (await api("/api/oauth/start", { method: "POST", json: { provider, services, ...visibility } })).json();
    window.location.href = r.url;
  } catch (err) {
    button.disabled = false; button.innerHTML = old;
    if (errorEl) errorEl.textContent = err.message; else toast(err.message, true);
  }
}

async function itemsDialog(k) {
  const [icon, label] = SERVICE_INFO[k.type];
  const f = openDialog(`<h2>${ic(icon)} ${esc(label)}</h2>
    <p class="muted small">${k.config.username ? esc(k.config.username) + " · " : ""}What was imported, newest first.</p>
    <div class="search-box" style="width:100%">${ic("search")}<input type="search" placeholder="Search" data-q dir="auto"></div>
    <div data-list style="margin-top:8px"></div>
    <div class="dialog-actions"><button class="btn" type="button" data-close>Close</button></div>`);
  const list = $("[data-list]", f);
  const load = () => pagedDocs(list, { reset: true, query: { connector_id: k.id, q: $("[data-q]", f).value } }, { pageSize: 15, empty: "Nothing found." });
  $("[data-q]", f).addEventListener("input", debounce(load));
  $("[data-close]", f).onclick = closeDialog;
  await load();
}

// ---------- Websites
function drawWebsites() {
  const sites = conns.filter((k) => k.type === "website");
  $("#web-list").innerHTML = sites.map((k) => `<div class="svc">
      <div class="svc-name" dir="ltr">${esc((k.config.url || "").replace(/^https?:\/\//, "").replace(/\/$/, ""))}</div>
      <div>${sourceStatus(k)}<div class="status-detail">Who can find it: ${visibilityPills(k)}</div></div>
      <div class="acc-actions">${k.documents ? `<button class="btn link" data-items="${k.id}">View pages</button>` : ""}
        <button class="btn link" data-web-sync="${k.id}" ${k.syncing ? "disabled" : ""}>Check now</button>
        <button class="btn link danger" data-web-del="${k.id}">Remove</button></div></div>`).join("");
  $$("#web-list [data-items]").forEach((b) => b.onclick = () => itemsDialog(conns.find((k) => k.id === Number(b.dataset.items))));
  $$("[data-web-sync]").forEach((b) => b.onclick = async () => { b.disabled = true; await api(`/api/connectors/${b.dataset.webSync}/sync`, { method: "POST" }); toast("Checking the website."); setTimeout(() => refreshSources(), 800); });
  $$("[data-web-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmBox({ title: "Remove this website?", text: "Its pages will no longer be searchable.", confirm: "Remove", danger: true })) return;
    await api(`/api/connectors/${b.dataset.webDel}`, { method: "DELETE" }); toast("Website removed."); refreshSources();
  });
}
$("#web-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const url = e.target.url.value.trim();
  const v = readVisibility($("#web-visibility"));
  $("#web-error").textContent = "";
  if (!url) { $("#web-error").textContent = "Type the website address first, for example www.yourcompany.com."; return; }
  const err = visibilityError(v);
  if (err) { $("#web-error").textContent = err; return; }
  busy($("button", e.target), "Checking the website…", async () => {
    try {
      await api("/api/connectors", { method: "POST", json: { type: "website", name: url, config: { url }, ...v } });
      e.target.reset(); toast("Website added. Reading all its pages now."); refreshSources();
    } catch (err2) { $("#web-error").textContent = err2.message; }
  });
});

// ---------- Files and recordings
const uploadForm = $("#upload-form");
$("#wa-upload").addEventListener("click", () => {
  const radio = $("input[name=kind][value=whatsapp]", uploadForm);
  radio.checked = true; radio.dispatchEvent(new Event("change"));
  $("#files-card").scrollIntoView({ behavior: "smooth", block: "start" });
  setTimeout(() => $("#dropzone input").click(), 400);
});
function uploadKind() { return $("input[name=kind]:checked", uploadForm).value; }
$$("input[name=kind]", uploadForm).forEach((r) => r.addEventListener("change", () => {
  $$(".kind-help", uploadForm).forEach((p) => p.classList.toggle("hidden", p.dataset.kind !== uploadKind()));
  $(".interview-fields", uploadForm).classList.toggle("hidden", uploadKind() !== "interview");
}));
const dz = $("#dropzone"); const fileInput = $("input[type=file]", dz);
function showChosen() { $(".dz-files", dz).textContent = [...fileInput.files].map((f) => f.name).join(", "); }
fileInput.addEventListener("change", showChosen);
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, () => dz.classList.remove("drag")));
dz.addEventListener("drop", (e) => { e.preventDefault(); fileInput.files = e.dataTransfer.files; showChosen(); });

uploadForm.addEventListener("submit", (e) => {
  e.preventDefault();
  $("#upload-error").textContent = "";
  const files = [...fileInput.files];
  const v = readVisibility($("#upload-visibility"));
  const err = !files.length ? "Choose at least one file." : visibilityError(v);
  if (err) { $("#upload-error").textContent = err; return; }
  const body = new FormData();
  files.forEach((f) => body.append("files", f));
  body.append("department_ids", v.department_ids.join(","));
  body.append("company_wide", v.company_wide ? "true" : "false");
  body.append("kind", uploadKind());
  for (const k of ["interviewee", "role", "interview_date"]) body.append(k, uploadForm[k].value || "");
  busy($("button[type=submit]", uploadForm), "Uploading…", async () => {
    try {
      await api("/api/documents", { method: "POST", body });
      fileInput.value = ""; showChosen(); ["interviewee", "role", "interview_date"].forEach((k) => (uploadForm[k].value = ""));
      $("#upload-visibility").innerHTML = visibilityPicker([], false);
      toast(`Uploaded ${plural(files.length, "file", "files")}. ${uploadKind() === "interview" ? "Recordings take a few minutes to turn into text." : "They'll be searchable in a moment."}`);
      await loadFiles(true); refreshSources();
    } catch (err2) { $("#upload-error").textContent = err2.message; }
  });
});
async function loadFiles(reset) {
  await pagedDocs($("#files-list"), { reset, query: { source: "uploads", q: $("#files-search").value } },
    { manage: true, pageSize: 10, empty: "No files uploaded yet." });
}
$("#files-search").addEventListener("input", debounce(() => loadFiles(true)));

// ---------- Other sources (IMAP mailbox, calendar link)
const connForm = $("#conn-form");
function connType() { return $("input[name=type]:checked", connForm).value; }
function showConnFields() {
  const t = connType();
  $$(".conn-fields", connForm).forEach((el) => { const on = el.dataset.for === t; el.classList.toggle("hidden", !on); $$("input", el).forEach((i) => (i.disabled = !on)); });
  $("#conn-test").classList.toggle("hidden", t !== "imap");
}
$$("input[name=type]", connForm).forEach((r) => r.addEventListener("change", () => { $("#conn-error").textContent = ""; $("#imap-folders").textContent = ""; showConnFields(); }));
function connBody() {
  const t = connType(); const config = {};
  $$(`.conn-fields[data-for=${t}] input`, connForm).forEach((i) => (config[i.name] = i.value));
  const name = config.name || config.username || (t === "ics" ? "Calendar" : "Mailbox"); delete config.name;
  return { type: t, name, config, enabled: true, ...readVisibility($("#conn-visibility")) };
}
function resetConnForm() {
  connForm.reset(); connForm.id.value = "";
  $("#conn-types").classList.remove("hidden"); $("#conn-form-title").classList.add("hidden"); $("#conn-cancel").classList.add("hidden");
  $("#conn-error").textContent = ""; $("#imap-folders").textContent = "";
  $$("input[name=password], .conn-fields[data-for=ics] input[name=url]", connForm).forEach((i) => (i.placeholder = ""));
  $("#conn-visibility").innerHTML = visibilityPicker([], false);
  showConnFields();
}
function drawManual() {
  const list = conns.filter((k) => k.type === "imap" || k.type === "ics");
  $("#manual-list").innerHTML = list.length ? list.map((k) => `<div class="svc">
      <div class="svc-name"><span class="icon-tile sm">${ic(SERVICE_INFO[k.type][0])}</span>${esc(k.name)}</div>
      <div>${sourceStatus(k)}<div class="status-detail">Who can find it: ${visibilityPills(k)}</div></div>
      <div class="acc-actions">${k.documents ? `<button class="btn link" data-items="${k.id}">View items</button>` : ""}
        <button class="btn link" data-m-edit="${k.id}">Edit</button><button class="btn link danger" data-m-del="${k.id}">Remove</button></div></div>`).join("") + "<hr>" : "";
  if (list.length) $("#more-sources").open = true;
  $$("#manual-list [data-items]").forEach((b) => b.onclick = () => itemsDialog(conns.find((k) => k.id === Number(b.dataset.items))));
  $$("[data-m-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmBox({ title: "Remove this source?", text: "Everything imported from it will no longer be searchable.", confirm: "Remove", danger: true })) return;
    await api(`/api/connectors/${b.dataset.mDel}`, { method: "DELETE" }); toast("Removed."); refreshSources();
  });
  $$("[data-m-edit]").forEach((b) => b.onclick = () => {
    const k = conns.find((x) => x.id === Number(b.dataset.mEdit));
    resetConnForm(); editing = true;
    connForm.id.value = k.id;
    $(`input[name=type][value=${k.type}]`, connForm).checked = true; showConnFields();
    $("#conn-types").classList.add("hidden");
    $("#conn-form-title").textContent = `Edit ${k.name}`; $("#conn-form-title").classList.remove("hidden");
    const fields = $(`.conn-fields[data-for=${k.type}]`, connForm);
    $("input[name=name]", fields).value = k.name;
    for (const [key, val] of Object.entries(k.config)) { const i = $(`input[name=${key}]`, fields); if (i && val) i.value = val; }
    $$("input[name=password], input[name=url]", fields).forEach((i) => (i.placeholder = "Leave empty to keep the saved one"));
    $("#conn-visibility").innerHTML = visibilityPicker(k.departments.map((d) => d.id), k.company_wide);
    $("#conn-cancel").classList.remove("hidden");
    connForm.scrollIntoView({ behavior: "smooth" });
  });
}
$("#conn-cancel").addEventListener("click", () => { editing = false; resetConnForm(); refreshSources(); });
$("#conn-test").addEventListener("click", (e) => busy(e.target, "Testing…", async () => {
  $("#conn-error").textContent = "";
  try {
    const r = await (await api("/api/connectors/test", { method: "POST", json: { ...connBody(), company_wide: true } })).json();
    toast("Connected successfully.");
    if (r.folders.length) $("#imap-folders").textContent = "Folders in this mailbox: " + r.folders.join(", ");
  } catch (err) { $("#conn-error").textContent = err.message; }
}));
connForm.addEventListener("submit", (e) => {
  e.preventDefault();
  $("#conn-error").textContent = "";
  const body = connBody();
  const err = visibilityError(body);
  if (err) { $("#conn-error").textContent = err; return; }
  busy($("button[type=submit]", connForm), "Connecting…", async () => {
    try {
      const id = connForm.id.value;
      await api(id ? `/api/connectors/${id}` : "/api/connectors", { method: id ? "PUT" : "POST", json: body });
      editing = false; resetConnForm(); toast("Saved. Importing now."); refreshSources();
    } catch (err2) { $("#conn-error").textContent = err2.message; }
  });
});

// ================================================================== People
LOADERS.people = async function loadPeople() {
  await refreshDepartments();
  $("#dept-chips").innerHTML = departments.length ? departments.map((d) => `<span class="stat-chip"><b>${esc(d.name)}</b>
      <span class="muted">${plural(d.users, "person", "people")}</span>
      <button class="btn link danger small" data-del-dept="${d.id}" data-name="${esc(d.name)}" aria-label="Delete ${esc(d.name)}" title="Delete">${ic("x")}</button></span>`).join("")
    : '<p class="muted">No departments yet. Without departments, everything is visible to everyone in the company.</p>';
  $$("[data-del-dept]").forEach((b) => b.onclick = async () => {
    if (!await confirmBox({ title: `Delete the ${b.dataset.name} department?`, confirm: "Delete", danger: true,
      text: "People and information stay, but they will no longer be grouped under this department. Information that was only visible to this department will be visible to admins only." })) return;
    await api(`/api/departments/${b.dataset.delDept}`, { method: "DELETE" }); toast("Department deleted."); LOADERS.people();
  });
  const users = await getJSON("/api/users");
  $("#user-list").innerHTML = users.map((u) => `<div class="list-row"><div class="row tight">
      <span class="avatar">${esc(initials(u.name || u.email))}</span><div>
      <div class="title">${esc(u.name || u.email)} ${u.role === "admin" ? badge("info", "Admin") : ""} ${u.active ? "" : badge("", "Disabled")}</div>
      <div class="meta">${esc(u.email)} · ${u.role === "admin" ? "Sees everything" : (u.departments.map((d) => `<span class="pill">${esc(d.name)}</span>`).join("") || "Only information shared with everyone")}</div></div></div>
      <div class="actions"><button class="btn link" data-edit-user="${u.id}">Edit</button></div></div>`).join("");
  $$("[data-edit-user]").forEach((b) => b.onclick = () => userDialog(users.find((u) => u.id === Number(b.dataset.editUser))));
};
$("#dept-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("#dept-error").textContent = "";
  const name = e.target.name.value.trim();
  if (!name) { $("#dept-error").textContent = "Type a department name."; return; }
  busy($("button", e.target), "Adding…", async () => {
    try { await api("/api/departments", { method: "POST", json: { name } }); e.target.reset(); toast(`${name} added.`); LOADERS.people(); }
    catch (err) { $("#dept-error").textContent = err.message; }
  });
});
$("#add-user").addEventListener("click", () => userDialog(null));
function userDialog(u) {
  const f = openDialog(`<h2>${u ? "Edit " + esc(u.name || u.email) : "Add a person"}</h2>
    <label>Full name <input name="name" value="${esc(u?.name || "")}" dir="auto" required></label>
    <label>Email (used to log in) <input name="email" type="email" value="${esc(u?.email || "")}" required></label>
    <label>${u ? "New password (leave empty to keep the current one)" : "Password (at least 8 characters)"}
      <input name="password" type="text" autocomplete="off" ${u ? "" : "required minlength=8"}></label>
    <label>Role <select name="role"><option value="member">Team member – finds information for their departments</option>
      <option value="admin">Admin – sees everything and manages KnowledgeDesk</option></select></label>
    <span class="label">Departments</span>
    <div>${departments.map((d) => `<label class="check"><input type="checkbox" name="dept" value="${d.id}" ${u?.departments.some((x) => x.id === d.id) ? "checked" : ""}> ${esc(d.name)}</label>`).join("") || '<span class="muted small">No departments yet.</span>'}</div>
    ${u ? `<label class="check" style="margin-top:8px"><input type="checkbox" name="active" ${u.active ? "checked" : ""}> Can log in</label>` : ""}
    <p class="error" role="alert"></p>
    <div class="dialog-actions"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="submit">${u ? "Save" : "Add person"}</button></div>`);
  f.role.value = u?.role || "member";
  if (!u) f.password.value = Math.random().toString(36).slice(2, 7) + "-" + Math.random().toString(36).slice(2, 7);
  $("[data-close]", f).onclick = closeDialog;
  f.onsubmit = (e) => {
    e.preventDefault();
    const body = { name: f.name.value.trim(), email: f.email.value.trim(), password: f.password.value || null, role: f.role.value,
      active: u ? f.active.checked : true, department_ids: $$("input[name=dept]:checked", f).map((x) => Number(x.value)) };
    if (!body.name || !body.email) { $(".error", f).textContent = "Name and email are required."; return; }
    if (body.password && body.password.length < 8) { $(".error", f).textContent = "The password must be at least 8 characters."; return; }
    busy($("button[type=submit]", f), "Saving…", async () => {
      try {
        await api(u ? `/api/users/${u.id}` : "/api/users", { method: u ? "PUT" : "POST", json: body });
        if (u) { closeDialog(); toast("Saved."); LOADERS.people(); return; }
        const details = `KnowledgeDesk – ${appStatus.company || "our company"}\nAddress: ${location.origin}\nEmail: ${body.email}\nPassword: ${body.password}`;
        const d = openDialog(`<h2>${esc(body.name)} was added</h2>
          <p class="muted">Send them these login details. They can change the password after logging in (click their name at the bottom left).</p>
          <textarea rows="4" readonly data-details>${esc(details)}</textarea>
          <div class="dialog-actions"><button class="btn" type="button" data-copy>Copy</button><button class="btn primary" type="button" data-done>Done</button></div>`);
        $("[data-copy]", d).onclick = async () => {
          try { await navigator.clipboard.writeText(details); toast("Copied."); }
          catch { $("[data-details]", d).select(); document.execCommand("copy"); toast("Copied."); }
        };
        $("[data-done]", d).onclick = closeDialog;
        LOADERS.people();
      } catch (err) { $(".error", f).textContent = err.message; }
    });
  };
}

// ================================================================== Activity
let qData = null;
LOADERS.activity = async function loadActivity() { qData = await getJSON("/api/admin/questions"); drawQuestions(); };
function drawQuestions() {
  const s = qData.stats;
  $("#q-stats").innerHTML = [["Asked in total", s.total], ["This week", s.last_7_days], ["Helpful", s.helpful], ["Not helpful", s.not_helpful], ["No answer found", s.no_sources]]
    .map(([l, v]) => `<div class="stat"><div class="label">${l}</div><div class="big-number">${num(v)}</div></div>`).join("");
  const gaps = $("#q-gaps").checked;
  const list = qData.questions.filter((q) => !gaps || !q.found_sources || q.feedback === -1);
  $("#q-list").innerHTML = list.map((q) => `<div class="list-row"><div>
      <details><summary class="title" dir="auto">${esc(q.question)}</summary><div class="muted small" style="white-space:pre-wrap;margin-top:6px" dir="auto">${esc(q.answer || "")}</div></details>
      <div class="meta">${esc(q.user_name || q.user_email || "")} · ${new Date(q.created_at).toLocaleString()}</div></div>
      <div>${!q.found_sources ? badge("bad", "No answer found") : q.feedback === -1 ? badge("bad", "Not helpful") : q.feedback === 1 ? badge("ok", "Helpful") : ""}</div></div>`).join("")
    || `<div class="empty">${gaps ? "No unanswered questions." : "No questions yet."}</div>`;
}
$("#q-gaps").addEventListener("change", drawQuestions);

// ================================================================== Settings
let lastHealth = null;
LOADERS.settings = async function loadSettings() {
  await Promise.all([drawHealth(), drawAiKey()]);
  const st = await (await fetch("/api/status")).json();
  const l = st.license;
  $("#license-info").innerHTML = l.state === "unchecked" ? '<p class="muted">Licensing is not configured on this installation.</p>'
    : `<p>${badge(l.state === "valid" ? "ok" : l.state === "grace" ? "busy" : "bad", l.state)}
       ${l.customer ? esc(l.customer) : ""}${l.expires ? ` · valid until ${esc(l.expires)}` : ""}${l.max_users ? ` · up to ${l.max_users} people` : ""}</p>
       ${l.message ? `<p class="muted">${esc(l.message)}</p>` : ""}`;
};
const HEALTH_ICON = { ok: "check", busy: "clock", warn: "alert", bad: "error", info: "info" };
async function drawHealth() {
  $("#health-list").innerHTML = '<div class="row tight muted"><span class="spinner"></span> Checking…</div>';
  lastHealth = await getJSON("/api/admin/health");
  $("#health-list").innerHTML = lastHealth.checks.map((c) => `<div class="check-row"><span class="${c.state}">${ic(HEALTH_ICON[c.state])}</span>
      <b>${esc(c.label)}</b><span class="${["ok", "info"].includes(c.state) ? "" : "state-text " + (c.state === "warn" ? "busy" : c.state)}">${esc(c.detail)}</span></div>`).join("");
  $("#problem-list").innerHTML = lastHealth.problems.length ? lastHealth.problems.map((p) =>
    `<div class="problem"><span class="muted">${new Date(p.time).toLocaleString()} · ${esc(p.where)}</span><br>${esc(p.message)}</div>`).join("")
    : '<p class="muted small">No problems recorded since the last restart.</p>';
  if (lastHealth.checks.some((c) => c.state === "busy")) poll(drawHealth, 10000);
}
async function drawAiKey() {
  const k = await getJSON("/api/admin/ai-key");
  const state = !k.set ? badge("bad", "Not set") : k.check.state === "ok" ? badge("ok", "Working") : badge("bad", "Not working");
  $("#ai-state").innerHTML = `${state} ${k.masked ? `<code>${esc(k.masked)}</code>` : ""}`;
}
$("#ai-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("#ai-error").textContent = "";
  const key = e.target.key.value.trim();
  if (!key) { $("#ai-error").textContent = "Paste the key first."; return; }
  busy($("button", e.target), "Testing…", async () => {
    try {
      await api("/api/admin/ai-key", { method: "PUT", json: { key } });
      e.target.reset(); toast("Key saved. Answers are on.");
      appStatus.llm_configured = true; updateBanner();
      await Promise.all([drawAiKey(), drawHealth()]);
    } catch (err) { $("#ai-error").textContent = err.message; }
  });
});
$("#health-refresh").addEventListener("click", (e) => busy(e.target, "Checking…", drawHealth));
$("#health-download").addEventListener("click", () => {
  const report = { generated: new Date().toISOString(), company: appStatus.company, browser: navigator.userAgent, ...lastHealth };
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" })),
    download: `knowledgedesk-health-${new Date().toISOString().slice(0, 10)}.json` });
  a.click();
});
$("#license-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("#license-error").textContent = "";
  const body = new FormData(); body.append("file", e.target.file.files[0]);
  busy($("button", e.target), "Installing…", async () => {
    try { await api("/api/admin/license", { method: "POST", body }); e.target.reset(); toast("License installed."); LOADERS.settings(); }
    catch (err) { $("#license-error").textContent = err.message; }
  });
});

boot();
