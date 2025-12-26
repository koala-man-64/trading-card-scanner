"""Test helpers for resolving Azure Storage connection strings and settings."""

import json
import os
from pathlib import Path
from typing import Optional

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOCAL_SETTINGS = ROOT / "local.settings.json"


def get_devstore_connection_string() -> str:
    """Return the dev store connection string from env or skip."""
    connection = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not connection or "devstoreaccount1" not in connection:
        pytest.skip("AZURE_STORAGE_CONNECTION_STRING for dev store not configured.")
    assert connection is not None
    return connection


def load_settings() -> dict:
    """Load values from local.settings.json."""
    if not LOCAL_SETTINGS.exists():
        return {}
    try:
        data = json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data.get("Values", {})


def normalize_connection_string(connection: str) -> str:
    """Expand shorthand dev storage connection strings for Azurite."""
    if not connection:
        return connection
    if "usedevelopmentstorage=true" in connection.lower():
        return get_devstore_connection_string()
    return connection


def get_storage_connection(monkeypatch: Optional[pytest.MonkeyPatch] = None) -> str:
    """Resolve storage connection string from env first, then local.settings.json."""
    env_connection = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if env_connection:
        connection = normalize_connection_string(env_connection)
        if monkeypatch:
            monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", connection)
        return connection

    values = load_settings()
    connection = values.get("AZURE_STORAGE_CONNECTION_STRING") or ""
    if not connection:
        pytest.skip(
            "AZURE_STORAGE_CONNECTION_STRING not configured in env or local.settings.json"
        )

    normalized = normalize_connection_string(connection)
    if monkeypatch:
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", normalized)
    return normalized
