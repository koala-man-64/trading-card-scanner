import csv
import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

from .detection import detect_cards_from_image_bytes
from .detection_post import suppress_overlapping_cards
from .detection_types import DetectedCard

try:
    import pytesseract
except ImportError:
    pytesseract = None  # type: ignore

logger = logging.getLogger(__name__)

BoundingBox = Tuple[int, int, int, int]  # (x, y, w, h)

CARD_ASPECT_RATIO_RANGE = (0.55, 1.85)
CARD_MIN_AREA_RATIO = 0.001
CARD_DUPLICATE_IOU_THRESHOLD = 0.5
CARD_NAME_MATCH_THRESHOLD = 0.78


@dataclass(frozen=True)
class CardIdentification:
    """Best-effort card identity inferred after crop extraction."""

    name: str
    source_text: str
    match_score: float
    source: str


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


def _card_geometry_is_plausible(
    element: DetectedCard,
    *,
    image_width: int,
    image_height: int,
) -> bool:
    x1, y1, x2, y2 = element.bbox_xyxy
    width = x2 - x1
    height = y2 - y1
    if width <= 0 or height <= 0 or image_width <= 0 or image_height <= 0:
        return False

    area_ratio = (width * height) / (image_width * image_height)
    if area_ratio < CARD_MIN_AREA_RATIO:
        return False

    aspect_ratio = width / height
    min_ratio, max_ratio = CARD_ASPECT_RATIO_RANGE
    return min_ratio <= aspect_ratio <= max_ratio


def _postprocess_card_elements(
    elements: Iterable[DetectedCard],
    *,
    image_width: int,
    image_height: int,
) -> List[DetectedCard]:
    card_elements = [
        element
        for element in elements
        if _is_card_label(element.label)
        and _card_geometry_is_plausible(
            element, image_width=image_width, image_height=image_height
        )
    ]
    deduped = suppress_overlapping_cards(
        card_elements,
        iou_threshold=CARD_DUPLICATE_IOU_THRESHOLD,
    )
    deduped.sort(key=lambda el: (el.bbox_xyxy[1], el.bbox_xyxy[0]))
    return deduped


