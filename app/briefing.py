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


# Mail sent by machines (receipts, bills charged automatically, alerts, newsletters) is never a task
AUTOMATED = re.compile(r"no-?reply|do-?not-?reply|notification|alert|billing|invoice|receipt|statement|mailer|newsletter|news@|"
                       r"marketing|updates?@|accounts?@|payments?@|bounce|calendar-notification|@.*\.(google|amazon|apple|microsoft|"
                       r"paypal|stripe|facebookmail|linkedin|meta|zoom|slack|atlassian|notion|wix|godaddy)\.", re.I)
AUTOMATED_SUBJECT = re.compile(r"receipt|your (invoice|order|bill|payment|subscription)|payment (received|confirmation|successful)|"
                               r"statement|newsletter|webinar|verify|verification|security alert|sign-?in|password|"
                               r"קבלה|חשבונית מס|אישור תשלום", re.I)
PAYMENT_ASK = re.compile(r"invoice|payment request|please pay|bank transfer|amount due|חשבונית|דרישת תשלום|לתשלום|העברה בנקאית", re.I)
PAID = re.compile(r"\bpaid\b|payment (sent|made|done|transferred)|transferred|receipt|wire sent|שולם|שילמנו|העברתי|העברנו|בוצעה העברה", re.I)
QUESTIONISH = re.compile(r"\?|please|can you|could you|would you|let me know|waiting for|need (you|your)|אפשר|תוכל|תוכלי|נשמח|מחכה|\?", re.I)
REPLY_DAYS, CHASE_DAYS, PAYMENT_DAYS = 10, 30, 60


def _subject(title: str) -> str:
    return re.sub(r"^((re|fwd?|fw|השב|הועבר)\s*:\s*)+", "", (title or "").replace("Email: ", "", 1), flags=re.I).strip().lower()


def _addr(value: str) -> str:
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", value or "")
    return m.group(0).lower() if m else ""


def _person(value: str) -> str:
    return re.sub(r"\s*<[^>]*>", "", value or "").strip().strip('"') or _addr(value)


def candidates(user: dict) -> tuple[list[dict], list[dict]]:
    """The conversations that may need this person, already filtered: someone waiting for their answer, people who went
    quiet after they wrote, payment requests with no sign of payment. Plus their meetings in the next week."""
    from .main import VISIBLE, _my_addresses, _visible_params

    p = _visible_params(user)
    mine = _my_addresses(user)
    first_text = "(SELECT ch.content FROM chunks ch WHERE ch.document_id = d.id ORDER BY ch.position LIMIT 1) AS text"
    with conn() as c:
        rows = c.execute(
            f"SELECT d.id, d.kind, d.title, d.metadata, coalesce(d.doc_date, d.updated_at) AS at, d.owner_id, {first_text} "
            f"FROM documents d WHERE {VISIBLE} AND d.kind = 'email' AND d.status = 'ready' "
            "AND coalesce(d.doc_date, d.updated_at) > now() - make_interval(days => %(back)s) ORDER BY at", {**p, "back": PAYMENT_DAYS}).fetchall()
        meetings = c.execute(
            f"SELECT e.id, e.title, e.starts_at, e.all_day, e.attendees FROM events e JOIN documents d ON d.id = e.document_id "
            f"WHERE {VISIBLE} AND e.starts_at >= now() - interval '1 hour' AND e.starts_at < now() + interval '7 days' "
            "ORDER BY e.starts_at LIMIT 20", p).fetchall()
    now = datetime.now(timezone.utc)
    threads: dict[str, list[dict]] = {}
    for r in rows:
        meta = r["metadata"] or {}
        r["from_me"] = _addr(meta.get("from", "")) in mine
        r["to_me"] = r["owner_id"] == user["id"] or any(m in (meta.get("to", "") + " " + meta.get("cc", "")).lower() for m in mine)
        r["machine"] = bool(AUTOMATED.search(meta.get("from", "")) or AUTOMATED_SUBJECT.search(r["title"] or ""))
        r["age"] = (now - r["at"]).days
        threads.setdefault(_subject(r["title"]), []).append(r)
    out = []
    for subject, msgs in threads.items():
        last = msgs[-1]
        if not subject or last["machine"]:
            continue
        others = [m for m in msgs if not m["from_me"]]
        if not last["from_me"]:
            # Someone wrote to me, and nobody from my side answered after it
            if last["to_me"] and last["age"] <= REPLY_DAYS:
                out.append({**last, "signal": "waiting_for_my_reply", "thread": msgs[-3:]})
            continue
        # My side wrote last
        counterpart = _addr((last["metadata"] or {}).get("to", ""))
        if not counterpart or AUTOMATED.search(counterpart):
            continue
        if PAYMENT_ASK.search(f"{last['title']} {last['text'] or ''}") and 5 <= last["age"] <= PAYMENT_DAYS:
            domain = counterpart.split("@")[-1]
            paid = any(PAID.search(f"{m['title']} {m['text'] or ''}") and not m["from_me"] and domain in (m["metadata"] or {}).get("from", "").lower()
                       for m in rows if m["at"] > last["at"])
            if not paid:
                out.append({**last, "signal": "payment_requested_no_sign_of_payment", "thread": msgs[-3:]})
            continue
        if 4 <= last["age"] <= CHASE_DAYS and (others or QUESTIONISH.search(last["text"] or "")):
            out.append({**last, "signal": "no_answer_to_my_email", "thread": msgs[-3:]})
    out.sort(key=lambda m: m["at"], reverse=True)
    return out[:40], meetings


