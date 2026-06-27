import pytest

from .helpers import get_storage_connection


def test_real_storage_connection_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AZURE_STORAGE_CONNECTION_STRING",
        "RealStorageConnectionStringSentinel",
    )
    monkeypatch.delenv("ALLOW_REAL_AZURE_STORAGE_TESTS", raising=False)

    with pytest.raises(pytest.skip.Exception):
        get_storage_connection(monkeypatch)


def test_real_storage_connection_can_be_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = "RealStorageConnectionStringSentinel"
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", connection)
    monkeypatch.setenv("ALLOW_REAL_AZURE_STORAGE_TESTS", "1")

    assert get_storage_connection(monkeypatch) == connection


def test_local_emulator_connection_does_not_require_real_storage_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = (
        "DefaultEndpointsProtocol=http;"
        "AccountName=devstoreaccount1;"
        "AccountKey=test;"
        "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
    )
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", connection)
    monkeypatch.delenv("ALLOW_REAL_AZURE_STORAGE_TESTS", raising=False)

    assert get_storage_connection(monkeypatch) == connection
