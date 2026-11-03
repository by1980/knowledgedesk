"""Asking must never break, and must respect what the person chose, whatever the AI's search plan says."""
import json

import pytest

from app import llm, retrieval
from app.connectors.base import Sync
from app.db import conn
from tests.test_oauth import login, process_pending

captured = {}


@pytest.fixture(autouse=True)
def fake_answers(monkeypatch):
    def fake_stream(question, sources_block, history):
        captured["sources"] = sources_block
        yield "ok [1]"

    monkeypatch.setattr(llm, "stream_answer", fake_stream)


@pytest.fixture(scope="module")
def data(client):
    with conn() as c:
        cid = c.execute("INSERT INTO connectors(type, name, config, company_wide) VALUES ('ics', 'Team calendar', '{}', true) RETURNING id").fetchone()["id"]
        s = Sync(c, c.execute("SELECT * FROM connectors WHERE id=%s", (cid,)).fetchone())
        s.upsert("cal-1", "Calendar: Team calendar – March 2026", "march.txt",
                 b"Quarterly planning meeting with the Northwind supplier on 12 March 2026 at 10:00.", "calendar",
                 {"day": "2026-03-12", "events": 1})
        s.upsert("mail-1", "Email: Northwind supplier invoice", "message.eml",
                 b"From: billing@northwind.test\nTo: yaniv@by1labs.com\nSubject: Northwind supplier invoice\n\nInvoice for the Northwind supplier order.",
                 "email", {"from": "billing@northwind.test", "to": "yaniv@by1labs.com", "date": "Tue, 10 Mar 2026 09:00:00 +0200"})
    process_pending()
    yield
    with conn() as c:  # leave the shared test database as we found it
        c.execute("DELETE FROM connectors WHERE id=%s", (cid,))


def _ask(client, headers, question, **extra):
    r = client.post("/api/ask", headers=headers, json={"question": question, **extra})
    assert r.status_code == 200, r.text
    events = [json.loads(x) for x in r.text.splitlines()]
    assert not any(e["type"] == "error" for e in events), events
    return next(e for e in events if e["type"] == "sources")["sources"]


@pytest.mark.parametrize("plan", [
    {"query": "Northwind", "keywords": ["Northwind"], "people": [], "direction": "any", "newest_first": True, "after": "last week", "before": "soon"},
    {"query": "Northwind", "keywords": [], "people": ["northwind"], "direction": "both", "newest_first": False, "kinds": ["document", "pdf"]},
    {"query": "", "keywords": [], "people": ["'; DROP TABLE documents; --", "a"], "direction": "sent", "newest_first": True},
    {"query": "Northwind", "keywords": ["c++", "50%", "!!!", "שלום"], "people": ["O'Brien & Sons"], "direction": "received", "newest_first": False},
])
def test_odd_search_plans_never_break_the_answer(client, data, monkeypatch, plan):
    monkeypatch.setattr(llm, "plan_search", lambda *a, **k: plan)
    sources = _ask(client, login(client), "What do we have about Northwind?")
    with conn() as c:
        assert c.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] > 0  # nothing was harmed


def test_search_in_choice_wins_over_the_ai(client, data, monkeypatch):
    monkeypatch.setattr(llm, "plan_search", lambda *a, **k: {"query": "Northwind invoice", "keywords": ["invoice"], "people": [],
                                                            "direction": "any", "newest_first": False, "kinds": ["email"]})
    sources = _ask(client, login(client), "Northwind?", kinds=["calendar"])
    assert sources and all(s["kind"] == "calendar" for s in sources), sources


def test_a_failing_planned_search_falls_back_to_plain_search(client, data, monkeypatch):
    monkeypatch.setattr(llm, "plan_search", lambda *a, **k: {"query": "Northwind", "keywords": [], "people": [], "direction": "any", "newest_first": True})

    def broken(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(retrieval, "_planned", broken)
    sources = _ask(client, login(client), "Northwind supplier")
    assert any("Northwind" in s["title"] for s in sources)


def test_ai_unavailable_still_answers_with_sources(client, data, monkeypatch):
    monkeypatch.setattr(llm, "plan_search", lambda *a, **k: None)
    sources = _ask(client, login(client), "When is the Northwind planning meeting?")
    assert any(s["kind"] == "calendar" for s in sources[:3]), sources


def test_hebrew_question_and_follow_up(client, data, monkeypatch):
    monkeypatch.setattr(llm, "plan_search", lambda *a, **k: None)
    admin = login(client)
    first = client.post("/api/ask", headers=admin, json={"question": "מתי הפגישה עם Northwind?"})
    cid = next(json.loads(x)["id"] for x in first.text.splitlines() if json.loads(x)["type"] == "conversation")
    sources = _ask(client, admin, "and the invoice?", conversation_id=cid,
                   history=[{"role": "user", "content": "מתי הפגישה עם Northwind?"}, {"role": "assistant", "content": "On 12 March [1]"}])
    assert any("Northwind" in s["title"] for s in sources), sources


def test_empty_and_huge_questions(client, data):
    admin = login(client)
    assert client.post("/api/ask", headers=admin, json={"question": "   "}).status_code == 400
    r = client.post("/api/ask", headers=admin, json={"question": "Northwind " * 3000})
    assert r.status_code == 200 and '"type": "error"' not in r.text
