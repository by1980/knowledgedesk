"""Finding a specific item in a busy mailbox: "the last proposal I sent to cutinside".

Recreates a real miss: many emails mention Cut Inside (call scripts, notifications), the proposal itself went to
dima@cutinside.com with a Hebrew subject and the proposal as an attachment. Cortex must put it first."""
import json
from email.message import EmailMessage

import pytest

from app import llm
from app.connectors.base import Sync
from app.connectors.email_imap import store_message
from app.db import conn
from tests.test_oauth import _docx, login, process_pending

captured = {}
PLAN = {"query": "the most recent proposal I sent to Cut Inside", "keywords": ["proposal", "quote", "offer", "הצעה", "הצעת מחיר"],
        "people": ["cutinside", "cut inside"], "direction": "sent", "newest_first": True}


def _email(frm, to, subject, body, date, attachment=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = frm, to, subject, date
    m.set_content(body)
    if attachment:
        name, data = attachment
        m.add_attachment(data, maintype="application", subtype="vnd.openxmlformats-officedocument.wordprocessingml.document", filename=name)
    return m.as_bytes()


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    def fake_stream(question, sources_block, history):
        captured["sources"] = sources_block
        yield "ok [1]"

    monkeypatch.setattr(llm, "stream_answer", fake_stream)


@pytest.fixture(scope="module")
def mailbox(client):
    with conn() as c:
        cid = c.execute("INSERT INTO connectors(type, name, config, company_wide) VALUES ('imap', 'Yaniv mailbox', %s, true) RETURNING id",
                        (json.dumps({"username": "yaniv@by1labs.com"}),)).fetchone()["id"]
        connector = c.execute("SELECT * FROM connectors WHERE id=%s", (cid,)).fetchone()
        s = Sync(c, connector)
        for i in range(30):  # the noise that used to win
            store_message(s, f"noise{i}", _email("notifications@calendly.com", "yaniv@by1labs.com", f"Cut Inside call script {i}",
                          f"Cut Inside sales call script number {i}. Talk about Cut Inside services and pricing questions.",
                          f"Mon, {i % 28 + 1:02d} Sep 2025 10:00:00 +0300"), "INBOX")
        store_message(s, "old", _email("Yaniv Barak <yaniv@by1labs.com>", "dima@cutinside.com", "הצעת מחיר ראשונה",
                      "היי דימה, מצורפת הצעת מחיר ראשונה לפרויקט.", "Tue, 10 Jun 2025 09:00:00 +0300"), "Sent")
        store_message(s, "latest", _email("Yaniv Barak <yaniv@by1labs.com>", "Dima <dima@cutinside.com>", "הצעת מחיר - Operad",
                      "היי דימה, כמו שסיכמנו, מצורפת ההצעה המעודכנת. נשמח להתחיל בספטמבר.", "Tue, 12 Aug 2025 11:30:00 +0300",
                      ("Operad proposal.docx", _docx("Proposal for Cut Inside: Operad AI agents, monthly retainer 12,000 NIS."))), "Sent")
        store_message(s, "reply", _email("Dima <dima@cutinside.com>", "yaniv@by1labs.com", "Re: meeting",
                      "Thanks Yaniv, let's talk next week.", "Thu, 07 Aug 2025 15:00:00 +0300"), "INBOX")
    process_pending()
    return cid


def _ask(client, admin, question):
    r = client.post("/api/ask", headers=admin, json={"question": question})
    events = [json.loads(x) for x in r.text.splitlines()]
    return next(e for e in events if e["type"] == "sources")["sources"]


def test_addresses_are_searchable_words(client, mailbox, monkeypatch):
    """Even without the AI's plan: "cutinside" now matches the recipient address dima@cutinside.com."""
    monkeypatch.setattr(llm, "plan_search", lambda q, h: None)
    admin = login(client)
    titles = [s["title"] for s in _ask(client, admin, "proposal sent to cutinside")]
    assert any("Operad" in t for t in titles), titles


def test_last_proposal_sent_to_cutinside_comes_first(client, mailbox, monkeypatch):
    monkeypatch.setattr(llm, "plan_search", lambda q, h: PLAN)
    admin = login(client)
    sources = _ask(client, admin, "find me the last proposal I sent to cutinside")
    titles = [s["title"] for s in sources]
    first_three = " | ".join(titles[:3])
    assert "Operad" in first_three, titles
    # newest first: the August proposal before the June one
    latest = next(i for i, t in enumerate(titles) if "Operad" in t)
    older = next((i for i, t in enumerate(titles) if "ראשונה" in t), 99)
    assert latest < older, titles
    # the AI sees the real date and who it was sent to
    assert 'date="2025-08-12"' in captured["sources"] and 'to="Dima &lt;dima@cutinside.com&gt;"' in captured["sources"]
    # the attachment knows its email's recipient, so it is found too
    assert any("Operad proposal" in t for t in titles), titles


def test_received_vs_sent(client, mailbox, monkeypatch):
    monkeypatch.setattr(llm, "plan_search", lambda q, h: {**PLAN, "query": "latest email from Cut Inside", "keywords": [],
                                                         "direction": "received"})
    admin = login(client)
    titles = [s["title"] for s in _ask(client, admin, "what was the last email Dima from cutinside sent me?")]
    assert titles[0] == "Email: Re: meeting", titles
