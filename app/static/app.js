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

function show(id) { $$("body > section").forEach((s) => s.classList.add("hidden")); $(id).classList.remove("hidden"); }
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
    window.history.replaceState(null, "", "/");
    $("button[data-view=admin]").click(); openSub("conns");
    showConnectedNotice(connected);
  }
  loadAskSummary();
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
async function loadAskSummary() {
  try {
    const o = await getJSON("/api/overview");
    const names = { email: "emails", files: "files", file: "documents", calendar: "months of calendar events", web: "web pages",
      whatsapp: "WhatsApp chats", interview: "interviews" };
    const parts = Object.entries(o.ready).map(([k, n]) => `${n} ${names[k] || k}`);
    $("#ask-summary").textContent = parts.length ? `You can ask about: ${parts.join(" · ")}.`
      : (me.role === "admin" ? "Nothing to search yet – add sources in Admin → Connections." : "Nothing to search yet – ask your admin to add company information.");
  } catch {}
}
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
// One screen that answers three questions: what is connected, is it working, and what can be searched.
const PROVIDER_TYPES = {
  google: { mail: "gmail", calendar: "gcal", files: "gdrive" },
  microsoft: { mail: "ms_mail", calendar: "ms_cal", files: "ms_drive" },
};
const PROVIDER_NAME = { google: "Google", microsoft: "Microsoft" };
const SERVICE_INFO = {
  gmail: ["📧", "Gmail", "email", "emails"], ms_mail: ["📧", "Outlook mail", "email", "emails"],
  gcal: ["📅", "Google Calendar", "month of events", "months of events"], ms_cal: ["📅", "Outlook calendar", "month of events", "months of events"],
  gdrive: ["📁", "Google Drive", "file", "files"], ms_drive: ["📁", "OneDrive", "file", "files"],
  imap: ["📧", "Email", "email", "emails"], ics: ["📅", "Calendar", "month of events", "months of events"],
  website: ["🌐", "Website", "page", "pages"],
};
const SERVICE_OF = { gmail: "mail", ms_mail: "mail", gcal: "calendar", ms_cal: "calendar", gdrive: "files", ms_drive: "files" };
const OAUTH_TYPES = Object.keys(SERVICE_OF);
const TYPE_LABEL = Object.fromEntries(Object.entries(SERVICE_INFO).map(([t, i]) => [t, `${i[0]} ${i[1]}`]));

let conns = [];
let oauthState = { configured: {}, redirect_url: "" };
let connPoll = null;
let editing = false; // true while a "who can search" editor or the connect panel is open: don't redraw under the user
const connForm = $("#conn-form");

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts).toLocaleDateString();
}

// One clear line per source: importing / preparing / ready / problem
function statusLine(k) {
  const [, , one, many] = SERVICE_INFO[k.type] || ["", "", "item", "items"];
  const note = k.last_note ? `<div class="muted small">${esc(k.last_note)}</div>` : "";
  if (k.syncing || !k.last_sync)
    return `<span class="st st-busy">⏳ Importing… ${plural(k.documents, one, many)} found so far</span>${note}`;
  if (k.indexing)
    return `<span class="st st-busy">⏳ Getting ready for search: ${k.ready} of ${k.documents} done</span>${note}`;
  const checked = `<span class="muted small"> · checked ${ago(k.last_sync)}</span>`;
  if (k.last_error)
    return `<span class="st st-bad">⚠ ${esc(k.last_error)}</span>` +
      (k.ready ? `<div class="small">${plural(k.ready, one, many)} already searchable.</div>` : "") + note;
  if (!k.ready) return `<span class="st st-ok">✓ Connected – nothing found to import</span>${checked}${note}`;
  return `<span class="st st-ok">✓ ${plural(k.ready, one, many)} searchable</span>${checked}${note}`;
}

