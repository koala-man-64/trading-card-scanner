"""Inference helpers for DETR-based object detection."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import torch
from PIL import Image

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TRANSFORMERS_NO_FLAX", "1")

from transformers import DetrForObjectDetection, DetrImageProcessor
from transformers.feature_extraction_utils import BatchFeature

from .detection_types import RawDetection


def infer_detections(
    model: DetrForObjectDetection,
    processor: DetrImageProcessor,
    img: Image.Image,
    *,
    conf: float,
    imgsz: Optional[int] = None,
    iou: float = 1.0,
) -> List[RawDetection]:
    """Run DETR inference and return raw detections.

    Args:
        conf: Confidence threshold for keeping a detection.
        imgsz: When set, resizes so the shortest image edge is ``imgsz`` pixels
            before inference (higher = more detail for small cards, but slower).
            When ``None`` the processor's default resize is used.
        iou: IoU threshold for non-max suppression of overlapping boxes. DETR
            rarely emits duplicates, so this acts as a safety net; ``1.0``
            disables suppression entirely.
    """
    device = next(model.parameters()).device
    processor_kwargs: Dict[str, Any] = {"images": img, "return_tensors": "pt"}
    if imgsz:
        processor_kwargs["size"] = {
            "shortest_edge": int(imgsz),
            "longest_edge": max(1333, int(imgsz)),
        }
    inputs: BatchFeature = processor(**processor_kwargs)
    inputs = inputs.to(device)
    with torch.no_grad():
        outputs = model(**inputs)

    target_sizes = [(img.height, img.width)]
    results = processor.post_process_object_detection(
        outputs, threshold=conf, target_sizes=target_sizes
    )

    detections: List[RawDetection] = []
    for score, label, box in zip(
        results[0]["scores"], results[0]["labels"], results[0]["boxes"]
    ):
        x1, y1, x2, y2 = box.tolist()
        detections.append(
            RawDetection(
                label=str(int(label.item())),
                confidence=float(score.item()),
                bbox_xyxy=(float(x1), float(y1), float(x2), float(y2)),
            )
        )
    return suppress_overlapping_detections(detections, iou)


def _iou_xyxy(
    a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]
) -> float:
    """Intersection-over-union for two boxes in (x1, y1, x2, y2) format."""
    inter_x1 = max(a[0], b[0])
    inter_y1 = max(a[1], b[1])
    inter_x2 = min(a[2], b[2])
    inter_y2 = min(a[3], b[3])
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    intersection = inter_w * inter_h
    if intersection <= 0.0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0.0 else 0.0


def suppress_overlapping_detections(
    detections: Sequence[RawDetection], iou_threshold: float
) -> List[RawDetection]:
    """Greedy non-max suppression over detections, highest confidence first."""
    if not detections or iou_threshold >= 1.0:
        return list(detections)
    ordered = sorted(detections, key=lambda det: det.confidence, reverse=True)
    kept: List[RawDetection] = []
    for det in ordered:
        if all(
            _iou_xyxy(det.bbox_xyxy, kept_det.bbox_xyxy) <= iou_threshold
            for kept_det in kept
        ):
            kept.append(det)
    return kept
