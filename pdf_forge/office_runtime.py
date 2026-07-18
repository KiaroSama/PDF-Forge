"""Project-local, CLI-only LibreOffice + unoserver runtime for PDF conversion.

Design (see README "Convert to PDF"):
  * ``unoserver`` is installed into the project ``.venv`` like any other Python
    dependency; its client talks to the server over a private localhost XMLRPC
    port, so the client itself never imports ``uno``.
  * LibreOffice is a native renderer that cannot live inside a venv, so it is
    provisioned project-locally under ``.tools/libreoffice/`` (git-ignored) via
    an official administrative extraction - never a system-wide desktop install,
    no PATH/registry/shortcut/service changes, no GUI.
  * The server runs under LibreOffice's *bundled* Python (which can ``import
    uno``) with the venv's ``site-packages`` on ``PYTHONPATH`` so it still loads
    the venv-installed ``unoserver``.
  * Every conversion run starts a dedicated headless ``soffice`` on a random
    localhost port with an isolated temporary user profile, and terminates only
    that process on success, failure, timeout, cancellation, or exit.

This module holds no interactive UI; :mod:`pdf_forge.ops_office` drives it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .constants import *  # noqa: F401,F403

__all__ = [
    'OfficeRuntimeError', 'runtime_root', 'libreoffice_dir', 'load_runtime_meta',
    'find_soffice', 'find_soffice_python', 'venv_site_packages',
    'unoserver_installed', 'unoserver_version', 'libreoffice_version',
    'runtime_status', 'random_localhost_port',
    'ConversionServer', 'convert_to_pdf', 'provision_runtime', 'clean_runtime',
    'start_conversion_server', 'is_bridge_lost', 'convert_via_soffice_cli',
    'PASSWORD_SENTINEL', 'BRIDGE_LOST_SENTINEL', 'warm_up', 'save_with_password',
]


class OfficeRuntimeError(RuntimeError):
    """Raised when the local conversion runtime is missing or misbehaves."""


# Env override to point at an already-installed LibreOffice instead of the
# project-local runtime (the project-local runtime is still preferred by
# default; this only takes effect when explicitly set).
_SOFFICE_ENV = "PDF_FORGE_SOFFICE"

# Bounded timeouts (seconds). A timeout is a failure signal, never a password
# attempt limit.
SERVER_START_TIMEOUT = 90
CONVERT_TIMEOUT = 180
# Provisioning unpacks a ~360 MB package; generous, but never unbounded.
EXTRACT_TIMEOUT = 1800
# Abort sooner when Windows Installer writes nothing at all (a silent stall).
NO_PROGRESS_TIMEOUT = 180


def runtime_root() -> Path:
    """The git-ignored ``.tools`` directory at the project root."""
    return Path(__file__).resolve().parent.parent / ".tools"


def libreoffice_dir() -> Path:
    """The project-local LibreOffice runtime directory."""
    return runtime_root() / "libreoffice"


def _meta_path() -> Path:
    """Tracked runtime metadata (pinned version, source URL, checksum)."""
    return Path(__file__).resolve().parent.parent / "office_runtime_meta.json"


def load_runtime_meta() -> dict:
    """Load the pinned LibreOffice provisioning metadata (tracked JSON)."""
    try:
        return json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OfficeRuntimeError(
            f"Could not read runtime metadata ({_meta_path().name}): {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def _soffice_names() -> List[str]:
    """Executable names to look for, best first.

    On Windows prefer ``soffice.exe``, NOT the ``soffice.com`` console shim:
    the shim only launches the real binary and then exits, so unoserver - which
    watches the PID it spawned - concludes "LibreOffice died" and disposes the
    UNO bridge in the middle of the first export.
    """
    if os.name == "nt":
        return ["soffice.exe", "soffice.com"]
    return ["soffice"]


def _search_soffice_under(base: Path) -> Optional[Path]:
    """Find a soffice binary somewhere under ``base`` (program/ subdir first)."""
    if not base.exists():
        return None
    for name in _soffice_names():
        direct = base / "program" / name
        if direct.exists():
            return direct
    # Administrative extraction can nest under LibreOffice/program.
    for name in _soffice_names():
        for candidate in base.rglob(name):
            if candidate.is_file():
                return candidate
    return None


def find_soffice() -> Optional[Path]:
    """Locate the soffice executable to use.

    Order: project-local runtime first (preferred), then an explicit override
    via ``PDF_FORGE_SOFFICE``. A system-wide LibreOffice is used only when that
    env var points at it - never auto-registered or modified.
    """
    local = _search_soffice_under(libreoffice_dir())
    if local is not None:
        return local
    override = os.environ.get(_SOFFICE_ENV)
    if override:
        p = Path(override)
        if p.is_file():
            return p
        found = _search_soffice_under(p)
        if found is not None:
            return found
    return None


def find_soffice_python() -> Optional[Path]:
    """Locate LibreOffice's bundled Python (runs the unoserver server).

    The bundled interpreter is the only one that can ``import uno`` reliably on
    Windows, so the server runs under it while loading the venv's unoserver via
    ``PYTHONPATH``.
    """
    soffice = find_soffice()
    if soffice is None:
        return None
    program = soffice.parent
    for name in ("python.exe", "python3", "python"):
        candidate = program / name
        if candidate.exists():
            return candidate
    return None


def venv_site_packages() -> List[str]:
    """Return the *site-packages* directories of the running interpreter.

    Only real ``site-packages`` folders are returned. ``site.getsitepackages()``
    also yields ``sys.prefix`` itself in a virtualenv, and putting that on
    ``PYTHONPATH`` breaks LibreOffice's embedded Python (it starts resolving its
    own stdlib against the venv and the UNO bridge dies on first use). The
    server only needs the directory that actually contains ``unoserver``.
    """
    import site

    candidates: List[str] = []
    try:
        candidates.extend(site.getsitepackages())
    except AttributeError:  # pragma: no cover - virtualenv edge
        pass
    candidates.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    candidates.append(os.path.join(sys.prefix, "lib", "site-packages"))

    seen, out = set(), []
    for path in candidates:
        if not path or path in seen or not os.path.isdir(path):
            continue
        if os.path.basename(path.rstrip("\\/")).lower() != "site-packages":
            continue  # never the venv root itself
        seen.add(path)
        out.append(path)
    return out


def unoserver_installed() -> bool:
    """True when the venv has the unoserver package importable."""
    import importlib.util

    return importlib.util.find_spec("unoserver") is not None


def unoserver_version() -> Optional[str]:
    """Installed unoserver version string, or ``None`` when not installed."""
    try:
        from importlib.metadata import version

        return version("unoserver")
    except Exception:  # noqa: BLE001 - not installed / metadata missing
        return None


def libreoffice_version() -> Optional[str]:
    """Best-effort LibreOffice version from the provisioning metadata sidecar."""
    marker = libreoffice_dir() / ".provisioned.json"
    try:
        return json.loads(marker.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def runtime_status() -> dict:
    """Summarize what parts of the conversion runtime are available."""
    soffice = find_soffice()
    return {
        "unoserver_installed": unoserver_installed(),
        "unoserver_version": unoserver_version(),
        "soffice": str(soffice) if soffice else None,
        "soffice_python": str(find_soffice_python() or "") or None,
        "libreoffice_version": libreoffice_version(),
        "ready": bool(soffice) and unoserver_installed()
        and find_soffice_python() is not None,
    }


def random_localhost_port() -> int:
    """Reserve a free localhost TCP port (bind to 0, read it back, release)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #

