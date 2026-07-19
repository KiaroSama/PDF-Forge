# -*- coding: utf-8 -*-
"""C-17: every version surface must agree.

The application reported 2.0.1 while the project metadata still declared 2.0.0.
A single source of truth removes the class of defect rather than resyncing two
numbers that will drift again.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    """The version the project metadata resolves to.

    Read with tomllib so a dynamic declaration is visible as such: a literal
    ``version`` must match, and a ``dynamic`` one must point at the package
    attribute that the rest of these assertions use.
    """
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    if "version" in project:
        return project["version"]
    assert "version" in project.get("dynamic", []), (
        "pyproject declares neither a literal nor a dynamic version"
    )
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "pdf_forge.constants.APP_VERSION", (
        f"the dynamic version must come from the package constant, not {attr!r}"
    )
    return app.APP_VERSION


def _latest_released_changelog_version() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for match in re.finditer(r"^## \[([^\]]+)\]", text, re.M):
        name = match.group(1)
        if name.lower() != "unreleased":
            return name
    raise AssertionError("the changelog has no released version heading")


def test_project_metadata_matches_the_application_version():
    assert _pyproject_version() == app.APP_VERSION


def test_changelog_matches_the_application_version():
    assert _latest_released_changelog_version() == app.APP_VERSION


def test_the_cli_reports_the_same_version():
    result = subprocess.run(
        [sys.executable, "-m", "pdf_forge", "--version"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert app.APP_VERSION in result.stdout, result.stdout
