// Cortex web interface. Plain JavaScript, no build step.
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
  catch { throw new Error("Can't reach Cortex. Check that it's running, then try again."); }
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
// Who can find something: Only me, some departments (my teams), or everyone in the company
function visibilityPicker(selected = [], everyone = false, title = "Who can find this?", { only = false } = {}) {
  const mine = me.role === "admin" ? departments : me.departments;   // team members share with their own teams
  const boxes = mine.map((d) => `<label class="check"><input type="checkbox" value="${d.id}" ${!only && selected.includes(d.id) ? "checked" : ""}> ${esc(d.name)}</label>`).join("");
  return `<div class="vis-block"><span class="label">${esc(title)}</span>
    <div class="circles">
      <label class="circle"><input type="checkbox" class="vis-private" ${only ? "checked" : ""}><span>${ic("lock")}<b>Only me</b><small>Private: only your questions use it</small></span></label>
      <label class="circle"><input type="checkbox" class="vis-everyone" ${everyone && !only ? "checked" : ""}><span>${ic("globe")}<b>Everyone</b><small>The whole company</small></span></label>
    </div>
    ${boxes ? `<div class="muted small">or only ${me.role === "admin" ? "these teams" : "your teams"}:</div><div class="vis-depts ${everyone || only ? "off" : ""}">${boxes}</div>` :
      `<div class="muted small">${me.role === "admin" ? "To share with certain teams, first create departments under People." : ""}</div>`}</div>`;
}
document.addEventListener("change", (e) => {
  const block = e.target.closest?.(".vis-block");
  if (!block) return;
  const everyone = $(".vis-everyone", block), priv = $(".vis-private", block);
  const depts = $$(".vis-depts input", block);
  if (e.target.checked) {   // the three circles exclude each other
    if (e.target === priv) { everyone.checked = false; depts.forEach((d) => (d.checked = false)); }
    else if (e.target === everyone) { priv.checked = false; depts.forEach((d) => (d.checked = false)); }
    else { priv.checked = false; everyone.checked = false; }
  }
  $(".vis-depts", block)?.classList.toggle("off", everyone.checked || priv.checked);
  const err = block.closest(".panel, form, #dialog-form")?.querySelector(".error");   // a choice was made: the warning is answered
  if (err && /Choose who can find it/.test(err.textContent)) err.textContent = "";
});
function readVisibility(root) {
  const el = root.classList?.contains("vis-block") ? root : $(".vis-block", root);
  const priv = $(".vis-private", el).checked;
  return { private: priv, company_wide: !priv && $(".vis-everyone", el).checked,
    department_ids: priv ? [] : $$(".vis-depts input:checked", el).map((x) => Number(x.value)) };
}
function visibilityError(v) { return !v.private && !v.company_wide && !v.department_ids.length ? "Choose who can find it: only you, a team, or everyone." : ""; }
function visibilityPills(item) {
  if (item.private) return `<span class="pill pill-private">${ic("lock")}Only you</span>`;
  if (item.company_wide) return '<span class="pill">Everyone</span>';
  return (item.departments || []).map((d) => `<span class="pill">${esc(d.name)}</span>`).join("") || '<span class="pill">Admins only</span>';
}
async function refreshDepartments() { departments = await getJSON("/api/departments"); }

// ---------- what can be searched
const KIND_ICON = { file: "file", documents: "file", interview: "mic", whatsapp: "chat", email: "mail", calendar: "calendar", web: "globe", slack: "ask", social: "share" };
const KIND_NAMES = { email: ["email", "emails"], documents: ["document", "documents"], calendar: ["calendar event", "calendar events"],
  slack: ["Slack message", "Slack messages"], web: ["web page", "web pages"], whatsapp: ["WhatsApp chat", "WhatsApp chats"], interview: ["interview", "interviews"],
  social: ["social media post", "social media posts"] };
const kindName = (k) => (KIND_NAMES[k === "file" ? "documents" : k] || ["item"])[0];
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
// One sidebar that never changes: New chat, then the pages
const NAV = {
  admin: [["today", "Today", "home"], ["ask", "Chat", "ask"], ["brain", "Brain", "brain"], ["sources", "Sources", "sources"], ["people", "People", "people"],
    ["home", "Insights", "activity"], ["learning", "Learning Center", "learn"], ["health", "Health", "check"], ["settings", "Settings", "settings"]],
  member: [["today", "Today", "home"], ["ask", "Chat", "ask"], ["brain", "Brain", "brain"], ["mine", "My sources", "sources"], ["learning", "Learning Center", "learn"]],
  learner: [["learning", "Learning Center", "learn"], ["ask", "Chat", "ask"]],   // new employees: only their learning path
};
const HOME_PAGE = { admin: "today", member: "today", learner: "learning" };
const navFor = (u) => NAV[u.role] || NAV.member;
// every page a person may open (past chats open from the clock button in the chat)
const pagesFor = (u) => [...navFor(u), ["conversations", "Past chats", "chat"]];
function drawNav() {
  $("#nav").innerHTML = `<button type="button" class="nav-new" id="nav-new">${ic("plus")}<span>New chat</span></button>
    ${navFor(me).map(([id, label, icon]) => `<a href="#${id}" data-page="${id}">${ic(icon)}<span>${label}</span><b class="nav-count hidden" data-count="${id}"></b></a>`).join("")}`;
  $("#nav-new").onclick = () => { if (location.hash !== "#ask") location.hash = "#ask"; setTimeout(newChat, 0); };
  refreshTodayCount();
}
const ROLE_LABEL = { admin: "Admin", learner: "New employee" };
const LOADERS = {};

function showView(id) { $$("body > section").forEach((s) => s.classList.add("hidden")); $(id).classList.remove("hidden"); }
// Logging out reloads the page, so nothing of the previous person (their conversation, lists) stays on screen.
function logout() { token = null; try { localStorage.removeItem("kd_token"); } catch {} me = null; location.replace("/app"); }
// The person's first name: their name, or. If none was set. A guess from their email (yaniv.b@… → Yaniv).
function firstName(user) {
  const name = (user.name || "").trim();
  if (name && name.toLowerCase() !== "administrator") return name.split(/\s+/)[0];
  const local = (user.email || "").split("@")[0].split(/[._\-+0-9]/).find((p) => p.length > 1) || "";
  return local ? local[0].toUpperCase() + local.slice(1).toLowerCase() : "";
}
const hasRealName = (user) => (user.name || "").trim() && user.name.trim().toLowerCase() !== "administrator";
function initials(name) { return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join(""); }

async function boot() {
  try { appStatus = await (await fetch("/api/status")).json(); } catch { appStatus = { company: "", license: {} }; }
  $("#login-company").textContent = appStatus.company || "";
  drawBrand();
  $("#app-version").textContent = appStatus.version ? `Version ${appStatus.version}` : "";
  if (!token) return showView("#login-view");
  try { me = await getJSON("/api/me"); } catch { return showView("#login-view"); }
  await refreshDepartments();
  drawMe();
  drawNav();
  updateBanner();
  showView("#app-view");
  const connected = new URLSearchParams(location.search).get("connected");
  if (connected !== null) {
    window.history.replaceState(null, "", me.role === "admin" ? "/app#sources" : "/app#mine");
    toast("Account connected. Importing now. You can follow the progress on this page.");
  }
  route();
}
function drawMe() {
  const shown = hasRealName(me) ? me.name : (firstName(me) || me.email);
  $("#user-name").innerHTML = `<span class="avatar">${esc(initials(shown))}</span>
    <span class="who"><b>${esc(shown)}</b><span>${ROLE_LABEL[me.role] || esc(me.email)}</span></span>`;
}
function drawBrand() {
  $("#company-name").textContent = appStatus.company || "";
  const logo = $("#company-logo");
  logo.classList.toggle("hidden", !appStatus.logo);
  if (appStatus.logo) { logo.src = appStatus.logo; logo.alt = appStatus.company || "Company logo"; logo.className = `tone-${appStatus.logo_tone || "dark"}`; }
  const link = $("#company-link");
  if (appStatus.website) link.href = appStatus.website; else link.removeAttribute("href");
}
function updateBanner() {
  const lic = appStatus.license || {};
  const banner = $("#banner");
  let html = "";
  if (!appStatus.llm_configured && me?.role === "admin") html = `${ic("alert")}<span>Answers are off until the AI service key is added. <a href="#health">Add it</a></span>`;
  else if (lic.message && lic.state !== "unchecked") html = `${ic("alert")}<span>${esc(lic.message)}</span>`;
  banner.innerHTML = html; banner.classList.toggle("hidden", !html);
}

function route() {
  if (!me) return;
  clearTimeout(pagePoll);
  const pages = pagesFor(me);
  let name = location.hash.slice(1);
  if (!pages.some(([id]) => id === name)) name = HOME_PAGE[me.role] || "ask";
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
  const f = openDialog(`<h2>My account</h2><p class="muted">${esc(me.email)}</p>
    <label>Your name <input name="name" dir="auto" value="${esc(hasRealName(me) ? me.name : firstName(me))}" required></label>
    <div class="row tight" style="margin-bottom:16px"><button class="btn" type="button" data-save-name>Save name</button></div>
    <span class="label">Change password</span>
    <label>Current password <input name="current" type="password" autocomplete="current-password"></label>
    <label>New password (at least 8 characters) <input name="next" type="password" autocomplete="new-password" minlength="8"></label>
    <p class="error" role="alert"></p>
    <div class="dialog-actions"><button class="btn" type="button" data-close>Close</button><button class="btn primary" type="submit">Change password</button></div>`);
  $("[data-close]", f).onclick = closeDialog;
  $("[data-save-name]", f).onclick = (e) => busy(e.target, "Saving…", async () => {
    try {
      await api("/api/me", { method: "PUT", json: { name: f.name.value } });
      me.name = f.name.value.trim(); drawMe(); toast("Name saved.");
      if (location.hash === "#today" || !location.hash) LOADERS.today?.();
    } catch (err) { $(".error", f).textContent = err.message; }
  });
  f.onsubmit = (e) => {
    e.preventDefault();
    if (!f.current.value || !f.next.value) { $(".error", f).textContent = "Fill in both password fields."; return; }
    busy($("button[type=submit]", f), "Saving…", async () => {
      try { await api("/api/me/password", { method: "POST", json: { current_password: f.current.value, new_password: f.next.value } }); closeDialog(); toast("Password changed."); }
      catch (err) { $(".error", f).textContent = err.message; }
    });
  };
});

// ================================================================== charts
const tip = Object.assign(document.createElement("div"), { id: "viz-tip" });
document.body.append(tip);
document.addEventListener("mouseover", (e) => {
  const hit = e.target.closest?.(".hit");
  document.querySelectorAll(".hover-band.on").forEach((b) => b.classList.remove("on"));
  if (hit) hit.closest("svg").querySelector(`[data-band="${hit.dataset.i}"]`)?.classList.add("on");
});
document.addEventListener("mousemove", (e) => {
  const t = e.target.closest?.("[data-tip]");
  if (!t) { tip.style.display = "none"; return; }
  tip.innerHTML = t.dataset.tip;
  tip.style.display = "block";
  const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  tip.style.left = `${x}px`; tip.style.top = `${e.clientY + 14}px`;
});
function niceMax(v) {
  if (v <= 4) return 4;
  const p = 10 ** Math.floor(Math.log10(v)); const m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
}
const shortDay = (d) => new Date(d + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });
// Columns, optionally stacked. series: [{ key, label, color }], bottom first.
function columnChart(rows, series, { height = 190, title = "" } = {}) {
  const W = 760, H = height, L = 34, R = 6, T = 8, B = 24;
  const totals = rows.map((r) => series.reduce((a, sr) => a + (r[sr.key] || 0), 0));
  const max = niceMax(Math.max(...totals, 1));
  const band = (W - L - R) / rows.length, bw = Math.max(2, Math.min(24, band * 0.62));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const ticks = [0, max / 2, max];
  const every = Math.ceil(rows.length / 8);
  let bars = "", hits = "", xs = "", bands = "";
  rows.forEach((r, i) => {
    const x = L + band * i + (band - bw) / 2;
    let base = 0;
    const live = series.filter((sr) => r[sr.key] > 0);
    live.forEach((sr, j) => {
      const v = r[sr.key];
      const y0 = y(base), y1 = y(base + v) + (j ? 2 : 0); // 2px surface gap between stacked segments
      const h = Math.max(0, y0 - y1);
      const top = j === live.length - 1, rr = Math.min(4, h, bw / 2);
      bars += top ? `<path d="M${x},${y0}V${y1 + rr}Q${x},${y1} ${x + rr},${y1}H${x + bw - rr}Q${x + bw},${y1} ${x + bw},${y1 + rr}V${y0}Z" fill="var(${sr.color})"/>`
        : `<rect x="${x}" y="${y1}" width="${bw}" height="${h}" fill="var(${sr.color})"/>`;
      base += v;
    });
    const tipHtml = `<b>${esc(shortDay(r.day))}</b>` + series.map((sr) => `<div><i style="background:var(${sr.color})"></i>${esc(sr.label)}: ${num(r[sr.key])}</div>`).join("");
    bands += `<rect class="hover-band" data-band="${i}" x="${L + band * i}" y="${T}" width="${band}" height="${H - T - B}"/>`;
    hits += `<rect class="hit" data-i="${i}" x="${L + band * i}" y="${T}" width="${band}" height="${H - T - B}" data-tip="${esc(tipHtml)}"/>`;
    // label every few days, counted back from today so the latest day is always labeled and labels never collide
    if ((rows.length - 1 - i) % every === 0) xs += `<text x="${L + band * i + band / 2}" y="${H - 6}" text-anchor="middle">${esc(shortDay(r.day))}</text>`;
  });
  const grid = ticks.map((t) => `<line x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/>`).join("");
  const yl = ticks.map((t) => `<text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${num(t)}</text>`).join("");
  const legend = series.length > 1 ? `<div class="legend">${series.map((sr) => `<span><i style="background:var(${sr.color})"></i>${esc(sr.label)}</span>`).join("")}</div>` : "";
  const table = `<details class="viz-table"><summary>View as table</summary><table><tr><th>Day</th>${series.map((sr) => `<th>${esc(sr.label)}</th>`).join("")}</tr>
    ${rows.map((r) => `<tr><td>${esc(shortDay(r.day))}</td>${series.map((sr) => `<td>${num(r[sr.key])}</td>`).join("")}</tr>`).join("")}</table></details>`;
  return `<div class="viz">${legend}<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}"><g class="grid">${grid}</g>
    <g class="axis">${yl}${xs}</g>${bands}${bars}${hits}</svg>${table}</div>`;
}
function hbarChart(rows, empty) {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const max = Math.max(...rows.map((r) => r.n), 1);
  return `<div class="viz"><div class="hbars">${rows.map((r) => `<span class="name" title="${esc(r.name)}">${esc(r.name)}</span>
    <span class="track"><span class="bar" style="display:block;width:${(r.n / max) * 100}%" data-tip="${esc(`<b>${esc(r.name)}</b>${plural(r.n, "question", "questions")}`)}"></span></span>
    <span class="val">${num(r.n)}</span>`).join("")}</div></div>`;
}

