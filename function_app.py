import base64
import hashlib
import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple, Union, cast
from urllib.parse import urlencode

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.core.paging import ItemPaged
from azure.storage.blob import BlobServiceClient, ContainerClient

from card_processor.api_responses import error_response, json_response
from card_processor import process_utils
from card_processor.detection import detect_cards_from_image_bytes
from card_processor.detection_model import ModelResolutionError, resolve_model_id
from card_processor.request_validation import (
    RequestValidationError,
    normalize_image_format,
    parse_detection_params,
    parse_process_params,
    parse_since_param,
    read_bounded_blob,
    read_bounded_http_body,
    set_pillow_decompression_limit,
    validate_image_bytes,
)
from card_processor.settings import ScannerSettings, load_settings
from card_processor.telemetry import correlation_id_from_request, log_event
from card_processor.upload_results import UploadBatchResult

try:
    from azure.identity import DefaultAzureCredential
except ImportError:
    DefaultAzureCredential = None  # type: ignore

app = func.FunctionApp()
logger = logging.getLogger(__name__)

# Define container names and storage binding names from environment variables with defaults.
PROCESSED_CONTAINER_NAME = os.environ.get("PROCESSED_CONTAINER_NAME", "processed")
INPUT_CONTAINER_NAME = os.environ.get("INPUT_CONTAINER_NAME", "input")
INPUT_BLOB_PREFIX = os.environ.get("INPUT_BLOB_PREFIX", "raw").strip().strip("/")
INPUT_STORAGE_CONNECTION_NAME = os.environ.get(
    "INPUT_STORAGE_CONNECTION_NAME", "AzureWebJobsStorage"
)
INPUT_BLOB_SOURCE = func.BlobSource.EVENT_GRID
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
ADMIN_GALLERY_MANAGE_SCOPE = os.environ.get(
    "ADMIN_GALLERY_MANAGE_SCOPE", "gallery.manage"
).strip()
ADMIN_ALLOWED_OBJECT_IDS = {
    value.strip()
    for value in os.environ.get("ADMIN_ALLOWED_OBJECT_IDS", "").split(",")
    if value.strip()
}
ADMIN_ALLOWED_ROLES = {
    value.strip()
    for value in os.environ.get("ADMIN_ALLOWED_ROLES", "Gallery.Admin").split(",")
    if value.strip()
}
LINEAGE_PREFIX = os.environ.get("GALLERY_LINEAGE_PREFIX", "lineage").strip("/")
GALLERY_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")


class _BlobClientUrl(Protocol):
    url: str


class _BlobDownload(Protocol):
    def readall(self) -> bytes: ...


class _GalleryBlobClient(_BlobClientUrl, Protocol):
    def download_blob(self) -> _BlobDownload: ...

    def delete_blob(self) -> None: ...

    def get_blob_properties(self) -> Any: ...


class _BlobListItem(Protocol):
    name: str
    size: int | None
    last_modified: Optional[datetime]


class _GalleryContainerClient(Protocol):
    def get_blob_client(
        self,
        blob: str,
        snapshot: Optional[str] = None,
        *,
        version_id: Optional[str] = None,
    ) -> _GalleryBlobClient: ...

    def list_blobs(
        self,
        name_starts_with: Optional[str] = None,
        include: str | list[str] | None = None,
        *,
        timeout: Optional[int] = None,
        **kwargs: Any,
    ) -> Iterable[_BlobListItem] | ItemPaged[Any]: ...

    def upload_blob(
        self,
        name: str,
        data: bytes,
        *,
        overwrite: bool,
        **kwargs: Any,
    ) -> object: ...


class _UploadContainerClient(Protocol):
    def upload_blob(
        self,
        name: str,
        data: bytes,
        *,
        overwrite: bool,
        **kwargs: Any,
    ) -> object: ...


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
READY_AUTH_LEVEL = _resolve_auth_level(
    os.environ.get("READY_AUTH_LEVEL"), DEFAULT_AUTH_LEVEL
)


