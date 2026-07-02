"""Function binding metadata checks that do not import the ML runtime stack."""

from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FUNCTION_APP = PROJECT_ROOT / "function_app.py"


def _function_app_tree() -> ast.Module:
    return ast.parse(FUNCTION_APP.read_text(encoding="utf-8"))


def test_input_blob_source_uses_event_grid() -> None:
    tree = _function_app_tree()
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "INPUT_BLOB_SOURCE"
            for target in node.targets
        )
    )

    assert ast.unparse(assignment.value) == "func.BlobSource.EVENT_GRID"


def test_process_blob_trigger_uses_input_blob_source() -> None:
    tree = _function_app_tree()
    process_blob = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "process_blob"
    )
    blob_trigger = next(
        decorator
        for decorator in process_blob.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "blob_trigger"
    )
    source_keyword = next(
        keyword for keyword in blob_trigger.keywords if keyword.arg == "source"
    )

    assert ast.unparse(source_keyword.value) == "INPUT_BLOB_SOURCE"