// ================================================================== Today: your meetings and what needs you
const ALERT_KIND = {
  reply: ["mail", "Draft reply"], send: ["send", "Draft email"], renew: ["refresh", "Review"], follow_up: ["people", "Draft follow up"],
  prepare: ["calendar", "Prepare me"], deadline: ["clock", "Help me"],
};
const timeOf = (d) => new Date(d).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const dayOf = (d) => new Date(d).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
// Start a new chat with this request, as if typed
function askInChat(prompt) {
  if (chat.busy) return toast("Wait for the current answer to finish.", true);
  chat.history = []; chat.cid = null; $("#messages").innerHTML = ""; leaveWelcome();
  location.hash = "#ask";
  setTimeout(() => { qBox.value = prompt; $("#ask-form").requestSubmit(); }, 0);
}
function setNavCount(page, n) {
  const el = $(`[data-count="${page}"]`);
  if (el) { el.textContent = n > 9 ? "9+" : String(n || ""); el.classList.toggle("hidden", !n); }
}
async function refreshTodayCount() {
  if (!me || me.role === "learner") return;
  try { const t = await getJSON(`/api/today?tz=${new Date().getTimezoneOffset()}`); setNavCount("today", t.alerts.length); } catch {}
}
function meetingPrompt(e) {
  const who = (e.attendees || []).slice(0, 8).join(", ");
  return `Prepare me for my meeting "${e.title}" ${e.all_day ? `on ${dayOf(e.starts_at)}` : `at ${timeOf(e.starts_at)} on ${dayOf(e.starts_at)}`}${who ? ` with ${who}` : ""}: `
    + "who they are, what we discussed with them before, what is still open, and what I should bring up.";
}
function meetingRow(e, withDay) {
  const when = e.all_day ? "All day" : timeOf(e.starts_at);
  const people = (e.attendees || []).length;
  return `<div class="meeting">
    <div class="m-time">${withDay ? `<span>${esc(dayOf(e.starts_at))}</span>` : ""}<b>${esc(when)}</b></div>
    <div class="grow"><div class="m-title" dir="auto">${esc(e.title)}</div>
      ${people || e.location ? `<div class="muted small">${[people ? plural(people, "person", "people") : "", e.location ? esc(e.location) : ""].filter(Boolean).join(" · ")}</div>` : ""}</div>
    <button type="button" class="btn small" data-prep="${e.id}">${ic("brain")}Prepare me</button></div>`;
}
let todayData = null;
LOADERS.today = async function loadToday() {
  const t = todayData = await getJSON(`/api/today?tz=${new Date().getTimezoneOffset()}`);
  $("#today-title").textContent = greeting();
  $("#today-date").textContent = new Date().toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  setNavCount("today", t.alerts.length);
  if (me.role === "admin") drawAdminNotices().catch(() => {});

  $("#today-alerts").innerHTML = t.alerts.length ? t.alerts.map((a) => {
    const [icon, label] = ALERT_KIND[a.kind] || ["info", "Help me"];
    return `<div class="alert-card" data-key="${esc(a.key)}">
      <span class="a-icon kind-${esc(a.kind)}">${ic(icon)}</span>
      <div class="grow">
        <div class="a-title" dir="auto">${esc(a.title)}${a.due ? ` <span class="pill">${esc(dayOf(a.due))}</span>` : ""}</div>
        <div class="a-why muted" dir="auto">${esc(a.why || "")}</div>
        ${(a.sources || []).length ? `<div class="a-src">${a.sources.map((d) => `<a href="#" data-doc="${d.id}" title="Open">${ic(KIND_ICON[d.kind] || "file")}<span>${esc(d.title)}</span></a>`).join("")}</div>` : ""}
      </div>
      <div class="a-actions">
        <button type="button" class="btn small primary" data-act>${label}</button>
        <button type="button" class="icon-btn" data-state="done" title="Done" aria-label="Done">${ic("tick")}</button>
        <button type="button" class="icon-btn" data-state="later" title="Remind me tomorrow" aria-label="Later">${ic("clock")}</button>
      </div></div>`;
  }).join("") : `<div class="empty-state">${ic("check")}<span>${t.refreshing ? "Checking your emails and documents…" : "Nothing needs you right now."}</span></div>`;
  $("#today-updated").textContent = t.refreshing ? "Checking…" : t.updated_at ? `Updated ${timeOf(t.updated_at)}` : "";

  const soon = t.meetings_soon.slice(0, 5);
  const noCalendar = !t.meetings_today.length && !t.meetings_soon.length;
  $("#today-meetings").innerHTML = t.meetings_today.length ? t.meetings_today.map((e) => meetingRow(e, false)).join("")
    : `<div class="empty-state small">${ic("calendar")}<span>${noCalendar ? `No meetings found. <a href="${me.role === "admin" ? "#sources" : "#mine"}">Connect your calendar</a>` : "No meetings today."}</span></div>`;
  $("#today-soon-card").classList.toggle("hidden", !soon.length);
  $("#today-soon").innerHTML = soon.map((e) => meetingRow(e, true)).join("");

  const all = [...t.meetings_today, ...t.meetings_soon];
  $$("#page-today [data-prep]").forEach((b) => b.onclick = () => askInChat(meetingPrompt(all.find((e) => String(e.id) === b.dataset.prep))));
  $$("#today-alerts .alert-card").forEach((card) => {
    const a = t.alerts.find((x) => x.key === card.dataset.key);
    $("[data-act]", card).onclick = () => askInChat(a.action || a.title);
    $$("[data-state]", card).forEach((b) => b.onclick = async () => {
      card.classList.add("leaving");
      try { await api(`/api/today/alerts/${a.key}`, { method: "POST", json: { action: b.dataset.state } }); } catch (err) { toast(err.message, true); }
      todayData.alerts = todayData.alerts.filter((x) => x.key !== a.key);
      setTimeout(() => { card.remove(); setNavCount("today", todayData.alerts.length);
        if (!todayData.alerts.length) $("#today-alerts").innerHTML = `<div class="empty-state">${ic("check")}<span>Nothing needs you right now.</span></div>`; }, 200);
      toast(b.dataset.state === "done" ? "Marked done." : "We'll remind you tomorrow.");
    });
  });
  if (t.refreshing) poll(LOADERS.today, 3000);
};
$("#today-refresh").addEventListener("click", async () => {
  const b = $("#today-refresh"); b.disabled = true;
  try { await api("/api/today/refresh", { method: "POST" }); await LOADERS.today(); } catch (err) { toast(err.message, true); }
  b.disabled = false;
});
// Admins: things that are broken, and the first steps, at the top of Today
async function drawAdminNotices() {
  const [setup, health] = await Promise.all([getJSON("/api/admin/setup"), getJSON("/api/admin/health")]);
  const FIX = { ai: ["#health", "Fix it"], sources: ["#sources", "Open Sources"], license: ["#settings", "Open Settings"] };
  const broken = health.checks.filter((c) => c.state === "bad");
  $("#today-health").innerHTML = !broken.length ? "" : `<div class="health-banner bad">${ic("error")}<div class="grow">
      <b>${broken.length === 1 ? "Something isn't working" : "Some things aren't working"}</b>
      <ul>${broken.map((c) => `<li><b>${esc(c.label)}:</b> ${esc(c.detail)}</li>`).join("")}</ul></div>
      <a class="btn" href="${(FIX[broken[0].id] || ["#health"])[0]}">${(FIX[broken[0].id] || ["", "See details"])[1]}</a></div>`;
  const steps = [
    [setup.has_sources, "Connect your company's information", "#sources", "Connect sources"],
    [setup.has_team, "Add your team", "#people", "Add people"],
    [setup.has_asked, "Ask your first question", "#ask", "Ask a question"],
  ];
  const done = steps.filter((x) => x[0]).length, nextIndex = steps.findIndex((x) => !x[0]);
  $("#today-setup").innerHTML = done === steps.length ? "" : `<div class="card"><div class="toolbar"><h2>Get set up</h2>
      <span class="muted small">${done} of ${steps.length} done</span></div>
    <ol class="steps">${steps.map(([ok, title, href, label], i) => `<li class="${ok ? "done" : ""}">
      <span class="step-dot">${ok ? ic("tick") : i + 1}</span><div><b>${title}</b></div>
      ${ok ? badge("ok", "Done") : `<a class="btn ${nextIndex === i ? "primary" : ""}" href="${href}">${label}</a>`}</li>`).join("")}</ol></div>`;
}

// ================================================================== Insights (admins)
LOADERS.home = async function loadHome() {
  const days = Number($("#home-days").value);
  const a = await getJSON(`/api/admin/analytics?days=${days}`);
  const t = a.totals;
  const rate = t.asked_period ? Math.round((t.answered_period / t.asked_period) * 100) : null;
  const [brain, qs] = await Promise.all([getJSON("/api/brain"), getJSON("/api/admin/questions?limit=300")]);

  // 1. Four numbers, each a plain sentence
  const week = a.per_day.slice(-7), before = a.per_day.slice(-14, -7);
  const askedWeek = week.reduce((s, d) => s + d.asked, 0), askedBefore = before.reduce((s, d) => s + d.asked, 0);
  const trend = (now, prev) => before.length < 7 ? "" : now === prev ? "Same as the week before" : `${now > prev ? "Up" : "Down"} from ${num(prev)} the week before`;
  const gaps = qs.questions.filter((q) => !q.found_sources || q.feedback === -1);
  const tiles = [
    ["brain", "#brain", num(t.searchable), "things Cortex knows", `From ${plural(brain.apps.length, "connected app", "connected apps")}`],
    ["refresh", "#sources", num(t.added_week), "new items learned this week", `${num(t.added_today)} today`],
    ["ask", "#conversations", num(askedWeek), "questions asked this week", trend(askedWeek, askedBefore) || `${num(t.asked_period)} in the last ${days} days`],
    ["check", "#conversations", rate == null ? "0" : `${rate}%`, "of questions answered", rate == null ? "Once people start asking" : `In the last ${days} days`],
  ];
  $("#home-kpis").innerHTML = tiles.map(([, href, value, label, sub]) => `<a class="stat-tile" href="${href}">
      <span class="st-text"><span class="st-value">${value}</span> <span class="st-label">${label}</span>
      <span class="st-sub">${esc(sub)}</span></span></a>`).join("");

  // 2. Is the team using it: questions per day
  const qRows = a.per_day.map((r) => ({ day: r.day, answered: r.asked - r.unanswered, unanswered: r.unanswered }));
  $("#home-q-chart").innerHTML = t.asked_period ? columnChart(qRows, [
    { key: "answered", label: "Answered", color: "--series-1" }, { key: "unanswered", label: "Not answered", color: "--series-2" }],
    { title: "Questions per day", height: 190, legend: false }) : '<div class="empty">No questions yet. Once people start asking, you\'ll see each day here.</div>';

  // 3. What it couldn't answer: the actual questions
  $("#home-gaps").innerHTML = gaps.length ? `<div class="gap-list">${gaps.slice(0, 6).map((q) => `<a href="#" class="gap" data-gap-conv="${q.conversation_id || ""}">
      <span class="gap-q" dir="auto">${esc(q.question)}</span>
      <span class="gap-meta">${esc(q.user_name || q.user_email || "")} · ${ago(q.created_at)} · ${!q.found_sources ? "Nothing found" : "Marked unhelpful"}</span></a>`).join("")}</div>
      <a class="btn small" href="#conversations" id="gaps-all">See all unanswered</a>`
    : `<div class="empty">${t.asked_period ? "Every question so far got an answer." : "Nothing yet."}</div>`;
  $$("#home-gaps [data-gap-conv]").forEach((g) => g.onclick = (e) => { e.preventDefault(); if (g.dataset.gapConv) openConversation(Number(g.dataset.gapConv)).catch((err) => toast(err.message, true)); });
  $("#gaps-all")?.addEventListener("click", () => { conv.scope = "all"; $$("#conv-scope button").forEach((x) => x.classList.toggle("on", x.dataset.scope === "all")); $("#q-gaps").checked = true; });

  // 4. What it knows, by app
  const top = Math.max(1, ...brain.apps.map((f) => f.count));
  $("#home-knows").innerHTML = brain.apps.length ? `<div class="knows">${brain.apps.slice(0, 9).map((f) => {
      const [label] = APP_FOLDERS[f.key] || [f.label || f.key];
      const icon = f.key.startsWith("upload:") ? brandTile(APP_LOGO[f.key] || "", f.key === "upload:interview" ? "mic" : "upload", "sm") : brandTile(f.key, (SERVICE_INFO[f.key] || ["file"])[0], "sm");
      return `<a href="#brain" class="know-row" data-know="${esc(f.key)}">${icon}<span class="kr-label">${esc(f.label || label)}</span>
        <span class="kr-bar"><i style="width:${Math.max(2, (f.count / top) * 100)}%"></i></span><span class="kr-n">${num(f.count)}</span></a>`;
    }).join("")}</div>`
    : '<div class="empty">Nothing connected yet. <a href="#sources">Connect your first source</a></div>';
  $$("#home-knows [data-know]").forEach((r) => r.onclick = () => { brainState.folder = { key: r.dataset.know }; setTimeout(() => brainState.data && openFolder(r.dataset.know), 300); });

  $("#home-by-person").innerHTML = hbarChart(a.by_person, "No questions yet.");
  $("#home-by-dept").innerHTML = hbarChart(a.by_department, "No questions yet.");
  drawLearning(a);
};
$("#home-days").addEventListener("change", () => LOADERS.home());


const LEARN_LABEL = { gmail: "Gmail", gcal: "Google Calendar", gdrive: "Google Drive", ms_mail: "Outlook mail", ms_cal: "Outlook calendar",
  ms_drive: "OneDrive", slack: "Slack", meta_social: "Facebook and Instagram", youtube: "YouTube", website: "Website", whatsapp_business: "WhatsApp Business", imap: "Mailbox", ics: "Calendar" };
function drawLearning(a) {
  const t = a.totals;
  const rows = a.freshness.map((k) => {
    const [icon] = SERVICE_INFO[k.type] || ["file"];
    const state = k.last_error ? badge("bad", "Needs attention") : !k.enabled ? badge("", "Paused") : badge("ok", "Live");
    const next = k.last_sync ? Math.max(0, a.check_every_minutes - Math.round((Date.now() - new Date(k.last_sync)) / 60000)) : 0;
    return `<div class="learn-row">${brandTile(k.type, icon, "sm")}
      <div><div class="who">${esc(LEARN_LABEL[k.type] || k.name)} ${state}</div>
        <div class="meta" dir="auto">${esc(k.account || (k.type === "website" ? k.name : ""))}</div></div>
      <div class="right">${k.last_new ? `Last new item ${ago(k.last_new)}` : "Nothing new yet"}${k.added_today ? ` · <b>${num(k.added_today)}</b> today` : ""}
        <br>${k.type === "whatsapp_business" ? "Messages arrive instantly" : k.last_sync ? `Checked ${ago(k.last_sync)} · next check ${next ? `in ~${next} min` : "now"}` : "First import running"}</div></div>`;
  });
  if (a.uploads.last_new) rows.push(`<div class="learn-row"><span class="icon-tile sm">${ic("upload")}</span>
      <div><div class="who">Uploaded files ${badge("ok", "Added")}</div><div class="meta">Documents, interviews and chats added by hand</div></div>
      <div class="right">Last added ${ago(a.uploads.last_new)}${a.uploads.added_today ? ` · <b>${num(a.uploads.added_today)}</b> today` : ""}</div></div>`);
  $("#home-learning").innerHTML = `<div class="learn-head"><div><h2>Keeping up to date</h2>
      <p class="muted small" style="margin:0">Checked every ${a.check_every_minutes} minutes.</p></div>
      <div class="learn-stats"><div><div class="big-number">${num(t.added_today)}</div><div class="muted small">new today</div></div>
      <div><div class="big-number">${num(t.added_week)}</div><div class="muted small">new this week</div></div></div></div>
    <div style="margin-top:12px">${rows.join("") || '<div class="empty">Nothing connected yet. <a href="#sources">Connect your first source</a></div>'}</div>
    ${t.added_week || a.growth.some((g) => g.added) ? `<h3 style="margin:18px 0 2px">New items per day</h3><p class="muted small" style="margin:0 0 8px">How much new information was added each day.</p>${columnChart(a.growth, [{ key: "added", label: "New items", color: "--series-1" }], { height: 140, title: "New items per day" })}` : ""}`;
}

