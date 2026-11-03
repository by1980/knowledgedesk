"""KnowledgeDesk web server: API + web interface."""
import json
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
import re
import shutil
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, branding, embeddings, license, llm, oauth, retrieval, worker
from .config import settings
from .connectors import base as connector_base
from .connectors import calendar_ics, email_imap
from .connectors import runner as connector_runner
from .db import conn, init_schema
from .ingest.extract import SUPPORTED_EXT, is_audio_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


class _RecentProblems(logging.Handler):
    """Keeps the last warnings and errors so admins can see them in Settings → Health."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.items: list[dict] = []

    OURS = ("app", "ingest", "worker", "connectors", "db", "folders", "oauth", "uvicorn.error")

    def emit(self, record: logging.LogRecord) -> None:
        from datetime import datetime, timezone

        if not record.name.startswith(self.OURS):  # ignore chatter from libraries
            return

        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            message += f" ({record.exc_info[0].__name__}: {record.exc_info[1]})"
        self.items.append({"time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                           "level": record.levelname.lower(), "where": record.name, "message": message[:500]})
        del self.items[:-100]


recent_problems = _RecentProblems()
logging.getLogger().addHandler(recent_problems)

STATIC = Path(__file__).parent / "static"
APP_VERSION = "0.9.2"


def _ensure_secret_key() -> None:
    """If no SECRET_KEY was configured, create a random one once and keep it in the data folder."""
    if settings.secret_key and settings.secret_key != "change-me-please":
        return
    key_file = settings.data_dir / "secret.key"
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(secrets.token_hex(32))
        key_file.chmod(0o600)
    settings.secret_key = key_file.read_text().strip()


def startup() -> None:
    _ensure_secret_key()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    (settings.sources_dir / "_company").mkdir(parents=True, exist_ok=True)
    init_schema(embeddings.dimension())
    auth.ensure_first_admin()
    merged = oauth.merge_duplicate_accounts()
    if merged:
        log.info("Merged %d duplicate connected account(s)", merged)
    if settings.run_worker:
        worker.start()
        threading.Thread(target=branding.ensure_defaults, daemon=True).start()
    log.info("KnowledgeDesk started for %s", settings.company_name)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup()
    yield


app = FastAPI(title="KnowledgeDesk", lifespan=lifespan)


# ---------------------------------------------------------------- public

@app.get("/api/status")
def status():
    lic = license.status()
    b = branding.get()
    return {"company": b["name"], "website": b["website"],
            "logo": f"/api/brand/logo?v={b['logo_version']}" if b["logo"] else "", "logo_tone": b["logo_tone"],
            "license": lic.__dict__ | {"allows_use": lic.allows_use},
            "llm_configured": bool(llm.api_key()) or settings.fake_llm, "version": APP_VERSION}


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/login")
def login(body: LoginIn):
    with conn() as c:
        row = c.execute(
            "SELECT id, password_hash, active FROM users WHERE email=%s", (body.email.strip().lower(),)
        ).fetchone()
    if not row or not row["active"] or not auth.verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Wrong email or password")
    return {"token": auth.make_token(row["id"])}


@app.get("/api/me")
def me(user: dict = Depends(auth.current_user)):
    return user


# ---------------------------------------------------------------- asking questions

class AskIn(BaseModel):
    question: str
    department_id: int | None = None
    kinds: list[str] = []  # limit to some kinds of sources, e.g. ["email"]
    history: list[dict] = []


def _clean_history(history: list[dict]) -> list[dict]:
    out = []
    for m in history:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip():
            out.append({"role": m["role"], "content": m["content"][:4000]})
    # The API needs alternating turns that start with the user.
    while out and out[0]["role"] != "user":
        out.pop(0)
    if out and out[-1]["role"] == "user":
        out.pop()
    return out


@app.post("/api/ask")
def ask(body: AskIn, user: dict = Depends(auth.current_user)):
    lic = license.status()
    if not lic.allows_use:
        raise HTTPException(402, lic.message)
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Please type a question")

    is_admin = user["role"] == "admin"
    dept_ids = [d["id"] for d in user["departments"]]
    if body.department_id is not None and not is_admin and body.department_id not in dept_ids:
        raise HTTPException(403, "You are not a member of that department")

    history = _clean_history(body.history)
    scope = {"is_admin": is_admin, "user_depts": dept_ids, "only_dept": body.department_id,
             "kinds": [k for k in body.kinds if k in KINDS] or None}
    hits = retrieval.search(question, **scope)
    # A follow-up ("and for VIP customers?") may need the previous question too. The new question
    # always leads; results for "previous + new" only fill in behind it.
    last_user_q = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    if last_user_q:
        hits = retrieval.fuse(hits, retrieval.search(f"{last_user_q}\n{question}", **scope))
    sources = [
        {"n": i, "document_id": h["document_id"], "title": h["title"], "location": h["location"] or "",
         "kind": h["kind"], "snippet": h["content"][:300],
         "url": (h["metadata"] or {}).get("url") or (h["metadata"] or {}).get("link") or ""}
        for i, h in enumerate(hits, 1)
    ]

    def events():
        yield json.dumps({"type": "sources", "sources": sources}) + "\n"
        answer = []
        try:
            if not hits:
                text = ("I couldn't find anything about this in the documents you have access to. "
                        "Try rephrasing, or ask an admin to add documents on this topic.")
                answer.append(text)
                yield json.dumps({"type": "text", "text": text}) + "\n"
            else:
                for piece in llm.stream_answer(question, retrieval.format_sources(hits), history):
                    answer.append(piece)
                    yield json.dumps({"type": "text", "text": piece}) + "\n"
        except llm.LLMNotConfigured as e:
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"
        except Exception:  # noqa: BLE001
            log.exception("Answer failed")
            yield json.dumps({"type": "error", "message": "The assistant is unavailable right now. Please try again."}) + "\n"
        with conn() as c:
            qid = c.execute(
                "INSERT INTO questions(user_id, department_id, question, answer, sources, found_sources) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (user["id"], body.department_id, question, "".join(answer), json.dumps(sources), bool(hits)),
            ).fetchone()["id"]
        yield json.dumps({"type": "done", "question_id": qid}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


KINDS = {"email", "file", "calendar", "slack", "whatsapp", "web", "interview", "social"}


class FeedbackIn(BaseModel):
    value: int  # 1 or -1


@app.post("/api/questions/{qid}/feedback")
def feedback(qid: int, body: FeedbackIn, user: dict = Depends(auth.current_user)):
    with conn() as c:
        c.execute("UPDATE questions SET feedback=%s WHERE id=%s AND user_id=%s",
                  (1 if body.value > 0 else -1, qid, user["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- what can be searched

@app.get("/api/overview")
def overview(user: dict = Depends(auth.current_user)):
    is_admin = user["role"] == "admin"
    dept_ids = [d["id"] for d in user["departments"]]
    with conn() as c:
        rows = c.execute(
            "SELECT d.kind, d.source, d.status, count(*) AS n, "
            " sum(coalesce((d.metadata->>'events')::int, (d.metadata->>'messages')::int, 1)) AS events FROM documents d "
            "WHERE %s OR d.company_wide OR EXISTS (SELECT 1 FROM document_departments dd "
            "  WHERE dd.document_id=d.id AND dd.department_id = ANY(%s)) "
            "GROUP BY d.kind, d.source, d.status",
            (is_admin, dept_ids),
        ).fetchall()
    ready: dict[str, int] = {}
    importing = 0
    for r in rows:
        if r["status"] == "ready":
            key = {"file": "documents"}.get(r["kind"], r["kind"])
            ready[key] = ready.get(key, 0) + (r["events"] if r["kind"] in ("calendar", "slack") else r["n"])
        elif r["status"] in ("pending", "processing"):
            importing += r["n"]
    left = worker.minutes_left(importing)
    return {"ready": ready, "total": sum(ready.values()), "importing": importing,
            "eta": _duration(left) if left else None}


# ---------------------------------------------------------------- departments

@app.get("/api/departments")
def list_departments(user: dict = Depends(auth.current_user)):
    if user["role"] != "admin":
        return user["departments"]
    with conn() as c:
        return c.execute(
            "SELECT d.id, d.name, "
            " (SELECT count(*) FROM user_departments ud WHERE ud.department_id=d.id) AS users, "
            " (SELECT count(*) FROM document_departments dd WHERE dd.department_id=d.id) AS documents "
            "FROM departments d ORDER BY d.name"
        ).fetchall()


class DepartmentIn(BaseModel):
    name: str


@app.post("/api/departments")
def create_department(body: DepartmentIn, _: dict = Depends(auth.admin_user)):
    name = body.name.strip()
    if not name or "+" in name or "/" in name:
        raise HTTPException(400, "Department name cannot be empty or contain + or /")
    with conn() as c:
        return c.execute(
            "INSERT INTO departments(name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name "
            "RETURNING id, name", (name,)
        ).fetchone()


@app.delete("/api/departments/{dept_id}")
def delete_department(dept_id: int, _: dict = Depends(auth.admin_user)):
    with conn() as c:
        c.execute("DELETE FROM departments WHERE id=%s", (dept_id,))
    return {"ok": True}


# ---------------------------------------------------------------- users

@app.get("/api/users")
def list_users(_: dict = Depends(auth.admin_user)):
    with conn() as c:
        return c.execute(
            "SELECT u.id, u.email, u.name, u.role, u.active, "
            " coalesce(json_agg(json_build_object('id', d.id, 'name', d.name)) "
            "   FILTER (WHERE d.id IS NOT NULL), '[]') AS departments "
            "FROM users u LEFT JOIN user_departments ud ON ud.user_id=u.id "
            "LEFT JOIN departments d ON d.id=ud.department_id GROUP BY u.id ORDER BY u.name, u.email"
        ).fetchall()


class UserIn(BaseModel):
    email: str
    name: str = ""
    password: str | None = None
    role: str = "member"
    active: bool = True
    department_ids: list[int] = []


def _set_user_departments(c, user_id: int, dept_ids: list[int]) -> None:
    c.execute("DELETE FROM user_departments WHERE user_id=%s", (user_id,))
    for d in set(dept_ids):
        c.execute("INSERT INTO user_departments VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, d))


@app.post("/api/users")
def create_user(body: UserIn, _: dict = Depends(auth.admin_user)):
    if not body.password or len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if body.role not in ("admin", "member"):
        raise HTTPException(400, "Role must be admin or member")
    lic = license.status()
    with conn() as c:
        if lic.max_users:
            count = c.execute("SELECT count(*) AS n FROM users WHERE active").fetchone()["n"]
            if count >= lic.max_users:
                raise HTTPException(402, f"Your plan allows {lic.max_users} active users. Contact your provider to add more.")
        if c.execute("SELECT 1 FROM users WHERE email=%s", (body.email.strip().lower(),)).fetchone():
            raise HTTPException(400, "A user with this email already exists")
        uid = c.execute(
            "INSERT INTO users(email, name, password_hash, role, active) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (body.email.strip().lower(), body.name.strip(), auth.hash_password(body.password), body.role, body.active),
        ).fetchone()["id"]
        _set_user_departments(c, uid, body.department_ids)
    return {"id": uid}


@app.put("/api/users/{uid}")
def update_user(uid: int, body: UserIn, admin: dict = Depends(auth.admin_user)):
    if body.role not in ("admin", "member"):
        raise HTTPException(400, "Role must be admin or member")
    if uid == admin["id"] and (body.role != "admin" or not body.active):
        raise HTTPException(400, "You cannot remove your own admin access")
    with conn() as c:
        c.execute("UPDATE users SET email=%s, name=%s, role=%s, active=%s WHERE id=%s",
                  (body.email.strip().lower(), body.name.strip(), body.role, body.active, uid))
        if body.password:
            if len(body.password) < 8:
                raise HTTPException(400, "Password must be at least 8 characters")
            c.execute("UPDATE users SET password_hash=%s WHERE id=%s", (auth.hash_password(body.password), uid))
        _set_user_departments(c, uid, body.department_ids)
    return {"ok": True}


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


class NameIn(BaseModel):
    name: str


@app.put("/api/me")
def update_me(body: NameIn, user: dict = Depends(auth.current_user)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Type your name.")
    with conn() as c:
        c.execute("UPDATE users SET name=%s WHERE id=%s", (name[:100], user["id"]))
    return {"ok": True}


@app.post("/api/me/password")
def change_password(body: PasswordIn, user: dict = Depends(auth.current_user)):
    with conn() as c:
        row = c.execute("SELECT password_hash FROM users WHERE id=%s", (user["id"],)).fetchone()
        if not auth.verify_password(body.current_password, row["password_hash"]):
            raise HTTPException(400, "Current password is wrong")
        if len(body.new_password) < 8:
            raise HTTPException(400, "Password must be at least 8 characters")
        c.execute("UPDATE users SET password_hash=%s WHERE id=%s", (auth.hash_password(body.new_password), user["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- documents

def _can_see(c, user: dict, doc_id: int) -> bool:
    if user["role"] == "admin":
        return True
    row = c.execute(
        "SELECT 1 FROM documents d WHERE d.id=%s AND (d.company_wide OR EXISTS ("
        " SELECT 1 FROM document_departments dd WHERE dd.document_id=d.id AND dd.department_id = ANY(%s)))",
        (doc_id, [d["id"] for d in user["departments"]]),
    ).fetchone()
    return row is not None


@app.get("/api/documents")
def list_documents(response: Response, user: dict = Depends(auth.current_user), q: str = "",
                   source: str = "", connector_id: int | None = None, status: str = "", kind: str = "",
                   limit: int = 1000, offset: int = 0):
    """source: "uploads" = files added by hand or from synced folders. Total count is in X-Total-Count."""
    is_admin = user["role"] == "admin"
    where = ["(%(admin)s OR d.company_wide OR EXISTS (SELECT 1 FROM document_departments dd "
             "WHERE dd.document_id=d.id AND dd.department_id = ANY(%(depts)s)))"]
    params = {"admin": is_admin, "depts": [d["id"] for d in user["departments"]],
              "limit": max(1, min(limit, 1000)), "offset": max(0, offset)}
    if q.strip():
        where.append("(d.title ILIKE %(q)s OR d.filename ILIKE %(q)s OR EXISTS (SELECT 1 FROM chunks ch "
                     "WHERE ch.document_id = d.id AND ch.tsv @@ plainto_tsquery('simple', %(qt)s)))")
        params["q"] = f"%{q.strip()}%"
        params["qt"] = q.strip()
    if source == "uploads":
        where.append("d.source IN ('upload', 'folder')")
    if connector_id is not None:
        where.append("d.connector_id = %(cid)s")
        params["cid"] = connector_id
    if kind:
        where.append("d.kind = %(kind)s")
        params["kind"] = kind
    if status == "ready":
        where.append("d.status = 'ready'")
    elif status == "problem":
        where.append("d.status = 'error'")
    sql_where = " AND ".join(where)
    with conn() as c:
        total = c.execute(f"SELECT count(*) AS n FROM documents d WHERE {sql_where}", params).fetchone()["n"]
        rows = c.execute(
            "SELECT d.id, d.title, d.filename, d.kind, d.source, d.company_wide, d.status, d.error, "
            " d.metadata, d.created_at, d.updated_at, d.connector_id, "
            " coalesce((SELECT json_agg(json_build_object('id', dp.id, 'name', dp.name)) "
            "   FROM document_departments dd JOIN departments dp ON dp.id=dd.department_id "
            "   WHERE dd.document_id=d.id), '[]') AS departments "
            f"FROM documents d WHERE {sql_where} ORDER BY d.updated_at DESC LIMIT %(limit)s OFFSET %(offset)s",
            params,
        ).fetchall()
    response.headers["X-Total-Count"] = str(total)
    return rows


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\- ]+", "_", name, flags=re.UNICODE)[:200] or "file"


@app.post("/api/documents")
def upload_documents(
    files: list[UploadFile] = File(...),
    department_ids: str = Form(""),
    company_wide: bool = Form(False),
    kind: str = Form("file"),
    interviewee: str = Form(""),
    role: str = Form(""),
    interview_date: str = Form(""),
    admin: dict = Depends(auth.admin_user),
):
    depts = [int(x) for x in department_ids.split(",") if x.strip().isdigit()]
    if not depts and not company_wide:
        raise HTTPException(400, "Choose at least one department, or mark the document as company-wide")
    if kind not in ("file", "interview", "whatsapp", "linkedin"):
        raise HTTPException(400, "Unknown document kind")
    meta = {k: v for k, v in {"interviewee": interviewee, "role": role, "interview_date": interview_date}.items() if v}
    created = []
    with conn() as c:
        for f in files:
            name = _safe_name(f.filename or "file")
            if Path(name).suffix.lower() not in SUPPORTED_EXT:
                raise HTTPException(400, f"{name}: this file type is not supported yet")
            dest = settings.uploads_dir / f"{uuid.uuid4().hex}_{name}"
            with dest.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            doc_kind = "interview" if kind == "interview" or is_audio_video(dest) else "file"
            title = Path(name).stem
            if kind == "whatsapp" or (doc_kind == "file" and (name.lower().startswith("whatsapp")
                                                           or Path(name).suffix.lower() == ".zip")):
                doc_kind = "whatsapp"
                title = "WhatsApp: " + re.sub(r"(?i)^whatsapp\s*(chat)?\s*(with|-)?\s*", "", title).strip()
            if kind == "linkedin":
                doc_kind = "social"
                title = "LinkedIn: " + title
                meta["platform"] = "LinkedIn"
            if doc_kind == "interview" and interviewee:
                title = f"Interview - {interviewee}" + (f" ({role})" if role else "")
            doc_id = c.execute(
                "INSERT INTO documents(title, filename, path, kind, source, company_wide, metadata, uploaded_by) "
                "VALUES (%s, %s, %s, %s, 'upload', %s, %s, %s) RETURNING id",
                (title, name, str(dest), doc_kind, company_wide, json.dumps(meta), admin["id"]),
            ).fetchone()["id"]
            for d in depts:
                c.execute("INSERT INTO document_departments VALUES (%s, %s) ON CONFLICT DO NOTHING", (doc_id, d))
            created.append(doc_id)
    worker.wake()
    return {"created": created}


class DocumentUpdate(BaseModel):
    title: str
    department_ids: list[int] = []
    company_wide: bool = False


@app.put("/api/documents/{doc_id}")
def update_document(doc_id: int, body: DocumentUpdate, _: dict = Depends(auth.admin_user)):
    with conn() as c:
        c.execute("UPDATE documents SET title=%s, company_wide=%s, updated_at=now() WHERE id=%s",
                  (body.title.strip() or "Untitled", body.company_wide, doc_id))
        c.execute("DELETE FROM document_departments WHERE document_id=%s", (doc_id,))
        for d in set(body.department_ids):
            c.execute("INSERT INTO document_departments VALUES (%s, %s)", (doc_id, d))
    return {"ok": True}


@app.post("/api/documents/{doc_id}/reindex")
def reindex_document(doc_id: int, _: dict = Depends(auth.admin_user)):
    with conn() as c:
        c.execute("UPDATE documents SET status='pending' WHERE id=%s", (doc_id,))
    worker.wake()
    return {"ok": True}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, _: dict = Depends(auth.admin_user)):
    with conn() as c:
        row = c.execute("DELETE FROM documents WHERE id=%s RETURNING path, source", (doc_id,)).fetchone()
    if row and row["source"] == "upload":
        Path(row["path"]).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/documents/{doc_id}/file")
def download_document(doc_id: int, user: dict = Depends(auth.current_user)):
    with conn() as c:
        if not _can_see(c, user, doc_id):
            raise HTTPException(404, "Not found")
        row = c.execute("SELECT path, filename FROM documents WHERE id=%s", (doc_id,)).fetchone()
    if not row or not Path(row["path"]).exists():
        raise HTTPException(404, "File no longer exists")
    return FileResponse(row["path"], filename=row["filename"])


# ---------------------------------------------------------------- connections (email, calendar, website)

class ConnectorIn(BaseModel):
    type: str
    name: str
    config: dict = {}
    department_ids: list[int] = []
    company_wide: bool = False
    enabled: bool = True


def _check_connector(body: ConnectorIn, old_config: dict | None = None) -> dict:
    """Validate, merge with saved secrets, test the connection, and return the config to store."""
    if body.type not in connector_runner.TYPES:
        raise HTTPException(400, "Unknown connection type")
    if not body.name.strip():
        raise HTTPException(400, "Please give the connection a name")
    if not body.department_ids and not body.company_wide:
        raise HTTPException(400, "Choose at least one department, or make it visible to everyone")
    if body.type in connector_runner.OAUTH_TYPES:
        if old_config is None:
            raise HTTPException(400, "Use the Connect Google / Microsoft buttons for this type")
        return {**old_config, **{k: v for k, v in body.config.items() if k in ("days", "max_messages", "max_files")}}
    secrets_ = connector_runner.SECRETS.get(body.type, set())
    cfg = {k: (str(v).strip() if isinstance(v, str) else v) for k, v in body.config.items()}
    for key in secrets_:
        if not cfg.get(key) and old_config and old_config.get(key):
            cfg[key] = old_config[key]  # left empty while editing = keep the saved one
        elif cfg.get(key):
            cfg[key] = connector_base.encrypt(cfg[key])
    try:
        if body.type == "imap":
            for field in ("host", "username", "password"):
                if not cfg.get(field):
                    raise HTTPException(400, f"Missing {field}")
            email_imap.test_connection(cfg)
        elif body.type == "ics":
            if not cfg.get("url"):
                raise HTTPException(400, "Missing calendar link")
            calendar_ics.events_by_month(calendar_ics.fetch(connector_base.decrypt(cfg["url"])))
        elif body.type == "whatsapp_business":
            from .connectors import whatsapp_business

            if not cfg.get("phone_number_id") or not cfg.get("access_token"):
                raise HTTPException(400, "Fill in the Phone number ID and the access token from Meta.")
            cfg.setdefault("verify_token", (old_config or {}).get("verify_token") or secrets.token_urlsafe(18))
            cfg["number"] = whatsapp_business.check(cfg)
        elif body.type == "website":
            from .connectors import website as website_connector

            cfg["url"] = website_connector.start_url(cfg.get("url", ""))
            website_connector.check(cfg["url"])
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - show the admin why the connection failed
        raise HTTPException(400, str(e) or "Could not connect")
    return cfg


def _public_config(type_: str, cfg: dict) -> dict:
    return {k: ("" if k in connector_runner.SECRETS.get(type_, set()) else v) for k, v in cfg.items()}


def _with_account_emails(rows: list[dict]) -> list[dict]:
    ids = [r["config"].get("account_id") for r in rows if r["config"].get("account_id")]
    if ids:
        with conn() as c:
            emails = {a["id"]: a for a in c.execute(
                "SELECT id, provider, email FROM oauth_accounts WHERE id = ANY(%s)", (ids,)).fetchall()}
        for r in rows:
            a = emails.get(r["config"].get("account_id"))
            if a:
                r["config"]["username"] = a["email"]
                r["provider"] = a["provider"]
    return rows


@app.get("/api/connectors")
def list_connectors(_: dict = Depends(auth.admin_user)):
    with conn() as c:
        rows = c.execute(
            "SELECT k.id, k.type, k.name, k.config, k.state, k.company_wide, k.enabled, k.last_sync, k.last_error, "
            " (SELECT count(*) FROM documents d WHERE d.connector_id=k.id) AS documents, "
            " (SELECT count(*) FROM documents d WHERE d.connector_id=k.id AND d.status IN ('pending','processing')) AS indexing, "
            " (SELECT count(*) FROM documents d WHERE d.connector_id=k.id AND d.status = 'ready') AS ready, "
            " (SELECT coalesce(sum(coalesce((d.metadata->>'events')::int, (d.metadata->>'messages')::int, 1)), 0) FROM documents d "
            "   WHERE d.connector_id=k.id AND d.status = 'ready') AS events, "
            " (SELECT count(*) FROM documents d WHERE d.connector_id=k.id AND d.status = 'error') AS failed, k.last_note, "
            " coalesce((SELECT json_agg(json_build_object('id', dp.id, 'name', dp.name)) FROM connector_departments cd "
            "   JOIN departments dp ON dp.id=cd.department_id WHERE cd.connector_id=k.id), '[]') AS departments "
            "FROM connectors k ORDER BY k.name"
        ).fetchall()
    for r in rows:
        r["config"] = _public_config(r["type"], r["config"])
        r["syncing"] = connector_runner.lock_for(r["id"]).locked()
        r["progress"] = (r.get("state") or {}).get("progress")
        r["more"] = bool((r.get("state") or {}).get("more"))
        r["channels"] = (r.get("state") or {}).get("channel_count")
        r["private_not_added"] = (r.get("state") or {}).get("private_not_added")
        r["social_accounts"] = (r.get("state") or {}).get("accounts")
        r.pop("state", None)
    return _with_account_emails(rows)


@app.post("/api/connectors/test")
def test_connector(body: ConnectorIn, _: dict = Depends(auth.admin_user)):
    body.company_wide = True  # visibility isn't needed to test
    cfg = _check_connector(body)
    folders = email_imap.test_connection(cfg) if body.type == "imap" else []
    return {"ok": True, "folders": folders}


def _save_connector_departments(c, cid: int, dept_ids: list[int]) -> None:
    c.execute("DELETE FROM connector_departments WHERE connector_id=%s", (cid,))
    for d in set(dept_ids):
        c.execute("INSERT INTO connector_departments VALUES (%s, %s)", (cid, d))


def _sync_in_background(cid: int) -> None:
    def run():
        connector_runner.sync_connector(cid)
        worker.wake()
    threading.Thread(target=run, daemon=True).start()


@app.post("/api/connectors")
def create_connector(body: ConnectorIn, _: dict = Depends(auth.admin_user)):
    cfg = _check_connector(body)
    with conn() as c:
        cid = c.execute(
            "INSERT INTO connectors(type, name, config, company_wide, enabled) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (body.type, body.name.strip(), json.dumps(cfg), body.company_wide, body.enabled),
        ).fetchone()["id"]
        _save_connector_departments(c, cid, body.department_ids)
    _sync_in_background(cid)
    return {"id": cid}


@app.put("/api/connectors/{cid}")
def update_connector(cid: int, body: ConnectorIn, _: dict = Depends(auth.admin_user)):
    with conn() as c:
        old = c.execute("SELECT type, config FROM connectors WHERE id=%s", (cid,)).fetchone()
    if not old:
        raise HTTPException(404, "Not found")
    body.type = old["type"]
    cfg = _check_connector(body, old["config"])
    with conn() as c:
        c.execute("UPDATE connectors SET name=%s, config=%s, company_wide=%s, enabled=%s WHERE id=%s",
                  (body.name.strip(), json.dumps(cfg), body.company_wide, body.enabled, cid))
        _save_connector_departments(c, cid, body.department_ids)
        connector_runner.apply_visibility(c, cid, body.company_wide)  # takes effect right away, no re-import
    if body.type not in connector_runner.OAUTH_TYPES:
        _sync_in_background(cid)  # address/password may have changed: check and import again
    return {"ok": True}


@app.post("/api/connectors/{cid}/sync")
def sync_connector_now(cid: int, _: dict = Depends(auth.admin_user)):
    _sync_in_background(cid)
    return {"ok": True}


@app.delete("/api/connectors/{cid}")
def delete_connector(cid: int, _: dict = Depends(auth.admin_user)):
    lock = connector_runner.lock_for(cid)
    if not lock.acquire(timeout=300):
        raise HTTPException(409, "This source is still importing. Try again in a few minutes.")
    try:
        with conn() as c:
            row = c.execute("DELETE FROM connectors WHERE id=%s RETURNING config", (cid,)).fetchone()
            account = (row or {}).get("config", {}).get("account_id")
            if account and not c.execute("SELECT 1 FROM connectors WHERE config->>'account_id' = %s",
                                         (str(account),)).fetchone():
                c.execute("DELETE FROM oauth_accounts WHERE id=%s", (account,))  # forget the tokens
    finally:
        lock.release()
    shutil.rmtree(settings.data_dir / "connectors" / str(cid), ignore_errors=True)
    return {"ok": True}


# ---------------------------------------------------------------- WhatsApp Business webhook (Meta calls these)

@app.get("/webhooks/whatsapp/{cid}")
def whatsapp_verify(cid: int, request: Request):
    """Meta checks the address once when you save it in the Meta app settings."""
    q = request.query_params
    with conn() as c:
        row = c.execute("SELECT config FROM connectors WHERE id=%s AND type='whatsapp_business'", (cid,)).fetchone()
    if not row or q.get("hub.mode") != "subscribe" or q.get("hub.verify_token") != row["config"].get("verify_token"):
        raise HTTPException(403, "Verification failed")
    return Response(q.get("hub.challenge", ""), media_type="text/plain")


@app.post("/webhooks/whatsapp/{cid}")
async def whatsapp_receive(cid: int, request: Request):
    from .connectors import whatsapp_business

    body = await request.body()
    with conn() as c:
        row = c.execute("SELECT config FROM connectors WHERE id=%s AND type='whatsapp_business'", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Unknown connection")
    if not whatsapp_business.verify_signature(row["config"], body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(403, "Bad signature")
    stored = whatsapp_business.receive(cid, whatsapp_business.as_json(body))
    if stored:
        worker.wake()
    return {"ok": True, "stored": stored}


# ---------------------------------------------------------------- one-click sign-in (Google / Microsoft)

class OAuthStartIn(BaseModel):
    provider: str
    services: list[str]
    department_ids: list[int] = []
    company_wide: bool = False


def _origin(request: Request) -> str:
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return origin.rstrip("/")
    return str(request.base_url).rstrip("/")


@app.get("/api/oauth/status")
def oauth_status(request: Request, _: dict = Depends(auth.admin_user)):
    return {"configured": oauth.configured(), "redirect_url": oauth.redirect_url(_origin(request))}


@app.post("/api/oauth/start")
def oauth_start(body: OAuthStartIn, request: Request, _: dict = Depends(auth.admin_user)):
    if not body.department_ids and not body.company_wide:
        raise HTTPException(400, "Choose at least one department, or make it visible to everyone")
    try:
        return {"url": oauth.start(body.provider, body.services, body.department_ids, body.company_wide,
                                   _origin(request))}
    except ValueError as e:
        raise HTTPException(400, str(e))


def _message_page(title: str, text: str) -> HTMLResponse:
    from html import escape

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>KnowledgeDesk</title><body style="font-family:system-ui;max-width:520px;margin:15vh auto;padding:16px">
<h2>{escape(title)}</h2><p>{escape(text)}</p><p><a href="/">Back to KnowledgeDesk</a></p></body>""")


