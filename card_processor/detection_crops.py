"""Crop extraction helpers for detected cards."""

from __future__ import annotations

from io import BytesIO
from typing import Any, Dict, List, Tuple

from PIL import Image

from .detection_types import DetectedCard


def crop_region(img: Image.Image, bbox_xyxy: Tuple[int, int, int, int]) -> Image.Image:
    x1, y1, x2, y2 = bbox_xyxy
    return img.crop((x1, y1, x2, y2))


def encode_image_bytes(
    img: Image.Image, *, format: str = "png", quality: int = 90
) -> Tuple[bytes, str]:
    buf = BytesIO()
    save_kwargs: Dict[str, Any] = {"format": format.upper()}
    if format.lower() == "jpeg":
        save_kwargs["quality"] = quality
        save_kwargs["optimize"] = True
    img.save(buf, **save_kwargs)
    mime = f"image/{'jpeg' if format.lower() == 'jpeg' else 'png'}"
    return buf.getvalue(), mime


def attach_crops(
    cards: List[DetectedCard],
    img: Image.Image,
    *,
    crop_format: str = "png",
) -> List[DetectedCard]:
    """Attach encoded crop bytes to each detected card."""
    for card in cards:
        crop = crop_region(img, card.bbox_xyxy)
        card.crop_bytes, card.crop_mime = encode_image_bytes(crop, format=crop_format)
    return cards
