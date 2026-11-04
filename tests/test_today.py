"""Today: your meetings, and what needs your attention. Both follow the same circles as everything else."""
from datetime import datetime, timedelta, timezone

import pytest

from app import briefing, llm
from app.config import settings
from app.connectors import calendar_ics
from app.connectors.base import Sync
from app.db import conn
from tests.test_oauth import login, process_pending
from tests.test_privacy import _user


def _connector(c, ctype, name, owner_id, private):
    cid = c.execute("INSERT INTO connectors(type, name, config, company_wide, private, owner_id) VALUES (%s, %s, '{}', %s, %s, %s) RETURNING id",
                    (ctype, name, not private, private, owner_id)).fetchone()["id"]
    return Sync(c, c.execute("SELECT * FROM connectors WHERE id=%s", (cid,)).fetchone())


@pytest.fixture(scope="module")
def office(client):
    admin = login(client)
    rina, rina_id = _user(client, admin, "rina@acme.test")
    gal, gal_id = _user(client, admin, "gal@acme.test")
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    soon = now + timedelta(hours=1) if now.hour < 22 else now   # still today in UTC
    with conn() as c:
        cal = _connector(c, "ics", "Rina calendar", rina_id, private=True)
        calendar_ics.save(cal, [
            {"start": soon, "end": soon + timedelta(hours=1), "title": "Quorvex pricing call", "attendees": ["dima@quorvex.test"]},
            {"start": now + timedelta(days=2), "title": "Quorvex follow up", "attendees": []},
            {"start": now - timedelta(days=40), "title": "Old Quorvex kickoff", "attendees": []},
        ])
        mail = _connector(c, "imap", "Rina mail", rina_id, private=True)
        mail.upsert("q-1", "Email: Quorvex website proposal", "mail.txt",
                    b"Hi Rina, can you send me the updated Quorvex proposal by Thursday? Thanks, Dima", "email",
                    {"from": "Dima <dima@quorvex.test>", "to": "rina@acme.test", "date": now.isoformat()})
        mail.upsert("q-2", "Email: Weekly newsletter", "mail2.txt", b"Our weekly newsletter. Read more on our blog.", "email",
                    {"from": "news@shop.test", "to": "rina@acme.test", "date": now.isoformat()})
        docs = _connector(c, "gdrive", "Shared drive", None, private=False)
        docs.upsert("q-3", "Quorvex hosting contract", "contract.txt",
                    b"Quorvex hosting contract. The contract renews automatically on 1 December unless cancelled.", "file", {})
    process_pending()
    yield {"rina": rina, "gal": gal, "rina_id": rina_id, "gal_id": gal_id}
    with conn() as c:
        c.execute("DELETE FROM connectors WHERE name IN ('Rina calendar', 'Rina mail', 'Shared drive')")
        c.execute("DELETE FROM users WHERE id IN (%s, %s)", (rina_id, gal_id))


def _user_row(uid):
    import time

    for _ in range(300):   # let a refresh the page started finish first
        if uid not in briefing._running:
            break
        time.sleep(0.1)
    from app.auth import load_user

    return load_user(uid)


def test_calendar_events_are_saved_and_follow_their_circle(client, office):
    rina = client.get("/api/today", headers=office["rina"]).json()
    assert [e["title"] for e in rina["meetings_today"]] == ["Quorvex pricing call"]
    assert rina["meetings_today"][0]["attendees"] == ["dima@quorvex.test"]
    assert "Quorvex follow up" in [e["title"] for e in rina["meetings_soon"]]
    assert "Old Quorvex kickoff" not in str(rina)
    gal = client.get("/api/today", headers=office["gal"]).json()
    assert "Quorvex" not in str(gal["meetings_today"] + gal["meetings_soon"])   # Rina's calendar is only hers


def test_resyncing_a_calendar_replaces_its_events(client, office):
    with conn() as c:
        cal = Sync(c, c.execute("SELECT * FROM connectors WHERE name='Rina calendar'").fetchone())
        start = datetime.now(timezone.utc) + timedelta(days=3)
        calendar_ics.save(cal, [{"start": start, "title": "Only this one"}])
        titles = [r["title"] for r in c.execute("SELECT title FROM events WHERE connector_id=%s", (cal.connector["id"],)).fetchall()]
    assert titles == ["Only this one"]
    with conn() as c:   # put the original calendar back for the other tests
        cal = Sync(c, c.execute("SELECT * FROM connectors WHERE name='Rina calendar'").fetchone())
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        soon = now + timedelta(hours=1) if now.hour < 22 else now
        calendar_ics.save(cal, [{"start": soon, "end": soon + timedelta(hours=1), "title": "Quorvex pricing call", "attendees": ["dima@quorvex.test"]},
                                {"start": now + timedelta(days=2), "title": "Quorvex follow up", "attendees": []}])


