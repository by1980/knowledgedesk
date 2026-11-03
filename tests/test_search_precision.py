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


def _pdf(text: str) -> bytes:
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    s = DecodedStreamObject()
    s.set_data(f"BT /F1 14 Tf 72 700 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = w._add_object(s)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def _apple_mail(subject, body, date, pdf_name, pdf_bytes):
    """How Apple Mail sends a PDF: inside the message layout (alternative > mixed > html + pdf), not at the top level."""
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = "Yaniv Barak <yaniv@by1labs.com>", "Dima Binkovsky <dima@operad.com>", subject, date
    m.set_content(body)
    m.add_alternative(f"<p>{body}</p>", subtype="html")
    html = m.get_payload()[1]
    html.make_mixed()
    html.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_name, disposition="inline")
    return m.as_bytes()


def test_pdf_inside_apple_mail_message_is_found_and_downloadable(client, mailbox, monkeypatch):
    raw = _apple_mail("Re: Call summery", "Please find the proposal & agreement attached.", "Wed, 04 Mar 2026 10:00:00 +0200",
                      "Proposal & Agreement - Operad.pdf", _pdf("Proposal and Agreement for Operad: 15,000 NIS per month"))
    with conn() as c:
        s = Sync(c, c.execute("SELECT * FROM connectors WHERE id=%s", (mailbox,)).fetchone())
        store_message(s, "apple-pdf", raw, "Sent")
    process_pending()
    monkeypatch.setattr(llm, "plan_search", lambda q, h: {"query": "latest PDF proposal sent to Dima", "keywords": ["proposal", "pdf", "הצעה"],
                                                         "people": ["dima", "operad"], "direction": "sent", "newest_first": True, "kinds": []})
    admin = login(client)
    sources = _ask(client, admin, "I want the PDF proposal I sent to Dima")
    pdf = next((s for s in sources if s["filename"].endswith(".pdf")), None)
    assert pdf and sources.index(pdf) < 3, [s["title"] for s in sources]
    assert "15,000 NIS" in captured["sources"]
    r = client.get(f"/api/documents/{pdf['document_id']}/file", headers=admin)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_rescan_adds_attachments_that_older_versions_missed(client, mailbox):
    from app.connectors import email_imap

    raw = _apple_mail("Signed agreement", "Signed copy attached.", "Thu, 05 Mar 2026 10:00:00 +0200",
                      "Signed agreement.pdf", _pdf("Signed agreement Operad"))
    with conn() as c:  # imported the old way: the email only, its PDF missed
        s = Sync(c, c.execute("SELECT * FROM connectors WHERE id=%s", (mailbox,)).fetchone())
        s.upsert("old-import", "Email: Signed agreement", "message.eml", raw, "email", {"from": "yaniv@by1labs.com", "folder": "Sent"})
        c.execute("DELETE FROM meta WHERE key='email_rescan'")
    assert email_imap.rescan_saved_emails() >= 1
    with conn() as c:
        att = c.execute("SELECT title, metadata FROM documents WHERE external_id='old-import:att:Signed agreement.pdf'").fetchone()
        email_doc = c.execute("SELECT metadata, status FROM documents WHERE external_id='old-import'").fetchone()
    assert att and "dima@operad.com" in att["metadata"]["to"]
    assert email_doc["metadata"]["attachments"] == "Signed agreement.pdf" and email_doc["status"] == "pending"
    assert email_imap.rescan_saved_emails() == 0  # only once


def test_found_email_brings_its_attachment(client, mailbox, monkeypatch):
    """The search matches only the email's own words; its attached PDF (about something else entirely) still comes
    right after it, ready to open or download."""
    raw = _apple_mail("Weekly sync notes", "Attaching the Lumenfield offer for your review.", "Fri, 06 Mar 2026 09:00:00 +0200",
                      "Zebra quarterly figures.pdf", _pdf("Zebra quarterly figures"))
    with conn() as c:
        sync = Sync(c, c.execute("SELECT * FROM connectors WHERE id=%s", (mailbox,)).fetchone())
        store_message(sync, "brings-att", raw, "Sent")
    process_pending()
    monkeypatch.setattr(llm, "plan_search", lambda q, h: None)
    admin = login(client)
    sources = _ask(client, admin, "Lumenfield offer for your review")
    i = next(i for i, x in enumerate(sources) if x["title"] == "Email: Weekly sync notes")
    nxt = sources[i + 1]
    assert nxt["filename"] == "Zebra quarterly figures.pdf" and nxt["email_subject"] == "Weekly sync notes", nxt
    assert nxt["date"].startswith("2026-03-06") and "dima@operad.com" in nxt["to"]
