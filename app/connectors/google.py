"""Google connectors (connected with "Sign in with Google"): Gmail, Google Calendar, Google Drive."""
import base64
from datetime import date, datetime, time, timezone

from ..ingest.extract import is_audio_video, is_supported
from ..oauth import Api, ApiError
from . import calendar_ics
from .base import Sync
from .email_imap import store_message

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"
MAX_FILE_BYTES = 50 * 1024 * 1024

# Google Docs/Sheets/Slides are exported to formats the indexer understands.
GOOGLE_EXPORT = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}


def _account(sync: Sync) -> int:
    return int(sync.connector["config"]["account_id"])


# ------------------------------------------------------------------ Gmail

def sync_gmail(sync: Sync) -> None:
    cfg, state = sync.connector["config"], sync.connector["state"]
    days = int(cfg.get("days") or 365)
    max_per_run = int(cfg.get("max_messages") or 500)
    # Skip spam, trash and newsletters; only mail newer than the last sync after the first run.
    query = "-in:spam -in:trash -category:promotions -category:social"
    query += f" after:{state['after']}" if state.get("after") else f" newer_than:{days}d"
    started = int(datetime.now(timezone.utc).timestamp()) - 3600  # overlap; already-imported mail is skipped

    with Api(_account(sync)) as api:
        ids, page = [], None
        while len(ids) < max_per_run:
            params = {"q": query, "maxResults": 100}
            if page:
                params["pageToken"] = page
            data = api.json(f"{GMAIL}/messages", params)
            ids += [m["id"] for m in data.get("messages", [])]
            page = data.get("nextPageToken")
            if not page:
                break
        for mid in ids[:max_per_run]:
            if sync.exists(f"gmail:{mid}"):
                continue
            try:
                raw = api.json(f"{GMAIL}/messages/{mid}", {"format": "raw"})["raw"]
            except ApiError as e:
                if e.status in (400, 403, 404):
                    sync.skip(f"email {mid}", str(e))
                    continue
                raise
            store_message(sync, f"gmail:{mid}", base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    if len(ids) < max_per_run:  # everything fetched; next time only newer mail
        state["after"] = started


# ------------------------------------------------------------------ Google Calendar

def _gtime(t: dict):
    if "dateTime" in t:
        return datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00"))
    return date.fromisoformat(t["date"])


def sync_gcal(sync: Sync) -> None:
    start, end = calendar_ics.window()
    events = []
    with Api(_account(sync)) as api:
        calendars = api.json(f"{CAL}/users/me/calendarList", {"minAccessRole": "reader"}).get("items", [])
        for cal in calendars:
            if cal.get("id", "").endswith("#holiday@group.v.calendar.google.com"):
                continue
            page = None
            while True:
                params = {"timeMin": datetime.combine(start, time.min, timezone.utc).isoformat(),
                          "timeMax": datetime.combine(end, time.min, timezone.utc).isoformat(),
                          "singleEvents": "true", "orderBy": "startTime", "maxResults": 2500}
                if page:
                    params["pageToken"] = page
                data = api.json(f"{CAL}/calendars/{cal['id']}/events", params)
                for ev in data.get("items", []):
                    if ev.get("status") == "cancelled" or "start" not in ev:
                        continue
                    events.append({
                        "start": _gtime(ev["start"]), "end": _gtime(ev["end"]) if "end" in ev else None,
                        "title": ev.get("summary"), "location": ev.get("location"),
                        "organizer": (ev.get("organizer") or {}).get("email", ""),
                        "attendees": [a.get("displayName") or a.get("email", "") for a in ev.get("attendees", [])],
                        "description": ev.get("description"),
                    })
                page = data.get("nextPageToken")
                if not page:
                    break
    calendar_ics.save_months(sync, calendar_ics.group_by_month(events))


# ------------------------------------------------------------------ Google Drive

def sync_gdrive(sync: Sync) -> None:
    cfg = sync.connector["config"]
    max_files = int(cfg.get("max_files") or 3000)
    complete = True
    with Api(_account(sync)) as api:
        page, count = None, 0
        while True:
            params = {"q": "trashed = false and mimeType != 'application/vnd.google-apps.folder'",
                      "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, size, webViewLink)",
                      "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            data = api.json(f"{DRIVE}/files", params)
            for f in data.get("files", []):
                if count >= max_files:
                    complete = False
                    break
                try:
                    if _drive_file(sync, api, f):
                        count += 1
                except ApiError as e:
                    if e.status in (400, 403, 404):  # e.g. owner blocked downloading, or too big to export
                        sync.seen.add(f"gdrive:{f['id']}")
                        sync.skip(f["name"], "can't be downloaded" if e.status == 403 else str(e))
                        continue
                    raise
            page = data.get("nextPageToken")
            if not page or not complete:
                break
    if complete:
        sync.remove_unseen()  # files deleted in Drive are removed here too


def _drive_file(sync: Sync, api: Api, f: dict) -> bool:
    name, mime = f["name"], f["mimeType"]
    if mime in GOOGLE_EXPORT:
        export_mime, ext = GOOGLE_EXPORT[mime]
        filename = name + ext
    else:
        filename = name
        from pathlib import Path

        p = Path(name)
        if not is_supported(p) or is_audio_video(p) or int(f.get("size") or 0) > MAX_FILE_BYTES:
            return False
    ext_id = f"gdrive:{f['id']}"
    version = f.get("modifiedTime", "")
    if sync.is_current(ext_id, version):
        return True
    if mime in GOOGLE_EXPORT:
        data = api.bytes(f"{DRIVE}/files/{f['id']}/export", {"mimeType": export_mime})
    else:
        data = api.bytes(f"{DRIVE}/files/{f['id']}", {"alt": "media", "supportsAllDrives": "true"})
    sync.upsert(ext_id, name, filename, data, "file",
                {"version": version, "link": f.get("webViewLink", ""), "drive": "Google Drive"})
    return True
