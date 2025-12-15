import io
import os
import uuid
from typing import List, Tuple

import cv2
import numpy as np
from PIL import Image

try:
    import pytesseract  # noqa: WPS433
except ImportError:
    pytesseract = None  # type: ignore


def non_max_suppression(boxes: List[Tuple[int, int, int, int]], overlap_thresh: float = 0.3) -> List[Tuple[int, int, int, int]]:
    """Apply non‑maximum suppression to a list of bounding boxes.

    This helps remove duplicate/overlapping detections that often occur
    when multiple contours are found for the same card.

    Args:
        boxes: List of bounding boxes in (x, y, w, h) format.
        overlap_thresh: Intersection over union (IOU) threshold to discard overlaps.

    Returns:
        A filtered list of bounding boxes.
    """
    print(f"non_max_suppression called with {len(boxes)} boxes and overlap_thresh={overlap_thresh}")
    if not boxes:
        return []

    # Convert to float numpy array for computation
    rects = np.array(boxes, dtype=float)
    # Compute bottom right coordinates
    x1 = rects[:, 0]
    y1 = rects[:, 1]
    x2 = rects[:, 0] + rects[:, 2]
    y2 = rects[:, 1] + rects[:, 3]
    areas = rects[:, 2] * rects[:, 3]
    order = areas.argsort()[::-1]  # sort by area descending

    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append(boxes[int(i)])
        # compute intersection areas with the rest
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        union = areas[i] + areas[order[1:]] - inter
        # Compute IOU
        iou = inter / (union + 1e-6)
        # Keep boxes with IOU less than threshold
        inds = np.where(iou <= overlap_thresh)[0]
        order = order[inds + 1]

    return keep


