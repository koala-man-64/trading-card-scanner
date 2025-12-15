import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Tuple, Union

import azure.functions as func
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContainerClient

from CardProcessor import process_utils

app = func.FunctionApp()


def _get_storage_clients() -> Tuple[Optional[BlobServiceClient], Optional[ContainerClient]]:
    """Return storage service and processed container clients if configured."""
    connection = os.environ.get("AzureWebJobsStorage")
    if not connection:
        logging.error("AzureWebJobsStorage connection string not found in environment")
        return None, None

    service_client = BlobServiceClient.from_connection_string(connection)
    processed_container = service_client.get_container_client("processed")
    try:
        processed_container.create_container()
    except ResourceExistsError:
        logging.info("Container 'processed' already exists")
    except Exception as exc:
        logging.error("Failed to create/get container 'processed': %s", exc)
        raise
    return service_client, processed_container


def _build_processed_card_name(source_name: str, idx: int) -> str:
    base_name = os.path.splitext(os.path.basename(source_name))[0]
    return f"{base_name}_{idx}.jpg"


def _upload_processed_cards(processed_container: ContainerClient, source_name: str, cards: Iterable[Tuple[str, bytes]]) -> None:
    """Upload processed card crops to the processed container."""
    for idx, (name, img_bytes) in enumerate(cards, 1):
        blob_name = _build_processed_card_name(source_name, idx)
        try:
            processed_container.upload_blob(name=blob_name, data=img_bytes, overwrite=True)
            logging.info("Uploaded processed card %s as %s", name, blob_name)
        except Exception as exc:
            logging.error("Failed to upload processed card %s: %s", name, exc)


def _save_processed_cards_to_folder(output_dir: Union[Path, str], source_name: str, cards: Iterable[Tuple[str, bytes]]) -> None:
    """Write processed card crops to a local folder."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    for idx, (name, img_bytes) in enumerate(cards, 1):
        file_name = _build_processed_card_name(source_name, idx)
        destination = output_path / file_name
        try:
            destination.write_bytes(img_bytes)
            logging.info("Saved processed card %s to %s", name, destination)
        except Exception as exc:
            logging.error("Failed to save processed card %s to %s: %s", name, destination, exc)


def _process_blob_bytes(source_name: str, blob_bytes: bytes, processed_container: ContainerClient) -> None:
    """Run card processing pipeline for a blob and upload results."""
    cards = process_utils.extract_card_crops_from_image_bytes(blob_bytes)
    if not cards:
        logging.info("No cards detected in %s", source_name)
        return

    _upload_processed_cards(processed_container, source_name, cards)


@app.function_name(name="ProcessBlob")
@app.blob_trigger(arg_name="inputBlob", path="input/{name}", connection="AzureWebJobsStorage")
def process_blob(inputBlob: func.InputStream) -> None:
    """Blob trigger to process trading card images uploaded to the input container."""
    logging.info("Processing blob: %s", inputBlob.name)

    try:
        blob_bytes = inputBlob.read()
    except Exception as exc:
        logging.error("Failed to read blob %s: %s", inputBlob.name, exc)
        return

    _, processed_container = _get_storage_clients()
    if not processed_container:
        return

    _process_blob_bytes(inputBlob.name, blob_bytes, processed_container)


@app.function_name(name="ProcessTimer")
@app.schedule(schedule="0 0 */6 * * *", arg_name="mytimer", run_on_startup=False, use_monitor=True)
def process_timer(mytimer: func.TimerRequest) -> None:
    """Scheduled function that scans the input container for unprocessed images."""
    utc_now = datetime.utcnow().isoformat()
    logging.info("Timer trigger fired at %s", utc_now)

    service_client, processed_container = _get_storage_clients()
    if not service_client or not processed_container:
        return

    input_container = service_client.get_container_client("input")

    for blob in input_container.list_blobs():
        blob_client = input_container.get_blob_client(blob)
        logging.info("Processing blob: %s", blob.name)
        try:
            blob_bytes = blob_client.download_blob().readall()
        except Exception as exc:
            logging.error("Failed to download blob %s: %s", blob.name, exc)
            continue

        _process_blob_bytes(blob.name, blob_bytes, processed_container)
