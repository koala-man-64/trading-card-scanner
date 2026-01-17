# Testing

This document outlines the procedures for testing the application locally and in the cloud.

## Local Testing

### Prerequisites

1.  **Python:** Ensure you have Python 3.10 installed (see `.python-version`).
2.  **Dependencies:** Install the required dependencies using pip:

    ```bash
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    ```

### Running Tests

To run the test suite, execute the following command from the root of the project:

```bash
python -m pytest
```

You can also run tests with the `-q` flag for a more concise output:

```bash
python -m pytest -q
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
    $env:AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
    ```

    **Bash (Linux/macOS):**
    ```bash
    export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
    ```

3.  **Run Integration Tests:** You can run only the integration tests using the `integration` marker:

    ```bash
    python -m pytest -m integration
    ```

## Cloud Testing (CI/CD)

The project is configured with a GitHub Actions workflow for continuous integration (CI). The CI pipeline is defined in the `.github/workflows/ci.yml` file.

The CI pipeline automatically triggers on every push to any branch and performs the following steps:

1.  Checks out the code.
2.  Sets up Python 3.10.
3.  Starts an Azurite service to emulate Azure Storage.
4.  Installs all dependencies.
5.  Runs linting, formatting, and type-checking with `ruff` and `mypy`.
6.  Executes the test suite using `pytest`.
