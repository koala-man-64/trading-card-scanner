"""
Upload tests for processed trading card images.

There are two categories of tests in this file:

1) Unit-style tests (no Azure access):
   - Use in-memory stub "container clients" that record upload calls.
   - Validate naming, overwrite behavior, and error handling.

2) Integration tests (real Azure Blob Storage):
   - Require `AZURE_STORAGE_CONNECTION_STRING` (preferred) or a value in
     `local.settings.json` under `Values.AZURE_STORAGE_CONNECTION_STRING`.
   - Write blobs to real containers so you can inspect output.

Environment knobs for integration tests:
  - `TEST_OUTPUT_CONTAINER` (default: `processed-tests-output`):
      Container used by `test_upload_processed_cards_writes_blobs_to_storage`.
  - `TEST_CARD_FOLDER` (default: `test-card-folder`):
      "Folder" prefix used inside the `input` container for parsing-result uploads.
  - `DELETE_TEST_OUTPUT_CONTAINER` (default: off):
      When truthy, cleanup runs (delete container or delete uploaded prefix).
"""

import json
import logging
import os
from pathlib import Path

import pytest
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient

import function_app
from CardProcessor import process_utils
from function_app import _upload_processed_cards


SAMPLES = Path(__file__).parent / "Samples"
ROOT = Path(__file__).resolve().parents[1]
LOCAL_SETTINGS = ROOT / "local.settings.json"
# Azure Storage emulator (Azurite) uses a well-known connection string. This expands
# the shorthand `UseDevelopmentStorage=true` into explicit endpoints so the
# `azure-storage-blob` SDK can connect reliably.
DEVSTORE_CONNECTION = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
    "QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)


def _read_sample(name: str) -> bytes:
    # Test fixtures are real JPEG files under `Tests/Samples`.
    # These are used instead of placeholder bytes to better mimic production.
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"Sample file missing: {path}")
    return path.read_bytes()


