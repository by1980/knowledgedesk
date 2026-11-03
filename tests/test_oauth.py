"""One-click sign-in with Google and Microsoft, against simulated Google/Microsoft servers."""
import base64
import io
import json
import time
from email.message import EmailMessage
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import llm, oauth
from app.config import settings
from app.ingest.pipeline import process_document

captured = {}
calls = []


def _raw_email(subject, body):
    m = EmailMessage()
    m["From"] = "Owner <owner@demo.com>"
    m["To"] = "team@demo.com"
    m["Subject"] = subject
    m["Date"] = "Mon, 02 Nov 2026 10:00:00 +0200"
    m.set_content(body)
    return m.as_bytes()


def _docx(text):
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def fake_servers(request: httpx.Request) -> httpx.Response:
    url, host, path = request.url, request.url.host, request.url.path
    calls.append(f"{request.method} {host}{path}")
    form = parse_qs(request.content.decode()) if request.method == "POST" else {}

    if path.endswith("/token"):
        if form.get("grant_type") == ["authorization_code"]:
            assert form.get("code") == ["good-code"] and form.get("code_verifier"), form
            return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})
        if form.get("grant_type") == ["refresh_token"]:
            return httpx.Response(200, json={"access_token": "AT2", "expires_in": 3600, "refresh_token": "RT2"})
    assert request.headers.get("authorization", "").startswith("Bearer AT"), f"no token for {url}"

    # ---- Google
    if host == "openidconnect.googleapis.com":
        return httpx.Response(200, json={"email": "owner@demo.com"})
    if path == "/gmail/v1/users/me/messages":
        return httpx.Response(200, json={"messages": [{"id": "g1"}]})
    if path == "/gmail/v1/users/me/messages/g1":
        raw = _raw_email("Pricing for Levi Gardens", "Levi Gardens annual contract: 7,200 NIS, paid quarterly.")
        return httpx.Response(200, json={"raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")})
    if path == "/calendar/v3/users/me/calendarList":
        return httpx.Response(200, json={"items": [{"id": "primary"}]})
    if path == "/calendar/v3/calendars/primary/events":
        return httpx.Response(200, json={"items": [{"summary": "Supplier visit - Electra Parts",
                                                    "start": {"dateTime": "2026-11-20T09:00:00Z"},
                                                    "end": {"dateTime": "2026-11-20T10:00:00Z"},
                                                    "location": "Warehouse"}]})
    if path == "/drive/v3/files":
        return httpx.Response(200, json={"files": [
            {"id": "d1", "name": "Onboarding guide", "mimeType": "application/vnd.google-apps.document",
             "modifiedTime": "2026-10-01T00:00:00Z"},
            {"id": "d2", "name": "logo.png", "mimeType": "image/png", "modifiedTime": "2026-10-01T00:00:00Z"},
        ]})
    if path == "/drive/v3/files/d1/export":
        return httpx.Response(200, content=_docx("New technicians shadow a senior technician for two weeks."))

    # ---- Microsoft
    if path == "/v1.0/me":
        return httpx.Response(200, json={"userPrincipalName": "boss@demo.onmicrosoft.com"})
    if path == "/v1.0/me/mailFolders/inbox/messages":
        return httpx.Response(200, json={"value": [{"id": "o1"}]})
    if path == "/v1.0/me/mailFolders/sentitems/messages":
        return httpx.Response(200, json={"value": []})
    if path == "/v1.0/me/messages/o1/$value":
        return httpx.Response(200, content=_raw_email("Vacation policy", "Employees get 14 vacation days per year."))
    if path == "/v1.0/me/calendarView":
        return httpx.Response(200, json={"value": [{"subject": "Quarterly safety training", "isAllDay": False,
                                                    "start": {"dateTime": "2026-11-25T08:00:00.0000000"},
                                                    "end": {"dateTime": "2026-11-25T12:00:00.0000000"}}]})
    if path == "/v1.0/me/drive/root/delta":
        return httpx.Response(200, json={"value": [
            {"id": "f1", "name": "Safety handbook.txt", "file": {}, "size": 80, "lastModifiedDateTime": "v1"},
            {"id": "f2", "name": "Old.txt", "deleted": {}},
        ]})
    if path == "/v1.0/me/drive/sharedWithMe":
        return httpx.Response(200, json={"value": []})
    if path == "/v1.0/me/drive/items/f1/content":
        return httpx.Response(200, content=b"Always wear gloves when handling boiler elements.")
    return httpx.Response(404, json={"error": {"message": f"not mocked: {url}"}})


