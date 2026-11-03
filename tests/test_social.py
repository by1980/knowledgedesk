"""Social media: Facebook Pages + Instagram (Connect with Facebook), YouTube (through Google), LinkedIn import.
Runs against the same simulated services the browser QA uses (tests/e2e/simulator.py)."""
import io
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from app import llm, oauth
from app.config import settings
from app.connectors import social
from tests.e2e import simulator
from tests.test_oauth import login, process_pending, wait_synced

captured = {}


class SimulatorTransport(httpx.BaseTransport):
    """Send every outgoing call to the simulator app, at /<real host>/<real path>."""

    def __init__(self):
        self.sim = TestClient(simulator.app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        r = self.sim.request(request.method, f"/{request.url.host}{request.url.raw_path.decode()}",
                             content=request.content,
                             headers={k: v for k, v in request.headers.items() if k.lower() in ("authorization", "content-type")})
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "")})


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(oauth.Api, "rate_wait_scale", 0)
    monkeypatch.setattr(oauth, "http", lambda: httpx.Client(transport=SimulatorTransport()))
    monkeypatch.setattr(social, "http", lambda: httpx.Client(transport=SimulatorTransport()))
    monkeypatch.setattr(settings, "google_client_id", "google-client")
    monkeypatch.setattr(settings, "google_client_secret", "google-secret")
    monkeypatch.setattr(settings, "meta_client_id", "meta-app")
    monkeypatch.setattr(settings, "meta_client_secret", "meta-secret")
    monkeypatch.setattr(settings, "oauth_redirect_url", "")

    def fake_stream(question, sources_block, history):
        captured["sources"] = sources_block
        yield "ok [1]"

    monkeypatch.setattr(llm, "stream_answer", fake_stream)
    yield
    # Let background imports finish, so they can't write into the next test's fresh database
    from app.connectors import runner
    for _ in range(200):
        if not any(lock.locked() for lock in list(runner._locks.values())):
            break
        time.sleep(0.05)


def sign_in(client, admin, provider, services):
    r = client.post("/api/oauth/start", headers={**admin, "Origin": "http://localhost:8000"},
                    json={"provider": provider, "services": services, "company_wide": True})
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["url"]).query)
    r = client.get("/oauth/callback", params={"state": q["state"][0], "code": "sim-code"}, follow_redirects=False)
    assert r.status_code == 303 and "connected=" in r.headers["location"], r.text
    return q


def ask(client, admin, question):
    r = client.post("/api/ask", headers=admin, json={"question": question})
    return [e for e in map(json.loads, r.text.splitlines()) if e["type"] != "conversation"]


def test_facebook_and_instagram(client):
    admin = login(client)
    q = sign_in(client, admin, "meta", ["social"])
    assert "instagram_basic" in q["scope"][0] and "pages_read_engagement" in q["scope"][0]
    assert "code_challenge" not in q
    k = wait_synced(client, admin, {"meta_social"})[0]
    assert k["last_error"] is None, k["last_error"]
    assert k["config"]["username"] == "Dana Cohen"
    accounts = {a["platform"]: a for a in k["social_accounts"]}
    assert accounts["Facebook"]["posts"] == 3  # followed the "next" page
    assert accounts["Facebook"]["followers"] == 1840
    assert accounts["Instagram"]["handle"] == "@acme.plumbing" and accounts["Instagram"]["posts"] == 2
    assert accounts["Instagram"]["likes_week"] == 212 and accounts["Instagram"]["posts_week"] == 1
    assert k["documents"] == 3 + 2 + 2  # posts + one summary per account

    process_pending()
    ask(client, admin, "How many likes did our last Instagram post get?")
    src = captured["sources"]
    assert "Instagram account summary" in src and "(social media)" in src
    assert "Latest post" in src and "212 likes" in src
    assert "Last 7 days: 1 post published, 212 likes" in src


def test_youtube_keeps_existing_google_access(client):
    admin = login(client)
    q = sign_in(client, admin, "google", ["youtube"])
    assert "youtube.readonly" in q["scope"][0] and q["include_granted_scopes"] == ["true"]
    k = wait_synced(client, admin, {"youtube"})[0]
    assert k["last_error"] is None, k["last_error"]
    a = k["social_accounts"][0]
    assert a["platform"] == "YouTube" and a["followers"] == 860 and a["posts"] == 2
    process_pending()
    ask(client, admin, "Which YouTube video has the most views?")
    src = captured["sources"]
    assert "9,800 views" in src and "YouTube account summary" in src
    assert "Saved me a call-out" in src or "How to bleed a radiator" in src


def test_manual_check_runs_but_schedule_waits_an_hour(client):
    from app.connectors import runner
    from app.db import conn

    admin = login(client)
    sign_in(client, admin, "meta", ["social"])
    k = wait_synced(client, admin, {"meta_social"})[0]
    while runner.lock_for(k["id"]).locked():  # the first import thread is finishing up
        time.sleep(0.05)
    with conn() as c:
        before = c.execute("SELECT state->>'checked_at' AS t FROM connectors WHERE id=%s", (k["id"],)).fetchone()["t"]
    runner.sync_connector(k["id"], scheduled=True)
    with conn() as c:
        assert c.execute("SELECT state->>'checked_at' AS t FROM connectors WHERE id=%s", (k["id"],)).fetchone()["t"] == before
    assert runner.sync_connector(k["id"]) == {"queued": 0, "error": None}
    with conn() as c:
        assert c.execute("SELECT state->>'checked_at' AS t FROM connectors WHERE id=%s", (k["id"],)).fetchone()["t"] != before


