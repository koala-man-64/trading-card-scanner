# Android App Knowledge Transfer

This document is for an Android app that consumes images and metadata produced by
the trading card scanner Function App.

Use the HTTP API as the Android integration surface. Do not connect the mobile
app directly to Azure Blob Storage unless you are building an internal/admin-only
tool with a separate storage-auth design.

## Capture-Ingest Exception

`trading-card-uploader` is the approved capture-ingest exception to this
HTTP-first guidance. It does not call scanner `/api/process` directly. Its flow
is:

1. Android signs in with MSAL.
2. Android calls the uploader SAS issuer at `POST /api/v1/uploads/sas`.
3. The SAS issuer returns a short-lived, write-only SAS for one blob.
4. Android uploads image bytes directly to private Blob Storage under `raw/`.
5. The scanner blob trigger processes only `raw/` blobs and ignores uploader
   idempotency manifests under `manifests/`.

Scanner deployments that consume uploader blobs should configure the scanner
input binding explicitly:

```text
INPUT_CONTAINER_NAME=card-uploads
INPUT_BLOB_PREFIX=raw
INPUT_STORAGE_CONNECTION_NAME=SCANNER_INPUT_STORAGE
SCANNER_INPUT_STORAGE__blobServiceUri=https://<uploader-storage>.blob.core.windows.net
SCANNER_INPUT_STORAGE__queueServiceUri=https://<uploader-storage>.queue.core.windows.net
```

The scanner output/gallery storage remains configured separately through
`STORAGE_AUTH_MODE`, `STORAGE_ACCOUNT_URL`, `PROCESSED_CONTAINER_NAME`, and
`GALLERY_CONTAINER_NAME`.

## Connection Basics

Base URLs:

- Local Functions host: `http://localhost:7071`
- NPE Function App:
  `https://fa-trading-card-scanner-npe-evcpctgjhhcthjgq.eastus2-01.azurewebsites.net`

All deployed Android traffic should use HTTPS. The intended cloud posture is
Microsoft Entra EasyAuth at the Function App boundary, with the Android app
sending an access token as a bearer token:

```http
Authorization: Bearer <access-token>
```

Do not ship Function keys, storage account keys, connection strings, SAS tokens,
or local settings in the Android app. Function keys are acceptable only for local
developer testing or controlled admin scripts.

For image upload endpoints, send the image bytes as the raw request body:

```http
Content-Type: application/octet-stream
```

Supported image formats are configured by `CARD_SCANNER_ALLOWED_IMAGE_FORMATS`;
the default is `jpeg,png,webp,heic,heif`. Default request and blob size limits
are 10 MB, default max image pixels is 25,000,000, default max crops is 100, and
default max returned ZIP size is 50 MB.

## Recommended Android Flow

1. Call `GET /api/ready` after sign-in or app startup to confirm the service can
   reach storage and load its configured model registry.
2. Poll `GET /api/gallery/images?category=processed` for available processed
   images.
3. Store the returned `next_since` value and send it on later polls as
   `since=<next_since>` to request only newer blobs.
4. Download each image through the returned `url`, normally
   `/api/gallery/image?category=processed&name=<blob-name>`.
5. Cache downloaded image bytes by blob `name` plus the latest `ETag` or
   `Last-Modified` response header.
6. When revalidating a cached image, send `If-None-Match` or
   `If-Modified-Since`; treat HTTP `304` as "keep local cached bytes."

If the Android app needs to submit an image for processing, call
`POST /api/process?output=upload&name=<client-file-name>` and then refresh the
gallery using the `folder` or returned blob names from the upload response.

## Gallery API

### List Images

```http
GET /api/gallery/images?category=processed
GET /api/gallery/images?category=processed&since=2026-06-28T15:30:00.000Z
```

Supported categories are `input`, `processed`, and `segmented`. Categories map
to prefixes inside the configured gallery container. In the default deployment,
`processed` uses the root of the `processed` container; `input` and `segmented`
use configured prefixes.

Example response:

```json
{
  "container": "processed",
  "category": "processed",
  "prefix": "",
  "blobs": [
    {
      "name": "sample_input_1/sample_input_1_1.jpg",
      "size": 184522,
      "last_modified": "2026-06-28T15:31:10.123Z",
      "url": "/api/gallery/image?name=sample_input_1%2Fsample_input_1_1.jpg&category=processed"
    }
  ],
  "refreshed_at": "2026-06-28T15:31:15.456Z",
  "refresh_seconds": 5,
  "next_since": "2026-06-28T15:31:10.123Z"
}
```

Notes for Android:

- Treat `name` as the stable blob identifier.
- Treat `url` as the image download location. It may be a relative Function App
  URL or, if public gallery URLs are enabled, an absolute Blob URL.
- Use `next_since` for incremental polling. If there are no blobs, the service
  still returns a usable timestamp.
