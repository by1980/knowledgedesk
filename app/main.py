"""KnowledgeDesk web server: API + web interface."""
import json
import logging
import secrets
import threading
from contextlib import asynccontextmanager
import re
import shutil
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, embeddings, license, llm, oauth, retrieval, worker
from .config import settings
from .connectors import base as connector_base
from .connectors import calendar_ics, email_imap
from .connectors import runner as connector_runner
from .db import conn, init_schema
from .ingest.extract import SUPPORTED_EXT, is_audio_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC = Path(__file__).parent / "static"


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
    if settings.run_worker:
        worker.start()
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
    return {"company": settings.company_name, "license": lic.__dict__ | {"allows_use": lic.allows_use},
            "llm_configured": bool(settings.anthropic_api_key)}


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
    # Follow-up questions ("and for VIP customers?") need the previous question to search well.
    last_user_q = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    search_text = f"{last_user_q}\n{question}" if last_user_q and len(question) < 80 else question

    hits = retrieval.search(search_text, is_admin=is_admin, user_depts=dept_ids, only_dept=body.department_id)
    sources = [
        {"n": i, "document_id": h["document_id"], "title": h["title"], "location": h["location"] or "",
         "kind": h["kind"], "snippet": h["content"][:300]}
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


class FeedbackIn(BaseModel):
    value: int  # 1 or -1


@app.post("/api/questions/{qid}/feedback")
def feedback(qid: int, body: FeedbackIn, user: dict = Depends(auth.current_user)):
    with conn() as c:
        c.execute("UPDATE questions SET feedback=%s WHERE id=%s AND user_id=%s",
                  (1 if body.value > 0 else -1, qid, user["id"]))
    return {"ok": True}


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
def list_documents(user: dict = Depends(auth.current_user)):
    is_admin = user["role"] == "admin"
    with conn() as c:
        return c.execute(
            "SELECT d.id, d.title, d.filename, d.kind, d.source, d.company_wide, d.status, d.error, "
            " d.metadata, d.created_at, d.updated_at, "
            " (SELECT count(*) FROM chunks ch WHERE ch.document_id=d.id) AS chunks, "
            " coalesce((SELECT json_agg(json_build_object('id', dp.id, 'name', dp.name)) "
            "   FROM document_departments dd JOIN departments dp ON dp.id=dd.department_id "
            "   WHERE dd.document_id=d.id), '[]') AS departments "
            "FROM documents d WHERE %s OR d.company_wide OR EXISTS ("
            " SELECT 1 FROM document_departments dd WHERE dd.document_id=d.id AND dd.department_id = ANY(%s)) "
            "ORDER BY d.updated_at DESC",
            (is_admin, [d["id"] for d in user["departments"]]),
        ).fetchall()


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
    if kind not in ("file", "interview"):
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
            if doc_kind == "file" and (name.lower().startswith("whatsapp") or Path(name).suffix.lower() == ".zip"):
                doc_kind = "whatsapp"
                title = "WhatsApp: " + re.sub(r"(?i)^whatsapp\s*(chat)?\s*(with|-)?\s*", "", title).strip()
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
        elif body.type == "website":
            if not cfg.get("url"):
                raise HTTPException(400, "Missing website address")
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
            "SELECT k.id, k.type, k.name, k.config, k.company_wide, k.enabled, k.last_sync, k.last_error, "
            " (SELECT count(*) FROM documents d WHERE d.connector_id=k.id) AS documents, "
            " (SELECT count(*) FROM documents d WHERE d.connector_id=k.id AND d.status IN ('pending','processing')) AS indexing, "
            " coalesce((SELECT json_agg(json_build_object('id', dp.id, 'name', dp.name)) FROM connector_departments cd "
            "   JOIN departments dp ON dp.id=cd.department_id WHERE cd.connector_id=k.id), '[]') AS departments "
            "FROM connectors k ORDER BY k.name"
        ).fetchall()
    for r in rows:
        r["config"] = _public_config(r["type"], r["config"])
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
    _sync_in_background(cid)
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
    return RedirectResponse(f"/?connected={len(created)}#connections", status_code=303)


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
