"""Write release provenance for an Azure Functions zip artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(artifact: Path, function_app_name: str) -> dict[str, object]:
    requirements = Path("requirements.txt")
    return {
        "artifact_name": artifact.name,
        "artifact_sha256": _sha256(artifact),
        "artifact_size_bytes": artifact.stat().st_size,
        "build_id": os.environ.get("BUILD_BUILDID", ""),
        "build_number": os.environ.get("BUILD_BUILDNUMBER", ""),
        "definition_name": os.environ.get("BUILD_DEFINITIONNAME", ""),
        "function_app_name": function_app_name,
        "python_version": os.environ.get("PYTHON_VERSION", "3.10"),
        "repository_uri": os.environ.get("BUILD_REPOSITORY_URI", ""),
        "source_branch": os.environ.get("BUILD_SOURCEBRANCH", ""),
        "source_sha": os.environ.get("BUILD_SOURCEVERSION", ""),
        "requirements_sha256": _sha256(requirements) if requirements.exists() else "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--function-app-name", required=True)
    args = parser.parse_args(argv)

    manifest = build_manifest(args.artifact, args.function_app_name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