@dataclass
class ConversionServer:
    """A running, task-owned headless LibreOffice + unoserver instance.

    Use via :func:`start_conversion_server` as a context manager. On exit it
    terminates *only* the process it started and removes the isolated profile,
    on every path (success, error, timeout, cancellation).
    """

    process: subprocess.Popen
    port: int
    profile_dir: Path
    soffice: Path
    log_handle: object = None
    log_path: Optional[Path] = None

    def read_log(self, limit: int = 8000) -> str:
        """Tail of the child's diagnostics (empty when unavailable)."""
        try:
            data = Path(self.log_path).read_bytes()[-limit:]
            return data.decode("utf-8", errors="replace")
        except (OSError, TypeError):
            return ""

    def stop(self) -> None:
        _terminate(self.process)
        # Close our end of the log before the profile (which holds it) is removed.
        try:
            if self.log_handle is not None:
                self.log_handle.close()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass
        shutil.rmtree(self.profile_dir, ignore_errors=True)



def _profile_argument(profile_dir: Path) -> str:
    """Profile location to hand to ``unoserver --user-installation``.

    unoserver converts this to a file:// URI itself (``Path(value).as_uri()``),
    so it must be a plain absolute path - passing a URI makes ``Path`` treat it
    as relative and abort with "relative path can't be expressed as a file URI".
    """
    return str(profile_dir.resolve())


