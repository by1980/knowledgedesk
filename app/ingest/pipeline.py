"""Process documents: extract text -> (interview notes) -> chunk -> embed -> store."""
import hashlib
import json
import logging
from pathlib import Path

from .. import embeddings, llm
from ..config import settings
from ..db import conn
from . import chunk
from .extract import extract, is_audio_video

log = logging.getLogger("ingest")
MAX_PIECES = 400  # about 600,000 characters per item


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _interviewee(meta: dict) -> str:
    parts = [meta.get("interviewee"), meta.get("role"), meta.get("interview_date")]
    return ", ".join(p for p in parts if p) or "an employee"


def process_document(doc_id: int) -> None:
    with conn() as c:
        doc = c.execute("SELECT * FROM documents WHERE id = %s", (doc_id,)).fetchone()
        if not doc:
            return
        c.execute("UPDATE documents SET status='processing', error=NULL WHERE id=%s", (doc_id,))

    try:
        path = Path(doc["path"])
        meta = doc["metadata"] or {}
        sections = extract(path)

        if doc["kind"] == "interview" or is_audio_video(path):
            who = _interviewee(meta)
            # Label the transcript so answers can say whose account it is.
            sections = [(f"[Interview with {who}] {t}", f"at {loc}" if loc else "") for t, loc in sections]
            if settings.summarize_interviews and llm.api_key():
                transcript = "\n".join(f"[{loc}] {t}" for t, loc in sections)
                notes = llm.interview_notes(transcript, who)
                if notes:
                    sections.insert(0, (f"Interview notes - {who}\n\n{notes}", "interview notes"))
                    meta = {**meta, "has_notes": True}

        pieces: list[tuple[str, str]] = []
        for text, loc in sections:
            for piece in chunk.split(text, settings.chunk_chars, settings.chunk_overlap):
                pieces.append((piece, loc))
        if not pieces:
            raise ValueError("No text could be read from this file (is it a scanned image?).")
        if len(pieces) > MAX_PIECES:  # e.g. a 100,000-row export: keep the start, note it
            meta = {**meta, "truncated": True}
            pieces = pieces[:MAX_PIECES]

        # Prefix each chunk with the document title so it carries context on its own.
        vectors = embeddings.embed_passages([f"{doc['title']}\n{p}" for p, _ in pieces])

        with conn() as c:
            with c.transaction():
                c.execute("DELETE FROM chunks WHERE document_id=%s", (doc_id,))
                with c.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO chunks(document_id, position, content, location, embedding) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        [(doc_id, i, p, loc, v) for i, ((p, loc), v) in enumerate(zip(pieces, vectors))],
                    )
                c.execute(
                    "UPDATE documents SET status='ready', metadata=%s, updated_at=now() WHERE id=%s",
                    (json.dumps(meta), doc_id),
                )
        log.info("Indexed %s (%d chunks)", doc["filename"], len(pieces))
    except Exception as e:  # noqa: BLE001 - store any failure on the document for the admin to see
        log.exception("Failed to index document %s", doc_id)
        with conn() as c:
            c.execute("UPDATE documents SET status='error', error=%s WHERE id=%s", (str(e)[:1000], doc_id))
