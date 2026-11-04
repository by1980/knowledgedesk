"""The memory of dates and commitments.

Every document and email is read once for anything with a date that someone has to act on: a contract that ends,
a subscription that renews, the last day to give notice, a payment due, a deadline, a promise ("I'll send it by
Thursday"). They are kept in the commitments table and shown on Tasks ahead of time, each with its lead time
(a renewal six weeks before, a promise a few days before). Nothing is invented: every date points to its source.
"""
import calendar
import logging
import re
import time
from datetime import date, datetime, timedelta

from . import llm
from .config import settings
from .db import conn

log = logging.getLogger("dates")
VERSION = 2   # 2: needs_action and who pays; old results are read again
KINDS = ("renewal", "contract_end", "notice", "payment", "deadline", "promise")
# How many days before the date it starts showing on Tasks
LEAD_DAYS = {"renewal": 45, "contract_end": 45, "notice": 21, "payment": 10, "deadline": 10, "promise": 5}
# How long after the date it still shows (as overdue), if nobody marked it done
OVERDUE_DAYS = {"renewal": 0, "contract_end": 0, "notice": 0, "payment": 21, "deadline": 3, "promise": 3}
CHECK_KINDS = ("email", "file", "documents", "slack", "whatsapp", "interview")
EMAIL_BACK_DAYS = 45    # older emails are not read for dates (their promises are long gone)

# Worth a closer look only if it talks about time limits or commitments
HINT = re.compile(
    r"renew|expir|terminat|notice|contract|agreement|subscription|licen[cs]e|lease|insurance|warranty|due\b|deadline|"
    r"invoice|payment|pay by|valid (until|till|through)|ends? on|until\b|by (mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"by (the )?end of|by tomorrow|next week|i'?ll send|i will send|will get back|promise|"
    r"חידוש|חוזה|הסכם|פג|תוקף|תשלום|חשבונית|מועד|דדליין|עד ה|אשלח|נחזור",
    re.I)


def _text(c, doc_id: int, limit: int = 14000) -> str:
    rows = c.execute("SELECT content FROM chunks WHERE document_id=%s ORDER BY position", (doc_id,)).fetchall()
    out, size = [], 0
    for r in rows:
        out.append(r["content"])
        size += len(r["content"])
        if size > limit:
            break
    return "\n".join(out)[:limit]


# ---------------------------------------------------------------- test mode (no AI): plain patterns
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_DATE = (r"(?P<d1>\d{1,2})(?:st|nd|rd|th)? (?P<m1>[A-Za-z]{3,9})(?: (?P<y1>\d{4}))?|(?P<m2>[A-Za-z]{3,9}) (?P<d2>\d{1,2})(?:st|nd|rd|th)?,?(?: (?P<y2>\d{4}))?"
         r"|(?P<iso>\d{4}-\d{2}-\d{2})")
_RULES = [
    ("renewal", rf"renews?(?: automatically)?(?: on)? (?:{_DATE})"),
    ("contract_end", rf"(?:ends|expires|terminates|valid until|runs until)(?: on)? (?:{_DATE})"),
    ("payment", rf"(?:payment|invoice)[^.\n]{{0,40}}?due(?: on| by)? (?:{_DATE})"),
    ("deadline", rf"deadline(?: is)?(?: on)? (?:{_DATE})"),
]


def _parse(m: re.Match, ref: date) -> date | None:
    try:
        if m.group("iso"):
            return date.fromisoformat(m.group("iso"))
        day, month, year = (m.group("d1"), m.group("m1"), m.group("y1")) if m.group("d1") else (m.group("d2"), m.group("m2"), m.group("y2"))
        month_no = MONTHS.get(month.lower())
        if not month_no:
            return None
        d = date(int(year) if year else ref.year, month_no, int(day))
        return d.replace(year=d.year + 1) if not year and d < ref - timedelta(days=60) else d
    except (ValueError, TypeError):
        return None


