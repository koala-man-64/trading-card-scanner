"""Inference helpers for document layout detection."""

from __future__ import annotations

from typing import List

from PIL import Image
from ultralytics import YOLO
from ultralytics.engine.results import Boxes

from .layout_types import RawDetection


def _boxes_to_detections(boxes: Boxes) -> List[RawDetection]:
    detections: List[RawDetection] = []
    if boxes is None:
        return detections

    for xyxy, conf, cls_idx in zip(boxes.xyxy, boxes.conf, boxes.cls):
        x1, y1, x2, y2 = xyxy.tolist()
        detections.append(
            RawDetection(
                label=str(int(cls_idx.item())),
                confidence=float(conf.item()),
                bbox_xyxy=(float(x1), float(y1), float(x2), float(y2)),
            )
        )
    return detections


def infer_layout(
    model: YOLO, img: Image.Image, *, imgsz: int, conf: float, iou: float
) -> List[RawDetection]:
    """Run YOLO inference and return raw detections."""
    results = model.predict(img, imgsz=imgsz, conf=conf, iou=iou, verbose=False)
    detections: List[RawDetection] = []
    for res in results:
        boxes = res.boxes
        res_dets = _boxes_to_detections(boxes)
        names = res.names or model.names
        if names:
            for det in res_dets:
                try:
                    det.label = str(names[int(det.label)])
                except Exception:
                    pass
        detections.extend(res_dets)
    return detections

