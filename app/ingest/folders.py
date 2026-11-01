"""Watched folders: anything placed under SOURCES_DIR is indexed automatically.

Layout:
    sources/
        _company/        -> visible to everyone
        Sales/           -> visible to the Sales department
        HR/              -> visible to HR
        Sales+Support/   -> visible to both Sales and Support

Use rclone, OneDrive/Google Drive sync, a network share, or a scanner's output folder to keep
these folders filled from wherever the business keeps its files.
"""
import logging
from pathlib import Path

from ..config import settings
from ..db import conn
from .extract import is_audio_video, is_supported
from .pipeline import sha256_file

log = logging.getLogger("folders")
COMPANY_FOLDER = "_company"


def _department_ids(c, folder_name: str) -> list[int]:
    ids = []
    for name in [n.strip() for n in folder_name.split("+") if n.strip()]:
        row = c.execute(
            "INSERT INTO departments(name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id",
            (name,),
        ).fetchone()
        ids.append(row["id"])
    return ids


def scan() -> int:
    """Sync the database with the files on disk. Returns how many documents were queued."""
    root = settings.sources_dir
    if not root.exists():
        return 0
    queued = 0
    seen: set[str] = set()

    with conn() as c:
        existing = {
            r["path"]: r
            for r in c.execute("SELECT id, path, sha256 FROM documents WHERE source='folder'").fetchall()
        }

        for top in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            company_wide = top.name == COMPANY_FOLDER
            dept_ids = [] if company_wide else _department_ids(c, top.name)
            for path in top.rglob("*"):
                if not path.is_file() or path.name.startswith((".", "~$")) or not is_supported(path):
                    continue
                key = str(path)
                seen.add(key)
                digest = sha256_file(path)
                old = existing.get(key)
                if old and old["sha256"] == digest:
                    continue
                kind = "interview" if is_audio_video(path) or "interview" in path.parts[-2].lower() else "file"
                if old:
                    c.execute(
                        "UPDATE documents SET sha256=%s, status='pending', updated_at=now() WHERE id=%s",
                        (digest, old["id"]),
                    )
                    doc_id = old["id"]
                else:
                    doc_id = c.execute(
                        "INSERT INTO documents(title, filename, path, kind, source, sha256, company_wide) "
                        "VALUES (%s, %s, %s, %s, 'folder', %s, %s) RETURNING id",
                        (path.stem, path.name, key, kind, digest, company_wide),
                    ).fetchone()["id"]
                c.execute("DELETE FROM document_departments WHERE document_id=%s", (doc_id,))
                for d in dept_ids:
                    c.execute("INSERT INTO document_departments VALUES (%s, %s)", (doc_id, d))
                queued += 1

        removed = [r["id"] for k, r in existing.items() if k not in seen]
        if removed:
            c.execute("DELETE FROM documents WHERE id = ANY(%s)", (removed,))
            log.info("Removed %d documents whose files were deleted", len(removed))

    if queued:
        log.info("Queued %d new or changed files from %s", queued, root)
    return queued
