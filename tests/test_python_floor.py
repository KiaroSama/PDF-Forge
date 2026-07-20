# -*- coding: utf-8 -*-
"""The declared Python floor must actually hold.

`requires-python = ">=3.10"` and the CI matrix start at 3.10, but development
happens on a newer interpreter, so a module that only became stdlib later
imports cleanly here and fails on the oldest matrix job. That is exactly how
`tomllib` (3.11+) reached CI: every local check was green and both 3.10 jobs
failed on import.

This scans the source with the AST instead of importing anything, so it runs on
any interpreter and catches the mistake before it costs a CI round-trip.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Standard-library modules that do not exist on the declared floor. Keyed by the
# version that introduced them, so raising the floor is a one-line edit.
_STDLIB_ADDED_AFTER_3_10 = {
    (3, 11): {"tomllib", "wsgiref.types"},
    (3, 12): {"typing_extensions_placeholder"},  # none in 3.12; kept for shape
    (3, 13): {"dbm.sqlite3"},
}

# An import is allowed only when the code structurally guards it - inside a
# try/except that handles ImportError. A per-filename allow-list was tried
# first and was worse than useless: it let a bare top-level `import tomllib`
# back into the very file it exempted, so the guard passed while the defect was
# present. Structure has to be earned, not declared.


def _declared_floor() -> tuple:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*">=\s*(\d+)\.(\d+)"', text)
    assert match, "pyproject.toml does not declare requires-python"
    return int(match.group(1)), int(match.group(2))


def _too_new(floor: tuple) -> set:
    names = set()
    for version, modules in _STDLIB_ADDED_AFTER_3_10.items():
        if version > floor:
            names |= modules
    return names


def _python_files():
    for folder in ("pdf_forge", "tests", "scripts"):
        base = ROOT / folder
        if base.exists():
            yield from sorted(base.rglob("*.py"))


def _guarded_import_nodes(tree: ast.AST) -> set:
    """Import statements that sit inside a try/except handling ImportError.

    Those are the ones with a real fallback, so they are safe on an interpreter
    that lacks the module. Anything else is a hard dependency on the version
    that introduced it.
    """
    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_import_error = any(
            handler.type is not None and (
                (isinstance(handler.type, ast.Name)
                 and handler.type.id in ("ImportError", "ModuleNotFoundError"))
                or (isinstance(handler.type, ast.Tuple) and any(
                    isinstance(e, ast.Name)
                    and e.id in ("ImportError", "ModuleNotFoundError")
                    for e in handler.type.elts))
            )
            for handler in node.handlers
        )
        if not catches_import_error:
            continue
        for statement in node.body:
            for inner in ast.walk(statement):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(inner))
    return guarded


def _imported_names(tree: ast.AST):
    """Every unguarded module name imported in the file, with its line."""
    guarded = _guarded_import_nodes(tree)
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module, node.lineno


def test_the_declared_floor_matches_the_ci_matrix():
    floor = _declared_floor()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    versions = set(re.findall(r"'(3\.\d+)'", workflow))
    matrix = {tuple(int(p) for p in v.split(".")) for v in versions}
    oldest = min(matrix)
    assert oldest == floor, (
        f"pyproject declares >={floor[0]}.{floor[1]} but the CI matrix starts at "
        f"{oldest[0]}.{oldest[1]}; one of them is wrong"
    )


def test_no_module_newer_than_the_floor_is_imported_unguarded():
    floor = _declared_floor()
    forbidden = _too_new(floor)
    assert forbidden, "the floor is newer than every entry in the table"

    offenders = []
    for path in _python_files():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, line in _imported_names(tree):
            root = name.split(".")[0]
            if name in forbidden or root in forbidden:
                offenders.append(f"{relative}:{line} imports {name}")
    assert not offenders, (
        f"these imports do not exist on Python {floor[0]}.{floor[1]}, the "
        "declared floor and the oldest CI job:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="already running on the floor or older")
def test_the_guarded_import_really_is_guarded():
    """A name on the allow-list must have a working fallback, not a bare import."""
    import builtins

    sys.path.insert(0, str(ROOT / "tests"))
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name in ("tomllib", "tomli"):
            raise ImportError(f"simulated: {name} unavailable")
        return real_import(name, *args, **kwargs)

    import test_version_consistency as module

    builtins.__import__ = blocked
    try:
        assert module._pyproject_version() == module.app.APP_VERSION, (
            "the fallback path does not produce the same answer"
        )
    finally:
        builtins.__import__ = real_import
