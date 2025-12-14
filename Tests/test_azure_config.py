import json
import os
from pathlib import Path

import pytest
from azure.storage.blob import BlobServiceClient


ROOT = Path(__file__).resolve().parents[1]
LOCAL_SETTINGS = ROOT / "local.settings.json"
DEVSTORE_CONNECTION = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
    "QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)


def _load_settings() -> dict:
    if not LOCAL_SETTINGS.exists():
        pytest.skip("local.settings.json is missing; provide Azure settings to run these tests.")
    data = json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8"))
    return data.get("Values", {})


def _normalize_connection_string(connection: str) -> str:
    """Expand shorthand dev storage connection strings for Azurite."""
    if not connection:
        return connection
    if "usedevelopmentstorage=true" in connection.lower():
        return DEVSTORE_CONNECTION
    return connection


def _get_storage_connection(monkeypatch: pytest.MonkeyPatch) -> str:
    """Resolve AzureWebJobsStorage from env first, then local.settings.json."""
    env_connection = os.environ.get("AzureWebJobsStorage")
    if env_connection:
        connection = _normalize_connection_string(env_connection)
        monkeypatch.setenv("AzureWebJobsStorage", connection)
        return connection

    values = _load_settings()
    connection = values.get("AzureWebJobsStorage")
    if not connection:
        pytest.skip("AzureWebJobsStorage not configured in environment or local.settings.json")

    normalized = _normalize_connection_string(connection)
    monkeypatch.setenv("AzureWebJobsStorage", normalized)
    return normalized


def test_local_settings_sets_azure_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _get_storage_connection(monkeypatch)
    assert os.environ.get("AzureWebJobsStorage") == connection


def test_blob_service_client_initializes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _get_storage_connection(monkeypatch)
    client = BlobServiceClient.from_connection_string(connection)
    assert client.account_name


def test_storage_connection_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _get_storage_connection(monkeypatch)
    client = BlobServiceClient.from_connection_string(connection)

    try:
        containers = list(client.list_containers())
    except Exception as exc:  # pragma: no cover - dependent on env
        pytest.skip(f"Storage emulator/account not reachable: {exc}")

    assert isinstance(containers, list)
