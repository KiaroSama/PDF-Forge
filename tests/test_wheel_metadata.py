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

import importlib.util
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

    # Reproduce the runtime state, not just the tracked one. The app writes a
    # git-ignored logs/ directory when it runs, and setuptools' flat-layout
    # discovery counts any root directory with an identifier-legal name as a
    # package - so logs/ alone was enough to fail the build with "Multiple
    # top-level packages discovered". An export of tracked files never contains
    # it, which is exactly why a wheel test built from one cannot notice the
    # declaration being removed again. Creating it here is what makes that
    # regression reachable.
    logs = destination / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "pdf_forge_2026-07-20_00-00-00_UTC.log").write_text(
        "[2026-07-20 00:00:00 UTC] [INFO] [TEST] runtime log placeholder\n",
        encoding="utf-8",
    )
    return destination


def _git_status() -> list:
    result = subprocess.run(
        ["git", "status", "--short"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line.strip()]


def _attempt_build(source: Path, wheelhouse: Path, cwd: Path) -> tuple:
    """Build a wheel from ``source``. Returns (succeeded, combined output).

    Returns rather than asserts, because one caller needs a build to succeed
    and another needs to inspect why one failed.
    """
    base = [sys.executable, "-m", "pip", "wheel", str(source),
            "--no-deps", "--wheel-dir", str(wheelhouse)]
    attempts = []
    if importlib.util.find_spec("setuptools") is not None:
        attempts.append(("no build isolation", base + ["--no-build-isolation"]))
    attempts.append(("isolated build", base))

    transcript = []
    for label, command in attempts:
        build = subprocess.run(command, capture_output=True, text=True,
                               cwd=str(cwd), timeout=600)
        transcript.append(f"--- {label} ---\n{build.stdout}{build.stderr}")
        if build.returncode == 0:
            return True, "\n".join(transcript)
    return False, "\n".join(transcript)


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

    # pyproject declares no [build-system], so pip falls back to setuptools and
    # can get it two ways. Neither works everywhere, so both are tried:
    #
    #   --no-build-isolation  builds with the setuptools already here, so it
    #                         works offline. Not always possible: Python 3.12
    #                         dropped setuptools from the bundled environment,
    #                         and a runner can have setuptools too old to build
    #                         a wheel by itself ("invalid command 'bdist_wheel'").
    #                         Both failures showed up on CI.
    #   isolated (default)    pip provisions its own backend - works on any
    #                         interpreter, but needs the network.
    #
    # Tried in that order rather than predicted, because "can setuptools build a
    # wheel here" is not answerable by an import check: the first CI attempt
    # guessed from `find_spec` and was wrong on Windows. A failed first attempt
    # costs seconds; guessing wrong costs a red build.
    #
    # --no-deps throughout: runtime requirements are irrelevant to metadata.
    ok, output = _attempt_build(source, wheelhouse, workspace)
    if not ok:
        pytest.fail("no wheel build strategy worked:\n" + output)

    wheels = sorted(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        entries = [n for n in names if n.endswith(".dist-info/METADATA")]
        assert len(entries) == 1, f"expected one METADATA, got {entries}"
        metadata = archive.read(entries[0]).decode("utf-8")
        top_level = sorted({n.split("/")[0] for n in names})

    return {"metadata": metadata, "status_before": before,
            "top_level": top_level}


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


def test_the_wheel_ships_the_package_and_nothing_else(built_wheel):
    """Only pdf_forge and its dist-info may be distributed.

    setuptools' flat-layout discovery counts every root directory with an
    identifier-legal name as a package, so the runtime `logs/` directory this
    app writes was enough to break the build with "Multiple top-level packages
    discovered". pyproject now declares the package explicitly instead.

    This guards the outcome rather than the declaration, because the fixture
    builds from an export of *tracked* files - where logs/ never appears - so
    only inspecting what the wheel actually contains can catch a stray
    top-level directory being packaged.
    """
    unexpected = [name for name in built_wheel["top_level"]
                  if name != "pdf_forge" and not name.endswith(".dist-info")]
    assert not unexpected, (
        f"the wheel ships more than the package: {unexpected} "
        f"(all top-level entries: {built_wheel['top_level']})"
    )


def test_removing_the_package_declaration_brings_the_failure_back(tmp_path):
    """The guard above only means something if the defect is reachable.

    The other tests build from a source tree that now contains a runtime-like
    logs/, so they would go red if the declaration disappeared. This proves
    that directly, in the one direction assertions on a successful build cannot
    reach: strip `packages = [...]` from pyproject, keep everything else, and
    the build must fail the way it originally did.

    Without this, a green suite would only show that *the current* pyproject
    packages nothing extra - not that the guard notices the declaration being
    removed, which is the regression being guarded against.
    """
    source = _export_tracked_tree(tmp_path / "src")
    pyproject = source / "pyproject.toml"
    stripped = re.sub(r'^packages = \[.*?\]\s*$', "",
                      pyproject.read_text(encoding="utf-8"), flags=re.M)
    assert stripped != pyproject.read_text(encoding="utf-8"), (
        "the packages declaration was not found in pyproject.toml; this test "
        "no longer removes what it claims to remove"
    )
    pyproject.write_text(stripped, encoding="utf-8")

    ok, output = _attempt_build(source, tmp_path / "wheelhouse", tmp_path)

    assert not ok, (
        "the build succeeded without an explicit package declaration, so "
        "auto-discovery no longer trips over logs/ and this guard is inert"
    )
    assert "Multiple top-level packages discovered" in output, (
        "the build failed for some other reason than flat-layout discovery:\n"
        + output[-2000:]
    )
    assert "logs" in output, (
        "flat-layout discovery did not name logs/, so the exported tree is not "
        "reproducing the runtime state this guard depends on:\n" + output[-2000:]
    )


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
