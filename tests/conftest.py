"""Test setup: a real Postgres database, a small embedding model, and a fake Claude."""
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="kd-test-"))
os.environ.update({
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql://kd:kd@localhost:5432/kd_test"),
    "EMBED_MODEL": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "MODEL_CACHE": os.environ.get("MODEL_CACHE", str(Path.home() / ".cache" / "kd-models")),
    "DATA_DIR": str(_tmp),
    "SOURCES_DIR": str(_tmp / "sources"),
    "UPLOADS_DIR": str(_tmp / "uploads"),
    "LICENSE_FILE": str(_tmp / "license.key"),
    "RUN_WORKER": "false",
    "ANTHROPIC_API_KEY": "test-key",
    "SUMMARIZE_INTERVIEWS": "false",
    "ADMIN_EMAIL": "admin@test.local",
    "ADMIN_PASSWORD": "admin-pass-123",
    "SECRET_KEY": "test-secret-0123456789abcdef0123456789abcdef",
    "LICENSE_PUBLIC_KEY": "",
})

import psycopg  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def fresh_database():
    url = os.environ["DATABASE_URL"]
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    name = url.rsplit("/", 1)[1]
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"')
    yield


@pytest.fixture(scope="session")
def client(fresh_database):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def tmp_root():
    return _tmp
