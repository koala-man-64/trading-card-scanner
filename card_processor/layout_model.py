"""Model resolution and caching for DETR-based card detection."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional

import torch

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TRANSFORMERS_NO_FLAX", "1")

from transformers import DetrForObjectDetection, DetrImageProcessor

DEFAULT_MODEL_ID = "Matthieu68857/pokemon-cards-detection"

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


def resolve_model_id(model_variant: Optional[str]) -> str:
    """Resolve a model alias to a Hugging Face model id."""
    if not model_variant:
        return DEFAULT_MODEL_ID
    normalized = model_variant.strip()
    alias = _MODEL_ALIASES.get(normalized.lower())
    return alias or normalized


def _resolve_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def get_model(model_variant: Optional[str] = None) -> ModelBundle:
    """Return a cached DETR model + processor bundle."""
    model_id = resolve_model_id(model_variant)
    if model_id in _MODEL_CACHE:
        return _MODEL_CACHE[model_id]

    with _MODEL_LOCK:
        if model_id in _MODEL_CACHE:
            return _MODEL_CACHE[model_id]
        device = _resolve_device()
        model = DetrForObjectDetection.from_pretrained(model_id)
        model.to(device)
        model.eval()
        processor = DetrImageProcessor.from_pretrained(model_id)
        bundle = ModelBundle(
            model=model, processor=processor, device=device, model_id=model_id
        )
        _MODEL_CACHE[model_id] = bundle
        return bundle
