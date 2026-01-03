import os

import pytest
from azure.storage.blob import BlobServiceClient

from .helpers import get_storage_connection


def test_local_settings_sets_azure_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = get_storage_connection(monkeypatch)
    assert os.environ.get("AZURE_STORAGE_CONNECTION_STRING") == connection


def test_blob_service_client_initializes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = get_storage_connection(monkeypatch)
    client = BlobServiceClient.from_connection_string(connection)
    assert client.account_name


def test_storage_connection_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = get_storage_connection(monkeypatch)
    client = BlobServiceClient.from_connection_string(connection)

    try:
        containers = list(client.list_containers())
    except Exception as exc:  # pragma: no cover - dependent on env
        pytest.skip(f"Storage emulator/account not reachable: {exc}")

    assert isinstance(containers, list)