def _heuristic(items: list[dict], mine: list[str]) -> list[dict]:
    """Test mode (no AI): a simple, predictable version of the list."""
    out = []
    for i in items:
        meta, subject = i["metadata"] or {}, re.sub(r"^((re|fwd?):\s*)+", "", (i["title"] or "").replace("Email: ", ""), flags=re.I)
        if i["signal"] == "waiting_for_my_reply" and QUESTIONISH.search(i["text"] or ""):
            who = _person(meta.get("from", "")) or "them"
            out.append({"kind": "reply", "title": f"Reply to {who} about {subject}", "why": f"{who} asked you something and hasn't had an answer yet.",
                        "due": "", "person": who, "source_ids": [i["id"]], "action": f"Draft a reply to {who}'s email \"{subject}\"."})
        elif i["signal"] == "payment_requested_no_sign_of_payment":
            who = _person(meta.get("to", "")) or "them"
            out.append({"kind": "follow_up", "title": f"Check the payment from {who}", "why": f"You asked for payment {i['age']} days ago and there's no sign it was paid.",
                        "due": "", "person": who, "source_ids": [i["id"]], "action": f"Draft a polite payment reminder to {who} about \"{subject}\"."})
    return out[:8]


def compute(user: dict) -> dict:
    """The attention list, and which key dates are worth showing (with why)."""
    from . import dates
    from .main import _my_addresses

    items, meetings = candidates(user)
    upcoming = dates.upcoming(user, datetime.now(timezone.utc).date(), judged=False)
    if not items and not meetings and not upcoming:
        return {"alerts": [], "dates": {}}
    mine = _my_addresses(user)
    if settings.fake_llm:
        result = {"alerts": _heuristic(items, mine), "keep_dates": [{"id": d["id"], "why": ""} for d in upcoming]}
    else:
        result = llm.briefing(user.get("name") or user["email"], llm.company_name(), mine, items, meetings, upcoming)
    known = {i["id"] for i in items}
    clean = []
    for a in result.get("alerts") or []:
        if a.get("kind") not in KINDS or not a.get("title"):
            continue
        a["source_ids"] = [s for s in (a.get("source_ids") or []) if s in known][:4]
        if not a["source_ids"] and a["kind"] != "prepare":
            continue   # nothing invented: every item points to where it comes from
        a["key"] = _key(a)
        clean.append(a)
    ids = {d["id"] for d in upcoming}
    keep = {str(k["id"]): str(k.get("why") or "") for k in result.get("keep_dates") or [] if k.get("id") in ids}
    return {"alerts": clean[:8], "dates": keep}


def refresh(user: dict) -> None:
    with _lock:
        if user["id"] in _running:
            return
        _running.add(user["id"])
    try:
        result = compute(user)
        import json

        with conn() as c:
            c.execute("INSERT INTO briefings(user_id, alerts, date_notes, created_at) VALUES (%s, %s, %s, now()) "
                      "ON CONFLICT (user_id) DO UPDATE SET alerts=EXCLUDED.alerts, date_notes=EXCLUDED.date_notes, created_at=now()",
                      (user["id"], json.dumps(result["alerts"]), json.dumps(result["dates"])))
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
        row = c.execute("SELECT created_at, greatest((SELECT max(created_at) FROM documents), (SELECT max(created_at) FROM commitments)) "
                        "AS newest FROM briefings WHERE user_id=%s",
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
        row = c.execute("SELECT alerts, date_notes, created_at FROM briefings WHERE user_id=%s", (user["id"],)).fetchone()
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
    from . import dates

    notes = row["date_notes"] if row else None   # the AI's verdict on each date: only the ones that matter, and why
    key_dates = [d for d in dates.rhythms(user, local_date) + dates.upcoming(user, local_date, judged=notes) if d["key"] not in states]
    return {"date": local_date.isoformat(), "dates": key_dates,
            "meetings_today": [e for e in events if local_day(e) == local_date],
            "meetings_soon": [e for e in events if local_day(e) > local_date][:12],
            "alerts": alerts, "updated_at": row["created_at"] if row else None}


def set_state(user: dict, key: str, action: str) -> None:
    until = None if action == "done" else datetime.now(timezone.utc) + timedelta(days=1)
    with conn() as c:
        c.execute("INSERT INTO alert_states(user_id, key, until) VALUES (%s, %s, %s) "
                  "ON CONFLICT (user_id, key) DO UPDATE SET until=EXCLUDED.until", (user["id"], key, until))


BRIEF_VERSION = "2"   # 2: conversations instead of single emails; the AI also judges the dates


def upgrade() -> None:
    """Once per new version: forget the old lists so everyone gets the new, smarter one."""
    with conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key='briefing_version'").fetchone()
        if row and row["value"] == BRIEF_VERSION:
            return
        c.execute("DELETE FROM briefings")
        c.execute("INSERT INTO meta(key, value) VALUES ('briefing_version', %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                  (BRIEF_VERSION,))
