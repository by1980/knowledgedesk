"""Email connector (IMAP). Works with Gmail / Google Workspace (using an app password) and most
mail providers. Each email becomes a document; supported attachments become documents too.
"""
import email
import imaplib
import re
from datetime import date, timedelta
from email import policy
from email.header import decode_header, make_header
from pathlib import Path

from ..ingest.extract import is_audio_video, is_supported
from .base import Sync, decrypt

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:  # noqa: BLE001
        return str(value)


def _connect(cfg: dict) -> imaplib.IMAP4:
    host = cfg["host"].strip()
    port = int(cfg.get("port") or 993)
    conn = imaplib.IMAP4_SSL(host, port, timeout=20) if port == 993 else imaplib.IMAP4(host, port, timeout=20)
    if port != 993:
        conn.starttls()
    conn.login(cfg["username"].strip(), decrypt(cfg["password"]))
    return conn


def _friendly(e: Exception) -> str:
    text = str(e)
    if isinstance(e, (TimeoutError, OSError)) and not isinstance(e, imaplib.IMAP4.error):
        return "Could not reach the mail server. Check the server name and port (Gmail: imap.gmail.com, 993)."
    if "AUTHENTICATIONFAILED" in text or "Invalid credentials" in text or "LOGIN failed" in text:
        return ("Wrong email or password. For Gmail you must use an app password, not your normal password "
                "(see 'How do I connect Gmail').")
    return text


def test_connection(cfg: dict) -> list[str]:
    """Log in and return the mailbox's folder names (shown to the admin to choose from)."""
    try:
        conn = _connect(cfg)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(_friendly(e)) from e
    try:
        _, data = conn.list()
        folders = []
        for raw in data or []:
            line = raw.decode(errors="replace")
            m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if m and "\\Noselect" not in line:
                folders.append(m.group(1))
        return folders
    finally:
        conn.logout()


def _folders(cfg: dict) -> list[str]:
    return [f.strip() for f in (cfg.get("folders") or "INBOX").split(",") if f.strip()]


def sync(sync: Sync) -> None:
    cfg = sync.connector["config"]
    state = sync.connector["state"]
    days = int(cfg.get("days") or 3650)
    max_per_run = int(cfg.get("max_messages") or 500)
    try:
        conn = _connect(cfg)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(_friendly(e)) from e
    try:
        for folder in _folders(cfg):
            typ, _ = conn.select(f'"{folder}"', readonly=True)
            if typ != "OK":
                raise RuntimeError(f'Folder "{folder}" was not found in this mailbox')
            validity = (conn.response("UIDVALIDITY")[1] or [b"0"])[0]
            validity = validity.decode() if isinstance(validity, bytes) else str(validity)
            fstate = state.get(folder, {})
            if fstate.get("uidvalidity") != validity:
                fstate = {"uidvalidity": validity, "last_uid": 0}

            if fstate["last_uid"]:
                _, data = conn.uid("SEARCH", None, f"UID {fstate['last_uid'] + 1}:*")
            else:
                since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
                _, data = conn.uid("SEARCH", None, f"SINCE {since}")
            uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > fstate["last_uid"])
            uids = uids[-max_per_run:]  # newest first on a big first import

            for uid in uids:
                _, msg_data = conn.uid("FETCH", str(uid), "(RFC822)")
                raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
                if raw:
                    store_message(sync, f"{folder}:{uid}", raw, folder)
                fstate["last_uid"] = max(fstate["last_uid"], uid)
            state[folder] = fstate
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def store_message(sync: Sync, base_id: str, raw: bytes, folder: str = "") -> None:
    """Save one email (and its supported attachments) as documents."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = _decode(msg["subject"]) or "(no subject)"
    sender = _decode(msg["from"])
    meta = {"from": sender, "to": _decode(msg["to"]), "date": str(msg["date"] or ""), "folder": folder}
    sync.upsert(base_id, f"Email: {subject}", "message.eml", raw, "email", meta)

    for part in msg.iter_attachments():
        name = _decode(part.get_filename())
        if not name:
            continue
        p = Path(name)
        if not is_supported(p) or is_audio_video(p):
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload or len(payload) > MAX_ATTACHMENT_BYTES:
            continue
        sync.upsert(f"{base_id}:att:{name}", f"{p.stem} (attachment to: {subject})", name, payload, "file",
                    {"from": sender, "email_subject": subject, "date": meta["date"]})
