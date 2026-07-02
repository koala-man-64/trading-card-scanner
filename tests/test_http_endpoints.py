import base64
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Union
from urllib.parse import parse_qs, urlparse

import azure.functions as func
import pytest
from azure.core.exceptions import ResourceNotFoundError
from PIL import Image

import function_app
from card_processor.detection_types import DetectionResult, DetectedCard
from card_processor.upload_results import UploadBatchResult


class _StubRequest:
    def __init__(
        self,
        body: bytes = b"",
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._body = body
        self.params = params or {}
        self.headers = headers or {}

    def get_body(self) -> bytes:
        return self._body


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), color="white").save(buf, format="PNG")
    return buf.getvalue()


class _StubBlob:
    def __init__(
        self, name: str, size: int = 0, last_modified: Optional[datetime] = None
    ) -> None:
        self.name = name
        self.size = size
        self.last_modified = last_modified


class _StubContentSettings:
    def __init__(self, content_type: Optional[str]) -> None:
        self.content_type = content_type


class _StubBlobProperties:
    def __init__(
        self,
        content_type: Optional[str],
        etag: Optional[str] = None,
        last_modified: Optional[datetime] = None,
    ) -> None:
        self.content_settings = _StubContentSettings(content_type)
        self.etag = etag
        self.last_modified = last_modified


class _StubDownload:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class _StubBlobClient:
    def __init__(
        self,
        name: str,
        data_map: Dict[str, bytes],
        content_types: Dict[str, str],
        etag_map: Optional[Dict[str, str]] = None,
        last_modified_map: Optional[Dict[str, datetime]] = None,
    ) -> None:
        self._name = name
        self._data_map = data_map
        self._content_types = content_types
        self._etag_map = etag_map or {}
        self._last_modified_map = last_modified_map or {}
        self.url = f"https://example.blob.core.windows.net/container/{name}"

    def get_blob_properties(self) -> _StubBlobProperties:
        if self._name not in self._data_map:
            raise ResourceNotFoundError(message="Blob not found")
        content_type = self._content_types.get(self._name)
        return _StubBlobProperties(
            content_type,
            etag=self._etag_map.get(self._name),
            last_modified=self._last_modified_map.get(self._name),
        )

    def download_blob(self) -> _StubDownload:
        if self._name not in self._data_map:
            raise ResourceNotFoundError(message="Blob not found")
        return _StubDownload(self._data_map[self._name])

    def delete_blob(self) -> None:
        if self._name not in self._data_map:
            raise ResourceNotFoundError(message="Blob not found")
        del self._data_map[self._name]


class _StubPageIterator:
    """Single-page iterator mimicking azure-storage-blob's by_page() pager."""

    def __init__(self, page_items: List[_StubBlob], next_token: Optional[str]) -> None:
        self._page_items = page_items
        self._next_token = next_token
        self.continuation_token: Optional[str] = None
        self._yielded = False

    def __iter__(self) -> "_StubPageIterator":
        return self

    def __next__(self) -> List[_StubBlob]:
        if self._yielded:
            raise StopIteration
        self._yielded = True
        self.continuation_token = self._next_token
        return self._page_items


class _StubBlobPager(list):
    """List result that also supports continuation-token paging via by_page()."""

    def __init__(self, items: List[_StubBlob], page_size: Optional[int]) -> None:
        super().__init__(items)
        self._page_size = page_size

    def by_page(self, continuation_token: Optional[str] = None) -> _StubPageIterator:
        start = int(continuation_token) if continuation_token else 0
        size = self._page_size or len(self)
        page_items = list(self)[start : start + size]
        next_start = start + size
        next_token = str(next_start) if next_start < len(self) else None
        return _StubPageIterator(page_items, next_token)


