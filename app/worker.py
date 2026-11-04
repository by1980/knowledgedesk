"""Background worker: indexes pending documents and rescans watched folders."""
import logging
import threading
import time
from collections import deque

from .config import settings
from .db import conn
from .connectors import runner
from .ingest import folders
from .ingest.pipeline import process_document

log = logging.getLogger("worker")
_wake = threading.Event()
_finished: deque = deque(maxlen=300)  # times at which items finished, to measure real speed
_durations: deque = deque(maxlen=50)  # (seconds, kind, title) of recent items
current: dict = {}  # the item being prepared right now


def recent_durations() -> list[tuple[float, str, str]]:
    return list(_durations)


def speed_per_minute() -> float | None:
    """Items prepared per minute over the last 15 minutes of work (None until there's enough data)."""
    now = time.time()
    recent = [t for t in _finished if now - t < 900]
    if len(recent) < 3:
        return None
    span = max(now - recent[0], 30)
    return len(recent) / (span / 60)


def minutes_left(waiting: int) -> int | None:
    rate = speed_per_minute()
    return None if not rate or not waiting else max(1, round(waiting / rate))


def wake() -> None:
    """Call after adding a document so it is processed right away."""
    _wake.set()


def _next_pending() -> int | None:
    with conn() as c:
        # Websites, documents, calendars and chats first (few, valuable), then emails newest first.
        row = c.execute(
            "SELECT id FROM documents WHERE status='pending' ORDER BY (kind = 'email'), created_at LIMIT 1"
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
                with conn() as c:
                    row = c.execute("SELECT title, kind FROM documents WHERE id=%s", (doc_id,)).fetchone()
                started = time.time()
                current.update(id=doc_id, title=(row or {}).get("title", ""), kind=(row or {}).get("kind", ""), started=started)
                try:
                    process_document(doc_id)
                finally:
                    _durations.append((time.time() - started, current.get("kind", ""), current.get("title", "")))
                    current.clear()
                _finished.append(time.time())
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
    from . import dates
    threading.Thread(target=dates.loop, name="dates", daemon=True).start()


def rescan_now() -> int:
    n = folders.scan()
    wake()
    return n
