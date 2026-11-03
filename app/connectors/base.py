"""Shared helpers for connectors: secret encryption, file storage and document bookkeeping."""
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

SECRET_FIELDS = {"password"}


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(("connectors:" + settings.secret_key).encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return "enc:" + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value.startswith("enc:"):
        return value
    try:
        return _fernet().decrypt(value[4:].encode()).decode()
    except InvalidToken:
        raise RuntimeError("Saved password can't be read (the SECRET_KEY changed). Re-enter it in Connections.")


def storage_dir(connector_id: int) -> Path:
    d = settings.data_dir / "connectors" / str(connector_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc_date(meta: dict):
    """The moment an item belongs to: an email's Date header, a post's publish time, a chat day..."""
    from email.utils import parsedate_to_datetime

    for key in ("date", "posted", "day", "interview_date"):
        value = (meta or {}).get(key)
        if not value:
            continue
        try:
            if key == "date" and not str(value)[:4].isdigit():
                d = parsedate_to_datetime(str(value))
            else:
                d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError):
            continue
    return None


class Sync:
    """Tracks the documents a connector produces during one sync run."""

    def __init__(self, c, connector: dict):
        self.c = c
        self.connector = connector
        self.seen: set[str] = set()
        self.queued = 0
        self.skipped: list[str] = []
        self.scheduled = False  # True when the background schedule started this run (not "Check now")

    def skip(self, name: str, reason: str) -> None:
        """Remember an item that could not be imported, and carry on with the rest."""
        self.skipped.append(f"{name}: {reason}")

    def note(self) -> str | None:
        if not self.skipped:
            return None
        n = len(self.skipped)
        head = "Skipped 1 item" if n == 1 else f"Skipped {n} items"
        return head + ": " + "; ".join(self.skipped[:3]) + (" …" if n > 3 else "")

    def imported_ids(self) -> set[str]:
        """Everything this source already imported (one query instead of one per item)."""
        rows = self.c.execute("SELECT external_id FROM documents WHERE connector_id=%s", (self.connector["id"],)).fetchall()
        return {r["external_id"] for r in rows}

    def progress(self, done: int, total: int, commit: bool = False) -> None:
        """Shown on the Sources page as "Importing 120 of 2,340"."""
        self.connector["state"]["progress"] = {"done": done, "total": total}
        self.c.execute("UPDATE connectors SET state = jsonb_set(state, '{progress}', %s::jsonb) WHERE id=%s",
                       (json.dumps({"done": done, "total": total}), self.connector["id"]))
        if commit:
            self.c.commit()

    def exists(self, external_id: str) -> bool:
        """True if this item was already imported (marks it as still present)."""
        row = self.c.execute("SELECT 1 FROM documents WHERE connector_id=%s AND external_id=%s",
                             (self.connector["id"], external_id)).fetchone()
        if row:
            self.seen.add(external_id)
        return row is not None

    def is_current(self, external_id: str, version: str) -> bool:
        """True if this item was imported with the same version (e.g. modified time); marks it as present."""
        row = self.c.execute(
            "SELECT metadata->>'version' AS v FROM documents WHERE connector_id=%s AND external_id=%s",
            (self.connector["id"], external_id)).fetchone()
        if row and row["v"] == version:
            self.seen.add(external_id)
            return True
        return False

    def upsert(self, external_id: str, title: str, filename: str, data: bytes, kind: str,
               metadata: dict | None = None, allowed_emails: list[str] | None = None) -> None:
        """allowed_emails: only these people may find the item (e.g. a private Slack channel's members)."""
        """Save the content as a file and create/refresh its document if it is new or changed."""
        self.seen.add(external_id)
        digest = sha(data)
        row = self.c.execute(
            "SELECT id, sha256 FROM documents WHERE connector_id=%s AND external_id=%s",
            (self.connector["id"], external_id),
        ).fetchone()
        if row and row["sha256"] == digest:
            if allowed_emails is not None:  # channel members can change without the content changing
                self.c.execute("UPDATE documents SET allowed_emails=%s WHERE id=%s", (allowed_emails, row["id"]))
            return
        safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in filename)[:150] or "item"
        path = storage_dir(self.connector["id"]) / f"{sha(external_id.encode())[:16]}_{safe}"
        path.write_bytes(data)
        meta = json.dumps(metadata or {})
        when = doc_date(metadata or {})
        if row:
            self.c.execute(
                "UPDATE documents SET title=%s, filename=%s, path=%s, sha256=%s, metadata=%s, status='pending', "
                "updated_at=now(), doc_date=%s, allowed_emails=%s WHERE id=%s",
                (title[:300], filename, str(path), digest, meta, when, allowed_emails, row["id"]),
            )
        else:
            self.c.execute(
                "INSERT INTO documents(title, filename, path, kind, source, sha256, company_wide, metadata, "
                "connector_id, external_id, doc_date, private, owner_id, allowed_emails) "
                "VALUES (%s, %s, %s, %s, 'connector', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (title[:300], filename, str(path), kind, digest, self.connector["company_wide"], meta,
                 self.connector["id"], external_id, when, bool(self.connector.get("private")),
                 self.connector.get("owner_id"), allowed_emails),
            )
            # Visible to the connector's departments right away
            self.c.execute(
                "INSERT INTO document_departments(document_id, department_id) "
                "SELECT d.id, cd.department_id FROM documents d JOIN connector_departments cd "
                "ON cd.connector_id = d.connector_id WHERE d.connector_id=%s AND d.external_id=%s "
                "ON CONFLICT DO NOTHING", (self.connector["id"], external_id))
        self.queued += 1
        if self.queued % 20 == 0:
            self.c.commit()  # save progress so far: indexing starts and nothing is lost if the sync stops

    def remove_unseen(self) -> None:
        """Delete documents the source no longer has (used by calendar and website)."""
        rows = self.c.execute(
            "SELECT id, path, external_id FROM documents WHERE connector_id=%s", (self.connector["id"],)
        ).fetchall()
        for r in rows:
            if r["external_id"] not in self.seen:
                self.c.execute("DELETE FROM documents WHERE id=%s", (r["id"],))
                Path(r["path"]).unlink(missing_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
