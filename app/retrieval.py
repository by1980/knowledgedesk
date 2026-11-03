"""Hybrid search (meaning + keywords) restricted to the documents a user is allowed to see.

Permission filtering happens here in SQL, before anything reaches the language model, so a user
can never get an answer built from another department's files.
"""
import re
from html import escape

from . import embeddings
from .config import settings
from .db import conn

_ACCESS = """
    d.status = 'ready' AND (
        CASE WHEN d.private THEN d.owner_id = %(me)s AND NOT %(learner)s
        WHEN d.allowed_emails IS NOT NULL THEN lower(%(me_email)s) = ANY(d.allowed_emails) AND NOT %(learner)s
        WHEN %(learner)s THEN d.id IN (SELECT document_id FROM learning_items)
        ELSE (
            %(is_admin)s
            OR d.company_wide
            OR EXISTS (
                SELECT 1 FROM document_departments dd
                WHERE dd.document_id = d.id AND dd.department_id = ANY(%(user_depts)s)
            )
            OR d.id IN (SELECT document_id FROM learning_items)
        ) END
    )
    AND (%(kinds)s::text[] IS NULL OR d.kind = ANY(%(kinds)s))
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
    WHERE {_ACCESS} AND {{extra}}
    ORDER BY c.embedding <=> %(qvec)s
    LIMIT 40
),
kw AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q) + 0.5 * ts_rank_cd(d.meta_tsv, q) DESC) AS r
    FROM chunks c JOIN documents d ON d.id = c.document_id,
         to_tsquery('simple', %(tsq)s) q
    WHERE %(tsq)s <> '' AND (c.tsv @@ q OR d.meta_tsv @@ q) AND {_ACCESS} AND {{extra}}
    ORDER BY ts_rank_cd(c.tsv, q) + 0.5 * ts_rank_cd(d.meta_tsv, q) DESC
    LIMIT 40
),
fused AS (
    SELECT id, sum(1.0 / (60 + r)) AS score
    FROM (SELECT * FROM vec UNION ALL SELECT * FROM kw) s
    GROUP BY id
)
SELECT c.id, c.content, c.location, c.document_id, d.title, d.filename, d.kind,
       d.metadata, d.updated_at, d.doc_date, d.private, f.score
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


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 1 and w not in _STOP]


def _keyword_query(text: str) -> str:
    # OR the words together; ranking rewards chunks that contain more of them. Longer words also match
    # as a prefix, so "proposal" finds "proposals".
    words = [w + ":*" if len(w) >= 4 else w for w in dict.fromkeys(_words(text))]
    return " | ".join(words)[:3000]


def _names_query(names: list[str]) -> str:
    """People, companies, addresses: each name must match as a whole ("cut inside" = cut AND inside),
    and the joined spelling counts too ("cutinside")."""
    alts = []
    for name in names:
        ws = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 1]
        if not ws:
            continue
        alts.append("(" + " & ".join(w + ":*" for w in ws) + ")")
        if len(ws) > 1:
            alts.append("".join(ws) + ":*")
    return " | ".join(dict.fromkeys(alts))[:2000]


def search(question: str, *, is_admin: bool, user_depts: list[int], only_dept: int | None = None,
           include_company: bool = True, k: int | None = None, kinds: list[str] | None = None,
           learner: bool = False, plan: dict | None = None, mine: list[str] | None = None,
           own_site: str = "", user_id: int = 0, user_email: str = "") -> list[dict]:
    """Find the passages that answer the question. With a search plan (from the AI: who, when, which way,
    newest first...) several targeted searches run and their results are combined."""
    params = {
        "learner": learner,
        "me": user_id,
        "me_email": user_email,
        "qvec": embeddings.embed_query(question),
        "tsq": _keyword_query(question),
        "is_admin": is_admin,
        "user_depts": user_depts,
        "only_dept": only_dept,
        "include_company": include_company,
        "kinds": kinds or None,
        "k": k or settings.top_k,
        "_question": question,
        "_own_site": own_site,
    }
    with conn() as c:
        hits = _run(c, params, plan, mine)
        if _SOCIAL.search(question) and (not kinds or "social" in kinds):
            # Questions about posts, likes or followers: always include the account summaries,
            # which hold the latest post and week-by-week numbers.
            named = [name for word, name in _PLATFORMS.items() if re.search(word, question, re.I)]
            summaries = c.execute(_SUMMARIES.format(access=_ACCESS), {**params, "named": named}).fetchall()
            have = {h["id"] for h in summaries}
            hits = (summaries + [h for h in hits if h["id"] not in have])[:params["k"]]
        return hits


_SOCIAL = re.compile(r"\b(instagram|insta|facebook|fb|youtube|linkedin|social|posts?|likes?|followers?|subscribers?|views|"
                     r"reels?|videos?|engagement)\b|אינסטגרם|פייסבוק|יוטיוב|לינקדאין|פוסט|לייק|עוקבים|צפיות|סרטון|רשתות", re.I)
# The platform a question names comes first ("our last Instagram post" → the Instagram summary).
_PLATFORMS = {r"instagram|\binsta\b|אינסטגרם": "Instagram", r"facebook|\bfb\b|פייסבוק": "Facebook",
              r"youtube|יוטיוב": "YouTube", r"linkedin|לינקדאין": "LinkedIn"}
_SUMMARIES = """
SELECT c.id, c.content, c.location, c.document_id, d.title, d.filename, d.kind, d.metadata, d.updated_at, d.doc_date, d.private, 1.0 AS score
FROM chunks c JOIN documents d ON d.id = c.document_id
WHERE d.kind = 'social' AND d.metadata->>'summary' = 'true' AND {access}
ORDER BY (d.metadata->>'platform' = ANY(%(named)s::text[])) DESC, d.updated_at DESC, c.id
LIMIT 6
"""


def fuse(primary: list[dict], secondary: list[dict], k: int | None = None) -> list[dict]:
    """Merge two result lists; the first list counts more."""
    k = k or settings.top_k
    score: dict[int, float] = {}
    by_id: dict[int, dict] = {}
    for weight, hits in ((1.0, primary), (0.6, secondary)):
        for rank, h in enumerate(hits):
            score[h["id"]] = score.get(h["id"], 0) + weight / (10 + rank)
            by_id.setdefault(h["id"], h)
    return [by_id[i] for i in sorted(score, key=score.get, reverse=True)[:k]]


def format_sources(hits: list[dict]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        where = f", {h['location']}" if h["location"] else ""
        kind = {"interview": " (employee interview)", "email": " (email)", "whatsapp": " (WhatsApp chat)",
                "calendar": " (calendar)", "web": " (website)", "slack": " (Slack)",
                "social": " (social media)"}.get(h["kind"], "")
        when = h.get("doc_date") or h.get("updated_at")
        date = when.date().isoformat() if when else ""
        meta = h.get("metadata") or {}
        people = "".join(f' {k}="{escape(str(meta[k])[:200])}"' for k in ("from", "to", "cc") if meta.get(k))
        blocks.append(
            f'<source id="{i}" title="{escape(h["title"])}{escape(where)}{kind}" date="{date}"{people}>\n{h["content"]}\n</source>'
        )
    return "\n".join(blocks)


_SQL_PARTS = _SQL


KNOWN_KINDS = {"email", "file", "calendar", "slack", "whatsapp", "web", "interview", "social"}


def _is_date(value) -> bool:
    """The AI should give YYYY-MM-DD; anything else ("last week") is ignored rather than breaking the search."""
    from datetime import date

    try:
        date.fromisoformat(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _filters(plan: dict | None, mine: list[str] | None, with_people: bool = True) -> tuple[str, dict]:
    parts, params = [], {}
    if not plan:
        return "TRUE", params
    names = _names_query(plan.get("people") or [])
    direction = plan.get("direction")
    if with_people and names:
        params["names"] = names
        if direction in ("sent", "received"):
            # A clear direction: the name must be the actual sender (received) or a recipient (sent),
            # not just mentioned somewhere in the text.
            field = "coalesce(d.metadata->>'from', '')" if direction == "received" else \
                "coalesce(d.metadata->>'to', '') || ' ' || coalesce(d.metadata->>'cc', '')"
            parts.append(f"to_tsvector('simple', regexp_replace({field}, '[@._/:+=<>(),;|*#&~-]+', ' ', 'g')) @@ to_tsquery('simple', %(names)s)")
        else:
            parts.append("(d.meta_tsv @@ to_tsquery('simple', %(names)s) OR EXISTS (SELECT 1 FROM chunks cn "
                         "WHERE cn.document_id = d.id AND cn.tsv @@ to_tsquery('simple', %(names)s)))")
    if direction in ("sent", "received") and mine:
        params["mine"] = [f"%{m}%" for m in mine]
        parts.append("coalesce(d.metadata->>'from', '') ILIKE ANY(%(mine)s)" if direction == "sent"
                     else "(d.kind = 'email' AND NOT coalesce(d.metadata->>'from', '') ILIKE ANY(%(mine)s))")
    for key, op in (("after", ">="), ("before", "<")):
        if _is_date(plan.get(key)):
            parts.append(f"coalesce(d.doc_date, d.updated_at) {op} %({key})s::date")
            params[key] = plan[key]
    return (" AND ".join(parts) or "TRUE"), params


_NEWEST = """
WITH docs AS (
    SELECT d.id, coalesce(d.doc_date, d.updated_at) AS at,
           (%(topic)s <> '' AND (d.meta_tsv @@ to_tsquery('simple', %(topic)s) OR EXISTS (SELECT 1 FROM chunks ct
             WHERE ct.document_id = d.id AND ct.tsv @@ to_tsquery('simple', %(topic)s)))) AS on_topic
    FROM documents d
    WHERE {access} AND {extra}
    ORDER BY on_topic DESC, at DESC NULLS LAST
    LIMIT 8
)
SELECT DISTINCT ON (c.document_id) c.id, c.content, c.location, c.document_id, d.title, d.filename, d.kind,
       d.metadata, d.updated_at, d.doc_date, d.private, 1.0 AS score, docs.on_topic, docs.at