def test_attention_list_keeps_only_real_sources_the_person_may_see(client, office, monkeypatch):
    with conn() as c:
        ids = {r["title"]: r["id"] for r in c.execute("SELECT id, title FROM documents WHERE title LIKE '%%Quorvex%%' OR title LIKE '%%newsletter%%'").fetchall()}
    seen = {}

    def fake_briefing(name, company, addresses, items, meetings):
        seen["items"] = [i["title"] for i in items]
        seen["meetings"] = [m["title"] for m in meetings]
        seen["addresses"] = addresses
        return [
            {"kind": "send", "title": "Send Dima the Quorvex proposal", "why": "Dima asked for it by Thursday.", "due": "",
             "person": "Dima", "source_ids": [ids["Email: Quorvex website proposal"]], "action": "Draft an email to Dima with the proposal."},
            {"kind": "renew", "title": "Quorvex hosting renews 1 December", "why": "It renews automatically.", "due": "2026-12-01",
             "person": "", "source_ids": [ids["Quorvex hosting contract"]], "action": "Summarise the renewal terms."},
            {"kind": "reply", "title": "Made up", "why": "Invented.", "source_ids": [999999], "action": "x"},
            {"kind": "party", "title": "Not a kind", "why": "x", "source_ids": [ids["Quorvex hosting contract"]], "action": "x"},
        ]

    monkeypatch.setattr(llm, "briefing", fake_briefing)
    monkeypatch.setattr(settings, "fake_llm", False)
    briefing.refresh(_user_row(office["rina_id"]))
    assert "Email: Quorvex website proposal" in seen["items"] and "Quorvex hosting contract" in seen["items"]
    assert "Quorvex pricing call" in seen["meetings"] and "rina@acme.test" in seen["addresses"]
    today = client.get("/api/today", headers=office["rina"]).json()
    titles = [a["title"] for a in today["alerts"]]
    assert titles == ["Send Dima the Quorvex proposal", "Quorvex hosting renews 1 December"]   # nothing invented
    assert today["alerts"][0]["sources"][0]["title"] == "Email: Quorvex website proposal"
    assert today["updated_at"]

    # Gal's list is made only from what Gal may see: not Rina's private mail
    briefing.refresh(_user_row(office["gal_id"]))
    assert "Email: Quorvex website proposal" not in seen["items"] and "Quorvex hosting contract" in seen["items"]
    assert "Quorvex pricing call" not in seen["meetings"]


def test_done_and_later(client, office):
    alerts = client.get("/api/today", headers=office["rina"]).json()["alerts"]
    first, second = alerts[0]["key"], alerts[1]["key"]
    assert client.post(f"/api/today/alerts/{first}", headers=office["rina"], json={"action": "done"}).json()["ok"]
    assert client.post(f"/api/today/alerts/{second}", headers=office["rina"], json={"action": "later"}).json()["ok"]
    assert client.get("/api/today", headers=office["rina"]).json()["alerts"] == []
    assert client.post(f"/api/today/alerts/{first}", headers=office["rina"], json={"action": "delete"}).status_code == 400
    assert client.post("/api/today/alerts/not-a-key", headers=office["rina"], json={"action": "done"}).status_code == 400
    with conn() as c:   # "Later" comes back tomorrow
        c.execute("UPDATE alert_states SET until = now() - interval '1 minute' WHERE key=%s", (second,))
    assert [a["key"] for a in client.get("/api/today", headers=office["rina"]).json()["alerts"]] == [second]


def test_test_mode_list_without_ai(client, office, monkeypatch):
    monkeypatch.setattr(settings, "fake_llm", True)
    alerts = briefing.compute(_user_row(office["rina_id"]))
    kinds = {a["kind"]: a for a in alerts}
    assert "Dima" in kinds["reply"]["title"] and "renew" in kinds
    assert not any("newsletter" in a["title"].lower() for a in alerts)


def test_learners_have_no_today(client):
    admin = login(client)
    learner, uid = _user(client, admin, "newbie-today@acme.test", role="learner")
    assert client.get("/api/today", headers=learner).status_code == 403
    with conn() as c:
        c.execute("DELETE FROM users WHERE id=%s", (uid,))
