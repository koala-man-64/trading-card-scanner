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


def bbox_iou(left: BBox, right: BBox) -> float:
    """Return intersection-over-union for two xyxy boxes."""
    left_x1, left_y1, left_x2, left_y2 = left
    right_x1, right_y1, right_x2, right_y2 = right

    inter_x1 = max(left_x1, right_x1)
    inter_y1 = max(left_y1, right_y1)
    inter_x2 = min(left_x2, right_x2)
    inter_y2 = min(left_y2, right_y2)

    inter_w = max(0, inter_x2 - inter_x1)
    inter_h = max(0, inter_y2 - inter_y1)
    intersection = inter_w * inter_h
    if intersection == 0:
        return 0.0

    left_area = max(0, left_x2 - left_x1) * max(0, left_y2 - left_y1)
    right_area = max(0, right_x2 - right_x1) * max(0, right_y2 - right_y1)
    union = left_area + right_area - intersection
    if union <= 0:
        return 0.0
    return intersection / union


def to_detected_cards(
    raw_dets: Iterable[RawDetection],
    width: int,
    height: int,
    class_map: Dict[str, str],
) -> List[DetectedCard]:
    """Convert raw detections to structured detected-card records."""
    elements: List[DetectedCard] = []
    for det in raw_dets:
        label = class_map.get(det.label, det.label)
        clamped = clamp_bbox(*det.bbox_xyxy, width=width, height=height)
        if not clamped:
            continue
        norm = _normalize_bbox(clamped, width, height)
        elements.append(
            DetectedCard(
                label=label,
                confidence=det.confidence,
                bbox_xyxy=clamped,
                bbox_norm=norm,
            )
        )
    return elements


def suppress_overlapping_cards(
    elements: Iterable[DetectedCard],
    *,
    iou_threshold: float,
) -> List[DetectedCard]:
    """Apply class-aware non-maximum suppression to detected cards."""
    indexed = list(enumerate(elements))
    if not indexed or iou_threshold >= 1:
        return [element for _, element in indexed]

    def sort_key(item: Tuple[int, DetectedCard]) -> Tuple[float, int]:
        _, element = item
        x1, y1, x2, y2 = element.bbox_xyxy
        area = max(0, x2 - x1) * max(0, y2 - y1)
        return (element.confidence, area)

    candidates = sorted(indexed, key=sort_key, reverse=True)
    kept: List[Tuple[int, DetectedCard]] = []

    while candidates:
        current_idx, current = candidates.pop(0)
        kept.append((current_idx, current))
        candidates = [
            (idx, candidate)
            for idx, candidate in candidates
            if candidate.label != current.label
            or bbox_iou(current.bbox_xyxy, candidate.bbox_xyxy) <= iou_threshold
        ]

    return [element for _, element in sorted(kept, key=lambda item: item[0])]