def detect_cards(image: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Detect potential trading card slabs in an image.

    The heuristic relies on edge detection and contour analysis to locate
    rectangular regions that correspond to individual cards. A non-maximum
    suppression step is applied to remove overlapping detections.

    Args:
        image: BGR image as a numpy array.

    Returns:
        A list of bounding boxes in (x, y, w, h) format.
    """
    print(f"detect_cards called with image of shape {image.shape}")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Slight blur to smooth noise
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    # Canny edge detection
    edged = cv2.Canny(blurred, 50, 150)
    # Dilate edges to close gaps
    kernel = np.ones((3, 3), np.uint8)
    dilated = cv2.dilate(edged, kernel, iterations=1)
    # Find contours
    cnts, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    print(f"detect_cards found {len(cnts)} contours before filtering")
    height, width = image.shape[:2]
    min_area = (height * width) * 0.01  # ignore very small contours (<1% of image)
    max_area = (height * width) * 0.9   # ignore extremely large area (likely the entire image)
    candidates: List[Tuple[int, int, int, int]] = []
    for cnt in cnts:
        area = cv2.contourArea(cnt)
        if area < min_area or area > max_area:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        # aspect ratio filter: cards are roughly rectangular but not extremely elongated
        aspect = w / float(h)
        if 0.3 < aspect < 3.5:
            candidates.append((x, y, w, h))

    print(f"detect_cards retained {len(candidates)} candidate boxes after area/aspect filtering")

    # Apply non-maximum suppression to reduce duplicates
    boxes = non_max_suppression(candidates, overlap_thresh=0.5)

    # Additional splitting for boxes that likely contain multiple cards
    split_boxes: List[Tuple[int, int, int, int]] = []
    for box in boxes:
        split_boxes.extend(_split_if_needed(image, box, depth=0))

    # Apply non-maximum suppression again to remove overlaps after splitting
    final_boxes = non_max_suppression(split_boxes, overlap_thresh=0.3)
    print(f"detect_cards returning {len(final_boxes)} final boxes after suppression and splitting")
    # Sort by y then x for consistent ordering
    final_boxes.sort(key=lambda b: (b[1], b[0]))
    return final_boxes


def _split_if_needed(image: np.ndarray, box: Tuple[int, int, int, int], depth: int = 0) -> List[Tuple[int, int, int, int]]:
    """Recursively split a bounding box if it appears to contain multiple cards.

    The heuristic examines the aspect ratio of the box and the distribution
    of edge intensity within the box. If the width is significantly larger
    than the height, the box is split vertically where the cumulative edge
    intensity is low (i.e. between cards). Similarly, if the height is
    significantly larger than the width, the box is split horizontally. A
    maximum recursion depth prevents infinite splitting.

    Args:
        image: The original image.
        box: Bounding box (x, y, w, h).
        depth: Current recursion depth.

    Returns:
        A list of bounding boxes; either the original box or smaller splits.
    """
    print(f"_split_if_needed called with box={box} at depth={depth}")
    MAX_DEPTH = 2
    if depth >= MAX_DEPTH:
        return [box]
    x, y, w, h = box
    # Determine if box is likely to contain more than one card
    # A typical slab has aspect ratio around 0.6–0.8 (width/height). If the
    # ratio is much larger or smaller, it may encompass multiple cards.
    aspect = w / float(h)
    splits: List[Tuple[int, int, int, int]] = []
    # Convert region to grayscale and detect edges
    roi = image[y:y + h, x:x + w]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)

    # Helper to perform vertical split
    def vertical_splits() -> List[int]:
        print(f"vertical_splits evaluating box width {w}")
        col_sum = np.sum(edges, axis=0)
        # Normalize and invert so that spaces (between cards) have high values
        # Add a small epsilon to avoid division by zero
        max_val = np.max(col_sum) + 1e-6
        inv = 1.0 - (col_sum / max_val)
        # Smooth via 1D convolution
        smooth = np.convolve(inv, np.ones(11) / 11.0, mode='same')
        # Identify regions where the smooth signal exceeds a threshold
        threshold = np.mean(smooth) + np.std(smooth) * 0.5
        candidates = np.where(smooth > threshold)[0]
        if len(candidates) == 0:
            return []
        # Group contiguous candidates into segments
        boundaries = []
        start = candidates[0]
        prev = start
        for c in candidates[1:]:
            if c - prev > 1:
                boundaries.append((start, prev))
                start = c
            prev = c
        boundaries.append((start, prev))
        # Filter segments that are wide enough to plausibly be between cards
        min_width = w * 0.05
        centers = []
        for s, e in boundaries:
            if (e - s) >= min_width:
                centers.append(int((s + e) / 2))
        return centers

    def horizontal_splits() -> List[int]:
        print(f"horizontal_splits evaluating box height {h}")
        row_sum = np.sum(edges, axis=1)
        max_val = np.max(row_sum) + 1e-6
        inv = 1.0 - (row_sum / max_val)
        smooth = np.convolve(inv, np.ones(11) / 11.0, mode='same')
        threshold = np.mean(smooth) + np.std(smooth) * 0.5
        candidates = np.where(smooth > threshold)[0]
        if len(candidates) == 0:
            return []
        boundaries = []
        start = candidates[0]
        prev = start
        for r in candidates[1:]:
            if r - prev > 1:
                boundaries.append((start, prev))
                start = r
            prev = r
        boundaries.append((start, prev))
        min_height = h * 0.05
        centers = []
        for s, e in boundaries:
            if (e - s) >= min_height:
                centers.append(int((s + e) / 2))
        return centers

    # If width dominates height, try splitting vertically
    if aspect > 1.2:
        centers = vertical_splits()
        if centers:
            # Compute boundaries from centers
            boundaries = [0] + centers + [w]
            for i in range(len(boundaries) - 1):
                cx0 = boundaries[i]
                cx1 = boundaries[i + 1]
                if cx1 - cx0 <= 0:
                    continue
                sub_box = (x + cx0, y, cx1 - cx0, h)
                splits.extend(_split_if_needed(image, sub_box, depth + 1))
            return splits
    # If height dominates width, try splitting horizontally
    if (1 / aspect) > 1.2:
        centers = horizontal_splits()
        if centers:
            boundaries = [0] + centers + [h]
            for i in range(len(boundaries) - 1):
                cy0 = boundaries[i]
                cy1 = boundaries[i + 1]
                if cy1 - cy0 <= 0:
                    continue
                sub_box = (x, y + cy0, w, cy1 - cy0)
                splits.extend(_split_if_needed(image, sub_box, depth + 1))
            return splits
    # No splitting applied
    return [box]


def extract_card_name(crop: np.ndarray) -> str:
    """Attempt to extract the card name from a cropped card image.

    The function looks at the top region of the card—where the label
    typically resides—and runs OCR on that area. If pytesseract is not
    available, a fallback placeholder string is returned.

    Args:
        crop: Cropped card image as a numpy array (BGR).

    Returns:
        The detected card name or "unknown" if extraction fails.
    """
    print("extract_card_name called")
    if pytesseract is None:
        return "unknown"
    # Convert to RGB for Pillow
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)
    # Take the top 25% region where the label is typically located
    w, h = pil_img.size
    top_h = int(h * 0.25)
    top_crop = pil_img.crop((0, 0, w, top_h))
    # Convert to grayscale and increase contrast to aid OCR
    gray = top_crop.convert("L")
    # Perform simple threshold to highlight text
    thresh = gray.point(lambda p: 255 if p > 180 else 0)
    text = pytesseract.image_to_string(thresh, lang="eng")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown"
    # Use the first line as card name, limited length
    name = lines[0]
    # Clean up: remove non‑alphanumeric characters (keep spaces, hyphens, apostrophes)
    import re  # imported here to avoid global import if pytesseract unavailable
    name = re.sub(r"[^A-Za-z0-9 '\-]", "", name)
    # If name too short, fallback to unknown
    return name if len(name) >= 2 else "unknown"


def process_image(data: bytes) -> List[Tuple[str, bytes]]:
    """Process raw image bytes, crop cards, run OCR and return list of (name, image bytes).

    Args:
        data: JPEG/PNG image bytes.

    Returns:
        A list of tuples, each containing the detected card name and the JPEG bytes of the
        cropped card.
    """
    print(f"process_image called with data length {len(data)} bytes")
    # Decode image from bytes
    file_bytes = np.asarray(bytearray(data), dtype=np.uint8)
    image = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
    if image is None:
        return []
    boxes = detect_cards(image)
    results: List[Tuple[str, bytes]] = []
    for x, y, w, h in boxes:
        # expand bounding box slightly to include borders
        pad = int(min(w, h) * 0.05)
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(image.shape[1], x + w + pad)
        y1 = min(image.shape[0], y + h + pad)
        crop = image[y0:y1, x0:x1].copy()
        # Upload naming is derived from the input file name + index; we do not rely on OCR.
        name = "unknown"
        # Encode cropped image back to JPEG for storage
        _, buf = cv2.imencode('.jpg', crop)
        results.append((name, buf.tobytes()))
    return results
