import logging
from pathlib import Path

import pytest

import function_app


SAMPLES = Path(__file__).parent / "Samples"


def _read_sample(name: str) -> bytes:
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"Sample file missing: {path}")
    return path.read_bytes()


class _StubContainer:
    def __init__(self) -> None:
        self.uploads = []

    def upload_blob(self, name, data, overwrite):
        self.uploads.append((name, data, overwrite))


def test_save_processed_cards_to_folder_writes_files(tmp_path: Path) -> None:
    cards = [
        ("Charizard V", _read_sample("sample output 1.jpg")),
        ("Unknown Hero", _read_sample("sample output 2.jpg")),
        ("unknown", _read_sample("sample output 3.jpg")),
    ]
    source_path = str(SAMPLES / "sample input 1.jpg")

    function_app._save_processed_cards_to_folder(tmp_path, source_path, cards)

    assert (tmp_path / "sample input 1_1.jpg").read_bytes() == cards[0][1]
    assert (tmp_path / "sample input 1_2.jpg").read_bytes() == cards[1][1]
    assert (tmp_path / "sample input 1_3.jpg").read_bytes() == cards[2][1]


def test_save_processed_cards_to_folder_logs_and_continues_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original_write_bytes = Path.write_bytes

    def _write_bytes_with_failure(path: Path, data: bytes) -> int:
        if path.name.endswith("_1.jpg"):
            raise OSError("disk full")
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", _write_bytes_with_failure)

    first_bytes = _read_sample("sample output 1.jpg")
    second_bytes = _read_sample("sample output 2.jpg")
    cards = [("Card One", first_bytes), ("Card Two", second_bytes)]
    source_path = str(SAMPLES / "sample input 2.jpg")
    with caplog.at_level(logging.ERROR):
        function_app._save_processed_cards_to_folder(tmp_path, source_path, cards)

    assert "Failed to save processed card Card One" in caplog.text
    assert not (tmp_path / "sample input 2_1.jpg").exists()
    assert (tmp_path / "sample input 2_2.jpg").read_bytes() == second_bytes


def test_process_blob_bytes_uploads_processed_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _StubContainer()
    sample_bytes = _read_sample("sample output 1.jpg")
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda _: [("Cloud Card", sample_bytes)],
    )
    source_path = str(SAMPLES / "sample input 1.jpg")

    function_app._process_blob_bytes(source_path, b"blob-bytes", container)

    assert container.uploads == [("sample input 1_1.jpg", sample_bytes, True)]


def test_process_blob_bytes_skips_upload_when_no_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _StubContainer()
    monkeypatch.setattr(
        function_app.process_utils,
        "extract_card_crops_from_image_bytes",
        lambda _: [],
    )
    source_path = str(SAMPLES / "sample input 1.jpg")

    function_app._process_blob_bytes(source_path, b"blob-bytes", container)

    assert container.uploads == []