- The gallery endpoint does not include Function keys in returned URLs.
- An invalid category returns `400`. Invalid `since` returns the standard JSON
  error shape.

### Download Image

```http
GET /api/gallery/image?category=processed&name=sample_input_1/sample_input_1_1.jpg
```

Successful response:

```http
HTTP/1.1 200 OK
Content-Type: image/jpeg
Cache-Control: public, max-age=60
ETag: "0x8DB..."
Last-Modified: Sun, 28 Jun 2026 15:31:10 GMT

<image bytes>
```

Conditional-cache response:

```http
HTTP/1.1 304 Not Modified
Cache-Control: public, max-age=60
ETag: "0x8DB..."
Last-Modified: Sun, 28 Jun 2026 15:31:10 GMT
```

Validation rules:

- `name` is required.
- `category` defaults to `processed`.
- If the category has a configured prefix, the blob name must start with that
  prefix or the service returns `400`.
- Missing blobs return `404`.

## Processing API

`POST /api/process` accepts raw image bytes in the request body.

### Count Only

```http
POST /api/process
Content-Type: application/octet-stream

<image bytes>
```

Default behavior is `output=none`, which returns only the detected card count:

```json
{
  "card_count": 3
}
```

### Return Metadata

```http
POST /api/process?output=return&format=json
Content-Type: application/octet-stream

<image bytes>
```

Response:

```json
{
  "card_count": 2,
  "cards": [
    {
      "index": 1,
      "name": "card_1",
      "bytes": 184522
    },
    {
      "index": 2,
      "name": "card_2",
      "bytes": 176904
    }
  ]
}
```

This JSON response does not include image bytes. Use ZIP return or upload mode
when the Android app needs actual cropped card images.

### Return ZIP

```http
POST /api/process?output=return&format=zip
Content-Type: application/octet-stream

<image bytes>
```

Successful response:

```http
HTTP/1.1 200 OK
Content-Type: application/zip
Content-Disposition: attachment; filename=processed_cards.zip
X-Card-Count: 2
x-correlation-id: <correlation-id>

<zip bytes>
```

ZIP entries are named with a two-digit index and sanitized card label, for
example `01_card_1.jpg`.

### Upload Processed Crops

```http
POST /api/process?output=upload&name=my%20photo.jpg
Content-Type: application/octet-stream

<image bytes>
```

The service extracts card crops and uploads them to the configured processed
container. The source `name` controls the folder and blob names. Unsafe
characters are replaced with underscores.

Example response:

```json
{
  "card_count": 2,
  "uploaded": {
    "container": "processed",
    "folder": "my_photo",
    "attempted": 2,
    "uploaded_count": 2,
    "failed_count": 0,
    "blobs": [
      "my_photo/my_photo_1.jpg",
      "my_photo/my_photo_2.jpg"
    ],
    "failed": []
  }
}
```

Upload behavior:

- Crops are uploaded with `overwrite=True`; rerunning the same source name can
  replace existing blobs.
- Blob names use `<source-base>_<index>.jpg`.
- Upload status is `200` when all attempted uploads succeed, `207` when some
  uploads fail, and `502` when all attempted uploads fail.
- No detected cards returns `card_count: 0` and an empty upload result with HTTP
  `200`.

## Layout Analysis API

Use this endpoint when the Android app needs detection metadata, bounding boxes,
confidence values, or inline base64 crops instead of only saved gallery images.

```http
POST /api/layout?model_variant=nano&imgsz=1280&conf=0.25&iou=0.5&extract_crops=true&crop_format=png
Content-Type: application/octet-stream

<image bytes>
```

Query parameters:

- `model_variant`: model alias or allowed model ID. Defaults to the configured
  default model. Default aliases are `nano`, `small`, and `medium`.
- `model_id`: explicit model ID; if present, it takes precedence over
  `model_variant`.
- `imgsz`: integer from `128` to `4096`; default `1280`.
- `conf`: confidence threshold from `0.0` to `1.0`; default `0.25`.
- `iou`: intersection-over-union threshold from `0.0` to `1.0`; default `0.5`.
- `extract_crops`: boolean; default `true`.
- `crop_format`: `png`, `jpeg`, or `jpg`; default `png`.

Example response:

```json
{
  "image_width": 1000,
  "image_height": 1400,
  "elements": [
    {
      "index": 1,
      "label": "card",
      "confidence": 0.94,
      "bbox_xyxy": [100, 120, 420, 560],
      "bbox_norm": [0.1, 0.0857, 0.42, 0.4],
      "reading_order_hint": 0,
      "crop": {
        "mime": "image/png",
        "data": "<base64-image-bytes>"
      }
    }
  ],
  "model_info": {
    "model_variant": "nano",
    "model_id": "Matthieu68857/pokemon-cards-detection"
  },
  "errors": []
}
```