FROM chunks c JOIN documents d ON d.id = c.document_id JOIN docs ON docs.id = d.id
ORDER BY c.document_id,
         (CASE WHEN %(topic)s <> '' THEN ts_rank_cd(c.tsv, to_tsquery('simple', %(topic)s)) ELSE 0 END) DESC, c.position
"""


# "What services do WE offer?": questions about the company itself
_ABOUT_US = re.compile(r"(?i:\b(we|our|ours)\b)|\bus\b|אנחנו|שלנו|אנו|החברה שלנו|\bלנו\b")


def _about_us(c, params: dict, text: str) -> list[dict]:
    """The company's own website pages (and its uploaded documents) that match, so "we" is never answered from
    other companies' emails."""
    site = params.get("_own_site") or ""
    extra = "(d.kind = 'web' AND (%(own)s = '' OR d.metadata->>'url' ILIKE %(own_like)s) OR d.source IN ('upload', 'folder'))"
    p = {**params, "own": site, "own_like": f"%{site}%", "qvec": embeddings.embed_query(text), "tsq": _keyword_query(text)}
    return c.execute(_SQL_PARTS.format(extra=extra), p).fetchall()


def _run(c, params: dict, plan: dict | None, mine: list[str] | None) -> list[dict]:
    base = c.execute(_SQL_PARTS.format(extra="TRUE"), params).fetchall()
    about = plan.get("about_us") if plan else bool(_ABOUT_US.search(params["_question"]))
    ours = []
    if about and params.get("kinds") is None:
        # our own pages lead; everything else fills in behind them
        try:
            ours = fuse_many([(1.0, _about_us(c, params, (plan or {}).get("query") or params["_question"]))], 5)
        except Exception:  # noqa: BLE001
            c.rollback()
    try:
        found = _planned(c, params, plan, mine, base) if plan else base
    except Exception:  # noqa: BLE001 - fall back to the plain search rather than failing the question
        import logging

        logging.getLogger("search").exception("Planned search failed; using the plain search")
        c.rollback()
        found = base
    if not ours:
        return found
    seen = {h["id"] for h in ours}
    return (ours + [h for h in found if h["id"] not in seen])[: params["k"] + (4 if plan else 0)]


