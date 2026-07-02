"""Crop extraction helpers for detected cards."""

from __future__ import annotations

from io import BytesIO
from typing import Any, Dict, List, Tuple

from PIL import Image

from .detection_types import DetectedCard


def padded_bbox(
    bbox_xyxy: Tuple[int, int, int, int],
    *,
    image_width: int,
    image_height: int,
    padding_ratio: float = 0.0,
) -> Tuple[int, int, int, int]:
    """Expand an xyxy bbox by a percentage of its own size, clamped to the image."""
    x1, y1, x2, y2 = bbox_xyxy
    if padding_ratio <= 0:
        return bbox_xyxy

    width = max(0, x2 - x1)
    height = max(0, y2 - y1)
    pad_x = int(round(width * padding_ratio))
    pad_y = int(round(height * padding_ratio))

    return (
        max(0, x1 - pad_x),
        max(0, y1 - pad_y),
        min(image_width, x2 + pad_x),
        min(image_height, y2 + pad_y),
    )


def crop_region(
    img: Image.Image,
    bbox_xyxy: Tuple[int, int, int, int],
    *,
    padding_ratio: float = 0.0,
) -> Image.Image:
    x1, y1, x2, y2 = padded_bbox(
        bbox_xyxy,
        image_width=img.width,
        image_height=img.height,
        padding_ratio=padding_ratio,
    )
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
    elements: List[DetectedCard],
    img: Image.Image,
    *,
    crop_format: str = "png",
    padding_ratio: float = 0.0,
) -> List[DetectedCard]:
    """Attach encoded crop bytes to each element."""
    for element in elements:
        crop = crop_region(img, element.bbox_xyxy, padding_ratio=padding_ratio)
        element.crop_bytes, element.crop_mime = encode_image_bytes(
            crop, format=crop_format
        )
    return elements