def _settings() -> ScannerSettings:
    settings = load_settings()
    set_pillow_decompression_limit(settings)
    return settings


def _get_storage_clients() -> (
    Tuple[Optional[BlobServiceClient], Optional[ContainerClient]]
):
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


def _json_error(message: str, status_code: int, code: str) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps({"error": code, "message": message}),
        status_code=status_code,
        mimetype="application/json",
    )


def _extract_easy_auth_claims(header: Optional[str]) -> dict[str, list[str]]:
    if not header:
        return {}
    try:
        padded = header + "=" * (-len(header) % 4)
        payload = json.loads(base64.b64decode(padded).decode("utf-8"))
    except Exception:
        logging.warning("Invalid x-ms-client-principal header")
        return {}

    claims: dict[str, list[str]] = {}
    for item in payload.get("claims", []):
        typ = str(item.get("typ", "")).strip()
        val = str(item.get("val", "")).strip()
        if typ and val:
            claims.setdefault(typ, []).append(val)
    return claims


def _admin_claim_values(claims: dict[str, list[str]], key: str) -> set[str]:
    return {
        part for value in claims.get(key, []) for part in str(value).split() if part
    }


def _require_gallery_admin(
    req: func.HttpRequest,
) -> tuple[bool, func.HttpResponse | None]:
    claims = _extract_easy_auth_claims(req.headers.get("x-ms-client-principal"))
    if not claims:
        return False, _json_error(
            "Admin gallery requests require an authenticated Entra principal.",
            401,
            "missing_admin_principal",
        )

    scopes = _admin_claim_values(claims, "scp")
    roles = _admin_claim_values(claims, "roles") | _admin_claim_values(
        claims,
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
    )
    object_ids = _admin_claim_values(claims, "oid") | _admin_claim_values(
        claims,
        "http://schemas.microsoft.com/identity/claims/objectidentifier",
    )

    if ADMIN_GALLERY_MANAGE_SCOPE not in scopes:
        return False, _json_error(
            f"Required scope is missing: {ADMIN_GALLERY_MANAGE_SCOPE}",
            403,
            "missing_scope",
        )

    if not ADMIN_ALLOWED_OBJECT_IDS and not ADMIN_ALLOWED_ROLES:
        return False, _json_error(
            "Admin gallery authorization is not configured.",
            500,
            "admin_authorization_not_configured",
        )

    if ADMIN_ALLOWED_OBJECT_IDS.intersection(
        object_ids
    ) or ADMIN_ALLOWED_ROLES.intersection(roles):
        return True, None

    return False, _json_error(
        "The authenticated principal is not allowed to manage gallery images.",
        403,
        "admin_not_allowed",
    )


def _build_input_blob_trigger_path(container_name: str, prefix: str) -> str:
    normalized_prefix = _normalize_prefix(prefix)
    if normalized_prefix:
        return f"{container_name}/{normalized_prefix}{{name}}"
    return f"{container_name}/{{name}}"


INPUT_BLOB_TRIGGER_PATH = _build_input_blob_trigger_path(
    INPUT_CONTAINER_NAME, INPUT_BLOB_PREFIX
)


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


def _lineage_blob_name(source_name: str) -> str:
    digest = hashlib.sha256(source_name.encode("utf-8")).hexdigest()
    return f"{LINEAGE_PREFIX}/{digest}.json"


def _is_gallery_image_blob(blob_name: str) -> bool:
    lowered = blob_name.lower()
    return lowered.endswith(GALLERY_IMAGE_EXTENSIONS)


def _load_lineage_manifest(
    container_client: _GalleryContainerClient,
    source_name: str,
) -> dict[str, object] | None:
    blob_client = container_client.get_blob_client(_lineage_blob_name(source_name))
    try:
        data = blob_client.download_blob().readall()
    except ResourceNotFoundError:
        return None
    return cast(dict[str, object], json.loads(data.decode("utf-8")))


