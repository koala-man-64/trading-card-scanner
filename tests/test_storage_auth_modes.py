import function_app


class _FakeCredential:
    pass


class _FakeBlobServiceClient:
    created_with = None

    def __init__(self, *, account_url, credential):
        self.account_url = account_url
        self.credential = credential
        _FakeBlobServiceClient.created_with = (account_url, credential)

    @classmethod
    def from_connection_string(cls, connection):
        client = cls(account_url="connection", credential=connection)
        cls.created_with = ("connection", connection)
        return client


def test_managed_identity_requires_account_url(monkeypatch) -> None:
    monkeypatch.setattr(function_app, "STORAGE_AUTH_MODE", "managed_identity")
    monkeypatch.setattr(function_app, "STORAGE_ACCOUNT_URL", "")

    assert function_app._get_storage_service_client() is None


def test_managed_identity_uses_default_azure_credential(monkeypatch) -> None:
    monkeypatch.setattr(function_app, "STORAGE_AUTH_MODE", "managed_identity")
    monkeypatch.setattr(
        function_app,
        "STORAGE_ACCOUNT_URL",
        "https://acct.blob.core.windows.net",
    )
    monkeypatch.setattr(function_app, "DefaultAzureCredential", _FakeCredential)
    monkeypatch.setattr(function_app, "BlobServiceClient", _FakeBlobServiceClient)

    client = function_app._get_storage_service_client()

    assert isinstance(client, _FakeBlobServiceClient)
    account_url, credential = _FakeBlobServiceClient.created_with
    assert account_url == "https://acct.blob.core.windows.net"
    assert isinstance(credential, _FakeCredential)
