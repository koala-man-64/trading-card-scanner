"""Document layout analysis pipeline using Hugging Face YOLO11 model."""

from __future__ import annotations

import logging
from typing import Dict

from .image_io import load_rgb_image
from .layout_crops import attach_crops
from .layout_infer import infer_layout
from .layout_model import UnknownVariantError, get_model
from .layout_post import assign_reading_order, to_layout_elements
from .layout_types import LayoutAnalysisResult

logger = logging.getLogger(__name__)

# Model class mapping from the YOLO dataset
_CLASS_MAP: Dict[str, str] = {
    "0": "Text",
    "1": "Title",
    "2": "Section-header",
    "3": "Table",
    "4": "Picture",
    "5": "Caption",
    "6": "List-item",
    "7": "Formula",
    "8": "Page-header",
    "9": "Page-footer",
    "10": "Footnote",
}


def analyze_layout_from_image_bytes(
    image_bytes: bytes,
    *,
    model_variant: str = "nano",
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
        model = get_model(model_variant)
    except UnknownVariantError as exc:
        return LayoutAnalysisResult(
            image_width=width,
            image_height=height,
            elements=[],
            model_info={},
            errors=[str(exc)],
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Failed to load model variant %s", model_variant)
        return LayoutAnalysisResult(
            image_width=width,
            image_height=height,
            elements=[],
            model_info={},
            errors=[f"model_load_error: {exc}"],
        )

    raw_dets = infer_layout(model, img, imgsz=imgsz, conf=conf, iou=iou)
    elements = to_layout_elements(raw_dets, width, height, _CLASS_MAP)
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
            "model_variant": model_variant,
            "class_map": _CLASS_MAP,
            "imgsz": imgsz,
            "conf": conf,
            "iou": iou,
        },
        errors=errors,
    )

