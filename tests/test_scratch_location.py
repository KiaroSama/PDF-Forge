# -*- coding: utf-8 -*-
"""The app's scratch directories must be redirectable, and all of them routed.

A LibreOffice conversion profile is a large tree that a killed run leaves
behind. In the user's %TEMP% those orphans are anonymous - nothing ties them to
the project that made them - so the suite points them at a project-local root
and the residue sweep can attribute them.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf_forge.safeio import scratch_dir  # noqa: E402

PACKAGE = Path(__file__).resolve().parent.parent / "pdf_forge"

#: Scratch calls that deliberately do NOT use scratch_dir(), each with the
#: reason it is placed where it is. A new unrouted call fails the test below.
DELIBERATE = {
    # Same-volume staging: the runtime is extracted beside where it will live.
    "pdfforge_lostage_": "staged inside runtime_root() for an atomic move",
    # Same-volume staging: promotion to the final name must not cross volumes.
    ".pdfforge_convert_": "staged in out.parent so promotion stays atomic",
    # Security boundary: an env-redirectable plaintext location could be aimed
    # at an indexed or synced folder (SEC-01).
    "pdfforge_decrypt_": "decrypted plaintext stays on the system temp",
}


def test_scratch_dir_defaults_to_the_system_temp(monkeypatch):
    """A real user gets the OS temp: scratch is throwaway and the OS cleans it."""
    monkeypatch.delenv("PDF_FORGE_TEMP_DIR", raising=False)
    assert scratch_dir() == Path(tempfile.gettempdir())


def test_scratch_dir_honours_the_override(monkeypatch, tmp_path):
    target = tmp_path / "scratch_root"
    monkeypatch.setenv("PDF_FORGE_TEMP_DIR", str(target))
    assert scratch_dir() == target
    assert target.is_dir(), "the override root must be created, not merely returned"


def test_an_unusable_override_falls_back_instead_of_failing(monkeypatch, tmp_path):
    """Scratch placement is hygiene; it must never abort the user's conversion."""
    blocker = tmp_path / "a_file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("PDF_FORGE_TEMP_DIR", str(blocker / "under_a_file"))
    assert scratch_dir() == Path(tempfile.gettempdir())


def test_the_suite_redirects_scratch_into_the_project():
    """conftest must be pointing the app at the project, not at %TEMP%."""
    configured = os.environ.get("PDF_FORGE_TEMP_DIR")
    assert configured, "conftest did not set PDF_FORGE_TEMP_DIR"
    root = PACKAGE.parent.resolve()
    assert Path(configured).resolve().is_relative_to(root), (
        f"test scratch {configured} is outside the project root {root}")


def test_every_scratch_call_is_routed_or_listed_as_deliberate():
    """A new unrouted mkdtemp is how %TEMP% silently starts collecting profiles.

    Structural, because the leak is invisible at runtime until someone goes
    looking in their temp folder months later.
    """
    unrouted = []
    for source_file in sorted(PACKAGE.glob("*.py")):
        text = source_file.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", "")
            if name not in ("mkdtemp", "TemporaryDirectory"):
                continue
            call = ast.get_source_segment(text, node) or ""
            prefix = re.search(r'prefix\s*=\s*"([^"]+)"', call)
            if prefix and prefix.group(1) in DELIBERATE:
                continue
            if "dir=" not in call:
                unrouted.append(
                    f"{source_file.name}:{node.lineno} prefix="
                    f"{prefix.group(1) if prefix else '?'}")

    assert not unrouted, (
        "these scratch directories are not routed through scratch_dir() and are "
        "not listed in DELIBERATE with a reason:\n  " + "\n  ".join(unrouted))
