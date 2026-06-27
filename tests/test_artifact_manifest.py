import zipfile

from scripts.package_release import build_release, inspect_release, iter_package_files


def test_release_package_allowlist_excludes_forbidden_paths(tmp_path) -> None:
    root = tmp_path / "repo"
    (root / "card_processor").mkdir(parents=True)
    (root / "templates").mkdir()
    (root / "tests").mkdir()
    (root / ".github").mkdir()
    (root / "function_app.py").write_text("# app\n", encoding="utf-8")
    (root / "host.json").write_text("{}\n", encoding="utf-8")
    (root / "requirements.txt").write_text("azure-functions==1.21.3\n")
    (root / "local.settings.json").write_text("{}\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (root / "tests" / "test_sample.py").write_text("def test_x(): pass\n")
    (root / ".github" / "workflow.yml").write_text("name: x\n")
    (root / "card_processor" / "__init__.py").write_text("", encoding="utf-8")
    (root / "templates" / "gallery.html").write_text("<html></html>\n")

    package_files = {
        path.relative_to(root).as_posix() for path in iter_package_files(root)
    }
    assert "function_app.py" in package_files
    assert "card_processor/__init__.py" in package_files
    assert "templates/gallery.html" in package_files
    assert ".env" not in package_files
    assert "local.settings.json" not in package_files
    assert "tests/test_sample.py" not in package_files

    artifact = tmp_path / "release.zip"
    build_release(root, artifact)
    assert inspect_release(artifact) == []


def test_release_inspection_rejects_forbidden_paths(tmp_path) -> None:
    artifact = tmp_path / "bad.zip"
    with zipfile.ZipFile(artifact, "w") as zf:
        zf.writestr("function_app.py", "# app")
        zf.writestr("host.json", "{}")
        zf.writestr("requirements.txt", "azure-functions==1.21.3")
        zf.writestr("local.settings.json", "{}")
    errors = inspect_release(artifact)
    assert any("local.settings.json" in error for error in errors)
