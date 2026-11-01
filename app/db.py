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


def init_schema(embedding_dim: int) -> None:
    """Create tables. The chunks table depends on the embedding size of the chosen model."""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    # The vector extension must exist before register_vector can run, so use a plain connection first.
    import psycopg

    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute(sql)
        row = c.execute("SELECT value FROM meta WHERE key = 'embedding_dim'").fetchone()
        if row and int(row[0]) != embedding_dim:
            raise RuntimeError(
                f"The database was indexed with embeddings of size {row[0]}, but the current "
                f"EMBED_MODEL produces size {embedding_dim}. Run `python -m tools.reindex` "
                "after changing the embedding model."
            )
        c.execute(
            "INSERT INTO meta(key, value) VALUES ('embedding_dim', %s) ON CONFLICT (key) DO NOTHING",
            (str(embedding_dim),),
        )
        c.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chunks (
                id          BIGSERIAL PRIMARY KEY,
                document_id INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                position    INT NOT NULL,
                content     TEXT NOT NULL,
                location    TEXT,
                embedding   vector({embedding_dim}) NOT NULL,
                tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
            );
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(document_id);
            CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin(tsv);
            CREATE INDEX IF NOT EXISTS chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
            """
        )
