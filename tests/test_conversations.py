"""Conversations: questions are grouped, can be reopened and continued, and stay private."""
import json

import pytest

from app import llm
from tests.test_oauth import login


@pytest.fixture(autouse=True)
def fake_answers(monkeypatch):
    def fake_stream(question, sources_block, history):
        yield f"answer to: {question} (history {len(history)}) [1]"

    monkeypatch.setattr(llm, "stream_answer", fake_stream)


def ask(client, headers, question, cid=None, history=()):
    r = client.post("/api/ask", headers=headers, json={"question": question, "conversation_id": cid, "history": list(history)})
    assert r.status_code == 200, r.text
    events = [json.loads(x) for x in r.text.splitlines()]
    return next(e["id"] for e in events if e["type"] == "conversation"), events


def test_conversations(client):
    admin = login(client)
    client.post("/api/documents", headers=admin, data={"company_wide": "true"},
                files={"files": ("Opening hours.txt", b"The shop is open Sunday to Thursday, 8:00 to 17:00.")})
    from tests.test_oauth import process_pending
    process_pending()

    cid, events = ask(client, admin, "When is the shop open on Sunday?")
    assert events[0]["type"] == "conversation"
    cid2, _ = ask(client, admin, "And on Friday?", cid, [{"role": "user", "content": "When is the shop open on Sunday?"},
                                                         {"role": "assistant", "content": "8 to 17"}])
    assert cid2 == cid

    mine = client.get("/api/conversations", headers=admin).json()
    row = next(c for c in mine if c["id"] == cid)
    assert row["title"] == "When is the shop open on Sunday?" and row["messages"] == 2
    full = client.get(f"/api/conversations/{cid}", headers=admin).json()
    assert [m["question"] for m in full["messages"]] == ["When is the shop open on Sunday?", "And on Friday?"]
    assert full["mine"] is True
    assert client.get("/api/conversations?q=friday", headers=admin).json()[0]["id"] == cid

    # Someone else can't see or continue it; admins can read everyone's
    client.post("/api/users", headers=admin, json={"name": "Avi", "email": "avi@test.local", "password": "avi-pass-123",
                                                   "role": "member", "active": True, "department_ids": []})
    avi = {"Authorization": "Bearer " + client.post("/api/login", json={"email": "avi@test.local", "password": "avi-pass-123"}).json()["token"]}
    assert client.get(f"/api/conversations/{cid}", headers=avi).status_code == 404
    assert client.post("/api/ask", headers=avi, json={"question": "hi", "conversation_id": cid}).status_code == 404
    assert all(c["id"] != cid for c in client.get("/api/conversations?scope=all", headers=avi).json())
    avi_cid, _ = ask(client, avi, "Where is the storage key?")
    everyone = client.get("/api/conversations?scope=all", headers=admin).json()
    assert {cid, avi_cid} <= {c["id"] for c in everyone}
    assert client.get(f"/api/conversations/{avi_cid}", headers=admin).json()["mine"] is False
    assert client.delete(f"/api/conversations/{avi_cid}", headers=admin).status_code == 403

    # Delete your own
    assert client.delete(f"/api/conversations/{cid}", headers=admin).status_code == 200
    assert client.get(f"/api/conversations/{cid}", headers=admin).status_code == 404
