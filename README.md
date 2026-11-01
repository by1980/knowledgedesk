# KnowledgeDesk

A private "ask the company" assistant for small and medium businesses. It is installed on the
customer's own server. Employees ask questions in plain language (Hebrew, English or any other
language) and get answers with sources. The answers come only from the company's own documents
and recorded employee interviews, and each person only sees what their departments are allowed
to see.

## What it does

- **Answers questions by department.** Each document belongs to one or more departments, or to
  everyone. Sales staff can never get answers built from HR files. This is enforced in the
  database search, before the AI ever sees anything.
- **Reads almost any file:** PDF, Word, Excel, PowerPoint, CSV, text, HTML, emails (.eml),
  subtitles, and **audio/video recordings**. Recordings are transcribed automatically on the
  server.
- **Employee interviews:** upload a recording of an interview with an experienced employee. It
  is transcribed and turned into structured knowledge notes (processes, contacts, tips, and
  questions it answers). Answers say "according to Moshe…".
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
(a few GB). Then open `http://<server-ip>:8000` and log in as the admin.

**First setup in the app:**
1. **Admin → Departments:** create departments (Sales, Service, HR, Finance…).
2. **Admin → Users:** add employees and tick their departments.
3. **Admin → Documents:** upload files, or choose "Employee interview" for recordings.
4. Employees log in and ask questions under **Ask**.

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
| `LLM_EFFORT` | `medium` | `low` = faster and cheaper, `high` = more careful |
| `EMBED_MODEL` | `intfloat/multilingual-e5-large` | Local search model. Changing it requires re-indexing |
| `WHISPER_MODEL` | `small` | Speech-to-text size: `tiny`…`large-v3` |
| `SUMMARIZE_INTERVIEWS` | `true` | Create knowledge notes from interviews |
| `SCAN_INTERVAL_SECONDS` | `300` | How often `data/sources` is checked |
| `TOP_K` | `8` | Passages given to the assistant per question |

## Roadmap ideas

- Direct Google Drive / SharePoint connectors with per-file permissions
- WhatsApp, Slack or Teams bot for asking questions
- Department managers who can upload for their own department
- Scanned-PDF OCR
- Answer quality test set per customer
