# Testing

This document outlines the procedures for testing the application locally and in the cloud.

## Local Testing

### Prerequisites

1.  **Python:** Ensure you have Python 3.10 installed (see `.python-version`).
2.  **Dependencies:** Install the required dependencies using pip:

    ```bash
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    python -m pip check
    ```

3.  **Local settings:** Copy `local.settings.json.example` to
    `local.settings.json` for local-only development. Do not commit
    `local.settings.json` or `.env`.

### Running Tests

To run the test suite, execute the following command from the root of the project:

```bash
python -m pytest
```

You can also run tests with the `-q` flag for a more concise output:

```bash
python -m pytest -q
```

Run the static checks used by CI:

```bash
ruff check .
ruff format . --check
mypy . --ignore-missing-imports
python scripts/package_release.py build release.zip
python scripts/package_release.py check release.zip
```

### Integration Tests

The integration tests require a running instance of Azurite, an Azure Storage emulator.

1.  **Start Azurite:** You can run Azurite using Docker:

    ```bash
    docker run -d -p 10000:10000 -p 10001:10001 -p 10002:10002 mcr.microsoft.com/azure-storage/azurite
    ```

2.  **Set Environment Variable:** Set the `AZURE_STORAGE_CONNECTION_STRING` environment variable to the default Azurite connection string:

    **PowerShell (Windows):**
    ```powershell
    $env:AZURE_STORAGE_CONNECTION_STRING="UseDevelopmentStorage=true"
    ```

    **Bash (Linux/macOS):**
    ```bash
    export AZURE_STORAGE_CONNECTION_STRING="UseDevelopmentStorage=true"
    ```

3.  **Run Integration Tests:** You can run only the integration tests using the `integration` marker. These tests use Azurite by default. Real Azure Storage requires setting `ALLOW_REAL_AZURE_STORAGE_TESTS=1` explicitly.

    ```bash
    python -m pytest -m integration
    ```

## Runtime Readiness

`GET /api/health` is a lightweight liveness check.

`GET /api/ready` validates runtime settings, allowed model configuration, and processed-storage reachability. In Azure, protect `/api/ready` with platform auth and use it for deployment smoke checks.

## Azure Authentication And Storage

The intended Azure posture is Microsoft Entra EasyAuth at the Function App boundary plus managed identity for app-data Blob Storage.

- Set `STORAGE_AUTH_MODE=managed_identity`.
- Set `STORAGE_ACCOUNT_URL=https://<account>.blob.core.windows.net`.
- For capture-ingest uploads from `trading-card-uploader`, set
  `INPUT_CONTAINER_NAME=card-uploads`, `INPUT_BLOB_PREFIX=raw`, and
  `INPUT_STORAGE_CONNECTION_NAME=<connection-prefix>` so the blob trigger watches
  uploaded images and not uploader idempotency manifests.
- For the input trigger connection, configure
  `<connection-prefix>__blobServiceUri` and
  `<connection-prefix>__queueServiceUri` for the uploader storage account.
- Assign the Function App managed identity least-privilege Blob Storage roles for app data.
- Keep connection-string mode for local Azurite and emergency rollback only.
- Do not put Function keys in browser URLs; Postman/Azure clients should use authenticated Entra sessions or bearer tokens.

## Cloud Testing (CI/CD)

Azure DevOps is the CI/CD source of truth. Pipeline definitions live in
`azure-pipelines/`:

- `quality.yml` validates pull requests and `main`.
- `security.yml` runs secret and dependency scans on pull requests, `main`, and
  a weekly schedule.
- `release.yml` packages a clean `release.zip` and release manifest after main
  quality validation.
- `deploy-npe.yml` deploys the release artifact to `fa-trading-card-scanner-npe`
  and publishes smoke-test evidence.

The quality pipeline performs these steps:

1.  Checks out the code.
2.  Sets up Python 3.10.
3.  Installs pinned dependencies and runs `pip check`.
4.  Starts Azurite with a randomized account key.
5.  Runs linting, formatting, and type-checking with `ruff` and `mypy`.
6.  Executes the test suite using `pytest`.
7.  Builds and inspects the release artifact to ensure local secrets, tests,
    samples, Postman files, caches, and virtualenvs are excluded.

Deployment uses the same Python 3.10 runtime standard before uploading the
clean `release.zip` artifact to Azure Functions.

GitHub Actions workflow definitions have been removed after Azure Repos pull
request validation, release, deploy, and keyed `/api/health` plus `/api/ready`
smoke checks proved parity. See `docs/azure-devops-migration.md` for the
cutover record.
