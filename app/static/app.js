// KnowledgeDesk web interface (plain JavaScript, no build step).
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let token = null;
try { token = localStorage.getItem("kd_token"); } catch {}
let me = null;
let departments = [];
let history = [];

async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 && path !== "/api/login") { logout(); throw new Error("Please log in again"); }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res;
}
const getJSON = async (p) => (await api(p)).json();

function show(id) { $$("section").forEach((s) => s.classList.add("hidden")); $(id).classList.remove("hidden"); }
function logout() { token = null; try { localStorage.removeItem("kd_token"); } catch {} show("#login-view"); }

// ------------------------------------------------------------------ start
async function boot() {
  const st = await (await fetch("/api/status")).json();
  $("#login-company").textContent = st.company;
  $("#company-name").textContent = st.company;
  const lic = st.license;
  if (lic.message && lic.state !== "unchecked") { $("#banner").textContent = lic.message; $("#banner").classList.remove("hidden"); }
  if (!st.llm_configured) { $("#banner").textContent = "The AI service key (ANTHROPIC_API_KEY) is not set. Answers will not work until it is configured."; $("#banner").classList.remove("hidden"); }
  if (!token) return show("#login-view");
  try { me = await getJSON("/api/me"); } catch { return show("#login-view"); }
  $("#user-name").textContent = me.name || me.email;
  $$(".admin-only").forEach((el) => el.classList.toggle("hidden", me.role !== "admin"));
  departments = await getJSON("/api/departments");
  $("#dept-filter").innerHTML = `<option value="">All my departments</option>` +
    departments.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join("");
  show("#app-view");
  const connected = new URLSearchParams(location.search).get("connected");
  if (connected !== null && me.role === "admin") {
    history.replaceState(null, "", "/");
    $("button[data-view=admin]").click(); openSub("conns");
    $("#oauth-done").textContent = `✓ Account connected (${connected} source${connected === "1" ? "" : "s"}). Importing now – a big mailbox or drive can take a while; you can keep working.`;
    $("#oauth-done").classList.remove("hidden");
  }
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    const r = await (await api("/api/login", { method: "POST", json: { email: f.get("email"), password: f.get("password") } })).json();
    token = r.token; try { localStorage.setItem("kd_token", token); } catch {}
    await boot();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", logout);

// ------------------------------------------------------------------ navigation
$$(".tab").forEach((b) => b.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === b));
  $$(".view").forEach((v) => v.classList.toggle("hidden", v.id !== b.dataset.view));
  if (b.dataset.view === "library") loadLibrary();
  if (b.dataset.view === "admin") openSub("docs");
}));
$$(".subtab").forEach((b) => b.addEventListener("click", () => openSub(b.dataset.sub)));
function openSub(name) {
  $$(".subtab").forEach((x) => x.classList.toggle("active", x.dataset.sub === name));
  $$(".sub").forEach((s) => s.classList.toggle("hidden", s.id !== `sub-${name}`));
  ({ docs: loadDocs, conns: loadConns, depts: loadDepts, users: loadUsers, questions: loadQuestions, system: loadSystem })[name]();
}

// ------------------------------------------------------------------ chat
const qBox = $("#question");
qBox.addEventListener("input", () => { qBox.style.height = "auto"; qBox.style.height = qBox.scrollHeight + "px"; });
qBox.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask-form").requestSubmit(); } });
$("#new-chat").addEventListener("click", () => { history = []; $("#messages").innerHTML = ""; $("#messages").append(emptyState); });
const emptyState = $("#chat-empty");

function renderAnswer(text, sources) {
  // Turn [1] into hoverable citation markers.
  return esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/^#{1,4} (.+)$/gm, "<b>$1</b>").replace(/\[(\d+)\]/g, (m, n) => {
    const s = sources[Number(n) - 1];
    return s ? `<span class="cite" title="${esc(s.title + (s.location ? " – " + s.location : ""))}">[${n}]</span>` : m;
  });
}

function addMsg(cls, html) {
  emptyState.remove();
  const div = document.createElement("div");
  div.className = `msg ${cls}`; div.dir = "auto"; div.innerHTML = html;
  $("#messages").append(div);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return div;
}