def _planned(c, params: dict, plan: dict, mine: list[str] | None, base: list[dict]) -> list[dict]:
    text = " ".join([plan.get("query") or ""] + list(plan.get("keywords") or []))
    words = " ".join([params["_question"], text] + list(plan.get("people") or []))
    p2 = {**params, "qvec": embeddings.embed_query(text or params["_question"]), "tsq": _keyword_query(words)}
    planned_kinds = [k for k in (plan.get("kinds") or []) if k in KNOWN_KINDS]
    if planned_kinds and params.get("kinds") is None:   # the person's own "Search in" choice always wins
        p2["kinds"] = planned_kinds
    lists = [(1.0, base)]
    extra, fp = _filters(plan, mine)
    if extra != "TRUE":
        lists.append((1.4, c.execute(_SQL_PARTS.format(extra=extra), {**p2, **fp}).fetchall()))
    loose, lp = _filters(plan, mine, with_people=False)
    lists.append((1.0, c.execute(_SQL_PARTS.format(extra=loose), {**p2, **lp}).fetchall()))
    front = []
    if plan.get("newest_first") or extra != "TRUE":
        topic = _keyword_query(" ".join(plan.get("keywords") or []))
        newest = c.execute(_NEWEST.format(access=_ACCESS, extra=extra), {**p2, **fp, "topic": topic}).fetchall()
        newest.sort(key=lambda h: (bool(h["on_topic"]), h["at"]), reverse=True)
        lists.insert(0, (1.8, newest))
        if plan.get("newest_first") and newest:
            # "the last / latest ...": the matching items lead, newest first, ahead of everything else
            on_topic = [h for h in newest if h["on_topic"]]
            front = (on_topic or newest)[:4]
    fused = fuse_many(lists, params["k"] + 4)
    if not front:
        return fused
    lead = {h["document_id"] for h in front}
    return front + [h for h in fused if h["document_id"] not in lead][: params["k"] + 4 - len(front)]


def fuse_many(lists: list[tuple[float, list[dict]]], k: int, per_document: int = 3) -> list[dict]:
    """Combine ranked lists (earlier ranks and heavier lists count more); at most a few passages per item,
    so one long document can't push everything else out."""
    score: dict[int, float] = {}
    by_id: dict[int, dict] = {}
    for weight, hits in lists:
        for rank, h in enumerate(hits):
            score[h["id"]] = score.get(h["id"], 0) + weight / (8 + rank)
            by_id.setdefault(h["id"], h)
    out, per_doc = [], {}
    for cid in sorted(score, key=score.get, reverse=True):
        h = by_id[cid]
        if per_doc.get(h["document_id"], 0) >= per_document:
            continue
        per_doc[h["document_id"]] = per_doc.get(h["document_id"], 0) + 1
        out.append(h)
        if len(out) >= k:
            break
    return out
