## Testing Summary

Added unit-style tests to cover HTTP handlers and helper logic in `function_app.py`.

### New test file
- `Tests/test_http_endpoints.py`

### Coverage highlights
- Gallery listing (`gallery_images`) and gallery page HTML (`gallery_page`)
- Gallery image proxy (`gallery_image`) including error paths
- Auth level resolution (`_resolve_auth_level`)
- Gallery URL construction (`_build_gallery_image_url`, `_list_blob_images`)
- Health endpoint (`health`)
- Layout analysis handler (`analyze_layout`) including error/207 responses
- Image processing handler (`process_image`) for none/json/zip/upload modes

### Command executed
```
C:\Users\rdpro\Projects\trading-card-scanner\.venv\Scripts\python.exe -m pytest Tests\test_http_endpoints.py -q
```

### Result
- 26 passed, 14 warnings
