import logging
import os
from datetime import datetime
from typing import Iterable, Optional, Tuple

import azure.functions as func
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
    except Exception:
        # Container already exists
        pass
    return service_client, processed_container


def _upload_processed_cards(processed_container: ContainerClient, source_name: str, cards: Iterable[Tuple[str, bytes]]) -> None:
    """Upload processed card crops to the processed container."""
    for idx, (name, img_bytes) in enumerate(cards, 1):
        safe_name = name.replace(" ", "_").lower() if name != "unknown" else "unknown"
        blob_name = f"{os.path.splitext(os.path.basename(source_name))[0]}_{idx}_{safe_name}.jpg"
        try:
            processed_container.upload_blob(name=blob_name, data=img_bytes, overwrite=True)
            logging.info("Uploaded processed card %s as %s", name, blob_name)
        except Exception as exc:
            logging.error("Failed to upload processed card %s: %s", name, exc)


def _process_blob_bytes(source_name: str, blob_bytes: bytes, processed_container: ContainerClient) -> None:
    """Run card processing pipeline for a blob and upload results."""
    cards = process_utils.process_image(blob_bytes)
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