class _StubContainerClient:
    def __init__(
        self,
        blobs: Optional[List[_StubBlob]] = None,
        data_map: Optional[Dict[str, bytes]] = None,
        content_types: Optional[Dict[str, str]] = None,
        etag_map: Optional[Dict[str, str]] = None,
        last_modified_map: Optional[Dict[str, datetime]] = None,
    ) -> None:
        self._blobs = list(blobs or [])
        self._data_map = data_map or {}
        self._content_types = content_types or {}
        self._etag_map = etag_map or {}
        self._last_modified_map = last_modified_map or {}
        self.account_name = "acct"
        self.container_name = "container"
        self.last_prefix: Optional[str] = None

    def list_blobs(
        self,
        name_starts_with: Optional[str] = None,
        include: Optional[Union[str, List[str]]] = None,
        *,
        timeout: Optional[int] = None,
        **kwargs: object,
    ):
        self.last_prefix = name_starts_with
        if name_starts_with is None:
            filtered = list(self._blobs)
        else:
            filtered = [
                blob for blob in self._blobs if blob.name.startswith(name_starts_with)
            ]
        page_size = kwargs.get("results_per_page")
        return _StubBlobPager(
            filtered, page_size=page_size if isinstance(page_size, int) else None
        )

    def get_blob_client(
        self,
        blob: str,
        snapshot: Optional[str] = None,
        *,
        version_id: Optional[str] = None,
    ) -> _StubBlobClient:
        return _StubBlobClient(
            blob,
            self._data_map,
            self._content_types,
            etag_map=self._etag_map,
            last_modified_map=self._last_modified_map,
        )

    def get_container_properties(self):
        return {"name": self.container_name}

    def upload_blob(
        self,
        name: str,
        data: bytes,
        *,
        overwrite: bool,
        **kwargs: object,
    ) -> object:
        if not overwrite and name in self._data_map:
            raise ValueError("blob exists")
        self._data_map[name] = data
        self._content_types.setdefault(name, "application/json")
        if not any(blob.name == name for blob in self._blobs):
            self._blobs.append(
                _StubBlob(
                    name, size=len(data), last_modified=datetime.now(timezone.utc)
                )
            )
        return object()


def _admin_header(
    *,
    scopes: str = "gallery.manage",
    oid: str = "admin-user",
    roles: Optional[str] = None,
) -> Dict[str, str]:
    claims = [
        {"typ": "scp", "val": scopes},
        {"typ": "oid", "val": oid},
    ]
    if roles is not None:
        claims.append({"typ": "roles", "val": roles})
    encoded = base64.b64encode(json.dumps({"claims": claims}).encode("utf-8")).decode(
        "ascii"
    )
    return {"x-ms-client-principal": encoded}


def test_resolve_auth_level_defaults_and_validation() -> None:
    default = func.AuthLevel.FUNCTION
    assert function_app._resolve_auth_level(None, default) == default
    assert (
        function_app._resolve_auth_level("anonymous", default)
        == func.AuthLevel.ANONYMOUS
    )
    assert (
        function_app._resolve_auth_level("FUNCTION", default) == func.AuthLevel.FUNCTION
    )
    assert function_app._resolve_auth_level("admin", default) == func.AuthLevel.ADMIN
    assert function_app._resolve_auth_level("unknown", default) == default


def test_input_blob_trigger_path_defaults_to_raw_prefix() -> None:
    assert function_app.INPUT_BLOB_TRIGGER_PATH == "input/raw/{name}"


def test_process_blob_trigger_uses_event_grid_source() -> None:
    process_blob = next(
        item
        for item in function_app.app.get_functions()
        if item.get_function_name() == "ProcessBlob"
    )
    binding = process_blob.get_bindings_dict()["bindings"][0]

    assert binding["type"] == "blobTrigger"
    assert binding["path"] == function_app.INPUT_BLOB_TRIGGER_PATH
    assert binding["source"] == "EventGrid"


def test_build_input_blob_trigger_path_allows_empty_prefix() -> None:
    assert function_app._build_input_blob_trigger_path("input", "") == "input/{name}"


def test_gallery_prefix_for_category() -> None:
    assert (
        function_app._gallery_prefix_for_category("input")
        == function_app.GALLERY_INPUT_PREFIX
    )
    assert function_app._gallery_prefix_for_category("processed") == ""
    assert (
        function_app._gallery_prefix_for_category("segmented")
        == function_app.GALLERY_SEGMENTED_PREFIX
    )
    assert function_app._gallery_prefix_for_category("bad") is None


