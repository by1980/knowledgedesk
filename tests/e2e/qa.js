// End-to-end QA: drives a real browser through every flow like a new customer would.
// Run with tests/e2e/run.sh (starts a fresh app + the Google/Microsoft simulator).
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const APP = process.env.APP || "http://127.0.0.1:8100";
const SIM = process.env.SIM || "http://127.0.0.1:9999";
const OUT = process.env.OUT || path.join(__dirname, "screens");
fs.mkdirSync(OUT, { recursive: true });

const results = [];
function check(name, ok, detail = "") { results.push({ name, ok: !!ok, detail }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail && !ok ? "  — " + detail : ""}`); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 60000, every = 1000) { const end = Date.now() + ms; while (Date.now() < end) { try { if (await fn()) return true; } catch {} await sleep(every); } return false; }

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const page = await ctx.newPage();
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/401|Failed to load resource/.test(m.text())) consoleErrors.push(m.text()); });
  let shotN = 0;
  const shot = async (name, full = true) => page.screenshot({ path: path.join(OUT, `${String(++shotN).padStart(2, "0")}-${name}.png`), fullPage: full });
  const text = async (sel) => (await page.textContent(sel)) || "";
  const noOverflow = async () => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
  const emojisOnPage = async () => page.evaluate(() => (document.body.innerText.match(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B50}\u{2705}]/gu) || []).join(""));
  const toastSeen = async (re) => until(async () => re.test(await page.evaluate(() => [...document.querySelectorAll(".toast")].map((t) => t.textContent).join(" | "))), 8000, 200);

  // ------------------------------------------------------------------ 1. Login
  await page.goto(APP);
  await page.waitForSelector("#login-view:not(.hidden)");
  await page.fill("input[name=email]", "admin@acme.test"); await page.fill("input[name=password]", "wrong-password");
  await page.click("#login-form button");
  check("Wrong password shows a clear error", await until(async () => /Wrong email or password/.test(await text("#login-error")), 5000, 200));
  await shot("login-error");
  await page.fill("input[name=password]", "admin-pass-123"); await page.click("#login-form button");
  await page.waitForSelector("#app-view:not(.hidden)");
  check("Admin lands on Home", /#home|\/$/.test(page.url()) && await page.isVisible("#page-home"));
  check("Admin menu has all six pages", (await page.$$eval("#nav a", (as) => as.map((a) => a.textContent))).join(",") === "Home,Ask,Sources,People,Activity,Settings");

  // ------------------------------------------------------------------ 2. Home on first use
  await until(async () => (await text("#home-setup")).includes("0 of 3"), 10000, 300);
  check("Home shows the setup checklist (0 of 3)", (await text("#home-setup")).includes("0 of 3 done"));
  check("Home says what needs attention", /Connected sources|Nothing connected/.test(await text("#home-health")));
  await shot("home-first-use");

  // ------------------------------------------------------------------ 3. Ask before anything is connected
  await page.click("#nav a[data-page=ask]");
  check("Ask page explains nothing can be searched yet", await until(async () => /Nothing can be searched yet/.test(await text("#messages")), 5000, 200));
  check("…and offers to connect a source", await page.isVisible("#messages a[href='#sources']"));

  // ------------------------------------------------------------------ 4. Connect Google
  await page.click("#nav a[data-page=sources]");
  const g = ".provider-card[data-provider=google]";
  await page.waitForSelector(`${g} [data-open-connect]`);
  check("Google: clear 'Connect Google account' button", (await text(`${g} [data-open-connect]`)).trim() === "Connect Google account");
  await shot("sources-empty");
  await page.click(`${g} [data-open-connect]`);
  await page.click(`${g} [data-go]`);
  check("Google: asks who can find it before continuing", /Choose who can find it/.test(await text(`${g} .panel .error`)));
  await page.check(`${g} .panel .vis-everyone`);
  await shot("google-connect-panel", false);
  await page.click(`${g} [data-go]`);
  await page.waitForURL(/accounts\.google\.com/);
  await page.click("#allow");
  await page.waitForURL(/#sources/);
  check("After Google sign-in: back on Sources with a confirmation", await toastSeen(/Account connected/));
  await page.waitForSelector(`${g} .account`);
  check("Google: account shows as Connected with its email", /owner@acme-plumbing\.test[\s\S]*Connected/.test(await text(`${g} .account`)));
  check("Google: shows Importing or ready status right away", /Importing|Preparing|searchable/.test(await text(`${g} .account`)));
  const googleReady = await until(async () => {
    const t = await text(`${g} .account`);
    return /\d+ emails searchable/.test(t) && /files? searchable/.test(t) && /\d+ events? searchable/.test(t);
  }, 180000, 2000);
  check("Google: Gmail, Calendar and Drive all become searchable", googleReady, await text(`${g} .account`));
  const gtext = await text(`${g} .account`);
  check("Gmail: all 30 emails imported despite a Google slow-down", /30 emails searchable/.test(gtext), gtext);
  check("Drive: protected file is skipped and explained, not an error", /Skipped 1 item: Locked contract: can't be downloaded/.test(gtext) && !/Needs attention/.test(gtext), gtext);
  check("Calendar: counted in events (3), not months", /3 events searchable/.test(gtext), gtext);
  check("Google: button now says 'Connect another Google account'", /Connect another Google account/.test(await text(`${g} .pc-connect`)));
  await shot("google-connected");

  // reconnecting the same account must not duplicate it
  await page.click(`${g} [data-open-connect]`); await page.check(`${g} .panel .vis-everyone`); await page.click(`${g} [data-go]`);
  await page.waitForURL(/accounts\.google\.com/); await page.click("#allow"); await page.waitForURL(/#sources/);
  await page.waitForSelector(`${g} .account`);
  await sleep(1500);
  check("Connecting the same Google account again does not duplicate it", (await page.$$(`${g} .account`)).length === 1);

  // ------------------------------------------------------------------ 5. View what was imported
  await page.click(`${g} [data-items]`);
  await page.waitForSelector("#dialog[open] .list-row");
  check("'View items' lists the imported emails", (await page.$$("#dialog[open] .list-row")).length === 15);
  check("…with paging (30 emails)", /1–15 of 30/.test(await text("#dialog[open] .pager")));
  await page.click("#dialog[open] [data-next]"); await sleep(500);
  check("…and the next page works", /16–30 of 30/.test(await text("#dialog[open] .pager")));
  await page.fill("#dialog[open] [data-q]", "Levi"); await sleep(800);
  check("…and search inside the items works", (await page.$$("#dialog[open] .list-row")).length === 1);
  await shot("view-items", false);
  await page.click("#dialog[open] [data-close]");

  // ------------------------------------------------------------------ 6. Connect Microsoft
  const m = ".provider-card[data-provider=microsoft]";
  await page.click(`${m} [data-open-connect]`); await page.check(`${m} .panel .vis-everyone`); await page.click(`${m} [data-go]`);
  await page.waitForURL(/login\.microsoftonline\.com/); await page.click("#allow"); await page.waitForURL(/#sources/);
  const msReady = await until(async () => /2 emails searchable/.test(await text(`${m} .account`)) && /1 file searchable/.test(await text(`${m} .account`)), 120000, 2000);
  check("Microsoft: Outlook mail, calendar and OneDrive become searchable", msReady, await text(`${m} .pc-accounts`));

  // cancelling on the sign-in page
  await page.click(`${m} [data-open-connect]`); await page.check(`${m} .panel .vis-everyone`); await page.click(`${m} [data-go]`);
  await page.waitForURL(/login\.microsoftonline\.com/); await page.click("#deny");
  check("Cancelling sign-in explains what happened", /cancelled/.test(await page.textContent("body")));
  await page.goto(APP + "/#sources");
  await page.waitForSelector(`${m} .account`);

  // ------------------------------------------------------------------ 7. Website
  await page.fill("#web-form input[name=url]", "hello"); await page.click("#web-form button");
  check("Website: a wrong address gets a clear message", await until(async () => /doesn't look like a website address/.test(await text("#web-error")), 15000, 300));
  await page.fill("#web-form input[name=url]", `${SIM}/site/`); await page.click("#web-form button");
  const webReady = await until(async () => /3 pages searchable/.test(await text("#web-list")), 60000, 1500);
  check("Website: all 3 pages become searchable", webReady, await text("#web-list"));

  // ------------------------------------------------------------------ 8. People: departments and a team member
  await page.click("#nav a[data-page=people]");
  for (const name of ["Sales", "HR"]) {
    await page.fill("#dept-form input[name=name]", name); await page.click("#dept-form button");
    await until(async () => (await text("#dept-chips")).includes(name), 5000, 200);
  }
  check("People: departments added", /Sales[\s\S]*HR|HR[\s\S]*Sales/.test(await text("#dept-chips")));
  await page.click("#add-user");
  await page.fill("#dialog[open] input[name=name]", "Dana Cohen");
  await page.fill("#dialog[open] input[name=email]", "dana@acme.test");
  const danaPassword = await page.inputValue("#dialog[open] input[name=password]");
  check("Add person: a password is suggested", danaPassword.length >= 8);
  await page.check("#dialog[open] label:has-text('Sales') input");
  await shot("add-person", false);
  await page.click("#dialog[open] button[type=submit]");
  check("Add person: shows the login details to send, with a Copy button",
    await until(async () => /was added/.test(await text("#dialog[open]")) && (await page.inputValue("#dialog[open] [data-details]")).includes(danaPassword), 5000, 200));
  await page.click("#dialog[open] [data-done]");
  await until(async () => (await text("#user-list")).includes("Dana Cohen"), 5000, 200);
  check("People: Dana listed with her department", /Dana Cohen[\s\S]*Sales/.test(await text("#user-list")));
  await shot("people");

  // ------------------------------------------------------------------ 9. Uploads: document (HR only), interview, WhatsApp
  await page.click("#nav a[data-page=sources]");
  await page.waitForSelector("#upload-visibility .vis-block");
  const tmp = path.join(OUT, "_files"); fs.mkdirSync(tmp, { recursive: true });
  fs.writeFileSync(path.join(tmp, "Salaries 2026.txt"), "Confidential salary table: senior technician salary is 14,000 NIS per month.");
  fs.writeFileSync(path.join(tmp, "moshe-interview.txt"), "Interviewer: How do you handle a new boiler order?\nMoshe: First I check stock on shelf C in the back warehouse, then I call Electra Parts.");
  fs.writeFileSync(path.join(tmp, "chat.txt"), "[12/10/2026, 09:15:02] Dana: Who has the storage key?\n[12/10/2026, 09:16:40] Avi: The storage key is with Rina at the front desk\n");
  await page.click("#upload-form button[type=submit]");
  check("Upload: says to choose a file first", /Choose at least one file/.test(await text("#upload-error")));
  await page.setInputFiles("#dropzone input", path.join(tmp, "Salaries 2026.txt"));
  check("Upload: chosen file name is shown", (await text(".dz-files")).includes("Salaries 2026.txt"));
  await page.click("#upload-form button[type=submit]");
  check("Upload: asks who can find it", /Choose who can find it/.test(await text("#upload-error")));
  await page.check("#upload-visibility .vis-everyone");
  await page.check("#upload-visibility label:has-text('HR') input");
  check("Who-can-find: choosing a department clears 'Everyone'", !(await page.isChecked("#upload-visibility .vis-everyone")));
  await page.click("#upload-form button[type=submit]");
  check("Upload: confirmation shown", await toastSeen(/Uploaded 1 file/));
  check("Who-can-find resets after an upload", !(await page.isChecked("#upload-visibility label:has-text('HR') input")));
  await page.click("#upload-form label:has-text('Employee interview')");
  check("Upload: interview fields appear for interviews", await page.isVisible(".interview-fields"));
  await page.fill("#upload-form input[name=interviewee]", "Moshe"); await page.fill("#upload-form input[name=role]", "Senior technician");
  await page.setInputFiles("#dropzone input", path.join(tmp, "moshe-interview.txt"));
  await page.check("#upload-visibility .vis-everyone");
  await page.click("#upload-form button[type=submit]");
  await toastSeen(/Uploaded 1 file/);
  await page.click("#upload-form label:has-text('WhatsApp chat')");
  check("Upload: WhatsApp export instructions shown", /Export chat/.test(await text(".kind-help[data-kind=whatsapp]")));
  await page.setInputFiles("#dropzone input", path.join(tmp, "chat.txt"));
  await page.check("#upload-visibility .vis-everyone");
  await page.click("#upload-form button[type=submit]");
  await toastSeen(/Uploaded 1 file/);
  const filesReady = await until(async () => {
    await page.fill("#files-search", ""); const t = await text("#files-list");
    return (t.match(/Searchable/g) || []).length === 3;
  }, 90000, 2000);
  check("Uploads: all 3 files become searchable", filesReady, await text("#files-list"));
  check("Uploads: interview and WhatsApp get clear titles", /Interview - Moshe \(Senior technician\)/.test(await text("#files-list")) && /WhatsApp: chat/.test(await text("#files-list")));
  await shot("sources-full");

  // limit the Google account to Sales
  await page.click(`${g} [data-edit-vis]`);
  await page.uncheck(`${g} .panel .vis-everyone`); await page.check(`${g} .panel label:has-text('Sales') input`);
  await page.click(`${g} [data-save-vis]`);
  check("Changing who can find an account is confirmed", await toastSeen(/Saved/));
  check("…and shown on the account", await until(async () => /Who can find this:\s*Sales/.test(await text(`${g} .acc-head`)), 5000, 300));

  // ------------------------------------------------------------------ 10. Ask as admin
  await page.click("#nav a[data-page=ask]");
  await page.waitForSelector(".welcome");
  check("Ask: shows what can be asked about", /emails/.test(await text(".welcome .chips")) && /web pages/.test(await text(".welcome .chips")));
  check("Ask: offers example questions", (await page.$$(".examples button")).length >= 2);
  await shot("ask-welcome");
  await page.fill("#question", "How much is the Levi Gardens maintenance contract?"); await page.press("#question", "Enter");
  await page.waitForSelector(".msg.bot .feedback", { timeout: 30000 });
  const answer = await text(".msg.bot");
  check("Ask: answer cites the right email", /Levi Gardens/.test(answer) && /Sources/.test(answer), answer.slice(0, 300));
  await page.click(".msg.bot .feedback button[data-v='-1']");
  check("Feedback is confirmed", await toastSeen(/Thanks/));
  await page.fill("#question", "When is the supplier visit?"); await page.press("#question", "Enter");
  await until(async () => (await page.$$(".msg.bot .feedback")).length === 2, 30000, 300);
  check("Ask: calendar question finds the calendar", /Calendar|Supplier visit/.test(await text(".msg.bot:last-of-type")));
  await shot("ask-answers");

  // ------------------------------------------------------------------ 11. A team member only sees what they're allowed to
  await page.click("#logout");
  await page.fill("input[name=email]", "dana@acme.test"); await page.fill("input[name=password]", danaPassword);
  await page.click("#login-form button"); await page.waitForSelector("#app-view:not(.hidden)");
  check("Team member menu: only Ask and Library", (await page.$$eval("#nav a", (as) => as.map((a) => a.textContent))).join(",") === "Ask,Library");
  await page.click("#user-name");
  await page.fill("#dialog[open] input[name=current]", danaPassword); await page.fill("#dialog[open] input[name=next]", "dana-new-pass-1");
  await page.click("#dialog[open] button[type=submit]");
  check("Team member can change their own password", await toastSeen(/Password changed/));
  await page.goto(APP + "/#settings"); await sleep(500);
  check("Team member can't open admin pages", !(await page.isVisible("#page-settings")));
  await page.click("#nav a[data-page=ask]");
  await page.fill("#question", "What is the senior technician salary?"); await page.press("#question", "Enter");
  await page.waitForSelector(".msg.bot .feedback", { timeout: 30000 });
  check("Team member never gets HR-only information", !/14,000/.test(await text("#messages")));
  await page.click("#nav a[data-page=library]");
  await until(async () => (await page.$$("#lib-list .list-row")).length > 0, 10000, 300);
  await page.fill("#lib-search", "Salaries"); await sleep(800);
  check("Library: HR-only file is not listed for Sales", /Nothing to show/.test(await text("#lib-list")));
  await page.fill("#lib-search", "Levi"); await sleep(800);
  check("Library: Sales can see the Google emails", /Levi Gardens/.test(await text("#lib-list")));
  await shot("member-library");

  // ------------------------------------------------------------------ 12. Activity and health (admin)
  await page.click("#logout");
  await page.fill("input[name=email]", "admin@acme.test"); await page.fill("input[name=password]", "admin-pass-123");
  await page.click("#login-form button"); await page.waitForSelector("#app-view:not(.hidden)");
  await until(async () => /Everything is working|need your attention|Almost ready/.test(await text("#home-health")), 10000, 300);
  check("Home: setup checklist complete (hidden)", (await text("#home-setup")).trim() === "");
  await shot("home-after-setup");
  await page.click("#nav a[data-page=activity]");
  await page.check("#q-gaps");
  check("Activity: the 👎 question is listed as a gap", /Levi Gardens/.test(await text("#q-list")));
  await shot("activity");
  await page.click("#nav a[data-page=settings]");
  await until(async () => /Claude/.test(await text("#health-list")), 15000, 300);
  const health = await text("#health-list");
  check("Health: every check is listed in plain words", /AI answers/.test(health) && /Search engine/.test(health) && /Connected sources/.test(health));
  check("Health: sources reported as working", /all working/.test(health), health);
  const [download] = await Promise.all([page.waitForEvent("download"), page.click("#health-download")]);
  check("Health: report can be downloaded", /knowledgedesk-health/.test(download.suggestedFilename()));
  await page.fill("#ai-form input[name=key]", "hello"); await page.click("#ai-form button");
  check("Claude API key: a wrong key gets a clear message", await until(async () => /doesn't look like a Claude API key/.test(await text("#ai-error")), 5000, 200));
  await shot("settings");

  // ------------------------------------------------------------------ 13. Disconnect
  await page.click("#nav a[data-page=sources]");
  await page.waitForSelector(`${m} [data-disconnect]`);
  await page.click(`${m} [data-disconnect]`);
  check("Disconnect asks for confirmation and explains the effect", /removed from KnowledgeDesk/.test(await text("#dialog[open]")));
  await page.click("#dialog[open] [data-yes]");
  check("Disconnect: account removed", await until(async () => (await page.$$(`${m} .account`)).length === 0, 15000, 500));
  check("…and the big Connect button is back", (await text(`${m} [data-open-connect]`)).trim() === "Connect Microsoft account");

  // ------------------------------------------------------------------ 14. Phone layout
  await page.setViewportSize({ width: 390, height: 844 });
  for (const p of ["home", "ask", "sources", "people", "activity", "settings"]) {
    await page.click(`#nav a[data-page=${p}]`); await sleep(1200);
    check(`Phone: ${p} page fits the screen (no sideways scrolling)`, await noOverflow());
    const emojis = await emojisOnPage();
    check(`${p} page uses icons, no emojis`, !emojis, emojis);
    await shot(`phone-${p}`, p !== "sources");
  }

  check("No JavaScript errors during the whole run", consoleErrors.length === 0, consoleErrors.join(" | "));
  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
  fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify(results, null, 2));
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
