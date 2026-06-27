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
- Assign the Function App managed identity least-privilege Blob Storage roles for app data.
- Keep connection-string mode for local Azurite and emergency rollback only.
- Do not put Function keys in browser URLs; Postman/Azure clients should use authenticated Entra sessions or bearer tokens.

## Cloud Testing (CI/CD)

The project is configured with a GitHub Actions workflow for continuous integration (CI). The CI pipeline is defined in the `.github/workflows/ci.yml` file.

The CI pipeline automatically triggers on pushes and pull requests to `dev` and `main` and performs the following steps:

1.  Checks out the code.
2.  Runs a full-history secret scan.
3.  Sets up Python 3.10.
4.  Installs pinned dependencies and runs `pip check`.
5.  Starts an Azurite service to emulate Azure Storage.
6.  Runs linting, formatting, and type-checking with `ruff` and `mypy`.
7.  Executes the test suite using `pytest`.
8.  Builds and inspects the release artifact to ensure local secrets, tests, samples, Postman files, caches, and virtualenvs are excluded.

Deployment uses the same Python 3.10 gates before uploading a clean `release.zip` artifact to Azure Functions.