function visibilityEditor(id, selectedDepts, everyone) {
  return `<div class="vis-edit" data-vis="${id}">
    <span class="label">Who can search this</span>
    <div class="checks">${departments.map((d) => `<label class="radio"><input type="checkbox" value="${d.id}"
      ${selectedDepts.includes(d.id) ? "checked" : ""}> ${esc(d.name)}</label>`).join("") ||
      '<span class="muted small">No departments yet – you can create them in the Departments tab.</span>'}</div>
    <label class="radio"><input type="checkbox" class="vis-everyone" ${everyone ? "checked" : ""}> Everyone in the company</label>
  </div>`;
}
function readVisibility(el) {
  return { department_ids: $$(".checks input:checked", el).map((x) => Number(x.value)), company_wide: $(".vis-everyone", el).checked };
}

// ---------- Summary: what can be searched now
async function drawSummary() {
  const o = await getJSON("/api/overview");
  const labels = [["email", "📧", "email", "emails"], ["files", "📁", "file", "files"], ["file", "📄", "document", "documents"],
    ["calendar", "📅", "month of calendar events", "months of calendar events"], ["web", "🌐", "web page", "web pages"],
    ["whatsapp", "💬", "WhatsApp chat", "WhatsApp chats"], ["interview", "🎙", "interview", "interviews"]];
  const parts = labels.filter(([k]) => o.ready[k]).map(([k, icon, one, many]) => `<span class="kb-item">${icon} ${plural(o.ready[k], one, many)}</span>`);
  const busy = conns.some((k) => k.syncing || !k.last_sync) || o.importing;
  $("#kb-summary").innerHTML = `
    <div class="row spread"><h2>What KnowledgeDesk can search now</h2>
      ${o.total ? '<button class="primary" id="kb-ask">Ask a question →</button>' : ""}</div>
    ${parts.length ? `<div class="kb-items">${parts.join("")}</div>` :
      '<p class="muted">Nothing yet. Connect an account or add your website below – it takes a minute.</p>'}
    ${busy ? `<p class="st st-busy small">⏳ Still importing${o.importing ? ` (${o.importing} item${o.importing === 1 ? "" : "s"} waiting)` : ""} – new items become searchable automatically. You can ask questions meanwhile.</p>` : ""}`;
  const ask = $("#kb-ask");
  if (ask) ask.onclick = () => $("button[data-view=chat]").click();
}

