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

-- Accounts connected with "Sign in with Google / Microsoft". Tokens are encrypted. Added in v0.3.
ALTER TABLE connectors ADD COLUMN IF NOT EXISTS last_note TEXT;

CREATE TABLE IF NOT EXISTS oauth_accounts (
    id         SERIAL PRIMARY KEY,
    provider   TEXT NOT NULL,
    email      TEXT NOT NULL DEFAULT '',
    tokens     TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- v0.9.3: Learning Center. "learner" = new employee who sees only the learning path.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin', 'member', 'learner'));

CREATE TABLE IF NOT EXISTS learning_items (
    id          SERIAL PRIMARY KEY,
    document_id INT UNIQUE NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position    INT NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS learning_progress (
    user_id     INT REFERENCES users(id) ON DELETE CASCADE,
    document_id INT REFERENCES documents(id) ON DELETE CASCADE,
    done_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, document_id)
);

-- v0.9.8: questions are grouped into conversations people can reopen and continue.
CREATE TABLE IF NOT EXISTS conversations (
    id          SERIAL PRIMARY KEY,
    user_id     INT REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE questions ADD COLUMN IF NOT EXISTS conversation_id INT REFERENCES conversations(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS questions_conversation ON questions(conversation_id);
CREATE INDEX IF NOT EXISTS conversations_user ON conversations(user_id, updated_at DESC);
-- Questions asked before conversations existed: one conversation each.
DO $$
DECLARE r RECORD; cid INT;
BEGIN
    FOR r IN SELECT id, user_id, question, created_at FROM questions WHERE conversation_id IS NULL LOOP
        INSERT INTO conversations(user_id, title, created_at, updated_at)
        VALUES (r.user_id, left(r.question, 80), r.created_at, r.created_at) RETURNING id INTO cid;
        UPDATE questions SET conversation_id = cid WHERE id = r.id;
    END LOOP;
END $$;

-- v0.9.9: precise search. The real date of each item (when an email was sent, a post published),
-- and its people and addresses as searchable words ("dima@cutinside.com" -> dima, cutinside, com).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_date TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS documents_doc_date ON documents(doc_date DESC NULLS LAST);
-- Attachments learn who their email was from and to.
UPDATE documents a SET metadata = a.metadata || jsonb_build_object('to', e.metadata->>'to', 'folder', e.metadata->>'folder')
  FROM documents e
 WHERE a.kind <> 'email' AND e.kind = 'email' AND a.connector_id = e.connector_id
   AND a.external_id LIKE e.external_id || ':att:%' AND NOT (a.metadata ? 'to');
ALTER TABLE documents ADD COLUMN IF NOT EXISTS meta_tsv tsvector GENERATED ALWAYS AS (
    to_tsvector('simple', regexp_replace(
        coalesce(title, '') || ' ' || coalesce(metadata->>'from', '') || ' ' || coalesce(metadata->>'to', '') || ' ' ||
        coalesce(metadata->>'cc', '') || ' ' || coalesce(metadata->>'email_subject', '') || ' ' ||
        coalesce(metadata->>'attachments', '') || ' ' || coalesce(metadata->>'account', '') || ' ' || coalesce(filename, ''),
        '[@._/:+=<>(),;|*#&~-]+', ' ', 'g'))) STORED;
CREATE INDEX IF NOT EXISTS documents_meta_tsv ON documents USING gin(meta_tsv);