def _encode_bgr_image(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        return b""
    return buf.tobytes()


def _card_elements_from_bytes(image_bytes: bytes):
    result = detect_cards_from_image_bytes(image_bytes, extract_crops=False)
    if result.errors:
        logger.warning("Card detection errors: %s", result.errors)
    return _postprocess_card_elements(
        result.elements,
        image_width=result.image_width,
        image_height=result.image_height,
    )


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
    gray_arr = np.array(gray)
    blurred = cv2.GaussianBlur(gray_arr, (3, 3), 0)
    _, thresholded_arr = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    thresholded = Image.fromarray(thresholded_arr)
    try:
        text = pytesseract.image_to_string(thresholded, lang="eng")
    except Exception as exc:
        logger.warning("Card OCR failed: %s", exc)
        return "unknown"

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown"

    name = re.sub(r"[^A-Za-z0-9 '\-]", "", lines[0])
    return name if len(name) >= 2 else "unknown"


def extract_card_name(crop: np.ndarray) -> str:
    """Backward-compatible wrapper for `extract_card_name_from_crop`."""
    return extract_card_name_from_crop(crop)


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def match_card_name(
    extracted_name: str,
    catalog_names: Sequence[str],
    *,
    min_score: float = CARD_NAME_MATCH_THRESHOLD,
) -> Tuple[Optional[str], float]:
    """Match an OCR name against a card-name catalog using stdlib fuzzy scoring."""
    normalized = _normalize_match_text(extracted_name)
    if not normalized or not catalog_names:
        return None, 0.0

    best_name: Optional[str] = None
    best_score = 0.0
    for catalog_name in catalog_names:
        candidate = catalog_name.strip()
        if not candidate:
            continue
        score = SequenceMatcher(
            None, normalized, _normalize_match_text(candidate)
        ).ratio()
        if score > best_score:
            best_name = candidate
            best_score = score

    if best_name is None or best_score < min_score:
        return None, best_score
    return best_name, best_score


def load_card_catalog_names(path: str | Path) -> List[str]:
    """Load card names from a CSV or JSON catalog for OCR post-processing."""
    catalog_path = Path(path)
    if catalog_path.suffix.lower() == ".json":
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            records = payload.get("cards") or payload.get("data") or []
        else:
            records = payload
        names = []
        for record in records:
            if isinstance(record, str):
                names.append(record)
            elif isinstance(record, dict):
                value = record.get("name") or record.get("card_name")
                if isinstance(value, str):
                    names.append(value)
        return [name.strip() for name in names if name.strip()]

    with catalog_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        name_field = "name" if "name" in reader.fieldnames else reader.fieldnames[0]
        return [
            row[name_field].strip()
            for row in reader
            if row.get(name_field) and row[name_field].strip()
        ]


def identify_card_from_crop(
    crop: np.ndarray,
    *,
    catalog_names: Optional[Sequence[str]] = None,
) -> CardIdentification:
    """Identify a cropped card via OCR, optionally anchored to a known catalog."""
    ocr_name = extract_card_name_from_crop(crop)
    if ocr_name == "unknown":
        return CardIdentification(
            name="unknown",
            source_text="",
            match_score=0.0,
            source="unknown",
        )

    if catalog_names:
        matched_name, score = match_card_name(ocr_name, catalog_names)
        if matched_name is not None:
            return CardIdentification(
                name=matched_name,
                source_text=ocr_name,
                match_score=score,
                source="catalog",
            )

    return CardIdentification(
        name=ocr_name,
        source_text=ocr_name,
        match_score=1.0,
        source="ocr",
    )


def _decode_crop_bytes(crop_bytes: bytes) -> Optional[np.ndarray]:
    decoded = cv2.imdecode(np.frombuffer(crop_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None or decoded.size == 0:
        return None
    return decoded


def _label_for_card_crop(
    crop_bytes: bytes,
    *,
    fallback_index: int,
    catalog_names: Optional[Sequence[str]] = None,
) -> str:
    crop = _decode_crop_bytes(crop_bytes)
    if crop is None:
        return f"card_{fallback_index}"

    identification = identify_card_from_crop(crop, catalog_names=catalog_names)
    if identification.source == "unknown":
        return f"card_{fallback_index}"
    return identification.name


def count_cards_in_image_bytes(image_bytes: bytes) -> int:
    """Analyze image bytes and return the number of detected cards."""
    elements = _card_elements_from_bytes(image_bytes)
    return len(elements)


def extract_card_crops_from_image_bytes(
    image_bytes: bytes,
    *,
    max_crops: Optional[int] = None,
    catalog_names: Optional[Sequence[str]] = None,
) -> List[Tuple[str, bytes]]:
    """Decode an image, detect cards, and return cropped card JPEG bytes.

    Names are best-effort OCR/catalog matches after crop extraction. The generated
    ``card_N`` label is retained when identification is unavailable or low-signal.
    """
    results: List[Tuple[str, bytes]] = []
    analysis = detect_cards_from_image_bytes(
        image_bytes, extract_crops=True, crop_format="jpeg"
    )
    if analysis.errors:
        logger.warning("Card crop errors: %s", analysis.errors)
        return results

    elements = _postprocess_card_elements(
        analysis.elements,
        image_width=analysis.image_width,
        image_height=analysis.image_height,
    )
    for element in elements:
        if max_crops is not None and len(results) >= max_crops:
            break
        if not element.crop_bytes:
            logger.warning("Missing crop bytes for detected card")
            continue
        label = _label_for_card_crop(
            element.crop_bytes,
            fallback_index=len(results) + 1,
            catalog_names=catalog_names,
        )
        results.append((label, element.crop_bytes))

    return results


def process_image(data: bytes) -> List[Tuple[str, bytes]]:
    """Backward-compatible wrapper for `extract_card_crops_from_image_bytes`."""
    return extract_card_crops_from_image_bytes(data)
