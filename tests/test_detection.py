import io

import pytest
from PIL import Image

from card_processor.detection_crops import attach_crops
from card_processor.detection_infer import suppress_overlapping_detections
from card_processor.detection_post import clamp_bbox, to_detected_cards
from card_processor.detection_types import DetectedCard, RawDetection
from card_processor.image_io import load_rgb_image


def test_load_rgb_image_invalid_bytes():
    with pytest.raises(ValueError):
        load_rgb_image(b"not an image")


def test_clamp_bbox_and_normalization():
    clamped = clamp_bbox(-5, 10.4, 110, 50, width=100, height=60)
    assert clamped == (0, 10, 100, 50)

    raw = [RawDetection(label="0", confidence=0.9, bbox_xyxy=(-5, 10.4, 110, 50))]
    cards = to_detected_cards(raw, width=100, height=60, class_map={"0": "Card"})
    assert len(cards) == 1
    assert cards[0].bbox_norm == (0.0, 10 / 60, 1.0, 50 / 60)
    assert cards[0].label == "Card"


def test_suppress_overlapping_detections_keeps_highest_confidence():
    high = RawDetection(label="0", confidence=0.9, bbox_xyxy=(0, 0, 10, 10))
    overlapping = RawDetection(label="0", confidence=0.6, bbox_xyxy=(1, 1, 11, 11))
    separate = RawDetection(label="0", confidence=0.5, bbox_xyxy=(100, 100, 110, 110))

    kept = suppress_overlapping_detections([high, overlapping, separate], 0.5)

    assert high in kept
    assert separate in kept
    assert overlapping not in kept


def test_suppress_overlapping_detections_disabled_at_one():
    a = RawDetection(label="0", confidence=0.9, bbox_xyxy=(0, 0, 10, 10))
    b = RawDetection(label="0", confidence=0.6, bbox_xyxy=(1, 1, 11, 11))

    kept = suppress_overlapping_detections([a, b], 1.0)

    assert kept == [a, b]


def test_attach_crops_encodes_bytes():
    img = Image.new("RGB", (20, 10), color="white")
    cards = [
        DetectedCard(
            label="Card",
            confidence=0.9,
            bbox_xyxy=(0, 0, 10, 10),
            bbox_norm=(0, 0, 0, 0),
        )
    ]
    attach_crops(cards, img, crop_format="png")
    assert cards[0].crop_bytes
    assert cards[0].crop_mime == "image/png"
    reopened = Image.open(io.BytesIO(cards[0].crop_bytes))
    assert reopened.size == (10, 10)
