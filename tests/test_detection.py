import io
from types import SimpleNamespace

import pytest
from PIL import Image

from card_processor import detection
from card_processor.image_io import load_rgb_image
from card_processor.detection_crops import attach_crops, padded_bbox
from card_processor.detection_post import (
    bbox_iou,
    clamp_bbox,
    suppress_overlapping_cards,
    to_detected_cards,
)
from card_processor.detection_types import DetectedCard, RawDetection


def test_load_rgb_image_invalid_bytes():
    with pytest.raises(ValueError):
        load_rgb_image(b"not an image")


def _png_bytes(width: int = 100, height: int = 100) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="white").save(buf, format="PNG")
    return buf.getvalue()


def test_clamp_bbox_and_normalization():
    clamped = clamp_bbox(-5, 10.4, 110, 50, width=100, height=60)
    assert clamped == (0, 10, 100, 50)

    raw = [RawDetection(label="0", confidence=0.9, bbox_xyxy=(-5, 10.4, 110, 50))]
    elements = to_detected_cards(raw, width=100, height=60, class_map={"0": "Text"})
    assert len(elements) == 1
    assert elements[0].bbox_norm == (0.0, 10 / 60, 1.0, 50 / 60)
    assert elements[0].label == "Text"


def test_suppress_overlapping_cards_keeps_highest_confidence_per_label():
    elements = [
        DetectedCard(
            label="Card",
            confidence=0.95,
            bbox_xyxy=(0, 0, 100, 100),
            bbox_norm=(0, 0, 1, 1),
        ),
        DetectedCard(
            label="Card",
            confidence=0.60,
            bbox_xyxy=(5, 5, 105, 105),
            bbox_norm=(0, 0, 1, 1),
        ),
        DetectedCard(
            label="Text",
            confidence=0.50,
            bbox_xyxy=(5, 5, 105, 105),
            bbox_norm=(0, 0, 1, 1),
        ),
    ]

    filtered = suppress_overlapping_cards(elements, iou_threshold=0.5)

    assert [element.label for element in filtered] == ["Card", "Text"]
    assert filtered[0].confidence == 0.95
    assert bbox_iou(elements[0].bbox_xyxy, elements[1].bbox_xyxy) > 0.5


def test_analyze_layout_applies_iou_nms(monkeypatch):
    fake_model = SimpleNamespace(config=SimpleNamespace(id2label={0: "Card"}))
    fake_bundle = SimpleNamespace(
        model=fake_model,
        processor=object(),
        device="cpu",
        model_id="test-model",
    )
    monkeypatch.setattr(detection, "get_model", lambda *_, **__: fake_bundle)
    monkeypatch.setattr(
        detection,
        "infer_detections",
        lambda *_, **__: [
            RawDetection(label="0", confidence=0.95, bbox_xyxy=(0, 0, 80, 80)),
            RawDetection(label="0", confidence=0.50, bbox_xyxy=(5, 5, 85, 85)),
        ],
    )

    result = detection.detect_cards_from_image_bytes(
        _png_bytes(),
        iou=0.5,
        extract_crops=False,
    )

    assert len(result.elements) == 1
    assert result.elements[0].confidence == 0.95
    assert result.model_info["iou"] == 0.5


def test_attach_crops_encodes_bytes():
    img = Image.new("RGB", (20, 10), color="white")
    elements = [
        DetectedCard(
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


def test_attach_crops_can_pad_within_image_bounds():
    img = Image.new("RGB", (20, 20), color="white")
    elements = [
        DetectedCard(
            label="Picture",
            confidence=0.9,
            bbox_xyxy=(5, 5, 15, 15),
            bbox_norm=(0, 0, 0, 0),
        )
    ]

    assert padded_bbox(
        elements[0].bbox_xyxy,
        image_width=20,
        image_height=20,
        padding_ratio=0.1,
    ) == (4, 4, 16, 16)

    attach_crops(elements, img, crop_format="png", padding_ratio=0.1)
    reopened = Image.open(io.BytesIO(elements[0].crop_bytes))
    assert reopened.size == (12, 12)
