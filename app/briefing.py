"""Proactive: what needs each person's attention today.

Looks at what the person may see (their recent emails, documents about contracts and renewals, their upcoming
meetings) and turns it into a short list: emails waiting for a reply, things they promised to send, renewals
coming up, people to follow up with, meetings to prepare. Each item points to its sources and comes with one
action Cortex can help with (usually a draft). Refreshed every few hours, or on request.
"""
import hashlib
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from . import llm
from .config import settings
from .db import conn

log = logging.getLogger("briefing")
REFRESH_HOURS = 4
KINDS = ("reply", "send", "renew", "follow_up", "prepare", "deadline")
_running: set[int] = set()
_again: set[int] = set()            # asked to refresh while one was running: run once more right after
_failed: dict[int, datetime] = {}   # when the last try failed, so a broken AI key isn't retried on every page load
_lock = threading.Lock()


def _key(alert: dict) -> str:
    return hashlib.sha1(f"{alert.get('kind')}|{alert.get('title', '').strip().lower()}".encode()).hexdigest()[:16]


def candidates(user: dict) -> tuple[list[dict], list[dict]]:
    """Recent items this person may see that could need action, and their meetings in the next week."""
    from .main import VISIBLE, _visible_params

    p = _visible_params(user)
    first_text = "(SELECT ch.content FROM chunks ch WHERE ch.document_id = d.id ORDER BY ch.position LIMIT 1) AS text"
    with conn() as c:
        emails = c.execute(
            f"SELECT d.id, d.kind, d.title, d.metadata, coalesce(d.doc_date, d.updated_at) AS at, d.private, {first_text} "
            f"FROM documents d WHERE {VISIBLE} AND d.kind = 'email' AND d.status = 'ready' "
            "AND coalesce(d.doc_date, d.updated_at) > now() - interval '21 days' ORDER BY d.private DESC, at DESC LIMIT 45", p).fetchall()
        deals = c.execute(
            f"SELECT d.id, d.kind, d.title, d.metadata, coalesce(d.doc_date, d.updated_at) AS at, d.private, {first_text} "
            f"FROM documents d WHERE {VISIBLE} AND d.kind <> 'email' AND d.status = 'ready' AND EXISTS (SELECT 1 FROM chunks cx "
            "WHERE cx.document_id = d.id AND cx.tsv @@ to_tsquery('simple', 'renew:* | renewal:* | contract:* | expir:* | חידוש:* | חוזה:*')) "
            "ORDER BY at DESC LIMIT 10", p).fetchall()
        meetings = c.execute(
            f"SELECT e.id, e.title, e.starts_at, e.all_day, e.attendees FROM events e JOIN documents d ON d.id = e.document_id "
            f"WHERE {VISIBLE} AND e.starts_at >= now() - interval '1 hour' AND e.starts_at < now() + interval '7 days' "
            "ORDER BY e.starts_at LIMIT 20", p).fetchall()
    return emails + deals, meetings


def _heuristic(items: list[dict], mine: list[str]) -> list[dict]:
    """Test mode (no AI): a simple, predictable version of the list."""
    out = []
    sent_subjects = {re.sub(r"^(re|fwd?):\s*", "", (i["title"] or "").replace("Email: ", ""), flags=re.I).lower()
                     for i in items if any(m in (i["metadata"] or {}).get("from", "").lower() for m in mine)}
    for i in items:
        meta, text = i["metadata"] or {}, (i["text"] or "")
        subject = re.sub(r"^(re|fwd?):\s*", "", (i["title"] or "").replace("Email: ", ""), flags=re.I)
        from_me = any(m in meta.get("from", "").lower() for m in mine)
        if i["kind"] == "email" and not from_me and re.search(r"\?|please|can you|could you|let me know", text, re.I) \
                and subject.lower() not in sent_subjects:
            who = re.sub(r"\s*<[^>]*>", "", meta.get("from", "")).strip() or "them"
            out.append({"kind": "reply", "title": f"Reply to {who} about {subject}", "why": f"{who} asked you something and hasn't had an answer yet.",
                        "due": "", "person": who, "source_ids": [i["id"]], "action": f"Draft a reply to {who}'s email \"{subject}\"."})
        elif re.search(r"renew|renewal|contract|expir|חידוש|חוזה", text, re.I) and i["kind"] != "email":
            out.append({"kind": "renew", "title": f"Check the renewal: {i['title']}", "why": "This mentions a contract or renewal.",
                        "due": "", "person": "", "source_ids": [i["id"]], "action": f"Summarise the renewal terms and dates in \"{i['title']}\"."})
    return out[:8]


def compute(user: dict) -> list[dict]:
    from .main import _my_addresses

    items, meetings = candidates(user)
    if not items and not meetings:
        return []
    mine = _my_addresses(user)
    if settings.fake_llm:
        alerts = _heuristic(items, mine)
    else:
        alerts = llm.briefing(user.get("name") or user["email"], llm.company_name(), mine, items, meetings)
    known = {i["id"] for i in items}
    clean = []
    for a in alerts or []:
        if a.get("kind") not in KINDS or not a.get("title"):
            continue
        a["source_ids"] = [s for s in (a.get("source_ids") or []) if s in known][:4]
        if not a["source_ids"] and a["kind"] != "prepare":
            continue   # nothing invented: every item points to where it comes from
        a["key"] = _key(a)
        clean.append(a)
    return clean[:8]


