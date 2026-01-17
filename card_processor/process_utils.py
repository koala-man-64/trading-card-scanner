import logging
import re
from typing import List, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

from .layout_analysis import analyze_layout_from_image_bytes

try:
    import pytesseract
except ImportError:
    pytesseract = None  # type: ignore

logger = logging.getLogger(__name__)

BoundingBox = Tuple[int, int, int, int]  # (x, y, w, h)


def suppress_overlapping_boxes(
    boxes: Sequence[BoundingBox], iou_threshold: float = 0.3
) -> List[BoundingBox]:
    """Filter overlapping bounding boxes using non-maximum suppression.

    Args:
        boxes: Bounding boxes in (x, y, w, h) format.
        iou_threshold: IoU threshold above which a box is discarded.

    Returns:
        Filtered bounding boxes.
    """
    if not boxes:
        return []

    rects = np.array(list(boxes), dtype=float)
    x1 = rects[:, 0]
    y1 = rects[:, 1]
    x2 = rects[:, 0] + rects[:, 2]
    y2 = rects[:, 1] + rects[:, 3]
    areas = rects[:, 2] * rects[:, 3]
    order = areas.argsort()[::-1]  # sort by area descending

    keep: List[BoundingBox] = []
    while len(order) > 0:
        i = int(order[0])
        keep.append(
            (int(rects[i, 0]), int(rects[i, 1]), int(rects[i, 2]), int(rects[i, 3]))
        )

        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        inter_w = np.maximum(0.0, xx2 - xx1)
        inter_h = np.maximum(0.0, yy2 - yy1)
        intersection = inter_w * inter_h
        union = areas[i] + areas[order[1:]] - intersection

        iou = intersection / (union + 1e-6)
        inds = np.where(iou <= iou_threshold)[0]
        order = order[inds + 1]

    return keep


def non_max_suppression(
    boxes: List[BoundingBox], overlap_thresh: float = 0.3
) -> List[BoundingBox]:
    """Backward-compatible alias for `suppress_overlapping_boxes`."""
    return suppress_overlapping_boxes(boxes, iou_threshold=overlap_thresh)


_CARD_LABEL_ALIASES = {"card", "pokemon-card", "pokemon_card", "prediction"}


def _is_card_label(label: str) -> bool:
    normalized = label.strip().lower()
    if not normalized:
        return False
    return normalized in _CARD_LABEL_ALIASES or "card" in normalized


def _encode_bgr_image(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        return b""
    return buf.tobytes()


def _card_elements_from_bytes(image_bytes: bytes):
    result = analyze_layout_from_image_bytes(image_bytes, extract_crops=False)
    if result.errors:
        logger.warning("Card detection errors: %s", result.errors)
    return [el for el in result.elements if _is_card_label(el.label)]


def detect_card_boxes(image: np.ndarray) -> List[BoundingBox]:
    """Detect trading-card bounding boxes in a BGR image via DETR."""
    image_bytes = _encode_bgr_image(image)
    if not image_bytes:
        return []

    elements = _card_elements_from_bytes(image_bytes)
    boxes: List[BoundingBox] = []
    for element in elements:
        x1, y1, x2, y2 = element.bbox_xyxy
        boxes.append((int(x1), int(y1), int(x2 - x1), int(y2 - y1)))

    boxes.sort(key=lambda b: (b[1], b[0]))
    logger.debug("detect_card_boxes: returning %d boxes from DETR", len(boxes))
    return boxes


def detect_cards(image: np.ndarray) -> List[BoundingBox]:
    """Backward-compatible wrapper for `detect_card_boxes`."""
    return detect_card_boxes(image)


def extract_card_name_from_crop(crop: np.ndarray) -> str:
    """Extract a card name from a cropped card image using OCR."""
    if pytesseract is None:
        return "unknown"

    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)
    img_width, img_height = pil_img.size

    label_height = int(img_height * 0.25)
    label_region = pil_img.crop((0, 0, img_width, label_height))

    gray = label_region.convert("L")
    thresholded = gray.point(lambda p: 255 if p > 180 else 0)
    text = pytesseract.image_to_string(thresholded, lang="eng")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown"

    name = re.sub(r"[^A-Za-z0-9 '\-]", "", lines[0])
    return name if len(name) >= 2 else "unknown"


def extract_card_name(crop: np.ndarray) -> str:
    """Backward-compatible wrapper for `extract_card_name_from_crop`."""
    return extract_card_name_from_crop(crop)


def count_cards_in_image_bytes(image_bytes: bytes) -> int:
    """Analyze image bytes and return the number of detected cards."""
    elements = _card_elements_from_bytes(image_bytes)
    return len(elements)


def extract_card_crops_from_image_bytes(image_bytes: bytes) -> List[Tuple[str, bytes]]:
    """Decode an image, detect cards, and return cropped card JPEG bytes.

    Crops are returned with a stable, generated label (e.g., ``card_1``) rather than
    attempting OCR-based name extraction.
    """
    results: List[Tuple[str, bytes]] = []
    analysis = analyze_layout_from_image_bytes(
        image_bytes, extract_crops=True, crop_format="jpeg"
    )
    if analysis.errors:
        logger.warning("Card crop errors: %s", analysis.errors)
        return results

    elements = [el for el in analysis.elements if _is_card_label(el.label)]
    elements.sort(key=lambda el: (el.bbox_xyxy[1], el.bbox_xyxy[0]))
    for element in elements:
        if not element.crop_bytes:
            logger.warning("Missing crop bytes for detected card")
            continue
        label = f"card_{len(results) + 1}"
        results.append((label, element.crop_bytes))

    return results


def process_image(data: bytes) -> List[Tuple[str, bytes]]:
    """Backward-compatible wrapper for `extract_card_crops_from_image_bytes`."""
    return extract_card_crops_from_image_bytes(data)