def test_expired_facebook_access_says_what_to_do(client):
    from app.connectors import runner
    from app.connectors.base import encrypt
    from app.db import conn

    admin = login(client)
    sign_in(client, admin, "meta", ["social"])
    k = wait_synced(client, admin, {"meta_social"})[0]
    with conn() as c:
        c.execute("UPDATE oauth_accounts SET tokens=%s", (encrypt(json.dumps({"access_token": "old", "expires_at": 0})),))
    while runner.lock_for(k["id"]).locked():
        time.sleep(0.05)
    runner.sync_connector(k["id"])
    k = next(x for x in client.get("/api/connectors", headers=admin).json() if x["id"] == k["id"])
    assert "Connect with Facebook again" in k["last_error"]


def test_linkedin_export_upload(client):
    ext = ".xlsx"
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["All posts – Acme Plumbing, 1 Oct 2026 – 31 Oct 2026"])
    ws.append(["Post title", "Post link", "Created date", "Impressions", "Likes", "Comments"])
    ws.append(["Proud to sponsor the Haifa marathon", "https://linkedin.com/feed/1", "10/28/2026", 4120, 87, 9])
    buf = io.BytesIO()
    wb.save(buf)
    admin = login(client)
    r = client.post("/api/documents", headers=admin, data={"company_wide": "true", "kind": "linkedin"},
                    files={"files": (f"acme_content{ext}", buf.getvalue())})
    assert r.status_code == 200, r.text
    docs = client.get("/api/documents?kind=social", headers=admin).json()
    assert docs[0]["title"].startswith("LinkedIn: ")
    process_pending()
    ask(client, admin, "How many likes did the LinkedIn marathon post get?")
    assert "Likes: 87" in captured["sources"] and "Post title: Proud to sponsor" in captured["sources"]


def test_sign_in_setup_in_the_app(client, monkeypatch):
    monkeypatch.setattr(settings, "meta_client_id", "")
    monkeypatch.setattr(settings, "meta_client_secret", "")
    oauth._apps_cache.clear()
    admin = login(client)
    assert client.get("/api/oauth/status", headers=admin).json()["configured"]["meta"] is False
    bad = client.put("/api/oauth/apps/meta", headers=admin, json={"client_id": "1234", "client_secret": "wrong"})
    assert bad.status_code == 400 and "doesn't recognise" in bad.json()["detail"]
    masked = client.put("/api/oauth/apps/meta", headers=admin, json={"client_id": "1234", "client_secret": "ab••••"})
    assert masked.status_code == 400 and "hidden" in masked.json()["detail"]
    r = client.put("/api/oauth/apps/meta", headers=admin, json={"client_id": " 1234 ", "client_secret": "s3cret", "config_id": "777"})
    assert r.status_code == 200, r.text
    apps = client.get("/api/oauth/apps", headers=admin).json()["apps"]
    assert apps["meta"]["source"] == "app" and apps["meta"]["client_id"] == "1234" and apps["meta"]["has_secret"]
    assert client.get("/api/oauth/status", headers=admin).json()["configured"]["meta"] is True
    url = client.post("/api/oauth/start", headers={**admin, "Origin": "http://localhost:8000"},
                      json={"provider": "meta", "services": ["social"], "company_wide": True}).json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["1234"] and q["config_id"] == ["777"] and "scope" not in q
    # the whole connection works with the app set up in the app
    r = client.get("/oauth/callback", params={"state": q["state"][0], "code": "sim-code"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert wait_synced(client, admin, {"meta_social"})[0]["last_error"] is None
    # Google: a wrong secret is caught before saving
    r = client.put("/api/oauth/apps/google", headers=admin, json={"client_id": "x.apps.googleusercontent.com", "client_secret": "wrong"})
    assert r.status_code == 400 and "doesn't recognise" in r.json()["detail"]
    r = client.put("/api/oauth/apps/google", headers=admin, json={"client_id": "my-project", "client_secret": "s"})
    assert r.status_code == 400 and "apps.googleusercontent.com" in r.json()["detail"]
    oauth.save_app("meta", None)


def test_youtube_api_switched_off_explains_with_link(client, monkeypatch):
    from app.connectors import runner

    def off(*a, **k):
        raise oauth.ApiError(403, "YouTube Data API v3 has not been used in project 513606 before or it is disabled. "
                                  "Enable it by visiting https://console.developers.google.com/apis/api/youtube.googleapis.com/overview?project=513606 then retry.")
    monkeypatch.setattr(oauth.Api, "json", off)
    admin = login(client)
    for k in client.get("/api/connectors", headers=admin).json():  # start without the channel from earlier tests
        if k["type"] == "youtube":
            client.delete(f"/api/connectors/{k['id']}", headers=admin)
    sign_in(client, admin, "google", ["youtube"])
    k = wait_synced(client, admin, {"youtube"})[0]
    assert "isn't switched on" in k["last_error"] and "youtube.googleapis.com/overview?project=513606" in k["last_error"]
