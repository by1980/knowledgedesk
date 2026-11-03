"""Learning Center: new employees (role "learner") see and search only the learning path."""
import json

import pytest

from app import llm
from tests.test_oauth import login, process_pending

captured = {}


@pytest.fixture(autouse=True)
def fake_answers(monkeypatch):
    def fake_stream(question, sources_block, history):
        captured["sources"] = sources_block
        yield "ok [1]"

    monkeypatch.setattr(llm, "stream_answer", fake_stream)


def upload(client, admin, name, text, **vis):
    r = client.post("/api/documents", headers=admin, data={"company_wide": "true", **vis},
                    files={"files": (name, text.encode())})
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def test_new_employee_sees_only_the_learning_path(client):
    admin = login(client)
    welcome = upload(client, admin, "Welcome guide.txt", "Welcome to Acme. Your first week: shadow a senior technician on the Haifa route.")
    secret = upload(client, admin, "Board minutes.txt", "Board decided the Haifa route bonus is 900 NIS per technician.")
    process_pending()

    # The path, in order, with a note
    assert client.post("/api/learning", headers=admin, json={"document_id": welcome, "note": "Start here"}).status_code == 200
    assert client.post("/api/learning", headers=admin, json={"document_id": welcome}).status_code == 200  # no duplicate
    path = client.get("/api/learning", headers=admin).json()
    assert path["total"] == 1 and path["items"][0]["note"] == "Start here"

    r = client.post("/api/users", headers=admin, json={"name": "Noa New", "email": "noa@test.local", "password": "noa-pass-123",
                                                       "role": "learner", "active": True, "department_ids": []})
    assert r.status_code == 200, r.text
    tok = client.post("/api/login", json={"email": "noa@test.local", "password": "noa-pass-123"}).json()["token"]
    noa = {"Authorization": f"Bearer {tok}"}

    titles = [d["title"] for d in client.get("/api/documents", headers=noa).json()]
    assert titles == ["Welcome guide"]
    assert client.get(f"/api/documents/{secret}/file", headers=noa).status_code == 404
    assert client.get(f"/api/documents/{welcome}/file", headers=noa).status_code == 200
    assert client.get("/api/overview", headers=noa).json()["total"] == 1

    lines = [json.loads(x) for x in client.post("/api/ask", headers=noa, json={"question": "What is the Haifa route bonus?"}).text.splitlines()]
    srcs = next(x for x in lines if x["type"] == "sources")["sources"]
    assert srcs and all(s["document_id"] == welcome for s in srcs)
    assert "900 NIS" not in captured.get("sources", "")

    # Admin pages stay closed
    assert client.get("/api/connectors", headers=noa).status_code == 403
    assert client.post("/api/learning", headers=noa, json={"document_id": secret}).status_code == 403

    # Progress
    item = path["items"][0]["id"]
    assert client.post(f"/api/learning/{item}/done", headers=noa, json={"done": True}).status_code == 200
    assert client.get("/api/learning", headers=noa).json()["done"] == 1
    people = client.get("/api/learning/progress", headers=admin).json()
    noa_row = next(p for p in people["people"] if p["email"] == "noa@test.local")
    assert noa_row["done"] == 1 and people["total"] == 1

    # Removing from the path takes it away from the new employee too
    client.delete(f"/api/learning/{item}", headers=admin)
    assert client.get("/api/documents", headers=noa).json() == []
