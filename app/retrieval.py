"""Hybrid search (meaning + keywords) restricted to the documents a user is allowed to see.

Permission filtering happens here in SQL, before anything reaches the language model, so a user
can never get an answer built from another department's files.
"""
import re

from . import embeddings
from .config import settings
from .db import conn

_ACCESS = """
    d.status = 'ready' AND (
        %(is_admin)s
        OR d.company_wide
        OR EXISTS (
            SELECT 1 FROM document_departments dd
            WHERE dd.document_id = d.id AND dd.department_id = ANY(%(user_depts)s)
        )
    )
    AND (
        %(only_dept)s::int IS NULL
        OR (%(include_company)s AND d.company_wide)
        OR EXISTS (
            SELECT 1 FROM document_departments dd2
            WHERE dd2.document_id = d.id AND dd2.department_id = %(only_dept)s
        )
    )
"""

_SQL = f"""
WITH vec AS (
    SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> %(qvec)s) AS r
    FROM chunks c JOIN documents d ON d.id = c.document_id
    WHERE {_ACCESS}
    ORDER BY c.embedding <=> %(qvec)s
    LIMIT 40
),
kw AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS r
    FROM chunks c JOIN documents d ON d.id = c.document_id,
         to_tsquery('simple', %(tsq)s) q
    WHERE %(tsq)s <> '' AND c.tsv @@ q AND {_ACCESS}
    ORDER BY ts_rank_cd(c.tsv, q) DESC
    LIMIT 40
),
fused AS (
    SELECT id, sum(1.0 / (60 + r)) AS score
    FROM (SELECT * FROM vec UNION ALL SELECT * FROM kw) s
    GROUP BY id
)
SELECT c.id, c.content, c.location, c.document_id, d.title, d.filename, d.kind,
       d.metadata, d.updated_at, f.score
FROM fused f
JOIN chunks c ON c.id = f.id
JOIN documents d ON d.id = c.document_id
ORDER BY f.score DESC
LIMIT %(k)s
"""

_STOP = {
    # tiny stop-word list; the vector search does the heavy lifting
    "the", "a", "an", "is", "are", "of", "to", "in", "on", "for", "and", "or", "what", "how", "who",
    "when", "where", "do", "does", "i", "we", "our", "my", "can", "with", "it", "this", "that",
    "את", "של", "על", "מה", "איך", "מי", "מתי", "איפה", "אני", "אנחנו", "זה", "הוא", "היא", "עם",
}


def _keyword_query(text: str) -> str:
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 1 and w not in _STOP]
    # OR the words together; ranking rewards chunks that contain more of them.
    return " | ".join(dict.fromkeys(words))[:2000]


def search(question: str, *, is_admin: bool, user_depts: list[int], only_dept: int | None = None,
           include_company: bool = True, k: int | None = None) -> list[dict]:
    params = {
        "qvec": embeddings.embed_query(question),
        "tsq": _keyword_query(question),
        "is_admin": is_admin,
        "user_depts": user_depts,
        "only_dept": only_dept,
        "include_company": include_company,
        "k": k or settings.top_k,
    }
    with conn() as c:
        return c.execute(_SQL, params).fetchall()


def format_sources(hits: list[dict]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        where = f", {h['location']}" if h["location"] else ""
        kind = " (employee interview)" if h["kind"] == "interview" else ""
        date = h["updated_at"].date().isoformat() if h.get("updated_at") else ""
        blocks.append(
            f'<source id="{i}" title="{h["title"]}{where}{kind}" updated="{date}">\n{h["content"]}\n</source>'
        )
    return "\n".join(blocks)
