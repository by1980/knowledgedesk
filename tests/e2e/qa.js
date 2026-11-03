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
  const go = async (p) => { await page.evaluate((x) => { location.hash = "#" + x; }, p); await sleep(400); };
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
  await page.goto(APP + "/app");
  await page.waitForSelector("#login-view:not(.hidden)");
  await page.fill("input[name=email]", "admin@acme.test"); await page.fill("input[name=password]", "wrong-password");
  await page.click("#login-form button");
  check("Wrong password shows a clear error", await until(async () => /Wrong email or password/.test(await text("#login-error")), 5000, 200));
  await shot("login-error");
  await page.fill("input[name=password]", "admin-pass-123"); await page.click("#login-form button");
  await page.waitForSelector("#app-view:not(.hidden)");
  check("Admin lands on Home", /#home|\/app$/.test(page.url()) && await page.isVisible("#page-home"));
  check("Home greets by first name, guessed from the email when no name is set", await until(async () => /^Hi Admin/.test((await text("#home-title")).trim()), 5000, 200));
  await page.click("#user-name"); await page.fill("#dialog[open] input[name=name]", "Yaniv Barak"); await page.click("#dialog[open] [data-save-name]");
  check("My account: saving my name updates the greeting", await until(async () => /^Hi Yaniv/.test((await text("#home-title")).trim()), 5000, 200));
  await page.click("#dialog[open] [data-close]");
  check("Admin sidebar: New chat, then every page, then recent chats", /^\s*New chat/.test(await text("#nav"))
    && (await page.$$eval("#nav a[data-page]:not(.recent-head a)", (as) => as.map((a) => a.textContent))).join(",") === "Home,Chat,Brain,Sources,People,Learning Center,Health,Settings" && /Recent chats[\s\S]*See all/.test(await text("#nav")));
  check("Sidebar has no switch: it stays the same on every page", !(await page.$("#mode-switch")));
  await go("ask");
  check("Sidebar is the same on the chat page", (await page.$$eval("#nav a[data-page]:not(.recent-head a)", (as) => as.map((a) => a.textContent))).join(",") === "Home,Chat,Brain,Sources,People,Learning Center,Health,Settings");
  await go("home");

  // ------------------------------------------------------------------ 2. Home on first use
  await until(async () => (await text("#home-setup")).includes("0 of 3"), 10000, 300);
  check("Home shows the setup checklist (0 of 3)", (await text("#home-setup")).includes("0 of 3 done"));
  check("Home: pulse cards (knowledge added, questions asked, connections)", /Knowledge added[\s\S]*Questions asked[\s\S]*Connections[\s\S]*Sources[\s\S]*Cortex[\s\S]*Teams/.test(await text("#home-kpis")));
  check("Home: empty chart explains itself", /No questions yet/.test(await text("#home-q-chart")));
  check("Home: no alert box when nothing is broken", (await text("#home-health")).trim() === "");
  check("Home: no 'Not your name?' link", !/Not your name/.test(await text("#page-home")));
  await shot("home-first-use");

  // ------------------------------------------------------------------ 3. Ask before anything is connected
  await go("ask");
  check("Ask page explains nothing can be searched yet", await until(async () => /Nothing can be searched yet/.test(await text("#chat-hello")), 5000, 200));
  check("…and offers to connect a source", await page.isVisible("#chat-hello a[href='#sources']"));

  // ------------------------------------------------------------------ 4. Connect Google
  await go("sources");
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
  check("Sources: no 'Ask a question' button on this page", !/Ask a question/.test(await text("#page-sources")));
  await until(async () => /Connected/.test(await text("[data-tile=gmail]")), 15000, 500);
  check("Connection hub: named 'Connection hub'", /Connection hub/.test(await text("#overview-card h2")));
  check("Connection hub: unconnected Outlook tile has a Connect button", await page.isVisible("[data-tile=ms_mail] [data-connect-tile]"));
  const scrollBefore = await page.evaluate(() => window.scrollY);
  await page.click("[data-tile=ms_mail] [data-connect-tile]");
  check("…which connects right there (a window opens, the page doesn't jump) with only Outlook mail ticked", await until(async () =>
    await page.isVisible("#dialog[open] .panel") && /Connect Microsoft 365/.test(await text("#dialog[open] h2")) &&
    await page.isChecked("#dialog[open] input[value=mail]") && !(await page.isChecked("#dialog[open] input[value=calendar]")), 5000, 200)
    && Math.abs((await page.evaluate(() => window.scrollY)) - scrollBefore) < 5);
  await shot("connect-from-tile", false);
  await page.click("#dialog[open] [data-cancel]");
  check("…and Cancel closes it", await until(async () => !(await page.isVisible("#dialog[open]")), 3000, 200));
  await page.click("[data-tile=youtube]");
  check("Clicking a not-connected tile itself also opens its connect window", await until(async () => /Connect YouTube/.test(await text("#dialog[open] h2")), 3000, 200));
  await page.click("#dialog[open] [data-cancel]");
  check("Overview: Gmail tile says Connected with the count", /Connected[\s\S]*30 emails/.test(await text("[data-tile=gmail]")), await text("[data-tile=gmail]"));
  check("Overview: Outlook tile says Not connected", /Not connected/.test(await text("[data-tile=ms_mail]")));
  check("Overview: Slack tile says Not connected", /Not connected/.test(await text("[data-tile=slack]")));
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
  await page.goto(APP + "/app#sources");
  await page.waitForSelector(`${m} .account`);
  check("WhatsApp: has its own card with an import button", await page.isVisible("#wa-upload"));

  // ------------------------------------------------------------------ 6b. Slack
  const sl = ".provider-card[data-provider=slack]";
  check("Slack: clear 'Connect Slack workspace' button", (await text(`${sl} [data-open-connect]`)).trim() === "Connect Slack workspace");
  await page.click(`${sl} [data-open-connect]`); await page.check(`${sl} .panel .vis-everyone`); await page.click(`${sl} [data-go]`);
  await page.waitForURL(/slack\.com/); await page.click("#allow"); await page.waitForURL(/#sources/);
  const slackReady = await until(async () => /3 messages searchable/.test(await text(`${sl} .pc-accounts`)), 90000, 2000);
  check("Slack: workspace connected and its messages become searchable", slackReady, await text(`${sl} .pc-accounts`));
  check("Slack: says which channels it reads and how to add private ones", /Reading 2 channels[\s\S]*1 private channel isn't included/.test(await text(`${sl} .pc-accounts`)));
  check("Overview: Slack tile now Connected", await until(async () => /Connected/.test(await text("[data-tile=slack]")), 10000, 500));
  await page.evaluate(() => document.querySelector("#page-sources").scrollTo(0, 0)); await sleep(300);
  await shot("sources-overview", false);
  // ------------------------------------------------------------------ 6c. Social media
  const fb = ".provider-card[data-provider=meta]";
  check("Social media: Facebook and Instagram card with a clear button", (await text(`${fb} [data-open-connect]`)).trim() === "Connect Facebook account");
  check("Connection hub: Facebook & Instagram, YouTube and LinkedIn tiles", await page.isVisible("[data-tile=meta_social] [data-connect-tile]")
    && await page.isVisible("[data-tile=youtube] [data-connect-tile]") && /Import/.test(await text("[data-tile=linkedin]")));
  await page.click("[data-tile=meta_social] [data-connect-tile]");
  check("Facebook not set up yet: a step-by-step setup window opens", await until(async () =>
    /Set up Facebook sign-in/.test(await text("#dialog[open] h2")) && /Configuration ID/.test(await text("#dialog[open]")), 5000, 200));
  await shot("facebook-setup", false);
  check("…with the exact return address to copy", /oauth\/callback/.test(await text("#dialog[open] .copy-row")));
  await page.fill("#dialog[open] input[name=client_id]", "1234567"); await page.fill("#dialog[open] input[name=client_secret]", "wrong");
  await page.click("#dialog[open] [data-save]");
  check("…a wrong App secret is caught, in plain words", await until(async () => /doesn't recognise this App ID and App secret/.test(await text("#dialog[open] .error")), 8000, 200));
  await page.fill("#dialog[open] input[name=client_secret]", "real-secret"); await page.fill("#dialog[open] input[name=config_id]", "777");
  await page.click("#dialog[open] [data-save]");
  check("…the right ones are accepted and it goes straight on to connecting", await until(async () =>
    /Connect Facebook and Instagram/.test(await text("#dialog[open] h2")), 8000, 200));
  check("Facebook: tells to pick the Page and its Instagram account", /Instagram account when asked/.test(await text("#dialog[open] .panel")));
  await page.check("#dialog[open] .vis-everyone"); await page.click("#dialog[open] [data-go]");
  await page.waitForURL(/facebook\.com/); await page.click("#allow"); await page.waitForURL(/#sources/);
  const fbReady = await until(async () => /@acme\.plumbing/.test(await text(`${fb} .pc-accounts`)) && /7 posts searchable/.test(await text(`${fb} .pc-accounts`)), 90000, 2000);
  check("Facebook: Page and Instagram connected, posts searchable", fbReady, await text(`${fb} .pc-accounts`));
  check("Facebook: shows followers, last post and likes this week", /1,840 followers/.test(await text(`${fb} .pc-accounts`))
    && /3,120 followers/.test(await text(`${fb} .pc-accounts`)) && /last post/.test(await text(`${fb} .pc-accounts`)) && /212/.test(await text(`${fb} .pc-accounts`)), await text(`${fb} .pc-accounts`));
  const yt = ".provider-card[data-provider=youtube]";
  check("YouTube: clear 'Connect YouTube channel' button", (await text(`${yt} [data-open-connect]`)).trim() === "Connect YouTube channel");
  await page.click(`${yt} [data-open-connect]`); await page.check(`${yt} .panel .vis-everyone`);
  check("YouTube: signs in with Google", /Continue to Google/.test(await text(`${yt} [data-go]`)));
  await page.click(`${yt} [data-go]`);
  await page.waitForURL(/google\.com/); await page.click("#allow"); await page.waitForURL(/#sources/);
  check("YouTube: channel connected with its numbers", await until(async () => /@acmeplumbing[\s\S]*860 subscribers/.test(await text(`${yt} .pc-accounts`)), 60000, 1500), await text(`${yt} .pc-accounts`));
  check("YouTube: Gmail on the same Google account is still fine", await until(async () => !/Needs attention/.test(await text(`${g} .account`)), 5000, 500));
  check("Connection hub: Facebook & Instagram tile now Connected", await until(async () => /Connected|Importing/.test(await text("[data-tile=meta_social]")), 10000, 500));
  await page.locator(fb).scrollIntoViewIfNeeded(); await sleep(300);
  await shot("social-media", false);
  await page.click("#li-upload");
  check("LinkedIn: import opens a small window with the steps", await until(async () => /Import from LinkedIn/.test(await text("#dialog[open] h2")), 3000, 200));
  await page.setInputFiles("#dialog[open] input[type=file]", { name: "acme_content.csv", mimeType: "text/csv",
    buffer: Buffer.from("Post title,Created date,Impressions,Likes,Comments\nProud to sponsor the Haifa marathon,10/28/2026,4120,87,9\n") });
  await page.check("#dialog[open] .vis-everyone"); await page.click("#dialog[open] [data-save]");
  check("LinkedIn: file imported", await toastSeen(/Uploaded 1 file/));
  check("Connection hub: LinkedIn tile says Imported", await until(async () => /Imported/.test(await text("[data-tile=linkedin]")), 10000, 500));

  await page.locator(".provider-card[data-provider=slack]").scrollIntoViewIfNeeded(); await sleep(300);
  await shot("slack-and-whatsapp", false);

  // ------------------------------------------------------------------ 6c. Find an item
  await page.fill("#find-form input", "Levi Gardens"); await page.press("#find-form input", "Enter");
  await page.waitForSelector("#dialog[open] [data-list]");
  check("Find an item: shows whether an email was imported", await until(async () => /Price agreement – Levi Gardens/.test(await text("#dialog[open] [data-list]")), 8000, 300));
  await page.fill("#dialog[open] [data-q]", "warehouse closes");
  check("Find an item: searches inside the text too (Slack message)", await until(async () => /Slack #general/.test(await text("#dialog[open] [data-list]")), 8000, 300));
  await page.click("#dialog[open] [data-close]");

  // ------------------------------------------------------------------ 7. Website
  await page.fill("#web-form input[name=url]", "hello"); await page.click("#web-form button");
  check("Website: a wrong address gets a clear message", await until(async () => /doesn't look like a website address/.test(await text("#web-error")), 15000, 300));
  await page.fill("#web-form input[name=url]", `${SIM}/site/`); await page.click("#web-form button");
  const webReady = await until(async () => /3 pages searchable/.test(await text("#web-list")), 60000, 1500);
  check("Website: all 3 pages become searchable", webReady, await text("#web-list"));

  // ------------------------------------------------------------------ 8. People: departments and a team member
  await go("people");
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
  await go("sources");
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
    return (t.match(/Searchable/g) || []).length === 4;
  }, 90000, 2000);
  check("Uploads: all 4 files (3 here + the LinkedIn file) become searchable", filesReady, await text("#files-list"));
  check("WhatsApp card: shows the imported chat", await until(async () => /1 chat imported/.test(await text("#wa-status")), 15000, 500));

  // WhatsApp Business: official connection
  await page.locator("#whatsapp-card").scrollIntoViewIfNeeded();
  await page.click("#wa-connect");
  check("WhatsApp: connect panel with the steps", /Phone number ID/.test(await text("#wa-panel")));
  check("WhatsApp: warns clearly that Meta needs a public https address", /public[\s\S]*https/.test(await text("#wa-panel")));
  await page.fill("#wa-form input[name=phone_number_id]", "PHONE1"); await page.fill("#wa-form input[name=access_token]", "wrong");
  await page.check("#wa-form .vis-everyone"); await page.click("#wa-form button[type=submit]");
  check("WhatsApp: a wrong token gets Meta's reason", await until(async () => /Meta didn't accept/.test(await text("#wa-form .error")), 8000, 300));
  await page.fill("#wa-form input[name=access_token]", "wa-token"); await page.click("#wa-form button[type=submit]");
  check("WhatsApp: connected with the number's name", await until(async () => /Acme Plumbing \+972 50-123-4567[\s\S]*Connected/.test(await text("#wa-list")), 10000, 300), await text("#wa-list"));
  const waId = await page.getAttribute("#wa-list [data-wa-del]", "data-wa-del");
  const verify = await page.request.get(`${APP}/webhooks/whatsapp/${waId}?hub.mode=subscribe&hub.challenge=12345&hub.verify_token=${encodeURIComponent(await page.locator("#wa-list code").nth(1).textContent())}`);
  check("WhatsApp: Meta's webhook check is answered", (await verify.text()) === "12345");
  await page.request.post(`${APP}/webhooks/whatsapp/${waId}`, { data: { object: "whatsapp_business_account", entry: [{ changes: [{ field: "messages", value: {
    contacts: [{ wa_id: "972501112233", profile: { name: "Yossi Levi" } }],
    messages: [{ from: "972501112233", id: "wamid.1", timestamp: String(Math.floor(Date.now() / 1000)), type: "text", text: { body: "Hi, can you send a technician tomorrow for the leaking boiler at Levi Gardens?" } }] } }] }] } });
  check("WhatsApp: an incoming message shows up and becomes searchable", await until(async () => /1 message so far/.test(await text("#wa-list")), 30000, 1000), await text("#wa-list"));
  check("Connection hub: WhatsApp tile says Connected", /Connected/.test(await text("[data-tile=whatsapp]")));
  check("Uploads: interview and WhatsApp get clear titles", /Interview - Moshe \(Senior technician\)/.test(await text("#files-list")) && /WhatsApp: chat/.test(await text("#files-list")));
  await shot("sources-full");

  // limit the Google account to Sales
  await page.click(`${g} [data-edit-vis]`);
  await page.uncheck(`${g} .panel .vis-everyone`); await page.check(`${g} .panel label:has-text('Sales') input`);
  await page.click(`${g} [data-save-vis]`);
  check("Changing who can find an account is confirmed", await toastSeen(/Saved/));
  check("…and shown on the account", await until(async () => /Who can find this:\s*Sales/.test(await text(`${g} .acc-head`)), 5000, 300));

  // ------------------------------------------------------------------ 10. Ask as admin
  await go("ask");
  await page.waitForSelector("#page-ask.is-new");
  check("Ask: greets by name, chat box in the middle, example questions", /Good (morning|afternoon|evening), Yaniv/.test(await text("#chat-hello h1"))
    && (await page.$$("#chat-suggest button")).length >= 2);
  check("Ask: a + button starts a new chat (no separate button)", await page.isVisible("#ask-form #new-chat") && !/New conversation/.test(await text("#page-ask")));
  await shot("ask-welcome");
  await page.fill("#question", "How much is the Levi Gardens maintenance contract?"); await page.press("#question", "Enter");
  await page.waitForSelector(".msg.bot .msg-actions", { timeout: 30000 });
  const answer = await text(".msg.bot");
  check("Ask: answer cites the right email, shown as a source chip", /Levi Gardens/.test(await text(".msg.bot .src-chips")), answer.slice(0, 300));
  check("Ask: your question on the right, the answer with the Cortex mark", await page.isVisible(".msg.user .bubble") && await page.isVisible(".msg.bot .bot-avatar"));
  await page.click(".msg.bot .msg-actions button[data-v='-1']");
  check("Feedback is confirmed", await toastSeen(/Thanks/));
  check("Ask: 'Search in' lists the kinds of sources that exist", await page.evaluate(() =>
    [...document.querySelectorAll("#scope-filter option")].map((o) => o.textContent).join(",")).then((t) => /Emails/.test(t) && /Calendar/.test(t) && /Slack/.test(t) && /Social media/.test(t)));
  await page.fill("#question", "How many likes did our last Instagram post get?"); await page.press("#question", "Enter");
  await until(async () => (await page.$$(".msg.bot .msg-actions")).length === 2, 30000, 300);
  check("Ask: Instagram question uses the account summary", /Instagram summary/.test(await text(".msg.bot:last-of-type .src-chips")), await text(".msg.bot:last-of-type"));
  await page.selectOption("#scope-filter", "kind:calendar");
  await page.fill("#question", "When is the supplier visit?"); await page.press("#question", "Enter");
  await until(async () => (await page.$$(".msg.bot .msg-actions")).length === 3, 30000, 300);
  check("Ask: calendar question finds the calendar", /Calendar|Supplier visit/.test(await text(".msg.bot:last-of-type")));
  check("Ask: 'Calendar only' searched only the calendar", !/Email:/.test(await text(".msg.bot:last-of-type .src-chips")));
  await page.selectOption("#scope-filter", "");
  await shot("ask-answers");
  await page.evaluate(() => {   // how a requested file is handed over: inside the answer, with Open and Download
    const bot = addBot();
    const src = [{ n: 1, kind: "file", filename: "Proposal & Agreement - Operad.pdf", title: "Proposal & Agreement - Operad", document_id: 1,
      date: "2026-03-04T08:00:00Z", to: "Dima Binkovsky <dima@operad.com>", email_subject: "Re: Call summery" }];
    const text = "Here is the last proposal you sent to Cut Inside, on 4 March 2026 [1].\n[[file:1]]";
    bot.querySelector(".answer").innerHTML = renderAnswer(text, src); finishBot(bot, text, src, null);
  });
  check("Ask: a requested file appears inside the answer with Open and Download", await page.isVisible(".msg.bot:last-of-type .answer .file-card")
    && /Proposal & Agreement - Operad\.pdf[\s\S]*Attached to "Re: Call summery"[\s\S]*sent[\s\S]*to Dima Binkovsky[\s\S]*Open[\s\S]*Download/.test(await text(".msg.bot:last-of-type .file-card"))
    && (await page.$$(".msg.bot:last-of-type .src-chips")).length === 0, await text(".msg.bot:last-of-type"));
  await page.locator(".msg.bot:last-of-type").scrollIntoViewIfNeeded();
  await shot("ask-file-card", false);
  const firstCid = await page.evaluate(() => chat.cid);
  await page.click("#new-chat");
  check("Ask: + starts a fresh chat", await until(async () => await page.isVisible("#page-ask.is-new") && (await page.$$(".msg")).length === 0, 5000, 200)
    && await page.evaluate(() => chat.cid === null));
  await go("conversations");
  check("Conversations: the chat is listed with its first question as the title", await until(async () => /How much is the Levi Gardens maintenance contract/.test(await text("#conv-list"))
    && /3 questions/.test(await text("#conv-list")), 8000, 300), await text("#conv-list"));
  await page.click(`#conv-list [data-conv="${firstCid}"]`);
  check("Conversations: opening one brings back the whole chat", await until(async () => !(await page.isHidden("#page-ask")) && (await page.$$(".msg.bot")).length === 3, 8000, 300));
  await page.fill("#question", "And when is it paid?"); await page.press("#question", "Enter");
  await until(async () => (await page.$$(".msg.bot .msg-actions")).length === 4, 30000, 300);
  check("Conversations: continuing adds to the same conversation", await page.evaluate(() => chat.cid) === firstCid);
  await shot("conversation-continued");

  // ------------------------------------------------------------------ 10a. Brain (browse everything like a drive)
  await go("brain");
  check("Brain: a folder for each connected app, in its colours, with counts", await until(async () => (await page.$$("#brain-view .folder-tile")).length >= 6
    && /Gmail[\s\S]*Google Drive|Google Drive[\s\S]*Gmail/.test(await text("#brain-view")) && (await page.$$("#brain-view .folder-badge")).length >= 4, 10000, 300));
  await shot("brain");
  await page.click('#brain-view [data-folder="gmail"]');
  check("Brain: opening Gmail lists its emails, newest first, with sender and recipient", await until(async () => (await page.$$("#brain-view .fl-row")).length >= 10
    && /Gmail[\s\S]*items/.test(await text("#brain-status")), 8000, 300));
  await page.click("#brain-view .fl-row >> nth=0");
  check("Brain: clicking an item shows its preview with Open and Ask about this", await until(async () => await page.isVisible("#brain-preview .pv-text")
    && /Open[\s\S]*Ask about this/.test(await text("#brain-preview .pv-actions")) && /From/.test(await text("#brain-preview .pv-meta")), 8000, 300));
  await shot("brain-preview");
  await page.click("#brain-preview [data-ask-about]");
  check("Brain: Ask about this opens the chat with the item named", await until(async () => !(await page.isHidden("#page-ask")) && /^About "/.test(await page.inputValue("#question")), 5000, 200));
  await page.fill("#question", "");
  await go("sources"); await go("brain");
  await page.click('#brain-side [data-folder="type:file"]');
  check("Brain: files show their type", await until(async () => /DOCX|PDF|TXT/.test(await text("#brain-view")), 8000, 300));

  // ------------------------------------------------------------------ 10b. Learning Center (admin builds the path)
  await go("learning");
  check("Learning Center: explains what it is and is empty at first", await until(async () => /New employee role see only these items/.test(await text("#learn-intro"))
    && /Nothing in the learning path yet/.test(await text("#learn-list")), 5000, 200));
  await page.click("#learn-add");
  await page.fill("#dialog[open] [data-q]", "Moshe");
  await until(async () => (await page.$$("#dialog[open] [data-add]")).length > 0, 8000, 300);
  await page.click("#dialog[open] [data-add] >> nth=0");
  check("Learning Center: an item is added from the knowledge base", await toastSeen(/Added to the learning path/)
    && await until(async () => /In the path/.test(await text("#dialog[open] [data-list]")), 5000, 300));
  await page.click("#dialog[open] [data-close]");
  check("…and listed as step 1", await until(async () => /Interview - Moshe/.test(await text("#learn-list")), 5000, 300));
  await page.click("#learn-list [data-note]");
  await page.fill("#dialog[open] textarea[name=note]", "How we handle a new customer order – read this first.");
  await page.click("#dialog[open] [data-save]");
  check("Learning Center: a note for new employees is shown", await until(async () => /read this first/.test(await text("#learn-list")), 5000, 300));
  const adminToken = await page.evaluate(() => localStorage.getItem("kd_token"));
  const created = await page.request.post(`${APP}/api/users`, { headers: { Authorization: `Bearer ${adminToken}` },
    data: { name: "Noa New", email: "noa@acme.test", password: "noa-pass-123", role: "learner", active: true, department_ids: [] } });
  check("A new employee can be added", created.ok());
  await go("people"); await go("learning");
  check("Learning Center: shows the new employee's progress", await until(async () => /Noa New[\s\S]*0 of 1/.test(await text("#learn-people")), 5000, 300));
  await shot("learning-admin");

  // ------------------------------------------------------------------ 11. A team member only sees what they're allowed to
  await Promise.all([page.waitForNavigation(), page.click("#logout")]); await page.waitForSelector("#login-form");
  await page.fill("input[name=email]", "dana@acme.test"); await page.fill("input[name=password]", danaPassword);
  await page.click("#login-form button"); await page.waitForSelector("#app-view:not(.hidden)");
  check("Team member lands on the chat, with New chat in the sidebar", !(await page.isHidden("#page-ask")) && /New chat/.test(await text("#nav")));
  check("Team member sidebar: Chat, Brain and Learning Center", (await page.$$eval("#nav a[data-page]:not(.recent-head a)", (as) => as.map((a) => a.textContent))).join(",") === "Chat,Brain,Learning Center");
  await page.click("#user-name");
  await page.fill("#dialog[open] input[name=current]", danaPassword); await page.fill("#dialog[open] input[name=next]", "dana-new-pass-1");
  await page.click("#dialog[open] button[type=submit]");
  check("Team member can change their own password", await toastSeen(/Password changed/));
  await page.goto(APP + "/app#settings"); await sleep(500);
  check("Team member can't open admin pages", !(await page.isVisible("#page-settings")));
  await go("ask");
  await page.fill("#question", "What is the senior technician salary?"); await page.press("#question", "Enter");
  const danaAnswered = await until(async () => (await page.$$(".msg.bot .msg-actions")).length > 0 || /couldn't find/.test(await text("#messages")), 30000, 300);
  check("Team member never gets HR-only information", danaAnswered && !/14,000/.test(await text("#messages")), await text("#messages"));
  await go("brain");
  check("Brain: folders for the connected apps Dana may see", await until(async () => /Gmail/.test(await text("#brain-view")), 10000, 300) && !/Uploaded files/.test(await text("#brain-side")) || /Gmail/.test(await text("#brain-view")));
  await page.fill("#brain-search", "Salaries"); await sleep(900);
  check("Brain: HR-only file is not listed for Sales", /Nothing matches/.test(await text("#brain-view")));
  await page.fill("#brain-search", "Levi"); await sleep(900);
  check("Brain: Sales can see the Google emails", /Levi Gardens/.test(await text("#brain-view")));
  await shot("member-library");

  // ------------------------------------------------------------------ 11b. A new employee sees only the learning path
  await Promise.all([page.waitForNavigation(), page.click("#logout")]); await page.waitForSelector("#login-form");
  await page.fill("input[name=email]", "noa@acme.test"); await page.fill("input[name=password]", "noa-pass-123");
  await page.click("#login-form button"); await page.waitForSelector("#app-view:not(.hidden)");
  check("New employee sidebar: only the Learning Center and Chat", (await page.$$eval("#nav a[data-page]:not(.recent-head a)", (as) => as.map((a) => a.textContent))).join(",") === "Learning Center,Chat");
  check("New employee lands on the Learning Center with the path and note", await until(async () => !(await page.isHidden("#page-learning"))
    && /Interview - Moshe/.test(await text("#learn-list")) && /read this first/.test(await text("#learn-list")) && /0 of 1/.test(await text("#learn-progress")), 8000, 300));
  check("New employee doesn't see admin buttons", !(await page.isVisible("#learn-add")) && !(await page.isVisible("#learn-people-card")));
  await page.click("#learn-list [data-done]");
  check("Mark as done updates the progress", await until(async () => /1 of 1/.test(await text("#learn-progress")) && /All done/.test(await text("#learn-progress")), 5000, 300));
  await shot("learning-new-employee");
  await go("ask");
  await page.fill("#question", "What is the senior technician salary?"); await page.press("#question", "Enter");
  await until(async () => (await page.$$(".msg.bot .msg-actions")).length > 0 || /couldn't find/.test(await text("#messages")), 30000, 300);
  check("Logging out leaves nothing of the previous person's questions", !/Levi Gardens|Supplier visit/.test(await text("#messages")), await text("#messages"));
  check("New employee's answers come only from the learning path", !/14,000/.test(await text("#messages")) && !/Salaries|Levi Gardens|Email:/.test(await text("#messages .src-chips")), await text("#messages"));
  await page.goto(APP + "/app#library"); await sleep(500);
  await go("brain"); await sleep(400);
  check("New employee can't open the Brain", !(await page.isVisible("#page-brain")));

  // ------------------------------------------------------------------ 12. Activity and health (admin)
  await Promise.all([page.waitForNavigation(), page.click("#logout")]); await page.waitForSelector("#login-form");
  await page.fill("input[name=email]", "admin@acme.test"); await page.fill("input[name=password]", "admin-pass-123");
  await page.click("#login-form button"); await page.waitForSelector("#app-view:not(.hidden)");
  await until(async () => /Items in the knowledge base/.test(await text("#home-kpis")), 10000, 300);
  check("Home: setup checklist complete (hidden)", (await text("#home-setup")).trim() === "");
  check("Home: questions-per-day chart drawn", await until(async () => (await page.$$("#home-q-chart svg path, #home-q-chart svg rect:not(.hit)")).length > 0, 5000, 300));
  check("Home: questions by person shows who asked", /Yaniv Barak/.test(await text("#home-by-person")) && /Dana Cohen/.test(await text("#home-by-person")));
  check("Home: 'Keeping up to date' lists live sources with their last new item", /Keeping up to date/.test(await text("#home-learning")) &&
    /Gmail[\s\S]*Live/.test(await text("#home-learning")) && /Last new item/.test(await text("#home-learning")));
  await page.hover("#home-q-chart .hit >> nth=-1");
  check("Home: hovering a day shows its numbers", await until(async () => /Answered/.test(await page.textContent("#viz-tip")), 3000, 200));
  await shot("home-after-setup");
  await go("conversations");
  await page.click("#conv-scope [data-scope=all]");
  check("Conversations (admin): everyone's chats, with Dana's", await until(async () => /Dana Cohen/.test(await text("#conv-list")), 8000, 300));
  await page.check("#q-gaps");
  check("Conversations (admin): the thumbs down chat is listed as a gap", await until(async () => /Levi Gardens/.test(await text("#conv-list")) && /Not helpful|No answer found/.test(await text("#conv-list")), 8000, 300));
  await shot("conversations");
  await page.uncheck("#q-gaps"); await page.click("#conv-scope [data-scope=mine]");
  await go("health");
  await until(async () => /Claude/.test(await text("#health-list")), 15000, 300);
  const health = await text("#health-list");
  check("Health: every check is listed in plain words", /AI answers/.test(health) && /Search engine/.test(health) && /Connected sources/.test(health));
  check("Health: sources reported as working", /all working/.test(health), health);
  check("Health: AI key box stays hidden while answers work", !(await page.isVisible("#ai-card")));
  await page.evaluate(() => document.querySelector("#ai-card").classList.remove("hidden")); // as when the key is missing
  await page.fill("#ai-form input[name=key]", "hello"); await page.click("#ai-form button");
  check("Health: a wrong AI key gets a clear message", await until(async () => /doesn.t look like a Claude API key \(received: 5 characters\)/.test(await text("#ai-error")), 5000, 200));
  await page.evaluate(() => document.querySelector("#ai-card").classList.add("hidden"));
  const [download] = await Promise.all([page.waitForEvent("download"), page.click("#health-download")]);
  check("Health: report can be downloaded", /cortex-health/.test(download.suggestedFilename()));
  await go("settings");
  check("Settings: only company and plan – no technical key box", !(await page.$("#page-settings #ai-form")) && await until(async () => await page.isVisible("#company-form")
    && /Plan and billing/.test(await text("#plan-card")) && (await page.inputValue("#company-form input[name=name]")) !== "", 8000, 200));
  await page.fill("#company-form input[name=name]", "Acme Plumbing Ltd");
  await page.fill("#company-form input[name=website]", `${SIM}/site/`);
  await page.click("#company-form button[type=submit]");
  check("Settings: the logo is found on the company website", await until(async () => await page.isVisible("#company-logo") && await page.isVisible("#logo-preview img"), 15000, 300));
  check("Sidebar: logo links to the company website", (await page.getAttribute("#company-link", "href")) === `${SIM}/site/`);
  await shot("settings");

  // ------------------------------------------------------------------ 13. Disconnect
  await go("sources");
  await page.waitForSelector(`${m} [data-disconnect]`);
  await page.click(`${m} [data-disconnect]`);
  check("Disconnect asks for confirmation and explains the effect", /removed from Cortex/.test(await text("#dialog[open]")));
  await page.click("#dialog[open] [data-yes]");
  check("Disconnect: account removed", await until(async () => (await page.$$(`${m} .account`)).length === 0, 15000, 500));
  check("…and the big Connect button is back", (await text(`${m} [data-open-connect]`)).trim() === "Connect Microsoft account");

  // ------------------------------------------------------------------ 14. Phone layout
  await page.setViewportSize({ width: 390, height: 844 });
  for (const p of ["home", "ask", "sources", "people", "learning", "conversations", "brain", "health", "settings"]) {
    await go(p); await sleep(1200);
    check(`Phone: ${p} page fits the screen (no sideways scrolling)`, await noOverflow());
    const emojis = await emojisOnPage();
    check(`${p} page uses icons, no emojis`, !emojis, emojis);
    await shot(`phone-${p}`, p !== "sources");
  }

  // ------------------------------------------------------------------ 15. Marketing page
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(APP + "/");
  check("Marketing page: clear hero headline", /Ask your business anything/.test(await text("h1")) && /Cortex/.test(await text(".logo")));
  check("Marketing page: the brain shows the connected tools", (await page.$$("#net .node")).length >= 10);
  check("Marketing page: the example appears (Ask, Find, Do)", await until(async () => (await page.$$("#out .chip, #out .result")).length > 0, 8000, 300));
  await page.click(".mode[data-mode='2']");
  check("Marketing page: Do shows a finished task", await until(async () => /Briefing ready/.test(await text("#out")), 8000, 300));
  await page.click("#tabs .tab >> nth=4");
  check("Marketing page: team tabs switch the example", /Instagram/.test(await text("#team")));
  check("Marketing page: See how it works opens the dashboard", (await page.getAttribute(".ctas .btn.glow", "href")) === "/app#home");
  check("Marketing page: no small labels above headlines", (await page.$$(".tag, .badge, .num")).length === 0);
  check("Marketing page: no dashes in the text", !/[–—]/.test(await page.evaluate(() => document.body.innerText)));
  check("Marketing page: product screenshots load", await page.evaluate(async () => (await Promise.all([...document.images].map((i) => fetch(i.src).then((r) => r.ok && /image/.test(r.headers.get("content-type")))))).every(Boolean)));
  check("Marketing page: buttons lead to the product", (await page.getAttribute(".nav-cta .btn", "href")) === "/app#home");
  await page.setViewportSize({ width: 390, height: 844 });
  check("Marketing page fits a phone screen", await noOverflow());
  check("Dashboard links back to the website (login page and sidebar)", (await (await page.request.get(APP + "/app")).text()).includes('id="to-website" class="side-link" href="/"'));
  await page.click(".nav-cta .btn"); await page.waitForURL(/\/app/);
  check("Website button opens the dashboard", await until(async () => await page.isVisible("#app-view") || await page.isVisible("#login-view"), 8000, 300));

  check("No JavaScript errors during the whole run", consoleErrors.length === 0, consoleErrors.join(" | "));
  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
  fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify(results, null, 2));
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
