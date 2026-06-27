"""Azure Functions response helpers with correlation-aware JSON errors."""

from __future__ import annotations

import json
from typing import Mapping, Optional

import azure.functions as func


def _headers(
    correlation_id: Optional[str], extra: Optional[Mapping[str, str]] = None
) -> dict[str, str]:
    headers = dict(extra or {})
    if correlation_id:
        headers["x-correlation-id"] = correlation_id
    return headers


def json_response(
    payload: object,
    *,
    status_code: int = 200,
    correlation_id: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
        headers=_headers(correlation_id, headers),
    )


def error_response(
    message: str,
    *,
    status_code: int,
    code: str,
    correlation_id: Optional[str],
) -> func.HttpResponse:
    return json_response(
        {
            "error": {
                "code": code,
                "message": message,
                "correlation_id": correlation_id,
            }
        },
        status_code=status_code,
        correlation_id=correlation_id,
    )
