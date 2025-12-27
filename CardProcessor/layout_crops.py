"""Crop extraction helpers for layout elements."""

from __future__ import annotations

from io import BytesIO
from typing import List, Tuple

from PIL import Image

from .layout_types import LayoutElement


def crop_region(img: Image.Image, bbox_xyxy: Tuple[int, int, int, int]) -> Image.Image:
    x1, y1, x2, y2 = bbox_xyxy
    return img.crop((x1, y1, x2, y2))


def encode_image_bytes(
    img: Image.Image, *, format: str = "png", quality: int = 90
) -> Tuple[bytes, str]:
    buf = BytesIO()
    save_kwargs = {"format": format.upper()}
    if format.lower() == "jpeg":
        save_kwargs["quality"] = quality
        save_kwargs["optimize"] = True
    img.save(buf, **save_kwargs)
    mime = f"image/{'jpeg' if format.lower() == 'jpeg' else 'png'}"
    return buf.getvalue(), mime


def attach_crops(
    elements: List[LayoutElement],
    img: Image.Image,
    *,
    crop_format: str = "png",
) -> List[LayoutElement]:
    """Attach encoded crop bytes to each element."""
    for element in elements:
        crop = crop_region(img, element.bbox_xyxy)
        element.crop_bytes, element.crop_mime = encode_image_bytes(
            crop, format=crop_format
        )
    return elements