def _xcu_prop(path: str, name: str, value_type: str, value: str) -> str:
    return (
        f'  <item oor:path="{path}">'
        f'<prop oor:name="{name}" oor:op="fuse" oor:type="xs:{value_type}">'
        f"<value>{value}</value></prop></item>\n"
    )


def _harden_profile(profile_dir: Path) -> Path:
    """Write a locked-down LibreOffice profile before the server starts.

    DISABLED BY DEFAULT - enable with ``PDF_FORGE_HARDEN_PROFILE=1``.

    Pre-seeding ``registrymodifications.xcu`` into a profile directory that
    LibreOffice has not initialized yet correlated with the UNO bridge being
    disposed on the first conversion ("Binary URP bridge already disposed") in
    the Windows end-to-end job, so it is not applied automatically. Macro and
    link-update hardening therefore is NOT currently enforced; that remains an
    open item rather than a claim.

    What it writes when enabled:

      * ``MacroSecurityLevel = 3`` (very high) and macro execution disabled, so
        a macro inside a converted document is never run;
      * link/update modes set to 0, so a document cannot refresh external links,
        DDE, or data sources - i.e. cannot reach the network - while converting;
      * document recovery and the first-start wizard disabled, so conversion
        never blocks on a dialog.

    The profile is created fresh per run and removed on teardown, so these
    settings can never leak into a user's own LibreOffice configuration.
    """
    registry = profile_dir / "user"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        + _xcu_prop("/org.openoffice.Office.Common/Security/Scripting",
                    "MacroSecurityLevel", "int", "3")
        + _xcu_prop("/org.openoffice.Office.Common/Security/Scripting",
                    "DisableMacrosExecution", "boolean", "true")
        + _xcu_prop("/org.openoffice.Office.Writer/Content/Update",
                    "Link", "int", "0")
        + _xcu_prop("/org.openoffice.Office.Calc/Content/Update",
                    "Link", "int", "0")
        + _xcu_prop("/org.openoffice.Office.Common/Save/Document",
                    "CreateBackup", "boolean", "false")
        + _xcu_prop("/org.openoffice.Office.Recovery/RecoveryInfo",
                    "Enabled", "boolean", "false")
        + _xcu_prop("/org.openoffice.Setup/Office",
                    "FirstStartWizardCompleted", "boolean", "true")
        + "</oor:items>\n",
        encoding="utf-8",
    )
    return profile_dir


def save_with_password(server, src: Path, out: Path, password: str) -> bool:
    """Save ``src`` as a password-protected copy using LibreOffice itself.

    Used to build encrypted fixtures for the end-to-end tests without needing
    Microsoft Office. Returns ``False`` when this build cannot do it, so callers
    skip rather than fail. The password is passed in memory only.
    """
    from unoserver.client import UnoClient

    try:
        client = UnoClient(server="127.0.0.1", port=str(server.port))
        client.convert(
            inpath=str(src),
            outpath=str(out),
            convert_to="docx",
            filter_options=[f"EncryptFile={password}"],
        )
        return Path(out).exists() and Path(out).stat().st_size > 0
    except Exception as exc:  # noqa: BLE001 - fixture creation is best effort
        logger.info("Could not create an encrypted fixture: %s", exc)
        return False


def _terminate(process: subprocess.Popen) -> None:
    """Terminate a task-owned process **and its children**, nothing else.

    The unoserver process launches ``soffice`` itself, so terminating only the
    parent can orphan LibreOffice. On Windows the whole tree is taken down by
    PID with ``taskkill /T``, which touches exactly this process tree and never
    an unrelated LibreOffice the user has open.
    """
    if process.poll() is None and os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    if process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    except Exception:  # noqa: BLE001 - never raise from teardown
        pass