$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = qBox.value.trim();
  if (!question) return;
  qBox.value = ""; qBox.style.height = "auto";
  addMsg("user", esc(question));
  const bot = addMsg("bot", `<span class="muted">Searching…</span>`);
  $("#ask-btn").disabled = true;
  const dept = $("#dept-filter").value;
  let text = "", sources = [], qid = null;
  try {
    const res = await api("/api/ask", { method: "POST", json: { question, history, department_id: dept ? Number(dept) : null } });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buf.slice(0, nl)); buf = buf.slice(nl + 1);
        if (ev.type === "sources") sources = ev.sources;
        if (ev.type === "text") { text += ev.text; bot.innerHTML = renderAnswer(text, sources); }
        if (ev.type === "error") { text += (text ? "\n\n" : "") + ev.message; bot.innerHTML = `<span class="error">${esc(ev.message)}</span>`; }
        if (ev.type === "done") qid = ev.question_id;
        $("#messages").scrollTop = $("#messages").scrollHeight;
      }
    }
  } catch (err) { bot.innerHTML = `<span class="error">${esc(err.message)}</span>`; }
  $("#ask-btn").disabled = false;
  history.push({ role: "user", content: question }, { role: "assistant", content: text });

  const used = sources.filter((s) => text.includes(`[${s.n}]`));
  if (used.length) {
    bot.insertAdjacentHTML("beforeend", `<details class="sources" open><summary>Sources</summary><ol>${
      used.map((s) => `<li value="${s.n}"><a href="#" data-doc="${s.document_id}">${esc(s.title)}</a>${s.location ? ` <span class="muted">(${esc(s.location)})</span>` : ""}</li>`).join("")}</ol></details>`);
  }
  if (qid) {
    bot.insertAdjacentHTML("beforeend", `<div class="feedback"><button data-v="1" title="Helpful">👍</button><button data-v="-1" title="Not helpful">👎</button></div>`);
    $$(".feedback button", bot).forEach((b) => b.addEventListener("click", async () => {
      $$(".feedback button", bot).forEach((x) => x.classList.toggle("chosen", x === b));
      await api(`/api/questions/${qid}/feedback`, { method: "POST", json: { value: Number(b.dataset.v) } });
    }));
  }
});

// Open source files (authenticated download).
document.addEventListener("click", async (e) => {
  const a = e.target.closest("a[data-doc]");
  if (!a) return;
  e.preventDefault();
  try {
    const res = await api(`/api/documents/${a.dataset.doc}/file`);
    const url = URL.createObjectURL(await res.blob());
    window.open(url, "_blank");
  } catch (err) { alert(err.message); }
});

// ------------------------------------------------------------------ library
let libDocs = [];
async function loadLibrary() { libDocs = await getJSON("/api/documents"); drawLibrary(); }
function drawLibrary() {
  const f = $("#lib-search").value.toLowerCase();
  $("#lib-rows").innerHTML = libDocs.filter((d) => d.status === "ready" && d.title.toLowerCase().includes(f)).map((d) => `
    <tr><td dir="auto"><a href="#" data-doc="${d.id}">${esc(d.title)}</a></td><td>${KIND[d.kind] || "Document"}</td>
    <td>${deptPills(d)}</td><td>${new Date(d.updated_at).toLocaleDateString()}</td></tr>`).join("") ||
    `<tr><td colspan="4" class="muted">No documents yet.</td></tr>`;
}
$("#lib-search").addEventListener("input", drawLibrary);
const KIND = { file: "Document", interview: "Interview", email: "Email", whatsapp: "WhatsApp", calendar: "Calendar", web: "Website" };
const deptPills = (d) => (d.company_wide ? `<span class="pill">Everyone</span>` : "") + d.departments.map((x) => `<span class="pill">${esc(x.name)}</span>`).join("");

// ------------------------------------------------------------------ admin: documents
function deptChecks(container, name, selected = []) {
  $(container).innerHTML = departments.map((d) =>
    `<label class="radio"><input type="checkbox" name="${name}" value="${d.id}" ${selected.includes(d.id) ? "checked" : ""}> ${esc(d.name)}</label>`
  ).join("") || `<span class="muted small">No departments yet – create them in the Departments tab.</span>`;
}
async function refreshDepartments() { departments = await getJSON("/api/departments"); }