@app.get("/oauth/callback")
def oauth_callback(state: str = "", code: str = "", error: str = "", error_description: str = ""):
    if error:
        reason = "You cancelled the sign-in." if error == "access_denied" else (error_description or error)
        return _message_page("Not connected", reason)
    try:
        created = oauth.finish(state, code)
    except (ValueError, RuntimeError) as e:
        return _message_page("Not connected", str(e))
    for cid in created:
        _sync_in_background(cid)
    return RedirectResponse(f"/?connected={len(created)}#sources", status_code=303)


# ---------------------------------------------------------------- company branding

@app.get("/api/brand/logo")
def brand_logo():
    path = branding.logo_path()
    if not path:
        raise HTTPException(404, "No logo")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


class CompanyIn(BaseModel):
    name: str
    website: str = ""


@app.put("/api/admin/company")
def save_company(body: CompanyIn, _: dict = Depends(auth.admin_user)):
    from .connectors.website import WebsiteError, start_url

    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Type the company name.")
    website = ""
    if body.website.strip():
        try:
            website = start_url(body.website)
        except WebsiteError as e:
            raise HTTPException(400, str(e))
    old = branding.get()
    branding.save(name=name, website=website)
    found = None
    if website and (website != old["website"] or not old["logo"]):
        try:
            found = branding.find_logo(website)
        except Exception:  # noqa: BLE001
            found = False
    return {"ok": True, "logo_found": found}