If layout analysis completes with non-fatal errors, the endpoint can return HTTP
`207` and include details in `errors`.

## Health And Readiness

Liveness:

```http
GET /api/health
```

Response:

```text
OK
```

Readiness:

```http
GET /api/ready
```

Example response:

```json
{
  "ready": true,
  "components": {
    "settings": {
      "ok": true
    },
    "models": {
      "ok": true,
      "allowed_model_ids": [
        "Matthieu68857/pokemon-cards-detection"
      ],
      "model_aliases": {
        "nano": "Matthieu68857/pokemon-cards-detection",
        "small": "Matthieu68857/pokemon-cards-detection",
        "medium": "Matthieu68857/pokemon-cards-detection"
      }
    },
    "storage": {
      "ok": true,
      "container": "processed"
    }
  }
}
```

`/api/ready` returns `503` when settings, model configuration, or processed
storage access is not ready.

## Error Handling

Most JSON API errors use this shape:

```json
{
  "error": {
    "code": "invalid_image",
    "message": "Invalid image bytes.",
    "correlation_id": "<correlation-id>"
  }
}
```

Expected statuses for Android:

- `200`: success.
- `207`: partial success. Read the response body; do not treat it as a total
  failure.
- `304`: cached gallery image is still valid.
- `400`: bad query parameter, missing body, invalid `since`, invalid output
  mode, invalid numeric parameter, or disallowed model.
- `401` or `403`: authentication or authorization failed at the Function App or
  platform-auth layer.
- `404`: gallery image blob was not found.
- `413`: request body, source blob, image dimensions, or returned ZIP exceeded a
  configured limit.
- `415`: unsupported image format.
- `500`: storage or server configuration problem.
- `502`: all attempted processed-crop uploads failed.

Always log or surface `x-correlation-id` from responses when present. It is the
fastest way to connect Android-side failures to Function App logs.

## Android Implementation Notes

Use any Android HTTP client that supports streaming request/response bodies and
custom headers. With OkHttp, the app should:

- Add the bearer token to every deployed API call.
- Stream uploads from a file or content URI instead of loading large images into
  memory when possible.
- Use `POST /api/process?output=upload&name=<display-name>` when the service
  should persist crops for later gallery consumption.
- Use `POST /api/process?output=return&format=zip` only when the Android app
  needs immediate cropped bytes and can safely unzip the response.
- Use the gallery API for normal browsing and sync.
- Persist a gallery sync cursor per category using `next_since`.
- Persist image cache records keyed by blob `name`, with `ETag`,
  `Last-Modified`, content type, byte size, local file path, and last successful
  fetch time.
- Retry transient `500`/`502` failures with backoff. Do not automatically retry
  `400`, `413`, or `415` without changing the request.

## Output Organization

The scanner has two production input/output paths:

- Blob trigger: an image uploaded to the configured input container and
  `INPUT_BLOB_PREFIX` is processed and crops are written to the configured
  processed container. For `trading-card-uploader`, this prefix is `raw`.
- HTTP upload mode: an image posted to `/api/process?output=upload` is processed
  and crops are written under a folder prefix derived from the supplied `name`.

Default storage-related settings:

- `INPUT_CONTAINER_NAME=input`
- `INPUT_BLOB_PREFIX=raw`
- `INPUT_STORAGE_CONNECTION_NAME=AzureWebJobsStorage`
- `PROCESSED_CONTAINER_NAME=processed`
- `GALLERY_CONTAINER_NAME=processed`
- `GALLERY_INPUT_PREFIX=input`
- `GALLERY_PROCESSED_PREFIX=processed`, normalized to root when it matches the
  gallery container name
- `GALLERY_SEGMENTED_PREFIX=segmented`
- `GALLERY_USE_PUBLIC_URLS=false`

For Android, the important abstraction is the gallery payload, not the storage
account layout. Use `blobs[].name` and `blobs[].url` from
`/api/gallery/images`; do not reconstruct Blob URLs in the app.

## Source Evidence

This document is based on these repo sources:

- `function_app.py`: Function routes, gallery listing/image proxy, process
  modes, layout response serialization, upload naming, caching headers, and
  readiness behavior.
- `card_processor/request_validation.py`: request limits, query parameter
  parsing, `since` parsing, and validation error codes.
- `card_processor/settings.py`: default runtime limits, allowed image formats,
  and default model aliases.
- `card_processor/upload_results.py`: upload result payload and status-code
  rules.
- `tests/test_http_endpoints.py`: pinned HTTP response shapes and error cases.
- `tests/test_upload_processed_cards.py`: upload naming, overwrite behavior, and
  storage roundtrip expectations.
- `postman/trading-card-scanner.postman_collection.json`: endpoint examples.
- `postman/environments/trading-card-scanner.local.postman_environment.json` and
  `postman/environments/trading-card-scanner.azure.postman_environment.json`:
  local and NPE base URLs.
