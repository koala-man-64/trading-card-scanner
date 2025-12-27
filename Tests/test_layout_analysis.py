import io

import pytest
from PIL import Image

from CardProcessor.image_io import load_rgb_image
from CardProcessor.layout_crops import attach_crops
from CardProcessor.layout_post import assign_reading_order, clamp_bbox, to_layout_elements
from CardProcessor.layout_types import LayoutElement, RawDetection


def test_load_rgb_image_invalid_bytes():
    with pytest.raises(ValueError):
        load_rgb_image(b"not an image")


def test_clamp_bbox_and_normalization():
    clamped = clamp_bbox(-5, 10.4, 110, 50, width=100, height=60)
    assert clamped == (0, 10, 100, 50)

    raw = [RawDetection(label="0", confidence=0.9, bbox_xyxy=(-5, 10.4, 110, 50))]
    elements = to_layout_elements(raw, width=100, height=60, class_map={"0": "Text"})
    assert len(elements) == 1
    assert elements[0].bbox_norm == (0.0, 10 / 60, 1.0, 50 / 60)
    assert elements[0].label == "Text"


def test_assign_reading_order():
    elements = [
        LayoutElement(
            label="Text",
            confidence=0.9,
            bbox_xyxy=(0, 10, 5, 20),
            bbox_norm=(0, 0, 0, 0),
        ),
        LayoutElement(
            label="Title",
            confidence=0.9,
            bbox_xyxy=(0, 0, 5, 5),
            bbox_norm=(0, 0, 0, 0),
        ),
    ]
    assign_reading_order(elements)
    assert elements[0].reading_order_hint == 1
    assert elements[1].reading_order_hint == 0


def test_attach_crops_encodes_bytes():
    img = Image.new("RGB", (20, 10), color="white")
    elements = [
        LayoutElement(
            label="Picture",
            confidence=0.9,
            bbox_xyxy=(0, 0, 10, 10),
            bbox_norm=(0, 0, 0, 0),
        )
    ]
    attach_crops(elements, img, crop_format="png")
    assert elements[0].crop_bytes
    assert elements[0].crop_mime == "image/png"
    reopened = Image.open(io.BytesIO(elements[0].crop_bytes))
    assert reopened.size == (10, 10)