def _truthy_env(name: str) -> bool:
    # Common "truthy" parsing for environment variable toggles.
    value = (os.environ.get(name) or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def _output_container_name() -> str:
    # Container name for the processed-card integration upload test.
    return (os.environ.get("TEST_OUTPUT_CONTAINER") or "processed-tests-output").strip()


def _should_delete_output_container() -> bool:
    # Default is off so the container is kept for inspection.
    return _truthy_env("DELETE_TEST_OUTPUT_CONTAINER")


class _StubContainer:
    # A minimal stand-in for `azure.storage.blob.ContainerClient`.
    # It records calls made by `_upload_processed_cards` so we can assert on them.
    def __init__(self) -> None:
        self.uploads = []

    def upload_blob(self, name, data, overwrite):  # noqa: WPS110
        self.uploads.append((name, data, overwrite))


class _FailingFirstUpload:
    # Like `_StubContainer`, but raises an exception on the first upload to verify
    # that `_upload_processed_cards` logs and continues with later cards.
    def __init__(self) -> None:
        self.calls = 0
        self.uploads = []

    def upload_blob(self, name, data, overwrite):  # noqa: WPS110
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient failure")
        self.uploads.append((name, data, overwrite))


def _load_settings() -> dict:
    # Read `local.settings.json` (Azure Functions local dev convention) and return
    # its `Values` dict. This supports UTF-8 with BOM (common on Windows).
    if not LOCAL_SETTINGS.exists():
        return {}
    try:
        data = json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data.get("Values", {})


def _normalize_connection_string(connection: str) -> str:
    # If the connection string is `UseDevelopmentStorage=true`, expand it for Azurite.
    if not connection:
        return connection
    if "usedevelopmentstorage=true" in connection.lower():
        return DEVSTORE_CONNECTION
    return connection


def _get_storage_connection() -> str:
    # Prefer the standard Azure SDK env var:
    #   AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=...;AccountName=...;AccountKey=...;...
    # Fallback to `local.settings.json` so tests can run without exporting env vars.
    env_connection = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if env_connection:
        connection = _normalize_connection_string(env_connection)
        os.environ["AZURE_STORAGE_CONNECTION_STRING"] = connection
        return connection

    values = _load_settings()
    connection = values.get("AZURE_STORAGE_CONNECTION_STRING") or ""
    if not connection:
        return ""

    normalized = _normalize_connection_string(connection)
    os.environ["AZURE_STORAGE_CONNECTION_STRING"] = normalized
    return normalized


def _card_folder_name() -> str:
    # "Folder" is represented in Blob Storage as a blob-name prefix.
    # This returns the prefix name to use under the `input` container.
    folder = (os.environ.get("TEST_CARD_FOLDER") or "test-card-folder").strip().strip("/").strip("\\")
    return folder or "test-card-folder"


def _delete_prefix(container_client, prefix: str) -> None:
    # Best-effort delete of blobs under a prefix. Used for optional cleanup.
    failures = []
    for blob in container_client.list_blobs(name_starts_with=prefix):
        try:
            container_client.get_blob_client(blob.name).delete_blob()
        except Exception as exc:
            failures.append((blob.name, exc))

    if failures:
        details = "; ".join(f"{name}: {exc}" for name, exc in failures)
        raise RuntimeError(f"Failed to delete {len(failures)} blobs under prefix '{prefix}': {details}")


def test_upload_processed_cards_builds_names_and_uploads():
    # Unit test: verify blob naming rules, overwrite behavior, and byte passthrough.
    #
    # This test does not talk to Azure: `_StubContainer` captures upload calls.
    container = _StubContainer()
    # Use real JPEG bytes from `Tests/Samples` to simulate actual images.
    cards = [
        ("Charizard V", _read_sample("sample output 1.jpg")),
        ("Unknown Hero", _read_sample("sample output 2.jpg")),
        ("unknown", _read_sample("sample output 3.jpg")),
    ]
    # The production naming code uses the basename of `source_name`, so we point at
    # a real sample input file to avoid "fake" paths.
    source_path = str(SAMPLES / "sample input 1.jpg")

    _upload_processed_cards(container, source_path, cards)

    # Ensure the blob names are deterministic and sanitized.
    assert [name for name, *_ in container.uploads] == [
        "sample input 1_1.jpg",
        "sample input 1_2.jpg",
        "sample input 1_3.jpg",
    ]
    # `_upload_processed_cards` always sets overwrite=True so reruns replace blobs.
    assert all(overwrite for *_, overwrite in container.uploads)
    # Uploaded content should match the card image bytes passed in.
    assert [data for _, data, _ in container.uploads] == [cards[0][1], cards[1][1], cards[2][1]]


def test_upload_processed_cards_logs_and_continues_on_error(caplog):
    # Unit test: verify an upload exception is logged and does not stop later uploads.
    container = _FailingFirstUpload()
    first_bytes = _read_sample("sample output 1.jpg")
    second_bytes = _read_sample("sample output 2.jpg")
    cards = [("Card One", first_bytes), ("Card Two", second_bytes)]
    source_path = str(SAMPLES / "sample input 2.jpg")

    with caplog.at_level(logging.ERROR):
        _upload_processed_cards(container, source_path, cards)

    # The first upload fails; the second should still succeed with idx=2 naming.
    assert "Failed to upload processed card Card One" in caplog.text
    assert container.uploads == [("sample input 2_2.jpg", second_bytes, True)]


@pytest.mark.integration
def test_upload_processed_cards_writes_blobs_to_storage():
    # Integration test: write blobs into a real Azure container and confirm they can be
    # listed and downloaded.
    #
    # This tests:
    # - Connection string resolution (`AZURE_STORAGE_CONNECTION_STRING`)
    # - Container creation (or reuse)
    # - `_upload_processed_cards` with a real ContainerClient
    connection = _get_storage_connection()
    if not connection:
        pytest.skip("AZURE_STORAGE_CONNECTION_STRING not configured in environment or local.settings.json")

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

        # Upload two images into the output container using the production naming scheme.
        cards = [("Cloud Card", _read_sample("sample output 1.jpg")), ("unknown", _read_sample("sample output 2.jpg"))]
        source_path = str(SAMPLES / "sample input 1.jpg")
        _upload_processed_cards(container_client, source_path, cards)

        # Verify expected blob names are present.
        blobs = {blob.name for blob in container_client.list_blobs()}
        assert {"sample input 1_1.jpg", "sample input 1_2.jpg"} <= blobs

        # Verify one blob's content roundtrips correctly.
        downloaded = container_client.download_blob("sample input 1_1.jpg").readall()
        assert downloaded == cards[0][1]
    finally:
        # Default behavior keeps the container for manual inspection; set
        # DELETE_TEST_OUTPUT_CONTAINER=1 to clean up.
        if _should_delete_output_container():
            try:
                container_client.delete_container()
            except Exception as exc:
                raise RuntimeError(f"Failed to delete container '{container_name}': {exc}") from exc


@pytest.mark.integration
def test_upload_parsing_results_to_input_container_under_card_folder():
    # Integration test: run the parsing pipeline on a real sample input image, then
    # upload the resulting cropped card images to the `input` container under a
    # "folder" prefix (blob-name prefix).
    #
    # This is useful when you want to visually inspect what parsing produced in
    # Azure Storage: the images end up under:
    #   container: input
    #   prefix:    <TEST_CARD_FOLDER>/...
    connection = _get_storage_connection()
    if not connection:
        pytest.skip("AZURE_STORAGE_CONNECTION_STRING not configured in environment or local.settings.json")

    service_client = BlobServiceClient.from_connection_string(connection)
    input_container = service_client.get_container_client("input")
    try:
        input_container.create_container()
    except ResourceExistsError:
        logging.info("Container 'input' already exists")
    except Exception as exc:
        raise RuntimeError(f"Could not create/get container 'input': {exc}") from exc

    card_folder = _card_folder_name()
    prefix = f"{card_folder}/"

    # Parse cards from the sample input image.
    input_bytes = _read_sample("sample input 1.jpg")
    crops = process_utils.extract_card_crops_from_image_bytes(input_bytes)
    assert crops, "Expected at least one parsed card crop from sample input"

    # Upload a small subset (first 3) to keep the test fast and the container tidy.
    uploads = []
    for idx, (name, img_bytes) in enumerate(crops[:3], 1):
        # Reuse the same naming helper as the app so blob names mirror production outputs.
        file_name = function_app._build_processed_card_name("sample input 1.jpg", idx)
        blob_name = f"{prefix}{file_name}"
        input_container.upload_blob(name=blob_name, data=img_bytes, overwrite=True)
        uploads.append((blob_name, img_bytes))

    # Validate that the expected blob names exist under the prefix.
    existing = {blob.name for blob in input_container.list_blobs(name_starts_with=prefix)}
    assert {name for name, _ in uploads} <= existing

    # Validate that at least one blob roundtrips correctly.
    check_name, check_bytes = uploads[0]
    downloaded = input_container.download_blob(check_name).readall()
    assert downloaded == check_bytes
    # Optional cleanup for repeated runs.
    if _should_delete_output_container():
        _delete_prefix(input_container, prefix)