// ---------- Google / Microsoft cards
function drawProvider(provider) {
  const card = $(`.provider-card[data-provider=${provider}]`);
  const types = Object.values(PROVIDER_TYPES[provider]);
  const mine = conns.filter((k) => types.includes(k.type));
  const accounts = {};
  mine.forEach((k) => { (accounts[k.config.account_id] ||= []).push(k); });

  $(".pc-accounts", card).innerHTML = Object.entries(accounts).map(([accountId, list]) => {
    const email = list[0].config.username || "Connected account";
    const first = list[0];
    const deptIds = first.departments.map((d) => d.id);
    const rows = Object.entries(PROVIDER_TYPES[provider]).map(([svc, type]) => {
      const k = list.find((x) => x.type === type);
      const [icon, label] = SERVICE_INFO[type];
      if (!k) return `<div class="svc off"><div class="svc-name">${icon} ${label}</div>
        <div class="muted small">Not connected</div>
        <div><button class="link" data-add-svc="${svc}" data-account="${accountId}">Connect</button></div></div>`;
      return `<div class="svc"><div class="svc-name">${icon} ${label}${k.enabled ? "" : ' <span class="muted small">(paused)</span>'}</div>
        <div>${statusLine(k)}</div><div></div></div>`;
    }).join("");
    return `<div class="account" data-account="${accountId}">
      <div class="acc-head">
        <div><b dir="ltr">${esc(email)}</b> <span class="badge-ok">Connected</span>
          <div class="muted small">Can be searched by: ${deptPills(first) || "nobody yet"}
            <button class="link small" data-edit-vis="${accountId}">Change</button></div></div>
        <div class="acc-actions">
          <button class="secondary small-btn" data-sync-account="${accountId}" ${list.some((k) => k.syncing) ? "disabled" : ""}>
            ${list.some((k) => k.syncing) ? "Importing…" : "Check for new items"}</button>
          <button class="danger" data-disconnect="${accountId}">Disconnect</button>
        </div>
      </div>
      ${rows}
      <div class="vis-slot"></div>
    </div>`;
  }).join("");

  // Connect button: clearly says "another" once an account is connected
  const slot = $(".pc-connect", card);
  if (!slot.querySelector(".connect-panel")) {
    const configured = oauthState.configured[provider];
    const has = Object.keys(accounts).length > 0;
    slot.innerHTML = !configured
      ? `<p class="muted small">One-click ${PROVIDER_NAME[provider]} sign-in isn't set up on this installation yet
         (see README → "Setting up one-click sign-in"). Return address to register: <code>${esc(oauthState.redirect_url)}</code></p>`
      : `<button class="${has ? "link" : "primary"}" data-open-connect>${has ? `+ Connect another ${PROVIDER_NAME[provider]} account` : `Connect ${PROVIDER_NAME[provider]} account`}</button>`;
    const open = $("[data-open-connect]", slot);
    if (open) open.onclick = () => openConnectPanel(provider);
  }

  $$("[data-edit-vis]", card).forEach((b) => b.onclick = () => {
    const acc = b.closest(".account"); const list = accounts[b.dataset.editVis];
    editing = true;
    $(".vis-slot", acc).innerHTML = visibilityEditor(b.dataset.editVis, list[0].departments.map((d) => d.id), list[0].company_wide) +
      `<div class="row"><button class="primary" data-save-vis>Save</button><button class="secondary" data-cancel-vis>Cancel</button></div>
       <p class="error"></p>`;
    $("[data-cancel-vis]", acc).onclick = () => { editing = false; refreshConnections(); };
    $("[data-save-vis]", acc).onclick = async () => {
      const v = readVisibility($(".vis-edit", acc));
      if (!v.department_ids.length && !v.company_wide) { $(".vis-slot .error", acc).textContent = "Choose at least one department, or Everyone."; return; }
      for (const k of list)
        await api(`/api/connectors/${k.id}`, { method: "PUT", json: { type: k.type, name: k.name, config: {}, enabled: k.enabled, ...v } });
      editing = false; refreshConnections();
    };
  });
  $$("[data-sync-account]", card).forEach((b) => b.onclick = async () => {
    b.disabled = true; b.textContent = "Importing…";
    for (const k of accounts[b.dataset.syncAccount]) await api(`/api/connectors/${k.id}/sync`, { method: "POST" });
    setTimeout(refreshConnections, 800);
  });
  $$("[data-disconnect]", card).forEach((b) => b.onclick = async () => {
    const list = accounts[b.dataset.disconnect];
    if (!confirm(`Disconnect ${list[0].config.username || "this account"}?\n\nEverything imported from it will be removed from the knowledge base, and KnowledgeDesk will forget its sign-in.`)) return;
    b.disabled = true; b.textContent = "Disconnecting…";
    try { for (const k of list) await api(`/api/connectors/${k.id}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
    refreshConnections();
  });
  $$("[data-add-svc]", card).forEach((b) => b.onclick = async () => {
    const list = accounts[b.dataset.account];
    await startSignIn(provider, [b.dataset.addSvc], list[0].departments.map((d) => d.id), list[0].company_wide, b);
  });
}

function openConnectPanel(provider) {
  editing = true;
  const slot = $(`.provider-card[data-provider=${provider}] .pc-connect`);
  const names = { google: ["Gmail", "Google Calendar", "Google Drive"], microsoft: ["Outlook mail", "Outlook calendar", "OneDrive"] }[provider];
  slot.innerHTML = `<div class="connect-panel">
    <span class="label">What should KnowledgeDesk read?</span>
    <div class="checks">
      <label class="radio"><input type="checkbox" name="svc" value="mail" checked> 📧 ${names[0]}</label>
      <label class="radio"><input type="checkbox" name="svc" value="calendar" checked> 📅 ${names[1]}</label>
      <label class="radio"><input type="checkbox" name="svc" value="files" checked> 📁 ${names[2]}</label>
    </div>
    ${visibilityEditor("new-" + provider, [], false)}
    <p class="muted small">You'll sign in on ${PROVIDER_NAME[provider]}'s own page. KnowledgeDesk only reads – it never sends, changes or deletes anything.
      ${provider === "google" ? 'If Google shows "Google hasn\'t verified this app", click <b>Continue</b>, and tick <b>all</b> the boxes on the next page.' : ""}</p>
    <div class="row"><button class="primary" data-go>Continue to ${PROVIDER_NAME[provider]}</button>
      <button class="secondary" data-cancel>Cancel</button></div>
    <p class="error"></p></div>`;
  $("[data-cancel]", slot).onclick = () => { editing = false; slot.innerHTML = ""; refreshConnections(); };
  $("[data-go]", slot).onclick = async (e) => {
    const services = $$("input[name=svc]:checked", slot).map((x) => x.value);
    const v = readVisibility($(".vis-edit", slot));
    if (!services.length) { $(".error", slot).textContent = "Tick at least one thing to read."; return; }
    if (!v.department_ids.length && !v.company_wide) { $(".error", slot).textContent = "Choose who can search it: at least one department, or Everyone."; return; }
    await startSignIn(provider, services, v.department_ids, v.company_wide, e.target, $(".error", slot));
  };
}

async function startSignIn(provider, services, department_ids, company_wide, button, errorEl) {
  button.disabled = true;
  try {
    const r = await (await api("/api/oauth/start", { method: "POST", json: { provider, services, department_ids, company_wide } })).json();
    window.location.href = r.url;
  } catch (err) { button.disabled = false; if (errorEl) errorEl.textContent = err.message; else alert(err.message); }
}

// ---------- Websites
function drawWebsites() {
  const sites = conns.filter((k) => k.type === "website");
  $("#web-list").innerHTML = sites.map((k) => `<div class="svc">
      <div class="svc-name" dir="ltr">${esc((k.config.url || "").replace(/^https?:\/\//, ""))}</div>
      <div>${statusLine(k)}<div class="muted small">Can be searched by: ${deptPills(k)}</div></div>
      <div class="acc-actions"><button class="link" data-web-sync="${k.id}" ${k.syncing ? "disabled" : ""}>Check now</button>
        <button class="danger" data-web-del="${k.id}">Remove</button></div></div>`).join("");
  $$("[data-web-sync]").forEach((b) => b.onclick = async () => { b.disabled = true; await api(`/api/connectors/${b.dataset.webSync}/sync`, { method: "POST" }); setTimeout(refreshConnections, 800); });
  $$("[data-web-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("Remove this website? Its pages will no longer be searchable.")) return;
    await api(`/api/connectors/${b.dataset.webDel}`, { method: "DELETE" }); refreshConnections();
  });
}
$("#web-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = e.target.url.value.trim();
  const v = readVisibility($("#web-visibility .vis-edit"));
  $("#web-error").textContent = "";
  if (!url) { $("#web-error").textContent = "Type the website address first."; return; }
  if (!v.department_ids.length && !v.company_wide) { $("#web-error").textContent = "Choose who can search it."; return; }
  const btn = $("button", e.target); btn.disabled = true; btn.textContent = "Checking the website…";
  try {
    await api("/api/connectors", { method: "POST", json: { type: "website", name: url, config: { url }, ...v } });
    e.target.reset(); refreshConnections();
  } catch (err) { $("#web-error").textContent = err.message; }
  btn.disabled = false; btn.textContent = "Add website";
});

// ---------- More ways (other email, calendar link, WhatsApp)
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
  $("#conn-test").classList.toggle("hidden", t !== "imap");
}
$$("input[name=type]", connForm).forEach((r) => r.addEventListener("change", () => {
  $("#conn-error").textContent = ""; $("#conn-ok").textContent = ""; $("#imap-folders").textContent = ""; showConnFields();
}));
function connBody() {
  const t = connType(); const config = {};
  $$(`.conn-fields[data-for=${t}] input`, connForm).forEach((i) => (config[i.name] = i.value));
  const name = config.name || config.username || "Calendar"; delete config.name;
  return { type: t, name, config, company_wide: connForm.company_wide.checked, enabled: connForm.enabled.checked,
    department_ids: $$("input[name=cdept]:checked", connForm).map((x) => Number(x.value)) };
}
function resetConnForm() {
  connForm.reset(); connForm.id.value = "";
  $("#conn-types").classList.remove("hidden"); $("#conn-form-title").classList.add("hidden");
  $("#conn-cancel").classList.add("hidden"); $("#conn-error").textContent = ""; $("#imap-folders").textContent = "";
  $$("input[name=password], .conn-fields[data-for=ics] input[name=url]", connForm).forEach((i) => (i.placeholder = ""));
  deptChecks("#conn-depts", "cdept"); showConnFields();
}
function drawManual() {
  const list = conns.filter((k) => k.type === "imap" || k.type === "ics");
  $("#manual-list").innerHTML = list.length ? `<div class="manual">${list.map((k) => `<div class="svc">
      <div class="svc-name">${TYPE_LABEL[k.type]}<div class="muted small" dir="auto">${esc(k.config.username || k.name)}</div></div>
      <div>${statusLine(k)}<div class="muted small">Can be searched by: ${deptPills(k)}</div></div>
      <div class="acc-actions"><button class="link" data-m-sync="${k.id}" ${k.syncing ? "disabled" : ""}>Check now</button>
        <button class="link" data-m-edit="${k.id}">Edit</button><button class="danger" data-m-del="${k.id}">Remove</button></div></div>`).join("")}</div>` : "";
  $$("[data-m-sync]").forEach((b) => b.onclick = async () => { b.disabled = true; await api(`/api/connectors/${b.dataset.mSync}/sync`, { method: "POST" }); setTimeout(refreshConnections, 800); });
  $$("[data-m-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("Remove this source? Everything imported from it will be removed.")) return;
    await api(`/api/connectors/${b.dataset.mDel}`, { method: "DELETE" }); refreshConnections();
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
    $$("input[name=password], input[name=url]", fields).forEach((i) => (i.placeholder = "leave empty to keep the saved one"));
    connForm.company_wide.checked = k.company_wide; connForm.enabled.checked = k.enabled;
    deptChecks("#conn-depts", "cdept", k.departments.map((d) => d.id));
    $("#conn-cancel").classList.remove("hidden");
    connForm.scrollIntoView({ behavior: "smooth" });
  });
}
$("#conn-cancel").addEventListener("click", () => { editing = false; resetConnForm(); });
$("#conn-test").addEventListener("click", async () => {
  $("#conn-error").textContent = ""; $("#conn-ok").textContent = "Testing…";
  try {
    const r = await (await api("/api/connectors/test", { method: "POST", json: connBody() })).json();
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
    editing = false; resetConnForm(); $("#conn-ok").textContent = "✓ Saved. Importing now – see the status above.";
    refreshConnections();
  } catch (err) { $("#conn-ok").textContent = ""; $("#conn-error").textContent = err.message; }
  btn.disabled = false;
});

// ---------- Loading and refreshing
async function loadConns() {
  await refreshDepartments();
  try { oauthState = await getJSON("/api/oauth/status"); } catch {}
  editing = false;
  $$(".pc-connect").forEach((el) => (el.innerHTML = ""));
  $("#web-visibility").innerHTML = visibilityEditor("web", [], true);
  resetConnForm();
  await refreshConnections();
}

async function refreshConnections() {
  clearTimeout(connPoll);
  conns = await getJSON("/api/connectors");
  if (!editing) { drawProvider("google"); drawProvider("microsoft"); drawWebsites(); drawManual(); }
  await drawSummary();
  const busy = conns.some((k) => k.syncing || !k.last_sync || k.indexing);
  if (busy || editing)
    connPoll = setTimeout(() => { if (!$("#sub-conns").classList.contains("hidden")) refreshConnections(); }, 4000);
}

function showConnectedNotice(count) {
  $("#conn-notice").innerHTML = `<b>✓ Account connected.</b> KnowledgeDesk is importing it now – watch each item below change from
    <span class="st st-busy">⏳ Importing</span> to <span class="st st-ok">✓ searchable</span>. You can already
    <button class="link" id="notice-ask">ask questions</button> about anything that's searchable.`;
  $("#conn-notice").classList.remove("hidden");
  $("#notice-ask").onclick = () => $("button[data-view=chat]").click();
}

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