def _write_lineage_manifest(
    container_client: _GalleryContainerClient,
    source_name: str,
    processed_blobs: list[str],
) -> None:
    manifest = {
        "sourceBlobName": source_name,
        "outputsByCategory": {
            "processed": processed_blobs,
            "segmented": [],
        },
        "updatedAtUtc": _format_rfc3339(datetime.now(timezone.utc)),
    }
    container_client.upload_blob(
        name=_lineage_blob_name(source_name),
        data=json.dumps(manifest, sort_keys=True).encode("utf-8"),
        overwrite=True,
    )


def _lineage_source_index(
    container_client: _GalleryContainerClient,
) -> dict[str, str]:
    index: dict[str, str] = {}
    prefix = _normalize_prefix(LINEAGE_PREFIX)
    for blob in container_client.list_blobs(name_starts_with=prefix):
        try:
            data = container_client.get_blob_client(blob.name).download_blob().readall()
            manifest = json.loads(data.decode("utf-8"))
        except Exception:
            logging.warning("Skipping invalid lineage manifest %s", blob.name)
            continue
        source_name = str(manifest.get("sourceBlobName", "")).strip()
        outputs = manifest.get("outputsByCategory", {})
        if not source_name or not isinstance(outputs, dict):
            continue
        for names in outputs.values():
            if isinstance(names, list):
                for name in names:
                    if isinstance(name, str):
                        index[name] = source_name
    return index


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
    use_public_urls: bool,
) -> str:
    if use_public_urls:
        blob_client = cast(_BlobClientUrl, container_client.get_blob_client(blob_name))
        return blob_client.url

    params = {"name": blob_name, "category": category}
    return f"/api/gallery/image?{urlencode(params)}"


def _parse_page_size_param(value: Optional[str]) -> Optional[int]:
    """Parse an optional gallery page size. No upper bound is enforced."""
    if value is None or not value.strip():
        return None
    try:
        page_size = int(value)
    except ValueError as exc:
        raise RequestValidationError(
            "page_size must be an integer.",
            status_code=400,
            code="invalid_page_size",
        ) from exc
    if page_size < 1:
        raise RequestValidationError(
            "page_size must be greater than 0.",
            status_code=400,
            code="invalid_page_size",
        )
    return page_size


def _list_blob_images(
    container_client: _GalleryContainerClient,
    prefix: str,
    *,
    category: str,
    use_public_urls: bool,
    since: Optional[datetime] = None,
    page_size: Optional[int] = None,
    continuation_token: Optional[str] = None,
) -> Tuple[List[Dict[str, object]], Optional[datetime], Optional[str]]:
    """List gallery blobs.

    When ``page_size`` is given, a single page is returned along with a
    continuation token for the next page (``None`` when the listing is
    exhausted). When it is ``None`` the full listing is returned in one call.
    """
    blobs = []
    normalized_prefix = _normalize_prefix(prefix)
    latest_modified: Optional[datetime] = None
    next_continuation: Optional[str] = None

    if page_size is not None:
        pager = cast(
            Any,
            container_client.list_blobs(
                name_starts_with=normalized_prefix, results_per_page=page_size
            ),
        ).by_page(continuation_token=continuation_token)
        page = next(pager, None)
        blobs_iter = iter(page) if page is not None else iter(())
    else:
        blobs_iter = cast(
            Iterable[_BlobListItem],
            container_client.list_blobs(name_starts_with=normalized_prefix),
        )

    for blob in blobs_iter:
        if not _is_gallery_image_blob(blob.name):
            continue
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
                    use_public_urls=use_public_urls,
                ),
            }
        )

    if page_size is not None:
        next_continuation = getattr(pager, "continuation_token", None)
    return blobs, latest_modified, next_continuation


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
) -> UploadBatchResult:
    """Upload processed card crops to the processed container."""
    result = UploadBatchResult()
    prefix = _sanitize_blob_folder_name(folder) if folder else None
    for idx, (name, img_bytes) in enumerate(cards, 1):
        result.attempted += 1
        blob_name = _build_processed_card_name(source_name, idx)
        if prefix:
            blob_name = f"{prefix}/{blob_name}"
        try:
            processed_container.upload_blob(
                name=blob_name, data=img_bytes, overwrite=True
            )
            result.record_success(blob_name)
            logging.info("Uploaded processed card %s as %s", name, blob_name)
        except Exception as exc:
            result.record_failure(name, blob_name, exc)
            logging.error("Failed to upload processed card %s: %s", name, exc)
    return result


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
) -> UploadBatchResult:
    """Run card processing pipeline for a blob and upload results."""
    settings = _settings()
    validate_image_bytes(blob_bytes, settings)
    cards = process_utils.extract_card_crops_from_image_bytes(
        blob_bytes, max_crops=settings.max_crops
    )
    if not cards:
        logging.info("No cards detected in %s", source_name)
        return UploadBatchResult()

    result = _upload_processed_cards(processed_container, source_name, cards)
    _write_lineage_manifest(
        cast(_GalleryContainerClient, processed_container),
        source_name,
        result.uploaded_blobs,
    )
    if result.has_failures:
        raise RuntimeError(
            f"Failed to upload {result.failed_count} of {result.attempted} cards"
        )
    return result


