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
    "g3": ("Complaint – Ramat Aviv tower", "The customer at Ramat Aviv tower complained about a leak after our visit. Avi will go back on Thursday. Can you call the customer today to apologise?",
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
@app.get("/www.facebook.com/v23.0/dialog/oauth")
def authorize(request: Request, redirect_uri: str, state: str):
    path = request.url.path
    provider = ("Google" if "google" in path else "Slack" if "slack" in path else "Facebook" if "facebook" in path
                else "Microsoft")
    # A tiny consent screen with an Allow button, like the real thing
    return HTMLResponse(f"""<!doctype html><title>Sign in – {provider} (simulated)</title>
      <body style="font-family:system-ui;max-width:420px;margin:12vh auto">
      <h2>{provider} (simulated)</h2><p>Cortex wants to read your mail, calendar and files.</p>
      <a id="allow" href="{redirect_uri}?code=sim-code&state={state}">Allow</a> ·
      <a id="deny" href="{redirect_uri}?error=access_denied&state={state}">Cancel</a></body>""")


@app.post("/oauth2.googleapis.com/token")
@app.post("/login.microsoftonline.com/common/oauth2/v2.0/token")
async def token(request: Request):
    form = await request.form()
    if form.get("client_secret") == "wrong":
        return JSONResponse({"error": "invalid_client", "error_description": "Unauthorized"}, 401)
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
        {"summary": "Pricing call with Levi Gardens", "start": {"dateTime": at(0, 15)}, "end": {"dateTime": at(0, 16)},
         "attendees": [{"email": "yossi@levigardens.test"}]},
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
    renew = TODAY + timedelta(days=20)
    return Response(("Price list: service call 250 NIS. Solar water heater installation 2,500 NIS. Pipe repair from 400 NIS.\n"
                     f"Our supplier agreement with Electra Parts renews automatically on {renew.day} {renew:%B %Y}.").encode())


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



# ---------------------------------------------------------------- Facebook Pages + Instagram
def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


FB = "http://127.0.0.1:9999/graph.facebook.com/v23.0"


def _fb_ok(request: Request, token: str = "fb-long") -> bool:
    return request.headers.get("authorization") == f"Bearer {token}"


@app.get("/graph.facebook.com/v23.0/oauth/access_token")
def fb_token(request: Request):
    q = request.query_params
    if q.get("grant_type") == "client_credentials":  # Cortex checking a pasted App ID + secret
        if not q.get("client_id", "").isdigit() or q.get("client_secret") == "wrong":
            return JSONResponse({"error": {"message": "Error validating client secret.", "code": 1}}, 400)
        return {"access_token": "app-token", "token_type": "bearer"}
    if q.get("grant_type") == "fb_exchange_token":
        return {"access_token": "fb-long", "token_type": "bearer", "expires_in": 5184000}
    if q.get("code") != "sim-code" or not q.get("client_secret"):
        return JSONResponse({"error": {"message": "Invalid verification code format.", "code": 100}}, 400)
    return {"access_token": "fb-short", "token_type": "bearer", "expires_in": 3600}


@app.get("/graph.facebook.com/v23.0/me")
def fb_me(request: Request):
    if not _fb_ok(request):
        return JSONResponse({"error": {"message": "Invalid OAuth access token.", "code": 190}}, 401)
    return {"name": "Dana Cohen", "id": "U100"}


@app.get("/graph.facebook.com/v23.0/me/accounts")
def fb_accounts(request: Request):
    if not _fb_ok(request):
        return JSONResponse({"error": {"message": "Invalid OAuth access token.", "code": 190}}, 401)
    return {"data": [{"id": "P1", "name": "Acme Plumbing", "access_token": "page-token", "link": "https://facebook.com/acmeplumbing",
                      "followers_count": 1840, "fan_count": 1800,
                      "instagram_business_account": {"id": "IG1", "username": "acme.plumbing", "name": "Acme Plumbing",
                                                     "followers_count": 3120, "media_count": 3}}]}


@app.get("/graph.facebook.com/v23.0/P1/posts")
def fb_posts(request: Request, after: str = ""):
    if not _fb_ok(request, "page-token"):
        return JSONResponse({"error": {"message": "Invalid OAuth access token.", "code": 190}}, 401)
    if after == "page2":
        return {"data": [{"id": "P1_3", "message": "We're hiring a junior plumber in Haifa. Send your CV!", "created_time": _iso(40),
                          "permalink_url": "https://facebook.com/acmeplumbing/posts/3",
                          "reactions": {"summary": {"total_count": 15}}, "comments": {"data": [], "summary": {"total_count": 0}}}]}
    return {"data": [
        {"id": "P1_1", "message": "Winter boiler check: 20% off until the end of the month.", "created_time": _iso(1),
         "permalink_url": "https://facebook.com/acmeplumbing/posts/1", "shares": {"count": 4},
         "reactions": {"summary": {"total_count": 57}},
         "comments": {"data": [{"message": "Do you also cover Kiryat Ata?", "from": {"name": "Yossi"}}], "summary": {"total_count": 6}}},
        {"id": "P1_2", "message": "Our team at the Haifa trade fair", "created_time": _iso(12),
         "permalink_url": "https://facebook.com/acmeplumbing/posts/2",
         "reactions": {"summary": {"total_count": 31}}, "comments": {"data": [], "summary": {"total_count": 2}}}],
        "paging": {"next": "https://graph.facebook.com/v23.0/P1/posts?after=page2"}}


@app.get("/graph.facebook.com/v23.0/IG1/media")
def ig_media(request: Request):
    if not _fb_ok(request, "page-token"):
        return JSONResponse({"error": {"message": "Invalid OAuth access token.", "code": 190}}, 401)
    return {"data": [
        {"id": "M1", "caption": "Before and after: full bathroom renovation in Nesher #plumbing", "media_type": "CAROUSEL_ALBUM",
         "timestamp": _iso(2), "permalink": "https://instagram.com/p/M1", "like_count": 212, "comments_count": 14,
         "comments": {"data": [{"text": "Amazing work!", "username": "noa.k"}]}},
        {"id": "M2", "caption": "Meet Avi, our emergency technician", "media_type": "VIDEO", "timestamp": _iso(9),
         "permalink": "https://instagram.com/p/M2", "like_count": 98, "comments_count": 3}]}


# ---------------------------------------------------------------- YouTube
@app.get("/www.googleapis.com/youtube/v3/channels")
def yt_channels():
    return {"items": [{"id": "UC1", "snippet": {"title": "Acme Plumbing TV", "customUrl": "@acmeplumbing"},
                       "statistics": {"subscriberCount": "860", "viewCount": "45210"},
                       "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}


@app.get("/www.googleapis.com/youtube/v3/playlistItems")
def yt_playlist():
    return {"items": [{"contentDetails": {"videoId": "V1"}}, {"contentDetails": {"videoId": "V2"}}]}


@app.get("/www.googleapis.com/youtube/v3/videos")
def yt_videos():
    return {"items": [
        {"id": "V1", "snippet": {"title": "How to bleed a radiator in 3 minutes", "description": "Step by step guide.",
                                 "publishedAt": _iso(3).replace("+0000", "Z")},
         "statistics": {"viewCount": "1530", "likeCount": "88", "commentCount": "5"}},
        {"id": "V2", "snippet": {"title": "Fixing a leaking tap", "description": "", "publishedAt": _iso(60).replace("+0000", "Z")},
         "statistics": {"viewCount": "9800", "likeCount": "410", "commentCount": "0"}}]}


@app.get("/www.googleapis.com/youtube/v3/commentThreads")
def yt_comments():
    return {"items": [{"snippet": {"topLevelComment": {"snippet": {"authorDisplayName": "Ron", "textDisplay": "Saved me a call-out, thanks!"}}}}]}


# ---------------------------------------------------------------- Meta (WhatsApp Business)
@app.get("/graph.facebook.com/v23.0/{object_id}")
def meta_object(object_id: str, request: Request):
    if request.headers.get("authorization") != "Bearer wa-token":
        return JSONResponse({"error": {"message": "Invalid OAuth access token."}}, 401)
    if object_id == "PHONE1":
        return {"display_phone_number": "+972 50-123-4567", "verified_name": "Acme Plumbing", "id": "PHONE1"}
    return {"url": "http://127.0.0.1:9999/media/voice.ogg", "id": object_id}


# ---------------------------------------------------------------- a small company website
SITE = {
    "": ("Acme Plumbing – Haifa", '<a href="/site/"><img src="/site/logo.png" alt="Acme logo"></a><a href="/site/services">Services</a> <a href="/site/about/">About</a>'
         "<p>Acme Plumbing is a family plumbing company serving Haifa and the Krayot since 1998.</p>"),
    "services": ("Services", "<p>Emergency service 24/7: call 1-700-555-555. We fix leaks, burst pipes and boilers.</p>"),
    "about": ("About us", "<p>Founded by Moshe Cohen. 12 technicians, 3 vans, licensed by the Ministry of Labor.</p>"),
}


@app.get("/site/logo.png")
def site_logo():
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (120, 40), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([4, 8, 116, 32], fill=(20, 60, 160, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.get("/site/{page:path}")
def site(page: str):
    page = page.strip("/")
    if page not in SITE:
        return Response(status_code=404)
    title, body = SITE[page]
    return HTMLResponse(f"<html><head><title>{title}</title></head><body><nav><a href='/site/'>Home</a></nav>{body}</body></html>")
