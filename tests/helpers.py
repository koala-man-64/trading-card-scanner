"""Test helpers for resolving Azure Storage connection strings."""

import os
from typing import Optional

import pytest


def get_devstore_connection_string() -> str:
    """Return the local Azurite connection shorthand."""
    return "UseDevelopmentStorage=true"


def normalize_connection_string(connection: str) -> str:
    """Expand shorthand dev storage connection strings for Azurite."""
    if not connection:
        return connection
    if "usedevelopmentstorage=true" in connection.lower():
        return get_devstore_connection_string()
    return connection


def get_storage_connection(monkeypatch: Optional[pytest.MonkeyPatch] = None) -> str:
    """Resolve storage connection string from environment only.

    Real Azure Storage must be explicitly opted in so tests do not mutate a live
    account just because a developer has credentials on their machine.
    """
    env_connection = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not env_connection:
        pytest.skip("AZURE_STORAGE_CONNECTION_STRING not configured in environment")

    normalized = normalize_connection_string(env_connection)
    if (
        "usedevelopmentstorage=true" not in normalized.lower()
        and os.environ.get("ALLOW_REAL_AZURE_STORAGE_TESTS") != "1"
    ):
        pytest.skip("real Azure Storage tests require ALLOW_REAL_AZURE_STORAGE_TESTS=1")
    if monkeypatch:
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", normalized)
    return normalized