async function loadDocs() {
  await refreshDepartments();
  deptChecks("#upload-depts", "dept");
  const docs = await getJSON("/api/documents");
  $("#doc-rows").innerHTML = docs.map((d) => `
    <tr><td dir="auto"><a href="#" data-doc="${d.id}">${esc(d.title)}</a><div class="muted small">${esc(d.filename)}${d.source === "folder" ? " · synced folder" : d.source === "connector" ? " · connected source" : ""}</div></td>
    <td>${KIND[d.kind] || "Document"}</td><td>${deptPills(d)}</td>
    <td class="status-${d.status}" title="${esc(d.error || "")}">${d.status}${d.status === "ready" ? ` <span class="muted small">(${d.chunks} parts)</span>` : ""}${d.error ? `<div class="small">${esc(d.error)}</div>` : ""}</td>
    <td><button class="link" data-reindex="${d.id}">Re-index</button> <button class="danger" data-del="${d.id}">Delete</button></td></tr>`).join("") ||
    `<tr><td colspan="5" class="muted">Nothing uploaded yet.</td></tr>`;
  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this document from the knowledge base?")) return;
    await api(`/api/documents/${b.dataset.del}`, { method: "DELETE" }); loadDocs();
  });
  $$("[data-reindex]").forEach((b) => b.onclick = async () => { await api(`/api/documents/${b.dataset.reindex}/reindex`, { method: "POST" }); loadDocs(); });
  if (docs.some((d) => d.status === "pending" || d.status === "processing")) setTimeout(() => { if (!$("#sub-docs").classList.contains("hidden")) loadDocs(); }, 4000);
}
$("#refresh-docs").addEventListener("click", loadDocs);
$("#rescan").addEventListener("click", async () => { const r = await (await api("/api/admin/rescan", { method: "POST" })).json(); alert(`${r.queued} new or changed files queued.`); loadDocs(); });
$$("input[name=kind]").forEach((r) => r.addEventListener("change", () =>
  $("#interview-fields").classList.toggle("hidden", $("input[name=kind]:checked").value !== "interview")));

$("#upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target; const f = new FormData(form);
  const body = new FormData();
  for (const file of form.files.files) body.append("files", file);
  body.append("department_ids", $$("input[name=dept]:checked", form).map((x) => x.value).join(","));
  body.append("company_wide", form.company_wide.checked ? "true" : "false");
  body.append("kind", f.get("kind"));
  for (const k of ["interviewee", "role", "interview_date"]) body.append(k, f.get(k) || "");
  $("#upload-error").textContent = "";
  const btn = $("button[type=submit]", form); btn.disabled = true; btn.textContent = "Uploading…";
  try { await api("/api/documents", { method: "POST", body }); form.reset(); $("#interview-fields").classList.add("hidden"); loadDocs(); }
  catch (err) { $("#upload-error").textContent = err.message; }
  btn.disabled = false; btn.textContent = "Upload and index";
});

// ------------------------------------------------------------------ admin: connections
const connForm = $("#conn-form");
const TYPE_LABEL = { imap: "📧 Email", ics: "📅 Calendar", website: "🌐 Website", gmail: "📧 Gmail", gcal: "📅 Google Calendar",
  gdrive: "📁 Google Drive", ms_mail: "📧 Outlook mail", ms_cal: "📅 Outlook calendar", ms_drive: "📁 OneDrive" };
const OAUTH_TYPES = ["gmail", "gcal", "gdrive", "ms_mail", "ms_cal", "ms_drive"];

async function loadQuickConnect() {
  deptChecks("#oauth-depts", "odept");
  try {
    const st = await getJSON("/api/oauth/status");
    $$("button.provider").forEach((b) => (b.disabled = !st.configured[b.dataset.provider]));
    const missing = Object.entries(st.configured).filter(([, ok]) => !ok).map(([p]) => p === "google" ? "Google" : "Microsoft");
    $("#oauth-setup-note").innerHTML = missing.length ? `${missing.join(" and ")} sign-in isn't set up on this installation yet
      (README → "Setting up one-click sign-in"). Address to register: <code>${esc(st.redirect_url)}</code>` : "";
  } catch (err) { $("#oauth-setup-note").textContent = err.message; }
}
$$("button.provider").forEach((b) => b.addEventListener("click", async () => {
  $("#oauth-error").textContent = "";
  try {
    const r = await (await api("/api/oauth/start", { method: "POST", json: {
      provider: b.dataset.provider,
      services: $$("input[name=svc]:checked").map((x) => x.value),
      department_ids: $$("input[name=odept]:checked").map((x) => Number(x.value)),
      company_wide: $("#oauth-everyone").checked } })).json();
    window.location.href = r.url;
  } catch (err) { $("#oauth-error").textContent = err.message; }
}));
let conns = [];
function connType() { return $("input[name=type]:checked", connForm).value; }
function showConnFields() {
  const t = connType();
  $$(".conn-fields", connForm).forEach((el) => {
    const on = el.dataset.for === t;
    el.classList.toggle("hidden", !on);
    $$("input", el).forEach((i) => (i.disabled = !on));
  });
  const isWa = t === "whatsapp";
  $(".conn-visibility", connForm).classList.toggle("hidden", isWa);
  $(".conn-actions", connForm).classList.toggle("hidden", isWa);
  $("input[name=name]", connForm).closest("label").classList.toggle("hidden", isWa);
}
$$("input[name=type]", connForm).forEach((r) => r.addEventListener("change", () => {
  $("#conn-error").textContent = ""; $("#conn-ok").textContent = ""; $("#imap-folders").textContent = ""; showConnFields();
}));
function setSecretHints(editing) {
  $("input[name=password]", connForm).placeholder = editing ? "leave empty to keep the saved one" : "";
  $(".conn-fields[data-for=ics] input[name=url]", connForm).placeholder = editing
    ? "leave empty to keep the saved link" : "https://calendar.google.com/calendar/ical/…/basic.ics";
}

