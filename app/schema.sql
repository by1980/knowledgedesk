CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_departments (
    user_id       INT REFERENCES users(id) ON DELETE CASCADE,
    department_id INT REFERENCES departments(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, department_id)
);

-- kind: file | interview
-- source: upload | folder
-- status: pending | processing | ready | error
CREATE TABLE IF NOT EXISTS documents (
    id            SERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    filename      TEXT NOT NULL,
    path          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'file',
    source        TEXT NOT NULL DEFAULT 'upload',
    sha256        TEXT,
    company_wide  BOOLEAN NOT NULL DEFAULT FALSE,
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    uploaded_by   INT REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS documents_folder_path ON documents(path) WHERE source = 'folder';

CREATE TABLE IF NOT EXISTS document_departments (
    document_id   INT REFERENCES documents(id) ON DELETE CASCADE,
    department_id INT REFERENCES departments(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, department_id)
);

CREATE TABLE IF NOT EXISTS questions (
    id          SERIAL PRIMARY KEY,
    user_id     INT REFERENCES users(id) ON DELETE SET NULL,
    department_id INT REFERENCES departments(id) ON DELETE SET NULL,
    question    TEXT NOT NULL,
    answer      TEXT,
    sources     JSONB NOT NULL DEFAULT '[]'::jsonb,
    found_sources BOOLEAN NOT NULL DEFAULT TRUE,
    feedback    SMALLINT,   -- 1 = helpful, -1 = not helpful
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Connections to outside sources (email, calendar, website...). Added in v0.2.
-- type: imap | ics | website
CREATE TABLE IF NOT EXISTS connectors (
    id            SERIAL PRIMARY KEY,
    type          TEXT NOT NULL,
    name          TEXT NOT NULL,
    config        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- secrets inside are encrypted
    state         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- sync progress (e.g. last email id)
    company_wide  BOOLEAN NOT NULL DEFAULT FALSE,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    last_sync     TIMESTAMPTZ,
    last_error    TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS connector_departments (
    connector_id  INT REFERENCES connectors(id) ON DELETE CASCADE,
    department_id INT REFERENCES departments(id) ON DELETE CASCADE,
    PRIMARY KEY (connector_id, department_id)
);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS connector_id INT REFERENCES connectors(id) ON DELETE CASCADE;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS external_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS documents_connector_ext ON documents(connector_id, external_id)
    WHERE connector_id IS NOT NULL;
