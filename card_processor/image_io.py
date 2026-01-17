"""Image decoding helpers for layout analysis."""

from __future__ import annotations

from io import BytesIO
from typing import cast

from PIL import Image


def load_rgb_image(image_bytes: bytes) -> Image.Image:
    """Decode image bytes into an RGB PIL Image."""
    try:
        img = cast(Image.Image, Image.open(BytesIO(image_bytes)))
    except Exception as exc:
        raise ValueError("Invalid image bytes") from exc

    if img.mode != "RGB":
        img = img.convert("RGB")
    return img
