import logging
import re
from typing import List, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

try:
    import pytesseract
except ImportError:
    pytesseract = None  # type: ignore

logger = logging.getLogger(__name__)

BoundingBox = Tuple[int, int, int, int]  # (x, y, w, h)


def suppress_overlapping_boxes(boxes: Sequence[BoundingBox], iou_threshold: float = 0.3) -> List[BoundingBox]:
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
        keep.append((int(rects[i, 0]), int(rects[i, 1]), int(rects[i, 2]), int(rects[i, 3])))

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


def non_max_suppression(boxes: List[BoundingBox], overlap_thresh: float = 0.3) -> List[BoundingBox]:
    """Backward-compatible alias for `suppress_overlapping_boxes`."""
    return suppress_overlapping_boxes(boxes, iou_threshold=overlap_thresh)


def detect_card_boxes(image: np.ndarray) -> List[BoundingBox]:
    """Detect potential trading-card bounding boxes in a BGR image."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(blurred, 50, 150)
    kernel = np.ones((3, 3), np.uint8)
    dilated = cv2.dilate(edged, kernel, iterations=1)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    logger.debug("detect_card_boxes: found %d contours before filtering", len(contours))

    height, width = image.shape[:2]
    min_area = (height * width) * 0.01  # ignore very small contours (<1% of image)
    max_area = (height * width) * 0.9  # ignore extremely large contour (likely entire image)

    candidates: List[BoundingBox] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
            continue

        x, y, w, h = cv2.boundingRect(contour)
        aspect = w / float(h)
        if 0.3 < aspect < 3.5:
            candidates.append((x, y, w, h))

    logger.debug("detect_card_boxes: retained %d candidates after filtering", len(candidates))
    boxes = suppress_overlapping_boxes(candidates, iou_threshold=0.5)

    split_boxes: List[BoundingBox] = []
    for box in boxes:
        split_boxes.extend(_split_box_if_multiple_cards(image, box, recursion_depth=0))

    final_boxes = suppress_overlapping_boxes(split_boxes, iou_threshold=0.3)
    final_boxes.sort(key=lambda b: (b[1], b[0]))
    logger.debug("detect_card_boxes: returning %d boxes after suppression/splitting", len(final_boxes))
    return final_boxes


def detect_cards(image: np.ndarray) -> List[BoundingBox]:
    """Backward-compatible wrapper for `detect_card_boxes`."""
    return detect_card_boxes(image)


def _split_box_if_multiple_cards(
    image: np.ndarray,
    box: BoundingBox,
    recursion_depth: int = 0,
    *,
    max_recursion_depth: int = 2,
) -> List[BoundingBox]:
    """Split a bounding box if it likely contains multiple cards.

    The heuristic checks whether a box is unusually wide or tall and attempts to
    split it along low-edge-density regions (typically gaps between cards).
    """
    if recursion_depth >= max_recursion_depth:
        return [box]

    x, y, w, h = box
    aspect = w / float(h)

    roi = image[y : y + h, x : x + w]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)

    def _candidate_split_centers(*, axis: int, min_segment_fraction: float = 0.05) -> List[int]:
        projection = np.sum(edges, axis=axis)
        max_val = np.max(projection) + 1e-6
        inverted = 1.0 - (projection / max_val)
        smoothed = np.convolve(inverted, np.ones(11) / 11.0, mode="same")
        threshold = np.mean(smoothed) + (np.std(smoothed) * 0.5)

        candidate_indices = np.where(smoothed > threshold)[0]
        if len(candidate_indices) == 0:
            return []

        segments = []
        start = int(candidate_indices[0])
        prev = start
        for idx in candidate_indices[1:]:
            idx_int = int(idx)
            if idx_int - prev > 1:
                segments.append((start, prev))
                start = idx_int
            prev = idx_int
        segments.append((start, prev))

        region_size = w if axis == 0 else h
        min_segment_size = region_size * min_segment_fraction
        centers: List[int] = []
        for seg_start, seg_end in segments:
            if (seg_end - seg_start) >= min_segment_size:
                centers.append(int((seg_start + seg_end) / 2))
        return centers

    if aspect > 1.2:
        centers = _candidate_split_centers(axis=0)
        if centers:
            boundaries = [0] + centers + [w]
            splits: List[BoundingBox] = []
            for i in range(len(boundaries) - 1):
                cx0 = boundaries[i]
                cx1 = boundaries[i + 1]
                if cx1 - cx0 <= 0:
                    continue
                sub_box = (x + cx0, y, cx1 - cx0, h)
                splits.extend(
                    _split_box_if_multiple_cards(
                        image,
                        sub_box,
                        recursion_depth + 1,
                        max_recursion_depth=max_recursion_depth,
                    )
                )
            return splits

    if (1 / aspect) > 1.2:
        centers = _candidate_split_centers(axis=1)
        if centers:
            boundaries = [0] + centers + [h]
            splits = []
            for i in range(len(boundaries) - 1):
                cy0 = boundaries[i]
                cy1 = boundaries[i + 1]
                if cy1 - cy0 <= 0:
                    continue
                sub_box = (x, y + cy0, w, cy1 - cy0)
                splits.extend(
                    _split_box_if_multiple_cards(
                        image,
                        sub_box,
                        recursion_depth + 1,
                        max_recursion_depth=max_recursion_depth,
                    )
                )
            return splits

    return [box]


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


def extract_card_crops_from_image_bytes(image_bytes: bytes) -> List[Tuple[str, bytes]]:
    """Decode an image, detect cards, and return cropped card JPEG bytes."""
    file_bytes = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
    if image is None:
        return []

    boxes = detect_card_boxes(image)
    results: List[Tuple[str, bytes]] = []
    for x, y, w, h in boxes:
        pad = int(min(w, h) * 0.05)
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(image.shape[1], x + w + pad)
        y1 = min(image.shape[0], y + h + pad)
        crop = image[y0:y1, x0:x1].copy()

        name = extract_card_name_from_crop(crop)
        ok, buf = cv2.imencode(".jpg", crop)
        if not ok:
            logger.warning("Failed to encode crop as JPEG; skipping crop")
            continue

        results.append((name, buf.tobytes()))

    return results


def process_image(data: bytes) -> List[Tuple[str, bytes]]:
    """Backward-compatible wrapper for `extract_card_crops_from_image_bytes`."""
    return extract_card_crops_from_image_bytes(data)