function connBody() {
  if (connForm.dataset.oauthType) {
    return { type: connForm.dataset.oauthType, name: connForm.name.value, config: {}, company_wide: connForm.company_wide.checked,
      enabled: connForm.enabled.checked, department_ids: $$("input[name=cdept]:checked", connForm).map((x) => Number(x.value)) };
  }
  const t = connType(); const config = {};
  $$(`.conn-fields[data-for=${t}] input`, connForm).forEach((i) => (config[i.name] = i.value));
  return { type: t, name: connForm.name.value, config, company_wide: connForm.company_wide.checked, enabled: connForm.enabled.checked,
    department_ids: $$("input[name=cdept]:checked", connForm).map((x) => Number(x.value)) };
}
function resetConnForm() {
  connForm.reset(); connForm.id.value = ""; connForm.removeAttribute("data-oauth-type");
  $("#conn-types").classList.remove("hidden"); $("#conn-test").classList.remove("hidden");
  $$("input[name=type]", connForm).forEach((r) => (r.disabled = false));
  $("#conn-form-title").textContent = "Other ways to connect"; $("#conn-cancel").classList.add("hidden");
  $("#conn-error").textContent = ""; $("#conn-ok").textContent = ""; $("#imap-folders").textContent = "";
  deptChecks("#conn-depts", "cdept"); setSecretHints(false); showConnFields();
}
async function loadConns() {
  await refreshDepartments();
  loadQuickConnect();
  if (!connForm.id.value) resetConnForm();
  conns = await getJSON("/api/connectors");
  $("#conn-rows").innerHTML = conns.map((k) => `
    <tr><td dir="auto"><b>${esc(k.name)}</b><div class="muted small">${TYPE_LABEL[k.type]}${k.config.username ? " · " + esc(k.config.username) : ""}${k.config.url && k.type === "website" ? " · " + esc(k.config.url) : ""}${k.enabled ? "" : " · paused"}</div></td>
    <td>${deptPills(k)}</td>
    <td>${k.documents}${k.indexing ? ` <span class="status-pending small">(${k.indexing} indexing)</span>` : ""}</td>
    <td class="small">${k.last_sync ? new Date(k.last_sync).toLocaleString() : '<span class="status-pending">syncing…</span>'}
      ${k.last_error ? `<div class="status-error">${esc(k.last_error)}</div>` : ""}</td>
    <td><button class="link" data-csync="${k.id}">Sync now</button> <button class="link" data-cedit="${k.id}">Edit</button>
      <button class="danger" data-cdel="${k.id}">Remove</button></td></tr>`).join("") ||
    `<tr><td colspan="5" class="muted">Nothing connected yet.</td></tr>`;
  $$("[data-csync]").forEach((b) => b.onclick = async () => { await api(`/api/connectors/${b.dataset.csync}/sync`, { method: "POST" }); b.textContent = "Syncing…"; setTimeout(loadConns, 5000); });
  $$("[data-cedit]").forEach((b) => b.onclick = () => editConn(conns.find((k) => k.id === Number(b.dataset.cedit))));
  $$("[data-cdel]").forEach((b) => b.onclick = async () => {
    if (!confirm("Disconnect this source? Everything imported from it will be removed from the knowledge base.")) return;
    await api(`/api/connectors/${b.dataset.cdel}`, { method: "DELETE" }); loadConns();
  });
  if (conns.some((k) => !k.last_sync || k.indexing)) setTimeout(() => { if (!$("#sub-conns").classList.contains("hidden")) loadConns(); }, 5000);
}
function editConn(k) {
  resetConnForm();
  const isOauth = OAUTH_TYPES.includes(k.type);
  $("#conn-types").classList.toggle("hidden", isOauth);
  if (isOauth) {
    connForm.id.value = k.id; connForm.dataset.oauthType = k.type;
    $$(".conn-fields", connForm).forEach((el) => el.classList.add("hidden"));
    $(".conn-visibility", connForm).classList.remove("hidden"); $(".conn-actions", connForm).classList.remove("hidden");
    $("#conn-test").classList.add("hidden");
    $("input[name=name]", connForm).closest("label").classList.remove("hidden");
    connForm.name.value = k.name; connForm.company_wide.checked = k.company_wide; connForm.enabled.checked = k.enabled;
    deptChecks("#conn-depts", "cdept", k.departments.map((d) => d.id));
    $("#conn-form-title").textContent = `Edit ${k.name}`; $("#conn-cancel").classList.remove("hidden");
    connForm.scrollIntoView({ behavior: "smooth" });
    return;
  }
  connForm.id.value = k.id; setSecretHints(true);
  $(`input[name=type][value=${k.type}]`, connForm).checked = true;
  $$("input[name=type]", connForm).forEach((r) => (r.disabled = true));
  showConnFields();
  connForm.name.value = k.name;
  for (const [key, val] of Object.entries(k.config)) {
    const input = $(`.conn-fields[data-for=${k.type}] input[name=${key}]`, connForm);
    if (input) input.value = val;
  }
  connForm.company_wide.checked = k.company_wide; connForm.enabled.checked = k.enabled;
  deptChecks("#conn-depts", "cdept", k.departments.map((d) => d.id));
  $("#conn-form-title").textContent = `Edit ${k.name}`; $("#conn-cancel").classList.remove("hidden");
  connForm.scrollIntoView({ behavior: "smooth" });
}
$("#conn-cancel").addEventListener("click", resetConnForm);
$("#refresh-conns").addEventListener("click", loadConns);
$("#conn-test").addEventListener("click", async () => {
  $("#conn-error").textContent = ""; $("#conn-ok").textContent = "Testing…";
  try {
    const body = connBody();
    if (connForm.id.value && !body.config.password && !body.config.url) { $("#conn-ok").textContent = "Saved password is kept; press Save to re-check it."; return; }
    const r = await (await api("/api/connectors/test", { method: "POST", json: body })).json();
    $("#conn-ok").textContent = "✓ Connected successfully.";
    if (r.folders.length) $("#imap-folders").textContent = "Folders in this mailbox: " + r.folders.join(", ");
  } catch (err) { $("#conn-ok").textContent = ""; $("#conn-error").textContent = err.message; }
});
connForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#conn-error").textContent = ""; $("#conn-ok").textContent = "Connecting…";
  const btn = $("button[type=submit]", connForm); btn.disabled = true;
  try {
    const id = connForm.id.value;
    await api(id ? `/api/connectors/${id}` : "/api/connectors", { method: id ? "PUT" : "POST", json: connBody() });
    resetConnForm(); $("#conn-ok").textContent = "✓ Saved. Importing now – this can take a few minutes for a big mailbox.";
    loadConns();
  } catch (err) { $("#conn-ok").textContent = ""; $("#conn-error").textContent = err.message; }
  btn.disabled = false;
});