def heuristic(title: str, text: str, ref: date) -> list[dict]:
    out = []
    for kind, rule in _RULES:
        for m in re.finditer(rule, text, re.I):
            due = _parse(m, ref)
            if due:
                name = re.sub(r"\.\w{2,4}$", "", title)
                out.append({"kind": kind, "title": f"{name} {kind.replace('_', ' ')}", "due": due.isoformat(),
                            "person": "", "amount": "", "quote": m.group(0)[:200]})
    return out[:8]


# ---------------------------------------------------------------- reading one document
def check(doc_id: int) -> int:
    """Find and save the dates in one document. Returns how many were found."""
    with conn() as c:
        d = c.execute("SELECT id, title, kind, metadata, coalesce(doc_date, updated_at) AS at FROM documents WHERE id=%s",
                      (doc_id,)).fetchone()
        if not d:
            return 0
        text = _text(c, doc_id)
    ref = d["at"].date() if d["at"] else date.today()
    found: list[dict] = []
    if text and HINT.search(text):
        if settings.fake_llm:
            found = heuristic(d["title"], text, ref)
        else:
            meta = d["metadata"] or {}
            head = f"Title: {d['title']}\nType: {d['kind']}\nDate of this item: {ref:%Y-%m-%d}\n"
            if d["kind"] == "email":
                head += f"From: {meta.get('from', '')}\nTo: {meta.get('to', '')}\n"
            found = llm.extract_dates(head + "\n" + text)
    rows = []
    for f in found or []:
        if f.get("needs_action") is False or f.get("direction") == "we_pay_automatically":
            continue
        try:
            due = date.fromisoformat(str(f.get("due", ""))[:10])
        except ValueError:
            continue
        if f.get("kind") not in KINDS or not str(f.get("title", "")).strip():
            continue
        if not (date(2000, 1, 1) < due < date.today() + timedelta(days=3650)) or due < ref:
            continue   # a date before the item itself was written is history, not a commitment
        rows.append((doc_id, f["kind"], str(f["title"])[:300], due, str(f.get("person") or "")[:200] or None,
                     str(f.get("amount") or "")[:100] or None, str(f.get("quote") or "")[:400] or None, f.get("direction") or None))
    with conn() as c:
        c.execute("DELETE FROM commitments WHERE document_id=%s", (doc_id,))
        for r in rows[:8]:
            c.execute("INSERT INTO commitments(document_id, kind, title, due, person, amount, quote, direction) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", r)
        c.execute("UPDATE documents SET dates_checked=%s WHERE id=%s", (VERSION, doc_id))
    return len(rows)


def _next() -> int | None:
    """New and changed documents first; emails only from the last few months."""
    with conn() as c:
        row = c.execute(
            "SELECT id FROM documents WHERE (dates_checked IS NULL OR dates_checked < %s) AND status='ready' AND kind = ANY(%s) "
            "AND (kind <> 'email' OR coalesce(doc_date, updated_at) > now() - make_interval(days => %s)) "
            "ORDER BY (kind = 'email'), coalesce(doc_date, updated_at) DESC LIMIT 1", (VERSION, list(CHECK_KINDS), EMAIL_BACK_DAYS)).fetchone()
    return row["id"] if row else None


def loop() -> None:
    """Background: read every new or changed document for dates, one at a time."""
    while True:
        try:
            doc_id = _next()
            if doc_id is None:
                time.sleep(20)
                continue
            check(doc_id)
        except llm.LLMNotConfigured:
            time.sleep(300)   # no AI key yet: try again later
        except Exception:  # noqa: BLE001 - one bad document must not stop the rest
            log.exception("Reading dates failed")
            if doc_id is not None:
                with conn() as c:
                    c.execute("UPDATE documents SET dates_checked=%s WHERE id=%s", (VERSION, doc_id))
            time.sleep(5)