def test_build_gallery_image_url_public() -> None:
    container = _StubContainerClient()
    url = function_app._build_gallery_image_url(
        container,
        "processed/card.jpg",
        category="processed",
        use_public_urls=True,
    )
    assert url.endswith("/processed/card.jpg")


def test_build_gallery_image_url_proxy_does_not_include_code() -> None:
    container = _StubContainerClient()
    url = function_app._build_gallery_image_url(
        container,
        "processed/card one.jpg",
        category="processed",
        use_public_urls=False,
    )
    parsed = urlparse(url)
    assert parsed.path == "/api/gallery/image"
    qs = parse_qs(parsed.query)
    assert qs["name"] == ["processed/card one.jpg"]
    assert qs["category"] == ["processed"]
    assert "code" not in qs


def test_list_blob_images_builds_payloads() -> None:
    last_modified = datetime(2025, 1, 1, tzinfo=timezone.utc)
    blobs = [
        _StubBlob("processed/a.jpg", size=120, last_modified=last_modified),
        _StubBlob("processed/b.jpg", size=0, last_modified=None),
    ]
    container = _StubContainerClient(blobs=blobs)

    items, latest_modified, next_continuation = function_app._list_blob_images(
        container,
        "processed",
        category="processed",
        use_public_urls=False,
    )

    assert container.last_prefix == "processed/"
    assert items[0]["last_modified"] == function_app._format_rfc3339(last_modified)
    assert items[1]["last_modified"] is None
    assert str(items[0]["url"]).startswith("/api/gallery/image?")
    assert latest_modified == last_modified
    assert next_continuation is None


def test_list_blob_images_paginates() -> None:
    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    blobs = [
        _StubBlob(f"processed/{idx}.jpg", size=10, last_modified=base_time)
        for idx in range(5)
    ]
    container = _StubContainerClient(blobs=blobs)

    first, _, first_token = function_app._list_blob_images(
        container,
        "processed",
        category="processed",
        use_public_urls=False,
        page_size=2,
    )
    assert [item["name"] for item in first] == ["processed/0.jpg", "processed/1.jpg"]
    assert first_token == "2"

    last, _, last_token = function_app._list_blob_images(
        container,
        "processed",
        category="processed",
        use_public_urls=False,
        page_size=2,
        continuation_token="4",
    )
    assert [item["name"] for item in last] == ["processed/4.jpg"]
    assert last_token is None


def test_list_blob_images_filters_by_since() -> None:
    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    blobs = [
        _StubBlob("processed/old.jpg", size=120, last_modified=base_time),
        _StubBlob(
            "processed/new.jpg",
            size=120,
            last_modified=base_time + timedelta(minutes=5),
        ),
    ]
    container = _StubContainerClient(blobs=blobs)
    since = base_time + timedelta(minutes=1)

    items, latest_modified, _ = function_app._list_blob_images(
        container,
        "processed",
        category="processed",
        use_public_urls=False,
        since=since,
    )

    assert [item["name"] for item in items] == ["processed/new.jpg"]
    assert latest_modified == base_time + timedelta(minutes=5)


def test_gallery_images_invalid_category_returns_400() -> None:
    req = _StubRequest(params={"category": "bad"})
    resp = function_app.gallery_images(req)
    assert resp.status_code == 400


def test_gallery_images_storage_not_configured_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(function_app, "_get_container_client", lambda _: (None, None))
    req = _StubRequest(params={"category": "processed"})
    resp = function_app.gallery_images(req)
    assert resp.status_code == 500


def test_gallery_images_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    last_modified = datetime(2025, 1, 1, tzinfo=timezone.utc)
    blobs = [_StubBlob("processed/a.jpg", size=5, last_modified=last_modified)]
    container = _StubContainerClient(blobs=blobs)
    monkeypatch.setattr(
        function_app, "_get_container_client", lambda _: (None, container)
    )
    monkeypatch.setattr(function_app, "GALLERY_USE_PUBLIC_URLS", False)
    req = _StubRequest(params={"category": "processed", "code": "abc"})

    resp = function_app.gallery_images(req)
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert payload["category"] == "processed"
    assert payload["prefix"] == ""
    assert payload["blobs"][0]["name"] == "processed/a.jpg"
    assert payload["blobs"][0]["url"].startswith("/api/gallery/image?")
    assert "code=" not in payload["blobs"][0]["url"]
    assert payload["next_since"] == function_app._format_rfc3339(last_modified)
    assert payload["next_continuation"] is None


