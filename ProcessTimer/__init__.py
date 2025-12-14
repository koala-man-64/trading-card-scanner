import logging
import os
from datetime import datetime

import azure.functions as func
from azure.storage.blob import BlobServiceClient

from CardProcessor import process_utils


def main(mytimer: func.TimerRequest) -> None:
    """Scheduled function that scans the input container for unprocessed images.

    This function runs on a schedule configured in ``function.json``. It lists
    blobs in the ``input`` container and processes each one in turn. The
    results are stored in the ``processed`` container.

    Args:
        mytimer: TimerRequest context passed in by the Azure Functions runtime.
    """
    utc_now = datetime.utcnow().isoformat()
    logging.info("Timer trigger fired at %s", utc_now)

    connection = os.environ.get("AzureWebJobsStorage")
    if not connection:
        logging.error("AzureWebJobsStorage connection string not found in environment")
        return

    service_client = BlobServiceClient.from_connection_string(connection)
    input_container = service_client.get_container_client("input")
    processed_container = service_client.get_container_client("processed")
    # Ensure processed container exists
    try:
        processed_container.create_container()
    except Exception:
        pass

    # Iterate through blobs in input container
    for blob in input_container.list_blobs():
        blob_client = input_container.get_blob_client(blob)
        logging.info("Processing blob: %s", blob.name)
        try:
            blob_bytes = blob_client.download_blob().readall()
        except Exception as exc:
            logging.error("Failed to download blob %s: %s", blob.name, exc)
            continue
        cards = process_utils.process_image(blob_bytes)
        if not cards:
            logging.info("No cards detected in %s", blob.name)
            continue
        # Upload processed cards
        for idx, (name, img_bytes) in enumerate(cards, 1):
            safe_name = name.replace(" ", "_").lower() if name != "unknown" else "unknown"
            blob_name = f"{os.path.splitext(os.path.basename(blob.name))[0]}_{idx}_{safe_name}.jpg"
            try:
                processed_container.upload_blob(name=blob_name, data=img_bytes, overwrite=True)
                logging.info("Uploaded processed card %s as %s", name, blob_name)
            except Exception as exc:
                logging.error("Failed to upload processed card %s: %s", name, exc)