@app.post("/api/admin/company/logo")
def upload_logo(file: UploadFile = File(...), _: dict = Depends(auth.admin_user)):
    data = file.file.read(branding.MAX_LOGO_BYTES + 1)
    if len(data) > branding.MAX_LOGO_BYTES:
        raise HTTPException(400, "The logo must be smaller than 2 MB.")
    ext = Path(file.filename or "logo.png").suffix
    if ext.lower() not in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"):
        raise HTTPException(400, "Use a PNG, JPG, SVG or WEBP image.")
    branding.store_logo(data, ext)
    return {"ok": True}


@app.post("/api/admin/company/logo/find")
def refind_logo(_: dict = Depends(auth.admin_user)):
    website = branding.get()["website"]
    if not website:
        raise HTTPException(400, "Add the company website first.")
    try:
        found = branding.find_logo(website)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't read the website: {e}")
    if not found:
        raise HTTPException(400, "No logo found on the website. Upload it instead.")
    return {"ok": True}


# ---------------------------------------------------------------- dashboard numbers

@app.get("/api/admin/analytics")
def analytics(days: int = 30, _: dict = Depends(auth.admin_user)):
    days = max(7, min(days, 180))
    with conn() as c:
        per_day = c.execute(
            "SELECT to_char(g.day, 'YYYY-MM-DD') AS day, count(q.id) AS asked, "
            " count(q.id) FILTER (WHERE NOT q.found_sources OR q.feedback = -1) AS unanswered "
            "FROM generate_series(current_date - (%(d)s - 1), current_date, interval '1 day') AS g(day) "
            "LEFT JOIN questions q ON q.created_at::date = g.day::date GROUP BY g.day ORDER BY g.day", {"d": days}).fetchall()
        by_department = c.execute(
            "SELECT coalesce(d.name, 'No department') AS name, count(DISTINCT q.id) AS n FROM questions q "
            "LEFT JOIN user_departments ud ON ud.user_id = q.user_id LEFT JOIN departments d ON d.id = ud.department_id "
            "WHERE q.created_at > now() - make_interval(days => %(d)s) GROUP BY 1 ORDER BY 2 DESC LIMIT 8", {"d": days}).fetchall()
        by_person = c.execute(
            "SELECT coalesce(nullif(u.name, ''), u.email, 'Removed user') AS name, count(*) AS n FROM questions q "
            "LEFT JOIN users u ON u.id = q.user_id WHERE q.created_at > now() - make_interval(days => %(d)s) "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 8", {"d": days}).fetchall()
        growth = c.execute(
            "SELECT to_char(g.day, 'YYYY-MM-DD') AS day, count(d.id) AS added "
            "FROM generate_series(current_date - (%(d)s - 1), current_date, interval '1 day') AS g(day) "
            "LEFT JOIN documents d ON d.created_at::date = g.day::date GROUP BY g.day ORDER BY g.day", {"d": days}).fetchall()
        totals = c.execute(
            "SELECT (SELECT count(*) FROM documents WHERE status='ready') AS searchable, "
            " (SELECT count(*) FROM documents WHERE created_at > now() - interval '24 hours') AS added_today, "
            " (SELECT count(*) FROM documents WHERE created_at > now() - interval '7 days') AS added_week, "
            " (SELECT count(*) FROM questions WHERE created_at > now() - interval '7 days') AS asked_week, "
            " (SELECT count(*) FROM questions WHERE created_at > now() - make_interval(days => %(d)s)) AS asked_period, "
            " (SELECT count(*) FROM questions WHERE created_at > now() - make_interval(days => %(d)s) "
            "    AND found_sources AND coalesce(feedback, 0) >= 0) AS answered_period", {"d": days}).fetchone()
        freshness = c.execute(
            "SELECT k.id, k.type, k.name, k.enabled, k.last_sync, k.last_error, "
            " (SELECT max(d.created_at) FROM documents d WHERE d.connector_id = k.id) AS last_new, "
            " (SELECT count(*) FROM documents d WHERE d.connector_id = k.id AND d.created_at > now() - interval '24 hours') AS added_today, "
            " (SELECT a.email FROM oauth_accounts a WHERE a.id::text = k.config->>'account_id') AS account "
            "FROM connectors k ORDER BY k.type, k.id").fetchall()
        uploads = c.execute("SELECT max(created_at) AS last_new, count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS added_today "
                            "FROM documents WHERE source IN ('upload', 'folder')").fetchone()
    return {"days": days, "per_day": per_day, "by_department": by_department, "by_person": by_person, "growth": growth,
            "totals": totals, "freshness": freshness, "uploads": uploads,
            "check_every_minutes": settings.connector_sync_minutes}


# ---------------------------------------------------------------- admin home: setup progress and health

@app.get("/api/admin/setup")
def setup_progress(_: dict = Depends(auth.admin_user)):
    with conn() as c:
        row = c.execute(
            "SELECT (SELECT count(*) FROM connectors) AS connectors, "
            " (SELECT count(*) FROM documents WHERE source IN ('upload','folder')) AS uploads, "
            " (SELECT count(*) FROM users WHERE active) AS users, "
            " (SELECT count(*) FROM departments) AS departments, "
            " (SELECT count(*) FROM questions) AS questions, "
            " (SELECT count(*) FROM questions WHERE created_at > now() - interval '7 days') AS questions_week, "
            " (SELECT count(*) FROM questions WHERE (feedback = -1 OR NOT found_sources) "
            "    AND created_at > now() - interval '30 days') AS gaps"
        ).fetchone()
    return {**row, "has_sources": bool(row["connectors"] or row["uploads"]), "has_team": row["users"] > 1,
            "has_asked": row["questions"] > 0}


def _memory_gb() -> float | None:
    """Memory this installation may use (the Docker limit if there is one)."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            value = Path(path).read_text().strip()
            if value.isdigit() and int(value) < 1 << 50:
                return int(value) / 1024 ** 3
        except OSError:
            continue
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 ** 2
    except OSError:
        pass
    return None


def _cpus() -> float:
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            return round(int(quota) / int(period), 1)
    except (OSError, ValueError):
        pass
    return float(os.cpu_count() or 1)


def _resources_check(waiting: int) -> dict:
    import time

    mem, cpus = _memory_gb(), _cpus()
    durations = worker.recent_durations()
    avg = sum(d for d, _, _ in durations) / len(durations) if durations else None
    slowest = max(durations, default=None)
    parts = [f"{cpus:g} CPU cores", f"{mem:.1f} GB memory" if mem else "memory unknown"]
    if avg is not None:
        parts.append(f"{avg:.1f} s per item on average")
    if worker.current:
        parts.append(f"now preparing \"{worker.current.get('title', '')[:60]}\" "
                     f"({int(time.time() - worker.current.get('started', time.time()))} s so far)")
    detail = "; ".join(parts) + "."
    state = "ok"
    if (mem and mem < 3.5) or cpus < 2:
        state = "warn"
        detail += (" That's too little: give Docker more (Docker Desktop → Settings → Resources: "
                   "at least 4 CPUs and 8 GB memory), then restart KnowledgeDesk.")
    elif avg is not None and avg > 15 and waiting:
        state = "warn"
        detail += (f" Items are slow to prepare (slowest: {slowest[1]} \"{slowest[2][:50]}\", {slowest[0]:.0f} s). "
                   "Giving Docker more CPU and memory usually fixes this.")
    return {"id": "resources", "label": "Computer resources", "state": state, "detail": detail,
            "cpus": cpus, "memory_gb": round(mem, 1) if mem else None, "avg_seconds": round(avg, 1) if avg else None,
            "recent": [{"seconds": round(d, 1), "kind": k, "title": t[:80]} for d, k, t in durations[-15:]]}


_ai_check: dict = {"at": 0.0, "result": None}


def _duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    return f"{hours} h {mins} min" if hours < 10 else f"{hours} h"


class KeyIn(BaseModel):
    key: str


@app.get("/api/admin/ai-key")
def ai_key_status(_: dict = Depends(auth.admin_user)):
    key = llm.api_key()
    return {"set": bool(key), "masked": f"{key[:10]}…{key[-4:]}" if key else "", "check": _check_ai()}


@app.put("/api/admin/ai-key")
def ai_key_save(body: KeyIn, _: dict = Depends(auth.admin_user)):
    import anthropic

    key = "".join(body.key.split())  # drop spaces and line breaks picked up while copying
    seen = f"{key[:14]}…{key[-4:]}, {len(key)} characters" if len(key) > 20 else f"{len(key)} characters"
    if not key.startswith("sk-ant-"):
        raise HTTPException(400, f"That doesn't look like a Claude API key (received: {seen}). It starts with sk-ant-.")
    if any(ch in key for ch in "•*…●") or not key.isascii():
        raise HTTPException(400, "This key contains hidden dots (•) instead of letters – it was copied from a place that "
                                 "hides keys, such as a chat. Copy it again directly from console.anthropic.com "
                                 "(create a new key if needed).")
    try:
        anthropic.Anthropic(api_key=key).messages.count_tokens(
            model=settings.llm_model, messages=[{"role": "user", "content": "ok"}])
    except anthropic.AuthenticationError:
        raise HTTPException(400, f"Claude didn't accept this key (received: {seen}). Check that it's the whole key, "
                                 "or create a new one.")
    except anthropic.APIStatusError as e:
        raise HTTPException(400, f"Claude answered with an error: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(400, "Couldn't reach Claude. Check the internet connection and try again.")
    llm.save_api_key(key)
    _ai_check.update(at=0.0, result=None)
    return {"ok": True}


def _check_ai() -> dict:
    import time

    if settings.fake_llm:
        return {"state": "ok", "detail": "Test mode (answers are simulated)."}
    if not llm.api_key():
        return {"state": "bad", "detail": "No Claude API key yet. Paste one below."}
    if time.time() - _ai_check["at"] < 300 and _ai_check["result"]:
        return _ai_check["result"]
    try:
        llm.client().messages.count_tokens(model=settings.llm_model, messages=[{"role": "user", "content": "ok"}])
        result = {"state": "ok", "detail": f"Connected ({settings.llm_model})."}
    except Exception as e:  # noqa: BLE001
        text = str(e)
        if "401" in text or "authentication" in text.lower():
            result = {"state": "bad", "detail": "The Claude API key is wrong or was revoked. Paste a working key "
                                                "below (create one at console.anthropic.com)."}
        elif "credit" in text.lower() or "billing" in text.lower():
            result = {"state": "bad", "detail": "The Claude account has no credit left. Add credit at console.anthropic.com."}
        else:
            result = {"state": "warn", "detail": f"Couldn't reach Claude right now ({text[:150]})."}
    _ai_check.update(at=time.time(), result=result)
    return result


def _queue() -> dict:
    with conn() as c:
        return c.execute("SELECT count(*) FILTER (WHERE status IN ('pending','processing')) AS waiting FROM documents").fetchone()


@app.get("/api/admin/health")
def health(_: dict = Depends(auth.admin_user)):
    checks = []
    checks.append({"id": "ai", "label": "AI answers (Claude)", **_check_ai()})
    checks.append({"id": "search", "label": "Search engine", "state": "ok", "detail": "Ready."})
    with conn() as c:
        docs = c.execute(
            "SELECT count(*) FILTER (WHERE status='ready') AS ready, "
            " count(*) FILTER (WHERE status IN ('pending','processing')) AS waiting, "
            " count(*) FILTER (WHERE status='error') AS failed, "
            " count(*) FILTER (WHERE status='ready' AND updated_at > now() - interval '10 minutes') AS recent "
            "FROM documents").fetchone()
        failures = c.execute(
            "SELECT left(error, 160) AS reason, count(*) AS n FROM documents WHERE status='error' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 5").fetchall()
        sources = c.execute("SELECT type, name, last_error, last_sync FROM connectors ORDER BY name").fetchall()
    if docs["waiting"]:
        left = worker.minutes_left(docs["waiting"])
        eta = f" – about {_duration(left)} left" if left else " – measuring speed…"
        checks.append({"id": "indexing", "label": "Preparing content for search", "state": "busy",
                       "detail": f"{docs['ready']} items ready, {docs['waiting']} waiting{eta}."})
    else:
        checks.append({"id": "indexing", "label": "Preparing content for search", "state": "ok",
                       "detail": f"All {docs['ready']} items are searchable."})
    checks.append(_resources_check(docs["waiting"]))
    if docs["failed"]:
        checks.append({"id": "failed", "label": "Items that couldn't be read", "state": "warn",
                       "detail": f"{docs['failed']} item(s): " + "; ".join(f"{f['reason']} ({f['n']})" for f in failures)})
    broken = [s for s in sources if s["last_error"]]
    if not sources:
        checks.append({"id": "sources", "label": "Connected sources", "state": "warn", "detail": "Nothing connected yet."})
    elif broken:
        checks.append({"id": "sources", "label": "Connected sources", "state": "bad",
                       "detail": "; ".join(f"{s['name']}: {s['last_error']}" for s in broken[:3])})
    else:
        checks.append({"id": "sources", "label": "Connected sources", "state": "ok",
                       "detail": f"{len(sources)} connected, all working."})
    lic = license.status()
    checks.append({"id": "license", "label": "Subscription", "state": "ok" if lic.state in ("valid", "unchecked") else
                   ("warn" if lic.state == "grace" else "bad"),
                   "detail": lic.message or (f"Valid until {lic.expires}." if lic.expires else "Active.")})
    configured = oauth.configured()
    missing = [p.title() for p, ok in configured.items() if not ok]
    checks.append({"id": "signin", "label": "Google / Microsoft / Slack sign-in",
                   "state": "ok" if not missing else "info",
                   "detail": "Both ready." if not missing else
                   f"{', '.join(missing)} sign-in not set up – needed only to connect {' or '.join(missing)} accounts."})
    overall = "bad" if any(c["state"] == "bad" for c in checks) else \
        "warn" if any(c["state"] in ("warn", "busy") for c in checks) else "ok"
    return {"overall": overall, "checks": checks, "problems": list(reversed(recent_problems.items[-30:])),
            "version": APP_VERSION}


# ---------------------------------------------------------------- admin

@app.get("/api/admin/questions")
def recent_questions(limit: int = 200, _: dict = Depends(auth.admin_user)):
    with conn() as c:
        rows = c.execute(
            "SELECT q.id, q.question, q.answer, q.feedback, q.found_sources, q.created_at, "
            " u.name AS user_name, u.email AS user_email, d.name AS department "
            "FROM questions q LEFT JOIN users u ON u.id=q.user_id LEFT JOIN departments d ON d.id=q.department_id "
            "ORDER BY q.created_at DESC LIMIT %s", (min(limit, 1000),)
        ).fetchall()
        stats = c.execute(
            "SELECT count(*) AS total, "
            " count(*) FILTER (WHERE created_at > now() - interval '7 days') AS last_7_days, "
            " count(*) FILTER (WHERE feedback = 1) AS helpful, "
            " count(*) FILTER (WHERE feedback = -1) AS not_helpful, "
            " count(*) FILTER (WHERE NOT found_sources) AS no_sources "
            "FROM questions"
        ).fetchone()
    return {"stats": stats, "questions": rows}


@app.post("/api/admin/rescan")
def rescan(_: dict = Depends(auth.admin_user)):
    return {"queued": worker.rescan_now()}


@app.post("/api/admin/license")
def install_license(file: UploadFile = File(...), _: dict = Depends(auth.admin_user)):
    text = file.file.read().decode(errors="replace").strip()
    if settings.license_public_key:
        try:
            license.verify(text, settings.license_public_key)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "This license file is not valid")
    settings.license_file.parent.mkdir(parents=True, exist_ok=True)
    settings.license_file.write_text(text)
    return license.status().__dict__


# ---------------------------------------------------------------- web interface

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
