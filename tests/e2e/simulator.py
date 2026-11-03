"""Simulated Google + Microsoft (+ a small company website) for end-to-end QA.

Run:  python -m uvicorn tests.e2e.simulator:app --port 9999
The app is started with KD_PROVIDER_OVERRIDE=http://127.0.0.1:9999 so every Google/Microsoft call lands here.
Paths are "/<real host>/<real path>", e.g. /gmail.googleapis.com/gmail/v1/users/me/messages.
"""
import base64
import io
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

app = FastAPI()
STATE = {"gmail_rate_limited_once": False}
TODAY = date.today()


def _email(subject: str, body: str, sender: str, days_ago: int) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "owner@acme-plumbing.test"
    m["Subject"] = subject
    m["Date"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    m.set_content(body)
    return m.as_bytes()


EMAILS = {
    "g1": ("Price agreement – Levi Gardens", "Hi, confirming the annual maintenance contract for Levi Gardens: 7,200 NIS per year, "
           "paid quarterly. Contact person: Yossi Levi, 050-1112233.\n\nOn Mon, Dana wrote:\n> old quoted text that should not matter", "Dana Cohen <dana@acme-plumbing.test>", 3),
    "g2": ("Supplier for heating elements", "From next month we order heating elements from Electra Parts only. Orders go out every Monday.",
           "Moshe <moshe@acme-plumbing.test>", 6),
    "g3": ("Complaint – Ramat Aviv tower", "The customer at Ramat Aviv tower complained about a leak after our visit. Avi will go back on Thursday.",
           "Support <support@acme-plumbing.test>", 1),
    "g4": ("מחירון חדש לשירות", "שלום, מחיר קריאת שירות עולה ל-280 ש\"ח החל מהחודש הבא. התקנת דוד שמש: 2,600 ש\"ח.", "Rina <rina@acme-plumbing.test>", 2),
}
for i in range(5, 31):  # plenty of ordinary mail, to see progress and paging
    EMAILS[f"g{i}"] = (f"Job report #{1000 + i}", f"Job #{1000 + i} finished. Replaced valve, customer satisfied. Technician: Avi.",
                       "Avi <avi@acme-plumbing.test>", i)

OUTLOOK = {
    "o1": ("Vacation policy 2026", "Employees get 14 vacation days per year. Requests go to Rina at least two weeks ahead.", "HR <hr@acme.onmicrosoft.test>", 4),
    "o2": ("Safety training", "Quarterly safety training is mandatory for all technicians.", "HR <hr@acme.onmicrosoft.test>", 9),
}


def _docx(text: str) -> bytes:
    import docx

    d = docx.Document()
    for para in text.split("\n"):
        d.add_paragraph(para)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- sign-in pages and tokens
@app.get("/accounts.google.com/o/oauth2/v2/auth")
@app.get("/login.microsoftonline.com/common/oauth2/v2.0/authorize")
@app.get("/slack.com/oauth/v2/authorize")
def authorize(request: Request, redirect_uri: str, state: str):
    provider = "Google" if "google" in request.url.path else "Slack" if "slack" in request.url.path else "Microsoft"
    # A tiny consent screen with an Allow button, like the real thing
    return HTMLResponse(f"""<!doctype html><title>Sign in – {provider} (simulated)</title>
      <body style="font-family:system-ui;max-width:420px;margin:12vh auto">
      <h2>{provider} (simulated)</h2><p>KnowledgeDesk wants to read your mail, calendar and files.</p>
      <a id="allow" href="{redirect_uri}?code=sim-code&state={state}">Allow</a> ·
      <a id="deny" href="{redirect_uri}?error=access_denied&state={state}">Cancel</a></body>""")


@app.post("/oauth2.googleapis.com/token")
@app.post("/login.microsoftonline.com/common/oauth2/v2.0/token")
async def token(request: Request):
    form = await request.form()
    if form.get("grant_type") == "authorization_code" and form.get("code") != "sim-code":
        return JSONResponse({"error": "invalid_grant"}, 400)
    return {"access_token": "sim-access", "refresh_token": "sim-refresh", "expires_in": 3600}


@app.get("/openidconnect.googleapis.com/v1/userinfo")
def userinfo():
    return {"email": "owner@acme-plumbing.test"}


# ---------------------------------------------------------------- Gmail
@app.get("/gmail.googleapis.com/gmail/v1/users/me/messages")
def gmail_list():
    return {"messages": [{"id": k} for k in EMAILS]}


@app.get("/gmail.googleapis.com/gmail/v1/users/me/messages/{mid}")
def gmail_get(mid: str):
    if mid == "g7" and not STATE["gmail_rate_limited_once"]:  # Google says "slow down" once
        STATE["gmail_rate_limited_once"] = True
        return JSONResponse({"error": {"message": "Quota exceeded for quota metric 'Queries' and limit 'Queries per minute per user'"}}, 429,
                            headers={"retry-after": "1"})
    subject, body, sender, days = EMAILS[mid]
    raw = _email(subject, body, sender, days)
    return {"raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}


# ---------------------------------------------------------------- Google Calendar
@app.get("/www.googleapis.com/calendar/v3/users/me/calendarList")
def cal_list():
    return {"items": [{"id": "primary"}]}


@app.get("/www.googleapis.com/calendar/v3/calendars/primary/events")
def cal_events():
    def at(days, hour):
        return (datetime.combine(TODAY + timedelta(days=days), datetime.min.time()) + timedelta(hours=hour)).isoformat() + "Z"
    return {"items": [
        {"summary": "Supplier visit – Electra Parts", "start": {"dateTime": at(2, 9)}, "end": {"dateTime": at(2, 10)}, "location": "Warehouse"},
        {"summary": "Weekly team meeting", "start": {"dateTime": at(1, 8)}, "end": {"dateTime": at(1, 9)}, "location": "Office",
         "attendees": [{"displayName": "Dana"}, {"displayName": "Avi"}]},
        {"summary": "Boiler inspection – Cohen Towers", "start": {"date": (TODAY + timedelta(days=12)).isoformat()},
         "end": {"date": (TODAY + timedelta(days=13)).isoformat()}, "description": "Bring the pressure gauge."},
    ]}


# ---------------------------------------------------------------- Google Drive
@app.get("/www.googleapis.com/drive/v3/files")
def drive_list():
    return {"files": [
        {"id": "d1", "name": "Employee handbook", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-10-01T00:00:00Z",
         "webViewLink": "https://docs.google.com/document/d/d1"},
        {"id": "d2", "name": "Locked contract", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-10-02T00:00:00Z"},
        {"id": "d3", "name": "Price list.txt", "mimeType": "text/plain", "modifiedTime": "2026-10-03T00:00:00Z", "size": "120"},
        {"id": "d4", "name": "logo.png", "mimeType": "image/png", "modifiedTime": "2026-10-03T00:00:00Z"},
    ]}


@app.get("/www.googleapis.com/drive/v3/files/{fid}/export")
def drive_export(fid: str):
    if fid == "d2":
        return JSONResponse({"error": {"message": "This file cannot be exported by the user."}}, 403)
    return Response(_docx("Employee handbook\nNew technicians shadow a senior technician for two weeks.\n"
                          "Working hours are 7:30 to 16:30, Sunday to Thursday."))


@app.get("/www.googleapis.com/drive/v3/files/{fid}")
def drive_download(fid: str):
    return Response(b"Price list: service call 250 NIS. Solar water heater installation 2,500 NIS. Pipe repair from 400 NIS.")


# ---------------------------------------------------------------- Microsoft Graph
@app.get("/graph.microsoft.com/v1.0/me")
def ms_me():
    return {"userPrincipalName": "office@acme.onmicrosoft.test"}


@app.get("/graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages")
def ms_list(folder: str):
    return {"value": [{"id": k} for k in OUTLOOK] if folder == "inbox" else []}


@app.get("/graph.microsoft.com/v1.0/me/messages/{mid}/$value")
def ms_get(mid: str):
    subject, body, sender, days = OUTLOOK[mid]
    return Response(_email(subject, body, sender, days))


@app.get("/graph.microsoft.com/v1.0/me/calendarView")
def ms_cal():
    start = (datetime.combine(TODAY + timedelta(days=3), datetime.min.time()) + timedelta(hours=8)).isoformat()
    end = (datetime.combine(TODAY + timedelta(days=3), datetime.min.time()) + timedelta(hours=12)).isoformat()
    return {"value": [{"subject": "Quarterly safety training", "isAllDay": False, "start": {"dateTime": start}, "end": {"dateTime": end}}]}


@app.get("/graph.microsoft.com/v1.0/me/drive/root/delta")
def ms_delta():
    return {"value": [{"id": "f1", "name": "Safety rules.txt", "file": {}, "size": 80, "lastModifiedDateTime": "v1"}]}


@app.get("/graph.microsoft.com/v1.0/me/drive/sharedWithMe")
def ms_shared():
    return {"value": []}


@app.get("/graph.microsoft.com/v1.0/me/drive/items/{fid}/content")
def ms_content(fid: str):
    return Response(b"Safety rules: always wear gloves when handling boiler elements. Shut off the gas before any repair.")


# ---------------------------------------------------------------- Slack
NOW = datetime.now(timezone.utc).timestamp()
SLACK_HISTORY = {
    "C1": [{"ts": f"{NOW - 7200:.6f}", "user": "U1", "text": "Reminder: the Haifa warehouse closes at 15:00 on Fridays", "reply_count": 1},
           {"ts": f"{NOW - 3600:.6f}", "user": "U2", "text": "Welcome <@U1> to the team!"}],
    "C2": [{"ts": f"{NOW - 86400:.6f}", "user": "U2", "text": "Closed the Cohen Towers deal: 3 years, 21,000 NIS. See <https://crm.example/deal/9|deal page>"}],
}


@app.post("/slack.com/api/oauth.v2.access")
async def slack_token(request: Request):
    form = await request.form()
    if form.get("code") != "sim-code":
        return {"ok": False, "error": "invalid_code"}
    return {"ok": True, "access_token": "xoxb-sim", "team": {"id": "T1", "name": "Acme Plumbing Slack"}}


@app.post("/slack.com/api/{method}")
async def slack_api(method: str, request: Request):
    if request.headers.get("authorization") != "Bearer xoxb-sim":
        return {"ok": False, "error": "invalid_auth"}
    form = await request.form()
    if method == "users.list":
        return {"ok": True, "members": [{"id": "U1", "profile": {"real_name": "Dana Cohen"}}, {"id": "U2", "profile": {"real_name": "Avi Levi"}}]}
    if method == "conversations.list":
        return {"ok": True, "channels": [{"id": "C1", "name": "general", "is_member": False, "is_private": False},
                                         {"id": "C2", "name": "sales", "is_member": True, "is_private": False},
                                         {"id": "C3", "name": "board", "is_member": False, "is_private": True}]}
    if method == "conversations.join":
        return {"ok": True}
    if method == "conversations.history":
        oldest = float(form.get("oldest") or 0)
        return {"ok": True, "messages": [m for m in SLACK_HISTORY.get(form.get("channel"), []) if float(m["ts"]) >= oldest]}
    if method == "conversations.replies":
        return {"ok": True, "messages": [{"ts": form.get("ts"), "user": "U1", "text": "parent"},
                                         {"ts": f"{NOW - 7000:.6f}", "user": "U2", "text": "Except holidays – then it's closed all day"}]}
    return {"ok": False, "error": "unknown_method"}


# ---------------------------------------------------------------- a small company website
SITE = {
    "": ("Acme Plumbing – Haifa", '<a href="/site/services">Services</a> <a href="/site/about/">About</a>'
         "<p>Acme Plumbing is a family plumbing company serving Haifa and the Krayot since 1998.</p>"),
    "services": ("Services", "<p>Emergency service 24/7: call 1-700-555-555. We fix leaks, burst pipes and boilers.</p>"),
    "about": ("About us", "<p>Founded by Moshe Cohen. 12 technicians, 3 vans, licensed by the Ministry of Labor.</p>"),
}


@app.get("/site/{page:path}")
def site(page: str):
    page = page.strip("/")
    if page not in SITE:
        return Response(status_code=404)
    title, body = SITE[page]
    return HTMLResponse(f"<html><head><title>{title}</title></head><body><nav><a href='/site/'>Home</a></nav>{body}</body></html>")