def test_gallery_images_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    last_modified = datetime(2025, 1, 1, tzinfo=timezone.utc)
    blobs = [
        _StubBlob(f"processed/{idx}.jpg", size=5, last_modified=last_modified)
        for idx in range(3)
    ]
    container = _StubContainerClient(blobs=blobs)
    monkeypatch.setattr(
        function_app, "_get_container_client", lambda _: (None, container)
    )
    monkeypatch.setattr(function_app, "GALLERY_USE_PUBLIC_URLS", False)
    req = _StubRequest(params={"category": "processed", "page_size": "2"})

    resp = function_app.gallery_images(req)
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert len(payload["blobs"]) == 2
    assert payload["next_continuation"] == "2"


def test_gallery_images_invalid_since_returns_400() -> None:
    req = _StubRequest(params={"category": "processed", "since": "not-a-date"})
    resp = function_app.gallery_images(req)
    assert resp.status_code == 400


def test_gallery_images_invalid_page_size_returns_400() -> None:
    req = _StubRequest(params={"category": "processed", "page_size": "0"})
    resp = function_app.gallery_images(req)
    assert resp.status_code == 400


def test_gallery_page_contains_gallery_markup() -> None:
    resp = function_app.gallery_page(_StubRequest())
    body = resp.get_body().decode("utf-8")
    assert "Card Gallery" in body
    assert "/api/gallery/images" in body
    assert "buildApiUrl" in body


def test_gallery_image_missing_name_returns_400() -> None:
    resp = function_app.gallery_image(_StubRequest(params={"category": "processed"}))
    assert resp.status_code == 400


def test_gallery_image_invalid_category_returns_400() -> None:
    resp = function_app.gallery_image(
        _StubRequest(params={"category": "bad", "name": "processed/a.jpg"})
    )
    assert resp.status_code == 400


def test_gallery_image_prefix_mismatch_returns_400() -> None:
    resp = function_app.gallery_image(
        _StubRequest(params={"category": "input", "name": "processed/a.jpg"})
    )
    assert resp.status_code == 400


def test_gallery_image_storage_not_configured_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(function_app, "_get_container_client", lambda _: (None, None))
    resp = function_app.gallery_image(
        _StubRequest(params={"category": "processed", "name": "processed/a.jpg"})
    )
    assert resp.status_code == 500


def test_gallery_image_blob_not_found_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _StubContainerClient(data_map={})
    monkeypatch.setattr(
        function_app, "_get_container_client", lambda _: (None, container)
    )
    resp = function_app.gallery_image(
        _StubRequest(params={"category": "processed", "name": "processed/missing.jpg"})
    )
    assert resp.status_code == 404


def test_gallery_image_returns_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    blob_name = "processed/card.jpg"
    blob_data = {blob_name: b"image-bytes"}
    content_types = {blob_name: "image/jpeg"}
    etag_map = {blob_name: "etag-123"}
    last_modified_map = {blob_name: datetime(2025, 1, 1, tzinfo=timezone.utc)}
    container = _StubContainerClient(
        data_map=blob_data,
        content_types=content_types,
        etag_map=etag_map,
        last_modified_map=last_modified_map,
    )
    monkeypatch.setattr(
        function_app, "_get_container_client", lambda _: (None, container)
    )

    resp = function_app.gallery_image(
        _StubRequest(params={"category": "processed", "name": blob_name})
    )
    assert resp.status_code == 200
    assert resp.get_body() == b"image-bytes"
    assert resp.headers.get("ETag") == "etag-123"
    assert resp.headers.get("Last-Modified") is not None


