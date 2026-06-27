import os
from pathlib import Path


def test_canonical_lowercase_sample_fixture_path_exists() -> None:
    root = Path(__file__).resolve().parent
    assert (root / "samples" / "input").is_dir()
    assert (root / "samples" / "input" / "sample_input_1.jpg").is_file()
    if os.name != "nt":
        assert not (root.parent / "Tests").exists()
