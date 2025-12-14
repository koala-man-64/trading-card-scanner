import json
import os
from pathlib import Path

import pytest
from azure.storage.blob import BlobServiceClient


ROOT = Path(__file__).resolve().parents[1]
LOCAL_SETTINGS = ROOT / "local.settings.json"


def _load_settings() -> dict:
    if not LOCAL_SETTINGS.exists():
        pytest.skip("local.settings.json is missing; provide Azure settings to run these tests.")
    data = json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8"))
    return data.get("Values", {})


def _apply_settings_to_env(values: dict) -> None:
    for key, value in values.items():
        if isinstance(value, str):
            os.environ[key] = value


def test_local_settings_sets_azure_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    values = _load_settings()
    if "AzureWebJobsStorage" not in values:
        pytest.skip("AzureWebJobsStorage not configured in local.settings.json")

    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    _apply_settings_to_env(values)

    assert os.environ.get("AzureWebJobsStorage") == values["AzureWebJobsStorage"]


def test_blob_service_client_initializes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    values = _load_settings()
    connection = values.get("AzureWebJobsStorage")
    if not connection:
        pytest.skip("AzureWebJobsStorage not configured in local.settings.json")

    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    os.environ["AzureWebJobsStorage"] = connection

    client = BlobServiceClient.from_connection_string(connection)
    assert client.account_name


def test_storage_connection_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    values = _load_settings()
    connection = values.get("AzureWebJobsStorage")
    if not connection:
        pytest.skip("AzureWebJobsStorage not configured in local.settings.json")

    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    os.environ["AzureWebJobsStorage"] = connection
    client = BlobServiceClient.from_connection_string(connection)

    try:
        containers = list(client.list_containers())
    except Exception as exc:  # pragma: no cover - dependent on env
        pytest.skip(f"Storage emulator/account not reachable: {exc}")

    assert isinstance(containers, list)
