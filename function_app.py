import base64
import io
import json
import logging
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol, Tuple, Union
from urllib.parse import urlencode

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import (
    BlobServiceClient,
    ContainerClient,
)

from card_processor import process_utils
from card_processor.layout_analysis import analyze_layout_from_image_bytes

try:
    from azure.identity import DefaultAzureCredential
except ImportError:
    DefaultAzureCredential = None  # type: ignore

app = func.FunctionApp()

# Define container names from environment variables with defaults
PROCESSED_CONTAINER_NAME = os.environ.get("PROCESSED_CONTAINER_NAME", "processed")
INPUT_CONTAINER_NAME = os.environ.get("INPUT_CONTAINER_NAME", "input")
GALLERY_CONTAINER_NAME = os.environ.get(
    "GALLERY_CONTAINER_NAME", PROCESSED_CONTAINER_NAME
)
GALLERY_INPUT_PREFIX = os.environ.get("GALLERY_INPUT_PREFIX", "input")
GALLERY_PROCESSED_PREFIX = os.environ.get("GALLERY_PROCESSED_PREFIX", "processed")
GALLERY_SEGMENTED_PREFIX = os.environ.get("GALLERY_SEGMENTED_PREFIX", "segmented")
GALLERY_REFRESH_SECONDS = float(os.environ.get("GALLERY_REFRESH_SECONDS", "5"))
GALLERY_USE_PUBLIC_URLS = os.environ.get(
    "GALLERY_USE_PUBLIC_URLS", ""
).strip().lower() in {"1", "true", "yes", "on"}
GALLERY_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "gallery.html"
GALLERY_REFRESH_TOKEN = "__GALLERY_REFRESH_SECONDS__"
STORAGE_AUTH_MODE = (
    os.environ.get("STORAGE_AUTH_MODE", "connection_string").strip().lower()
)
STORAGE_ACCOUNT_URL = os.environ.get("STORAGE_ACCOUNT_URL")


class _BlobClientUrl(Protocol):
    url: str


class _BlobListItem(Protocol):
    name: str
    size: int | None
    last_modified: Optional[datetime]


class _GalleryContainerClient(Protocol):
    def get_blob_client(self, name: str) -> _BlobClientUrl: ...

    def list_blobs(
        self, name_starts_with: Optional[str] = None
    ) -> Iterable[_BlobListItem]: ...


class _UploadContainerClient(Protocol):
    def upload_blob(self, name: str, data: bytes, *, overwrite: bool) -> object: ...


def _resolve_auth_level(
    value: Optional[str], default: func.AuthLevel
) -> func.AuthLevel:
    if not value:
        return default
    normalized = value.strip().upper()
    if normalized in {"ANONYMOUS", "FUNCTION", "ADMIN"}:
        return getattr(func.AuthLevel, normalized)
    logging.warning("Unknown auth level '%s'; defaulting to %s", value, default)
    return default


DEFAULT_AUTH_LEVEL = _resolve_auth_level(
    os.environ.get("HTTP_AUTH_LEVEL"), func.AuthLevel.FUNCTION
)
GALLERY_AUTH_LEVEL = _resolve_auth_level(
    os.environ.get("GALLERY_AUTH_LEVEL"), DEFAULT_AUTH_LEVEL
)

HEALTH_AUTH_LEVEL = _resolve_auth_level(
    os.environ.get("HEALTH_AUTH_LEVEL"), DEFAULT_AUTH_LEVEL
)


def _get_storage_clients() -> Tuple[
    Optional[BlobServiceClient], Optional[ContainerClient]
]:
    """Return storage service and processed container clients if configured."""
    service_client = _get_storage_service_client()
    if not service_client:
        return None, None

    try:
        processed_container = service_client.get_container_client(
            PROCESSED_CONTAINER_NAME
        )
        return service_client, processed_container
    except Exception as exc:
        logging.error("Failed to create blob service client: %s", exc)
        return None, None


def _get_storage_service_client() -> Optional[BlobServiceClient]:
    if STORAGE_AUTH_MODE in {"managed_identity", "aad"}:
        if not STORAGE_ACCOUNT_URL:
            logging.error(
                "STORAGE_ACCOUNT_URL is required for managed identity storage access"
            )
            return None
        if DefaultAzureCredential is None:
            logging.error(
                "azure-identity is not installed; cannot use managed identity"
            )
            return None
        try:
            credential = DefaultAzureCredential()
            return BlobServiceClient(
                account_url=STORAGE_ACCOUNT_URL, credential=credential
            )
        except Exception as exc:
            logging.error(
                "Failed to create blob service client with managed identity: %s", exc
            )
            return None

    connection = os.environ.get("AzureWebJobsStorage")
    if not connection:
        logging.error("AzureWebJobsStorage connection string not found in environment")
        return None

    try:
        return BlobServiceClient.from_connection_string(connection)
    except Exception as exc:
        logging.error("Failed to create blob service client: %s", exc)
        return None


