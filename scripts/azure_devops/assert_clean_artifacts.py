"""Fail if pipeline evidence artifacts contain local config or obvious secrets."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


FORBIDDEN_NAMES = {
    ".env",
    "local.settings.json",
    "local.settings.development.json",
    "venv",
    ".venv",
    "__pycache__",
}
SECRET_ASSIGNMENT = re.compile(
    rb"(?i)(client[_-]?secret|secret[_-]?key|api[_-]?key|password|token)\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{12,})"
)
ALLOWLISTED_REPORT_FIELDS = {
    b'"Secret":',
    b'"secret":',
    b'"Match":',
    b'"match":',
    b'"Token":',
    b'"token":',
}


def _contains_secret_assignment(data: bytes) -> bool:
    for match in SECRET_ASSIGNMENT.finditer(data):
        prefix = data[max(0, match.start() - 32) : match.start()]
        if any(field in prefix for field in ALLOWLISTED_REPORT_FIELDS):
            continue
        if b"REDACTED" in match.group(0).upper():
            continue
        return True
    return False


def inspect_artifacts(root: Path) -> list[str]:
    errors: list[str] = []
    if not root.exists():
        return [f"artifact path does not exist: {root}"]
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        lowered_parts = {part.lower() for part in rel.parts}
        if lowered_parts & FORBIDDEN_NAMES:
            errors.append(f"forbidden artifact path: {rel.as_posix()}")
            continue
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            errors.append(f"could not read artifact {rel.as_posix()}: {exc}")
            continue
        if _contains_secret_assignment(data):
            errors.append(f"possible secret assignment in artifact: {rel.as_posix()}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)

    errors = inspect_artifacts(args.root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
