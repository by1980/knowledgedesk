# Cortex

A private "ask the company" assistant for small and medium businesses. It is installed on the
customer's own server. Employees ask questions in plain language (Hebrew, English or any other
language) and get answers with sources. The answers come only from the company's own documents
and recorded employee interviews, and each person only sees what their departments are allowed
to see.

## What it does

- **Tasks: it tells you what needs you.** On the Tasks page (where team members start; admins
  start on Home) everyone sees their meetings today and coming up, each with a **Prepare me** button, and **Needs your
  attention**: emails waiting for their reply, things they promised to send, contracts and
  renewals coming up, people to follow up with. Each item shows why, where it comes from, and
  one button that gets the work started (usually an email draft). **Done** removes it, **Later**
  brings it back tomorrow. The list is made by the AI from what that person is allowed to see,
  refreshed every 4 hours or with **Refresh**. Nothing is invented: every item points to its source.
- **Writes your emails, you send them.** An email draft in an answer gets **Open in email** (opens
  your own mail app with the recipient, subject and text filled in) and **Copy email**. Cortex
  itself never sends anything.
- **Answers questions by department.** Each document belongs to one or more departments, or to
  everyone. Sales staff can never get answers built from HR files. This is enforced in the
  database search, before the AI ever sees anything.
- **Reads almost any file:** PDF, Word, Excel, PowerPoint, CSV, text, HTML, emails (.eml),
  subtitles, and **audio/video recordings**. Recordings are transcribed automatically on the
  server.
- **Employee interviews:** upload a recording of an interview with an experienced employee. It
  is transcribed and turned into structured knowledge notes (processes, contacts, tips, and
  questions it answers). Answers say "according to Moshe…".