// ================================================================== Ask (the chat)
const chat = { history: [], busy: false, cid: null };
const EXAMPLES = {
  calendar: "What meetings do I have this week?", email: "What were the latest emails about prices?",
  documents: "What are our main procedures?", web: "What services do we offer?",
  interview: "How do we handle a new customer order?", whatsapp: "What was decided in the team chat recently?",
  slack: "What was discussed in Slack this week?",
  social: "What was our latest Instagram post and how many likes did it get?",
};
const SCOPE_KINDS = [["email", "email", "Emails"], ["documents", "file", "Documents and files"], ["calendar", "calendar", "Calendar"],
  ["slack", "slack", "Slack"], ["whatsapp", "whatsapp", "WhatsApp"], ["web", "web", "Websites"], ["social", "social", "Social media"], ["interview", "interview", "Interviews"]];
const qBox = $("#question");
const chatEmpty = () => !$(".msg", $("#messages"));

LOADERS.ask = async function loadAsk() {
  let o = { ready: {}, total: 0 };
  try { o = await getJSON("/api/overview"); } catch {}
  const kinds = SCOPE_KINDS.filter(([key]) => o.ready[key]);
  const mine = me.role === "admin" ? departments : me.departments;
  const sel = $("#scope-filter"), current = sel.value;
  sel.innerHTML = `<option value="">All sources</option>` +
    (kinds.length > 1 ? `<optgroup label="Only">${kinds.map(([, kind, label]) => `<option value="kind:${kind}">${label}</option>`).join("")}</optgroup>` : "") +
    (mine.length ? `<optgroup label="Department">${mine.map((d) => `<option value="dept:${d.id}">${esc(d.name)}</option>`).join("")}</optgroup>` : "");
  if ([...sel.options].some((x) => x.value === current)) sel.value = current;
  $("#scope-label").classList.toggle("hidden", sel.options.length < 2);
  if (!chat.busy && chatEmpty()) drawWelcome(o);
  qBox.focus();
};
function greeting() {
  const h = new Date().getHours();
  const part = h < 5 ? "Good evening" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  const first = firstName(me);
  return first ? `${part}, ${first}` : part;
}
// Empty chat: a greeting, the composer in the middle, and a few things to try
function drawWelcome(o) {
  if (chat.busy || !chatEmpty()) return;
  $("#page-ask").classList.add("is-new");
  const examples = Object.keys(EXAMPLES).filter((k) => o.ready[k]).map((k) => EXAMPLES[k]).slice(0, 4);
  $("#chat-hello").innerHTML = `<img src="/static/cortex.svg" alt="" class="hello-logo"><h1>${esc(greeting())}</h1>
    <p class="muted">${o.total ? "What would you like to know about the company?" : me.role === "admin"
      ? '<a href="#sources">Connect your first source</a> to start.' : "Your admin is still adding the company's information."}</p>`;
  $("#chat-suggest").innerHTML = examples.map((x) => `<button type="button">${esc(x)}</button>`).join("");
  $$("#chat-suggest button").forEach((b) => b.onclick = () => { qBox.value = b.textContent; $("#ask-form").requestSubmit(); });
}
function leaveWelcome() {
  $("#page-ask").classList.remove("is-new");
  $("#chat-hello").innerHTML = ""; $("#chat-suggest").innerHTML = "";
}
function newChat() {
  if (chat.busy) return;
  chat.history = []; chat.cid = null;
  $("#messages").innerHTML = "";
  getJSON("/api/overview").then(drawWelcome).catch(() => drawWelcome({ ready: {}, total: 0 }));
  qBox.value = ""; qBox.style.height = "auto"; qBox.focus();
}
$("#new-chat").addEventListener("click", newChat);
$("#conv-new").addEventListener("click", () => setTimeout(newChat, 0));

