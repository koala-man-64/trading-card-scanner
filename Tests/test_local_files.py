from pathlib import Path

import cv2
import numpy as np
import pytest

from CardProcessor import process_utils


SAMPLES = Path(__file__).parent / "Samples"


def _read_sample(name: str) -> bytes:
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"Sample file missing: {path}")
    return path.read_bytes()


def test_process_image_returns_crops_and_valid_bytes():
    data = _read_sample("sample input 1.jpg")
    crops = process_utils.process_image(data)

    assert crops, "Expected at least one cropped card"
    name, img_bytes = crops[0]

    assert isinstance(name, str)
    assert isinstance(img_bytes, (bytes, bytearray))

    decoded = cv2.imdecode(np.frombuffer(img_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None and decoded.size > 0


def test_detect_cards_finds_boxes_in_sample_image():
    data = _read_sample("sample input 1.jpg")
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)

    boxes = process_utils.detect_cards(img)
    assert boxes, "Expected at least one detected card"

    x, y, w, h = boxes[0]
    assert w > 0 and h > 0