# ---------------------------------------------------------------- what Tasks shows
def llm_ready() -> bool:
    try:
        llm.api_key()
        return True
    except llm.LLMNotConfigured:
        return False



def upcoming(user: dict, today: date, judged: dict | None | bool = None) -> list[dict]:
    """Dates this person may see that are inside their reminder window now.
    judged: the AI's verdict ({id: why}): only those are kept, with their why. None: not judged yet, show nothing new
    unless there's no AI at all. False: everything (for the AI to judge)."""
    from .main import VISIBLE, _visible_params

    with conn() as c:
        rows = c.execute(
            "SELECT k.id, k.kind, k.title, k.due, k.person, k.amount, k.quote, k.direction, d.id AS doc_id, d.title AS doc_title, d.kind AS doc_kind, "
            "coalesce(d.doc_date, d.updated_at) AS doc_at "
            f"FROM commitments k JOIN documents d ON d.id = k.document_id WHERE {VISIBLE} "
            "AND k.due BETWEEN %(from)s AND %(to)s ORDER BY k.due, k.id",
            {**_visible_params(user), "from": today - timedelta(days=14), "to": today + timedelta(days=max(LEAD_DAYS.values()))}).fetchall()
    out, seen = [], set()
    for r in rows:
        days = (r["due"] - today).days
        late = OVERDUE_DAYS[r["kind"]] if not (r["kind"] == "payment" and r["direction"] != "we_get_paid") else 0
        if days > LEAD_DAYS[r["kind"]] or -days > late:
            continue
        if judged is not False and not settings.fake_llm:
            if isinstance(judged, dict):
                if str(r["id"]) not in judged:
                    continue
                r["why"] = judged[str(r["id"])]
            elif llm_ready():
                continue   # waiting for the AI to check it
        same = (r["kind"], r["due"], r["title"].lower())
        if same in seen:   # the same date found in two copies of a document
            continue
        seen.add(same)
        out.append({**r, "key": f"c{r['id']:015d}"[-16:], "days": days})
    return out


def rhythms(user: dict, today: date) -> list[dict]:
    """The company's calendar: the end of the month, quarter and year, a little ahead of time (for admins)."""
    if user["role"] != "admin":
        return []
    out = []
    year_end = date(today.year, 12, 31)
    q = (today.month - 1) // 3
    quarter_end = date(today.year, 3 * q + 3, calendar.monthrange(today.year, 3 * q + 3)[1])
    month_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    if (year_end - today).days <= 30:
        out.append({"kind": "period", "title": f"The year ends in {(year_end - today).days} days", "due": year_end, "days": (year_end - today).days,
                    "key": f"y{today.year}".ljust(16, "0")[:16],
                    "action": f"The year {today.year} ends on 31 December. Using our emails, contracts and documents, list what I should close "
                              "before the year ends: contracts and subscriptions ending or renewing, unpaid invoices, open proposals, and "
                              "anything promised for this year. Then suggest what to plan for next year."})
    elif (quarter_end - today).days <= 10:
        out.append({"kind": "period", "title": f"Quarter {q + 1} ends in {(quarter_end - today).days} days", "due": quarter_end,
                    "days": (quarter_end - today).days, "key": f"q{today.year}{q + 1}".ljust(16, "0")[:16],
                    "action": f"Quarter {q + 1} ends on {quarter_end:%d %B}. Summarise this quarter from our emails and documents: new clients, "
                              "deals won and lost, open proposals, and what is still open before the quarter closes."})
    elif (month_end - today).days <= 3:
        out.append({"kind": "period", "title": f"The month ends in {(month_end - today).days} days", "due": month_end,
                    "days": (month_end - today).days, "key": f"m{today:%Y%m}".ljust(16, "0")[:16],
                    "action": f"{today:%B} ends on {month_end:%d %B}. List what is still open this month: invoices to send or chase, "
                              "payments due, and promises made for this month."})
    return out
