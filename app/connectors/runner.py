"""Runs connector syncs (on a schedule from the worker, or on demand from the admin screen)."""
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import psycopg

from ..config import settings
from ..db import conn
from . import calendar_ics, email_imap, google, microsoft, website
from .base import Sync

log = logging.getLogger("connectors")

# Sources the admin sets up by typing details in.
MANUAL_TYPES = {"imap": email_imap.sync, "ics": calendar_ics.sync, "website": website.sync}
# Sources connected with "Sign in with Google / Microsoft".
OAUTH_TYPES = {"gmail": google.sync_gmail, "gcal": google.sync_gcal, "gdrive": google.sync_gdrive,
               "ms_mail": microsoft.sync_mail, "ms_cal": microsoft.sync_calendar, "ms_drive": microsoft.sync_drive}
TYPES = {**MANUAL_TYPES, **OAUTH_TYPES}
# Config fields stored encrypted, per connector type.
SECRETS = {"imap": {"password"}, "ics": {"url"}}

# One lock per connector: a connector never syncs twice at once, and removing it waits for a running sync.
_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def lock_for(connector_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks[connector_id]


def sync_connector(connector_id: int) -> dict:
    lock = lock_for(connector_id)
    if not lock.acquire(blocking=False):
        return {"queued": 0, "error": None, "skipped": "already syncing"}
    try:
        return _sync(connector_id)
    finally:
        lock.release()


def _sync(connector_id: int) -> dict:
    with conn() as c:
        connector = c.execute("SELECT * FROM connectors WHERE id=%s", (connector_id,)).fetchone()
    if not connector:
        return {"error": "not found"}
    run = TYPES[connector["type"]]
    error = None
    queued = 0
    with conn() as c:
        s = Sync(c, connector)
        try:
            run(s)
        except Exception as e:  # noqa: BLE001 - keep everything imported so far and show the problem
            log.exception("Sync stopped for connector %s", connector_id)
            error = friendly_error(e)
            if isinstance(e, psycopg.Error):
                c.rollback()  # only the last few unsaved items are lost; earlier batches were committed
        queued = s.queued
        # Keep every document's visibility in line with the connector's departments.
        c.execute("UPDATE documents SET company_wide=%s WHERE connector_id=%s",
                  (connector["company_wide"], connector_id))
        c.execute("DELETE FROM document_departments WHERE document_id IN "
                  "(SELECT id FROM documents WHERE connector_id=%s)", (connector_id,))
        c.execute("INSERT INTO document_departments(document_id, department_id) "
                  "SELECT d.id, cd.department_id FROM documents d JOIN connector_departments cd "
                  "ON cd.connector_id=d.connector_id WHERE d.connector_id=%s", (connector_id,))
        c.execute("UPDATE connectors SET state=%s, last_sync=now(), last_error=%s, last_note=%s WHERE id=%s",
                  (json.dumps(connector["state"]), error, s.note(), connector_id))
    log.info("Synced %s: %d new or changed items%s", connector["name"], queued, f" (stopped: {error})" if error else "")
    return {"queued": queued, "error": error}


def friendly_error(e: Exception) -> str:
    text = str(e) or e.__class__.__name__
    low = text.lower()
    if "quota" in low or "rate limit" in low or "ratelimit" in low or "too many requests" in low or text.startswith("429"):
        return ("The provider is limiting how fast we can read. Everything imported so far is kept, "
                "and the rest continues automatically at the next sync.")
    if "revoked" in low or "invalid_grant" in low or text.startswith("401"):
        return "Access was removed or expired. Click Remove, then connect the account again."
    return text[:500]


def sync_due() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.connector_sync_minutes)
    with conn() as c:
        rows = c.execute(
            "SELECT id FROM connectors WHERE enabled AND (last_sync IS NULL OR last_sync < %s)", (cutoff,)
        ).fetchall()
    for r in rows:
        sync_connector(r["id"])