function renderAnswer(text, sources) {
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\[(\d+)\]/g, (m, n) => {
      const s = sources[Number(n) - 1];
      return s ? `<span class="cite" title="${esc(s.title + (s.location ? ", " + s.location : ""))}">${n}</span>` : m;
    });
  const out = []; let list = null;
  for (const raw of text.split("\n")) {
    // [[file:N]]: the file itself, right here in the answer, with Open and Download
    const cards = [];
    const line = raw.replace(/\[\[file:(\d+)\]\]/g, (m, n) => { const s = sources[Number(n) - 1]; if (s) cards.push(fileCard(s)); return ""; }).trimEnd();
    if (cards.length && !line.trim()) { if (list) { out.push(`</${list.type}>`); list = null; } out.push(...cards); continue; }
    if (cards.length) { out.push(`<p>${inline(line)}</p>`, ...cards); continue; }
    const bullet = line.match(/^\s*[-*•]\s+(.*)/); const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (bullet || numbered) {
      const type = bullet ? "ul" : "ol";
      if (!list || list.type !== type) { if (list) out.push(`</${list.type}>`); out.push(`<${type}>`); list = { type }; }
      out.push(`<li>${inline((bullet || numbered)[1])}</li>`); continue;
    }
    if (list) { out.push(`</${list.type}>`); list = null; }
    if (!line.trim()) continue;
    const h = line.match(/^#{1,4}\s+(.*)/);
    out.push(h ? `<h3>${inline(h[1])}</h3>` : `<p>${inline(line)}</p>`);
  }
  if (list) out.push(`</${list.type}>`);
  return out.join("");
}
const FILE_COLORS = { PDF: "#e5252a", DOC: "#2b579a", DOCX: "#2b579a", XLS: "#217346", XLSX: "#217346", CSV: "#217346", PPT: "#d24726", PPTX: "#d24726" };
function fileCard(s) {
  const ext = (s.filename || "").includes(".") ? s.filename.split(".").pop().toUpperCase().slice(0, 4) : "FILE";
  const when = s.date ? new Date(s.date).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
  const to = (s.to || "").replace(/<[^>]*>/g, "").split(",")[0].trim();
  const about = s.email_subject ? `Attached to "${esc(s.email_subject)}"` : esc(s.title);
  const meta = [about, when && (s.email_subject ? `sent ${when}` : when), to && s.email_subject ? `to ${esc(to)}` : "", s.private ? "Only you" : ""].filter(Boolean).join(" · ");
  const open = s.url ? `href="${esc(s.url)}" target="_blank" rel="noopener"` : `href="#" data-doc="${s.document_id}"`;
  return `<div class="file-card" dir="ltr"><span class="file-icon" style="--fc:${FILE_COLORS[ext] || "var(--accent)"}">${esc(ext)}</span>
    <div class="file-info"><div class="file-name" dir="auto">${esc(s.filename || s.title)}</div><div class="file-meta" dir="auto">${meta}</div></div>
    <a class="btn small" ${open}>Open</a>
    <a class="btn small primary" href="#" data-doc="${s.document_id}" data-download>${ic("download")}Download</a></div>`;
}
// Real files someone can open or download: attachments, Drive / OneDrive files and uploads (not synced posts or chat days)
const isFile = (s) => s.kind === "file" && /\.[a-z0-9]{2,5}$/i.test(s.filename || "");
function addUser(text) {
  leaveWelcome();
  const div = document.createElement("div");
  div.className = "msg user"; div.innerHTML = `<div class="bubble" dir="auto">${esc(text)}</div>`;
  $("#messages").append(div);
  return div;
}
function addBot() {
  const div = document.createElement("div");
  div.className = "msg bot";
  div.innerHTML = `<img src="/static/cortex.svg" alt="" class="bot-avatar"><div class="bot-body">
    <div class="answer" dir="auto"><div class="thinking"><span class="dots"><i></i><i></i><i></i></span><span>Searching your company's information</span></div></div></div>`;
  $("#messages").append(div);
  scrollChat(true);
  return div;
}
function scrollChat(force) {
  const el = $("#chat-scroll");
  if (force || el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight;
}
// Under every answer: its sources as chips, then copy and feedback
// An email draft in an answer: "To: ...", "Subject: ...", then the body
function emailDraft(text) {
  const clean = text.replace(/\[\[file:\d+\]\]\n?/g, "").replace(/\s*\[\d+\]/g, "").replace(/\*\*/g, "");
  const subj = clean.match(/^\s*(?:Subject|נושא)\s*:\s*(.+)$/mi);
  if (!subj) return null;
  const to = clean.match(/^\s*(?:To|אל)\s*:\s*(.+)$/mi);
  const after = clean.slice(subj.index + subj[0].length).replace(/^\s*\n/, "");
  const bodyText = after.replace(/\n\s*```\s*$/, "").trim();
  if (!bodyText) return null;
  const addr = to ? (to[1].match(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g) || []).join(",") : "";
  return { to: addr, subject: subj[1].trim(), body: bodyText };
}
function finishBot(bot, text, sources, qid, feedback = null) {
  const body = $(".bot-body", bot);
  const draft = emailDraft(text);
  const carded = new Set([...$$(".file-card [data-download]", body)].map((a) => a.dataset.doc));
  const cited = sources.filter((s) => text.includes(`[${s.n}]`) && !carded.has(String(s.document_id)));
  if (cited.length) body.insertAdjacentHTML("beforeend", `<div class="src-chips">${cited.map((s) => {
    // Files (attachments, Drive files, uploads) show their type and can be downloaded right here
    const ext = isFile(s) ? s.filename.split(".").pop().toUpperCase() : "";
    const label = `<b>${s.n}</b>${ic(KIND_ICON[s.kind] || "file")}<span>${esc(s.title)}</span>${ext ? `<i class="ftype">${esc(ext)}</i>` : ""}${s.private ? `<i class="only-you" title="Private: only you can see this">${ic("lock")}Only you</i>` : ""}`;
    const chip = s.url ? `<a class="src-chip" href="${esc(s.url)}" target="_blank" rel="noopener" title="${esc(s.title)}">${label}</a>`
      : `<a class="src-chip" href="#" data-doc="${s.document_id}" title="Open ${esc(s.title + (s.location ? ", " + s.location : ""))}">${label}</a>`;
    return ext && !s.url ? `<span class="src-file">${chip}<a class="src-dl" href="#" data-doc="${s.document_id}" data-download title="Download ${esc(s.filename)}" aria-label="Download ${esc(s.filename)}">${ic("download")}</a></span>` : chip;
  }).join("")}</div>`);
  if (draft) body.insertAdjacentHTML("beforeend", `<div class="draft-actions">
    <a class="btn small primary" href="mailto:${encodeURIComponent(draft.to).replace(/%2C/g, ",")}?subject=${encodeURIComponent(draft.subject)}&body=${encodeURIComponent(draft.body)}" data-open-mail>${ic("send")}Open in email</a>
    <button type="button" class="btn small" data-copy-mail>${ic("copy")}Copy email</button></div>`);
  if (draft) $("[data-copy-mail]", body).onclick = async () => {
    try { await navigator.clipboard.writeText(`Subject: ${draft.subject}\n\n${draft.body}`); toast("Email copied."); } catch { toast("Select the text and copy it.", true); }
  };
  body.insertAdjacentHTML("beforeend", `<div class="msg-actions">
    <button type="button" data-copy-answer title="Copy" aria-label="Copy">${ic("copy")}</button>
    ${qid ? `<button type="button" data-v="1" title="Helpful" aria-label="Helpful" class="${feedback === 1 ? "chosen" : ""}">${ic("up")}</button>
    <button type="button" data-v="-1" title="Not helpful" aria-label="Not helpful" class="${feedback === -1 ? "chosen" : ""}">${ic("down")}</button>` : ""}</div>`);
  $("[data-copy-answer]", body).onclick = async () => {
    try { await navigator.clipboard.writeText(text.replace(/\[\[file:\d+\]\]\n?/g, "").replace(/\s*\[\d+\]/g, "")); toast("Copied."); } catch { toast("Select the text and copy it.", true); }
  };
  $$("[data-v]", body).forEach((b) => b.onclick = async () => {
    $$("[data-v]", body).forEach((x) => x.classList.toggle("chosen", x === b));
    try { await api(`/api/questions/${qid}/feedback`, { method: "POST", json: { value: Number(b.dataset.v) } }); toast("Thanks. Your feedback helps improve the answers."); }
    catch (err) { toast(err.message, true); }
  });
}

qBox.addEventListener("input", () => { qBox.style.height = "auto"; qBox.style.height = Math.min(qBox.scrollHeight, 220) + "px"; });
qBox.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#ask-form").requestSubmit(); } });

$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = qBox.value.trim();
  if (!question || chat.busy) return;
  chat.busy = true; $("#ask-btn").disabled = true;
  qBox.value = ""; qBox.style.height = "auto";
  addUser(question);
  const bot = addBot();
  const answerEl = $(".answer", bot);
  const scope = $("#scope-filter").value;
  let text = "", sources = [], qid = null, failed = "";
  try {
    const res = await api("/api/ask", { method: "POST", json: { question, history: chat.history, conversation_id: chat.cid,
      department_id: scope.startsWith("dept:") ? Number(scope.slice(5)) : null, kinds: scope.startsWith("kind:") ? [scope.slice(5)] : [] } });
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buf.slice(0, nl)); buf = buf.slice(nl + 1);
        if (ev.type === "conversation") chat.cid = ev.id;
        if (ev.type === "plan" && ev.summary) { const t = $(".thinking span:last-child", bot); if (t) t.textContent = `Looking for: ${ev.summary}`; }
        if (ev.type === "sources") { sources = ev.sources; const t = $(".thinking span:last-child", bot); if (t && sources.length) t.textContent = `${t.textContent.startsWith("Looking for") ? t.textContent + ". " : ""}Found ${plural(sources.length, "match", "matches")}. Writing the answer`; }
        if (ev.type === "text") { text += ev.text; answerEl.innerHTML = renderAnswer(text, sources) + '<span class="caret"></span>'; }
        if (ev.type === "error") failed = ev.message;
        if (ev.type === "done") qid = ev.question_id;
      }
      scrollChat();
    }
  } catch (err) { failed = err.message; }
  chat.busy = false; $("#ask-btn").disabled = false;
  if (failed && !text) {
    answerEl.innerHTML = `<p class="state-text bad">${ic("alert")} ${esc(failed)}</p><button class="btn small" type="button">${ic("refresh")}Try again</button>`;
    $("button", answerEl).onclick = () => { bot.previousElementSibling?.remove(); bot.remove(); qBox.value = question; $("#ask-form").requestSubmit(); };
    return;
  }
  answerEl.innerHTML = renderAnswer(text, sources);
  if (!/\[\[file:\d+\]\]/.test(text)) {
    const files = sources.filter((s) => isFile(s) && text.includes(`[${s.n}]`));
    if (files.length === 1) answerEl.insertAdjacentHTML("beforeend", fileCard(files[0]));
  }
  chat.history.push({ role: "user", content: question }, { role: "assistant", content: text });
  finishBot(bot, text, sources, qid);
  scrollChat();
  qBox.focus();
});

// Open a saved conversation in the chat, to read it and carry on
async function openConversation(id) {
  const c = await getJSON(`/api/conversations/${id}`);
  if (!c.mine) return viewConversation(c);
  chat.cid = c.id; chat.history = [];
  $("#messages").innerHTML = ""; leaveWelcome();
  for (const m of c.messages) {
    addUser(m.question);
    const bot = addBot();
    $(".answer", bot).innerHTML = m.answer ? renderAnswer(m.answer, m.sources || []) : `<p class="muted">No answer was saved.</p>`;
    if (m.answer && !/\[\[file:\d+\]\]/.test(m.answer)) {
      const files = (m.sources || []).filter((s) => isFile(s) && m.answer.includes(`[${s.n}]`));
      if (files.length === 1) $(".answer", bot).insertAdjacentHTML("beforeend", fileCard(files[0]));
    }
    if (m.answer) finishBot(bot, m.answer, m.sources || [], m.id, m.feedback);
    chat.history.push({ role: "user", content: m.question }, { role: "assistant", content: m.answer || "" });
  }
  location.hash = "#ask";
  setTimeout(() => scrollChat(true), 50);
}
// Someone else's conversation (admins): read only
function viewConversation(c) {
  const f = openDialog(`<h2 dir="auto">${esc(c.title || "Conversation")}</h2>
    <p class="muted small">${esc(c.user_name || c.user_email || "")} · ${new Date(c.updated_at).toLocaleString()}</p>
    <div class="conv-view">${c.messages.map((m) => `<div class="msg user"><div class="bubble" dir="auto">${esc(m.question)}</div></div>
      <div class="msg bot"><img src="/static/cortex.svg" alt="" class="bot-avatar"><div class="bot-body"><div class="answer" dir="auto">${m.private_answer ? `<p class="muted">${ic("lock")} The answer used ${esc(c.user_name || "this person")}'s private items, so it's only visible to them.</p>` : m.answer ? renderAnswer(m.answer, m.sources || []) : '<p class="muted">No answer was saved.</p>'}</div>
      ${!m.found_sources ? badge("bad", "No answer found") : m.feedback === -1 ? badge("bad", "Not helpful") : m.feedback === 1 ? badge("ok", "Helpful") : ""}</div></div>`).join("")}</div>
    <div class="dialog-actions"><button class="btn primary" type="button" data-close>Close</button></div>`);
  $("[data-close]", f).onclick = closeDialog;
}

// ================================================================== Conversations
const conv = { scope: "mine" };
LOADERS.conversations = async function loadConversations() {
  const admin = me.role === "admin";
  $("#conv-scope").classList.toggle("hidden", !admin);
  $("#conv-gaps-label").classList.toggle("hidden", !admin || conv.scope !== "all");
  $("#q-stats").classList.toggle("hidden", !admin || conv.scope !== "all");
  $("#conv-intro").textContent = admin && conv.scope === "all"
    ? "Everyone's chats." : "";
  if (admin && conv.scope === "all") {
    const s = (await getJSON("/api/admin/questions?limit=1")).stats;
    $("#q-stats").innerHTML = [["Questions asked", s.total], ["This week", s.last_7_days], ["Helpful", s.helpful], ["Not helpful", s.not_helpful], ["No answer found", s.no_sources]]
      .map(([l, v]) => `<div class="stat"><div class="label">${l}</div><div class="big-number">${num(v)}</div></div>`).join("");
  }
  const q = new URLSearchParams({ scope: conv.scope, q: $("#conv-search").value, gaps: String(admin && conv.scope === "all" && $("#q-gaps").checked) });
  const rows = await getJSON(`/api/conversations?${q}`);
  const everyone = conv.scope === "all";
  $("#conv-list").innerHTML = rows.map((c) => `<div class="conv-row" data-conv="${c.id}" role="button" tabindex="0">
      <span class="icon-tile sm">${ic("chat")}</span>
      <div class="grow"><div class="title" dir="auto">${esc(c.title || "Conversation")}</div>
        <div class="meta" dir="auto">${esc((c.last_answer || "").replace(/\s*\[\d+\]/g, "").slice(0, 140))}</div>
        <div class="meta">${everyone ? `${esc(c.user_name || c.user_email || "")} · ` : ""}${plural(c.messages, "question", "questions")} · ${ago(c.updated_at)}</div></div>
      <div class="row tight">${c.unanswered ? badge("bad", "No answer found") : c.not_helpful ? badge("bad", "Not helpful") : ""}
        ${c.user_id === me.id ? `<button type="button" class="btn link small danger" data-del-conv="${c.id}" title="Delete" aria-label="Delete">${ic("trash")}</button>` : ""}</div></div>`).join("")
    || `<div class="empty">${$("#conv-search").value ? "No conversations match." : everyone && $("#q-gaps").checked ? "No unanswered questions." : 'No conversations yet. <a href="#ask">Ask your first question</a>'}</div>`;
  $$("#conv-list .conv-row").forEach((r) => {
    const open = (e) => { if (e.target.closest("[data-del-conv]")) return; openConversation(Number(r.dataset.conv)).catch((err) => toast(err.message, true)); };
    r.onclick = open; r.onkeydown = (e) => { if (e.key === "Enter") open(e); };
  });
  $$("[data-del-conv]").forEach((b) => b.onclick = async () => {
    if (!await confirmBox({ title: "Delete this conversation?", text: "It will be removed from your conversations.", confirm: "Delete", danger: true })) return;
    await api(`/api/conversations/${b.dataset.delConv}`, { method: "DELETE" });
    if (chat.cid === Number(b.dataset.delConv)) { chat.cid = null; chat.history = []; $("#messages").innerHTML = ""; }
    toast("Deleted."); LOADERS.conversations();
  });
};
$$("#conv-scope button").forEach((b) => b.onclick = () => {
  conv.scope = b.dataset.scope;
  $$("#conv-scope button").forEach((x) => x.classList.toggle("on", x === b));
  LOADERS.conversations();
});
$("#conv-search").addEventListener("input", debounce(() => LOADERS.conversations()));
$("#q-gaps").addEventListener("change", () => LOADERS.conversations());

document.addEventListener("click", async (e) => {
  const a = e.target.closest("a[data-doc]");
  if (!a) return;
  e.preventDefault();
  try {
    const res = await api(`/api/documents/${a.dataset.doc}/file`);
    const url = URL.createObjectURL(await res.blob());
    const name = (res.headers.get("content-disposition") || "").match(/filename\*?=(?:UTF-8'')?"?([^";]+)/i)?.[1];
    const link = Object.assign(document.createElement("a"), { href: url, target: "_blank", rel: "noopener" });
    if (name && (a.hasAttribute("data-download") || !/\.(pdf|txt|html?|png|jpe?g)$/i.test(decodeURIComponent(name)))) link.download = decodeURIComponent(name);
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
    (total > pageSize ? `<div class="pager"><span>${num(state.offset + 1)} to ${num(state.offset + rows.length)} of ${num(total)}</span><span class="row tight">
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




// Real service logos, so every connection is recognisable at a glance
const BRAND_SVG = {
  google: '<path fill="#4285F4" d="M21.6 12.2c0-.7-.1-1.4-.2-2H12v3.8h5.4a4.6 4.6 0 0 1-2 3v2.5h3.2c1.9-1.7 3-4.3 3-7.3z"/><path fill="#34A853" d="M12 22c2.7 0 5-.9 6.6-2.4l-3.2-2.5c-.9.6-2 1-3.4 1-2.6 0-4.8-1.8-5.6-4.1H3.1v2.6A10 10 0 0 0 12 22z"/><path fill="#FBBC04" d="M6.4 14c-.2-.6-.3-1.3-.3-2s.1-1.4.3-2V7.4H3.1a10 10 0 0 0 0 9.2z"/><path fill="#EA4335" d="M12 5.9c1.5 0 2.8.5 3.8 1.5l2.9-2.9A10 10 0 0 0 3.1 7.4L6.4 10c.8-2.4 3-4.1 5.6-4.1z"/>',
  gmail: '<path fill="#4285F4" d="M3.5 19h3V11L2 7.6v9.9c0 .8.7 1.5 1.5 1.5z"/><path fill="#34A853" d="M17.5 19h3c.8 0 1.5-.7 1.5-1.5V7.6L17.5 11z"/><path fill="#FBBC04" d="M17.5 5.5V11L22 7.6V6.3c0-1.7-1.9-2.6-3.2-1.6z"/><path fill="#EA4335" d="M6.5 11V5.5L12 9.6l5.5-4.1V11L12 15.1z"/><path fill="#C5221F" d="M2 6.3v1.3L6.5 11V5.5L5.2 4.7C3.9 3.7 2 4.6 2 6.3z"/>',
  gcal: '<rect x="3" y="3" width="18" height="18" rx="2.5" fill="#fff" stroke="#4285F4" stroke-width="1.6"/><path fill="#4285F4" d="M3 6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v2H3z"/><path fill="#EA4335" d="M17 21l4-4h-4z"/><text x="12" y="17.6" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" font-weight="700" font-size="8" fill="#1A73E8">31</text>',
  gdrive: '<path fill="#FBBC04" d="M8.3 3h7.4L22 14h-7.4z"/><path fill="#34A853" d="M8.3 3 12 9.4 5.7 20.5 2 14z"/><path fill="#4285F4" d="M5.7 20.5 9.4 14H22l-3.7 6.5z"/>',
  youtube: '<rect x="1" y="4.5" width="22" height="15" rx="4.5" fill="#FF0000"/><path fill="#fff" d="M10 8.8v6.4l5.5-3.2z"/>',
  microsoft: '<path fill="#F25022" d="M2 2h9.5v9.5H2z"/><path fill="#7FBA00" d="M12.5 2H22v9.5h-9.5z"/><path fill="#00A4EF" d="M2 12.5h9.5V22H2z"/><path fill="#FFB900" d="M12.5 12.5H22V22h-9.5z"/>',
  ms_mail: '<rect x="2" y="4" width="20" height="16" rx="3" fill="#0A64C9"/><ellipse cx="9" cy="12" rx="3.4" ry="4" fill="none" stroke="#fff" stroke-width="2"/><path d="M14.5 9h5M14.5 12h5M14.5 15h5" stroke="#9DCBFF" stroke-width="1.6"/>',
  ms_cal: '<rect x="3" y="3.5" width="18" height="17" rx="3" fill="#0A64C9"/><path fill="#fff" d="M6 9h12v8.5H6z"/><path d="M10 9v8.5M14 9v8.5M6 13.2h12" stroke="#0A64C9" stroke-width="1.2"/>',
  ms_drive: '<path fill="#0364B8" d="M10 8.2a5.2 5.2 0 0 1 9.4 2.4 3.7 3.7 0 0 1-.6 7.4H6.6A4.4 4.4 0 0 1 5.8 9.3 5.2 5.2 0 0 1 10 8.2z"/><path fill="#28A8EA" d="M10 8.2a5.2 5.2 0 0 1 9.4 2.4L12 13.8 5.8 9.3A5.2 5.2 0 0 1 10 8.2z" opacity=".85"/>',
  slack: '<g transform="scale(.444)"><path fill="#E01E5A" d="M19.7 34.2a5.4 5.4 0 1 1-5.4-5.4h5.4zm2.7 0a5.4 5.4 0 1 1 10.8 0v13.5a5.4 5.4 0 1 1-10.8 0z"/><path fill="#36C5F0" d="M27.8 19.7a5.4 5.4 0 1 1 5.4-5.4v5.4zm0 2.7a5.4 5.4 0 1 1 0 10.8H14.3a5.4 5.4 0 1 1 0-10.8z"/><path fill="#2EB67D" d="M34.3 27.8a5.4 5.4 0 1 1 5.4 5.4h-5.4zm-2.7 0a5.4 5.4 0 1 1-10.8 0V14.3a5.4 5.4 0 1 1 10.8 0z"/><path fill="#ECB22E" d="M26.2 34.3a5.4 5.4 0 1 1-5.4 5.4v-5.4zm0-2.7a5.4 5.4 0 1 1 0-10.8h13.5a5.4 5.4 0 1 1 0 10.8z"/></g>',
  facebook: '<circle cx="12" cy="12" r="11" fill="#1877F2"/><path fill="#fff" d="M13.4 21.9v-7.3h2.4l.4-2.9h-2.8V9.9c0-.8.3-1.4 1.4-1.4h1.5V6a19 19 0 0 0-2.2-.1c-2.2 0-3.7 1.3-3.7 3.8v2.1H8v2.9h2.4v7.3z"/>',
  instagram: '<defs><linearGradient id="ig-grad" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#FEDA75"/><stop offset=".35" stop-color="#FA7E1E"/><stop offset=".65" stop-color="#D62976"/><stop offset="1" stop-color="#4F5BD5"/></linearGradient></defs><rect x="1" y="1" width="22" height="22" rx="6" fill="url(#ig-grad)"/><rect x="5.5" y="5.5" width="13" height="13" rx="4" fill="none" stroke="#fff" stroke-width="1.8"/><circle cx="12" cy="12" r="3.1" fill="none" stroke="#fff" stroke-width="1.8"/><circle cx="16.4" cy="7.6" r="1" fill="#fff"/>',
  linkedin: '<rect x="1" y="1" width="22" height="22" rx="4" fill="#0A66C2"/><path fill="#fff" d="M5.5 9.5h2.8V18H5.5zM6.9 5.3a1.6 1.6 0 1 1 0 3.2 1.6 1.6 0 0 1 0-3.2zM10 9.5h2.7v1.2c.4-.7 1.3-1.4 2.7-1.4 2.9 0 3.4 1.9 3.4 4.3V18H16v-3.9c0-.9 0-2.1-1.3-2.1s-1.5 1-1.5 2V18H10z"/>',
  whatsapp: '<path fill="#25D366" d="M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2z"/><path fill="#fff" d="M8.9 7.3c-.2-.5-.4-.5-.6-.5h-.5c-.2 0-.5.1-.7.3-.3.3-.9.9-.9 2.2s.9 2.6 1.1 2.7c.1.2 1.8 2.9 4.5 3.9 2.2.9 2.7.7 3.1.7.5 0 1.6-.6 1.8-1.3.2-.6.2-1.2.2-1.3-.1-.1-.3-.2-.6-.3l-1.8-.9c-.3-.1-.5-.1-.7.1-.2.3-.7.9-.8 1-.2.2-.3.2-.6.1-.3-.1-1.1-.4-2.1-1.3-.8-.7-1.3-1.5-1.5-1.8-.1-.3 0-.4.1-.6l.4-.5.3-.5v-.5z"/>',
};
BRAND_SVG.whatsapp_business = BRAND_SVG.whatsapp;
const SOURCE_BRAND = { meta_social: ["facebook", "instagram"] };
// A logo tile for a source type; falls back to a plain icon for things without a brand (website, uploads).
function brandTile(type, fallbackIcon, size = "") {
  const marks = SOURCE_BRAND[type] || (BRAND_SVG[type] ? [type] : null);
  if (!marks) return `<span class="icon-tile ${size}">${ic(fallbackIcon)}</span>`;
  return `<span class="icon-tile brand ${size} ${marks.length > 1 ? "duo" : ""}">${marks.map((m) => `<svg viewBox="0 0 24 24" aria-hidden="true">${BRAND_SVG[m]}</svg>`).join("")}</span>`;
}

// ================================================================== Sources
const PROVIDER_TYPES = { google: { mail: "gmail", calendar: "gcal", files: "gdrive" }, microsoft: { mail: "ms_mail", calendar: "ms_cal", files: "ms_drive" },
  slack: { messages: "slack" }, meta: { social: "meta_social" }, youtube: { youtube: "youtube" } };
const PROVIDER_NAME = { google: "Google", microsoft: "Microsoft", slack: "Slack", meta: "Facebook", youtube: "YouTube" };
const PROVIDER_ACCOUNT = { google: "account", microsoft: "account", slack: "workspace", meta: "account", youtube: "channel" };
// Cards that sign in through another provider's page
const CARD_PROVIDER = { youtube: "google" };
const oauthOf = (card) => CARD_PROVIDER[card] || card;
const PROVIDER_SERVICES = {
  google: [["mail", "Gmail", "all mail except spam, trash and promotions"], ["calendar", "Google Calendar", ""], ["files", "Google Drive", ""]],
  microsoft: [["mail", "Outlook mail", "all mail in Inbox and Sent"], ["calendar", "Outlook calendar", ""], ["files", "OneDrive", ""]],
  slack: [["messages", "Slack channels", "public channels, plus private channels you add the Cortex app to"]],
  meta: [["social", "Facebook Pages and their Instagram accounts", "you choose which ones on Facebook's page"]],
  youtube: [["youtube", "YouTube channel", "videos, views, likes and comments"]],
};
const SERVICE_INFO = {
  gmail: ["mail", "Gmail", "email", "emails"], ms_mail: ["mail", "Outlook mail", "email", "emails"],
  gcal: ["calendar", "Google Calendar", "event", "events"], ms_cal: ["calendar", "Outlook calendar", "event", "events"],
  gdrive: ["folder", "Google Drive", "file", "files"], ms_drive: ["folder", "OneDrive", "file", "files"],
  imap: ["mail", "Mailbox", "email", "emails"], ics: ["calendar", "Calendar", "event", "events"], website: ["globe", "Website", "page", "pages"],
  slack: ["ask", "Channels", "message", "messages"],
  meta_social: ["share", "Pages and Instagram", "post", "posts"], youtube: ["play", "Videos", "video", "videos"],
  whatsapp_business: ["chat", "WhatsApp Business", "conversation-day", "conversation-days"],
};
const CALENDAR_TYPES = ["gcal", "ms_cal", "ics", "slack"]; // counted in events / messages, not documents
let conns = [];
let overviewNow = { importing: 0 };
let linkedinFiles = 0;
let oauthState = { configured: {}, redirect_url: "" };
let editing = false; // a form or panel is open: refreshes must not redraw it

function progressBar(done, total) {
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return `<div class="progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct}%"></span></div>`;
}
function linkify(text) {
  return esc(text).replace(/https:\/\/[^\s<]+[^\s<.,)]/g, (u) => `<a href="${u}" target="_blank" rel="noopener">${u.length > 60 ? u.slice(0, 57) + "…" : u}</a>`);
}
// Clear, honest status for one source: connected? importing? ready for search? any problem?
function sourceStatus(k) {
  const [, , one, many] = SERVICE_INFO[k.type] || ["", "", "item", "items"];
  const ready = CALENDAR_TYPES.includes(k.type) ? k.events : k.ready;
  const checked = k.last_sync ? `<span class="muted small">Checked ${ago(k.last_sync)}</span>` : "";
  let note = k.last_note ? `<div class="status-detail">${esc(k.last_note)}</div>` : "";
  if (k.type === "slack" && k.channels != null) note += `<div class="status-detail">Reading ${plural(k.channels, "channel", "channels")}.${k.private_not_added
    ? ` ${plural(k.private_not_added, "private channel isn't", "private channels aren't")} included. In Slack, open the channel → its name → Integrations → Add apps → Cortex.` : ""}</div>`;
  const searchable = ready ? `${plural(ready, one, many)} searchable` : "";
  if (k.last_error && !k.syncing)
    return `<div class="status-line">${badge("bad", "Needs attention")} ${checked}</div><div class="status-detail bad">${linkify(k.last_error)}</div>
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
      ${progressBar(done, total)}<div class="status-detail">Items are prepared one by one${overviewNow.importing > k.indexing ? `. ${num(overviewNow.importing)} items in line in total` : ""}${overviewNow.eta ? `, about ${esc(overviewNow.eta)} left` : ""}. You can already ask about the ready ones.</div>${note}`;
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
  ["slack", "Slack", "ask", "slack"], ["meta_social", "Facebook & Instagram", "share", "meta"], ["youtube", "YouTube", "play", "youtube"],
  ["linkedin", "LinkedIn", "share", "linkedin-card"], ["website", "Websites", "globe", "web-card"], ["whatsapp", "WhatsApp", "chat", "whatsapp-card"],
  ["uploads", "Uploaded files", "upload", "files-card"],
];
function tileState(type) {
  const ks = conns.filter((k) => k.type === type);
  if (type === "whatsapp") {
    const business = conns.filter((k) => k.type === "whatsapp_business");
    const n = overviewNow.ready.whatsapp || 0;
    if (business.some((k) => k.last_error)) return ["bad", "Needs attention", ""];
    if (business.length) return ["ok", "Connected", n ? plural(n, "conversation", "conversations") : "Waiting for messages"];
    return n ? ["ok", "Chats imported", plural(n, "chat", "chats")] : ["", "Not connected", ""];
  }
  if (type === "uploads") return ["", "", ""];
  if (type === "linkedin") return linkedinFiles ? ["ok", "Imported", plural(linkedinFiles, "file", "files")] : ["", "Not connected", ""];
  if (!ks.length) return ["", "Not connected", ""];
  const count = ks.reduce((a, k) => a + (CALENDAR_TYPES.includes(k.type) ? k.events : k.ready), 0);
  const [, , one, many] = SERVICE_INFO[type];
  if (ks.some((k) => k.last_error && !k.syncing)) return ["bad", "Needs attention", count ? plural(count, one, many) : ""];
  if (ks.some((k) => k.syncing || !k.last_sync || k.more || k.indexing)) return ["busy", "Importing", count ? `${plural(count, one, many)} so far` : ""];
  return ["ok", "Connected", plural(count, one, many)];
}
// Who can find a source, in two or three words. Several accounts with different circles: "Mixed".
function circleLabel(list) {
  const labels = [...new Set(list.map((k) => k.private ? "Only you" : k.company_wide ? "Everyone" : (k.departments || []).map((d) => d.name).join(", ") || "Admins only"))];
  return labels.length === 1 ? labels[0] : "Mixed";
}
// Change who can find one or more connections (an account's mail, calendar and files together, or one source)
function circleDialog(list, title, after) {
  const k = list[0];
  const f = openDialog(`<h2>${esc(title)}</h2>
    <div data-vis>${visibilityPicker(k.departments.map((d) => d.id), k.company_wide, "Who can find it?", { only: k.private })}</div><p class="error" role="alert"></p>
    <div class="dialog-actions"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="button" data-save>Save</button></div>`);
  $("[data-close]", f).onclick = closeDialog;
  $("[data-save]", f).onclick = (e) => busy(e.target, "Saving…", async () => {
    const v = readVisibility($("[data-vis]", f)); const err = visibilityError(v);
    if (err) { $(".error", f).textContent = err; return; }
    try {
      for (const x of list) await api(`/api/connectors/${x.id}`, { method: "PUT", json: { type: x.type, name: x.name, config: {}, enabled: x.enabled, ...v } });
      closeDialog(); toast("Saved."); after();
    } catch (err2) { $(".error", f).textContent = err2.message; }
  });
}
function tileConns(type) {
  if (type === "whatsapp") return conns.filter((k) => k.type === "whatsapp_business");
  return conns.filter((k) => k.type === type);
}
const CIRCLE_ICON = { "Only you": "lock", Everyone: "globe" };
function drawTiles(uploads) {
  $("#overview-tiles").innerHTML = TILES.map(([type, label, icon, target]) => {
    let [state, text, count] = tileState(type);
    if (type === "uploads") [state, text, count] = uploads ? ["ok", "Added", plural(uploads, "file", "files")] : ["", "None yet", ""];
    const action = type === "uploads" ? "Upload" : type === "website" ? "Add" : type === "linkedin" ? "Import" : "Connect";
    const ks = state ? tileConns(type) : [];
    const who = ks.length ? circleLabel(ks) : "";
    const circle = who ? `<button type="button" class="tile-circle" data-circle-tile="${type}" title="Who can find it. Click to change.">${ic(CIRCLE_ICON[who] || "people")}<span>${esc(who)}</span></button>` : "";
    return `<div class="tile ${state ? "" : "off"}" data-tile="${type}" data-target="${target}" role="button" tabindex="0">
      ${brandTile(type, icon)}<span><b>${label}</b>${text ? badge(state, text) : ""}${count ? `<div class="count">${count}</div>` : ""}${circle}
      ${state ? "" : `<button type="button" class="btn small primary tile-connect" data-connect-tile="${type}">${action}</button>`}</span></div>`;
  }).join("");
  $$("#overview-tiles .tile").forEach((t) => t.onclick = (e) => {
    const c = e.target.closest("[data-circle-tile]");
    if (c) {
      const label = TILES.find(([x]) => x === c.dataset.circleTile)[1];
      return circleDialog(tileConns(c.dataset.circleTile), `Who can find ${label}?`, () => refreshSources());
    }
    if (e.target.closest("[data-connect-tile]") || t.classList.contains("off")) return connectFromTile(t.dataset.tile);
    const el = document.getElementById(t.dataset.target) || $(`.provider-card[data-provider=${t.dataset.target}]`);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}
const TILE_SERVICE = { gmail: ["google", "mail"], gcal: ["google", "calendar"], gdrive: ["google", "files"],
  ms_mail: ["microsoft", "mail"], ms_cal: ["microsoft", "calendar"], ms_drive: ["microsoft", "files"], slack: ["slack", "messages"],
  meta_social: ["meta", "social"], youtube: ["youtube", "youtube"] };
const TILE_TITLE = { google: "Google", microsoft: "Microsoft 365", slack: "Slack", meta: "Facebook and Instagram", youtube: "YouTube" };
function connectFromTile(type) {
  if (TILE_SERVICE[type]) {
    const [provider, service] = TILE_SERVICE[type];
    return openConnectPanel(provider, [service], true);
  }
  if (type === "website") return websiteDialog();
  if (type === "whatsapp") return openWhatsAppPanel(true);
  if (type === "uploads") return uploadDialog("file");
  if (type === "linkedin") return uploadDialog("linkedin");
}
function websiteDialog() {
  const f = openDialog(`<h2>Add your website</h2>
    <p class="muted small">Cortex reads every page of the site and checks for changes by itself.</p>
    <label>Website address <input name="url" placeholder="www.yourcompany.com" dir="ltr" autocomplete="off"></label>
    <div data-vis>${visibilityPicker([], true, "Who can find its pages?")}</div>
    <p class="error" role="alert"></p>
    <div class="dialog-actions"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="button" data-save>Add website</button></div>`);
  setTimeout(() => f.url.focus(), 50);
  $("[data-close]", f).onclick = closeDialog;
  $("[data-save]", f).onclick = (e) => {
    const url = f.url.value.trim(); const v = readVisibility($("[data-vis]", f));
    const err = !url ? "Type the website address first, for example www.yourcompany.com." : visibilityError(v);
    if (err) { $(".error", f).textContent = err; return; }
    busy(e.target, "Checking the website…", async () => {
      try {
        await api("/api/connectors", { method: "POST", json: { type: "website", name: url, config: { url }, ...v } });
        closeDialog(); toast("Website added. Reading all its pages now."); refreshSources();
      } catch (err2) { $(".error", f).textContent = err2.message; }
    });
  };
}
const UPLOAD_KIND = {
  file: ["Upload files", "PDF, Word, Excel, PowerPoint, CSV, text, web pages and saved emails (.eml)."],
  linkedin: ["Import from LinkedIn", "On your LinkedIn company page: <b>Analytics → Content</b>, choose the dates, click <b>Export</b>. Then choose that Excel file here."],
};
function uploadDialog(kind) {
  const [title, help] = UPLOAD_KIND[kind];
  const f = openDialog(`<h2>${title}</h2><p class="muted small">${help}</p>
    <label class="btn" style="display:inline-flex">${ic("upload")}Choose file${kind === "file" ? "s" : ""}
      <input type="file" name="files" ${kind === "file" ? "multiple" : 'accept=".xls,.xlsx,.csv"'} hidden></label>
    <span class="muted small" data-names></span>
    <div data-vis>${visibilityPicker([], false, "Who can find it?", { only: me.role !== "admin" })}</div>
    <p class="error" role="alert"></p>
    <div class="dialog-actions"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="button" data-save>Upload</button></div>`);
  f.files.onchange = () => { $("[data-names]", f).textContent = [...f.files.files].map((x) => x.name).join(", "); };
  $("[data-close]", f).onclick = closeDialog;
  $("[data-save]", f).onclick = (e) => {
    const files = [...f.files.files]; const v = readVisibility($("[data-vis]", f));
    const err = !files.length ? "Choose a file first." : visibilityError(v);
    if (err) { $(".error", f).textContent = err; return; }
    const body = new FormData();
    files.forEach((x) => body.append("files", x));
    body.append("department_ids", v.department_ids.join(",")); body.append("company_wide", v.company_wide ? "true" : "false");
    body.append("private", v.private ? "true" : "false");
    body.append("kind", kind);
    busy(e.target, "Uploading…", async () => {
      try {
        await api("/api/documents", { method: "POST", body });
        closeDialog(); toast(`Uploaded ${plural(files.length, "file", "files")}. They'll be searchable in a moment.`);
        if (me.role === "admin") { await loadFiles(true); refreshSources(); } else LOADERS.mine();
      } catch (err2) { $(".error", f).textContent = err2.message; }
    });
  };
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
    empty: "Nothing found. It may not be imported yet. Check the status of its source on this page." });
  $("[data-q]", f).addEventListener("input", debounce(load));
  $("[data-close]", f).onclick = closeDialog;
  await load();
});

async function refreshSources(withFiles = false) {
  conns = await getJSON("/api/connectors");
  overviewNow = await getJSON("/api/overview");
  const o = overviewNow;
  linkedinFiles = await api("/api/documents?source=uploads&kind=social&limit=1").then((r) => Number(r.headers.get("X-Total-Count") || 0)).catch(() => linkedinFiles);
  try {
    const up = await api("/api/documents?source=uploads&limit=1");
    drawTiles(Number(up.headers.get("X-Total-Count") || 0));
  } catch { drawTiles(0); }
  linkedinFiles = await api("/api/documents?source=uploads&kind=social&limit=1").then((r) => Number(r.headers.get("X-Total-Count") || 0)).catch(() => 0);
  $("#li-status").innerHTML = linkedinFiles ? `<p class="muted small wa-status">${plural(linkedinFiles, "LinkedIn file", "LinkedIn files")} imported. Listed under Files and recordings.</p>` : "";
  const imported = await api("/api/documents?source=uploads&kind=whatsapp&limit=1").then((r) => Number(r.headers.get("X-Total-Count") || 0)).catch(() => 0);
  $("#wa-status").innerHTML = imported ? `<span class="muted small">${plural(imported, "chat", "chats")} imported from the phone. Listed under Files and recordings.</span>` : "";
  const importing = o.importing || conns.some((k) => k.syncing || !k.last_sync || k.more);
  $("#kb-summary").innerHTML = `<div class="toolbar"><h3>In the knowledge base now</h3></div>
    ${o.total ? `<div class="chips">${knowledgeChips(o.ready)}</div>` : '<p class="muted">Nothing yet. Connect an account, add your website, or upload files below.</p>'}
    ${importing ? `<p class="status-detail">${badge("busy", "Working")} ${o.importing ? `${plural(o.importing, "item is", "items are")} being prepared for search${o.eta ? ` (about ${esc(o.eta)} left)` : ""}.` : "Importing from your sources."} New items become searchable automatically. You can already ask questions.</p>` : ""}`;
  if (!editing) { drawProvider("google"); drawProvider("microsoft"); drawProvider("slack"); drawProvider("meta"); drawProvider("youtube"); drawWebsites(); drawManual(); drawWhatsApp(); }
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
  $("[data-open-connect]", card).onclick = () => openConnectPanel(provider);

  $(".pc-accounts", card).innerHTML = Object.entries(accounts).map(([accountId, list]) => {
    const first = list[0];
    const anyBusy = list.some((k) => k.syncing);
    const rows = Object.entries(PROVIDER_TYPES[provider]).map(([svc, type]) => {
      const k = list.find((x) => x.type === type);
      const [icon, label] = SERVICE_INFO[type];
      const name = `<div class="svc-name">${brandTile(type, icon, "sm")}${label}</div>`;
      if (!k) return `<div class="svc">${name}<div class="status-line">${badge("", "Not connected")}</div>
        <div><button class="btn small" data-add-svc="${svc}" data-account="${accountId}">${ic("plus")}Connect</button></div></div>`;
      return `<div class="svc">${name}<div>${sourceStatus(k)}${socialAccounts(k)}</div>
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
    $(".vis-slot", acc).innerHTML = `<div class="panel" style="margin:0 16px 16px">${visibilityPicker(list[0].departments.map((d) => d.id), list[0].company_wide, "Who can find this?", { only: list[0].private })}
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
      text: `Everything imported from this ${noun} will be removed from Cortex, and its sign-in will be forgotten. Nothing is deleted in the ${noun} itself.` });
    if (!ok) return;
    await busy(b, "Disconnecting…", async () => {
      try { for (const k of list) await api(`/api/connectors/${k.id}`, { method: "DELETE" }); toast("Disconnected."); } catch (err) { toast(err.message, true); }
    });
    refreshSources();
  });
  $$("[data-add-svc]", card).forEach((b) => b.onclick = () => {
    const list = accounts[b.dataset.account];
    startSignIn(oauthOf(provider), [b.dataset.addSvc], { department_ids: list[0].departments.map((d) => d.id), company_wide: list[0].company_wide }, b);
  });
}

// One-time setup of a sign-in app, done right here: step-by-step, then the keys are checked with the provider.
const SETUP_STEPS = {
  meta: (r) => `<ol class="setup-steps">
      <li>Open <a href="https://developers.facebook.com/apps" target="_blank" rel="noopener">developers.facebook.com/apps</a> (log in with your Facebook account) and click <b>Create app</b>.</li>
      <li>Name it <b>Cortex</b>. For the use case choose <b>Other</b>, then app type <b>Business</b>, and click <b>Create app</b>.</li>
      <li>On the app's dashboard, find <b>Facebook Login for Business</b> and click <b>Set up</b>.</li>
      <li>In the left menu open <b>Facebook Login for Business → Settings</b>. In <b>Valid OAuth Redirect URIs</b> paste this address, then <b>Save changes</b>:
        ${copyRow("Return address", r)}</li>
      <li>Open <b>Facebook Login for Business → Configurations → Create configuration</b>. Name: Cortex. Login variation: <b>General</b>.
        Access token: <b>User access token</b>. Assets: tick <b>Pages</b> and <b>Instagram accounts</b>. Permissions: tick
        <i>pages_show_list, pages_read_engagement, pages_read_user_content, instagram_basic, instagram_manage_insights, business_management</i>.
        Click <b>Create</b>, then copy the <b>Configuration ID</b>.</li>
      <li>Open <b>App settings → Basic</b>. Copy the <b>App ID</b>, and the <b>App secret</b> (click <b>Show</b>).</li>
    </ol>
    <p class="muted small">While the app is in Development mode, only you (and people you add under <b>App roles</b>) can connect. Enough for your own business.
      Selling to customers needs Meta's free App Review.</p>`,
  google: (r) => `<ol class="setup-steps">
      <li>Open <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">console.cloud.google.com → Credentials</a> in your Google project.</li>
      <li>Click <b>Create credentials → OAuth client ID</b>, type <b>Web application</b>, and under <b>Authorized redirect URIs</b> add: ${copyRow("Return address", r)}</li>
      <li>Click <b>Create</b> and copy the <b>Client ID</b> and <b>Client secret</b>.</li>
      <li>In <b>APIs &amp; Services → Library</b>, enable <b>Gmail API</b>, <b>Google Calendar API</b>, <b>Google Drive API</b> and <b>YouTube Data API v3</b>.</li>
    </ol>`,
  microsoft: (r) => `<ol class="setup-steps">
      <li>Open <a href="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade" target="_blank" rel="noopener">entra.microsoft.com → App registrations</a> → <b>New registration</b>.</li>
      <li>Name: Cortex. Account types: <b>any organizational directory and personal Microsoft accounts</b>. Redirect URI: platform <b>Web</b>: ${copyRow("Return address", r)}</li>
      <li>Copy the <b>Application (client) ID</b>. Then <b>Certificates &amp; secrets → New client secret</b> and copy its <b>Value</b>.</li>
      <li><b>API permissions → Add → Microsoft Graph → Delegated</b>: Mail.Read, Calendars.Read, Files.Read.All, User.Read, offline_access.</li>
    </ol>`,
  slack: (r) => `<ol class="setup-steps">
      <li>Open <a href="https://api.slack.com/apps" target="_blank" rel="noopener">api.slack.com/apps</a> → <b>Create New App → From scratch</b>, name Cortex.</li>
      <li><b>OAuth &amp; Permissions</b>: add the redirect URL ${copyRow("Return address", r)} and the bot scopes channels:read, channels:history, channels:join, groups:read, groups:history, users:read.</li>
      <li><b>Basic Information</b>: copy the <b>Client ID</b> and <b>Client Secret</b>.</li>
    </ol>
    ${/^https:/.test(r) ? "" : `<div class="warn-box">${ic("alert")} Slack only accepts <b>https</b> return addresses, so Slack can be connected once Cortex runs online.</div>`}`,
};
async function setupNeededDialog(card, then = null) {
  const provider = oauthOf(card);
  const name = PROVIDER_NAME[provider];
  let info = { redirect_url: oauthState.redirect_url, apps: {} };
  try { info = await getJSON("/api/oauth/apps"); } catch {}
  const f = openDialog(`<h2>Set up ${name} sign-in (one time)</h2>
    <p class="muted">${name} needs to know about Cortex before anyone can connect. It's free and takes about 10 minutes. Follow the steps, then paste what you copied below.</p>
    ${SETUP_STEPS[provider](info.redirect_url)}
    <div class="grid2">
      <label>${provider === "meta" ? "App ID" : provider === "microsoft" ? "Application (client) ID" : "Client ID"}
        <input name="client_id" autocomplete="off" data-1p-ignore data-lpignore="true"></label>
      <label>${provider === "meta" ? "App secret" : "Client secret"}${provider === "microsoft" ? " (Value)" : ""}
        <input name="client_secret" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"></label>
      ${provider === "meta" ? `<label>Configuration ID <input name="config_id" autocomplete="off" inputmode="numeric"></label>` : ""}
    </div>
    <p class="error" role="alert"></p>
    <div class="dialog-actions"><button class="btn" type="button" data-close>Cancel</button>
      <button class="btn primary" type="button" data-save>Save and check</button></div>`);
  $("[data-close]", f).onclick = closeDialog;
  $("[data-save]", f).onclick = (e) => busy(e.target, `Checking with ${name}…`, async () => {
    $(".error", f).textContent = "";
    const body = { client_id: f.client_id.value, client_secret: f.client_secret.value, config_id: f.config_id?.value || "" };
    try {
      await api(`/api/oauth/apps/${provider}`, { method: "PUT", json: body });
      oauthState = await getJSON("/api/oauth/status");
      toast(`${name} sign-in is set up.`);
      closeDialog();
      openConnectPanel(card, then, true);
    } catch (err) { $(".error", f).textContent = err.message; }
  });
}

// Email and calendar are personal: "Only me" unless the person shares them (e.g. a team mailbox)
function personalOnly(provider, services) {
  if (!["google", "microsoft"].includes(provider)) return false;
  const picked = services || PROVIDER_SERVICES[provider].map(([v]) => v);
  return picked.some((s) => s === "mail" || s === "calendar");
}
function openConnectPanel(provider, only = null, inDialog = false) {
  if (!oauthState.configured[oauthOf(provider)]) return setupNeededDialog(provider, only);
  editing = !inDialog;
  const slot = inDialog ? openDialog(`<h2>Connect ${esc(TILE_TITLE[provider] || PROVIDER_NAME[provider])}</h2><div data-slot></div>`).querySelector("[data-slot]")
    : $(`.provider-card[data-provider=${provider}] .pc-panel`);
  slot.innerHTML = `<div class="panel">
    <span class="label">What should Cortex read?</span>
    ${PROVIDER_SERVICES[provider].map(([value, label, hint]) => `<label class="check"><input type="checkbox" name="svc" value="${value}" ${!only || only.includes(value) ? "checked" : ""}> ${label}
      ${hint ? `<span class="muted small">(${hint})</span>` : ""}</label>`).join("<br>")}
    <div data-vis-slot>${visibilityPicker([], ["meta", "youtube", "slack"].includes(provider), "Who can find it?", { only: personalOnly(provider, only) })}</div>
    <p class="muted small">You'll sign in on ${PROVIDER_NAME[provider]}'s own page. Cortex only reads. It never sends, posts, changes or deletes anything.
      ${oauthOf(provider) === "google" ? "If Google says the app isn't verified, click <b>Continue</b>, then tick <b>all</b> the boxes on the next page." : ""}
      ${provider === "meta" ? "On Facebook, pick your business Page <b>and</b> its Instagram account when asked, and leave every permission switched on." : ""}
      ${provider === "youtube" ? "Sign in with the Google account that owns the channel. If you already connected Gmail with that account, it stays connected." : ""}</p>
    <div class="row"><button class="btn primary" data-go>Continue to ${PROVIDER_NAME[oauthOf(provider)]}</button><button class="btn" data-cancel>Cancel</button></div>
    <p class="error" role="alert"></p></div>`;
  $("[data-cancel]", slot).onclick = () => { if (inDialog) return closeDialog(); editing = false; slot.innerHTML = ""; refreshSources(); };
  $("[data-go]", slot).onclick = (e) => {
    const services = $$("input[name=svc]:checked", slot).map((x) => x.value);
    const v = readVisibility(slot);
    const err = !services.length ? "Tick at least one thing to read." : visibilityError(v);
    if (err) { $(".error", slot).textContent = err; return; }
    startSignIn(oauthOf(provider), services, v, e.target, $(".error", slot));
  };
}
// Facebook / Instagram / YouTube accounts behind one connection, with their headline numbers
function socialAccounts(k) {
  const list = k.social_accounts || [];
  if (!list.length) return "";
  const icon = { Facebook: "share", Instagram: "share", YouTube: "play" };
  return `<div class="social-list">${list.map((a) => `<div class="social-acc">
      ${brandTile(a.platform.toLowerCase(), "share", "sm")}
      <div class="grow"><b dir="auto">${a.link ? `<a href="${esc(a.link)}" target="_blank" rel="noopener">${esc(a.handle || a.name)}</a>` : esc(a.handle || a.name)}</b>
        <div class="muted small">${num(a.followers)} ${a.platform === "YouTube" ? "subscribers" : "followers"} · ${plural(a.posts, a.platform === "YouTube" ? "video" : "post", a.platform === "YouTube" ? "videos" : "posts")}
          ${a.last_post ? ` · last ${a.platform === "YouTube" ? "video" : "post"} ${ago(a.last_post)}` : ""}</div></div>
      <div class="social-week"><b>${num(a.likes_week)}</b><span class="muted small">likes on ${plural(a.posts_week, "post", "posts")} this week</span></div>
    </div>`).join("")}</div>`;
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
// ---------- WhatsApp Business (official Meta connection)
function isPublicAddress() { return location.protocol === "https:" && !/^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(location.hostname); }
function copyRow(label, value) {
  return `<div class="small muted">${esc(label)}</div><div class="copy-row"><code>${esc(value)}</code>
    <button type="button" class="btn small" data-copy="${esc(value)}">Copy</button></div>`;
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied."); } catch { toast("Select the text and copy it.", true); }
});
function drawWhatsApp() {
  const list = conns.filter((k) => k.type === "whatsapp_business");
  $("#wa-connect").innerHTML = list.length ? `${ic("plus")}Connect another number` : "Connect WhatsApp Business";
  $("#wa-connect").classList.toggle("primary", !list.length);
  $("#wa-list").innerHTML = list.map((k) => `<div class="account">
      <div class="acc-head"><div><b>${esc(k.config.number || k.name)}</b>
        <div class="muted small">Who can find this: ${visibilityPills(k)}</div></div>
        <div class="acc-actions">${k.documents ? `<button class="btn link" data-items="${k.id}">View conversations</button>` : ""}
          <button class="btn link danger" data-wa-del="${k.id}">Disconnect</button></div></div>
      <div class="svc"><div class="svc-name">${brandTile("whatsapp", "chat", "sm")}Messages</div>
        <div>${k.last_error ? `${badge("bad", "Needs attention")}<div class="status-detail bad">${esc(k.last_error)}</div>`
          : `<div class="status-line">${badge("ok", "Connected")} <span class="state-text ok small">${k.documents ? `${plural(k.events, "message", "messages")} so far` : "Waiting for the first message"}</span></div>`}
          ${isPublicAddress() ? "" : `<div class="warn-box">${ic("alert")} Meta can only deliver messages to a public <b>https</b> address. On this computer
            (${esc(location.origin)}) messages won't arrive. This starts working once Cortex runs online.</div>`}
          <details><summary class="small">Webhook details for Meta</summary>
            ${copyRow("Callback URL", `${location.origin}/webhooks/whatsapp/${k.id}`)}${copyRow("Verify token", k.config.verify_token || "")}
            <p class="muted small">In your Meta app: WhatsApp → Configuration → Webhook → Edit, paste both, then subscribe to <b>messages</b>
              (and <b>smb_message_echoes</b> if the number also uses the WhatsApp Business app).</p></details></div><div></div></div></div>`).join("");
  $$("#wa-list [data-items]").forEach((b) => b.onclick = () => itemsDialog(conns.find((k) => k.id === Number(b.dataset.items))));
  $$("[data-wa-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmBox({ title: "Disconnect this WhatsApp number?", text: "Its conversations will be removed from Cortex. Nothing changes in WhatsApp.", confirm: "Disconnect", danger: true })) return;
    await api(`/api/connectors/${b.dataset.waDel}`, { method: "DELETE" }); toast("Disconnected."); refreshSources();
  });
}
function openWhatsAppPanel(inDialog = false) {
  editing = !inDialog;
  const host = inDialog ? openDialog(`<h2>Connect WhatsApp Business</h2><div data-slot></div>`).querySelector("[data-slot]") : $("#wa-panel");
  host.innerHTML = `<div class="panel"><form id="wa-form">
    <span class="label">Connect a WhatsApp Business number</span>
    <ol class="wa-steps small">
      <li>Go to <a href="https://developers.facebook.com/apps" target="_blank" rel="noopener">developers.facebook.com/apps</a> → <b>Create app</b> → type <b>Business</b> → add the <b>WhatsApp</b> product, and add your business number.</li>
      <li>In <b>WhatsApp → API Setup</b>, copy the <b>Phone number ID</b>.</li>
      <li>In <b>Meta Business Settings → System users</b>, add a user, then <b>Generate token</b> for this app with
        <i>whatsapp_business_messaging</i> and <i>whatsapp_business_management</i>. Copy the token.</li>
      <li>Optional, for extra security: <b>App settings → Basic → App secret</b>.</li>
    </ol>
    ${isPublicAddress() ? "" : `<div class="warn-box">${ic("alert")} This page runs at ${esc(location.origin)}. Meta can only deliver messages to a public
      <b>https</b> address, so messages won't arrive here yet. You can still save the connection now. It starts working once Cortex runs online.</div>`}
    <div class="grid3">
      <label>Phone number ID <input name="phone_number_id" autocomplete="off" inputmode="numeric"></label>
      <label class="span2">Access token <input name="access_token" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"></label>
      <label>App secret (optional) <input name="app_secret" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"></label>
    </div>
    ${visibilityPicker([], false)}
    <div class="row"><button class="btn primary" type="submit">Check and connect</button><button class="btn" type="button" data-cancel>Cancel</button></div>
    <p class="error" role="alert"></p></form></div>`;
  const f = $("#wa-form");
  $("[data-cancel]", f).onclick = () => { if (inDialog) return closeDialog(); editing = false; host.innerHTML = ""; refreshSources(); };
  f.onsubmit = (e) => {
    e.preventDefault();
    const v = readVisibility(f);
    const err = !f.phone_number_id.value.trim() || !f.access_token.value.trim() ? "Fill in the Phone number ID and the access token." : visibilityError(v);
    if (err) { $(".error", f).textContent = err; return; }
    busy($("button[type=submit]", f), "Checking with Meta…", async () => {
      try {
        await api("/api/connectors", { method: "POST", json: { type: "whatsapp_business", name: "WhatsApp Business", ...v,
          config: { phone_number_id: f.phone_number_id.value.trim(), access_token: f.access_token.value.trim(), app_secret: f.app_secret.value.trim() } } });
        editing = false; if (inDialog) closeDialog(); else host.innerHTML = "";
        toast("WhatsApp Business connected. Now add the webhook details in Meta (shown below).");
        await refreshSources();
        $("#wa-list details")?.setAttribute("open", "");
      } catch (err2) { $(".error", f).textContent = err2.message; }
    });
  };
}
$("#wa-connect").addEventListener("click", () => openWhatsAppPanel());

const uploadForm = $("#upload-form");
$("#li-upload").addEventListener("click", () => uploadDialog("linkedin"));
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
  body.append("private", v.private ? "true" : "false");
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
    $("#conn-visibility").innerHTML = visibilityPicker(k.departments.map((d) => d.id), k.company_wide, "Who can find this?", { only: k.private });
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
      <div class="title">${esc(u.name || u.email)} ${u.role === "admin" ? badge("info", "Admin") : u.role === "learner" ? badge("busy", "New employee") : ""} ${u.active ? "" : badge("", "Disabled")}</div>
      <div class="meta">${esc(u.email)} · ${u.role === "admin" ? "Sees everything" : u.role === "learner" ? "Sees only the Learning Center" : (u.departments.map((d) => `<span class="pill">${esc(d.name)}</span>`).join("") || "Only information shared with everyone")}</div></div></div>
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
    <label>Role <select name="role"><option value="member">Team member: finds information for their departments</option>
      <option value="learner">New employee: sees only the Learning Center</option>
      <option value="admin">Admin: sees everything and manages Cortex</option></select></label>
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
        const details = `Cortex, ${appStatus.company || "our company"}\nAddress: ${location.origin}\nEmail: ${body.email}\nPassword: ${body.password}`;
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
// ================================================================== Settings
let lastHealth = null;
LOADERS.health = async () => { await drawHealth(); await drawAiKey(); };
LOADERS.settings = async function loadSettings() {
  const st = await (await fetch("/api/status")).json();
  appStatus = { ...appStatus, ...st };
  const f = $("#company-form");
  f.name.value = st.company || ""; f.website.value = st.website || "";
  drawLogoPreview();
};
function drawLogoPreview() {
  $("#logo-preview").innerHTML = appStatus.logo ? `<img src="${esc(appStatus.logo)}" class="tone-${esc(appStatus.logo_tone || "dark")}" alt="Logo">` : "No logo yet";
}
async function refreshBrand() {
  appStatus = { ...appStatus, ...(await (await fetch("/api/status")).json()) };
  drawBrand(); drawLogoPreview();
}
$("#company-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target; $("#company-error").textContent = "";
  busy($("button[type=submit]", f), "Saving…", async () => {
    try {
      const r = await (await api("/api/admin/company", { method: "PUT", json: { name: f.name.value, website: f.website.value } })).json();
      await refreshBrand();
      toast(r.logo_found === false ? "Saved. No logo was found on the website. You can upload it." : "Saved.");
    } catch (err) { $("#company-error").textContent = err.message; }
  });
});
$("#logo-find").addEventListener("click", (e) => busy(e.target, "Looking…", async () => {
  $("#company-error").textContent = "";
  try { await api("/api/admin/company/logo/find", { method: "POST" }); await refreshBrand(); toast("Logo updated from the website."); }
  catch (err) { $("#company-error").textContent = err.message; }
}));
$("#logo-file").addEventListener("change", async (e) => {
  const file = e.target.files[0]; if (!file) return;
  const body = new FormData(); body.append("file", file);
  $("#company-error").textContent = "";
  try { await api("/api/admin/company/logo", { method: "POST", body }); await refreshBrand(); toast("Logo uploaded."); }
  catch (err) { $("#company-error").textContent = err.message; }
  e.target.value = "";
});
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
  // Only shown when answers can't work: no key yet, or the key stopped working.
  $("#ai-card").classList.toggle("hidden", k.check.state === "ok");
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
      await drawHealth(); await drawAiKey();
    } catch (err) { $("#ai-error").textContent = err.message; }
  });
});
$("#health-refresh").addEventListener("click", (e) => busy(e.target, "Checking…", drawHealth));
$("#health-download").addEventListener("click", () => {
  const report = { generated: new Date().toISOString(), company: appStatus.company, browser: navigator.userAgent, ...lastHealth };
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" })),
    download: `cortex-health-${new Date().toISOString().slice(0, 10)}.json` });
  a.click();
});

boot();


// ================================================================== Learning Center
LOADERS.learning = async function loadLearning() {
  const admin = me.role === "admin";
  const path = await getJSON("/api/learning");
  $("#learn-intro").textContent = admin
    ? "Choose what new employees should read first, in order. People with the New employee role see only these items. And their questions are answered only from them."
    : me.role === "learner" ? "Welcome! Go through these items in order. Mark each one as done when you've read it. And ask anything about them on the Ask page."
    : "The company's onboarding path. A good place to start, or to refresh what matters most.";
  $("#learn-add").classList.toggle("hidden", !admin);
  $("#learn-people-card").classList.toggle("hidden", !admin);
  const pct = path.total ? Math.round((path.done / path.total) * 100) : 0;
  $("#learn-progress").innerHTML = path.total && !admin ? `<div class="learn-progress"><div><b>${path.done} of ${path.total}</b> done</div>${progressBar(path.done, path.total)}
      ${path.done === path.total ? `<div class="status-detail ok">${ic("check")} All done. Well done!</div>` : ""}</div>` : "";
  $("#learn-list").innerHTML = path.items.length ? path.items.map((it, i) => `<div class="learn-item ${it.done ? "done" : ""}" data-id="${it.id}">
      <span class="step">${it.done && !admin ? ic("tick") : i + 1}</span>
      <div class="grow"><div class="title">${it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a>` : `<a href="#" data-doc="${it.document_id}">${esc(it.title)}</a>`}
        <span class="muted small">· ${esc(kindName(it.kind))}</span></div>
        ${it.note ? `<div class="learn-note" dir="auto">${esc(it.note)}</div>` : ""}</div>
      <div class="acc-actions">${admin ? `
        <button class="btn small icon-only" type="button" data-move="-1" title="Move up" aria-label="Move up" ${i === 0 ? "disabled" : ""}>${ic("up-arrow")}</button>
        <button class="btn small icon-only" type="button" data-move="1" title="Move down" aria-label="Move down" ${i === path.items.length - 1 ? "disabled" : ""}>${ic("down-arrow")}</button>
        <button class="btn link small" type="button" data-note>${it.note ? "Edit note" : "Add note"}</button>
        <button class="btn link small danger" type="button" data-remove>Remove</button>`
        : `<button class="btn small ${it.done ? "" : "primary"}" type="button" data-done="${it.done ? 0 : 1}">${it.done ? "Mark as not done" : "Mark as done"}</button>`}</div></div>`).join("")
    : `<div class="empty">${admin ? "Nothing in the learning path yet. Click <b>Add to the path</b> and pick the documents, pages or recordings a new employee should start with." : "Nothing here yet. Your admin is preparing it."}</div>`;
  const ids = path.items.map((x) => x.id);
  $$("#learn-list .learn-item").forEach((row) => {
    const id = Number(row.dataset.id); const it = path.items.find((x) => x.id === id);
    $("[data-done]", row)?.addEventListener("click", async (e) => {
      await busy(e.target, "Saving…", () => api(`/api/learning/${id}/done`, { method: "POST", json: { done: e.target.dataset.done === "1" } }));
      LOADERS.learning();
    });
    $$("[data-move]", row).forEach((b) => b.onclick = async () => {
      const i = ids.indexOf(id), j = i + Number(b.dataset.move);
      [ids[i], ids[j]] = [ids[j], ids[i]];
      await api("/api/learning/order", { method: "PUT", json: { ids } }); LOADERS.learning();
    });
    $("[data-remove]", row)?.addEventListener("click", async () => {
      if (!await confirmBox({ title: "Remove from the learning path?", text: `"${it.title}" stays in the knowledge base; it's only taken out of the path.`, confirm: "Remove" })) return;
      await api(`/api/learning/${id}`, { method: "DELETE" }); toast("Removed from the path."); LOADERS.learning();
    });
    $("[data-note]", row)?.addEventListener("click", () => {
      const f = openDialog(`<h2>Note for new employees</h2><p class="muted small">${esc(it.title)}</p>
        <label>What should they look for here? <textarea name="note" rows="4" dir="auto" placeholder="For example: Read the returns policy carefully. Customers ask about it every day.">${esc(it.note)}</textarea></label>
        <div class="dialog-actions"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="button" data-save>Save</button></div>`);
      $("[data-close]", f).onclick = closeDialog;
      $("[data-save]", f).onclick = async () => { await api(`/api/learning/${id}`, { method: "PUT", json: { note: f.note.value } }); closeDialog(); toast("Saved."); LOADERS.learning(); };
    });
  });
  if (admin) await drawLearningPeople();
};

async function drawLearningPeople() {
  const p = await getJSON("/api/learning/progress");
  $("#learn-people").innerHTML = p.people.length ? p.people.map((u) => `<div class="list-row"><div class="grow">
      <div class="title">${esc(u.name || u.email)} ${u.role === "learner" ? badge("busy", "New employee") : ""}</div>
      <div class="meta">${u.last_done ? `Last progress ${ago(u.last_done)}` : "Hasn't started yet"}</div></div>
      <div style="min-width:180px"><div class="small"><b>${u.done} of ${p.total}</b> done</div>${progressBar(u.done, p.total)}</div></div>`).join("")
    : `<div class="empty">No new employees yet. On the <a href="#people">People</a> page, add a person with the role <b>New employee</b>. They'll see only this path.</div>`;
}

$("#learn-add").addEventListener("click", () => {
  const f = openDialog(`<h2>Add to the learning path</h2>
    <p class="muted small">Search everything in the knowledge base and add what a new employee should read.</p>
    <div class="search-box" style="width:100%">${ic("search")}<input type="search" data-q dir="auto" placeholder="For example: onboarding, prices, safety"></div>
    <div data-list style="margin-top:8px"></div>
    <div class="dialog-actions"><button class="btn primary" type="button" data-close>Done</button></div>`);
  const list = $("[data-list]", f);
  let inPath = new Set();
  const load = async () => {
    inPath = new Set((await getJSON("/api/learning")).items.map((x) => x.document_id));
    const q = new URLSearchParams({ q: $("[data-q]", f).value, status: "ready", limit: 15 });
    const rows = await getJSON(`/api/documents?${q}`);
    list.innerHTML = rows.length ? rows.map((d) => `<div class="list-row"><span class="icon-tile sm">${ic(KIND_ICON[d.kind] || "file")}</span>
        <div class="grow"><div class="title" dir="auto">${esc(d.title)}</div><div class="meta">${esc(kindName(d.kind))}</div></div>
        ${inPath.has(d.id) ? badge("ok", "In the path") : `<button class="btn small" type="button" data-add="${d.id}">${ic("plus")}Add</button>`}</div>`).join("")
      : '<div class="empty">Nothing found.</div>';
    $$("[data-add]", list).forEach((b) => b.onclick = async () => {
      await busy(b, "Adding…", () => api("/api/learning", { method: "POST", json: { document_id: Number(b.dataset.add) } }));
      toast("Added to the learning path."); load();
    });
  };
  $("[data-q]", f).addEventListener("input", debounce(load));
  $("[data-close]", f).onclick = () => { closeDialog(); LOADERS.learning(); };
  load();
});


// ================================================================== Brain: browse everything like a drive
const APP_FOLDERS = {
  gmail: ["Gmail", "#EA4335"], gcal: ["Google Calendar", "#4285F4"], gdrive: ["Google Drive", "#1FA463"],
  ms_mail: ["Outlook", "#0A64C9"], ms_cal: ["Outlook calendar", "#0A64C9"], ms_drive: ["OneDrive", "#0364B8"],
  slack: ["Slack", "#4A154B"], meta_social: ["Facebook & Instagram", "#1877F2"], youtube: ["YouTube", "#E62117"],
  website: ["Websites", "#64748B"], whatsapp_business: ["WhatsApp Business", "#1DA851"], imap: ["Mailbox", "#475467"], ics: ["Calendar link", "#475467"],
  "upload:file": ["Uploaded files", "#4F46E5"], "upload:interview": ["Interviews", "#9333EA"], "upload:whatsapp": ["WhatsApp chats", "#1DA851"],
  "upload:social": ["LinkedIn", "#0A66C2"],
};
const APP_LOGO = { "upload:whatsapp": "whatsapp", "upload:social": "linkedin", whatsapp_business: "whatsapp" };
const TYPE_ICON = { email: "mail", file: "file", calendar: "calendar", slack: "ask", whatsapp: "chat", web: "globe", social: "share", interview: "mic" };
const brainState = { folder: null, offset: 0, items: [], total: 0, selected: null, data: null };

function folderIcon(key, color, big = true) {
  const logo = APP_LOGO[key] || key;
  const badge = BRAND_SVG[logo] || SOURCE_BRAND[logo] ? `<span class="folder-badge">${(SOURCE_BRAND[logo] || [logo]).slice(0, 1).map((m) => `<svg viewBox="0 0 24 24">${BRAND_SVG[m]}</svg>`).join("")}</span>` : "";
  return `<span class="folder ${big ? "" : "sm"}" style="--fc:${color}"><svg viewBox="0 0 64 50" aria-hidden="true">
      <path d="M2 7a4 4 0 0 1 4-4h15.5a4 4 0 0 1 3 1.4L28 8h30a4 4 0 0 1 4 4v3H2z" fill="var(--fc)" opacity=".72"/>
      <rect x="2" y="12" width="60" height="36" rx="4" fill="var(--fc)"/><rect x="2" y="12" width="60" height="5" rx="2" fill="#fff" opacity=".14"/></svg>${badge}</span>`;
}
function folderFor(key) {
  const d = brainState.data;
  const app = d.apps.find((f) => f.key === key);
  if (app) { const [label, color] = APP_FOLDERS[key] || [app.label || key, "#64748B"];
    return { key, label: app.label || label, color, count: app.count, query: key.startsWith("upload:") ? { source: "uploads", kind: key.slice(7) } : { ctype: key } }; }
  const t = d.types.find((f) => "type:" + f.key === key);
  if (t) return { key, label: t.label, color: "#4F46E5", count: t.count, query: { kind: t.key }, icon: TYPE_ICON[t.key] };
  return null;
}
LOADERS.brain = async function loadBrain() {
  brainState.data = await getJSON("/api/brain");
  drawBrainSide();
  if (brainState.folder && !folderFor(brainState.folder.key)) brainState.folder = null;
  await drawBrainView(true);
};
function drawBrainSide() {
  const d = brainState.data;
  const item = (key, label, count, iconHtml) => `<a href="#" class="fs-item ${(brainState.folder?.key || "") === key ? "active" : ""}" data-folder="${key}">${iconHtml}<span class="grow">${esc(label)}</span><span class="fs-count">${num(count)}</span></a>`;
  $("#brain-side").innerHTML = item("", "Company Brain", d.total, `<img src="/static/cortex.svg" alt="" class="fs-logo">`)
    + (d.apps.length ? `<div class="fs-group">Connected apps</div>` + d.apps.map((f) => { const ff = folderFor(f.key); return item(f.key, ff.label, f.count, folderIcon(f.key, ff.color, false)); }).join("") : "")
    + (d.types.length ? `<div class="fs-group">By type</div>` + d.types.map((f) => item("type:" + f.key, f.label, f.count, `<span class="fs-ic">${ic(TYPE_ICON[f.key] || "file")}</span>`)).join("") : "");
  $$("#brain-side [data-folder]").forEach((a) => a.onclick = (e) => { e.preventDefault(); openFolder(a.dataset.folder); });
}
function openFolder(key) {
  brainState.folder = key ? folderFor(key) : null;
  brainState.selected = null;
  $("#brain-search").value = "";
  $("#brain-preview").classList.add("hidden");
  drawBrainSide();
  drawBrainView(true);
}
async function drawBrainView(reset) {
  const d = brainState.data, f = brainState.folder, q = $("#brain-search").value.trim();
  $("#brain-crumbs").innerHTML = `<a href="#" data-folder="">${ic("brain")}Company Brain</a>${f ? `<span class="sep">›</span><b>${esc(f.label)}</b>` : ""}${q ? `<span class="sep">›</span><span>Search: “${esc(q)}”</span>` : ""}`;
  $$("#brain-crumbs [data-folder]").forEach((a) => a.onclick = (e) => { e.preventDefault(); openFolder(""); });
  const view = $("#brain-view");
  if (!f && !q) {   // the top level: one folder per connected app, then by type
    const tile = (ff) => `<a href="#" class="folder-tile" data-folder="${ff.key}">${ff.icon ? `<span class="folder" style="--fc:${ff.color}"><svg viewBox="0 0 64 50"><path d="M2 7a4 4 0 0 1 4-4h15.5a4 4 0 0 1 3 1.4L28 8h30a4 4 0 0 1 4 4v3H2z" fill="var(--fc)" opacity=".72"/><rect x="2" y="12" width="60" height="36" rx="4" fill="var(--fc)"/></svg><span class="folder-glyph">${ic(ff.icon)}</span></span>` : folderIcon(ff.key, ff.color)}
      <span class="ft-name">${esc(ff.label)}</span><span class="ft-count">${num(ff.count)}</span></a>`;
    view.innerHTML = !d.total ? `<div class="empty">Nothing here yet. ${me.role === "admin" ? '<a href="#sources">Connect your first source</a>' : "Your admin is still adding the company's information."}</div>`
      : (d.apps.length ? `<h3 class="fv-title">Connected apps</h3><div class="folder-grid">${d.apps.map((a) => tile(folderFor(a.key))).join("")}</div>` : "")
      + (d.types.length ? `<h3 class="fv-title">By type</h3><div class="folder-grid">${d.types.map((t) => tile(folderFor("type:" + t.key))).join("")}</div>` : "");
    $$("#brain-view [data-folder]").forEach((a) => a.onclick = (e) => { e.preventDefault(); openFolder(a.dataset.folder); });
    $("#brain-status").innerHTML = `${ic("brain")}<span>Company Brain</span><span class="grow"></span><span>${plural(d.total, "item", "items")}</span>`;
    return;
  }
  if (reset) { brainState.offset = 0; brainState.items = []; }
  const params = new URLSearchParams({ ...(f ? f.query : {}), q, sort: "date", limit: 50, offset: brainState.offset });
  const res = await api(`/api/documents?${params}`);
  const rows = await res.json();
  brainState.total = Number(res.headers.get("X-Total-Count") || rows.length);
  brainState.items.push(...rows);
  brainState.offset += rows.length;
  const when = (r) => { const t = r.doc_date || r.updated_at; return t ? new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : ""; };
  const who = (r) => { const m = r.metadata || {}; const clean = (x) => (x || "").replace(/<[^>]*>/g, "").split(",")[0].trim();
    return r.kind === "email" ? `${esc(clean(m.from))}${m.to ? ` → ${esc(clean(m.to))}` : ""}` : esc((APP_FOLDERS[r.connector_type] || [r.source === "connector" ? "" : "Uploaded"])[0]); };
  view.innerHTML = brainState.items.length ? `<div class="file-list">${brainState.items.map((r) => `<a href="#" class="fl-row ${brainState.selected === r.id ? "active" : ""}" data-item="${r.id}">
      ${r.connector_type && BRAND_SVG[APP_LOGO[r.connector_type] || r.connector_type] ? brandTile(r.connector_type, "file", "sm") : `<span class="icon-tile sm">${ic(KIND_ICON[r.kind] || "file")}</span>`}
      <span class="fl-main"><span class="fl-title" dir="auto">${esc(r.kind === "email" ? r.title.replace(/^Email:\s*/, "") : r.title)}</span><span class="fl-meta" dir="auto">${who(r)}</span></span>
      ${isFile(r) ? `<i class="ftype">${esc(r.filename.split(".").pop().toUpperCase())}</i>` : ""}
      <span class="fl-date">${when(r)}</span></a>`).join("")}</div>
      ${brainState.items.length < brainState.total ? `<div class="fl-more"><button class="btn" type="button" id="brain-more">Show more</button></div>` : ""}`
    : `<div class="empty">${q ? "Nothing matches your search." : "This folder is empty."}</div>`;
  $$("#brain-view [data-item]").forEach((a) => a.onclick = (e) => { e.preventDefault(); showPreview(Number(a.dataset.item)); });
  $("#brain-more")?.addEventListener("click", () => drawBrainView(false));
  $("#brain-status").innerHTML = `${f ? folderIcon(f.key, f.color, false) : ic("search")}<span>${f ? esc(f.label) : "Search"}</span><span class="grow"></span><span>${plural(brainState.total, "item", "items")}</span>`;
}
$("#brain-search").addEventListener("input", debounce(() => drawBrainView(true)));

async function showPreview(id) {
  brainState.selected = id;
  $$("#brain-view .fl-row").forEach((r) => r.classList.toggle("active", Number(r.dataset.item) === id));
  const p = await getJSON(`/api/documents/${id}/preview`);
  const m = p.metadata || {};
  const when = p.doc_date || p.updated_at;
  const rows = [["From", m.from], ["To", m.to], ["Cc", m.cc], ["Date", when && new Date(when).toLocaleString()],
    ["In", m.email_subject && `Attached to “${m.email_subject}”`], ["Source", (APP_FOLDERS[p.connector_type] || [p.source === "connector" ? "" : "Uploaded"])[0]],
    ["Who can find it", p.company_wide ? "Everyone" : (p.departments || []).join(", ")]].filter(([, v]) => v);
  const file = isFile(p);
  const pane = $("#brain-preview");
  pane.classList.remove("hidden");
  pane.innerHTML = `<div class="pv-head"><span class="pv-icon">${file ? `<span class="file-icon" style="--fc:${FILE_COLORS[p.filename.split(".").pop().toUpperCase()] || "var(--accent)"}">${esc(p.filename.split(".").pop().toUpperCase().slice(0, 4))}</span>` : `<span class="icon-tile">${ic(KIND_ICON[p.kind] || "file")}</span>`}</span>
      <button class="btn link small pv-close" type="button" aria-label="Close preview" title="Close">${ic("x")}</button></div>
    <h3 class="pv-title" dir="auto">${esc(p.title)}</h3>
    <dl class="pv-meta">${rows.map(([k, v]) => `<dt>${k}</dt><dd dir="auto">${esc(String(v))}</dd>`).join("")}</dl>
    <div class="pv-actions">
      <a class="btn small" href="#" data-doc="${p.id}">Open</a>
      ${file ? `<a class="btn small" href="#" data-doc="${p.id}" data-download>${ic("download")}Download</a>` : ""}
      <button class="btn small primary" type="button" data-ask-about>${ic("ask")}Ask about this</button></div>
    <div class="pv-text" dir="auto">${p.text ? esc(p.text) : '<span class="muted">No text preview for this item.</span>'}</div>`;
  $(".pv-close", pane).onclick = () => { pane.classList.add("hidden"); brainState.selected = null; $$("#brain-view .fl-row.active").forEach((r) => r.classList.remove("active")); };
  $("[data-ask-about]", pane).onclick = () => {
    location.hash = "#ask";
    setTimeout(() => { newChat(); qBox.value = `About "${p.title}": `; qBox.focus(); qBox.dispatchEvent(new Event("input")); }, 50);
  };
}


// ================================================================== My sources: what each person brings to the company's brain
LOADERS.mine = async function loadMine() {
  try { oauthState = await getJSON("/api/oauth/status"); } catch {}
  const [rows, ups] = await Promise.all([getJSON("/api/connectors?mine=true"), api("/api/documents?source=uploads&mine=true&limit=1")]);
  const uploads = Number(ups.headers.get("X-Total-Count") || 0);
  const accounts = {};
  rows.forEach((k) => (accounts[k.config.account_id || k.id] ||= []).push(k));
  $("#mine-list").innerHTML = rows.length ? Object.values(accounts).map((list) => `<div class="account">
      <div class="acc-head"><div><b dir="auto">${esc(list[0].config.username || list[0].name)}</b></div>
        <div class="acc-actions"><button class="btn small" type="button" data-mine-sync="${list.map((k) => k.id).join(",")}">${ic("refresh")}Check for new items</button>
          <button class="btn link danger" type="button" data-mine-del="${list.map((k) => k.id).join(",")}">Disconnect</button></div></div>
      ${list.map((k) => { const [icon, label] = SERVICE_INFO[k.type] || ["file", k.name];
        return `<div class="svc"><div class="svc-name">${brandTile(k.type, icon, "sm")}${esc(label)}</div><div>${sourceStatus(k)}</div>
          <div class="mine-circle">${visibilityPills(k)} <button class="btn link small" type="button" data-mine-vis="${k.id}">Change</button></div></div>`; }).join("")}
    </div>`).join("") : '<div class="empty">Nothing connected yet. Connect your work account above, or add documents.</div>';
  $("#mine-uploads").textContent = uploads ? `${plural(uploads, "document", "documents")} added by you.` : "";
  $$("[data-mine-sync]").forEach((b) => b.onclick = async () => { b.disabled = true; for (const id of b.dataset.mineSync.split(",")) await api(`/api/connectors/${id}/sync`, { method: "POST" }); toast("Checking for new items."); setTimeout(() => LOADERS.mine(), 1500); });
  $$("[data-mine-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmBox({ title: "Disconnect this account?", text: "Everything Cortex learned from it will be removed. Nothing changes in the account itself.", confirm: "Disconnect", danger: true })) return;
    for (const id of b.dataset.mineDel.split(",")) await api(`/api/connectors/${id}`, { method: "DELETE" });
    toast("Disconnected."); LOADERS.mine();
  });
  $$("[data-mine-vis]").forEach((b) => b.onclick = () => {
    const k = rows.find((x) => x.id === Number(b.dataset.mineVis));
    circleDialog([k], `Who can find your ${(SERVICE_INFO[k.type] || ["", k.name])[1]}?`, () => LOADERS.mine());
  });
};
$$("[data-mine-connect]").forEach((b) => b.onclick = () => {
  const provider = b.dataset.mineConnect;
  if (!oauthState.configured[provider]) return toast(`${PROVIDER_NAME[provider]} sign-in isn't set up yet. Ask your admin.`, true);
  const f = openDialog(`<h2>Connect your ${PROVIDER_NAME[provider]} account</h2><div data-slot></div>`);
  const slot = $("[data-slot]", f);
  slot.innerHTML = `<div class="panel"><span class="label">What should Cortex learn from?</span>
    ${PROVIDER_SERVICES[provider].map(([value, label, hint]) => `<label class="check"><input type="checkbox" name="svc" value="${value}" checked> ${label}${hint ? ` <span class="muted small">(${hint})</span>` : ""}</label>`).join("<br>")}
    ${visibilityPicker([], false, "Who can find it?", { only: true })}
    <p class="muted small">Tip: keep email and calendar to yourself, and connect again to share Drive files with your team. You can change this any time.</p>
    <div class="row"><button class="btn primary" type="button" data-go>Continue to ${PROVIDER_NAME[provider]}</button><button class="btn" type="button" data-cancel>Cancel</button></div>
    <p class="error" role="alert"></p></div>`;
  $("[data-cancel]", slot).onclick = closeDialog;
  $("[data-go]", slot).onclick = (e) => {
    const services = $$("input[name=svc]:checked", slot).map((x) => x.value);
    const v = readVisibility(slot);
    const err = !services.length ? "Tick at least one thing." : visibilityError(v);
    if (err) { $(".error", slot).textContent = err; return; }
    startSignIn(provider, services, v, e.target, $(".error", slot));
  };
});
$("#mine-add-docs").addEventListener("click", () => uploadDialog("file"));
