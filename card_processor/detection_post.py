"""Post-processing utilities for card detection."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .detection_types import BBox, DetectedCard, RawDetection


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


def _normalize_bbox(
    bbox: BBox, width: int, height: int
) -> Tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox
    return (x1 / width, y1 / height, x2 / width, y2 / height)


def to_detected_cards(
    raw_dets: Iterable[RawDetection],
    width: int,
    height: int,
    class_map: Dict[str, str],
) -> List[DetectedCard]:
    """Convert raw detections to structured detected-card records."""
    cards: List[DetectedCard] = []
    for det in raw_dets:
        label = class_map.get(det.label, det.label)
        clamped = clamp_bbox(*det.bbox_xyxy, width=width, height=height)
        if not clamped:
            continue
        norm = _normalize_bbox(clamped, width, height)
        cards.append(
            DetectedCard(
                label=label,
                confidence=det.confidence,
                bbox_xyxy=clamped,
                bbox_norm=norm,
            )
        )
    return cards
