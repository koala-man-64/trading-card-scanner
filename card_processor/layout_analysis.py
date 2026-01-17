"""Document layout analysis pipeline using a DETR-based card detector."""

from __future__ import annotations

import logging
from typing import Dict, Optional

from .image_io import load_rgb_image
from .layout_crops import attach_crops
from .layout_infer import infer_layout
from .layout_model import get_model
from .layout_post import assign_reading_order, to_layout_elements
from .layout_types import LayoutAnalysisResult

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


def analyze_layout_from_image_bytes(
    image_bytes: bytes,
    *,
    model_variant: Optional[str] = None,
    imgsz: int = 1280,
    conf: float = 0.25,
    iou: float = 0.5,
    extract_crops: bool = True,
    crop_format: str = "png",
) -> LayoutAnalysisResult:
    """Analyze document layout from raw image bytes."""
    errors = []
    try:
        img = load_rgb_image(image_bytes)
    except Exception as exc:
        return LayoutAnalysisResult(
            image_width=0,
            image_height=0,
            elements=[],
            model_info={},
            errors=[str(exc)],
        )

    width, height = img.size

    try:
        bundle = get_model(model_variant)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Failed to load model %s", model_variant)
        return LayoutAnalysisResult(
            image_width=width,
            image_height=height,
            elements=[],
            model_info={},
            errors=[f"model_load_error: {exc}"],
        )

    class_map = _build_class_map(bundle.model)
    raw_dets = infer_layout(bundle.model, bundle.processor, img, conf=conf)
    elements = to_layout_elements(raw_dets, width, height, class_map)
    assign_reading_order(elements)

    if extract_crops and elements:
        try:
            attach_crops(elements, img, crop_format=crop_format)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed to attach crops")
            errors.append(f"crop_error: {exc}")

    return LayoutAnalysisResult(
        image_width=width,
        image_height=height,
        elements=elements,
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
