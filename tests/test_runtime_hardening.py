import io
from dataclasses import replace
from typing import Any

import pytest
from PIL import Image

from card_processor.image_io import inspect_image_bytes, load_rgb_image
from card_processor.detection_model import ModelResolutionError, resolve_model_id
from card_processor.request_validation import (
    RequestValidationError,
    validate_image_bytes,
)
from card_processor.settings import DEFAULT_MODEL_ID, ScannerSettings, load_settings


def _settings(**overrides: Any) -> ScannerSettings:
    return replace(load_settings(), **overrides)


def _image_bytes(format_name: str = "PNG", size: tuple[int, int] = (10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color="white").save(buf, format=format_name)
    return buf.getvalue()


def _heic_bytes() -> bytes:
    return _image_bytes("HEIF")


def test_heic_image_bytes_can_be_inspected_and_decoded() -> None:
    image_bytes = _heic_bytes()

    info = inspect_image_bytes(image_bytes)
    image = load_rgb_image(image_bytes)

    assert info.format_name in {"heic", "heif"}
    assert (info.width, info.height) == (10, 10)
    assert image.mode == "RGB"


def test_validate_image_bytes_accepts_heic_by_default() -> None:
    validate_image_bytes(_heic_bytes(), load_settings())


def test_validate_image_bytes_rejects_disallowed_format() -> None:
    settings = _settings(allowed_image_formats={"jpeg"})

    with pytest.raises(RequestValidationError) as exc:
        validate_image_bytes(_image_bytes("PNG"), settings)

    assert exc.value.status_code == 415


def test_validate_image_bytes_rejects_pixel_limit() -> None:
    settings = _settings(max_image_pixels=10)

    with pytest.raises(RequestValidationError) as exc:
        validate_image_bytes(_image_bytes("PNG", (10, 10)), settings)

    assert exc.value.status_code == 413


def test_model_resolution_rejects_unknown_model() -> None:
    settings = _settings(
        allowed_model_ids={DEFAULT_MODEL_ID},
        model_aliases={"nano": DEFAULT_MODEL_ID},
    )

    with pytest.raises(ModelResolutionError):
        resolve_model_id("someone/else", settings)


def test_model_resolution_allows_alias() -> None:
    settings = _settings(
        allowed_model_ids={DEFAULT_MODEL_ID},
        model_aliases={"nano": DEFAULT_MODEL_ID},
    )

    assert resolve_model_id("nano", settings) == DEFAULT_MODEL_ID
