"""
Helper script to fetch an image blob from an Azure Storage container.

This script connects to the storage account specified in the environment
variable `AzureWebJobsStorage`, lists blobs in the given container,
downloads the first blob (or a specific blob if a name is supplied), and
saves it to a local file. It's intended for quick sanity checks that
blobs are being created and can be retrieved correctly.

Usage example::

    python test_retrieve.py --container processed --output downloaded.jpg

This will download the first blob from the `processed` container and
write it to `downloaded.jpg`. You can also provide a specific blob
name with the ``--blob`` argument.
"""

import argparse
import os
from pathlib import Path
import json
from azure.storage.blob import BlobServiceClient



def load_local_settings_if_needed() -> None:
    if os.environ.get("AzureWebJobsStorage"):
        return

    settings_path = Path(__file__).with_name("local.settings.json")
    if not settings_path.exists():
        return

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    values = settings.get("Values", {})
    for k, v in values.items():
        if isinstance(v, str) and k not in os.environ:
            os.environ[k] = v

def main() -> None:
    parser = argparse.ArgumentParser(description="Download a blob from Azure Storage")
    parser.add_argument("--container", required=True, help="The container to read from")
    parser.add_argument("--output", required=True, help="Path to save the downloaded blob")
    parser.add_argument("--blob", help="Optional specific blob name to download")
    args = parser.parse_args()

    load_local_settings_if_needed()
    connection = os.environ.get("AzureWebJobsStorage")
    if not connection:
        raise RuntimeError("AzureWebJobsStorage environment variable is not set")
    service_client = BlobServiceClient.from_connection_string(connection)
    container_client = service_client.get_container_client(args.container)
    blob_name = args.blob
    if not blob_name:
        # Grab the first blob in the container
        blobs = list(container_client.list_blobs())
        if not blobs:
            raise RuntimeError(f"No blobs found in container '{args.container}'")
        blob_name = blobs[0].name
    print(f"Downloading blob '{blob_name}' from container '{args.container}'...")
    blob_client = container_client.get_blob_client(blob_name)
    data = blob_client.download_blob().readall()
    output_path = Path(args.output)
    output_path.write_bytes(data)
    print(f"Blob saved to {output_path}")


if __name__ == "__main__":
    main()
