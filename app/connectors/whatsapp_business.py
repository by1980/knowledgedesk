"""WhatsApp Business connection (official WhatsApp Business Platform / Cloud API from Meta).

Meta sends every message to and from the business number to this installation's webhook
(/webhooks/whatsapp/<connector id>), so it needs a public https address. Messages are kept as one
document per contact per day ("WhatsApp: Dana Cohen – 2026-11-03"). Voice notes are downloaded
and turned into text in the background.

Personal and group chats can't be connected this way – WhatsApp doesn't allow it. Those are
added by exporting the chat on the phone and importing the file.
"""
import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..db import conn
from ..oauth import http
from .base import Sync, decrypt, storage_dir

log = logging.getLogger("connectors")
GRAPH = "https://graph.facebook.com/v23.0"
_lock = threading.Lock()  # webhook deliveries can arrive in parallel


def check(cfg: dict) -> str:
    """Confirm the phone number id and token work; returns the number's display name."""
    with http() as h:
        r = h.get(f"{GRAPH}/{cfg['phone_number_id']}", params={"fields": "display_phone_number,verified_name"},
                  headers={"Authorization": f"Bearer {decrypt(cfg['access_token'])}"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        msg = (body.get("error") or {}).get("message") or f"error {r.status_code}"
        raise RuntimeError(f"Meta didn't accept these details: {msg}")
    return f"{body.get('verified_name', '')} {body.get('display_phone_number', '')}".strip()


def sync(sync: Sync) -> None:
    """Nothing to fetch (Meta pushes messages); just confirm the connection still works."""
    sync.connector["state"]["number"] = check(sync.connector["config"])


def verify_signature(cfg: dict, body: bytes, header: str) -> bool:
    secret = decrypt(cfg.get("app_secret") or "")
    if not secret:
        return True  # optional: without the app secret, deliveries aren't signature-checked
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def _line(m: dict, who: str) -> tuple[str, str | None]:
    """Text for one message, plus a media id when it's a voice note to transcribe."""
    t = m.get("type")
    when = datetime.fromtimestamp(int(m.get("timestamp", 0)), timezone.utc)
    if t == "text":
        body = (m.get("text") or {}).get("body", "")
    elif t in ("image", "video", "document"):
        media = m.get(t) or {}
        body = f"[{t}{': ' + media['filename'] if media.get('filename') else ''}] {media.get('caption', '')}".strip()
    elif t == "audio":
        return f"{when:%H:%M} {who}: [voice note]", (m.get("audio") or {}).get("id")
    elif t == "location":
        loc = m.get("location") or {}
        body = f"[location] {loc.get('name', '')} {loc.get('address', '')}".strip()
    else:
        body = f"[{t}]"
    return f"{when:%H:%M} {who}: {body}", None


def receive(connector_id: int, payload: dict) -> int:
    """Store the messages in one webhook delivery. Returns how many were stored."""
    stored = 0
    voice: list[tuple[str, str, str]] = []
    with _lock, conn() as c:
        connector = c.execute("SELECT * FROM connectors WHERE id=%s", (connector_id,)).fetchone()
        if not connector:
            return 0
        s = Sync(c, connector)
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                names = {ct.get("wa_id"): (ct.get("profile") or {}).get("name") or ct.get("wa_id") for ct in value.get("contacts", [])}
                incoming = [(m, m.get("from"), names.get(m.get("from")) or m.get("from")) for m in value.get("messages", [])]
                echoes = [(m, m.get("to"), "Us") for m in value.get("message_echoes", [])]  # messages the business sent
                for m, contact, who in incoming + echoes:
                    if not contact:
                        continue
                    line, media_id = _line(m, who)
                    day = datetime.fromtimestamp(int(m.get("timestamp", 0)), timezone.utc).strftime("%Y-%m-%d")
                    contact_name = names.get(contact) or contact
                    ext = f"wa:{contact}:{day}"
                    row = c.execute("SELECT path, metadata FROM documents WHERE connector_id=%s AND external_id=%s",
                                    (connector_id, ext)).fetchone()
                    old = Path(row["path"]).read_text() if row and Path(row["path"]).exists() else \
                        f"WhatsApp conversation with {contact_name} (+{contact}) – {day}\n\n"
                    if m.get("id") and m["id"] in old:
                        continue  # Meta may deliver the same message twice
                    text = old + line + (f"  <!--{m['id']}-->" if m.get("id") else "") + "\n"
                    count = (row["metadata"] or {}).get("messages", 0) + 1 if row else 1
                    s.upsert(ext, f"WhatsApp: {contact_name} – {day}", f"whatsapp-{day}.txt", text.encode(), "whatsapp",
                             {"contact": contact_name, "phone": contact, "day": day, "messages": count})
                    stored += 1
                    if media_id:
                        voice.append((ext, media_id, m.get("id", "")))
        c.execute("UPDATE connectors SET last_sync=now(), last_error=NULL WHERE id=%s", (connector_id,))
        cfg = connector["config"]
    for ext, media_id, mid in voice:
        threading.Thread(target=_transcribe_voice_note, args=(connector_id, cfg, ext, media_id, mid), daemon=True).start()
    return stored


def _transcribe_voice_note(connector_id: int, cfg: dict, ext: str, media_id: str, message_id: str) -> None:
    try:
        token = decrypt(cfg["access_token"])
        with http() as h:
            info = h.get(f"{GRAPH}/{media_id}", headers={"Authorization": f"Bearer {token}"}).json()
            audio = h.get(info["url"], headers={"Authorization": f"Bearer {token}"}).content
        path = storage_dir(connector_id) / f"voice-{media_id}.ogg"
        path.write_bytes(audio)
        from ..ingest.transcribe import transcribe

        words = " ".join(t for t, _ in transcribe(path))
        path.unlink(missing_ok=True)
        with _lock, conn() as c:
            connector = c.execute("SELECT * FROM connectors WHERE id=%s", (connector_id,)).fetchone()
            row = c.execute("SELECT title, path, metadata FROM documents WHERE connector_id=%s AND external_id=%s",
                            (connector_id, ext)).fetchone()
            if not connector or not row:
                return
            text = Path(row["path"]).read_text()
            marker = f"[voice note]  <!--{message_id}-->"
            if marker in text:
                text = text.replace(marker, f"[voice note: {words}]  <!--{message_id}-->")
                Sync(c, connector).upsert(ext, row["title"], Path(row["path"]).name.split("_", 1)[-1], text.encode(),
                                          "whatsapp", row["metadata"] or {})
    except Exception:  # noqa: BLE001 - the text of the conversation is already saved
        log.exception("Couldn't transcribe a WhatsApp voice note")


def as_json(body: bytes) -> dict:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}