def _get_container_client(
    container_name: str,
) -> Tuple[Optional[BlobServiceClient], Optional[ContainerClient]]:
    service_client = _get_storage_service_client()
    if not service_client:
        return None, None

    try:
        container_client = service_client.get_container_client(container_name)
        return service_client, container_client
    except Exception as exc:
        logging.error(
            "Failed to create blob container client for %s: %s",
            container_name,
            exc,
        )
        return None, None


def _normalize_prefix(prefix: str) -> str:
    cleaned = prefix.strip().strip("/")
    return f"{cleaned}/" if cleaned else ""


@lru_cache(maxsize=1)
def _load_gallery_template() -> Optional[str]:
    try:
        return GALLERY_TEMPLATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logging.error("Gallery template not found at %s", GALLERY_TEMPLATE_PATH)
    except Exception as exc:
        logging.error(
            "Failed to read gallery template at %s: %s", GALLERY_TEMPLATE_PATH, exc
        )
    return None


def _render_gallery_page(refresh_seconds: float) -> Optional[str]:
    template = _load_gallery_template()
    if not template:
        return None
    return template.replace(GALLERY_REFRESH_TOKEN, str(refresh_seconds))


def _format_rfc3339(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    formatted = normalized.isoformat(timespec="milliseconds")
    return formatted.replace("+00:00", "Z")


def _format_http_datetime(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _parse_since_param(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        logging.warning("Invalid since parameter '%s'; ignoring.", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _build_gallery_image_url(
    container_client: _GalleryContainerClient,
    blob_name: str,
    *,
    category: str,
    auth_code: Optional[str],
    use_public_urls: bool,
) -> str:
    if use_public_urls:
        return container_client.get_blob_client(blob_name).url

    params = {"name": blob_name, "category": category}
    if auth_code:
        params["code"] = auth_code
    return f"/api/gallery/image?{urlencode(params)}"


def _list_blob_images(
    container_client: _GalleryContainerClient,
    prefix: str,
    *,
    category: str,
    auth_code: Optional[str],
    use_public_urls: bool,
    since: Optional[datetime] = None,
) -> Tuple[List[Dict[str, object]], Optional[datetime]]:
    blobs = []
    normalized_prefix = _normalize_prefix(prefix)
    latest_modified: Optional[datetime] = None
    for blob in container_client.list_blobs(name_starts_with=normalized_prefix):
        blob_modified = getattr(blob, "last_modified", None)
        blob_modified_utc = (
            blob_modified.astimezone(timezone.utc) if blob_modified else None
        )
        if since and blob_modified_utc and blob_modified_utc < since:
            continue
        if blob_modified_utc and (
            latest_modified is None or blob_modified_utc > latest_modified
        ):
            latest_modified = blob_modified_utc
        last_modified = (
            _format_rfc3339(blob_modified_utc) if blob_modified_utc else None
        )
        blobs.append(
            {
                "name": blob.name,
                "size": blob.size or 0,
                "last_modified": last_modified,
                "url": _build_gallery_image_url(
                    container_client,
                    blob.name,
                    category=category,
                    auth_code=auth_code,
                    use_public_urls=use_public_urls,
                ),
            }
        )
    return blobs, latest_modified


def _is_not_modified(
    req: func.HttpRequest,
    *,
    etag: Optional[str],
    last_modified: Optional[datetime],
) -> bool:
    if etag:
        if_none_match = req.headers.get("If-None-Match")
        if if_none_match and if_none_match.strip() == etag:
            return True

    if last_modified:
        if_modified_since = req.headers.get("If-Modified-Since")
        if if_modified_since:
            try:
                parsed_since = parsedate_to_datetime(if_modified_since)
            except (TypeError, ValueError):
                parsed_since = None
            if parsed_since is not None:
                if parsed_since.tzinfo is None:
                    parsed_since = parsed_since.replace(tzinfo=timezone.utc)
                if last_modified.astimezone(timezone.utc) <= parsed_since.astimezone(
                    timezone.utc
                ):
                    return True

    return False


def _build_processed_card_name(source_name: str, idx: int) -> str:
    base_name = os.path.splitext(os.path.basename(source_name))[0]
    return f"{base_name}_{idx}.jpg"


def _sanitize_blob_folder_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return safe or "cards"


def _build_processed_card_folder(source_name: str) -> str:
    base_name = os.path.splitext(os.path.basename(source_name))[0]
    return _sanitize_blob_folder_name(base_name)


def _upload_processed_cards(
    processed_container: _UploadContainerClient,
    source_name: str,
    cards: Iterable[Tuple[str, bytes]],
    folder: Optional[str] = None,
) -> None:
    """Upload processed card crops to the processed container."""
    prefix = _sanitize_blob_folder_name(folder) if folder else None
    for idx, (name, img_bytes) in enumerate(cards, 1):
        blob_name = _build_processed_card_name(source_name, idx)
        if prefix:
            blob_name = f"{prefix}/{blob_name}"
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
    source_name: str, blob_bytes: bytes, processed_container: _UploadContainerClient
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


def _gallery_prefix_for_category(category: str) -> Optional[str]:
    normalized = category.strip().lower()
    if normalized == "input":
        return GALLERY_INPUT_PREFIX
    if normalized == "processed":
        prefix = (GALLERY_PROCESSED_PREFIX or "").strip()
        if prefix.lower() in {"", "root", "all", "*"}:
            return ""
        container_name = (GALLERY_CONTAINER_NAME or "").strip().lower()
        if prefix.strip("/").lower() == container_name:
            return ""
        return prefix
    if normalized == "segmented":
        return GALLERY_SEGMENTED_PREFIX
    return None


@app.function_name(name="GalleryImages")
@app.route(route="gallery/images", methods=["GET"], auth_level=GALLERY_AUTH_LEVEL)
def gallery_images(req: func.HttpRequest) -> func.HttpResponse:
    """Return JSON listing of blobs for the requested gallery category."""
    category = (req.params.get("category") or "processed").strip().lower()
    prefix = _gallery_prefix_for_category(category)
    if prefix is None:
        return func.HttpResponse(
            "Unsupported category. Use input, processed, or segmented.",
            status_code=400,
        )

    _, container_client = _get_container_client(GALLERY_CONTAINER_NAME)
    if not container_client:
        return func.HttpResponse(
            "Storage is not configured. Set AzureWebJobsStorage.", status_code=500
        )

    auth_code = req.params.get("code")
    since = _parse_since_param(req.params.get("since"))
    try:
        blobs, latest_modified = _list_blob_images(
            container_client,
            prefix,
            category=category,
            auth_code=auth_code,
            use_public_urls=GALLERY_USE_PUBLIC_URLS,
            since=since,
        )
    except Exception as exc:
        logging.error("Failed to list blobs for gallery: %s", exc)
        return func.HttpResponse("Failed to list images.", status_code=500)

    refreshed_at = datetime.now(timezone.utc)
    next_since = latest_modified or since or refreshed_at
    payload = {
        "container": container_client.container_name,
        "category": category,
        "prefix": _normalize_prefix(prefix),
        "blobs": blobs,
        "refreshed_at": _format_rfc3339(refreshed_at),
        "refresh_seconds": GALLERY_REFRESH_SECONDS,
        "next_since": _format_rfc3339(next_since),
    }
    return func.HttpResponse(
        body=json.dumps(payload), status_code=200, mimetype="application/json"
    )


@app.function_name(name="GalleryPage")
@app.route(route="gallery", methods=["GET"], auth_level=GALLERY_AUTH_LEVEL)
def gallery_page(req: func.HttpRequest) -> func.HttpResponse:
    """Serve a minimal gallery UI for browsing card images."""
    html = _render_gallery_page(GALLERY_REFRESH_SECONDS)
    if not html:
        return func.HttpResponse(
            "Gallery template is unavailable.", status_code=500, mimetype="text/plain"
        )
    return func.HttpResponse(html, status_code=200, mimetype="text/html")


@app.function_name(name="GalleryImage")
@app.route(route="gallery/image", methods=["GET"], auth_level=GALLERY_AUTH_LEVEL)
def gallery_image(req: func.HttpRequest) -> func.HttpResponse:
    """Serve a single blob image for gallery browsing."""
    name = (req.params.get("name") or "").strip()
    if not name:
        return func.HttpResponse("Missing blob name.", status_code=400)

    category = (req.params.get("category") or "processed").strip().lower()
    prefix = _gallery_prefix_for_category(category)
    if prefix is None:
        return func.HttpResponse(
            "Unsupported category. Use input, processed, or segmented.",
            status_code=400,
        )

    normalized_prefix = _normalize_prefix(prefix)
    if normalized_prefix and not name.startswith(normalized_prefix):
        return func.HttpResponse(
            "Blob name does not match category prefix.", status_code=400
        )

    _, container_client = _get_container_client(GALLERY_CONTAINER_NAME)
    if not container_client:
        return func.HttpResponse(
            "Storage is not configured. Set AzureWebJobsStorage.", status_code=500
        )

    blob_client = container_client.get_blob_client(name)
    try:
        props = blob_client.get_blob_properties()
        content_type = props.content_settings.content_type or "application/octet-stream"
        etag = props.etag
        last_modified = getattr(props, "last_modified", None)
        if _is_not_modified(req, etag=etag, last_modified=last_modified):
            headers = {"Cache-Control": "public, max-age=60"}
            if etag:
                headers["ETag"] = etag
            if last_modified:
                headers["Last-Modified"] = _format_http_datetime(last_modified)
            return func.HttpResponse(status_code=304, headers=headers)

        data = blob_client.download_blob().readall()
    except ResourceNotFoundError:
        return func.HttpResponse("Blob not found.", status_code=404)
    except Exception as exc:
        logging.error("Failed to download blob %s: %s", name, exc)
        return func.HttpResponse("Failed to download image.", status_code=500)

    headers = {"Cache-Control": "public, max-age=60"}
    if etag:
        headers["ETag"] = etag
    if last_modified:
        headers["Last-Modified"] = _format_http_datetime(last_modified)
    return func.HttpResponse(
        body=data, status_code=200, mimetype=content_type, headers=headers
    )


@app.function_name(name="Health")
@app.route(route="health", methods=["GET"], auth_level=HEALTH_AUTH_LEVEL)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Simple health endpoint for Postman/smoke tests."""
    return func.HttpResponse("OK", status_code=200)


def _parse_bool_param(value: Optional[str], *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@app.function_name(name="AnalyzeLayout")
@app.route(route="layout", methods=["POST"], auth_level=DEFAULT_AUTH_LEVEL)
def analyze_layout(req: func.HttpRequest) -> func.HttpResponse:
    """Run document layout analysis on uploaded image bytes."""
    image_bytes = req.get_body() or b""
    if not image_bytes:
        return func.HttpResponse(
            "Provide image bytes in the request body.", status_code=400
        )

    model_variant = (req.params.get("model_variant") or "nano").strip().lower()
    imgsz = int(req.params.get("imgsz") or 1280)
    conf = float(req.params.get("conf") or 0.25)
    iou = float(req.params.get("iou") or 0.5)
    extract_crops = _parse_bool_param(req.params.get("extract_crops"), default=True)
    crop_format = (req.params.get("crop_format") or "png").strip().lower()

    result = analyze_layout_from_image_bytes(
        image_bytes,
        model_variant=model_variant,
        imgsz=imgsz,
        conf=conf,
        iou=iou,
        extract_crops=extract_crops,
        crop_format=crop_format,
    )

    def _serialize_element(idx, el):
        payload = {
            "index": idx,
            "label": el.label,
            "confidence": el.confidence,
            "bbox_xyxy": el.bbox_xyxy,
            "bbox_norm": el.bbox_norm,
            "reading_order_hint": el.reading_order_hint,
        }
        if el.crop_bytes is not None:
            payload["crop"] = {
                "mime": el.crop_mime,
                "data": base64.b64encode(el.crop_bytes).decode("utf-8"),
            }
        return payload

    body = {
        "image_width": result.image_width,
        "image_height": result.image_height,
        "elements": [
            _serialize_element(idx, el) for idx, el in enumerate(result.elements, 1)
        ],
        "model_info": result.model_info,
        "errors": result.errors,
    }
    status_code = 200 if not result.errors else 207
    return func.HttpResponse(
        body=json.dumps(body),
        status_code=status_code,
        mimetype="application/json",
    )


@app.function_name(name="ProcessImage")
@app.route(route="process", methods=["POST"], auth_level=DEFAULT_AUTH_LEVEL)
def process_image(req: func.HttpRequest) -> func.HttpResponse:
    """Process an uploaded image and optionally return/upload detected card crops.

    Send the image bytes as the raw request body.

    Query params:
      - output=none|return|upload (default: none)
      - format=zip|json (default: zip; applies when output=return)
    Uploads are stored under a folder prefix derived from the input name.
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
        payload: dict[str, object] = {
            "card_count": process_utils.count_cards_in_image_bytes(image_bytes)
        }
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
        folder = _build_processed_card_folder(source_name)
        _upload_processed_cards(processed_container, source_name, cards, folder=folder)
        blob_names = [
            f"{folder}/{_build_processed_card_name(source_name, idx)}"
            for idx in range(1, len(cards) + 1)
        ]
        payload = {
            "card_count": len(cards),
            "uploaded": {
                "container": PROCESSED_CONTAINER_NAME,
                "folder": folder,
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