@pytest.fixture(autouse=True)
def setup_fakes(monkeypatch):
    monkeypatch.setattr(oauth, "http", lambda: httpx.Client(transport=httpx.MockTransport(fake_servers)))
    monkeypatch.setattr(settings, "google_client_id", "google-client")
    monkeypatch.setattr(settings, "google_client_secret", "google-secret")
    monkeypatch.setattr(settings, "microsoft_client_id", "ms-client")
    monkeypatch.setattr(settings, "oauth_redirect_url", "https://connect.vendor.example/oauth.html")

    def fake_stream(question, sources_block, history):
        captured["sources"] = sources_block
        yield "ok [1]"

    monkeypatch.setattr(llm, "stream_answer", fake_stream)


def login(client):
    r = client.post("/api/login", json={"email": "admin@test.local", "password": "admin-pass-123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def connect(client, admin, provider):
    r = client.post("/api/oauth/start", headers={**admin, "Origin": "http://192.168.1.20:8000"}, json={
        "provider": provider, "services": ["mail", "calendar", "files"], "company_wide": True})
    assert r.status_code == 200, r.text
    url = urlparse(r.json()["url"])
    q = parse_qs(url.query)
    assert q["redirect_uri"] == ["https://connect.vendor.example/oauth.html"]
    assert q["code_challenge_method"] == ["S256"]
    state = q["state"][0]
    # The relay page reads the installation address from the state
    target = json.loads(base64.urlsafe_b64decode(state + "=="))["r"]
    assert target == "http://192.168.1.20:8000"
    r = client.get("/oauth/callback", params={"state": state, "code": "good-code"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?connected=3"), r.text
    return q


def wait_synced(client, admin, types):
    for _ in range(200):
        rows = [k for k in client.get("/api/connectors", headers=admin).json() if k["type"] in types]
        if len(rows) == len(types) and all(k["last_sync"] for k in rows):
            return rows
        time.sleep(0.05)
    raise AssertionError(f"not synced: {rows}")


def process_pending():
    from app.db import conn

    with conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM documents WHERE status='pending'").fetchall()]
    for i in ids:
        process_document(i)


def ask(client, admin, q):
    r = client.post("/api/ask", headers=admin, json={"question": q})
    return [json.loads(x) for x in r.text.splitlines()]


def test_google_one_click(client):
    admin = login(client)
    q = connect(client, admin, "google")
    assert "https://www.googleapis.com/auth/gmail.readonly" in q["scope"][0] and q["access_type"] == ["offline"]
    rows = wait_synced(client, admin, {"gmail", "gcal", "gdrive"})
    assert all(k["last_error"] is None for k in rows), rows
    assert {k["config"]["username"] for k in rows} == {"owner@demo.com"}
    assert {k["type"]: k["documents"] for k in rows} == {"gmail": 1, "gcal": 1, "gdrive": 1}  # png skipped

    process_pending()
    ask(client, admin, "How much is the Levi Gardens contract?")
    assert "7,200 NIS" in captured["sources"]
    ask(client, admin, "When is the Electra Parts supplier visit?")
    assert "2026-11-20 09:00" in captured["sources"]
    ask(client, admin, "How long do new technicians shadow?")
    assert "two weeks" in captured["sources"]

    # Tokens are stored encrypted
    from app.db import conn

    with conn() as c:
        tokens = c.execute("SELECT tokens FROM oauth_accounts WHERE email='owner@demo.com'").fetchone()["tokens"]
    assert tokens.startswith("enc:") and "RT1" not in tokens


def test_microsoft_one_click_and_token_refresh(client):
    admin = login(client)
    connect(client, admin, "microsoft")
    rows = wait_synced(client, admin, {"ms_mail", "ms_cal", "ms_drive"})
    assert all(k["last_error"] is None for k in rows), rows
    assert {k["type"]: k["documents"] for k in rows} == {"ms_mail": 1, "ms_cal": 1, "ms_drive": 1}

    process_pending()
    ask(client, admin, "How many vacation days do employees get?")
    assert "14 vacation days" in captured["sources"]
    ask(client, admin, "Do I need gloves for boiler elements?")
    assert "wear gloves" in captured["sources"]

    # Expired access token -> refreshed automatically, and Microsoft's new refresh token is saved
    from app.connectors.base import decrypt, encrypt
    from app.db import conn

    with conn() as c:
        acc = c.execute("SELECT id, tokens FROM oauth_accounts WHERE provider='microsoft'").fetchone()
        t = json.loads(decrypt(acc["tokens"]))
        t["expires_at"] = 0
        c.execute("UPDATE oauth_accounts SET tokens=%s WHERE id=%s", (encrypt(json.dumps(t)), acc["id"]))
    assert oauth.access_token(acc["id"]) == "AT2"
    with conn() as c:
        t = json.loads(decrypt(c.execute("SELECT tokens FROM oauth_accounts WHERE id=%s", (acc["id"],)).fetchone()["tokens"]))
    assert t["refresh_token"] == "RT2"

    # Removing all of an account's connections forgets the account
    for k in rows:
        assert client.delete(f"/api/connectors/{k['id']}", headers=admin).status_code == 200
    with conn() as c:
        assert c.execute("SELECT 1 FROM oauth_accounts WHERE id=%s", (acc["id"],)).fetchone() is None


def test_oauth_errors_and_permissions(client):
    admin = login(client)
    # Cancelled on Google's page
    r = client.get("/oauth/callback", params={"error": "access_denied", "state": "x"})
    assert "cancelled" in r.text
    # Forged or replayed state
    bad = base64.urlsafe_b64encode(json.dumps({"k": "nope", "r": "http://x"}).encode()).decode()
    r = client.get("/oauth/callback", params={"state": bad, "code": "good-code"})
    assert "expired" in r.text
    # Must choose visibility and at least one service
    r = client.post("/api/oauth/start", headers=admin, json={"provider": "google", "services": ["mail"]})
    assert r.status_code == 400
    r = client.post("/api/oauth/start", headers=admin, json={"provider": "google", "services": [], "company_wide": True})
    assert r.status_code == 400
    # Not an admin
    client.post("/api/users", headers=admin, json={"email": "plain@test.local", "password": "password-123"})
    r = client.post("/api/login", json={"email": "plain@test.local", "password": "password-123"})
    member = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post("/api/oauth/start", headers=member,
                       json={"provider": "google", "services": ["mail"], "company_wide": True}).status_code == 403
    # Account-based connections can't be created by hand
    r = client.post("/api/connectors", headers=admin, json={"type": "gmail", "name": "x", "company_wide": True})
    assert r.status_code == 400


def test_not_configured(client, monkeypatch):
    admin = login(client)
    monkeypatch.setattr(settings, "microsoft_client_id", "")
    monkeypatch.setattr(oauth, "_vendor_file", lambda: {})
    st = client.get("/api/oauth/status", headers=admin).json()
    assert st["configured"] == {"google": True, "microsoft": False}
    r = client.post("/api/oauth/start", headers=admin,
                    json={"provider": "microsoft", "services": ["mail"], "company_wide": True})
    assert r.status_code == 400 and "not set up" in r.json()["detail"]
