"""Three circles: Only me, my team(s), everyone. Each person's assets reach exactly the people they chose."""
import json

import pytest

from app import llm
from app.connectors.base import Sync
from app.db import conn
from tests.test_oauth import login, process_pending

captured = {}


@pytest.fixture(autouse=True)
def fake_answers(monkeypatch):
    def fake_stream(question, sources_block, history):
        captured["sources"] = sources_block
        yield "Here it is [1]"

    monkeypatch.setattr(llm, "stream_answer", fake_stream)


def _user(client, admin, email, role="member", depts=()):
    client.post("/api/users", headers=admin, json={"name": email.split("@")[0].title(), "email": email, "password": "password-123",
                                                   "role": role, "active": True, "department_ids": list(depts)})
    token = client.post("/api/login", json={"email": email, "password": "password-123"}).json()["token"]
    with conn() as c:
        uid = c.execute("SELECT id FROM users WHERE email=%s", (email,)).fetchone()["id"]
    return {"Authorization": f"Bearer {token}"}, uid


def _source(owner_id, kind, title, text, *, private=False, company_wide=False, depts=(), allowed=None, ctype="imap"):
    with conn() as c:
        cid = c.execute("INSERT INTO connectors(type, name, config, company_wide, private, owner_id) VALUES (%s, %s, '{}', %s, %s, %s) RETURNING id",
                        (ctype, title, company_wide, private, owner_id)).fetchone()["id"]
        for d in depts:
            c.execute("INSERT INTO connector_departments VALUES (%s, %s)", (cid, d))
        s = Sync(c, c.execute("SELECT * FROM connectors WHERE id=%s", (cid,)).fetchone())
        s.upsert(f"{title}-1", title, "item.txt", text.encode(), kind, {"from": "someone@x.test"}, allowed_emails=allowed)
    return cid


def _titles(client, headers, question):
    r = client.post("/api/ask", headers=headers, json={"question": question})
    events = [json.loads(x) for x in r.text.splitlines()]
    return [s["title"] for s in next(e for e in events if e["type"] == "sources")["sources"]], events


@pytest.fixture(scope="module")
def company(client):
    admin = login(client)
    marketing = client.post("/api/departments", headers=admin, json={"name": "Marketing"}).json()["id"]
    product = client.post("/api/departments", headers=admin, json={"name": "Product"}).json()["id"]
    maya, maya_id = _user(client, admin, "maya@acme.test", depts=[marketing])
    tom, tom_id = _user(client, admin, "tom@acme.test", depts=[product])
    with conn() as c:
        admin_id = c.execute("SELECT id FROM users WHERE email='admin@test.local'").fetchone()["id"]
    # Maya's own mailbox: only Maya
    _source(maya_id, "email", "Email: Zephyrine salary negotiation", "Zephyrine salary negotiation details for Maya only.", private=True)
    # Maya's marketing Drive: the Marketing team
    _source(maya_id, "file", "Zephyrine brand guidelines", "Zephyrine brand guidelines: logo, colours, tone of voice.", depts=[marketing], ctype="gdrive")
    # Tom's product specs: everyone
    _source(tom_id, "file", "Zephyrine product roadmap", "Zephyrine product roadmap for next year, shared with everyone.", company_wide=True, ctype="gdrive")
    # The admin's own mailbox: only the admin, not even shared with other admins' searches
    _source(admin_id, "email", "Email: Zephyrine board compensation", "Zephyrine board compensation, private to the founder.", private=True)
    # A private Slack channel: only its members, whatever their department
    _source(admin_id, "slack", "Slack #launch-war-room", "Zephyrine launch war room: secret launch date.", company_wide=True,
            allowed=["maya@acme.test", "admin@test.local"], ctype="slack")
    process_pending()
    yield {"admin": admin, "maya": maya, "tom": tom, "marketing": marketing, "product": product}
    with conn() as c:  # leave the shared test database as we found it
        c.execute("DELETE FROM connectors WHERE owner_id IN (%s, %s) OR name IN ('Email: Zephyrine board compensation', 'Slack #launch-war-room')",
                  (maya_id, tom_id))
        c.execute("DELETE FROM documents WHERE title LIKE 'Zephyrine%%'")
        c.execute("DELETE FROM users WHERE id IN (%s, %s)", (maya_id, tom_id))
        c.execute("DELETE FROM departments WHERE id IN (%s, %s)", (marketing, product))


def test_each_person_finds_exactly_their_circles(client, company):
    q = "Everything about Zephyrine"
    maya, _ = _titles(client, company["maya"], q)
    tom, _ = _titles(client, company["tom"], q)
    admin, _ = _titles(client, company["admin"], q)
    assert "Email: Zephyrine salary negotiation" in maya
    assert "Email: Zephyrine salary negotiation" not in tom and "Email: Zephyrine salary negotiation" not in admin  # not even the admin
    assert "Zephyrine brand guidelines" in maya and "Zephyrine brand guidelines" not in tom   # Marketing team only
    assert all("Zephyrine product roadmap" in t for t in (maya, tom, admin))                  # everyone
    assert "Email: Zephyrine board compensation" in admin
    assert "Email: Zephyrine board compensation" not in maya and "Email: Zephyrine board compensation" not in tom
    assert "Slack #launch-war-room" in maya and "Slack #launch-war-room" in admin and "Slack #launch-war-room" not in tom


