"""Install, verify, trim and remove the project-local LibreOffice runtime.

Provisioning downloads the pinned official package, checks its SHA-256, unpacks
it with an official administrative extraction into a staging directory, removes
the components a headless PDF converter never uses, and only then promotes the
result atomically. Nothing is installed system-wide and no GUI is ever shown.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .constants import *  # noqa: F401,F403
from . import office_discovery as _discovery
from .office_discovery import (
    EXTRACT_TIMEOUT, NO_PROGRESS_TIMEOUT, OfficeRuntimeError, RuntimeCandidate,
)
from .office_server import _terminate


# Resolved through the module object rather than bound at import time, so a test
# that patches ``office_discovery.<name>`` also affects the calls made here.
def runtime_root():
    return _discovery.runtime_root()


def libreoffice_dir():
    return _discovery.libreoffice_dir()


def load_runtime_meta():
    return _discovery.load_runtime_meta()


def probe_soffice_version(soffice, timeout: int = 60):
    return _discovery.probe_soffice_version(soffice, timeout)


def _search_soffice_under(base):
    return _discovery._search_soffice_under(base)


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #

def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def clean_runtime() -> bool:
    """Remove ONLY the project-local LibreOffice runtime. Returns True if removed.

    Never touches a system LibreOffice or the ``PDF_FORGE_SOFFICE`` target.
    """
    target = libreoffice_dir()
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
        logger.info("Removed project-local LibreOffice runtime: %s", target)
        return not target.exists()
    return False


# Components an installation used *only* for headless document -> PDF
# conversion never touches. Every entry was verified empirically: it was moved
# aside, Word/Excel/PowerPoint/CSV sources were converted through both the
# soffice CLI and the production unoserver path, and the extracted text of
# every page was compared with a full install. All produced byte-identical
# text, so removing them cannot change a converted document.
#
# Deliberately NOT trimmed, because removing them *did* break conversion:
#   share/registry, share/config - core configuration the engine reads
#   Fonts                        - glyph coverage of the rendered output
#   program/python-core-*        - the interpreter the UNO bridge runs on
_TRIMMABLE = (
    "share/extensions",   # spelling dictionaries for ~30 languages (~470 MB)
    "program/resource",   # interface translations for ~120 languages (~270 MB)
    "help",               # bundled help pages
    "share/gallery",      # clipart
    "share/template",     # document templates
    "share/wizards",      # document wizards
    "share/basic",        # Basic IDE macros (macros are disabled anyway)
    "share/xpdfimport",   # PDF *import* filter; this tool only writes PDFs
    "program/classes",    # Java integration
    "program/shlxthdl",   # Explorer shell/thumbnail handler (GUI only)
    "readmes",
)


def _directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _trim_runtime(base: Path) -> int:
    """Delete components a headless PDF converter never uses. Returns bytes freed.

    This is what makes the provisioned runtime roughly half the size of a normal
    LibreOffice install (measured: 1.6 GB -> ~740 MB) without changing a single
    converted page. Failing to remove one component is not fatal - a larger
    runtime still works correctly.
    """
    freed = 0
    for relative in _TRIMMABLE:
        target = base / relative
        if not target.exists():
            continue
        size = _directory_size(target)
        try:
            shutil.rmtree(target)
        except OSError as exc:
            logger.warning("Could not trim '%s' from the runtime: %s", relative, exc)
            continue
        freed += size
        logger.info("Trimmed '%s' from the runtime (%d bytes).", relative, size)
    return freed


def _version_compatible(reported: str, expected: str) -> bool:
    """True when ``reported`` is the pinned version or a build refinement of it.

    Numeric components are compared, and the pinned components must be an exact
    prefix of the reported ones: pin ``25.8.7`` accepts ``25.8.7`` and the
    4-component binary build ``25.8.7.3``, but rejects ``25.8.8``, ``25.9.x``,
    ``25.2.x`` and ``26.x``. The old ``startswith(expected.split('.')[0])``
    compared only the major number, so any same-major drift passed.
    """
    def parts(v: str):
        out = []
        for tok in str(v).split("."):
            if not tok.strip().isdigit():
                break
            out.append(int(tok))
        return out

    exp = parts(expected)
    return bool(exp) and parts(reported)[:len(exp)] == exp


def verify_runtime_directory(base: Path, expect_version: str = None) -> "RuntimeCandidate":
    """Validate a runtime directory as a complete, self-consistent tuple.

    Checks the executable, the bundled Python/UNO interpreter, and the version
    the binary itself reports. A directory that merely contains a ``soffice``
    file is NOT accepted as installed (PF-015), and the provisioning marker is
    only a hint - the binary decides.
    """
    base = Path(base)
    if not base.exists():
        return RuntimeCandidate(source="runtime-dir", reason="directory does not exist")
    soffice = _search_soffice_under(base)
    if soffice is None:
        return RuntimeCandidate(source="runtime-dir", reason="soffice executable missing")
    program = soffice.parent
    python = None
    for name in ("python.exe", "python3", "python"):
        if (program / name).exists():
            python = program / name
            break
    if python is None:
        return RuntimeCandidate(
            source="runtime-dir", soffice=soffice,
            reason="LibreOffice's bundled Python is missing",
        )
    version = probe_soffice_version(soffice)
    if version is None:
        return RuntimeCandidate(
            source="runtime-dir", soffice=soffice, python=python,
            reason="the binary did not report a version",
        )
    if expect_version and not _version_compatible(version, expect_version):
        return RuntimeCandidate(
            source="runtime-dir", soffice=soffice, python=python, version=version,
            reason=f"version mismatch: expected {expect_version}, found {version}",
        )
    return RuntimeCandidate(source="runtime-dir", soffice=soffice, python=python,
                            version=version, complete=True)


def provision_runtime(
    progress=None, force: bool = False, download=None, verify_checksum: bool = True,
) -> dict:
    """Provision the project-local LibreOffice runtime (idempotent).

    Downloads the pinned official LibreOffice distribution, verifies its
    checksum against the tracked metadata, and performs an official
    administrative extraction (``msiexec /a`` on Windows) into
    ``.tools/libreoffice/`` - no system install, PATH, registry, shortcut, or
    service change. Re-validates and repairs an incomplete runtime; does nothing
    when a verified runtime already exists (unless ``force``).

    ``download`` is an injectable ``(url, dest) -> None`` for testing. Returns a
    status dict. Raises :class:`OfficeRuntimeError` when provisioning cannot be
    done safely on this platform (the caller then reports the exact limitation).
    """
    meta = load_runtime_meta()
    target = libreoffice_dir()

    if not force:
        # Pass the pin: a complete-but-wrong-version runtime (e.g. left over from
        # an earlier pin) must be rebuilt, not accepted as already-present.
        existing = verify_runtime_directory(target, expect_version=meta.get("version"))
        if existing.complete:
            logger.info("Verified LibreOffice runtime already present at %s.", target)
            return {"status": "already-present", "soffice": str(existing.soffice),
                    "version": existing.version}
        if target.exists():
            # Exists but does not verify: interrupted extraction, partial
            # delete, missing bundled Python, or a forged marker. Treat it as
            # damaged and rebuild rather than reporting it installed (PF-015).
            logger.warning(
                "Existing runtime at %s is incomplete (%s); rebuilding it.",
                target, existing.reason,
            )
            shutil.rmtree(target, ignore_errors=True)

    if os.name != "nt":
        raise OfficeRuntimeError(
            "Automated project-local provisioning is implemented for Windows "
            "(msiexec administrative extraction). On this platform, install "
            "LibreOffice via your package manager and set PDF_FORGE_SOFFICE to "
            "its soffice path."
        )

    plat = meta.get("windows") or {}
    url = plat.get("url")
    expected_sha = plat.get("sha256")
    version = meta.get("version", "unknown")
    if not url:
        raise OfficeRuntimeError("Runtime metadata has no Windows download URL.")

    runtime_root().mkdir(parents=True, exist_ok=True)
    # Cache the verified installer so a retry (or a repair of an interrupted
    # extraction) does not download ~360 MB again.
    cache_dir = runtime_root() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    installer = cache_dir / (plat.get("filename") or "LibreOffice.msi")

    cached_ok = False
    if installer.exists() and expected_sha:
        if progress:
            progress("Verifying cached download...")
        cached_ok = _sha256(installer).lower() == expected_sha.lower()
        if not cached_ok:
            installer.unlink(missing_ok=True)

    if not cached_ok:
        if progress:
            progress(f"Downloading LibreOffice {version} (official)...")
        partial = installer.with_suffix(installer.suffix + ".part")
        _download(url, partial, download)
        if verify_checksum and expected_sha:
            actual = _sha256(partial)
            if actual.lower() != expected_sha.lower():
                partial.unlink(missing_ok=True)
                raise OfficeRuntimeError(
                    "Downloaded LibreOffice failed checksum verification "
                    f"(expected {expected_sha[:12]}..., got {actual[:12]}...). "
                    "Aborting; nothing was installed."
                )
            if progress:
                progress("Checksum verified.")
        elif verify_checksum:
            partial.unlink(missing_ok=True)
            raise OfficeRuntimeError(
                "No pinned checksum in runtime metadata; refusing to install an "
                "unverified download."
            )
        os.replace(partial, installer)

    if force:
        clean_runtime()
    # Extract into a unique staging directory and promote it only once the
    # complete runtime verifies, so an interrupted or partial extraction can
    # never be mistaken for an installed runtime (PF-015).
    runtime_root().mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="pdfforge_lostage_",
                                    dir=str(runtime_root())))
    try:
        if progress:
            progress("Extracting (administrative install, no system changes)...")
        _admin_extract_msi(installer, staging)

        # Trim before verifying, so the runtime is only ever promoted in the
        # exact shape it will be used in.
        freed = _trim_runtime(staging)
        if freed and progress:
            progress(f"Removing components this tool does not use "
                     f"({freed // (1024 * 1024)} MB)...")

        staged = verify_runtime_directory(staging, expect_version=version)
        if not staged.complete:
            raise OfficeRuntimeError(
                f"The extracted runtime is incomplete: {staged.reason}. "
                "Nothing was installed."
            )
        # The marker is written LAST, so its presence implies a verified tree.
        (staging / ".provisioned.json").write_text(
            json.dumps({"version": staged.version,
                        "soffice": str(staged.soffice)}, indent=2),
            encoding="utf-8",
        )
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(staging), str(target))     # atomic promotion
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    final = verify_runtime_directory(target, expect_version=version)
    if not final.complete:
        raise OfficeRuntimeError(
            f"The promoted runtime does not verify: {final.reason}."
        )
    soffice = final.soffice
    logger.info("Provisioned LibreOffice %s at %s.", version, soffice)
    return {"status": "provisioned", "version": version, "soffice": str(soffice)}


def _download(url: str, dest: Path, download=None) -> None:
    if download is not None:
        download(url, dest)
        return
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)
    except Exception as exc:  # noqa: BLE001
        raise OfficeRuntimeError(f"Download failed: {exc}") from exc


def _ensure_installer_service() -> None:
    """Refuse to run msiexec only when the installer service is DISABLED.

    PDF Forge never changes Windows service state (PF-017). It does not need to:
    msiserver ships as a *demand-start* service, so being stopped is its normal
    idle state and the Service Control Manager starts it on demand when msiexec
    runs. Treating "stopped" as a blocker would reject the common case.

    A service configured as DISABLED is different: the SCM will not start it, so
    msiexec fails with an obscure error. Report that up front instead, and leave
    the fix to the user.
    """
    try:
        config = subprocess.run(["sc", "qc", "msiserver"], capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return  # Cannot query; let msiexec try (bounded by its own timeout).
    if "DISABLED" not in config.stdout.upper():
        return
    raise OfficeRuntimeError(
        "The Windows Installer service (msiserver) is disabled, so Windows "
        "cannot run the extraction. PDF Forge does not change Windows service "
        "state; re-enable it yourself and run --setup-office again:\n"
        "    sc config msiserver start= demand        (elevated)\n"
        "Nothing was downloaded or installed."
    )


def _admin_extract_msi(installer: Path, target: Path,
                       timeout: int = EXTRACT_TIMEOUT) -> None:
    """Officially supported silent administrative extraction (no registration).

    An administrative install unpacks the payload into ``target`` without
    registering the product: no system install, PATH, registry, shortcut, file
    association, or service change. Bounded by ``timeout`` so a stalled
    installer surfaces as a clear error instead of hanging the application.
    """
    _ensure_installer_service()
    # msiexec does not use standard argv parsing: a public property whose value
    # contains spaces must be written TARGETDIR="C:\with spaces" - quoting the
    # whole "TARGETDIR=..." token (what Python's argv quoting produces) is
    # rejected with 1639 ERROR_INVALID_COMMAND_LINE. Pass an explicit command
    # line instead. This is NOT a shell invocation (shell=False), so the string
    # goes straight to CreateProcess and no shell metacharacter is interpreted;
    # every value here is an application-controlled path, never user input.
    destination = str(target.resolve()).rstrip("\\")
    cmdline = f'msiexec /a "{installer}" /qn TARGETDIR="{destination}"'
    logger.info("Administrative extraction: %s", cmdline)
    try:
        process = subprocess.Popen(cmdline, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   shell=False)
    except OSError as exc:
        raise OfficeRuntimeError(f"Administrative extraction failed: {exc}") from exc

    try:
        _await_extraction(process, target, timeout)
    except OfficeRuntimeError:
        _terminate(process)
        raise

    if process.returncode != 0:
        raise OfficeRuntimeError(
            f"msiexec administrative extraction failed (code {process.returncode}). "
            "Nothing was installed system-wide."
        )


def _await_extraction(process: subprocess.Popen, target: Path, timeout: int,
                      no_progress_timeout: int = NO_PROGRESS_TIMEOUT) -> None:
    """Wait for msiexec, aborting if it stalls instead of hanging forever.

    Windows Installer can block indefinitely (no CPU, nothing written) when it
    cannot service the request - for example in a restricted or non-interactive
    session. Watching TARGETDIR for growth turns that silent hang into a clear,
    actionable failure well before the overall timeout.
    """
    deadline = time.monotonic() + timeout
    last_change = time.monotonic()
    last_count = -1
    while process.poll() is None:
        if time.monotonic() > deadline:
            raise OfficeRuntimeError(
                f"The administrative extraction did not finish within {timeout}s "
                "and was stopped. Nothing was installed system-wide."
            )
        count = _extracted_file_count(target)
        if count != last_count:
            last_count = count
            last_change = time.monotonic()
        elif time.monotonic() - last_change > no_progress_timeout:
            raise OfficeRuntimeError(
                "Windows Installer accepted the request but wrote nothing for "
                f"{no_progress_timeout}s, so the extraction was stopped "
                "(nothing was installed system-wide).\n"
                "This happens when Windows Installer cannot service the request "
                "in the current session - commonly a restricted, non-interactive, "
                "or policy-limited environment.\n"
                "Work around it by extracting once from an interactive "
                "PowerShell window:\n"
                f'  msiexec /a "<downloaded .msi>" /qb TARGETDIR="{target}"\n'
                "The download is cached under .tools/cache, so re-running "
                "--setup-office afterwards will reuse it. Alternatively install "
                "LibreOffice yourself and point PDF_FORGE_SOFFICE at its "
                "soffice executable."
            )
        time.sleep(2)


def _extracted_file_count(target: Path) -> int:
    """Number of files written into the extraction target so far."""
    try:
        return sum(1 for _ in target.rglob("*"))
    except OSError:
        return 0