- **Connected sources** (Admin → Connections), synced automatically every 30 minutes:
  - 📧 **Email inboxes** (Gmail / Google Workspace, and any IMAP mailbox): emails and their
    attachments (PDF, Word, Excel…).
  - 📅 **Calendars** (Google Calendar or Outlook, via the calendar's private iCal link).
  - 🌐 **Websites** (company site, help pages, price pages).
  - **Slack** (one-click, all public channels and private channels the app is added to).
  - 💬 **WhatsApp chats and groups**, imported from the phone's "Export chat" file. Voice notes
    are transcribed.
- **Files from anywhere:** upload in the browser, or drop files into `data/sources/<Department>/`
  on the server. That folder can be kept in sync with Google Drive, OneDrive/SharePoint,
  Dropbox, or a network share (see below).
- **Answers with citations.** Every answer links to the source documents. The assistant says so
  when it doesn't know instead of guessing.
- **Admin dashboard:** departments, users, documents, and the questions people asked. Questions
  with no sources or a 👎 show which documentation is missing.
- **Subscription license:** a signed license file with an expiry date and a user limit, to
  support a monthly fee.

## How it works

```
Files / recordings ──► text extraction / speech-to-text (local) ──► chunks ──► local embeddings
                                                                              │
                                                     Postgres + pgvector  ◄───┘
Question ──► permission filter + hybrid search (meaning + keywords) ──► top sources ──► Claude ──► answer + citations
```

- **Runs on the customer's server:** the database, search, speech-to-text and embeddings.
- **Sent to Claude (Anthropic API) per question:** the question and the few relevant passages,
  to write the answer. Interview transcripts are also sent once, to create the knowledge notes.
  Data sent through the API is not used to train models.

| Part | Technology |
|---|---|
| Web server and API | Python, FastAPI |
| Database and vector search | PostgreSQL with pgvector |
| Embeddings (search) | `intfloat/multilingual-e5-large` via fastembed, local |
| Speech-to-text | Whisper via faster-whisper, local |
| Answers | Claude via the Anthropic API |
| Web interface | Plain HTML/JS with right-to-left support, served by the same app |

## Who sees what: three circles

Everyone in the company brings their own assets (email, calendar, Drive, documents, Slack channels...). Each asset
is in one circle, chosen by the person who adds it:

| Circle | Who can find it | Starts here by default |
|---|---|---|
| **Only me** | Only that person. Admins included: they can't search or open it. | Email and calendar |
| **Teams** | People in the chosen departments. Private Slack channels: only their members. | Team folders, private channels |
| **Everyone** | The whole company | Website, social media, public Slack channels, company guides |

- Team members connect their own Google or Microsoft account and add documents under **My sources**, sharing with
  only themselves, their own teams, or everyone. Admins manage company sources under **Sources**.
- Answers mark private items "Only you". When an admin opens someone else's conversation, answers that used that
  person's private items are hidden.
- The rule is enforced in the database for every search, list, preview and download.
- Updating from an earlier version makes connected email and calendars "Only me" for the admin who connected them,
  once. Everything else keeps its circle.

## Installing at a customer

**Server:** any Linux server or small PC (Ubuntu 22.04/24.04 recommended), 4+ CPU cores, 16 GB
RAM, 50 GB disk. A GPU is not needed. Long recordings transcribe faster with one.

```bash
git clone https://github.com/<you>/knowledgedesk.git
cd knowledgedesk
./install.sh
```

The installer asks for the company name, admin login and Claude API key, creates `.env` with
random passwords, and starts everything with Docker. The first start downloads the AI models
(a few GB). Then open `http://<server-ip>:8000/app` and log in as the admin (`http://<server-ip>:8000` is the Cortex website).

**First setup in the app.** The **Home** page shows admins a 3-step checklist:
1. **Sources:** connect Google or Microsoft accounts, add the website, and upload files,
   interviews or WhatsApp chats. Each source shows its status (⏳ Importing, ✓ N items
   searchable, or ⚠ a plain-English problem), and **View items** shows exactly what was imported.
2. **People:** create departments (Sales, Service, HR…) and add employees.
3. **Ask:** try a question. Employees see only **Ask** and **Library**.

**Settings → Health check** shows whether answers, search, sources and the subscription are working,
and lists recent problems. **Download report** saves it as a file to send to support.
**Settings → Claude API key** is where the admin pastes or replaces the key. It is tested before
it is saved and stored encrypted, and it overrides `ANTHROPIC_API_KEY` in `.env`.

**Importing everything.** Connected mailboxes import **all** mail, except spam, trash, promotions
and social. Big mailboxes are imported 1,000 messages per round, with a new round every minute
until everything is in, and the Sources page shows "Importing 1,200 of 8,400". Items are then
prepared for search one by one. Websites, documents and calendars go first, then emails, newest
first. You can ask questions while this is still running.

**Updating a customer to a new version:**
```bash
cd knowledgedesk && git pull && sudo docker compose up -d --build
```

**Backups:** everything lives in the `data/` folder (database, uploaded files, license). Back up
that folder.

### Connecting Google Drive, OneDrive/SharePoint, Dropbox…

Each folder inside `data/sources/` is a department, and `_company` is visible to everyone. Use
`Sales+Support` to share one folder with two departments. The app checks these folders every 5
minutes. It indexes new and changed files and removes deleted ones.

To fill them from cloud storage, install [rclone](https://rclone.org) on the server, connect the
customer's account (`rclone config`), and sync on a schedule (crontab):

```
*/15 * * * * rclone sync "gdrive:Company/Sales" /opt/knowledgedesk/data/sources/Sales
*/15 * * * * rclone sync "onedrive:HR Policies" /opt/knowledgedesk/data/sources/HR
```

### One-click: Connect Google / Microsoft account

In **Admin → Connections → Connect an account in one click**, choose Email, Calendar and/or Files,
choose who can see them, and click **Connect Google account** or **Connect Microsoft account**.
After approving on Google's or Microsoft's page, the account's mail, calendars and Drive/OneDrive
files are imported and kept in sync. Access is read-only. Tokens are stored encrypted, and removing
the connections forgets the account.

This needs a one-time setup by you, the vendor (below).

## Setting up one-click sign-in (vendor, once, free)

You register "Cortex" once with Google and once with Microsoft. Every customer then uses
that same registration.

**The return address.** After someone approves, Google and Microsoft send the browser to a fixed
address that you register.
- **Testing on your own computer:** use `http://localhost:8000/oauth/callback`. Nothing else is
  needed.
- **Customer servers:** put the single file `relay/oauth.html` on your own website, for example
  `https://connect.yourcompany.com/oauth.html`, and register that address. It forwards the browser
  back to whichever customer server started the sign-in. It holds no secrets.

Register **both** addresses with Google and with Microsoft.

### Google (console.cloud.google.com)
1. Create a project named `Cortex`.
2. **APIs & Services → Library:** enable **Gmail API**, **Google Calendar API** and **Google Drive API**.
3. **Google Auth Platform → Branding:** app name `Cortex`, your support email, your domain
   and privacy policy link. **Audience:** *External*, then **Publish app** (status *In production*).
   In *Testing* status, connections stop working after 7 days.
4. **Clients → Create client → Web application.** Under *Authorized redirect URIs*, add both return
   addresses. Copy the **Client ID** and **Client secret**.
5. Until Google verifies the app, users see "Google hasn't verified this app" and click **Advanced →
   Go to Cortex**. Up to 100 accounts can connect this way. Calendar-only verification is
   free. Gmail and Drive access beyond 100 accounts requires Google's paid security assessment. A
   Google Workspace customer can instead mark the app as trusted (Admin console → Security → API
   controls → App access control).

### Microsoft (entra.microsoft.com)
1. **App registrations → New registration:** name `Cortex`. Supported account types:
   *Accounts in any organizational directory and personal Microsoft accounts*. Redirect URI:
   platform **Web**, `http://localhost:8000/oauth/callback`. Then add the other return address
   under **Authentication**.
2. **Certificates & secrets → New client secret** (24 months). Copy the **Value** and put a reminder
   in your calendar to renew it.
3. **API permissions → Add → Microsoft Graph → Delegated:** `Mail.Read`, `Calendars.Read`,
   `Files.Read.All`, `User.Read`, `offline_access`.
4. Copy the **Application (client) ID** from Overview.
5. A customer whose IT blocks new apps has an admin click "Grant admin consent" once. This is free.

### Slack (api.slack.com/apps)
Slack only accepts **https** return addresses, so Slack needs the return page on your website
(`relay/oauth.html`). `http://localhost` doesn't work for Slack.
1. **Create New App → From scratch:** name it `Cortex` and pick your own workspace.
2. **OAuth & Permissions:**
   - Under *Redirect URLs*, add your return address, for example
     `https://connect.yourcompany.com/oauth.html`.
   - Under *Bot Token Scopes*, add `channels:read`, `channels:history`, `channels:join`,
     `groups:read`, `groups:history` and `users:read`.
3. **Manage Distribution → Activate Public Distribution.** This is free and lets your customers'
   workspaces install the app.
4. Under **Basic Information**, copy the **Client ID** and **Client Secret** into `.env`
   (`SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`) or into `app/vendor_oauth.json`.

Cortex reads all public channels and private channels the app was added to, including
thread replies. It never posts anything.

### Facebook and Instagram (developers.facebook.com)
One "Connect Facebook account" step reads the customer's Facebook Pages and the Instagram business
accounts linked to them: posts, captions, likes, comments, shares and follower counts. Like Slack, it
needs the **https** return page.
1. **My Apps → Create app**, use case **Other**, type **Business**, name `Cortex`.
2. Add the **Facebook Login for Business** product. Under *Settings*, add your return address to
   *Valid OAuth Redirect URIs*.
3. **App settings → Basic:** add your privacy policy link and app icon. Copy the **App ID** and
   **App secret** into `.env` (`META_APP_ID`, `META_APP_SECRET`) or into `app/vendor_oauth.json`
   under `"meta"`.
4. **App Review → Permissions and features:** request `pages_show_list`, `pages_read_engagement`,
   `pages_read_user_content`, `instagram_basic`, `instagram_manage_insights` and
   `business_management`. Until Meta approves them, only people with a role on the app (add them
   under **App roles**) can connect. Approval is free and needs a short screen recording of the
   Connect flow. Then switch the app to **Live**.

Facebook asks people to sign in again every 60 days. When that happens, the Sources page says so
and shows the Connect button.

### YouTube
YouTube uses the Google sign-in. In the Google project, also enable **YouTube Data API v3**
(APIs & Services → Library), and add the `youtube.readonly` scope under *Data access*. Adding YouTube
to an already connected Google account keeps its Gmail, Calendar and Drive access.

### LinkedIn
LinkedIn only lets approved marketing partners read company pages. Customers import the export
instead: on their company page, **Analytics → Content → Export**, then **Import LinkedIn file** on
the Sources page.

### How social numbers stay current
Posts and their numbers are checked every hour. Each account also gets a summary item: latest post,
this week and last week, the last 7 and 30 days, the best post of the month, week-by-week totals,
and follower changes. Questions like "how many likes did we get last week?" are answered from it,
with the date the numbers were taken.

### Putting the keys in
- **One computer:** add `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `MICROSOFT_CLIENT_ID` and
  `MICROSOFT_CLIENT_SECRET`, `SLACK_CLIENT_ID`/`SLACK_CLIENT_SECRET` and `META_APP_ID`/`META_APP_SECRET`
  (plus `OAUTH_REDIRECT_URL` for the relay address) to `.env`, then run
  `docker compose up -d`.
- **Every installation:** copy `app/vendor_oauth.example.json` to `app/vendor_oauth.json` and fill it
  in. It is then built into every installation. Keep the GitHub repository private, because this
  file contains your app secrets.

### Connecting email, calendar, website and WhatsApp (other ways)

Go to **Admin → Connections**. Each source gets a name and the departments allowed to see what
comes from it. For example, the Sales inbox can be visible to Sales only.

- **Gmail / Google Workspace:** turn on 2-Step Verification for the mailbox, create an
  [app password](https://myaccount.google.com/apppasswords), then connect with server
  `imap.gmail.com`, port `993`. The app password is stored encrypted. Use "Test connection" to see
  the folder names, for example `INBOX, [Gmail]/Sent Mail`. The first sync imports the last 365
  days (up to 500 emails per folder per run), then only new mail.
- **Google Calendar:** Settings → the calendar → Integrate calendar → *Secret address in iCal
  format*. **Outlook:** Settings → Calendar → Shared calendars → Publish → ICS link. Events from the
  last 4 months to the next 12 months are imported.
- **Website:** enter the address. Up to 200 pages of that site are read.
- **WhatsApp:** WhatsApp doesn't let other apps read chats. On the phone, use *Export chat* (with
  media, to include voice notes) and upload the file in Documents. Upload a new export of the same
  chat later to update it.
- **Microsoft 365 / Outlook.com mailboxes:** use **Connect Microsoft account**, since Microsoft
  doesn't allow app passwords. Live WhatsApp Business connection is not supported yet.

### Interviews with employees

The best knowledge in a small business is in people's heads. Record a 20–40 minute conversation
with each key employee (a phone recorder is fine) and upload it as an **Employee interview**
with their name and role. Good questions to ask:
- Walk me through your typical day/week. What are the steps for <main task>?
- What goes wrong most often, and how do you fix it?
- Who do you call for <X>? Which suppliers and customers need special handling?
- What do new employees always ask you?
- Where are things kept: files, tools, passwords policy, stock?

## Your business model: setup fee + monthly subscription

The license system lets you stop service if a customer stops paying, without visiting them.

**Once, on your own computer:**
```bash
pip install cryptography
python tools/license_tool.py keygen
```
This creates `vendor_private_key.txt`, which is secret and never shared or uploaded (git ignores
it), and `app/vendor_public_key.txt`, which you commit. Back up the private key: if you lose it,
you can't issue new licenses.

**Each billing period, per customer:**
```bash
python tools/license_tool.py issue --customer "Cohen Plumbing Ltd" --expires 2027-01-31 --max-users 25
```
Send the `.key` file to the customer. They install it under **Admin → System**, or you copy it to
`data/license.key`.

- After the expiry date there is a 14-day grace period with a warning banner. Then questions stop
  until a new license is installed. Documents and data are never deleted.
- `--max-users` limits how many active users they can create (0 = unlimited).
- A technical customer with access to the server could remove the check. The license is a
  reminder and an enforcement tool for normal customers. Your contract is the real protection.

**Costs to include in your pricing:** the Claude API is billed per use by Anthropic. A typical
question costs a few cents. Each customer can have their own API key on their own Anthropic
account, or you can use yours and include it in the monthly fee.

## Developing

```bash
# Needs PostgreSQL 16 with pgvector running locally
pip install -r requirements.txt -r requirements-dev.txt
createdb kd_test            # or set TEST_DATABASE_URL
python -m pytest
```

Tests use a small embedding model and a fake Claude, so they need no API key. They check that
department permissions hold, Hebrew questions, folder sync, interviews, feedback and licensing.

**End-to-end QA in a real browser** (needs Node and Playwright):
```bash
tests/e2e/run.sh
```
This starts a fresh copy of the app in test mode, together with a simulated Google and
Microsoft (`tests/e2e/simulator.py`) that has sign-in pages, emails, events, files, a protected
file and a temporary slow-down. It then clicks through every flow like a new customer: sign-in,
connecting accounts, websites, uploads, people, permissions, asking, health and disconnecting.
It takes screenshots on desktop and phone size and fails on any problem. Run it before every
release.

Run locally without Docker:
```bash
export DATABASE_URL=postgresql://user:pass@localhost:5432/kd DATA_DIR=./data ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload
```

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `COMPANY_NAME` | | Shown in the app and used by the assistant |
| `ANTHROPIC_API_KEY` | | Claude API key (required for answers) |
| `LLM_MODEL` | `claude-opus-5-5` | Claude model for answers |
| `LLM_EFFORT` | `low` | `low` = answers in seconds; `medium`/`high` = more careful but slower |
| `PLANNER_MODEL` | `claude-haiku-5-5` | Fast model that turns each question into a search plan (who, when, sent or received, newest first). Falls back to `LLM_MODEL` if unavailable |
| `EMBED_MODEL` | `paraphrase-multilingual-mpnet-base-v2` | Local search model. Changing it re-indexes everything automatically |
| `WHISPER_MODEL` | `small` | Speech-to-text size: `tiny`…`large-v3` |
| `SUMMARIZE_INTERVIEWS` | `true` | Create knowledge notes from interviews |
| `SCAN_INTERVAL_SECONDS` | `300` | How often `data/sources` is checked |
| `CONNECTOR_SYNC_MINUTES` | `30` | How often connected email/calendar/website sources are synced |
| `TOP_K` | `8` | Passages given to the assistant per question |

## Roadmap ideas

- SharePoint team sites and Teams channels
- Respecting each Drive/OneDrive file's own sharing permissions
- WhatsApp Business number for asking questions and capturing new messages live
- Slack or Teams bot for asking questions
- Department managers who can upload for their own department
- Scanned-PDF OCR
- Answer quality test set per customer