def test_gallery_image_returns_304_when_etag_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob_name = "processed/card.jpg"
    blob_data = {blob_name: b"image-bytes"}
    content_types = {blob_name: "image/jpeg"}
    etag_map = {blob_name: "etag-123"}
    last_modified_map = {blob_name: datetime(2025, 1, 1, tzinfo=timezone.utc)}
    container = _StubContainerClient(
        data_map=blob_data,
        content_types=content_types,
        etag_map=etag_map,
        last_modified_map=last_modified_map,
    )
    monkeypatch.setattr(
        function_app, "_get_container_client", lambda _: (None, container)
    )

    resp = function_app.gallery_image(
        _StubRequest(
            params={"category": "processed", "name": blob_name},
            headers={"If-None-Match": "etag-123"},
        )
    )
    assert resp.status_code == 304


def test_process_blob_bytes_writes_lineage_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = [("Card One", b"a"), ("Card Two", b"bb")]
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda *_, **__: cards,
    )
    container = _StubContainerClient()

    result = function_app._process_blob_bytes(
        "raw/source.jpg",
        _png_bytes(),
        container,
    )
    manifest_name = function_app._lineage_blob_name("raw/source.jpg")
    manifest = json.loads(container._data_map[manifest_name].decode("utf-8"))

    assert result.uploaded_count == 2
    assert manifest["sourceBlobName"] == "raw/source.jpg"
    assert manifest["outputsByCategory"]["processed"] == [
        "source_1.jpg",
        "source_2.jpg",
    ]


def test_admin_gallery_images_requires_configured_admin() -> None:
    req = _StubRequest(headers=_admin_header(oid="other"))
    resp = function_app.admin_gallery_images(req)

    assert resp.status_code == 403


def test_admin_gallery_images_lists_lineage_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(function_app, "ADMIN_ALLOWED_OBJECT_IDS", {"admin-user"})
    blob = _StubBlob(
        "processed/source_1.jpg",
        size=7,
        last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    manifest_blob = _StubBlob(function_app._lineage_blob_name("raw/source.jpg"))
    manifest = {
        "sourceBlobName": "raw/source.jpg",
        "outputsByCategory": {"processed": ["processed/source_1.jpg"]},
    }
    container = _StubContainerClient(
        blobs=[blob, manifest_blob],
        data_map={
            manifest_blob.name: json.dumps(manifest).encode("utf-8"),
        },
        content_types={manifest_blob.name: "application/json"},
    )
    monkeypatch.setattr(
        function_app, "_get_container_client", lambda _: (None, container)
    )

    resp = function_app.admin_gallery_images(
        _StubRequest(
            params={"category": "processed"},
            headers=_admin_header(),
        )
    )
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert resp.status_code == 200
    assert payload["items"][0]["sourceBlobName"] == "raw/source.jpg"
    assert payload["items"][0]["canCascade"] is True


def test_admin_gallery_image_rejects_input_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(function_app, "ADMIN_ALLOWED_OBJECT_IDS", {"admin-user"})

    resp = function_app.admin_gallery_image(
        _StubRequest(
            params={"category": "input", "name": "raw/source.jpg"},
            headers=_admin_header(),
        )
    )

    assert resp.status_code == 400


def test_admin_gallery_delete_by_source_requires_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(function_app, "ADMIN_ALLOWED_OBJECT_IDS", {"admin-user"})
    container = _StubContainerClient()
    monkeypatch.setattr(
        function_app, "_get_container_client", lambda _: (None, container)
    )

    resp = function_app.admin_gallery_delete_by_source(
        _StubRequest(
            body=json.dumps({"sourceBlobName": "raw/missing.jpg"}).encode("utf-8"),
            headers=_admin_header(),
        )
    )

    assert resp.status_code == 409


def test_admin_gallery_delete_by_source_is_idempotent_for_missing_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(function_app, "ADMIN_ALLOWED_OBJECT_IDS", {"admin-user"})
    manifest_name = function_app._lineage_blob_name("raw/source.jpg")
    manifest = {
        "sourceBlobName": "raw/source.jpg",
        "outputsByCategory": {
            "processed": ["processed/source_1.jpg", "processed/gone.jpg"]
        },
    }
    container = _StubContainerClient(
        blobs=[_StubBlob(manifest_name), _StubBlob("processed/source_1.jpg")],
        data_map={
            manifest_name: json.dumps(manifest).encode("utf-8"),
            "processed/source_1.jpg": b"image",
        },
        content_types={
            manifest_name: "application/json",
            "processed/source_1.jpg": "image/jpeg",
        },
    )
    monkeypatch.setattr(
        function_app, "_get_container_client", lambda _: (None, container)
    )

    resp = function_app.admin_gallery_delete_by_source(
        _StubRequest(
            body=json.dumps({"sourceBlobName": "raw/source.jpg"}).encode("utf-8"),
            headers=_admin_header(),
        )
    )
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert resp.status_code == 200
    assert payload["deleted"] == ["processed/source_1.jpg"]
    assert payload["missing"] == ["processed/gone.jpg"]
    assert manifest_name not in container._data_map


def test_admin_gallery_delete_image_deletes_legacy_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(function_app, "ADMIN_ALLOWED_OBJECT_IDS", {"admin-user"})
    container = _StubContainerClient(
        blobs=[_StubBlob("processed/legacy.jpg")],
        data_map={"processed/legacy.jpg": b"image"},
        content_types={"processed/legacy.jpg": "image/jpeg"},
    )
    monkeypatch.setattr(
        function_app,
        "_get_container_client",
        lambda _: (None, container),
    )

    resp = function_app.admin_gallery_delete_image(
        _StubRequest(
            body=json.dumps(
                {"category": "processed", "name": "processed/legacy.jpg"}
            ).encode("utf-8"),
            headers=_admin_header(),
        )
    )
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert resp.status_code == 200
    assert payload["deleted"] is True
    assert "processed/legacy.jpg" not in container._data_map


def test_admin_gallery_reprocess_source_uploads_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(function_app, "ADMIN_ALLOWED_OBJECT_IDS", {"admin-user"})
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda *_, **__: [("Card One", b"a")],
    )
    container = _StubContainerClient()
    monkeypatch.setattr(function_app, "_get_storage_clients", lambda: (None, container))

    resp = function_app.admin_gallery_reprocess_source(
        _StubRequest(
            body=json.dumps(
                {
                    "sourceBlobName": "raw/source.jpg",
                    "imageBytesBase64": base64.b64encode(_png_bytes()).decode("ascii"),
                }
            ).encode("utf-8"),
            headers=_admin_header(),
        )
    )
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert resp.status_code == 200
    assert payload["uploaded"]["uploaded_count"] == 1
    assert function_app._lineage_blob_name("raw/source.jpg") in container._data_map


