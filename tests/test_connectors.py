"""Connections: email (fake IMAP server), calendar (.ics), website (local web server), WhatsApp exports."""
import http.server
import json
import threading
import zipfile
from datetime import date
from email.message import EmailMessage
from functools import partial

import pytest

from app import llm
from app.connectors import calendar_ics, email_imap
from app.ingest import whatsapp
from app.ingest.extract import extract
from app.ingest.pipeline import process_document

captured = {}


@pytest.fixture(autouse=True)
def fake_claude(monkeypatch):
    def fake_stream(question, sources_block, history):
        captured["sources"] = sources_block
        yield "ok [1]"

    monkeypatch.setattr(llm, "stream_answer", fake_stream)


def login(client, email, password):
    r = client.post("/api/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def ask(client, headers, question):
    r = client.post("/api/ask", headers=headers, json={"question": question})
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines()]


def process_pending():
    from app.db import conn

    with conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM documents WHERE status='pending'").fetchall()]
    for i in ids:
        process_document(i)


def wait_for_sync(client, admin, cid):
    import time

    for _ in range(100):
        k = next(k for k in client.get("/api/connectors", headers=admin).json() if k["id"] == cid)
        if k["last_sync"]:
            return k
        time.sleep(0.1)
    raise AssertionError("sync did not finish")


# ------------------------------------------------------------------ WhatsApp

IOS_CHAT = """[12/03/2024, 09:15:02] Dana: Morning! Who has the key to the storage room?
[12/03/2024, 09:16:40] Avi: The storage key is with Rina at the front desk
[12/03/2024, 09:17:05] Avi: ‎<attached: 00000003-AUDIO-2024-03-12-09-17-05.opus>
[13/03/2024, 18:00:00] Dana: Reminder: the Haifa client pays only by bank transfer,
not by credit card.
"""

ANDROID_CHAT = """12.03.24, 09:15 - Messages and calls are end-to-end encrypted.
12.03.24, 09:16 - Avi: The storage key is with Rina at the front desk
13.03.24, 18:00 - Dana: Supplier meeting moved to Thursday
"""


def test_whatsapp_formats():
    assert whatsapp.looks_like_whatsapp(IOS_CHAT) and whatsapp.looks_like_whatsapp(ANDROID_CHAT)
    assert not whatsapp.looks_like_whatsapp("Just a normal note.\nWith two lines.")
    days = whatsapp.parse(IOS_CHAT, {"00000003-AUDIO-2024-03-12-09-17-05.opus": "and the spare key is in the office safe"})
    assert [loc for _, loc in days] == ["chat 12/03/2024", "chat 13/03/2024"]
    assert "Avi: The storage key is with Rina" in days[0][0]
    assert "[voice note: and the spare key is in the office safe]" in days[0][0]
    assert "bank transfer,\nnot by credit card" in days[1][0]  # multi-line message kept together
    assert len(whatsapp.parse(ANDROID_CHAT)) == 2


def test_whatsapp_zip(tmp_path, monkeypatch):
    from app.ingest import transcribe

    monkeypatch.setattr(transcribe, "transcribe", lambda p: [("spare key is in the safe", "0:00")])
    z = tmp_path / "WhatsApp Chat - Office.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("_chat.txt", IOS_CHAT)
        f.writestr("00000003-AUDIO-2024-03-12-09-17-05.opus", b"fake audio")
    sections = extract(z)
    assert "[voice note: spare key is in the safe]" in sections[0][0]


