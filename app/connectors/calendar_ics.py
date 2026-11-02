"""Calendar connector. Uses the calendar's private iCal (.ics) link, which both Google Calendar
and Outlook provide. Events are grouped into one document per month.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta

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


def events_by_month(ics: bytes, today: date | None = None) -> dict[str, list[str]]:
    import icalendar
    import recurring_ical_events

    today = today or date.today()
    cal = icalendar.Calendar.from_ical(ics)
    start, end = today - timedelta(days=PAST_DAYS), today + timedelta(days=FUTURE_DAYS)
    months: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ev in recurring_ical_events.of(cal).between(start, end):
        begin = ev.get("DTSTART").dt
        finish = ev.get("DTEND").dt if ev.get("DTEND") else None
        lines = [f"Event: {ev.get('SUMMARY', '(no title)')}", f"When: {_fmt(begin)}" + (f" to {_fmt(finish)}" if finish else "")]
        if ev.get("LOCATION"):
            lines.append(f"Where: {ev.get('LOCATION')}")
        if ev.get("ORGANIZER"):
            lines.append(f"Organizer: {str(ev.get('ORGANIZER')).replace('mailto:', '')}")
        attendees = ev.get("ATTENDEE")
        if attendees:
            attendees = attendees if isinstance(attendees, list) else [attendees]
            names = [a.params.get("CN") or str(a).replace("mailto:", "") for a in attendees]
            lines.append("Attendees: " + ", ".join(names))
        if ev.get("DESCRIPTION"):
            lines.append(f"Details: {str(ev.get('DESCRIPTION')).strip()[:2000]}")
        key = begin.strftime("%Y-%m")
        months[key].append((_fmt(begin), "\n".join(lines)))
    return {m: [text for _, text in sorted(items)] for m, items in months.items()}


def sync(sync: Sync) -> None:
    cfg = sync.connector["config"]
    name = sync.connector["name"]
    months = events_by_month(fetch(decrypt(cfg["url"])))
    for month, events in sorted(months.items()):
        text = f"Calendar: {name}\nMonth: {month}\n\n" + "\n\n".join(events)
        sync.upsert(f"month:{month}", f"Calendar {name} - {month}", f"calendar-{month}.txt",
                    text.encode(), "calendar", {"month": month})
    sync.remove_unseen()
