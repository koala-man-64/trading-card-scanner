import base64
import io
import json
import logging
import os
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import quote

import azure.functions as func
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContainerClient,
    generate_blob_sas,
)

from CardProcessor import process_utils
from CardProcessor.layout_analysis import analyze_layout_from_image_bytes

app = func.FunctionApp()

# Define container names from environment variables with defaults
PROCESSED_CONTAINER_NAME = os.environ.get("PROCESSED_CONTAINER_NAME", "processed")
INPUT_CONTAINER_NAME = os.environ.get("INPUT_CONTAINER_NAME", "input")
GALLERY_CONTAINER_NAME = os.environ.get(
    "GALLERY_CONTAINER_NAME", PROCESSED_CONTAINER_NAME
)
GALLERY_RAW_PREFIX = os.environ.get("GALLERY_RAW_PREFIX", "raw")
GALLERY_PROCESSED_PREFIX = os.environ.get("GALLERY_PROCESSED_PREFIX", "processed")
GALLERY_SEGMENTED_PREFIX = os.environ.get("GALLERY_SEGMENTED_PREFIX", "segmented")
GALLERY_REFRESH_SECONDS = float(os.environ.get("GALLERY_REFRESH_SECONDS", "5"))


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
        logging.error("Failed to create blob container client for %s: %s", container_name, exc)
        return None, None


def _extract_account_key(connection: Optional[str]) -> Optional[str]:
    if not connection:
        return None

    for part in connection.split(";"):
        if part.lower().startswith("accountkey="):
            return part.split("=", 1)[1]
    return None


def _normalize_prefix(prefix: str) -> str:
    cleaned = prefix.strip().strip("/")
    return f"{cleaned}/" if cleaned else ""


