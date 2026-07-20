# -*- coding: utf-8 -*-
"""F-05: the version chain has to survive an actual build.

test_version_consistency.py proves the DECLARATION agrees with the CLI and the
changelog. None of that inspects a built artifact, so a packaging break that
emits the wrong ``Version:`` into the wheel would pass every existing gate and
still ship the wrong number to anyone who installs it. This module closes that
last link: build a real wheel, open it, read its METADATA.

The build runs against a temporary export of the tracked tree rather than the
checkout itself. Two reasons, both practical:

* ``pip wheel .`` writes ``build/`` and ``pdf_forge.egg-info/`` next to the
  sources. Those are git-ignored, so ``git status`` would stay clean while the
  checkout quietly accumulated artifacts - and cleaning them up afterwards
  means deleting directories from a real working tree, which is a worse risk
  than it is worth for an assertion.
* Building the tracked file list is also what a consumer actually gets. A
  stray untracked directory in the root is not part of the distribution.

That second point is not hypothetical here: setuptools' flat-layout discovery
treats any root directory with a valid identifier name as a package, so a local
``logs/`` directory (git-ignored runtime output) makes an in-place
``pip wheel .`` fail outright with "Multiple top-level packages discovered".
Exporting first keeps this test measuring packaging, not the developer's
leftovers.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from test_version_consistency import (  # noqa: E402
    _latest_released_changelog_version,
)

ROOT = Path(__file__).resolve().parent.parent

# The one concrete anchor. Everything else is read from its source, so this is
# what catches the case the cross-checks cannot: all four surfaces drifting
# together to the same wrong number.
EXPECTED_VERSION = "2.0.1"

# Names that mean "a build happened here". Checked across the checkout so the
# proof is not limited to the root.
ARTIFACT_NAMES = ("build", "dist")
ARTIFACT_SUFFIX = ".egg-info"

# Directories that legitimately contain such names and are not ours to inspect.
IGNORED_TREES = {".git", ".venv", ".tools", "graphify-out", "__pycache__"}


def _export_tracked_tree(destination: Path) -> Path:
    """Copy the tracked working-tree files into ``destination``.

    ``git ls-files`` rather than ``git archive`` on purpose: the former reflects
    the working tree, so an uncommitted version bump is still what gets built.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True, cwd=str(ROOT), timeout=120,
    )
    assert listing.returncode == 0, listing.stderr
    names = [n for n in listing.stdout.decode("utf-8").split("\0") if n]
    assert names, "git reported no tracked files"

    for name in names:
        source = ROOT / name
        if not source.is_file():
            continue  # a deleted-but-still-tracked path; not our concern here
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination


def _git_status() -> list:
    result = subprocess.run(
        ["git", "status", "--short"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line.strip()]


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory) -> dict:
    """Build the wheel once per session; return its METADATA and a before-state.

    Session scope because the build costs seconds, not milliseconds, and every
    assertion below reads the same artifact.
    """
    before = _git_status()
    workspace = tmp_path_factory.mktemp("wheel_build")
    source = _export_tracked_tree(workspace / "src")
    wheelhouse = workspace / "wheelhouse"

    # --no-build-isolation: pyproject declares no [build-system], so pip would
    # otherwise provision a fresh setuptools into an overlay environment, which
    # needs the network. The venv already has setuptools and wheel, and using
    # them keeps this gate runnable offline. --no-deps because the runtime
    # requirements are irrelevant to the metadata under test.
    build = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(source),
         "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheelhouse)],
        capture_output=True, text=True, cwd=str(workspace), timeout=600,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    wheels = sorted(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    with zipfile.ZipFile(wheels[0]) as archive:
        entries = [n for n in archive.namelist()
                   if n.endswith(".dist-info/METADATA")]
        assert len(entries) == 1, f"expected one METADATA, got {entries}"
        metadata = archive.read(entries[0]).decode("utf-8")

    return {"metadata": metadata, "status_before": before}


def _metadata_version(metadata: str) -> str:
    match = re.search(r"^Version:\s*(.+)$", metadata, re.M)
    assert match, "the wheel METADATA has no Version: field"
    return match.group(1).strip()


def _cli_version() -> str:
    """The version the shipped CLI prints, parsed rather than merely contained.

    test_version_consistency checks containment; comparing all four surfaces
    needs the actual value.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pdf_forge", "--version"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    match = re.search(r"^\s*Version:\s*(\S+)", result.stdout, re.M)
    assert match, f"--version printed no Version: line:\n{result.stdout}"
    return match.group(1)


def _stray_build_artifacts() -> list:
    """Every build-artifact directory anywhere in the checkout.

    os.walk with in-place pruning rather than rglob: the ignored trees include
    a bundled LibreOffice runtime, and descending into it costs seconds for
    directories that are excluded anyway.
    """
    paths = []
    for parent, dirnames, _ in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_TREES]
        for name in dirnames:
            if name in ARTIFACT_NAMES or name.endswith(ARTIFACT_SUFFIX):
                paths.append(Path(parent) / name)
    return paths


def test_the_built_wheel_reports_the_expected_version(built_wheel):
    """The declaration, the package, the CLI and the changelog agree - and the
    artifact that is actually installed agrees with all three."""
    wheel_version = _metadata_version(built_wheel["metadata"])
    surfaces = {
        "wheel METADATA": wheel_version,
        "APP_VERSION": app.APP_VERSION,
        "CLI --version": _cli_version(),
        "CHANGELOG": _latest_released_changelog_version(),
    }
    assert len(set(surfaces.values())) == 1, surfaces
    assert wheel_version == EXPECTED_VERSION, surfaces


def test_the_build_leaves_the_checkout_clean(built_wheel):
    """Nothing was written into the working tree.

    Both halves are needed: build/, dist/ and *.egg-info are git-ignored, so
    ``git status`` alone would not notice them, and a filesystem scan alone
    would not notice a build that produced a tracked-looking file.

    The git half is expressed as "no packaging-shaped entry appeared since the
    build started" rather than "the status is empty". An empty-status assertion
    would fail on any work in progress - including a parallel edit landing
    while the build runs - which is churn, not evidence about packaging. The
    footprint of ``pip wheel`` is exactly the names below, and the filesystem
    scan above already covers the git-ignored ones.
    """
    assert _stray_build_artifacts() == []

    appeared = set(_git_status()) - set(built_wheel["status_before"])
    # git status --short is "XY<space>path"; match the path, not the flags.
    leaked = [entry for entry in appeared
              if re.search(r"(^|[/\\])(build|dist)([/\\]|$)"
                           r"|\.egg-info|\.whl$|\.tar\.gz$", entry[3:])]
    assert leaked == [], "the build dirtied the checkout:\n" + "\n".join(leaked)
