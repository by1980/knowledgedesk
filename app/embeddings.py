"""Text embeddings, computed locally on the customer's server (documents are not sent anywhere to be indexed)."""
import threading

import numpy as np

from .config import settings

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding

            _model = TextEmbedding(model_name=settings.embed_model, cache_dir=settings.model_cache)
    return _model


def _is_e5() -> bool:
    # E5 models expect "query: " / "passage: " prefixes.
    return "e5" in settings.embed_model.lower()


def embed_passages(texts: list[str]) -> list[np.ndarray]:
    if not texts:
        return []
    if _is_e5():
        texts = [f"passage: {t}" for t in texts]
    return list(_get_model().embed(texts, batch_size=16))


def embed_query(text: str) -> np.ndarray:
    if _is_e5():
        text = f"query: {text}"
    return next(iter(_get_model().embed([text])))


def dimension() -> int:
    return len(embed_query("dimension check"))
