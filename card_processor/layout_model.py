"""Model resolution and caching for DETR-based card detection."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, cast

import torch

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TRANSFORMERS_NO_FLAX", "1")

from transformers import DetrForObjectDetection, DetrImageProcessor

from .settings import DEFAULT_MODEL_ID, ScannerSettings, load_settings

_MODEL_ALIASES: Dict[str, str] = {
    "nano": DEFAULT_MODEL_ID,
    "small": DEFAULT_MODEL_ID,
    "medium": DEFAULT_MODEL_ID,
}

_MODEL_CACHE: Dict[str, "ModelBundle"] = {}
_MODEL_LOCK = threading.Lock()


@dataclass(frozen=True)
class ModelBundle:
    """Grouped model assets for inference."""

    model: DetrForObjectDetection
    processor: DetrImageProcessor
    device: torch.device
    model_id: str


class ModelResolutionError(ValueError):
    """Raised when a requested model is not allowed for this deployment."""


def resolve_model_id(
    model_variant: Optional[str], settings: Optional[ScannerSettings] = None
) -> str:
    """Resolve a model alias to a Hugging Face model id."""
    scanner_settings = settings or load_settings()
    if not model_variant:
        model_id = DEFAULT_MODEL_ID
    else:
        normalized = model_variant.strip()
        alias = scanner_settings.model_aliases.get(normalized.lower())
        if alias is None:
            alias = _MODEL_ALIASES.get(normalized.lower())
        model_id = alias or normalized

    if model_id not in scanner_settings.allowed_model_ids:
        raise ModelResolutionError(
            f"Model '{model_variant or model_id}' is not allowed"
        )
    return model_id


def allowed_model_ids(settings: Optional[ScannerSettings] = None) -> set[str]:
    scanner_settings = settings or load_settings()
    return set(scanner_settings.allowed_model_ids)


def allowed_model_aliases(settings: Optional[ScannerSettings] = None) -> Dict[str, str]:
    scanner_settings = settings or load_settings()
    return dict(scanner_settings.model_aliases)


def _resolve_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def get_model(
    model_variant: Optional[str] = None, settings: Optional[ScannerSettings] = None
) -> ModelBundle:
    """Return a cached DETR model + processor bundle."""
    model_id = resolve_model_id(model_variant, settings)
    if model_id in _MODEL_CACHE:
        return _MODEL_CACHE[model_id]

    with _MODEL_LOCK:
        if model_id in _MODEL_CACHE:
            return _MODEL_CACHE[model_id]
        device = _resolve_device()
        model = DetrForObjectDetection.from_pretrained(model_id)
        cast(Any, model).to(device)
        model.eval()
        processor = DetrImageProcessor.from_pretrained(model_id)
        bundle = ModelBundle(
            model=model, processor=processor, device=device, model_id=model_id
        )
        _MODEL_CACHE[model_id] = bundle
        return bundle
