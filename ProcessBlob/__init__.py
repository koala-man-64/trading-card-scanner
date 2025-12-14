import logging
import os

import azure.functions as func
from azure.storage.blob import BlobServiceClient

# Azure Functions Python worker adds the app root to sys.path so we can
# import our shared code directly from the package.
from CardProcessor import process_utils


def main(inputBlob: func.InputStream) -> None:
    """Blob trigger function to process trading card images.

    When a file is uploaded to the ``input`` container, this function
    extracts each card in the image, runs OCR to attempt to read the card name,
    and writes the cropped card images to the ``processed`` container.

    Args:
        inputBlob: Input blob stream provided by the Azure Functions runtime.
    """
    logging.info("Processing blob: %s", inputBlob.name)
    try:
        # Read blob contents into bytes
        blob_bytes = inputBlob.read()
    except Exception as exc:
        logging.error("Failed to read blob %s: %s", inputBlob.name, exc)
        return

    # Process the image and extract card crops
    cards = process_utils.process_image(blob_bytes)
    if not cards:
        logging.info("No cards detected in blob %s", inputBlob.name)
        return

    # Initialize storage client to write processed images
    connection = os.environ.get("AzureWebJobsStorage")
    if not connection:
        logging.error("AzureWebJobsStorage connection string not found in environment")
        return
    service_client = BlobServiceClient.from_connection_string(connection)
    processed_container = service_client.get_container_client("processed")
    # Create container if it doesn't exist
    try:
        processed_container.create_container()
    except Exception:
        # Already exists
        pass
    # Upload each card crop
    for idx, (name, img_bytes) in enumerate(cards, 1):
        # Ensure file name is unique
        safe_name = name.replace(" ", "_").lower() if name != "unknown" else "unknown"
        blob_name = f"{os.path.splitext(os.path.basename(inputBlob.name))[0]}_{idx}_{safe_name}.jpg"
        try:
            processed_container.upload_blob(name=blob_name, data=img_bytes, overwrite=True)
            logging.info("Uploaded processed card %s as %s", name, blob_name)
        except Exception as exc:
            logging.error("Failed to upload processed card %s: %s", name, exc)
