import base64
import io
import json
import logging
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import urlencode

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import (
    BlobServiceClient,
    ContainerClient,
)

from CardProcessor import process_utils
from CardProcessor.layout_analysis import analyze_layout_from_image_bytes

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
GALLERY_REFRESH_SECONDS = float(os.environ.get("GALLERY_REFRESH_SECONDS", "12000"))
GALLERY_USE_PUBLIC_URLS = (
    os.environ.get("GALLERY_USE_PUBLIC_URLS", "").strip().lower()
    in {"1", "true", "yes", "on"}
)
STORAGE_AUTH_MODE = (
    os.environ.get("STORAGE_AUTH_MODE", "connection_string").strip().lower()
)
STORAGE_ACCOUNT_URL = os.environ.get("STORAGE_ACCOUNT_URL")


def _resolve_auth_level(value: Optional[str], default: func.AuthLevel) -> func.AuthLevel:
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
            logging.error("azure-identity is not installed; cannot use managed identity")
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


def _build_gallery_image_url(
    container_client: ContainerClient,
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
    container_client: ContainerClient,
    prefix: str,
    *,
    category: str,
    auth_code: Optional[str],
    use_public_urls: bool,
) -> List[Dict[str, object]]:
    blobs = []
    normalized_prefix = _normalize_prefix(prefix)
    for blob in container_client.list_blobs(name_starts_with=normalized_prefix):
        last_modified = (
            blob.last_modified.astimezone(timezone.utc).isoformat()
            if getattr(blob, "last_modified", None)
            else None
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
    return blobs


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
    processed_container: ContainerClient,
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
    try:
        blobs = _list_blob_images(
            container_client,
            prefix,
            category=category,
            auth_code=auth_code,
            use_public_urls=GALLERY_USE_PUBLIC_URLS,
        )
    except Exception as exc:
        logging.error("Failed to list blobs for gallery: %s", exc)
        return func.HttpResponse("Failed to list images.", status_code=500)

    payload = {
        "container": container_client.container_name,
        "category": category,
        "prefix": _normalize_prefix(prefix),
        "blobs": blobs,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "refresh_seconds": GALLERY_REFRESH_SECONDS,
    }
    return func.HttpResponse(
        body=json.dumps(payload), status_code=200, mimetype="application/json"
    )


@app.function_name(name="GalleryPage")
@app.route(route="gallery", methods=["GET"], auth_level=GALLERY_AUTH_LEVEL)
def gallery_page(req: func.HttpRequest) -> func.HttpResponse:
    """Serve a minimal gallery UI for browsing card images."""
    html = f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Trading Card Gallery</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@600;700&family=Work+Sans:wght@400;500;600&display=swap" rel="stylesheet">
        <style>
            :root {{
                color-scheme: light;
                --bg: #f8f7f3;
                --bg-wash: #eef3f2;
                --surface: #ffffff;
                --surface-muted: #f7f8fa;
                --border: #e5e7eb;
                --border-soft: #edeff2;
                --text: #1f2933;
                --muted: #6b7280;
                --accent: #0f766e;
                --accent-soft: #e4f3f1;
                --shadow-sm: 0 10px 25px rgba(15, 23, 42, 0.06);
                --shadow-xs: 0 4px 12px rgba(15, 23, 42, 0.05);
            }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: "Work Sans", "Helvetica Neue", sans-serif;
                background:
                    radial-gradient(1200px 700px at 12% -10%, #fff9eb 0%, transparent 60%),
                    radial-gradient(900px 600px at 95% 0%, #e6f2f1 0%, transparent 55%),
                    linear-gradient(180deg, #f8f7f3 0%, #eef2f7 100%);
                color: var(--text);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                line-height: 1.5;
            }}
            header {{
                position: sticky;
                top: 0;
                z-index: 10;
                background: rgba(250, 249, 246, 0.92);
                backdrop-filter: blur(8px);
                border-bottom: 1px solid var(--border);
                padding: 16px 20px;
                display: flex;
                gap: 12px;
                align-items: center;
            }}
            .header-left {{
                display: flex;
                flex-direction: column;
                gap: 4px;
            }}
            h1 {{
                font-family: "Fraunces", "Iowan Old Style", serif;
                font-size: 20px;
                font-weight: 700;
                letter-spacing: 0.2px;
            }}
            .status {{
                font-size: 12px;
                color: var(--muted);
            }}
            .status.error {{
                color: #b45309;
            }}
            .header-right {{
                margin-left: auto;
            }}
            .tabs {{
                display: inline-flex;
                align-items: center;
                gap: 4px;
                padding: 4px;
                border-radius: 999px;
                background: var(--surface);
                border: 1px solid var(--border);
                box-shadow: var(--shadow-xs);
            }}
            .tab {{
                border: 0;
                background: transparent;
                color: var(--muted);
                padding: 6px 14px;
                border-radius: 999px;
                cursor: pointer;
                transition: background 0.15s ease, color 0.15s ease;
                font-size: 13px;
                font-weight: 500;
                font-family: inherit;
            }}
            .tab:hover {{
                color: var(--text);
            }}
            .tab.active {{
                background: var(--accent-soft);
                color: var(--accent);
                font-weight: 600;
            }}
            .tab:focus-visible {{
                outline: 2px solid var(--accent);
                outline-offset: 2px;
            }}
            main {{
                flex: 1;
                overflow-y: auto;
                padding: 24px 20px 48px;
                width: 100%;
                max-width: 1280px;
                margin: 0 auto;
            }}
            .gallery {{
                display: flex;
                flex-direction: column;
                gap: 24px;
            }}
            .folder {{
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 16px;
                box-shadow: var(--shadow-sm);
                overflow: hidden;
            }}
            .folder-header {{
                padding: 14px 18px;
                display: flex;
                align-items: baseline;
                gap: 8px;
                border-bottom: 1px solid var(--border-soft);
            }}
            .folder-title {{
                font-size: 14px;
                font-weight: 600;
            }}
            .folder-count {{
                color: var(--muted);
                font-size: 12px;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
                gap: 18px;
                padding: 18px;
            }}
            .card {{
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 14px;
                overflow: hidden;
                display: flex;
                flex-direction: column;
                box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
                transition: transform 0.15s ease, box-shadow 0.15s ease;
                animation: fadeUp 0.35s ease both;
                animation-delay: calc(var(--stagger, 0) * 35ms);
            }}
            .card:hover {{
                transform: translateY(-2px);
                box-shadow: 0 12px 28px rgba(15, 23, 42, 0.08);
            }}
            .thumb {{
                background: var(--surface-muted);
                padding: 8px;
                display: grid;
                place-items: center;
            }}
            .thumb img {{
                width: 100%;
                height: auto;
                aspect-ratio: 3 / 4;
                object-fit: cover;
                object-position: center;
                border-radius: 10px;
                background: #ffffff;
                box-shadow: inset 0 0 0 1px var(--border-soft);
            }}
            .meta {{
                padding: 12px 14px 14px;
                display: flex;
                flex-direction: column;
                gap: 6px;
            }}
            .empty {{
                text-align: center;
                padding: 28px;
                border: 1px dashed var(--border);
                border-radius: 16px;
                color: var(--muted);
                background: var(--surface);
            }}
            .name {{
                font-weight: 600;
                color: var(--text);
                font-size: 13px;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }}
            .details {{
                color: var(--muted);
                font-size: 12px;
            }}
            @keyframes fadeUp {{
                from {{ opacity: 0; transform: translateY(6px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}
            @media (max-width: 720px) {{
                header {{
                    flex-direction: column;
                    align-items: flex-start;
                    gap: 10px;
                }}
                .header-right {{
                    width: 100%;
                }}
                .tabs {{
                    width: 100%;
                    justify-content: space-between;
                }}
            }}
            @media (prefers-reduced-motion: reduce) {{
                * {{
                    animation: none !important;
                    transition: none !important;
                }}
            }}
        </style>
    </head>
    <body>
        <header>
            <div class="header-left">
                <h1>Card Gallery</h1>
                <div class="status" id="status" aria-live="polite"></div>
            </div>
            <div class="header-right">
                <div class="tabs" role="tablist" aria-label="Gallery categories">
                    <button class="tab" data-category="input" role="tab" aria-selected="false">Input</button>
                    <button class="tab" data-category="processed" role="tab" aria-selected="false">Processed</button>
                    <button class="tab" data-category="segmented" role="tab" aria-selected="false">Segmented</button>
                </div>
            </div>
        </header>
        <main>
            <div class="gallery" id="gallery"></div>
        </main>
        <script>
            const refreshSeconds = {GALLERY_REFRESH_SECONDS};
            const defaultCategory = "processed";
            const urlParams = new URLSearchParams(window.location.search);
            const authCode = urlParams.get("code");
            let currentCategory = defaultCategory;
            const galleryEl = document.getElementById("gallery");
            const statusEl = document.getElementById("status");

            function formatBytes(bytes) {{
                if (!bytes) return "0 B";
                if (bytes < 1024) return `${{bytes}} B`;
                const kb = bytes / 1024;
                if (kb < 1024) return `${{Math.round(kb)}} KB`;
                return `${{(kb / 1024).toFixed(2)}} MB`;
            }}

            function formatDate(value) {{
                if (!value) return "";
                const parsed = new Date(value);
                if (Number.isNaN(parsed.getTime())) return value;
                return parsed.toLocaleString();
            }}

            function setActiveTab(category) {{
                document.querySelectorAll(".tab").forEach((btn) => {{
                    const isActive = btn.dataset.category === category;
                    btn.classList.toggle("active", isActive);
                    btn.setAttribute("aria-selected", isActive ? "true" : "false");
                }});
            }}

            function normalizeRelativeName(name, prefix) {{
                const normalizedPrefix = prefix || "";
                if (normalizedPrefix && name.startsWith(normalizedPrefix)) {{
                    return name.slice(normalizedPrefix.length);
                }}
                return name;
            }}

            function groupByFolder(blobs, prefix) {{
                const folders = new Map();
                blobs.forEach((blob) => {{
                    const relativeName = normalizeRelativeName(blob.name || "", prefix);
                    const parts = relativeName.split("/").filter(Boolean);
                    const folder = parts.length > 1 ? parts.slice(0, -1).join("/") : "root";
                    if (!folders.has(folder)) {{
                        folders.set(folder, []);
                    }}
                    folders.get(folder).push(blob);
                }});
                return Array.from(folders.entries()).map(([folder, items]) => ({{
                    folder,
                    items,
                }}));
            }}

            function formatFolderName(folder) {{
                return folder === "root" ? "Root" : folder;
            }}

            function formatDisplayName(blob, prefix) {{
                const relativeName = normalizeRelativeName(blob.name || "", prefix);
                const parts = relativeName.split("/").filter(Boolean);
                const shortName = parts.length ? parts[parts.length - 1] : relativeName;
                return {{
                    shortName,
                    fullName: relativeName || blob.name || "",
                }};
            }}

            function renderFolders(groups, prefix) {{
                if (!groups.length) {{
                    return `<div class="empty">No images yet for this category.</div>`;
                }}

                return groups.map((group) => {{
                    const folderName = formatFolderName(group.folder);
                    const cards = group.items.map((blob, index) => {{
                        const nameInfo = formatDisplayName(blob, prefix);
                        const dateLabel = formatDate(blob.last_modified);
                        const details = [formatBytes(blob.size), dateLabel].filter(Boolean).join(" | ");
                        return `
                            <article class="card" style="--stagger: ${{index}}">
                                <div class="thumb">
                                    <img loading="lazy" decoding="async" src="${{blob.url}}" alt="${{nameInfo.fullName}}" />
                                </div>
                                <div class="meta">
                                    <div class="name" title="${{nameInfo.fullName}}">${{nameInfo.shortName}}</div>
                                    <div class="details">${{details}}</div>
                                </div>
                            </article>
                        `;
                    }}).join("");
                    return `
                        <section class="folder">
                            <div class="folder-header">
                                <div class="folder-title" title="${{group.folder}}">${{folderName}}</div>
                                <div class="folder-count">${{group.items.length}} image(s)</div>
                            </div>
                            <div class="grid">
                                ${{cards}}
                            </div>
                        </section>
                    `;
                }}).join("");
            }}

            function buildApiUrl(path, params) {{
                const query = new URLSearchParams(params);
                if (authCode) {{
                    query.set("code", authCode);
                }}
                const qs = query.toString();
                return qs ? `${{path}}?${{qs}}` : path;
            }}

            async function loadGallery(category) {{
                currentCategory = category;
                setActiveTab(category);
                statusEl.classList.remove("error");
                statusEl.textContent = "Loading images...";
                galleryEl.innerHTML = `<div class="empty">Loading images...</div>`;
                try {{
                    const response = await fetch(
                        buildApiUrl("/api/gallery/images", {{ category }}),
                        {{ cache: "no-store" }}
                    );
                    if (!response.ok) throw new Error(`Request failed: ${{response.status}}`);
                    const payload = await response.json();
                    const blobs = Array.isArray(payload.blobs) ? payload.blobs : [];
                    const groups = groupByFolder(blobs, payload.prefix || "");
                    galleryEl.innerHTML = renderFolders(groups, payload.prefix || "");
                    statusEl.textContent = `${{blobs.length}} image(s) in ${{groups.length}} folder(s) | Updated ${{new Date().toLocaleTimeString()}}`;
                }} catch (err) {{
                    console.error(err);
                    statusEl.classList.add("error");
                    statusEl.textContent = `Error: ${{err.message}}`;
                    galleryEl.innerHTML = `<div class="empty">We couldn't load images. Try again shortly.</div>`;
                }}
            }}

            document.querySelectorAll(".tab").forEach((btn) => {{
                btn.addEventListener("click", () => loadGallery(btn.dataset.category));
            }});

            loadGallery(defaultCategory);
            setInterval(() => loadGallery(currentCategory), refreshSeconds * 1000);
        </script>
    </body>
    </html>
    """
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
        content_type = (
            props.content_settings.content_type or "application/octet-stream"
        )
        data = blob_client.download_blob().readall()
    except ResourceNotFoundError:
        return func.HttpResponse("Blob not found.", status_code=404)
    except Exception as exc:
        logging.error("Failed to download blob %s: %s", name, exc)
        return func.HttpResponse("Failed to download image.", status_code=500)

    headers = {"Cache-Control": "public, max-age=60"}
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