def test_answers_mark_private_items(client, company):
    _, events = _titles(client, company["maya"], "Zephyrine salary negotiation")
    sources = next(e for e in events if e["type"] == "sources")["sources"]
    mine = next(s for s in sources if s["title"] == "Email: Zephyrine salary negotiation")
    assert mine["private"] is True


def test_brain_lists_and_files_follow_the_circles(client, company):
    tom_docs = [d["title"] for d in client.get("/api/documents?q=Zephyrine", headers=company["tom"]).json()]
    assert tom_docs == ["Zephyrine product roadmap"], tom_docs
    with conn() as c:
        secret = c.execute("SELECT id FROM documents WHERE title='Email: Zephyrine salary negotiation'").fetchone()["id"]
    assert client.get(f"/api/documents/{secret}/file", headers=company["admin"]).status_code == 404
    assert client.get(f"/api/documents/{secret}/preview", headers=company["tom"]).status_code == 404
    assert client.get(f"/api/documents/{secret}/preview", headers=company["maya"]).status_code == 200


def test_admin_reads_others_questions_but_not_their_private_answers(client, company):
    r = client.post("/api/ask", headers=company["maya"], json={"question": "Zephyrine salary negotiation"})
    cid = next(json.loads(x)["id"] for x in r.text.splitlines() if json.loads(x)["type"] == "conversation")
    seen = client.get(f"/api/conversations/{cid}", headers=company["admin"]).json()
    assert seen["messages"][0]["question"] == "Zephyrine salary negotiation"
    assert seen["messages"][0]["answer"] is None and seen["messages"][0]["private_answer"] is True
    assert all(not s.get("private") for s in seen["messages"][0]["sources"])
    listed = next(c for c in client.get("/api/conversations?scope=all", headers=company["admin"]).json() if c["id"] == cid)
    assert listed["last_answer"] is None
    gaps = client.get("/api/admin/questions", headers=company["admin"]).json()["questions"]
    assert all(q["answer"] is None for q in gaps if q["conversation_id"] == cid)
    # Maya herself still sees her full answer
    assert client.get(f"/api/conversations/{cid}", headers=company["maya"]).json()["messages"][0]["answer"]


def test_people_add_documents_to_the_circle_they_choose(client, company):
    files = {"files": ("Zephyrine campaign plan.txt", b"Zephyrine campaign plan for the spring launch.")}
    assert client.post("/api/documents", headers=company["tom"], data={"department_ids": str(company["marketing"])}, files=files).status_code == 403
    r = client.post("/api/documents", headers=company["maya"], data={"department_ids": str(company["marketing"])},
                    files={"files": ("Zephyrine campaign plan.txt", b"Zephyrine campaign plan for the spring launch.")})
    assert r.status_code == 200, r.text
    r = client.post("/api/documents", headers=company["tom"], data={"private": "true"},
                    files={"files": ("Zephyrine my notes.txt", b"Zephyrine notes Tom keeps to himself.")})
    assert r.status_code == 200, r.text
    process_pending()
    assert "Zephyrine campaign plan" in _titles(client, company["maya"], "Zephyrine campaign plan")[0]
    assert "Zephyrine campaign plan" not in _titles(client, company["tom"], "Zephyrine campaign plan")[0]
    assert "Zephyrine my notes" in _titles(client, company["tom"], "Zephyrine notes")[0]
    assert "Zephyrine my notes" not in _titles(client, company["admin"], "Zephyrine notes")[0]


def test_people_manage_only_their_own_connections(client, company):
    with conn() as c:
        maya_mail = c.execute("SELECT connector_id FROM documents WHERE title='Email: Zephyrine salary negotiation'").fetchone()["connector_id"]
    mine = client.get("/api/connectors?mine=true", headers=company["maya"]).json()
    assert maya_mail in [k["id"] for k in mine]
    assert maya_mail not in [k["id"] for k in client.get("/api/connectors", headers=company["admin"]).json()]  # not in the admin's list
    assert client.delete(f"/api/connectors/{maya_mail}", headers=company["tom"]).status_code == 404
    assert client.delete(f"/api/connectors/{maya_mail}", headers=company["admin"]).status_code == 404
    assert client.get("/api/connectors", headers=company["tom"]).status_code == 403
    # Maya can share her own source with her team, but not with a team she isn't in
    body = {"type": "imap", "name": "Maya mail", "config": {}, "private": False, "company_wide": False}
    assert client.put(f"/api/connectors/{maya_mail}", headers=company["maya"], json={**body, "department_ids": [company["product"]]}).status_code == 403
