import json
import logging
import os
from pathlib import Path

import pytest
from azure.storage.blob import BlobServiceClient

from function_app import _upload_processed_cards


SAMPLES = Path(__file__).parent / "Samples"


def _read_sample(name: str) -> bytes:
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"Sample file missing: {path}")
    return path.read_bytes()


def _truthy_env(name: str) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def _output_container_name() -> str:
    return (os.environ.get("TEST_OUTPUT_CONTAINER") or "processed-tests-output").strip()


def _should_delete_output_container() -> bool:
    # Default is off so the container is kept for inspection.
    return _truthy_env("DELETE_TEST_OUTPUT_CONTAINER")


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
    cards = [
        ("Charizard V", _read_sample("sample output 1.jpg")),
        ("Unknown Hero", _read_sample("sample output 2.jpg")),
        ("unknown", _read_sample("sample output 3.jpg")),
    ]

    _upload_processed_cards(container, "folder/My Source.JPG", cards)

    assert [name for name, *_ in container.uploads] == [
        "My Source_1_charizard_v.jpg",
        "My Source_2_unknown_hero.jpg",
        "My Source_3_unknown.jpg",
    ]
    assert all(overwrite for *_, overwrite in container.uploads)
    assert [data for _, data, _ in container.uploads] == [cards[0][1], cards[1][1], cards[2][1]]


def test_upload_processed_cards_logs_and_continues_on_error(caplog):
    container = _FailingFirstUpload()
    first_bytes = _read_sample("sample output 1.jpg")
    second_bytes = _read_sample("sample output 2.jpg")
    cards = [("Card One", first_bytes), ("Card Two", second_bytes)]

    with caplog.at_level(logging.ERROR):
        _upload_processed_cards(container, "input.jpg", cards)

    assert "Failed to upload processed card Card One" in caplog.text
    assert container.uploads == [("input_2_card_two.jpg", second_bytes, True)]


@pytest.mark.integration
def test_upload_processed_cards_writes_blobs_to_storage():
    connection = _get_connection_string()
    if not connection:
        pytest.skip("No real AzureWebJobsStorage connection string found in local.settings.json")

    service_client = BlobServiceClient.from_connection_string(connection)
    container_name = _output_container_name()
    container_client = service_client.get_container_client(container_name)

    try:
        try:
            container_client.create_container()
        except Exception as exc:
            # If it already exists, keep going; otherwise skip.
            if "ContainerAlreadyExists" not in str(exc):
                pytest.skip(f"Could not create/get container for integration test: {exc}")

        cards = [("Cloud Card", _read_sample("sample output 1.jpg")), ("unknown", _read_sample("sample output 2.jpg"))]
        _upload_processed_cards(container_client, "cloud/input.jpg", cards)

        blobs = {blob.name for blob in container_client.list_blobs()}
        assert {"input_1_cloud_card.jpg", "input_2_unknown.jpg"} <= blobs

        downloaded = container_client.download_blob("input_1_cloud_card.jpg").readall()
        assert downloaded == cards[0][1]
    finally:
        if _should_delete_output_container():
            try:
                container_client.delete_container()
            except Exception:
                pass
