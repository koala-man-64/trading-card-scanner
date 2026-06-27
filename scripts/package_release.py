"""Build and inspect clean Azure Functions release artifacts."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from typing import Iterable


ALLOWED_FILES = {"function_app.py", "host.json", "requirements.txt"}
ALLOWED_DIRS = {"card_processor", "templates"}
FORBIDDEN_PARTS = {
    ".env",
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "local.settings.json",
    "postman",
    "tests",
    "venv",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo"}


def _is_forbidden(path: Path) -> bool:
    parts = set(path.parts)
    if parts & FORBIDDEN_PARTS:
        return True
    return path.suffix.lower() in FORBIDDEN_SUFFIXES


def iter_package_files(root: Path) -> Iterable[Path]:
    for file_name in sorted(ALLOWED_FILES):
        path = root / file_name
        if path.exists():
            yield path
    for dir_name in sorted(ALLOWED_DIRS):
        directory = root / dir_name
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not _is_forbidden(path.relative_to(root)):
                yield path


def build_release(root: Path, output: Path) -> None:
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in iter_package_files(root):
            rel = path.relative_to(root).as_posix()
            zf.write(path, rel)


def inspect_release(artifact: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(artifact) as zf:
        names = zf.namelist()
    if not names:
        errors.append("artifact is empty")
    for name in names:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            errors.append(f"unsafe archive path: {name}")
        if _is_forbidden(path):
            errors.append(f"forbidden path in artifact: {name}")
    required = {"function_app.py", "host.json", "requirements.txt"}
    missing = required - set(names)
    if missing:
        errors.append("missing required files: " + ", ".join(sorted(missing)))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("artifact", type=Path)
    check = sub.add_parser("check")
    check.add_argument("artifact", type=Path)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    if args.command == "build":
        build_release(root, args.artifact)
        return 0

    errors = inspect_release(args.artifact)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
