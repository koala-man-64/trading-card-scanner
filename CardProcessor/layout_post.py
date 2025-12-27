"""Post-processing utilities for layout detection."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .layout_types import BBox, LayoutElement, RawDetection


def clamp_bbox(
    x1: float, y1: float, x2: float, y2: float, width: int, height: int
) -> Optional[BBox]:
    """Clamp a bbox to image bounds; return None if invalid after clamping."""
    ix1 = max(0, min(int(round(x1)), width))
    iy1 = max(0, min(int(round(y1)), height))
    ix2 = max(0, min(int(round(x2)), width))
    iy2 = max(0, min(int(round(y2)), height))

    if ix2 <= ix1 or iy2 <= iy1:
        return None
    return (ix1, iy1, ix2, iy2)


def _normalize_bbox(bbox: BBox, width: int, height: int) -> Tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox
    return (x1 / width, y1 / height, x2 / width, y2 / height)


def to_layout_elements(
    raw_dets: Iterable[RawDetection],
    width: int,
    height: int,
    class_map: Dict[str, str],
) -> List[LayoutElement]:
    """Convert raw detections to structured layout elements."""
    elements: List[LayoutElement] = []
    for det in raw_dets:
        label = class_map.get(det.label, det.label)
        clamped = clamp_bbox(*det.bbox_xyxy, width=width, height=height)
        if not clamped:
            continue
        norm = _normalize_bbox(clamped, width, height)
        elements.append(
            LayoutElement(
                label=label,
                confidence=det.confidence,
                bbox_xyxy=clamped,
                bbox_norm=norm,
            )
        )
    return elements


_READING_ORDER_LABELS = {
    "Title",
    "Section-header",
    "Text",
    "List-item",
    "Caption",
    "Footnote",
}


def assign_reading_order(elements: List[LayoutElement]) -> List[LayoutElement]:
    """Assign reading_order_hint to text-like elements based on top-left ordering."""
    text_like = [
        (idx, el)
        for idx, el in enumerate(elements)
        if el.label in _READING_ORDER_LABELS
    ]
    sorted_pairs = sorted(text_like, key=lambda pair: (pair[1].bbox_xyxy[1], pair[1].bbox_xyxy[0]))
    for order, (idx, _) in enumerate(sorted_pairs):
        elements[idx].reading_order_hint = order
    return elements

