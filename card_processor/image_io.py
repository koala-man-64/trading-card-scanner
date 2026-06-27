"""Image decoding helpers for layout analysis."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import cast

from PIL import Image


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    format_name: str


def inspect_image_bytes(image_bytes: bytes) -> ImageInfo:
    """Inspect image metadata without returning decoded pixel data."""
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            format_name = (img.format or "").lower()
            if not format_name:
                raise ValueError("Unknown image format")
            return ImageInfo(
                width=int(img.width),
                height=int(img.height),
                format_name="jpeg" if format_name == "jpg" else format_name,
            )
    except Exception as exc:
        raise ValueError("Invalid image bytes") from exc


def load_rgb_image(image_bytes: bytes) -> Image.Image:
    """Decode image bytes into an RGB PIL Image."""
    try:
        img = cast(Image.Image, Image.open(BytesIO(image_bytes)))
    except Exception as exc:
        raise ValueError("Invalid image bytes") from exc

    if img.mode != "RGB":
        img = img.convert("RGB")
    return img
