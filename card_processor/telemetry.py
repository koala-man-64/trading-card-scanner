"""Small structured logging helpers for Azure Functions diagnostics."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Mapping, Optional


def correlation_id_from_request(req) -> str:
    headers: Mapping[str, str] = getattr(req, "headers", {}) or {}
    for key, value in headers.items():
        if key.lower() == "x-correlation-id" and value.strip():
            return value.strip()
    return uuid.uuid4().hex


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    correlation_id: Optional[str] = None,
    **fields: Any,
) -> None:
    payload = {
        "event": event,
        "correlation_id": correlation_id,
        **fields,
    }
    logger.log(level, json.dumps(payload, default=str, sort_keys=True))