def test_health_returns_ok() -> None:
    resp = function_app.health(_StubRequest())
    assert resp.status_code == 200
    assert resp.get_body() == b"OK"


def test_ready_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _StubContainerClient()
    monkeypatch.setattr(
        function_app, "_get_container_client", lambda _: (None, container)
    )
    resp = function_app.ready(_StubRequest())
    payload = json.loads(resp.get_body().decode("utf-8"))
    assert resp.status_code == 200
    assert payload["ready"] is True
    assert payload["components"]["storage"]["ok"] is True
    assert payload["components"]["models"]["ok"] is True


def test_analyze_layout_missing_body_returns_400() -> None:
    resp = function_app.analyze_layout(_StubRequest(body=b""))
    assert resp.status_code == 400


def test_analyze_layout_serializes_response(monkeypatch: pytest.MonkeyPatch) -> None:
    element = DetectedCard(
        label="Text",
        confidence=0.9,
        bbox_xyxy=(0, 0, 10, 10),
        bbox_norm=(0.0, 0.0, 0.1, 0.2),
        crop_bytes=b"crop",
        crop_mime="image/png",
        reading_order_hint=0,
    )
    result = DetectionResult(
        image_width=100,
        image_height=50,
        elements=[element],
        model_info={"model_variant": "nano"},
        errors=[],
    )
    monkeypatch.setattr(
        function_app,
        "detect_cards_from_image_bytes",
        lambda *_, **__: result,
    )

    resp = function_app.analyze_layout(_StubRequest(body=_png_bytes(), params={}))
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert resp.status_code == 200
    assert payload["image_width"] == 100
    assert payload["elements"][0]["label"] == "Text"
    assert payload["elements"][0]["crop"]["data"] == base64.b64encode(b"crop").decode(
        "utf-8"
    )


