"""Card detection pipeline using a DETR-based detector."""

from __future__ import annotations

import logging
from typing import Dict, Optional

from .image_io import load_rgb_image
from .detection_crops import attach_crops
from .detection_infer import infer_detections
from .detection_model import get_model
from .settings import ScannerSettings
from .detection_post import to_detected_cards
from .detection_types import DetectionResult

logger = logging.getLogger(__name__)


def _normalize_label(label: str) -> str:
    normalized = label.strip()
    if not normalized:
        return "Card"
    lower = normalized.lower()
    if "card" in lower or lower == "prediction":
        return "Card"
    return normalized


def _build_class_map(model) -> Dict[str, str]:
    id2label = getattr(model.config, "id2label", None)
    if isinstance(id2label, dict) and id2label:
        if len(id2label) == 1:
            return {str(key): "Card" for key in id2label}
        return {
            str(key): _normalize_label(str(value)) for key, value in id2label.items()
        }
    return {"0": "Card"}


def detect_cards_from_image_bytes(
    image_bytes: bytes,
    *,
    model_variant: Optional[str] = None,
    imgsz: Optional[int] = None,
    conf: float = 0.25,
    iou: float = 0.5,
    extract_crops: bool = True,
    crop_format: str = "png",
    settings: Optional[ScannerSettings] = None,
) -> DetectionResult:
    """Detect trading cards in raw image bytes."""
    errors = []
    try:
        img = load_rgb_image(image_bytes)
    except Exception as exc:
        return DetectionResult(
            image_width=0,
            image_height=0,
            elements=[],
            model_info={},
            errors=[str(exc)],
        )

    width, height = img.size

    try:
        bundle = get_model(model_variant, settings=settings)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Failed to load model %s", model_variant)
        return DetectionResult(
            image_width=width,
            image_height=height,
            elements=[],
            model_info={},
            errors=[f"model_load_error: {exc}"],
        )

    class_map = _build_class_map(bundle.model)
    raw_dets = infer_detections(
        bundle.model, bundle.processor, img, conf=conf, imgsz=imgsz, iou=iou
    )
    cards = to_detected_cards(raw_dets, width, height, class_map)

    if extract_crops and cards:
        try:
            attach_crops(cards, img, crop_format=crop_format)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed to attach crops")
            errors.append(f"crop_error: {exc}")

    return DetectionResult(
        image_width=width,
        image_height=height,
        elements=cards,
        model_info={
            "model_id": bundle.model_id,
            "model_variant": model_variant,
            "class_map": class_map,
            "device": str(bundle.device),
            "imgsz": imgsz,
            "conf": conf,
            "iou": iou,
        },
        errors=errors,
    )
