"""Data structures for document layout analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


BBox = Tuple[int, int, int, int]
NormalizedBBox = Tuple[float, float, float, float]


@dataclass
class RawDetection:
    """Raw detection output from an object detection model before post-processing."""

    label: str
    confidence: float
    bbox_xyxy: Tuple[float, float, float, float]


@dataclass
class LayoutElement:
    """A single detected layout region."""

    label: str
    confidence: float
    bbox_xyxy: BBox
    bbox_norm: NormalizedBBox
    crop_bytes: Optional[bytes] = None
    crop_mime: Optional[str] = None
    reading_order_hint: Optional[int] = None


@dataclass
class LayoutAnalysisResult:
    """Structured result for layout analysis."""

    image_width: int
    image_height: int
    elements: List[LayoutElement] = field(default_factory=list)
    model_info: Dict[str, object] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
