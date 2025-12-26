import io
import json
import logging
import os
import re
import uuid
import zipfile
from pathlib import Path
from typing import Iterable, Optional, Tuple, Union

import azure.functions as func
from azure.storage.blob import BlobServiceClient, ContainerClient

from CardProcessor import process_utils

app = func.FunctionApp()

# Define container names from environment variables with defaults
PROCESSED_CONTAINER_NAME = os.environ.get("PROCESSED_CONTAINER_NAME", "processed")
INPUT_CONTAINER_NAME = os.environ.get("INPUT_CONTAINER_NAME", "input")


def _get_storage_clients() -> Tuple[
    Optional[BlobServiceClient], Optional[ContainerClient]
]:
    """Return storage service and processed container clients if configured."""
    connection = os.environ.get("AzureWebJobsStorage")
    if not connection:
        logging.error("AzureWebJobsStorage connection string not found in environment")
        return None, None

    try:
        service_client = BlobServiceClient.from_connection_string(connection)
        processed_container = service_client.get_container_client(
            PROCESSED_CONTAINER_NAME
        )
        return service_client, processed_container
    except Exception as exc:
        logging.error("Failed to create blob service client: %s", exc)
        return None, None


def _build_processed_card_name(source_name: str, idx: int) -> str:
    base_name = os.path.splitext(os.path.basename(source_name))[0]
    return f"{base_name}_{idx}.jpg"


def _upload_processed_cards(
    processed_container: ContainerClient,
    source_name: str,
    cards: Iterable[Tuple[str, bytes]],
) -> None:
    """Upload processed card crops to the processed container."""
    for idx, (name, img_bytes) in enumerate(cards, 1):
        blob_name = _build_processed_card_name(source_name, idx)
        try:
            processed_container.upload_blob(
                name=blob_name, data=img_bytes, overwrite=True
            )
            logging.info("Uploaded processed card %s as %s", name, blob_name)
        except Exception as exc:
            logging.error("Failed to upload processed card %s: %s", name, exc)


def _save_processed_cards_to_folder(
    output_dir: Union[Path, str],
    source_name: str,
    cards: Iterable[Tuple[str, bytes]],
) -> None:
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
            logging.error(
                "Failed to save processed card %s to %s: %s", name, destination, exc
            )


def _process_blob_bytes(
    source_name: str, blob_bytes: bytes, processed_container: ContainerClient
) -> None:
    """Run card processing pipeline for a blob and upload results."""
    cards = process_utils.extract_card_crops_from_image_bytes(blob_bytes)
    if not cards:
        logging.info("No cards detected in %s", source_name)
        return

    _upload_processed_cards(processed_container, source_name, cards)


@app.function_name(name="ProcessBlob")
@app.blob_trigger(
    arg_name="inputBlob",
    path=f"{INPUT_CONTAINER_NAME}/{{name}}",
    connection="AzureWebJobsStorage",
)
def process_blob(inputBlob: func.InputStream) -> None:
    """Blob trigger to process trading card images uploaded to the input container."""
    if not inputBlob.name:
        logging.error("Blob name is missing, cannot process.")
        return

    logging.info("Processing blob: %s", inputBlob.name)

    _, processed_container = _get_storage_clients()
    if not processed_container:
        logging.critical(
            "Exiting: Processed container client could not be initialized. "
            "Check storage connection string."
        )
        return

    try:
        blob_bytes = inputBlob.read()
    except Exception as exc:
        logging.error("Failed to read blob %s: %s", inputBlob.name, exc)
        return

    _process_blob_bytes(inputBlob.name, blob_bytes, processed_container)


def _sanitize_zip_member_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return safe or "card"


@app.function_name(name="Health")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Simple health endpoint for Postman/smoke tests."""
    return func.HttpResponse("OK", status_code=200)


@app.function_name(name="ProcessImage")
@app.route(route="process", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def process_image(req: func.HttpRequest) -> func.HttpResponse:
    """Process an uploaded image and optionally return/upload detected card crops.

    Send the image bytes as the raw request body.

    Query params:
      - output=none|return|upload (default: none)
      - format=zip|json (default: zip; applies when output=return)
    """
    output_mode = (req.params.get("output") or "").strip().lower()
    output_format = (req.params.get("format") or "").strip().lower()

    if not output_mode:
        output_mode = "return" if output_format else "none"

    if output_mode in {"return", "bytes"}:
        output_mode = "return"
    elif output_mode in {"upload", "cloud"}:
        output_mode = "upload"
    elif output_mode in {"none", "count"}:
        output_mode = "none"
    else:
        return func.HttpResponse(
            "Unsupported output. Use 'none', 'return', or 'upload'.",
            status_code=400,
        )

    image_bytes = req.get_body() or b""

    if not image_bytes:
        return func.HttpResponse(
            "Provide image bytes in the request body.", status_code=400
        )

    if output_mode == "none":
        payload = {"card_count": process_utils.count_cards_in_image_bytes(image_bytes)}
        return func.HttpResponse(
            body=json.dumps(payload),
            status_code=200,
            mimetype="application/json",
        )

    cards = process_utils.extract_card_crops_from_image_bytes(image_bytes)

    if output_mode == "upload":
        _, processed_container = _get_storage_clients()
        if not processed_container:
            return func.HttpResponse(
                "Storage is not configured. Set AzureWebJobsStorage.",
                status_code=500,
            )

        source_name = (
            (req.params.get("name") or "").strip()
            or req.headers.get("x-file-name")
            or f"upload_{uuid.uuid4().hex}.jpg"
        )
        _upload_processed_cards(processed_container, source_name, cards)
        blob_names = [
            _build_processed_card_name(source_name, idx)
            for idx in range(1, len(cards) + 1)
        ]
        payload = {
            "card_count": len(cards),
            "uploaded": {
                "container": PROCESSED_CONTAINER_NAME,
                "blobs": blob_names,
            },
        }
        return func.HttpResponse(
            body=json.dumps(payload),
            status_code=200,
            mimetype="application/json",
        )

    if not output_format:
        output_format = "zip"

    if output_format == "json":
        payload = {
            "card_count": len(cards),
            "cards": [
                {"index": idx, "name": name, "bytes": len(img_bytes)}
                for idx, (name, img_bytes) in enumerate(cards, 1)
            ],
        }
        return func.HttpResponse(
            body=json.dumps(payload),
            status_code=200,
            mimetype="application/json",
        )

    if output_format != "zip":
        return func.HttpResponse(
            "Unsupported format. Use 'zip' or 'json'.", status_code=400
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, (name, img_bytes) in enumerate(cards, 1):
            member_name = _sanitize_zip_member_name(name or "card")
            zf.writestr(f"{idx:02d}_{member_name}.jpg", img_bytes)

    headers = {
        "Content-Disposition": "attachment; filename=processed_cards.zip",
        "X-Card-Count": str(len(cards)),
    }
    return func.HttpResponse(
        body=buf.getvalue(),
        status_code=200,
        mimetype="application/zip",
        headers=headers,
    )