// ------------------------------------------------------------------ admin: departments
async function loadDepts() {
  await refreshDepartments();
  $("#dept-rows").innerHTML = departments.map((d) => `<tr><td dir="auto">${esc(d.name)}</td><td>${d.users}</td><td>${d.documents}</td>
    <td><button class="danger" data-deldept="${d.id}">Delete</button></td></tr>`).join("");
  $$("[data-deldept]").forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this department? Users and documents stay, but lose this department.")) return;
    await api(`/api/departments/${b.dataset.deldept}`, { method: "DELETE" }); loadDepts();
  });
}
$("#dept-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await api("/api/departments", { method: "POST", json: { name: e.target.name.value } }); e.target.reset(); loadDepts(); }
  catch (err) { alert(err.message); }
});

// ------------------------------------------------------------------ admin: users
let users = [];
async function loadUsers() {
  await refreshDepartments();
  resetUserForm();
  users = await getJSON("/api/users");
  $("#user-rows").innerHTML = users.map((u) => `<tr><td dir="auto">${esc(u.name)}${u.active ? "" : ' <span class="muted">(disabled)</span>'}</td><td>${esc(u.email)}</td>
    <td>${u.role}</td><td>${u.departments.map((d) => `<span class="pill">${esc(d.name)}</span>`).join("")}</td>
    <td><button class="link" data-edit="${u.id}">Edit</button></td></tr>`).join("");
  $$("[data-edit]").forEach((b) => b.onclick = () => editUser(users.find((u) => u.id === Number(b.dataset.edit))));
}
function resetUserForm() {
  const f = $("#user-form"); f.reset(); f.id.value = "";
  $("#user-form-title").textContent = "Add user"; $("#user-cancel").classList.add("hidden");
  deptChecks("#user-depts", "udept");
}
function editUser(u) {
  const f = $("#user-form");
  f.id.value = u.id; f.name.value = u.name; f.email.value = u.email; f.password.value = "";
  f.role.value = u.role; f.active.checked = u.active;
  deptChecks("#user-depts", "udept", u.departments.map((d) => d.id));
  $("#user-form-title").textContent = `Edit ${u.name || u.email}`; $("#user-cancel").classList.remove("hidden");
  f.scrollIntoView({ behavior: "smooth" });
}
$("#user-cancel").addEventListener("click", resetUserForm);
$("#user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { name: f.name.value, email: f.email.value, password: f.password.value || null, role: f.role.value,
    active: f.active.checked, department_ids: $$("input[name=udept]:checked", f).map((x) => Number(x.value)) };
  $("#user-error").textContent = "";
  try {
    if (f.id.value) await api(`/api/users/${f.id.value}`, { method: "PUT", json: body });
    else await api("/api/users", { method: "POST", json: body });
    loadUsers();
  } catch (err) { $("#user-error").textContent = err.message; }
});

