"""Model resolution and caching for YOLO11 document layout detection."""

from __future__ import annotations

import threading
from typing import Dict

from huggingface_hub import hf_hub_download
from ultralytics import YOLO

_MODEL_FILENAMES: Dict[str, str] = {
    "nano": "yolo11n_doc_layout.pt",
    "small": "yolo11s_doc_layout.pt",
    "medium": "yolo11m_doc_layout.pt",
}

_MODEL_CACHE: Dict[str, YOLO] = {}
_MODEL_LOCK = threading.Lock()


class UnknownVariantError(ValueError):
    """Raised when an unsupported model variant is requested."""


def resolve_model_path(variant: str) -> str:
    """Return the local path for the requested model variant, downloading if needed."""
    filename = _MODEL_FILENAMES.get(variant)
    if not filename:
        raise UnknownVariantError(
            f"Unsupported model_variant '{variant}'. "
            f"Choose from: {', '.join(sorted(_MODEL_FILENAMES))}."
        )
    return hf_hub_download(
        repo_id="Armaggheddon/yolo11-document-layout", filename=filename
    )


def get_model(variant: str) -> YOLO:
    """Return a cached YOLO model instance for the requested variant."""
    variant = variant.lower()
    if variant in _MODEL_CACHE:
        return _MODEL_CACHE[variant]

    with _MODEL_LOCK:
        if variant in _MODEL_CACHE:
            return _MODEL_CACHE[variant]
        model_path = resolve_model_path(variant)
        model = YOLO(model_path)
        _MODEL_CACHE[variant] = model
        return model
