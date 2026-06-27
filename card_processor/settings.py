"""Runtime settings for card scanner request and model safety limits."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Set


DEFAULT_MODEL_ID = "Matthieu68857/pokemon-cards-detection"


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


def _parse_csv_set(name: str, default: str) -> Set[str]:
    raw = os.environ.get(name, default)
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _parse_model_ids(name: str, default: str) -> Set[str]:
    raw = os.environ.get(name, default)
    return {item.strip() for item in raw.split(",") if item.strip()}


def _parse_aliases(name: str, default: str) -> Dict[str, str]:
    raw = os.environ.get(name, default)
    aliases: Dict[str, str] = {}
    for pair in raw.split(","):
        cleaned = pair.strip()
        if not cleaned:
            continue
        if "=" not in cleaned:
            raise ValueError(f"{name} entries must use alias=model_id")
        alias, model_id = cleaned.split("=", 1)
        alias = alias.strip().lower()
        model_id = model_id.strip()
        if not alias or not model_id:
            raise ValueError(f"{name} entries must include alias and model_id")
        aliases[alias] = model_id
    return aliases


@dataclass(frozen=True)
class ScannerSettings:
    max_request_bytes: int
    max_blob_bytes: int
    max_image_pixels: int
    max_crops: int
    max_return_bytes: int
    allowed_image_formats: Set[str]
    allowed_model_ids: Set[str]
    model_aliases: Dict[str, str]

    def validate(self) -> list[str]:
        errors: list[str] = []
        allowed_models = set(self.allowed_model_ids)
        alias_targets = set(self.model_aliases.values())
        missing_targets = alias_targets - allowed_models
        if missing_targets:
            errors.append(
                "model aliases reference disallowed model ids: "
                + ", ".join(sorted(missing_targets))
            )
        if not self.allowed_image_formats:
            errors.append("at least one image format must be allowed")
        if not allowed_models:
            errors.append("at least one model id must be allowed")
        return errors


def load_settings() -> ScannerSettings:
    default_aliases = ",".join(
        [
            f"nano={DEFAULT_MODEL_ID}",
            f"small={DEFAULT_MODEL_ID}",
            f"medium={DEFAULT_MODEL_ID}",
        ]
    )
    return ScannerSettings(
        max_request_bytes=_parse_positive_int(
            "CARD_SCANNER_MAX_REQUEST_BYTES", 10 * 1024 * 1024
        ),
        max_blob_bytes=_parse_positive_int(
            "CARD_SCANNER_MAX_BLOB_BYTES", 10 * 1024 * 1024
        ),
        max_image_pixels=_parse_positive_int(
            "CARD_SCANNER_MAX_IMAGE_PIXELS", 25_000_000
        ),
        max_crops=_parse_positive_int("CARD_SCANNER_MAX_CROPS", 100),
        max_return_bytes=_parse_positive_int(
            "CARD_SCANNER_MAX_RETURN_BYTES", 50 * 1024 * 1024
        ),
        allowed_image_formats=_parse_csv_set(
            "CARD_SCANNER_ALLOWED_IMAGE_FORMATS", "jpeg,png,webp"
        ),
        allowed_model_ids=_parse_model_ids(
            "CARD_SCANNER_ALLOWED_MODEL_IDS", DEFAULT_MODEL_ID
        ),
        model_aliases=_parse_aliases("CARD_SCANNER_MODEL_ALIASES", default_aliases),
    )
