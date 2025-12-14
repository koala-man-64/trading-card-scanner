import json
import logging
import os
from pathlib import Path
from uuid import uuid4

import pytest
from azure.storage.blob import BlobServiceClient

from function_app import _upload_processed_cards


class _StubContainer:
    def __init__(self) -> None:
        self.uploads = []

    def upload_blob(self, name, data, overwrite):  # noqa: WPS110
        self.uploads.append((name, data, overwrite))


class _FailingFirstUpload:
    def __init__(self) -> None:
        self.calls = 0
        self.uploads = []

    def upload_blob(self, name, data, overwrite):  # noqa: WPS110
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient failure")
        self.uploads.append((name, data, overwrite))


def _load_local_settings_into_env() -> None:
    settings_path = Path(__file__).resolve().parents[1] / "local.settings.json"
    if not settings_path.exists():
        return

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return

    values = settings.get("Values", {})
    for key, value in values.items():
        if not isinstance(value, str):
            continue
        if key in {"AzureWebJobsStorage", "STORAGE_ACCOUNT_NAME"}:
            os.environ[key] = value
            continue
        if key not in os.environ:
            os.environ[key] = value


def _get_connection_string() -> str:
    _load_local_settings_into_env()
    value = os.environ.get("AzureWebJobsStorage") or ""
    if "usedevelopmentstorage=true" in value.lower():
        return ""
    return value


def test_upload_processed_cards_builds_names_and_uploads():
    container = _StubContainer()
    cards = [("Charizard V", b"img1"), ("Unknown Hero", b"img2"), ("unknown", b"img3")]

    _upload_processed_cards(container, "folder/My Source.JPG", cards)

    assert [name for name, *_ in container.uploads] == [
        "My Source_1_charizard_v.jpg",
        "My Source_2_unknown_hero.jpg",
        "My Source_3_unknown.jpg",
    ]
    assert all(overwrite for *_, overwrite in container.uploads)
    assert [data for _, data, _ in container.uploads] == [b"img1", b"img2", b"img3"]


def test_upload_processed_cards_logs_and_continues_on_error(caplog):
    container = _FailingFirstUpload()
    cards = [("Card One", b"one"), ("Card Two", b"two")]

    with caplog.at_level(logging.ERROR):
        _upload_processed_cards(container, "input.jpg", cards)

    assert "Failed to upload processed card Card One" in caplog.text
    assert container.uploads == [("input_2_card_two.jpg", b"two", True)]


@pytest.mark.integration
def test_upload_processed_cards_writes_blobs_to_storage():
    connection = _get_connection_string()
    if not connection:
        pytest.skip("No real AzureWebJobsStorage connection string found in local.settings.json")

    service_client = BlobServiceClient.from_connection_string(connection)
    container_name = f"processed-tests-{uuid4().hex[:8]}"
    container_client = service_client.get_container_client(container_name)

    container_created = False
    try:
        try:
            container_client.create_container()
            container_created = True
        except Exception as exc:
            pytest.skip(f"Could not create container for integration test: {exc}")

        cards = [("Cloud Card", b"cloud-bytes"), ("unknown", b"mystery")]
        _upload_processed_cards(container_client, "cloud/input.jpg", cards)

        blobs = {blob.name for blob in container_client.list_blobs()}
        assert {"input_1_cloud_card.jpg", "input_2_unknown.jpg"} <= blobs

        downloaded = container_client.download_blob("input_1_cloud_card.jpg").readall()
        assert downloaded == b"cloud-bytes"
    finally:
        if container_created:
            try:
                container_client.delete_container()
            except Exception:
                pass
