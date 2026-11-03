"""Microsoft 365 connectors (connected with "Sign in with Microsoft"): Outlook mail, Outlook calendar,
OneDrive (including files shared with the user)."""
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from ..ingest.extract import is_audio_video, is_supported
from ..oauth import Api
from . import calendar_ics
from .base import Sync
from .email_imap import store_message

GRAPH = "https://graph.microsoft.com/v1.0"
MAX_FILE_BYTES = 50 * 1024 * 1024


def _account(sync: Sync) -> int:
    return int(sync.connector["config"]["account_id"])


def _pages(api: Api, url: str, params: dict | None = None, headers: dict | None = None, limit: int = 100000):
    """Follow Microsoft Graph @odata.nextLink pages."""
    got = 0
    while url and got < limit:
        data = api.json(url, params, headers)
        for item in data.get("value", []):
            yield item
            got += 1
            if got >= limit:
                return
        url, params = data.get("@odata.nextLink"), None


# ------------------------------------------------------------------ Outlook mail

def sync_mail(sync: Sync) -> None:
    cfg, state = sync.connector["config"], sync.connector["state"]
    days = int(cfg.get("days") or 365)
    max_per_run = int(cfg.get("max_messages") or 500)
    started = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since = state.get("since") or (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetched_all = True
    with Api(_account(sync)) as api:
        for folder in ("inbox", "sentitems"):
            params = {"$filter": f"receivedDateTime ge {since}", "$select": "id",
                      "$orderby": "receivedDateTime desc", "$top": 100}
            ids = [m["id"] for m in _pages(api, f"{GRAPH}/me/mailFolders/{folder}/messages", params,
                                           limit=max_per_run + 1)]
            if len(ids) > max_per_run:
                fetched_all = False
            for mid in ids[:max_per_run]:
                if sync.exists(f"outlook:{mid}"):
                    continue
                raw = api.bytes(f"{GRAPH}/me/messages/{mid}/$value")
                store_message(sync, f"outlook:{mid}", raw, folder)
    if fetched_all:
        state["since"] = started


# ------------------------------------------------------------------ Outlook calendar

def _mtime(t: dict, all_day: bool):
    value = datetime.fromisoformat(t["dateTime"][:19]).replace(tzinfo=timezone.utc)
    return value.date() if all_day else value


def sync_calendar(sync: Sync) -> None:
    start, end = calendar_ics.window()
    params = {"startDateTime": datetime.combine(start, time.min).isoformat() + "Z",
              "endDateTime": datetime.combine(end, time.min).isoformat() + "Z", "$top": 500,
              "$select": "subject,start,end,location,organizer,attendees,bodyPreview,isAllDay,isCancelled"}
    events = []
    with Api(_account(sync)) as api:
        for ev in _pages(api, f"{GRAPH}/me/calendarView", params, {"Prefer": 'outlook.timezone="UTC"'}):
            if ev.get("isCancelled"):
                continue
            all_day = bool(ev.get("isAllDay"))
            events.append({
                "start": _mtime(ev["start"], all_day), "end": _mtime(ev["end"], all_day),
                "title": ev.get("subject"), "location": (ev.get("location") or {}).get("displayName"),
                "organizer": ((ev.get("organizer") or {}).get("emailAddress") or {}).get("name", ""),
                "attendees": [((a.get("emailAddress") or {}).get("name") or (a.get("emailAddress") or {}).get("address", ""))
                              for a in ev.get("attendees", [])],
                "description": ev.get("bodyPreview"),
            })
    calendar_ics.save_months(sync, calendar_ics.group_by_month(events))


# ------------------------------------------------------------------ OneDrive

def sync_drive(sync: Sync) -> None:
    cfg = sync.connector["config"]
    max_files = int(cfg.get("max_files") or 3000)
    count, complete = 0, True
    with Api(_account(sync)) as api:
        items = list(_pages(api, f"{GRAPH}/me/drive/root/delta",
                            {"$select": "id,name,file,size,lastModifiedDateTime,deleted,webUrl,parentReference"}))
        shared = list(_pages(api, f"{GRAPH}/me/drive/sharedWithMe"))
        for item in shared:
            remote = item.get("remoteItem") or {}
            if "file" in remote:
                items.append({**remote, "name": remote.get("name") or item.get("name"),
                              "_drive": (remote.get("parentReference") or {}).get("driveId")})
        for item in items:
            if count >= max_files:
                complete = False
                break
            if "deleted" in item or "file" not in item:
                continue
            p = Path(item.get("name", ""))
            if not is_supported(p) or is_audio_video(p) or int(item.get("size") or 0) > MAX_FILE_BYTES:
                continue
            count += 1
            ext_id = f"onedrive:{item['id']}"
            version = item.get("lastModifiedDateTime", "")
            if sync.is_current(ext_id, version):
                continue
            drive = item.get("_drive")
            url = (f"{GRAPH}/drives/{drive}/items/{item['id']}/content" if drive
                   else f"{GRAPH}/me/drive/items/{item['id']}/content")
            data = api.bytes(url)
            sync.upsert(ext_id, p.stem, p.name, data, "file",
                        {"version": version, "link": item.get("webUrl", ""), "drive": "OneDrive"})
    if complete:
        sync.remove_unseen()
