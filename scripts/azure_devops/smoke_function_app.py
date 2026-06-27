"""Smoke test deployed Azure Functions HTTP endpoints."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


def _endpoint_url(base_url: str, route: str, function_key: str | None) -> str:
    base = base_url.rstrip("/") + "/"
    url = urljoin(base, f"api/{route.lstrip('/')}")
    if function_key:
        return f"{url}?{urlencode({'code': function_key})}"
    return url


def _probe(
    base_url: str, route: str, function_key: str | None, timeout: float
) -> dict[str, Any]:
    url = _endpoint_url(base_url, route, function_key)
    started = perf_counter()
    status = 0
    body = ""
    try:
        request = Request(url, headers={"User-Agent": "trading-card-scanner-smoke/1.0"})
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read(512).decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = exc.code
        body = exc.read(512).decode("utf-8", errors="replace")
    except URLError as exc:
        body = str(exc.reason)
    elapsed_ms = round((perf_counter() - started) * 1000, 2)
    return {"route": route, "status": status, "elapsed_ms": elapsed_ms, "body": body}


def run_smoke(
    base_url: str, function_key: str | None, timeout: float
) -> list[dict[str, Any]]:
    return [
        _probe(base_url, "health", function_key, timeout),
        _probe(base_url, "ready", function_key, timeout),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--function-key")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    results = run_smoke(args.base_url, args.function_key, args.timeout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

    failures = [
        result
        for result in results
        if result["status"] < 200 or result["status"] >= 300
    ]
    if failures:
        for failure in failures:
            print(
                f"{failure['route']} returned HTTP {failure['status']}: {failure['body']}",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
