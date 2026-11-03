"""Database connection pool and schema setup."""
from contextlib import contextmanager
from pathlib import Path

from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None


def _configure(conn):
    register_vector(conn)


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            configure=_configure,
            open=True,
        )
    return _pool


@contextmanager
def conn():
    with pool().connection() as c:
        yield c


# Words for keyword search: the text as is, plus addresses and links split apart, so "cutinside" finds
# "dima@cutinside.com" and "cutinside.co.il/proposal".
TSV_EXPR = "to_tsvector('simple', content || ' ' || regexp_replace(content, '[@._/:+=<>(),;|*#&~-]+', ' ', 'g'))"
TSV_VERSION = "2"


def init_schema(embedding_dim: int) -> None:
    """Create tables. The chunks table depends on the embedding size of the chosen model."""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    # The vector extension must exist before register_vector can run, so use a plain connection first.
    import psycopg

    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute(sql)
        row = c.execute("SELECT value FROM meta WHERE key = 'embedding_dim'").fetchone()
        model = c.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()
        changed = (row and int(row[0]) != embedding_dim) or (model and model[0] != settings.embed_model) \
            or (row and not model)
        if changed:
            # The search model changed: rebuild the search index from the stored files (automatic, in the background).
            import logging

            logging.getLogger("db").info("Search model changed to %s: re-indexing everything", settings.embed_model)
            c.execute("DROP TABLE IF EXISTS chunks")
            c.execute("UPDATE documents SET status='pending', error=NULL")
        c.execute("INSERT INTO meta(key, value) VALUES ('embedding_dim', %s) "
                  "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (str(embedding_dim),))
        c.execute("INSERT INTO meta(key, value) VALUES ('embedding_model', %s) "
                  "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (settings.embed_model,))
        c.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chunks (
                id          BIGSERIAL PRIMARY KEY,
                document_id INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                position    INT NOT NULL,
                content     TEXT NOT NULL,
                location    TEXT,
                embedding   vector({embedding_dim}) NOT NULL,
                tsv         tsvector GENERATED ALWAYS AS ({TSV_EXPR}) STORED
            );
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(document_id);
            CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin(tsv);
            CREATE INDEX IF NOT EXISTS chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
            """
        )
        version = c.execute("SELECT value FROM meta WHERE key = 'tsv_version'").fetchone()
        if not version or version[0] != TSV_VERSION:
            # Rebuild the keyword index of existing chunks (no re-reading of files needed).
            c.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS tsv")
            c.execute(f"ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS ({TSV_EXPR}) STORED")
            c.execute("CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin(tsv)")
            c.execute("INSERT INTO meta(key, value) VALUES ('tsv_version', %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                      (TSV_VERSION,))
        _backfill_dates(c)


def _backfill_dates(c) -> None:
    """Give items imported before v0.9.9 their real date (when the email was sent, the post published...)."""
    from .connectors.base import doc_date

    rows = c.execute("SELECT id, metadata FROM documents WHERE doc_date IS NULL AND metadata ?| array['date','posted','day','interview_date']").fetchall()
    for doc_id, meta in rows:
        when = doc_date(meta or {})
        if when:
            c.execute("UPDATE documents SET doc_date=%s WHERE id=%s", (when, doc_id))
