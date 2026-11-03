"""Runs connector syncs (on a schedule from the worker, or on demand from the admin screen)."""
import json
import logging
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import psycopg

from ..config import settings
from ..db import conn
from . import calendar_ics, email_imap, google, microsoft, slack, social, website, whatsapp_business
from .base import Sync

log = logging.getLogger("connectors")

# Sources the admin sets up by typing details in.
MANUAL_TYPES = {"imap": email_imap.sync, "ics": calendar_ics.sync, "website": website.sync,
                "whatsapp_business": whatsapp_business.sync}
# Sources connected with "Sign in with Google / Microsoft".
OAUTH_TYPES = {"gmail": google.sync_gmail, "gcal": google.sync_gcal, "gdrive": google.sync_gdrive,
               "ms_mail": microsoft.sync_mail, "ms_cal": microsoft.sync_calendar, "ms_drive": microsoft.sync_drive,
               "slack": slack.sync_slack, "meta_social": social.sync_meta_social, "youtube": social.sync_youtube}
TYPES = {**MANUAL_TYPES, **OAUTH_TYPES}
# Config fields stored encrypted, per connector type.
SECRETS = {"imap": {"password"}, "ics": {"url"}, "whatsapp_business": {"access_token", "app_secret"}}

# One lock per connector: a connector never syncs twice at once, and removing it waits for a running sync.
_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def lock_for(connector_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks[connector_id]


def sync_connector(connector_id: int, scheduled: bool = False) -> dict:
    lock = lock_for(connector_id)
    if not lock.acquire(blocking=False):
        return {"queued": 0, "error": None, "skipped": "already syncing"}
    try:
        return _sync(connector_id, scheduled)
    finally:
        lock.release()


def _sync(connector_id: int, scheduled: bool = False) -> dict:
    with conn() as c:
        connector = c.execute("SELECT * FROM connectors WHERE id=%s", (connector_id,)).fetchone()
    if not connector:
        return {"error": "not found"}
    run = TYPES[connector["type"]]
    error = None
    queued = 0
    with conn() as c:
        s = Sync(c, connector)
        s.scheduled = scheduled
        try:
            run(s)
        except Exception as e:  # noqa: BLE001 - keep everything imported so far and show the problem
            log.exception("Sync stopped for connector %s", connector_id)
            error = friendly_error(e)
            if isinstance(e, psycopg.Error):
                c.rollback()  # only the last few unsaved items are lost; earlier batches were committed
        queued = s.queued
        apply_visibility(c, connector_id, connector["company_wide"])
        c.execute("UPDATE connectors SET state=%s, last_sync=now(), last_error=%s, last_note=%s WHERE id=%s",
                  (json.dumps(connector["state"]), error, s.note(), connector_id))
    log.info("Synced %s: %d new or changed items%s", connector["name"], queued, f" (stopped: {error})" if error else "")
    return {"queued": queued, "error": error}


def apply_visibility(c, connector_id: int, company_wide: bool) -> None:
    """Make every item from this source visible to exactly the source's departments."""
    c.execute("UPDATE documents SET company_wide=%s WHERE connector_id=%s", (company_wide, connector_id))
    c.execute("DELETE FROM document_departments WHERE document_id IN "
              "(SELECT id FROM documents WHERE connector_id=%s)", (connector_id,))
    c.execute("INSERT INTO document_departments(document_id, department_id) "
              "SELECT d.id, cd.department_id FROM documents d JOIN connector_departments cd "
              "ON cd.connector_id=d.connector_id WHERE d.connector_id=%s", (connector_id,))


def friendly_error(e: Exception) -> str:
    text = str(e) or e.__class__.__name__
    low = text.lower()
    if "has not been used in project" in low or ("api" in low and "is disabled" in low):
        # Google: the API isn't switched on in the Google Cloud project. Its message has the exact link.
        m = re.search(r"https://console\.(?:developers|cloud)\.google\.com/\S+?(?=[\s\"']|\.?$|\. )", text)
        api = re.search(r"(Gmail|Google Calendar|Google Drive|YouTube Data|Drive|Calendar)[^.]*? API( v\d)?", text)
        what = api.group(0) if api else "This Google service"
        return (f"{what} isn't switched on in your Google Cloud project yet. Open "
                f"{m.group(0) if m else 'console.cloud.google.com → APIs & Services → Library'}, click Enable, wait a "
                "minute, then click Check for new items.")
    if "quota" in low or "rate limit" in low or "ratelimit" in low or "too many requests" in low or text.startswith("429"):
        return ("The provider is limiting how fast we can read. Everything imported so far is kept, "
                "and the rest continues automatically at the next sync.")
    if "facebook" in low or "youtube" in low:
        return text[:500]
    if "revoked" in low or "invalid_grant" in low or text.startswith("401") or "invalid_auth" in low:
        return "Access to this account was lost or expired. Click Disconnect, then connect the account again."
    return text[:500]


def sync_due() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.connector_sync_minutes)
    with conn() as c:
        rows = c.execute(
            "SELECT id FROM connectors WHERE enabled AND (last_sync IS NULL OR last_sync < %s "
            " OR (state->>'more')::boolean IS TRUE) ORDER BY last_sync NULLS FIRST", (cutoff,)
        ).fetchall()
    for r in rows:
        sync_connector(r["id"], scheduled=True)
