from pathlib import Path

import cv2
import numpy as np
import pytest

from CardProcessor import process_utils


SAMPLES = Path(__file__).parent / "Samples"
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
    for name in ("sample input 1.jpg", "sample input 2.jpg")
    if (INPUT_DIR / name).exists()
]
if not CORE_INPUT_IMAGES:
    CORE_INPUT_IMAGES = ALL_INPUT_IMAGES[:2] or ["sample input 1.jpg"]


def _read_input_sample(name: str) -> bytes:
    path = INPUT_DIR / name
    if not path.exists():
        pytest.skip(f"Sample file missing: {path}")
    return path.read_bytes()


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
    "sample_name", ALL_INPUT_IMAGES or CORE_INPUT_IMAGES or ["sample input 1.jpg"]
)
def test_extract_card_crops_handles_input_samples(sample_name: str):
    data = _read_input_sample(sample_name)
    crops = process_utils.extract_card_crops_from_image_bytes(data)

    assert isinstance(crops, list)
    for name, img_bytes in crops:
        assert isinstance(name, str)
        assert isinstance(img_bytes, (bytes, bytearray))
