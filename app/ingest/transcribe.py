"""Speech-to-text for interviews, meetings and voice notes. Runs locally with Whisper (any language)."""
import threading
from pathlib import Path

from ..config import settings

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(
                settings.whisper_model,
                device="auto",
                compute_type="int8",
                download_root=settings.model_cache,
            )
    return _model


def _ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def transcribe(path: Path) -> list[tuple[str, str]]:
    """Return ~1 minute sections of transcript, each labeled with its start time."""
    segments, _info = _get_model().transcribe(str(path), vad_filter=True)
    sections: list[tuple[str, str]] = []
    buf: list[str] = []
    start = None
    for seg in segments:
        if start is None:
            start = seg.start
        buf.append(seg.text.strip())
        if seg.end - start >= 60:
            sections.append((" ".join(buf), _ts(start)))
            buf, start = [], None
    if buf:
        sections.append((" ".join(buf), _ts(start or 0)))
    return sections
