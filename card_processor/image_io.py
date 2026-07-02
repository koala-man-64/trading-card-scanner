"""Image decoding helpers for card detection."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import cast

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener


register_heif_opener()


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    format_name: str


def inspect_image_bytes(image_bytes: bytes) -> ImageInfo:
    """Inspect image metadata without returning decoded pixel data."""
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            format_name = _normalize_format_name(img.format or "")
            if not format_name:
                raise ValueError("Unknown image format")
            return ImageInfo(
                width=int(img.width),
                height=int(img.height),
                format_name=format_name,
            )
    except Exception as exc:
        raise ValueError("Invalid image bytes") from exc


def _normalize_format_name(format_name: str) -> str:
    normalized = format_name.strip().lower()
    if normalized == "jpg":
        return "jpeg"
    return normalized


def load_rgb_image(image_bytes: bytes) -> Image.Image:
    """Decode image bytes into an RGB PIL Image."""
    try:
        img = cast(Image.Image, Image.open(BytesIO(image_bytes)))
    except Exception as exc:
        raise ValueError("Invalid image bytes") from exc

    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img