// ------------------------------------------------------------------ admin: questions
let qData = null;
async function loadQuestions() { qData = await getJSON("/api/admin/questions"); drawQuestions(); }
function drawQuestions() {
  const s = qData.stats;
  $("#q-stats").innerHTML = [["Questions (total)", s.total], ["Last 7 days", s.last_7_days], ["👍 Helpful", s.helpful],
    ["👎 Not helpful", s.not_helpful], ["No sources found", s.no_sources]].map(([l, v]) => `<div class="stat"><b>${v}</b><span class="muted small">${l}</span></div>`).join("");
  const gaps = $("#q-gaps").checked;
  $("#q-rows").innerHTML = qData.questions.filter((q) => !gaps || !q.found_sources || q.feedback === -1).map((q) => `
    <tr><td class="small">${new Date(q.created_at).toLocaleString()}</td><td class="small">${esc(q.user_name || q.user_email || "")}</td>
    <td dir="auto"><details><summary>${esc(q.question)}</summary><div class="small muted" style="white-space:pre-wrap">${esc(q.answer || "")}</div></details></td>
    <td>${!q.found_sources ? '<span class="status-error">no sources</span>' : q.feedback === 1 ? "👍" : q.feedback === -1 ? "👎" : ""}</td></tr>`).join("") ||
    `<tr><td colspan="4" class="muted">No questions yet.</td></tr>`;
}
$("#q-gaps").addEventListener("change", drawQuestions);

// ------------------------------------------------------------------ admin: system
async function loadSystem() {
  const st = await (await fetch("/api/status")).json();
  const l = st.license;
  $("#license-info").innerHTML = l.state === "unchecked" ? `<p class="muted">Licensing is not configured on this installation.</p>` :
    `<p>Status: <b>${esc(l.state)}</b>${l.customer ? ` · ${esc(l.customer)}` : ""}${l.expires ? ` · valid until ${esc(l.expires)}` : ""}${l.max_users ? ` · up to ${l.max_users} users` : ""}</p>${l.message ? `<p class="muted">${esc(l.message)}</p>` : ""}`;
}
$("#license-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = new FormData(); body.append("file", e.target.file.files[0]);
  $("#license-error").textContent = "";
  try { await api("/api/admin/license", { method: "POST", body }); e.target.reset(); loadSystem(); location.reload(); }
  catch (err) { $("#license-error").textContent = err.message; }
});
$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target; $("#pw-error").textContent = "";
  try { await api("/api/me/password", { method: "POST", json: { current_password: f.current_password.value, new_password: f.new_password.value } }); f.reset(); alert("Password changed."); }
  catch (err) { $("#pw-error").textContent = err.message; }
});

boot();
