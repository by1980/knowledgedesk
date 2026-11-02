"""Background worker: indexes pending documents and rescans watched folders."""
import logging
import threading
import time

from .config import settings
from .db import conn
from .connectors import runner
from .ingest import folders
from .ingest.pipeline import process_document

log = logging.getLogger("worker")
_wake = threading.Event()


def wake() -> None:
    """Call after adding a document so it is processed right away."""
    _wake.set()


def _next_pending() -> int | None:
    with conn() as c:
        row = c.execute(
            "SELECT id FROM documents WHERE status='pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def _loop() -> None:
    # Documents left half-done by a restart are retried.
    with conn() as c:
        c.execute("UPDATE documents SET status='pending' WHERE status='processing'")
    last_scan = 0.0
    while True:
        try:
            if time.time() - last_scan > settings.scan_interval_seconds:
                folders.scan()
                last_scan = time.time()
            doc_id = _next_pending()
            if doc_id is not None:
                process_document(doc_id)
                continue
        except Exception:  # noqa: BLE001 - keep the worker alive
            log.exception("Worker error")
        _wake.wait(timeout=5)
        _wake.clear()


def _connector_loop() -> None:
    while True:
        try:
            runner.sync_due()
        except Exception:  # noqa: BLE001
            log.exception("Connector sync error")
        time.sleep(60)


def start() -> None:
    threading.Thread(target=_loop, name="indexer", daemon=True).start()
    threading.Thread(target=_connector_loop, name="connectors", daemon=True).start()


def rescan_now() -> int:
    n = folders.scan()
    wake()
    return n