def test_analyze_layout_sets_207_on_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    result = DetectionResult(
        image_width=0,
        image_height=0,
        elements=[],
        model_info={},
        errors=["failed"],
    )
    monkeypatch.setattr(
        function_app,
        "detect_cards_from_image_bytes",
        lambda *_, **__: result,
    )

    resp = function_app.analyze_layout(_StubRequest(body=_png_bytes(), params={}))
    assert resp.status_code == 207


def test_analyze_layout_invalid_numeric_param_returns_400() -> None:
    resp = function_app.analyze_layout(
        _StubRequest(body=_png_bytes(), params={"conf": "not-a-number"})
    )
    assert resp.status_code == 400


def test_analyze_layout_disallowed_model_returns_400() -> None:
    resp = function_app.analyze_layout(
        _StubRequest(body=_png_bytes(), params={"model_id": "someone/else"})
    )
    assert resp.status_code == 400


def test_analyze_layout_oversized_content_length_returns_413() -> None:
    resp = function_app.analyze_layout(
        _StubRequest(
            body=_png_bytes(),
            headers={"content-length": str(11 * 1024 * 1024)},
        )
    )
    assert resp.status_code == 413


def test_process_image_missing_body_returns_400() -> None:
    resp = function_app.process_image(_StubRequest(body=b""))
    assert resp.status_code == 400


def test_process_image_invalid_output_returns_400() -> None:
    resp = function_app.process_image(
        _StubRequest(body=b"image", params={"output": "bad"})
    )
    assert resp.status_code == 400


def test_process_image_counts_cards_when_output_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        function_app.process_utils, "count_cards_in_image_bytes", lambda _: 3
    )
    resp = function_app.process_image(
        _StubRequest(body=_png_bytes(), params={"output": "none"})
    )
    payload = json.loads(resp.get_body().decode("utf-8"))
    assert payload["card_count"] == 3


def test_process_image_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    cards = [("Card One", b"a"), ("Card Two", b"bbb")]
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda *_, **__: cards,
    )

    resp = function_app.process_image(
        _StubRequest(body=_png_bytes(), params={"output": "return", "format": "json"})
    )
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert payload["card_count"] == 2
    assert payload["cards"][0]["bytes"] == 1


def test_process_image_returns_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    cards = [("Card One", b"aaa"), ("Card Two", b"bbb")]
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda *_, **__: cards,
    )

    resp = function_app.process_image(
        _StubRequest(body=_png_bytes(), params={"output": "return", "format": "zip"})
    )
    with zipfile.ZipFile(io.BytesIO(resp.get_body())) as zf:
        names = sorted(zf.namelist())
    assert names == ["01_Card_One.jpg", "02_Card_Two.jpg"]
    assert resp.headers.get("X-Card-Count") == "2"


def test_process_image_upload_mode_storage_not_configured_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(function_app, "_get_storage_clients", lambda: (None, None))
    resp = function_app.process_image(
        _StubRequest(body=_png_bytes(), params={"output": "upload"})
    )
    assert resp.status_code == 500


def test_process_image_upload_mode_returns_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = [("Card One", b"a")]
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda *_, **__: cards,
    )

    captured: Dict[str, Union[str, int]] = {}

    def _fake_upload(container, source_name, cards, folder=None) -> UploadBatchResult:
        captured["source_name"] = source_name
        captured["folder"] = folder or ""
        captured["count"] = len(cards)
        result = UploadBatchResult(attempted=len(cards))
        result.record_success(f"{folder}/my_photo_1.jpg")
        return result

    monkeypatch.setattr(function_app, "_get_storage_clients", lambda: (None, object()))
    monkeypatch.setattr(function_app, "_upload_processed_cards", _fake_upload)

    req = _StubRequest(
        body=_png_bytes(),
        params={"output": "upload", "name": "my photo.jpg"},
    )
    resp = function_app.process_image(req)
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert payload["card_count"] == 1
    assert payload["uploaded"]["container"] == function_app.PROCESSED_CONTAINER_NAME
    assert payload["uploaded"]["folder"] == function_app._build_processed_card_folder(
        "my photo.jpg"
    )
    assert payload["uploaded"]["failed"] == []
    assert captured["count"] == 1