def refresh(user: dict) -> None:
    with _lock:
        if user["id"] in _running:
            return
        _running.add(user["id"])
    try:
        alerts = compute(user)
        import json

        with conn() as c:
            c.execute("INSERT INTO briefings(user_id, alerts, created_at) VALUES (%s, %s, now()) "
                      "ON CONFLICT (user_id) DO UPDATE SET alerts=EXCLUDED.alerts, created_at=now()", (user["id"], json.dumps(alerts)))
    except llm.LLMNotConfigured:
        pass
    except Exception:  # noqa: BLE001 - the rest of Today still works
        _failed[user["id"]] = datetime.now(timezone.utc)
        log.exception("Briefing failed for user %s", user["id"])
    finally:
        with _lock:
            _running.discard(user["id"])
            again = user["id"] in _again
            _again.discard(user["id"])
        if again:
            refresh(user)


def refresh_in_background(user: dict, force: bool = False) -> bool:
    """Start a refresh if the list is missing or old (or forced). True when one is running."""
    with conn() as c:
        row = c.execute("SELECT created_at, (SELECT max(created_at) FROM documents) AS newest FROM briefings WHERE user_id=%s",
                        (user["id"],)).fetchone()
    now = datetime.now(timezone.utc)
    stale = force or not row or row["created_at"] < now - timedelta(hours=REFRESH_HOURS) \
        or (row["newest"] and row["newest"] > row["created_at"] and row["created_at"] < now - timedelta(minutes=15))   # new items arrived
    if not force and _failed.get(user["id"], now - timedelta(days=1)) > now - timedelta(minutes=15):
        stale = False
    with _lock:
        running = user["id"] in _running
        if stale and running and force:
            _again.add(user["id"])
    if stale and not running:
        threading.Thread(target=refresh, args=(user,), daemon=True).start()
        return True
    return running


def today(user: dict, tz_minutes: int = 0) -> dict:
    """Everything the Today page shows. tz_minutes: the browser's offset from UTC (JavaScript getTimezoneOffset)."""
    from .main import VISIBLE, _visible_params

    p = _visible_params(user)
    now = datetime.now(timezone.utc)
    local_now = now - timedelta(minutes=tz_minutes)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=tz_minutes)
    local_date = local_now.date()
    with conn() as c:
        events = c.execute(
            "SELECT e.id, e.title, e.starts_at, e.ends_at, e.all_day, e.location, e.organizer, e.attendees, d.private "
            f"FROM events e JOIN documents d ON d.id = e.document_id WHERE {VISIBLE} AND ("
            " (NOT e.all_day AND e.starts_at >= %(start)s AND e.starts_at < %(end)s) OR (e.all_day AND e.starts_at::date BETWEEN %(d0)s AND %(d7)s)) "
            "ORDER BY e.starts_at, e.title LIMIT 60",
            {**p, "start": day_start, "end": day_start + timedelta(days=7), "d0": local_date, "d7": local_date + timedelta(days=6)}).fetchall()
        row = c.execute("SELECT alerts, created_at FROM briefings WHERE user_id=%s", (user["id"],)).fetchone()
        states = {r["key"] for r in c.execute("SELECT key FROM alert_states WHERE user_id=%s AND (until IS NULL OR until > now())",
                                              (user["id"],)).fetchall()}
        alerts = [a for a in (row["alerts"] if row else []) if a.get("key") not in states]
        ids = sorted({s for a in alerts for s in a.get("source_ids", [])})
        docs = {d["id"]: d for d in c.execute(
            f"SELECT d.id, d.title, d.kind, d.filename, d.private, d.metadata->>'url' AS url FROM documents d WHERE d.id = ANY(%(ids)s) AND {VISIBLE}",
            {**p, "ids": ids}).fetchall()} if ids else {}
    for a in alerts:   # sources the person may (still) see
        a["sources"] = [docs[s] for s in a.get("source_ids", []) if s in docs]
    def local_day(e):
        return e["starts_at"].date() if e["all_day"] else (e["starts_at"] - timedelta(minutes=tz_minutes)).date()
    return {"date": local_date.isoformat(),
            "meetings_today": [e for e in events if local_day(e) == local_date],
            "meetings_soon": [e for e in events if local_day(e) > local_date][:12],
            "alerts": alerts, "updated_at": row["created_at"] if row else None}


def set_state(user: dict, key: str, action: str) -> None:
    until = None if action == "done" else datetime.now(timezone.utc) + timedelta(days=1)
    with conn() as c:
        c.execute("INSERT INTO alert_states(user_id, key, until) VALUES (%s, %s, %s) "
                  "ON CONFLICT (user_id, key) DO UPDATE SET until=EXCLUDED.until", (user["id"], key, until))
