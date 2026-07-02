"""Inference helpers for DETR-based object detection."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

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
) -> List[RawDetection]:
    """Run DETR inference and return raw detections.

    When ``imgsz`` is set, the processor resizes so the shortest image edge is
    ``imgsz`` pixels before inference (higher = more detail for small cards, but
    slower). When ``None`` the processor's default resize is used.
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
    return detections