# ------------------------------------------------------------------ calendar

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:1
DTSTART:20261105T090000Z
DTEND:20261105T100000Z
SUMMARY:Weekly sales meeting
LOCATION:Main office
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART;VALUE=DATE:20261210
SUMMARY:Annual boiler inspection - Cohen Towers
DESCRIPTION:Bring the pressure gauge. Contact: Yossi 050-1112233
END:VEVENT
END:VCALENDAR
"""


def test_calendar_parsing():
    months = calendar_ics.events_by_month(ICS, today=date(2026, 11, 1))
    assert sorted(months) == ["2026-11", "2026-12"]
    assert len(months["2026-11"]) == 3  # weekly meeting expanded
    assert "Where: Main office" in months["2026-11"][0]
    assert "Yossi 050-1112233" in months["2026-12"][0] and "(all day)" in months["2026-12"][0]


# ------------------------------------------------------------------ fake mailbox

def _email(subject, body, attachment=None):
    m = EmailMessage()
    m["From"] = "Rina <rina@demo.co.il>"
    m["To"] = "sales@demo.co.il"
    m["Subject"] = subject
    m["Date"] = "Tue, 12 Mar 2024 10:00:00 +0200"
    m.set_content(body)
    if attachment:
        m.add_attachment(attachment[1], maintype="text", subtype="csv", filename=attachment[0])
    return m.as_bytes()


class FakeIMAP:
    mailbox = {}

    def __init__(self, *a, **k):
        self.folder = None

    def login(self, user, password):
        if password != "app-password":
            raise email_imap.imaplib.IMAP4.error("AUTHENTICATIONFAILED Invalid credentials")

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "[Gmail]/Sent Mail"']

    def select(self, name, readonly=True):
        self.folder = name.strip('"')
        return ("OK", [b"1"]) if self.folder in self.mailbox else ("NO", [b"no such folder"])

    def response(self, code):
        return code, [b"777"]

    def uid(self, cmd, *args):
        msgs = self.mailbox[self.folder]
        if cmd == "SEARCH":
            q = args[-1]
            start = int(q.split()[1].split(":")[0]) if q.startswith("UID") else 1
            return "OK", [" ".join(str(u) for u in sorted(msgs) if u >= start).encode()]
        uid = int(args[0])
        return "OK", [(b"1 (RFC822 {1}", msgs[uid]), b")"]

    def logout(self):
        pass


@pytest.fixture
def fake_mailbox(monkeypatch):
    FakeIMAP.mailbox = {"INBOX": {
        1: _email("Price for Cohen Towers", "Agreed price for Cohen Towers maintenance: 4,800 NIS per year."),
        2: _email("Supplier list", "Attached the supplier list.",
                  ("suppliers.csv", b"supplier,item\nElectra Parts,heating elements\n")),
    }}
    monkeypatch.setattr(email_imap.imaplib, "IMAP4_SSL", FakeIMAP)


def test_email_connector_end_to_end(client, fake_mailbox):
    admin = login(client, "admin@test.local", "admin-pass-123")
    dept = client.post("/api/departments", headers=admin, json={"name": "Service"}).json()["id"]
    cfg = {"host": "imap.gmail.com", "port": "993", "username": "sales@demo.co.il", "password": "wrong", "folders": "INBOX"}
    body = {"type": "imap", "name": "Sales inbox", "config": cfg, "department_ids": [dept]}

    r = client.post("/api/connectors", headers=admin, json=body)
    assert r.status_code == 400 and "app password" in r.json()["detail"]

    cfg["password"] = "app-password"
    t = client.post("/api/connectors/test", headers=admin, json=body).json()
    assert "[Gmail]/Sent Mail" in t["folders"]

    cid = client.post("/api/connectors", headers=admin, json=body).json()["id"]
    k = wait_for_sync(client, admin, cid)
    assert k["last_error"] is None and k["documents"] == 3  # 2 emails + 1 attachment
    assert k["config"]["password"] == ""  # never sent back to the browser

    from app.db import conn

    with conn() as c:
        stored = c.execute("SELECT config FROM connectors WHERE id=%s", (cid,)).fetchone()["config"]
    assert stored["password"].startswith("enc:") and "app-password" not in json.dumps(stored)

    process_pending()
    client.post("/api/users", headers=admin, json={"email": "tech@test.local", "password": "password-123",
                                                   "department_ids": [dept]})
    tech = login(client, "tech@test.local", "password-123")
    events = ask(client, tech, "What price did we agree for Cohen Towers maintenance?")
    assert events[0]["sources"][0]["title"] == "Email: Price for Cohen Towers"
    assert "(email)" in captured["sources"]
    ask(client, tech, "Who supplies heating elements?")
    assert "Electra Parts" in captured["sources"]

    # New mail arrives -> only the new one is imported on the next sync
    FakeIMAP.mailbox["INBOX"][3] = _email("New warranty terms", "Warranty is now 5 years.")
    client.post(f"/api/connectors/{cid}/sync", headers=admin)
    import time

    for _ in range(100):
        if next(k for k in client.get("/api/connectors", headers=admin).json() if k["id"] == cid)["documents"] == 4:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("new email not imported")

    # Users outside the department can't see it
    other = client.post("/api/departments", headers=admin, json={"name": "Accounting"}).json()["id"]
    client.post("/api/users", headers=admin, json={"email": "acc@test.local", "password": "password-123",
                                                   "department_ids": [other]})
    acc = login(client, "acc@test.local", "password-123")
    captured.pop("sources", None)
    ask(client, acc, "Cohen Towers maintenance price")
    assert "4,800" not in captured.get("sources", "")

    # Edit without re-typing the password keeps the saved one
    cfg["password"] = ""
    assert client.put(f"/api/connectors/{cid}", headers=admin, json=body).status_code == 200

    # Removing the connection removes everything it imported
    client.delete(f"/api/connectors/{cid}", headers=admin)
    docs = client.get("/api/documents", headers=admin).json()
    assert not any(d["title"].startswith("Email:") for d in docs)


def test_calendar_and_website_connectors(client, monkeypatch, tmp_path):
    admin = login(client, "admin@test.local", "admin-pass-123")
    monkeypatch.setattr(calendar_ics, "fetch", lambda url: ICS)
    real = calendar_ics.events_by_month
    monkeypatch.setattr(calendar_ics, "events_by_month", lambda ics, today=None: real(ics, date(2026, 11, 1)))
    r = client.post("/api/connectors", headers=admin, json={
        "type": "ics", "name": "Office", "config": {"url": "https://calendar.example/secret.ics"}, "company_wide": True})
    assert r.status_code == 200, r.text
    k = wait_for_sync(client, admin, r.json()["id"])
    assert k["last_error"] is None and k["config"]["url"] == ""

    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text('<html><title>Demo Plumbing</title><body><a href="services.html">Services</a>'
                                     "<p>Family plumbing company since 1998, serving the Haifa area.</p></body></html>")
    (site / "services.html").write_text("<html><title>Services</title><body><p>Emergency service available 24/7. "
                                        "Call 1-700-555-555 for leaks and burst pipes.</p></body></html>")
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/index.html"
        r = client.post("/api/connectors", headers=admin, json={
            "type": "website", "name": "Our website", "config": {"url": url}, "company_wide": True})
        k = wait_for_sync(client, admin, r.json()["id"])
        assert k["last_error"] is None and k["documents"] == 2
    finally:
        server.shutdown()

    process_pending()
    events = ask(client, admin, "Is there emergency service at night?")
    assert any(s["title"] == "Web: Services" for s in events[0]["sources"])


def test_whatsapp_upload(client):
    admin = login(client, "admin@test.local", "admin-pass-123")
    r = client.post("/api/documents", headers=admin, data={"company_wide": "true"},
                    files={"files": ("WhatsApp Chat with Office Team.txt", IOS_CHAT.encode(), "text/plain")})
    assert r.status_code == 200, r.text
    process_pending()
    doc = next(d for d in client.get("/api/documents", headers=admin).json() if d["kind"] == "whatsapp")
    assert doc["title"] == "WhatsApp: Office Team" and doc["status"] == "ready"
    ask(client, admin, "Who has the key to the storage room?")
    assert "Rina at the front desk" in captured["sources"] and "(WhatsApp chat)" in captured["sources"]


def test_connectors_admin_only(client):
    admin = login(client, "admin@test.local", "admin-pass-123")
    client.post("/api/users", headers=admin, json={"email": "member@test.local", "password": "password-123"})
    member = login(client, "member@test.local", "password-123")
    assert client.get("/api/connectors", headers=member).status_code == 403


# ------------------------------------------------------------------ website reader details

def _site_client(pages: dict, status: dict | None = None):
    import httpx

    def handler(request):
        url = str(request.url)
        if url.rstrip("/") == "https://acme.example":  # bare domain redirects to www
            return httpx.Response(301, headers={"location": "https://www.acme.example/"})
        path = request.url.path.rstrip("/") or "/"
        if status and path in status:
            return httpx.Response(status[path])
        if path in pages:
            ctype = "application/xml" if path.endswith(".xml") else "text/html; charset=utf-8"
            return httpx.Response(200, text=pages[path], headers={"content-type": ctype})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_website_follows_www_redirect_and_sitemap():
    from app.connectors.website import crawl

    pages = {
        "/": '<html><title>Acme</title><body><a href="/about/">About</a><a href="https://www.acme.example/?utm_source=x">Home</a>'
             "<p>Acme builds sales engines for B2B companies.</p></body></html>",
        "/about": "<html><title>About</title><body><p>Founded by Dana in 2015 in Tel Aviv.</p></body></html>",
        "/hidden-pricing": "<html><title>Pricing</title><body><p>Packages start at 5,000 NIS per month.</p></body></html>",
        "/sitemap.xml": "<urlset><url><loc>https://www.acme.example/hidden-pricing</loc></url></urlset>",
    }
    result = crawl("acme.example", client=_site_client(pages))
    assert set(result) == {"https://www.acme.example", "https://www.acme.example/about", "https://www.acme.example/hidden-pricing"}


def test_website_clear_errors():
    from app.connectors.website import WebsiteError, crawl

    with pytest.raises(WebsiteError, match="refused"):
        crawl("https://www.acme.example", client=_site_client({}, {"/": 403}))
    with pytest.raises(WebsiteError, match="JavaScript"):
        crawl("https://www.acme.example", client=_site_client({"/": '<html><body><div id="root"></div><script>app()</script></body></html>'}))
    with pytest.raises(WebsiteError, match="doesn't look like"):
        crawl("hello")


def test_overview_counts_only_what_user_can_see(client):
    admin = login(client, "admin@test.local", "admin-pass-123")
    secret_dept = client.post("/api/departments", headers=admin, json={"name": "Board"}).json()["id"]
    client.post("/api/documents", headers=admin, data={"department_ids": str(secret_dept)},
                files={"files": ("board-notes.txt", b"Board meeting notes: plan to open a Haifa branch.", "text/plain")})
    process_pending()
    o = client.get("/api/overview", headers=admin).json()
    assert o["total"] > 0 and o["ready"].get("web", 0) >= 1
    client.post("/api/users", headers=admin, json={"email": "nodept@test.local", "password": "password-123"})
    nobody = login(client, "nodept@test.local", "password-123")
    mine = client.get("/api/overview", headers=nobody).json()
    assert mine["total"] < o["total"]  # department-only items are not counted for someone outside those departments


def test_masked_api_key_is_explained(client):
    admin = login(client, "admin@test.local", "admin-pass-123")
    r = client.put("/api/admin/ai-key", headers=admin, json={"key": "sk-ant-u" + "•" * 90 + "WwAA"})
    assert r.status_code == 400 and "hidden dots" in r.json()["detail"]