def start_conversion_server(
    start_timeout: int = SERVER_START_TIMEOUT,
) -> ConversionServer:
    """Start a dedicated headless conversion server on a random localhost port.

    Raises :class:`OfficeRuntimeError` when the runtime is incomplete or the
    server does not become ready within ``start_timeout`` seconds.
    """
    soffice = find_soffice()
    if soffice is None:
        raise OfficeRuntimeError(
            "The project-local LibreOffice runtime was not found. Provision it "
            "first (Convert menu -> setup, or Run.ps1 setup)."
        )
    lo_python = find_soffice_python()
    if lo_python is None:
        raise OfficeRuntimeError(
            "LibreOffice's bundled Python was not found next to soffice; the "
            "conversion server cannot start."
        )
    if not unoserver_installed():
        raise OfficeRuntimeError(
            "The 'unoserver' package is not installed in the project .venv."
        )

    port = random_localhost_port()
    uno_port = random_localhost_port()
    profile_dir = Path(tempfile.mkdtemp(prefix="pdfforge_loprofile_"))
    if os.environ.get("PDF_FORGE_HARDEN_PROFILE") == "1":
        # Opt-in only: see _harden_profile for why this is not the default.
        _harden_profile(profile_dir)

    env = dict(os.environ)
    # Let LibreOffice's bundled Python import the venv-installed unoserver.
    extra = os.pathsep.join(venv_site_packages())
    env["PYTHONPATH"] = extra + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    # The child's diagnostics go to a file inside its own profile directory, not
    # to an OS pipe. An undrained PIPE can fill its buffer and block the child
    # mid-conversion (PF-014); a file also means a crash leaves readable evidence
    # instead of output nobody ever consumed. The file dies with the profile, so
    # it cannot grow without bound across runs.
    log_path = profile_dir / "unoserver.log"
    cmd = [
        str(lo_python), "-m", "unoserver.server",
        "--executable", str(soffice),
        "--user-installation", _profile_argument(profile_dir),
        "--interface", "127.0.0.1",
        "--port", str(port),
        "--uno-port", str(uno_port),
        "--conversion-timeout", str(CONVERT_TIMEOUT),
    ]
    logger.info("Starting conversion server on 127.0.0.1:%d (uno %d).", port, uno_port)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        log_handle = open(log_path, "wb")
    except OSError as exc:
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise OfficeRuntimeError(f"Could not open the server log: {exc}") from exc
    try:
        process = subprocess.Popen(
            cmd, env=env, stdout=log_handle, stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except OSError as exc:
        log_handle.close()
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise OfficeRuntimeError(f"Could not start the conversion server: {exc}") from exc

    server = ConversionServer(process, port, profile_dir, soffice, log_handle,
                              log_path)
    try:
        _wait_until_ready(server, start_timeout)
    except Exception:
        server.stop()
        raise
    return server


def _wait_until_ready(server: ConversionServer, timeout: int) -> None:
    """Block until the server answers ``server_info``, or raise on timeout/exit."""
    from unoserver.client import UnoClient

    deadline = time.monotonic() + timeout
    client = UnoClient(server="127.0.0.1", port=str(server.port))
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            # Surface the server's own output; without it a startup failure is
            # an opaque exit code.
            detail = ""
            try:
                if server.process.stdout is not None:
                    detail = server.process.stdout.read()[-800:].strip()
            except Exception:  # noqa: BLE001
                pass
            raise OfficeRuntimeError(
                "The conversion server exited during startup "
                f"(code {server.process.returncode})."
                + (f"\n{detail}" if detail else "")
            )
        try:
            client.server_info()
            logger.info("Conversion server ready on 127.0.0.1:%d.", server.port)
            return
        except Exception:  # noqa: BLE001 - not up yet; retry until the deadline
            time.sleep(0.5)
    raise OfficeRuntimeError(
        f"The conversion server did not become ready within {timeout}s."
    )


def convert_to_pdf(
    server: ConversionServer,
    in_path: Path,
    out_path: Path,
    password: Optional[str] = None,
    timeout: int = CONVERT_TIMEOUT,
) -> None:
    """Convert one source file to PDF through the running server.

    The password (when given) is passed via the unoserver Python API in memory,
    never on a command line, in the environment, or in a filename. Raises
    :class:`OfficeRuntimeError` on failure so the caller can classify it.
    """
    from unoserver.client import UnoClient

    client = UnoClient(server="127.0.0.1", port=str(server.port))
    # Deliberately no explicit output filter: LibreOffice picks the right PDF
    # exporter for the loaded document type (writer_pdf_Export /
    # calc_pdf_Export / impress_pdf_Export). Forcing the Writer exporter on a
    # spreadsheet or presentation raises inside UNO and disposes the bridge,
    # which then fails every later conversion in the same run.
    kwargs = dict(
        inpath=str(in_path),
        outpath=str(out_path),
        convert_to="pdf",
    )
    if password is not None:
        kwargs["password"] = password

    # Bound every conversion: a wedged LibreOffice would otherwise block the
    # whole queue forever. The call runs on a *daemon* thread that is joined
    # with a timeout and, on timeout, simply abandoned - never joined again.
    #
    # It must not be a ThreadPoolExecutor: leaving its `with` block (or any
    # shutdown(wait=True)) joins the worker, so a hung convert would re-block
    # right after the timeout fired and hang the run indefinitely. A daemon
    # thread cannot hold up interpreter exit, and stopping the server kills
    # LibreOffice, which makes the abandoned call fail and the thread exit.
    # A timeout is a failure signal only - never a password-attempt limit.
    import threading

    outcome: dict = {}

    def _call() -> None:
        try:
            client.convert(**kwargs)
            outcome["ok"] = True
        except BaseException as exc:  # noqa: BLE001 - reported via `outcome`
            outcome["error"] = exc

    worker = threading.Thread(
        target=_call, name=f"pdfforge-convert-{Path(in_path).name}", daemon=True
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.error("Conversion of '%s' timed out after %ss.", in_path, timeout)
        # Abandon the thread; the caller replaces the runtime, which kills the
        # LibreOffice process the call is blocked on.
        raise OfficeRuntimeError(BRIDGE_LOST_SENTINEL)
    error = outcome.get("error")
    if error is not None:
        classified = _classify_convert_error(error)
        # A lost bridge with no password involved is recoverable: the same
        # document converts through the command line with the same runtime.
        if classified == BRIDGE_LOST_SENTINEL and password is None:
            logger.info(
                "unoserver could not export '%s'; retrying via the soffice CLI.",
                Path(in_path).name,
            )
            try:
                convert_via_soffice_cli(server.soffice, in_path, out_path,
                                        timeout=timeout)
                return
            except OfficeRuntimeError as cli_exc:
                logger.error("CLI fallback also failed: %s", cli_exc)
                raise
        # Never include the password in the message.
        raise OfficeRuntimeError(classified) from error


#: Sentinel returned when the source needs a password.
PASSWORD_SENTINEL = "PASSWORD"
#: Sentinel returned when the UNO bridge / LibreOffice process died. The caller
#: must start a *fresh* server (with a fresh profile) before retrying.
BRIDGE_LOST_SENTINEL = "BRIDGE_LOST"


def is_bridge_lost(exc: Exception) -> bool:
    """True when the error means the LibreOffice side is gone, not a bad input."""
    return str(exc) == BRIDGE_LOST_SENTINEL


def warm_up(server: "ConversionServer") -> "ConversionServer":
    """Run one throwaway conversion so a cold-start crash costs no real file.

    LibreOffice can crash on the *first* PDF export after a fresh start. Because
    every restart is also a fresh start, retrying a user's file on a new server
    would keep meeting the same cold-start crash. Absorbing it with a trivial
    document leaves a warmed runtime for the real work. Returns the server to
    use (a replacement when the warm-up had to restart it).
    """
    for attempt in range(2):
        scratch = Path(tempfile.mkdtemp(prefix="pdfforge_warmup_"))
        try:
            probe = scratch / "warmup.txt"
            probe.write_text("warmup\n", encoding="utf-8")
            convert_to_pdf(server, probe, scratch / "warmup.pdf", timeout=120)
            logger.info("Conversion runtime warmed up.")
            return server
        except OfficeRuntimeError as exc:
            if not is_bridge_lost(exc) or attempt == 1:
                # Not a cold-start crash (or we already retried): carry on and
                # let the real conversion report any genuine problem.
                logger.info("Warm-up did not complete cleanly: %s", exc)
                return server
            logger.info("Cold-start crash absorbed by warm-up; restarting.")
            server.stop()
            server = start_conversion_server()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return server



def convert_via_soffice_cli(soffice: Path, in_path: Path, out_path: Path,
                            timeout: int = CONVERT_TIMEOUT) -> None:
    """Convert to PDF by driving ``soffice --convert-to`` directly.

    Fallback for documents the unoserver bridge cannot export. unoserver 3.7 on
    LibreOffice 25.8 fails on **Writer** documents here: the export raises inside
    the UNO bridge and pyuno then cannot marshal its own exception
    ("'traceback' object has no attribute 'getTypes'"), which destroys the real
    error and disposes the bridge. Calc documents are unaffected. The same
    document converts correctly through the command-line interface, so this path
    keeps the feature working with the *same* project-local runtime - still
    local, still headless, still no GUI.

    It cannot carry a password (that would put the secret on a command line), so
    encrypted sources stay on the unoserver in-memory path.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="pdfforge_cliprof_"))
    outdir = Path(tempfile.mkdtemp(prefix="pdfforge_cliout_"))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    cmd = [
        str(soffice), "--headless", "--invisible", "--nologo", "--nofirststartwizard",
        "--norestore", "--nodefault", "--nocrashreport",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to", "pdf", "--outdir", str(outdir), str(in_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, creationflags=creationflags)
        produced = list(outdir.glob("*.pdf"))
        if not produced:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            raise OfficeRuntimeError(
                "LibreOffice produced no PDF for this document"
                + (f": {detail}" if detail else ".")
            )
        shutil.move(str(produced[0]), str(out_path))
    except subprocess.TimeoutExpired as exc:
        raise OfficeRuntimeError(
            f"LibreOffice timed out converting this document after {timeout}s."
        ) from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)


def _classify_convert_error(exc: Exception) -> str:
    """Turn a raw conversion error into a clean, non-sensitive message."""
    text = str(exc).lower()
    if "password" in text or "wrong password" in text or "protected" in text:
        return PASSWORD_SENTINEL  # the caller maps this to a password retry
    # LibreOffice crashed or the URP bridge was disposed: the profile is now
    # suspect, so the caller restarts the server instead of reusing it.
    # NOTE: ``text`` is already lower-cased, so every needle must be too.
    # unoserver reports a crashed LibreOffice as a failure to marshal a
    # traceback object ("...has no attribute 'getTypes'"), which is why that
    # signature counts as a lost bridge rather than a bad input file.
    if any(needle in text for needle in (
            "disposed", "died", "gettypes", "connection could be made",
            "10061", "connection refused")):
        return BRIDGE_LOST_SENTINEL
    if "no such file" in text or "not found" in text:
        return "The source file could not be opened by LibreOffice."
    return f"LibreOffice could not convert the file: {exc}"


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

    if not force and find_soffice() is not None and _search_soffice_under(target):
        logger.info("LibreOffice runtime already present at %s.", target)
        return {"status": "already-present", "soffice": str(find_soffice())}

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
    target.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("Extracting (administrative install, no system changes)...")
    _admin_extract_msi(installer, target)

    soffice = _search_soffice_under(target)
    if soffice is None:
        raise OfficeRuntimeError(
            "Extraction finished but soffice was not found under the runtime "
            "directory; the runtime is incomplete."
        )
    (target / ".provisioned.json").write_text(
        json.dumps({"version": version, "soffice": str(soffice)}, indent=2),
        encoding="utf-8",
    )
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
    """Make sure the Windows Installer service is running before calling msiexec.

    ``msiexec /a`` does not reliably start ``msiserver`` from a non-interactive
    session: when the service is stopped it can block indefinitely instead of
    failing. Starting it first (or reporting clearly that it cannot be started)
    turns a silent hang into an actionable message.
    """
    try:
        query = subprocess.run(["sc", "query", "msiserver"], capture_output=True,
                               text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return  # Cannot query; let msiexec try anyway (guarded by its timeout).
    if "RUNNING" in query.stdout.upper():
        return
    logger.info("Windows Installer service is not running; starting it.")
    try:
        started = subprocess.run(["net", "start", "msiserver"], capture_output=True,
                                 text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OfficeRuntimeError(
            "The Windows Installer service (msiserver) is not running and could "
            f"not be started ({exc}). Start it and run --setup-office again."
        ) from exc
    if started.returncode != 0:
        message = (started.stdout + started.stderr).strip().splitlines()
        detail = message[-1] if message else f"exit code {started.returncode}"
        raise OfficeRuntimeError(
            "The Windows Installer service (msiserver) is stopped and could not "
            f"be started: {detail}. Start it (an elevated 'net start msiserver') "
            "and run --setup-office again. Nothing was installed."
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
