import json
import os
import zipfile

from scripts.azure_devops.assert_clean_artifacts import inspect_artifacts
from scripts.azure_devops.smoke_function_app import _endpoint_url
from scripts.azure_devops.write_release_manifest import build_manifest


def test_release_manifest_records_artifact_hash_and_source(
    tmp_path, monkeypatch
) -> None:
    artifact = tmp_path / "release.zip"
    with zipfile.ZipFile(artifact, "w") as zf:
        zf.writestr("function_app.py", "# app\n")
    monkeypatch.setenv("BUILD_SOURCEVERSION", "abc123")
    monkeypatch.setenv("BUILD_BUILDID", "42")

    manifest = build_manifest(artifact, "fa-trading-card-scanner-npe")

    assert manifest["function_app_name"] == "fa-trading-card-scanner-npe"
    assert manifest["source_sha"] == "abc123"
    assert manifest["build_id"] == "42"
    artifact_sha = manifest["artifact_sha256"]
    assert isinstance(artifact_sha, str)
    assert len(artifact_sha) == 64


def test_artifact_scan_rejects_local_settings(tmp_path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "local.settings.json").write_text("{}", encoding="utf-8")

    errors = inspect_artifacts(artifact_dir)

    assert errors
    assert "local.settings.json" in errors[0]


def test_smoke_endpoint_appends_function_key() -> None:
    url = _endpoint_url(
        "https://example.azurewebsites.net/",
        "ready",
        "function key with spaces",
    )

    assert (
        url
        == "https://example.azurewebsites.net/api/ready?code=function+key+with+spaces"
    )


def test_deploy_artifact_evidence_shape_is_json_serializable(tmp_path) -> None:
    payload = {
        "function_app_name": "fa-trading-card-scanner-npe",
        "release_source_sha": "abc123",
        "deploy_run_id": "99",
    }
    output = tmp_path / "deploy-provenance.json"
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert "SECRET" not in os.environ
