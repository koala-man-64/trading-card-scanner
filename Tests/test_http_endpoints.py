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

import function_app
from card_processor.layout_types import LayoutAnalysisResult, LayoutElement


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
        return list(self._blobs)

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
        auth_code=None,
        use_public_urls=True,
    )
    assert url.endswith("/processed/card.jpg")


def test_build_gallery_image_url_proxy_includes_code() -> None:
    container = _StubContainerClient()
    url = function_app._build_gallery_image_url(
        container,
        "processed/card one.jpg",
        category="processed",
        auth_code="abc123",
        use_public_urls=False,
    )
    parsed = urlparse(url)
    assert parsed.path == "/api/gallery/image"
    qs = parse_qs(parsed.query)
    assert qs["name"] == ["processed/card one.jpg"]
    assert qs["category"] == ["processed"]
    assert qs["code"] == ["abc123"]


def test_list_blob_images_builds_payloads() -> None:
    last_modified = datetime(2025, 1, 1, tzinfo=timezone.utc)
    blobs = [
        _StubBlob("processed/a.jpg", size=120, last_modified=last_modified),
        _StubBlob("processed/b.jpg", size=0, last_modified=None),
    ]
    container = _StubContainerClient(blobs=blobs)

    items, latest_modified = function_app._list_blob_images(
        container,
        "processed",
        category="processed",
        auth_code="code",
        use_public_urls=False,
    )

    assert container.last_prefix == "processed/"
    assert items[0]["last_modified"] == function_app._format_rfc3339(last_modified)
    assert items[1]["last_modified"] is None
    assert str(items[0]["url"]).startswith("/api/gallery/image?")
    assert latest_modified == last_modified


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

    items, latest_modified = function_app._list_blob_images(
        container,
        "processed",
        category="processed",
        auth_code=None,
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
    assert payload["next_since"] == function_app._format_rfc3339(last_modified)


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


def test_health_returns_ok() -> None:
    resp = function_app.health(_StubRequest())
    assert resp.status_code == 200
    assert resp.get_body() == b"OK"


def test_analyze_layout_missing_body_returns_400() -> None:
    resp = function_app.analyze_layout(_StubRequest(body=b""))
    assert resp.status_code == 400


def test_analyze_layout_serializes_response(monkeypatch: pytest.MonkeyPatch) -> None:
    element = LayoutElement(
        label="Text",
        confidence=0.9,
        bbox_xyxy=(0, 0, 10, 10),
        bbox_norm=(0.0, 0.0, 0.1, 0.2),
        crop_bytes=b"crop",
        crop_mime="image/png",
        reading_order_hint=0,
    )
    result = LayoutAnalysisResult(
        image_width=100,
        image_height=50,
        elements=[element],
        model_info={"model_variant": "nano"},
        errors=[],
    )
    monkeypatch.setattr(
        function_app,
        "analyze_layout_from_image_bytes",
        lambda *_, **__: result,
    )

    resp = function_app.analyze_layout(_StubRequest(body=b"image", params={}))
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert resp.status_code == 200
    assert payload["image_width"] == 100
    assert payload["elements"][0]["label"] == "Text"
    assert payload["elements"][0]["crop"]["data"] == base64.b64encode(b"crop").decode(
        "utf-8"
    )


def test_analyze_layout_sets_207_on_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    result = LayoutAnalysisResult(
        image_width=0,
        image_height=0,
        elements=[],
        model_info={},
        errors=["failed"],
    )
    monkeypatch.setattr(
        function_app,
        "analyze_layout_from_image_bytes",
        lambda *_, **__: result,
    )

    resp = function_app.analyze_layout(_StubRequest(body=b"image", params={}))
    assert resp.status_code == 207


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
        _StubRequest(body=b"image", params={"output": "none"})
    )
    payload = json.loads(resp.get_body().decode("utf-8"))
    assert payload["card_count"] == 3


def test_process_image_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    cards = [("Card One", b"a"), ("Card Two", b"bbb")]
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda _: cards,
    )

    resp = function_app.process_image(
        _StubRequest(body=b"image", params={"output": "return", "format": "json"})
    )
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert payload["card_count"] == 2
    assert payload["cards"][0]["bytes"] == 1


def test_process_image_returns_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    cards = [("Card One", b"aaa"), ("Card Two", b"bbb")]
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda _: cards,
    )

    resp = function_app.process_image(
        _StubRequest(body=b"image", params={"output": "return", "format": "zip"})
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
        _StubRequest(body=b"image", params={"output": "upload"})
    )
    assert resp.status_code == 500


def test_process_image_upload_mode_returns_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = [("Card One", b"a")]
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda _: cards,
    )

    captured: Dict[str, Union[str, int]] = {}

    def _fake_upload(container, source_name, cards, folder=None) -> None:
        captured["source_name"] = source_name
        captured["folder"] = folder or ""
        captured["count"] = len(cards)

    monkeypatch.setattr(function_app, "_get_storage_clients", lambda: (None, object()))
    monkeypatch.setattr(function_app, "_upload_processed_cards", _fake_upload)

    req = _StubRequest(
        body=b"image",
        params={"output": "upload", "name": "my photo.jpg"},
    )
    resp = function_app.process_image(req)
    payload = json.loads(resp.get_body().decode("utf-8"))

    assert payload["card_count"] == 1
    assert payload["uploaded"]["container"] == function_app.PROCESSED_CONTAINER_NAME
    assert payload["uploaded"]["folder"] == function_app._build_processed_card_folder(
        "my photo.jpg"
    )
    assert captured["count"] == 1
