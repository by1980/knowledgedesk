"""Full flow: admin sets up departments/users/documents, employees ask questions.

The most important check: an employee never receives content from another department's documents.
"""
import json

import pytest

from app import llm
from app.ingest.pipeline import process_document

captured = {}


@pytest.fixture(autouse=True)
def fake_claude(monkeypatch):
    def fake_stream(question, sources_block, history):
        captured["sources"] = sources_block
        captured["history"] = history
        yield "Answer based on sources [1]."

    monkeypatch.setattr(llm, "stream_answer", fake_stream)


def login(client, email, password):
    r = client.post("/api/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def ask(client, headers, question, **extra):
    r = client.post("/api/ask", headers=headers, json={"question": question, **extra})
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines()]


def process_pending():
    from app.db import conn

    with conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM documents WHERE status='pending'").fetchall()]
    for i in ids:
        process_document(i)


@pytest.fixture(scope="module")
def setup(client):
    admin = login(client, "admin@test.local", "admin-pass-123")
    sales = client.post("/api/departments", headers=admin, json={"name": "Sales"}).json()["id"]
    hr = client.post("/api/departments", headers=admin, json={"name": "HR"}).json()["id"]

    for email, dept in [("sara@test.local", sales), ("hila@test.local", hr)]:
        r = client.post("/api/users", headers=admin, json={
            "email": email, "name": email.split("@")[0], "password": "password-123", "department_ids": [dept]})
        assert r.status_code == 200, r.text

    def upload(name, content, **form):
        r = client.post("/api/documents", headers=admin, files={"files": (name, content.encode(), "text/plain")},
                        data=form)
        assert r.status_code == 200, r.text
        return r.json()["created"][0]

    upload("discounts.txt", "Sales team discount policy: the maximum discount for repeat customers is 12 percent. "
                            "Discounts above 12 percent need approval from Yossi.", department_ids=str(sales))
    upload("salaries.txt", "Confidential salary table: junior technician salary is 9000 per month, "
                           "senior technician salary is 14000 per month.", department_ids=str(hr))
    upload("holidays.txt", "Office is closed on public holidays. Opening hours are 8:00 to 17:00.",
           company_wide="true")
    upload("interview.txt", "I handle the boiler repairs. The spare parts are kept in the back warehouse, "
                            "shelf C. The supplier for heating elements is Electra Parts.",
           department_ids=str(sales), kind="interview", interviewee="Moshe", role="Senior technician")
    process_pending()
    return {"admin": admin, "sales": sales, "hr": hr}


def test_documents_indexed(client, setup):
    docs = client.get("/api/documents", headers=setup["admin"]).json()
    assert {d["status"] for d in docs} == {"ready"}, docs
    interview = next(d for d in docs if d["kind"] == "interview")
    assert interview["title"] == "Interview - Moshe (Senior technician)"


def test_sales_user_never_sees_hr_documents(client, setup):
    sara = login(client, "sara@test.local", "password-123")
    events = ask(client, sara, "What is the salary of a senior technician?")
    titles = {s["title"] for s in events[0]["sources"]}
    assert "salaries" not in titles
    assert "14000" not in captured["sources"]
    # Sara's own and company-wide documents are searchable.
    events = ask(client, sara, "What is the maximum discount for repeat customers?")
    assert events[0]["sources"][0]["title"] == "discounts"
    assert "Answer based on sources" in "".join(e.get("text", "") for e in events)
    assert events[-1]["type"] == "done"


def test_hr_user_sees_hr_and_company_docs(client, setup):
    hila = login(client, "hila@test.local", "password-123")
    events = ask(client, hila, "senior technician salary")
    titles = [s["title"] for s in events[0]["sources"]]
    assert titles[0] == "salaries"
    assert "discounts" not in titles and "Interview - Moshe (Senior technician)" not in titles
    events = ask(client, hila, "What are the opening hours?")
    assert "holidays" in [s["title"] for s in events[0]["sources"]]


def test_department_filter_and_forbidden_department(client, setup):
    sara = login(client, "sara@test.local", "password-123")
    r = client.post("/api/ask", headers=sara, json={"question": "salary", "department_id": setup["hr"]})
    assert r.status_code == 403
    events = ask(client, sara, "where are spare parts kept?", department_id=setup["sales"])
    assert events[0]["sources"][0]["kind"] == "interview"
    assert "[Interview with Moshe, Senior technician]" in captured["sources"]


def test_hebrew_question(client, setup):
    admin = setup["admin"]
    client.post("/api/documents", headers=admin, data={"company_wide": "true"},
                files={"files": ("נהלים.txt", "נוהל החזרות: לקוח יכול להחזיר מוצר תוך 14 יום עם קבלה.".encode(), "text/plain")})
    process_pending()
    sara = login(client, "sara@test.local", "password-123")
    events = ask(client, sara, "תוך כמה ימים אפשר להחזיר מוצר?")
    assert "14 יום" in captured["sources"]
    assert events[0]["sources"][0]["title"] == "נהלים"


def test_follow_up_history_is_passed(client, setup):
    sara = login(client, "sara@test.local", "password-123")
    history = [{"role": "user", "content": "What is the max discount?"}, {"role": "assistant", "content": "12% [1]"}]
    ask(client, sara, "and who approves more?", history=history)
    assert captured["history"] == history


def test_feedback_and_admin_stats(client, setup):
    sara = login(client, "sara@test.local", "password-123")
    qid = ask(client, sara, "What is the maximum discount?")[-1]["question_id"]
    assert client.post(f"/api/questions/{qid}/feedback", headers=sara, json={"value": -1}).status_code == 200
    data = client.get("/api/admin/questions", headers=setup["admin"]).json()
    assert data["stats"]["not_helpful"] >= 1
    assert client.get("/api/admin/questions", headers=sara).status_code == 403


def test_file_download_permissions(client, setup):
    docs = client.get("/api/documents", headers=setup["admin"]).json()
    salaries = next(d for d in docs if d["title"] == "salaries")
    sara = login(client, "sara@test.local", "password-123")
    assert client.get(f"/api/documents/{salaries['id']}/file", headers=sara).status_code == 404
    hila = login(client, "hila@test.local", "password-123")
    assert client.get(f"/api/documents/{salaries['id']}/file", headers=hila).status_code == 200
    assert all(d["title"] != "salaries" for d in client.get("/api/documents", headers=sara).json())


def test_watched_folders(client, setup, tmp_root):
    from app.ingest import folders

    folder = tmp_root / "sources" / "Sales+Support"
    folder.mkdir(parents=True)
    f = folder / "warranty.md"
    f.write_text("Warranty on boilers is 3 years from installation date.")
    assert folders.scan() == 1
    assert folders.scan() == 0  # unchanged files are skipped
    process_pending()
    depts = {d["name"] for d in client.get("/api/departments", headers=setup["admin"]).json()}
    assert {"Sales", "Support"} <= depts

    sara = login(client, "sara@test.local", "password-123")
    events = ask(client, sara, "How long is the boiler warranty?")
    assert events[0]["sources"][0]["title"] == "warranty"

    f.unlink()
    folders.scan()
    docs = client.get("/api/documents", headers=setup["admin"]).json()
    assert all(d["title"] != "warranty" for d in docs)


def test_wrong_password_and_no_token(client):
    assert client.post("/api/login", json={"email": "admin@test.local", "password": "nope"}).status_code == 401
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 401