@app.function_name(name="ProcessBlob")
@app.blob_trigger(
    arg_name="inputBlob",
    path=INPUT_BLOB_TRIGGER_PATH,
    connection=INPUT_STORAGE_CONNECTION_NAME,
    source=INPUT_BLOB_SOURCE,
)
def process_blob(inputBlob: func.InputStream) -> None:
    """Process raw trading card images uploaded by the capture app."""
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
        blob_bytes = read_bounded_blob(inputBlob, _settings())
    except Exception as exc:
        logging.error("Failed to read blob %s: %s", inputBlob.name, exc)
        raise

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


def _parse_positive_limit(value: Optional[str], default: int = 50) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(1, min(parsed, 100))


def _read_json_body(req: func.HttpRequest) -> dict[str, object]:
    try:
        payload = json.loads(req.get_body().decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestValidationError("Request body must be a JSON object.") from exc
    if not isinstance(payload, dict):
        raise RequestValidationError("Request body must be a JSON object.")
    return cast(dict[str, object], payload)


def _delete_blob_if_exists(
    container_client: _GalleryContainerClient,
    blob_name: str,
) -> bool:
    try:
        container_client.get_blob_client(blob_name).delete_blob()
        return True
    except ResourceNotFoundError:
        return False


def _admin_gallery_items(
    container_client: _GalleryContainerClient,
    category: str,
    limit: int,
    cursor: Optional[str],
) -> tuple[list[dict[str, object]], Optional[str]]:
    prefix = _gallery_prefix_for_category(category)
    if prefix is None:
        raise RequestValidationError(
            "Unsupported category. Use processed or segmented."
        )
    source_index = _lineage_source_index(container_client)
    normalized_prefix = _normalize_prefix(prefix)
    blobs = [
        blob
        for blob in container_client.list_blobs(name_starts_with=normalized_prefix)
        if _is_gallery_image_blob(blob.name)
    ]
    start = int(cursor) if cursor and cursor.isdigit() else 0
    page = blobs[start : start + limit]
    next_cursor = str(start + limit) if start + limit < len(blobs) else None
    items: list[dict[str, object]] = []
    for blob in page:
        modified = getattr(blob, "last_modified", None)
        modified_utc = modified.astimezone(timezone.utc) if modified else None
        items.append(
            {
                "category": category,
                "name": blob.name,
                "sourceBlobName": source_index.get(blob.name),
                "size": blob.size or 0,
                "lastModifiedUtc": _format_rfc3339(modified_utc)
                if modified_utc
                else None,
                "previewUrl": (
                    "/api/v1/admin/gallery/image?"
                    + urlencode({"category": category, "name": blob.name})
                ),
                "canCascade": blob.name in source_index,
            }
        )
    return items, next_cursor


@app.function_name(name="AdminGalleryImages")
@app.route(
    route="v1/admin/gallery/images",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def admin_gallery_images(req: func.HttpRequest) -> func.HttpResponse:
    """Return admin gallery items for processed scanner outputs."""
    authorized, response = _require_gallery_admin(req)
    if not authorized:
        return cast(func.HttpResponse, response)

    category = (req.params.get("category") or "processed").strip().lower()
    if category not in {"processed", "segmented"}:
        return _json_error(
            "Unsupported category. Use processed or segmented.", 400, "invalid_category"
        )
    limit = _parse_positive_limit(req.params.get("limit"))
    cursor = req.params.get("cursor")
    _, container_client = _get_container_client(GALLERY_CONTAINER_NAME)
    if not container_client:
        return _json_error("Storage is not configured.", 500, "storage_not_configured")

    try:
        items, next_cursor = _admin_gallery_items(
            cast(_GalleryContainerClient, container_client),
            category,
            limit,
            cursor,
        )
    except RequestValidationError as exc:
        return _json_error(str(exc), exc.status_code, exc.code)

    return func.HttpResponse(
        body=json.dumps(
            {
                "category": category,
                "items": items,
                "nextCursor": next_cursor,
            }
        ),
        status_code=200,
        mimetype="application/json",
    )


@app.function_name(name="AdminGalleryImage")
@app.route(
    route="v1/admin/gallery/image",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def admin_gallery_image(req: func.HttpRequest) -> func.HttpResponse:
    """Serve a processed scanner image to an authorized admin."""
    authorized, response = _require_gallery_admin(req)
    if not authorized:
        return cast(func.HttpResponse, response)
    category = (req.params.get("category") or "processed").strip().lower()
    if category not in {"processed", "segmented"}:
        return _json_error(
            "Unsupported category. Use processed or segmented.",
            400,
            "invalid_category",
        )
    return gallery_image(req)


@app.function_name(name="AdminGalleryDeleteBySource")
@app.route(
    route="v1/admin/gallery/actions/delete-by-source",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def admin_gallery_delete_by_source(req: func.HttpRequest) -> func.HttpResponse:
    """Delete processed scanner outputs recorded for a source blob."""
    authorized, response = _require_gallery_admin(req)
    if not authorized:
        return cast(func.HttpResponse, response)

    try:
        payload = _read_json_body(req)
    except RequestValidationError as exc:
        return _json_error(str(exc), exc.status_code, exc.code)
    source_name = str(payload.get("sourceBlobName", "")).strip()
    if not source_name:
        return _json_error(
            "sourceBlobName is required.", 400, "missing_source_blob_name"
        )

    _, container_client = _get_container_client(GALLERY_CONTAINER_NAME)
    if not container_client:
        return _json_error("Storage is not configured.", 500, "storage_not_configured")
    gallery_container = cast(_GalleryContainerClient, container_client)
    manifest = _load_lineage_manifest(gallery_container, source_name)
    if manifest is None:
        return _json_error(
            "Lineage is missing for the requested source blob.",
            409,
            "lineage_missing",
        )

    outputs = manifest.get("outputsByCategory", {})
    deleted: list[str] = []
    missing: list[str] = []
    if isinstance(outputs, dict):
        for names in outputs.values():
            if isinstance(names, list):
                for name in names:
                    if not isinstance(name, str):
                        continue
                    if _delete_blob_if_exists(gallery_container, name):
                        deleted.append(name)
                    else:
                        missing.append(name)
    _delete_blob_if_exists(gallery_container, _lineage_blob_name(source_name))
    log_event(
        logger,
        logging.INFO,
        "admin_gallery_source_deleted",
        source_blob_name=source_name,
        deleted_count=len(deleted),
        missing_count=len(missing),
    )
    return func.HttpResponse(
        body=json.dumps(
            {
                "sourceBlobName": source_name,
                "deleted": deleted,
                "missing": missing,
                "lineageDeleted": True,
            }
        ),
        status_code=200,
        mimetype="application/json",
    )


@app.function_name(name="AdminGalleryDeleteImage")
@app.route(
    route="v1/admin/gallery/actions/delete-image",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def admin_gallery_delete_image(req: func.HttpRequest) -> func.HttpResponse:
    """Delete one processed scanner output by explicit blob name."""
    authorized, response = _require_gallery_admin(req)
    if not authorized:
        return cast(func.HttpResponse, response)

    try:
        payload = _read_json_body(req)
    except RequestValidationError as exc:
        return _json_error(str(exc), exc.status_code, exc.code)

    category = str(payload.get("category", "")).strip().lower()
    name = str(payload.get("name", "")).strip()
    if category not in {"processed", "segmented"}:
        return _json_error(
            "Unsupported category. Use processed or segmented.",
            400,
            "invalid_category",
        )
    if not name:
        return _json_error("name is required.", 400, "missing_blob_name")
    if not _is_gallery_image_blob(name):
        return _json_error(
            "name must reference an image blob.", 400, "invalid_blob_name"
        )

    prefix = _gallery_prefix_for_category(category)
    normalized_prefix = _normalize_prefix(prefix or "")
    if normalized_prefix and not name.startswith(normalized_prefix):
        return _json_error(
            "Blob name does not match category prefix.", 400, "prefix_mismatch"
        )

    _, container_client = _get_container_client(GALLERY_CONTAINER_NAME)
    if not container_client:
        return _json_error("Storage is not configured.", 500, "storage_not_configured")
    deleted = _delete_blob_if_exists(
        cast(_GalleryContainerClient, container_client), name
    )
    log_event(
        logger,
        logging.INFO,
        "admin_gallery_image_deleted",
        category=category,
        blob_name=name,
        deleted=deleted,
    )
    return func.HttpResponse(
        body=json.dumps({"category": category, "name": name, "deleted": deleted}),
        status_code=200,
        mimetype="application/json",
    )


@app.function_name(name="AdminGalleryReprocessSource")
@app.route(
    route="v1/admin/gallery/actions/reprocess-source",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def admin_gallery_reprocess_source(req: func.HttpRequest) -> func.HttpResponse:
    """Reprocess a raw source image provided by the uploader BFF."""
    authorized, response = _require_gallery_admin(req)
    if not authorized:
        return cast(func.HttpResponse, response)

    try:
        payload = _read_json_body(req)
    except RequestValidationError as exc:
        return _json_error(str(exc), exc.status_code, exc.code)
    source_name = str(payload.get("sourceBlobName", "")).strip()
    encoded = str(payload.get("imageBytesBase64", "")).strip()
    if not source_name:
        return _json_error(
            "sourceBlobName is required.", 400, "missing_source_blob_name"
        )
    if not encoded:
        return _json_error("imageBytesBase64 is required.", 400, "missing_image_bytes")

    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except ValueError:
        return _json_error("imageBytesBase64 is invalid.", 400, "invalid_image_bytes")

    _, processed_container = _get_storage_clients()
    if not processed_container:
        return _json_error("Storage is not configured.", 500, "storage_not_configured")

    try:
        result = _process_blob_bytes(source_name, image_bytes, processed_container)
    except RequestValidationError as exc:
        return _json_error(str(exc), exc.status_code, exc.code)

    log_event(
        logger,
        logging.INFO,
        "admin_gallery_source_reprocessed",
        source_blob_name=source_name,
        uploaded_count=result.uploaded_count,
        failed_count=result.failed_count,
    )
    return func.HttpResponse(
        body=json.dumps(
            {
                "sourceBlobName": source_name,
                "uploaded": result.to_payload(),
            }
        ),
        status_code=result.status_code(),
        mimetype="application/json",
    )


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

    correlation_id = correlation_id_from_request(req)
    try:
        since = parse_since_param(req.params.get("since"))
        page_size = _parse_page_size_param(req.params.get("page_size"))
    except RequestValidationError as exc:
        return error_response(
            str(exc),
            status_code=exc.status_code,
            code=exc.code,
            correlation_id=correlation_id,
        )
    continuation_token = (req.params.get("continuation") or "").strip() or None

    _, container_client = _get_container_client(GALLERY_CONTAINER_NAME)
    if not container_client:
        return func.HttpResponse(
            "Storage is not configured. Set AzureWebJobsStorage.", status_code=500
        )

    gallery_container = cast(_GalleryContainerClient, container_client)
    try:
        blobs, latest_modified, next_continuation = _list_blob_images(
            gallery_container,
            prefix,
            category=category,
            use_public_urls=GALLERY_USE_PUBLIC_URLS,
            since=since,
            page_size=page_size,
            continuation_token=continuation_token,
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
        "next_continuation": next_continuation,
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


@app.function_name(name="Ready")
@app.route(route="ready", methods=["GET"], auth_level=READY_AUTH_LEVEL)
def ready(req: func.HttpRequest) -> func.HttpResponse:
    """Readiness endpoint for configuration, storage, and model registry checks."""
    correlation_id = correlation_id_from_request(req)
    components: dict[str, object] = {}
    ready_status = True

    try:
        settings = _settings()
        settings_errors = settings.validate()
    except Exception as exc:
        settings_errors = [str(exc)]
        settings = None
    if settings_errors:
        ready_status = False
        components["settings"] = {"ok": False, "errors": settings_errors}
    else:
        components["settings"] = {"ok": True}

    if settings is not None:
        # Report allowed models only. Do NOT load the model here: /api/ready is a
        # fast liveness/deploy smoke probe, and a synchronous DETR load blocks the
        # request long enough to time out the probe on a cold instance.
        components["models"] = {
            "ok": True,
            "allowed_model_ids": sorted(settings.allowed_model_ids),
            "model_aliases": settings.model_aliases,
        }
    else:
        ready_status = False
        components["models"] = {"ok": False, "errors": ["settings unavailable"]}

    _, container_client = _get_container_client(PROCESSED_CONTAINER_NAME)
    if not container_client:
        ready_status = False
        components["storage"] = {
            "ok": False,
            "container": PROCESSED_CONTAINER_NAME,
            "error": "container client unavailable",
        }
    else:
        try:
            container_client.get_container_properties()
            components["storage"] = {
                "ok": True,
                "container": PROCESSED_CONTAINER_NAME,
            }
        except Exception as exc:
            ready_status = False
            components["storage"] = {
                "ok": False,
                "container": PROCESSED_CONTAINER_NAME,
                "error": str(exc),
            }

    log_event(
        logger,
        logging.INFO if ready_status else logging.WARNING,
        "readiness_checked",
        correlation_id=correlation_id,
        ready=ready_status,
    )
    return json_response(
        {"ready": ready_status, "components": components},
        status_code=200 if ready_status else 503,
        correlation_id=correlation_id,
    )


def _parse_bool_param(value: Optional[str], *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@app.function_name(name="AnalyzeLayout")
@app.route(route="layout", methods=["POST"], auth_level=DEFAULT_AUTH_LEVEL)
def analyze_layout(req: func.HttpRequest) -> func.HttpResponse:
    """Run card detection on uploaded image bytes."""
    correlation_id = correlation_id_from_request(req)
    settings = _settings()
    try:
        image_bytes = read_bounded_http_body(req, settings)
        if not image_bytes:
            raise RequestValidationError("Provide image bytes in the request body.")
        validate_image_bytes(image_bytes, settings)
        params = parse_detection_params(req.params)
        resolve_model_id(params.model_variant, settings)
    except RequestValidationError as exc:
        log_event(
            logger,
            logging.WARNING,
            "layout_request_rejected",
            correlation_id=correlation_id,
            code=exc.code,
            status_code=exc.status_code,
        )
        return error_response(
            str(exc),
            status_code=exc.status_code,
            code=exc.code,
            correlation_id=correlation_id,
        )
    except ModelResolutionError as exc:
        log_event(
            logger,
            logging.WARNING,
            "detection_model_rejected",
            correlation_id=correlation_id,
            model=params.model_variant,
        )
        return error_response(
            str(exc),
            status_code=400,
            code="model_not_allowed",
            correlation_id=correlation_id,
        )

    result = detect_cards_from_image_bytes(
        image_bytes,
        model_variant=params.model_variant,
        imgsz=params.imgsz,
        conf=params.conf,
        iou=params.iou,
        extract_crops=params.extract_crops,
        crop_format=normalize_image_format(params.crop_format),
        settings=settings,
    )
    log_event(
        logger,
        logging.INFO,
        "layout_request_completed",
        correlation_id=correlation_id,
        image_width=result.image_width,
        image_height=result.image_height,
        element_count=len(result.elements),
        error_count=len(result.errors),
        model_id=result.model_info.get("model_id"),
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
    return json_response(
        body,
        status_code=status_code,
        correlation_id=correlation_id,
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
    correlation_id = correlation_id_from_request(req)
    settings = _settings()
    try:
        params = parse_process_params(req.params)
        image_bytes = read_bounded_http_body(req, settings)
        if not image_bytes:
            raise RequestValidationError("Provide image bytes in the request body.")
        validate_image_bytes(image_bytes, settings)
    except RequestValidationError as exc:
        log_event(
            logger,
            logging.WARNING,
            "process_request_rejected",
            correlation_id=correlation_id,
            code=exc.code,
            status_code=exc.status_code,
        )
        return error_response(
            str(exc),
            status_code=exc.status_code,
            code=exc.code,
            correlation_id=correlation_id,
        )

    if params.output_mode == "none":
        payload: dict[str, object] = {
            "card_count": process_utils.count_cards_in_image_bytes(image_bytes)
        }
        return json_response(payload, status_code=200, correlation_id=correlation_id)

    if params.output_mode == "upload":
        _, processed_container = _get_storage_clients()
        if not processed_container:
            return error_response(
                "Storage is not configured. Set AzureWebJobsStorage.",
                status_code=500,
                code="storage_not_configured",
                correlation_id=correlation_id,
            )
        cards = process_utils.extract_card_crops_from_image_bytes(
            image_bytes, max_crops=settings.max_crops
        )

        source_name = (
            (req.params.get("name") or "").strip()
            or req.headers.get("x-file-name")
            or f"upload_{correlation_id}.jpg"
        )
        folder = _build_processed_card_folder(source_name)
        upload_result = _upload_processed_cards(
            processed_container, source_name, cards, folder=folder
        )
        upload_payload = upload_result.to_payload()
        payload = {
            "card_count": len(cards),
            "uploaded": {
                "container": PROCESSED_CONTAINER_NAME,
                "folder": folder,
                **upload_payload,
            },
        }
        log_event(
            logger,
            logging.INFO if not upload_result.has_failures else logging.ERROR,
            "process_upload_completed",
            correlation_id=correlation_id,
            card_count=len(cards),
            uploaded_count=upload_result.uploaded_count,
            failed_count=upload_result.failed_count,
        )
        return json_response(
            payload,
            status_code=upload_result.status_code(),
            correlation_id=correlation_id,
        )

    cards = process_utils.extract_card_crops_from_image_bytes(
        image_bytes, max_crops=settings.max_crops
    )

    if params.output_format == "json":
        payload = {
            "card_count": len(cards),
            "cards": [
                {"index": idx, "name": name, "bytes": len(img_bytes)}
                for idx, (name, img_bytes) in enumerate(cards, 1)
            ],
        }
        return json_response(payload, status_code=200, correlation_id=correlation_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, (name, img_bytes) in enumerate(cards, 1):
            member_name = _sanitize_zip_member_name(name or "card")
            zf.writestr(f"{idx:02d}_{member_name}.jpg", img_bytes)

    zip_bytes = buf.getvalue()
    if len(zip_bytes) > settings.max_return_bytes:
        return error_response(
            "Processed result exceeds the configured response size limit.",
            status_code=413,
            code="response_too_large",
            correlation_id=correlation_id,
        )

    headers = {
        "Content-Disposition": "attachment; filename=processed_cards.zip",
        "X-Card-Count": str(len(cards)),
        "x-correlation-id": correlation_id,
    }
    return func.HttpResponse(
        body=zip_bytes,
        status_code=200,
        mimetype="application/zip",
        headers=headers,
    )
