from pathlib import Path

import pytest

from CardProcessor import process_utils


FIXTURES = Path(__file__).parent / "fixtures"


def _read_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_process_image_returns_crops():
    data = _read_bytes("Tests\Samples\sample input 1.jpg")
    crops = process_utils.process_image(data)

    assert isinstance(crops, list)
    assert len(crops) > 0

    # Each item is (name, jpeg_bytes)
    name, img_bytes = crops[0]
    assert isinstance(name, str)
    assert isinstance(img_bytes, (bytes, bytearray))
    assert len(img_bytes) > 1000


def test_detect_cards_finds_boxes():
    import cv2
    import numpy as np

    data = _read_bytes("sample_input_1.jpg")
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)

    boxes = process_utils.detect_cards(img)
    assert len(boxes) > 0
    # box shape: (x, y, w, h)
    x, y, w, h = boxes[0]
    assert w > 0 and h > 0