def _build_blob_url(
    container_client: ContainerClient, blob_name: str, account_key: Optional[str]
) -> str:
    blob_client = container_client.get_blob_client(blob_name)
    base_url = blob_client.url

    if not account_key:
        return base_url

    sas_token = generate_blob_sas(
        account_name=container_client.account_name,
        container_name=container_client.container_name,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return f"{base_url}?{sas_token}"


def _list_blob_images(
    container_client: ContainerClient, prefix: str, account_key: Optional[str]
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
                "url": _build_blob_url(container_client, blob.name, account_key),
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
    if normalized == "raw":
        return GALLERY_RAW_PREFIX
    if normalized == "processed":
        return GALLERY_PROCESSED_PREFIX
    if normalized == "segmented":
        return GALLERY_SEGMENTED_PREFIX
    return None


@app.function_name(name="GalleryImages")
@app.route(route="gallery/images", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def gallery_images(req: func.HttpRequest) -> func.HttpResponse:
    """Return JSON listing of blobs for the requested gallery category."""
    category = (req.params.get("category") or "processed").strip().lower()
    prefix = _gallery_prefix_for_category(category)
    if prefix is None:
        return func.HttpResponse(
            "Unsupported category. Use raw, processed, or segmented.",
            status_code=400,
        )

    service_client, container_client = _get_container_client(GALLERY_CONTAINER_NAME)
    if not container_client:
        return func.HttpResponse(
            "Storage is not configured. Set AzureWebJobsStorage.", status_code=500
        )

    account_key = _extract_account_key(os.environ.get("AzureWebJobsStorage"))
    try:
        blobs = _list_blob_images(container_client, prefix, account_key)
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
@app.route(route="gallery", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def gallery_page(req: func.HttpRequest) -> func.HttpResponse:
    """Serve a minimal gallery UI for browsing card images."""
    html = f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Trading Card Gallery</title>
        <style>
            :root {{ color-scheme: light dark; }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background: #0b0c10;
                color: #e5e7eb;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
            }}
            header {{
                position: sticky;
                top: 0;
                z-index: 10;
                background: rgba(15, 17, 26, 0.9);
                backdrop-filter: blur(6px);
                border-bottom: 1px solid #1f2937;
                padding: 12px 16px;
                display: flex;
                gap: 12px;
                align-items: center;
            }}
            h1 {{ font-size: 18px; font-weight: 600; margin-right: auto; }}
            .tab {{
                border: 1px solid #1f2937;
                background: #111827;
                color: #e5e7eb;
                padding: 8px 14px;
                border-radius: 10px;
                cursor: pointer;
                transition: all 0.15s ease;
                text-transform: capitalize;
            }}
            .tab:hover {{ border-color: #2563eb; color: #bfdbfe; }}
            .tab.active {{
                background: linear-gradient(135deg, #2563eb, #9333ea);
                border-color: transparent;
                color: white;
                box-shadow: 0 8px 24px rgba(37, 99, 235, 0.35);
            }}
            .status {{
                margin-left: 8px;
                font-size: 13px;
                color: #9ca3af;
            }}
            main {{
                flex: 1;
                overflow-y: auto;
                padding: 18px;
            }}
            .gallery {{
                display: flex;
                flex-direction: column;
                gap: 16px;
            }}
            .folder {{
                background: #0f1628;
                border: 1px solid #1f2937;
                border-radius: 12px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.35);
                overflow: hidden;
            }}
            .folder-header {{
                padding: 12px;
                display: flex;
                align-items: baseline;
                gap: 8px;
            }}
            .folder-title {{
                font-size: 15px;
                font-weight: 600;
                color: #e5e7eb;
            }}
            .folder-count {{
                color: #9ca3af;
                font-size: 12px;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
                gap: 16px;
            }}
            .card {{
                background: #0f172a;
                border: 1px solid #1f2937;
                border-radius: 12px;
                overflow: hidden;
                display: flex;
                flex-direction: column;
                box-shadow: 0 10px 30px rgba(0,0,0,0.35);
                transition: transform 0.12s ease, box-shadow 0.12s ease;
            }}
            .card:hover {{
                transform: translateY(-2px);
                box-shadow: 0 14px 36px rgba(0,0,0,0.45);
            }}
            .thumb {{
                background: #0b1220;
                padding: 8px;
                display: grid;
                place-items: center;
                min-height: 240px;
            }}
            .thumb img {{
                width: 100%;
                height: 100%;
                object-fit: contain;
                border-radius: 8px;
                background: #111827;
            }}
            .meta {{
                padding: 12px;
                display: flex;
                flex-direction: column;
                gap: 6px;
            }}
            .empty {{
                text-align: center;
                padding: 20px;
                border: 1px dashed #1f2937;
                border-radius: 12px;
                color: #9ca3af;
                background: #0f172a;
            }}
            .name {{ font-weight: 600; color: #e5e7eb; font-size: 14px; }}
            .details {{ color: #9ca3af; font-size: 12px; }}
            @media (max-width: 640px) {{
                header {{ flex-wrap: wrap; gap: 8px; }}
                h1 {{ width: 100%; margin-bottom: 6px; }}
            }}
        </style>
    </head>
    <body>
        <header>
            <h1>Card Gallery</h1>
            <button class="tab" data-category="raw">Raw</button>
            <button class="tab" data-category="processed">Processed</button>
            <button class="tab" data-category="segmented">Segmented</button>
            <div class="status" id="status"></div>
        </header>
        <main>
            <div class="gallery" id="gallery"></div>
        </main>
        <script>
            const refreshSeconds = {GALLERY_REFRESH_SECONDS};
            const defaultCategory = "processed";
            let currentCategory = defaultCategory;
            const galleryEl = document.getElementById("gallery");
            const statusEl = document.getElementById("status");

            function formatBytes(bytes) {{
                if (!bytes) return "0 KB";
                const kb = bytes / 1024;
                if (kb < 1024) return `${{Math.round(kb)}} KB`;
                return `${{(kb / 1024).toFixed(2)}} MB`;
            }}

            function setActiveTab(category) {{
                document.querySelectorAll(".tab").forEach((btn) => {{
                    btn.classList.toggle("active", btn.dataset.category === category);
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

            function renderFolders(groups) {{
                if (!groups.length) {{
                    return `<div class="empty">No images found for this category.</div>`;
                }}

                return groups.map((group) => {{
                    const cards = group.items.map((blob) => `
                        <article class="card">
                            <div class="thumb">
                                <img loading="lazy" src="${{blob.url}}" alt="${{blob.name}}" />
                            </div>
                            <div class="meta">
                                <div class="name" title="${{blob.name}}">${{blob.name}}</div>
                                <div class="details">${{formatBytes(blob.size)}} • ${{blob.last_modified || ""}}</div>
                            </div>
                        </article>
                    `).join("");
                    return `
                        <section class="folder">
                            <div class="folder-header">
                                <div class="folder-title" title="${{group.folder}}">${{group.folder}}</div>
                                <div class="folder-count">${{group.items.length}} image(s)</div>
                            </div>
                            <div class="grid">
                                ${{cards}}
                            </div>
                        </section>
                    `;
                }}).join("");
            }}

            async function loadGallery(category) {{
                currentCategory = category;
                setActiveTab(category);
                statusEl.textContent = "Loading...";
                try {{
                    const response = await fetch(`/api/gallery/images?category=${{encodeURIComponent(category)}}`, {{ cache: "no-store" }});
                    if (!response.ok) throw new Error(`Request failed: ${{response.status}}`);
                    const payload = await response.json();
                    const blobs = Array.isArray(payload.blobs) ? payload.blobs : [];
                    const groups = groupByFolder(blobs, payload.prefix || "");
                    galleryEl.innerHTML = renderFolders(groups);
                    statusEl.textContent = `${{blobs.length}} image(s) across ${{groups.length}} folder(s) • Updated ${{new Date().toLocaleTimeString()}}`;
                }} catch (err) {{
                    console.error(err);
                    statusEl.textContent = `Error: ${{err.message}}`;
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


@app.function_name(name="Health")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Simple health endpoint for Postman/smoke tests."""
    return func.HttpResponse("OK", status_code=200)


def _parse_bool_param(value: Optional[str], *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@app.function_name(name="AnalyzeLayout")
@app.route(route="layout", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
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
@app.route(route="process", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
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
