"""Calendar connector. Uses the calendar's private iCal (.ics) link, which both Google Calendar
and Outlook provide. Events are grouped into one document per month.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx

from .base import Sync, decrypt

PAST_DAYS = 120
FUTURE_DAYS = 365


def fetch(url: str) -> bytes:
    url = url.strip().replace("webcal://", "https://", 1)
    r = httpx.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content


def _fmt(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d") + " (all day)"
    return str(value)


def window(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    return today - timedelta(days=PAST_DAYS), today + timedelta(days=FUTURE_DAYS)


def group_by_month(events: list[dict]) -> dict[str, list[str]]:
    """events: dicts with start, end (date/datetime), title, location, organizer, attendees, description."""
    months: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ev in events:
        begin, finish = ev["start"], ev.get("end")
        lines = [f"Event: {ev.get('title') or '(no title)'}",
                 f"When: {_fmt(begin)}" + (f" to {_fmt(finish)}" if finish else "")]
        if ev.get("location"):
            lines.append(f"Where: {ev['location']}")
        if ev.get("organizer"):
            lines.append(f"Organizer: {ev['organizer']}")
        if ev.get("attendees"):
            lines.append("Attendees: " + ", ".join(ev["attendees"]))
        if ev.get("description"):
            lines.append(f"Details: {str(ev['description']).strip()[:2000]}")
        months[begin.strftime("%Y-%m")].append((_fmt(begin), "\n".join(lines)))
    return {m: [text for _, text in sorted(items)] for m, items in months.items()}


def events_from_ics(ics: bytes, today: date | None = None) -> list[dict]:
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    events = []
    for ev in recurring_ical_events.of(cal).between(*window(today)):
        attendees = ev.get("ATTENDEE")
        attendees = attendees if isinstance(attendees, list) else ([attendees] if attendees else [])
        events.append({
            "start": ev.get("DTSTART").dt,
            "end": ev.get("DTEND").dt if ev.get("DTEND") else None,
            "title": ev.get("SUMMARY"),
            "location": ev.get("LOCATION"),
            "organizer": str(ev.get("ORGANIZER")).replace("mailto:", "") if ev.get("ORGANIZER") else "",
            "attendees": [a.params.get("CN") or str(a).replace("mailto:", "") for a in attendees],
            "description": ev.get("DESCRIPTION"),
        })
    return events


def events_by_month(ics: bytes, today: date | None = None) -> dict[str, list[str]]:
    return group_by_month(events_from_ics(ics, today))


def _moment(value):
    """A datetime in UTC (times without a zone are taken as UTC), and whether it is an all-day date."""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)), False
    return datetime.combine(value, datetime.min.time(), timezone.utc), True


def save(sync: Sync, events: list[dict]) -> None:
    """Save a calendar: month documents for searching, and each event on its own for "your meetings today"."""
    save_months(sync, group_by_month(events))
    c, cid = sync.c, sync.connector["id"]
    months = {r["external_id"]: r["id"] for r in c.execute(
        "SELECT id, external_id FROM documents WHERE connector_id=%s AND external_id LIKE 'month:%%'", (cid,)).fetchall()}
    c.execute("DELETE FROM events WHERE connector_id=%s", (cid,))
    for ev in events:
        start, all_day = _moment(ev["start"])
        end = _moment(ev["end"])[0] if ev.get("end") else None
        c.execute("INSERT INTO events(connector_id, document_id, title, starts_at, ends_at, all_day, location, organizer, attendees) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                  (cid, months.get(f"month:{ev['start'].strftime('%Y-%m')}"), str(ev.get("title") or "(no title)")[:300], start, end,
                   all_day, str(ev.get("location") or "")[:300] or None, str(ev.get("organizer") or "")[:200] or None,
                   [str(a)[:200] for a in (ev.get("attendees") or [])][:50]))


def save_months(sync: Sync, months: dict[str, list[str]]) -> None:
    name = sync.connector["name"]
    for month, events in sorted(months.items()):
        text = f"Calendar: {name}\nMonth: {month}\n\n" + "\n\n".join(events)
        sync.upsert(f"month:{month}", f"{name}, {datetime.strptime(month, '%Y-%m'):%B %Y}", f"calendar-{month}.txt",
                    text.encode(), "calendar", {"month": month, "events": len(events)})
    sync.remove_unseen()


def sync(sync: Sync) -> None:
    cfg = sync.connector["config"]
    save(sync, events_from_ics(fetch(decrypt(cfg["url"]))))

