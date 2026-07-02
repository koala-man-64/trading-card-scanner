from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from card_processor import process_utils
from card_processor.detection_types import DetectionResult, DetectedCard


SAMPLES = Path(__file__).parent / "samples"
INPUT_DIR = SAMPLES / "input"
ALL_INPUT_IMAGES = (
    sorted(
        p.name
        for p in INPUT_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if INPUT_DIR.exists()
    else []
)
CORE_INPUT_IMAGES = [
    name
    for name in ("sample_input_1.jpg", "sample_input_2.jpg")
    if (INPUT_DIR / name).exists()
]
if not CORE_INPUT_IMAGES:
    CORE_INPUT_IMAGES = ALL_INPUT_IMAGES[:2] or ["sample_input_1.jpg"]


def _read_input_sample(name: str) -> bytes:
    path = INPUT_DIR / name
    if not path.exists():
        pytest.skip(f"Sample file missing: {path}")
    return path.read_bytes()


def _jpeg_bytes(width: int = 40, height: int = 60) -> bytes:
    image = Image.new("RGB", (width, height), color="white")
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR))
    assert ok
    return buf.tobytes()


@pytest.mark.parametrize("sample_name", CORE_INPUT_IMAGES)
def test_process_image_returns_crops_and_valid_bytes(sample_name: str):
    data = _read_input_sample(sample_name)
    crops = process_utils.extract_card_crops_from_image_bytes(data)

    assert crops, "Expected at least one cropped card"
    name, img_bytes = crops[0]

    assert isinstance(name, str)
    assert isinstance(img_bytes, (bytes, bytearray))

    decoded = cv2.imdecode(np.frombuffer(img_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None and decoded.size > 0


@pytest.mark.parametrize("sample_name", CORE_INPUT_IMAGES)
def test_detect_cards_finds_boxes_in_sample_image(sample_name: str):
    data = _read_input_sample(sample_name)
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None, "Failed to decode sample image"

    boxes = process_utils.detect_card_boxes(img)
    assert boxes, "Expected at least one detected card"

    x, y, w, h = boxes[0]
    assert w > 0 and h > 0


@pytest.mark.parametrize(
    "sample_name", ALL_INPUT_IMAGES or CORE_INPUT_IMAGES or ["sample_input_1.jpg"]
)
def test_extract_card_crops_handles_input_samples(sample_name: str):
    data = _read_input_sample(sample_name)
    crops = process_utils.extract_card_crops_from_image_bytes(data)

    assert isinstance(crops, list)
    for name, img_bytes in crops:
        assert isinstance(name, str)
        assert isinstance(img_bytes, (bytes, bytearray))


def test_extract_card_crops_filters_implausible_and_duplicate_elements(monkeypatch):
    crop = _jpeg_bytes()
    result = DetectionResult(
        image_width=200,
        image_height=200,
        elements=[
            DetectedCard(
                label="Card",
                confidence=0.95,
                bbox_xyxy=(20, 20, 80, 110),
                bbox_norm=(0, 0, 0, 0),
                crop_bytes=crop,
                crop_mime="image/jpeg",
            ),
            DetectedCard(
                label="Card",
                confidence=0.50,
                bbox_xyxy=(22, 22, 82, 112),
                bbox_norm=(0, 0, 0, 0),
                crop_bytes=crop,
                crop_mime="image/jpeg",
            ),
            DetectedCard(
                label="Card",
                confidence=0.99,
                bbox_xyxy=(0, 0, 200, 20),
                bbox_norm=(0, 0, 0, 0),
                crop_bytes=crop,
                crop_mime="image/jpeg",
            ),
        ],
        model_info={},
        errors=[],
    )
    monkeypatch.setattr(
        process_utils,
        "detect_cards_from_image_bytes",
        lambda *_, **__: result,
    )

    crops = process_utils.extract_card_crops_from_image_bytes(b"image")

    assert crops == [("card_1", crop)]


def test_identify_card_from_crop_matches_ocr_to_catalog(monkeypatch):
    class FakeTesseract:
        @staticmethod
        def image_to_string(*args, **kwargs):
            return "Pikchu\n"

    monkeypatch.setattr(process_utils, "pytesseract", FakeTesseract)
    crop = np.full((80, 60, 3), 255, dtype=np.uint8)

    identification = process_utils.identify_card_from_crop(
        crop,
        catalog_names=["Pikachu", "Charizard"],
    )

    assert identification.name == "Pikachu"
    assert identification.source == "catalog"
    assert identification.match_score >= process_utils.CARD_NAME_MATCH_THRESHOLD
