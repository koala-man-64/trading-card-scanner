"""HTTP, blob, and image input validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

from PIL import Image

from .image_io import inspect_image_bytes
from .settings import ScannerSettings


class RequestValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = "invalid_request",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class LayoutParams:
    model_variant: str
    imgsz: int
    conf: float
    iou: float
    extract_crops: bool
    crop_format: str


@dataclass(frozen=True)
class ProcessParams:
    output_mode: str
    output_format: str


def _get_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def read_bounded_http_body(req, settings: ScannerSettings) -> bytes:
    content_length = _get_header(getattr(req, "headers", {}) or {}, "content-length")
    if content_length:
        try:
            size = int(content_length)
        except ValueError as exc:
            raise RequestValidationError(
                "Content-Length must be an integer",
                status_code=400,
                code="invalid_content_length",
            ) from exc
        if size > settings.max_request_bytes:
            raise RequestValidationError(
                "Request body exceeds the configured limit.",
                status_code=413,
                code="request_too_large",
            )
    body = req.get_body() or b""
    if len(body) > settings.max_request_bytes:
        raise RequestValidationError(
            "Request body exceeds the configured limit.",
            status_code=413,
            code="request_too_large",
        )
    return body


def read_bounded_blob(input_blob, settings: ScannerSettings) -> bytes:
    length = getattr(input_blob, "length", None)
    if isinstance(length, int) and length > settings.max_blob_bytes:
        raise RequestValidationError(
            "Blob exceeds the configured processing limit.",
            status_code=413,
            code="blob_too_large",
        )
    body = input_blob.read()
    if len(body) > settings.max_blob_bytes:
        raise RequestValidationError(
            "Blob exceeds the configured processing limit.",
            status_code=413,
            code="blob_too_large",
        )
    return body


def validate_image_bytes(image_bytes: bytes, settings: ScannerSettings) -> None:
    try:
        info = inspect_image_bytes(image_bytes)
    except ValueError as exc:
        raise RequestValidationError(
            "Invalid image bytes.", status_code=400, code="invalid_image"
        ) from exc
    if info.format_name.lower() not in settings.allowed_image_formats:
        raise RequestValidationError(
            "Unsupported image format.",
            status_code=415,
            code="unsupported_image_format",
        )
    pixels = info.width * info.height
    if pixels > settings.max_image_pixels:
        raise RequestValidationError(
            "Image dimensions exceed the configured pixel limit.",
            status_code=413,
            code="image_too_large",
        )


def parse_bool_param(value: Optional[str], *, default: bool, name: str) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RequestValidationError(f"{name} must be a boolean value.")


def parse_int_param(
    value: Optional[str],
    *,
    default: int,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RequestValidationError(f"{name} must be an integer.") from exc
    if parsed < minimum or parsed > maximum:
        raise RequestValidationError(f"{name} must be between {minimum} and {maximum}.")
    return parsed


def parse_float_param(
    value: Optional[str],
    *,
    default: float,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RequestValidationError(f"{name} must be a number.") from exc
    if parsed < minimum or parsed > maximum:
        raise RequestValidationError(f"{name} must be between {minimum} and {maximum}.")
    return parsed


def parse_choice_param(
    value: Optional[str],
    *,
    default: str,
    name: str,
    allowed: set[str],
) -> str:
    normalized = (value or default).strip().lower()
    if normalized not in allowed:
        allowed_list = ", ".join(sorted(allowed))
        raise RequestValidationError(f"{name} must be one of: {allowed_list}.")
    return normalized


def parse_since_param(value: Optional[str]) -> Optional[datetime]:
    if not value or not value.strip():
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise RequestValidationError("since must be an ISO-8601 datetime.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_layout_params(params: Mapping[str, str]) -> LayoutParams:
    model_id = (params.get("model_id") or "").strip()
    model_variant = (params.get("model_variant") or "").strip()
    if model_id:
        model_variant = model_id
    return LayoutParams(
        model_variant=model_variant,
        imgsz=parse_int_param(
            params.get("imgsz"), default=1280, name="imgsz", minimum=128, maximum=4096
        ),
        conf=parse_float_param(
            params.get("conf"), default=0.25, name="conf", minimum=0.0, maximum=1.0
        ),
        iou=parse_float_param(
            params.get("iou"), default=0.5, name="iou", minimum=0.0, maximum=1.0
        ),
        extract_crops=parse_bool_param(
            params.get("extract_crops"), default=True, name="extract_crops"
        ),
        crop_format=parse_choice_param(
            params.get("crop_format"),
            default="png",
            name="crop_format",
            allowed={"png", "jpeg", "jpg"},
        ),
    )


def parse_process_params(params: Mapping[str, str]) -> ProcessParams:
    output_mode = (params.get("output") or "").strip().lower()
    output_format = (params.get("format") or "").strip().lower()
    if not output_mode:
        output_mode = "return" if output_format else "none"
    aliases = {"bytes": "return", "cloud": "upload", "count": "none"}
    output_mode = aliases.get(output_mode, output_mode)
    if output_mode not in {"none", "return", "upload"}:
        raise RequestValidationError("output must be one of: none, return, upload.")
    if not output_format:
        output_format = "zip"
    if output_format not in {"zip", "json"}:
        raise RequestValidationError("format must be one of: zip, json.")
    if output_mode != "return":
        output_format = "json"
    return ProcessParams(output_mode=output_mode, output_format=output_format)


def normalize_image_format(format_name: str) -> str:
    normalized = format_name.strip().lower()
    return "jpeg" if normalized == "jpg" else normalized


def set_pillow_decompression_limit(settings: ScannerSettings) -> None:
    Image.MAX_IMAGE_PIXELS = settings.max_image_pixels
