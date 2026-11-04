"""The memory of dates: contracts, renewals, notice deadlines, payments and promises, shown ahead of time."""
from datetime import date, timedelta

import pytest

from app import dates, llm
from app.config import settings
from app.db import conn
from tests.test_oauth import login, process_pending
from tests.test_privacy import _source, _user


def _doc(title):
    with conn() as c:
        return c.execute("SELECT id FROM documents WHERE title=%s", (title,)).fetchone()["id"]


@pytest.fixture(scope="module")
def office(client):
    admin = login(client)
    sales = client.post("/api/departments", headers=admin, json={"name": "Sales dates"}).json()["id"]
    lior, lior_id = _user(client, admin, "lior@acme.test", depts=[])
    renew_on = date.today() + timedelta(days=20)
    with conn() as c:
        admin_id = c.execute("SELECT id FROM users WHERE email='admin@test.local'").fetchone()["id"]
    _source(admin_id, "file", "Quillon hosting agreement",
            f"Quillon hosting agreement. This agreement renews automatically on {renew_on:%d %B %Y} unless cancelled in writing.",
            depts=[sales], ctype="gdrive")
    _source(admin_id, "file", "Quillon office lease",
            f"Office lease with Quillon Properties. The lease ends on {date.today() + timedelta(days=200):%d %B %Y}.",
            company_wide=True, ctype="gdrive")
    process_pending()
    yield {"admin": admin, "lior": lior, "renew_on": renew_on, "lior_id": lior_id}
    with conn() as c:
        c.execute("DELETE FROM connectors WHERE name LIKE 'Quillon%%'")
        c.execute("DELETE FROM users WHERE id=%s", (lior_id,))
        c.execute("DELETE FROM departments WHERE id=%s", (sales,))


def test_patterns_find_common_dates():
    ref = date(2026, 11, 4)
    found = dates.heuristic("X", "The plan renews automatically on 1 December. Invoice 1042 payment is due on 2026-11-20.", ref)
    assert {(f["kind"], f["due"]) for f in found} == {("renewal", "2026-12-01"), ("payment", "2026-11-20")}
    # a month already passed means next year
    assert dates.heuristic("X", "The licence expires on 10 February.", ref)[0]["due"] == "2027-02-10"


def test_documents_are_read_and_shown_ahead_of_time(client, office, monkeypatch):
    monkeypatch.setattr(settings, "fake_llm", True)
    assert dates.check(_doc("Quillon hosting agreement")) == 1
    dates.check(_doc("Quillon office lease"))
    today = client.get("/api/today", headers=office["admin"]).json()
    titles = [d["doc_title"] for d in today["dates"] if "doc_title" in d]
    assert "Quillon hosting agreement" in titles                     # 20 days ahead: inside the 45 day window
    assert "Quillon office lease" not in titles                     # 200 days ahead: not yet
    item = next(d for d in today["dates"] if d.get("doc_title") == "Quillon hosting agreement")
    assert item["kind"] == "renewal" and item["days"] == 20 and item["due"] == office["renew_on"].isoformat()
    # only people who may see the document see its dates
    assert "Quillon hosting agreement" not in str(client.get("/api/today", headers=office["lior"]).json()["dates"])
    # Done hides it
    assert client.post(f"/api/today/alerts/{item['key']}", headers=office["admin"], json={"action": "done"}).json()["ok"]
    assert "Quillon hosting agreement" not in str(client.get("/api/today", headers=office["admin"]).json()["dates"])


def test_a_changed_document_is_read_again(client, office, monkeypatch):
    monkeypatch.setattr(settings, "fake_llm", True)
    doc = _doc("Quillon office lease")
    with conn() as c:
        assert c.execute("SELECT dates_checked FROM documents WHERE id=%s", (doc,)).fetchone()["dates_checked"] == dates.VERSION
    from app.ingest.pipeline import process_document

    process_document(doc)
    with conn() as c:
        assert c.execute("SELECT dates_checked FROM documents WHERE id=%s", (doc,)).fetchone()["dates_checked"] is None


def test_ai_results_are_checked(client, office, monkeypatch):
    monkeypatch.setattr(settings, "fake_llm", False)
    monkeypatch.setattr(llm, "extract_dates", lambda text: [
        {"kind": "notice", "title": "Last day to cancel Quillon hosting", "due": (date.today() + timedelta(days=5)).isoformat(), "quote": "x"},
        {"kind": "party", "title": "Not a kind", "due": "2026-12-01", "quote": "x"},
        {"kind": "payment", "title": "No date", "due": "soon", "quote": "x"},
        {"kind": "deadline", "title": "", "due": "2026-12-01", "quote": "x"},
    ])
    assert dates.check(_doc("Quillon hosting agreement")) == 1
    with conn() as c:
        rows = c.execute("SELECT kind, title FROM commitments WHERE document_id=%s", (_doc("Quillon hosting agreement"),)).fetchall()
    assert [(r["kind"], r["title"]) for r in rows] == [("notice", "Last day to cancel Quillon hosting")]


def test_company_rhythms_for_admins():
    admin, member = {"role": "admin"}, {"role": "member"}
    assert dates.rhythms(admin, date(2026, 12, 10))[0]["title"] == "The year ends in 21 days"
    assert dates.rhythms(admin, date(2026, 9, 25))[0]["title"].startswith("Quarter 3 ends in 5 days")
    assert dates.rhythms(admin, date(2026, 11, 28))[0]["title"] == "The month ends in 2 days"
    assert dates.rhythms(admin, date(2026, 11, 4)) == []
    assert dates.rhythms(member, date(2026, 12, 10)) == []
    keys = {dates.rhythms(admin, d)[0]["key"] for d in (date(2026, 12, 10), date(2026, 9, 25), date(2026, 11, 28))}
    assert all(len(k) == 16 for k in keys) and len(keys) == 3
