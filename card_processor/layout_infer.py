"""Inference helpers for DETR-based object detection."""

from __future__ import annotations

import os
from typing import List

import torch
from PIL import Image

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TRANSFORMERS_NO_FLAX", "1")

from transformers import DetrForObjectDetection, DetrImageProcessor

from .layout_types import RawDetection


def infer_layout(
    model: DetrForObjectDetection,
    processor: DetrImageProcessor,
    img: Image.Image,
    *,
    conf: float,
) -> List[RawDetection]:
    """Run DETR inference and return raw detections."""
    device = next(model.parameters()).device
    inputs = processor(images=img, return_tensors="pt")
    inputs = inputs.to(device)
    with torch.no_grad():
        outputs = model(**inputs)

    target_sizes = torch.tensor([[img.height, img.width]], device=device)
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
