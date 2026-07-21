"""Run a task-owned headless LibreOffice and convert documents through it.

Every conversion batch starts its own ``soffice`` on a random localhost port
with an isolated, hardened user profile, and terminates exactly that process on
success, failure, timeout, cancellation, or exit.

Discovery and paths live in :mod:`pdf_forge.office_discovery`; installing the
runtime lives in :mod:`pdf_forge.office_provision`.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .constants import *  # noqa: F401,F403
from .office_discovery import (
    CONVERT_TIMEOUT_MAX, OfficeRuntimeError, SERVER_START_TIMEOUT,
    conversion_timeout_for, find_soffice, find_soffice_python,
    random_localhost_port, unoserver_installed, venv_site_packages,
)


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
    #: POSIX process-group id captured at launch. Stored (not read from the live
    #: process) so the group can be reaped even after the launcher exits and
    #: os.getpgid() would fail - the parent-exited/child-alive case (N-12).
    pgid: Optional[int] = None

    def read_log(self, limit: int = 8000) -> str:
        """Tail of the child's diagnostics (empty when unavailable)."""
        try:
            data = Path(self.log_path).read_bytes()[-limit:]
            return data.decode("utf-8", errors="replace")
        except (OSError, TypeError):
            return ""

    def is_alive(self) -> bool:
        """Whether the unoserver process this handle owns is still running."""
        return self.process.poll() is None

    def stop(self) -> None:
        # If the unoserver parent has already exited - a lost bridge or a
        # cold-start crash, both routine here - then taskkill /T has no tree
        # left to walk and its soffice.bin child is orphaned. Decide that
        # before terminating, because afterwards the two cases look identical.
        orphaned = self.process.poll() is not None
        _terminate(self.process)
        # Close our end of the log before the profile (which holds it) is removed.
        try:
            if self.log_handle is not None:
                self.log_handle.close()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass
        if orphaned:
            _kill_profile_owners(self.profile_dir)
        # POSIX: reap the stored process group too. When the launcher has already
        # exited, _terminate's os.getpgid(pid) fails and cannot reach a child
        # that outlived it; the group id captured at launch still can (N-12).
        _reap_process_group(self.pgid)
        _remove_profile(self.profile_dir)



def _kill_profile_owners(profile_dir: Path) -> None:
    """Kill LibreOffice processes still using *our* profile, and only ours.

    ``_terminate`` takes down the whole process tree, but only while the
    unoserver parent is alive to define it. Once that parent has exited, its
    soffice.bin child is orphaned: nothing reaps it, it recreates the
    user-installation directory moments after ``stop()`` deleted it, and it sits
    on hundreds of megabytes. Measured: two survivors after one suite run, and a
    later server refusing to start.

    Matching on the profile path is what makes this safe. That directory is a
    fresh ``mkdtemp`` owned by this process, so a LibreOffice the user has open
    cannot match it - the promise of "task-owned processes, nothing else" holds.
    """
    if os.name != "nt":
        # POSIX orphans are not addressed here; the measured failure and the
        # runtime this ships against are Windows.
        return
    script = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='soffice.bin' or Name='soffice.exe'\" | "
        "Where-Object { $_.CommandLine -like $env:PDFFORGE_PROFILE_GLOB } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=30,
            # Match the URI form, which is what is actually on the command
            # line: we pass --user-installation a plain path, but unoserver
            # converts it with Path(...).as_uri() before launching soffice.
            # A glob built from the native path contains backslashes, which
            # PowerShell's -like treats literally, so it can never match the
            # forward-slash URI. Measured against a live process: the native
            # form matched 0 processes, this one matched 2.
            #
            # Through the environment, so a path containing backslashes,
            # spaces or quotes cannot be reinterpreted as PowerShell syntax.
            env=dict(os.environ,
                     PDFFORGE_PROFILE_GLOB=f"*{profile_dir.resolve().as_uri()}*"),
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _reap_process_group(pgid: Optional[int]) -> None:
    """POSIX: kill a stored process group, even after its leader has exited.

    A launcher started with ``start_new_session`` is its own group leader; the
    group persists as long as any member is alive. Capturing the pgid at launch
    (rather than reading ``os.getpgid`` of a possibly-dead leader) is what makes
    the parent-exited/child-alive case reachable: the surviving ``soffice.bin``
    is still in this group and is reaped here (N-12). No-op on Windows and when
    no pgid was captured.
    """
    if pgid is None or os.name == "nt":
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return  # the group is already gone (or not ours) - nothing to reap


def _remove_profile(path: Path, attempts: int = 12, delay: float = 0.25) -> None:
    """Remove a conversion profile, tolerating a child that is still exiting.

    Terminating the unoserver process does not instantly reap the soffice.bin
    it started, and on Windows that grandchild keeps handles open inside the
    profile for a moment afterwards. A single ``rmtree(ignore_errors=True)``
    then silently gives up and leaves the whole directory behind - the E2E
    leak gate caught exactly one such profile per run, and the docstring
    claiming profiles are "removed on teardown" was simply not true.

    Retrying briefly is enough; if it still cannot be removed the failure is
    logged rather than swallowed, because teardown must not raise.
    """
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(delay)
    # Still here: something is holding the profile open. The usual cause is an
    # orphaned soffice.bin that outlived its launcher - the CLI one-shot exits
    # the launcher before its child, so the finalizer never saw a live tree to
    # kill (N-12). Reap whatever still holds THIS profile, then try once more.
    # This only runs when the profile is genuinely stuck, so the PowerShell reap
    # cost is not paid on the normal path where removal succeeds immediately.
    _kill_profile_owners(path)
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(delay)
    logger.warning("Could not remove the conversion profile '%s'.", path)


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

    Applied to every conversion profile (set ``PDF_FORGE_HARDEN_PROFILE=0`` only
    to debug). An earlier revision made this opt-in on the assumption that it
    destabilised the UNO bridge; measuring it disproved that - with the
    lockdown applied, Writer documents that otherwise crash the bridge convert
    natively, because link and index updating is exactly what fails.

    What it writes:

      * ``MacroSecurityLevel = 3`` (very high) and macro execution disabled, so
        a macro inside a converted document is never run;
      * link/update modes set to 0, so a document cannot refresh external links,
        DDE, or data sources while converting, and untrusted referer links
        blocked so a *linked graphic* cannot be fetched either. The two are
        separate settings: the update modes alone left a document able to reach
        the network through an image whose target is an external URL, which is
        measured end to end in ``tests/test_office_links.py`` against a
        loopback recorder with a positive control;
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
        # The two Update/Link modes above govern links to other *documents*,
        # DDE and data sources - they do not cover a linked graphic, which
        # LibreOffice fetches while loading, before export. A converted
        # document could therefore still reach the network through the most
        # ordinary web-bug shape: an image whose target is an external URL.
        # Measured: 1 request without this property, 0 with it.
        + _xcu_prop("/org.openoffice.Office.Common/Security/Scripting",
                    "BlockUntrustedRefererLinks", "boolean", "true")
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

    The result is *verified* to be genuinely encrypted before success is
    reported. LibreOffice accepts ``EncryptFile`` and can still write a plain
    file, and the previous "the output exists and is non-empty" check reported
    that as success - which made a caller assert against an unencrypted fixture
    instead of skipping.
    """
    from unoserver.client import UnoClient

    from .office import is_encrypted_office_file

    try:
        client = UnoClient(server="127.0.0.1", port=str(server.port))
        client.convert(
            inpath=str(src),
            outpath=str(out),
            convert_to="docx",
            filter_options=[f"EncryptFile={password}"],
        )
    except Exception as exc:  # noqa: BLE001 - fixture creation is best effort
        logger.info("Could not create an encrypted fixture: %s", exc)
        return False

    produced = Path(out)
    if not produced.exists() or produced.stat().st_size == 0:
        return False
    if not is_encrypted_office_file(produced):
        logger.info(
            "This LibreOffice build wrote '%s' without encrypting it; "
            "no encrypted fixture is available.", produced.name,
        )
        try:
            produced.unlink()
        except OSError:
            pass
        return False
    return True


def _terminate(process: subprocess.Popen) -> None:
    """Terminate a task-owned process **and its children**, nothing else.

    The unoserver process launches ``soffice`` itself, so terminating only the
    parent can orphan LibreOffice. On Windows the whole tree is taken down by
    PID with ``taskkill /T``, which touches exactly this process tree and never
    an unrelated LibreOffice the user has open. On POSIX the child is started in
    its own session (``start_new_session``), so the whole process group is
    signalled with ``os.killpg`` - the equivalent tree-kill, reaching the
    ``soffice.bin`` the launcher spawned without touching unrelated processes.
    """
    if process.poll() is None and os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    elif process.poll() is None and os.name != "nt":
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
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


def _allocate_distinct_port_pair(max_attempts: int = 100) -> "tuple[int, int]":
    """Two distinct free localhost ports for unoserver's server and UNO ports.

    unoserver needs two *distinct* ports; ephemeral allocation can hand back the
    same number twice, so retry until they differ - but bounded, because a stuck
    allocator that only ever returns one value must fail cleanly rather than spin
    forever (N-11). The bind-then-close TOCTOU against another process remains a
    documented ceiling: unoserver takes port numbers, not inherited sockets, and
    fails cleanly at startup if one is already taken.
    """
    port = random_localhost_port()
    for _ in range(max_attempts):
        uno_port = random_localhost_port()
        if uno_port != port:
            return port, uno_port
    raise OfficeRuntimeError(
        "Could not allocate two distinct localhost ports for the conversion "
        f"server after {max_attempts} attempts."
    )


#: Substrings that identify a "port already taken" startup failure on either
#: platform, so the bind-to-zero TOCTOU can be retried with a fresh pair while
#: every other startup failure is reported immediately.
_ADDRESS_IN_USE_SIGNS = (
    "address already in use",                  # POSIX (errno 98)
    "only one usage of each socket address",   # Windows (WSAEADDRINUSE)
    "eaddrinuse", "wsaeaddrinuse",
    "errno 98", "10048",
)


def _is_address_in_use(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(sign in text for sign in _ADDRESS_IN_USE_SIGNS)


def start_conversion_server(
    start_timeout: int = SERVER_START_TIMEOUT,
    max_attempts: int = 3,
) -> ConversionServer:
    """Start a dedicated headless conversion server on a random localhost port.

    Raises :class:`OfficeRuntimeError` when the runtime is incomplete or the
    server does not become ready within ``start_timeout`` seconds.

    A startup failure caused *specifically* by the bind-to-zero TOCTOU (another
    process grabbed the port between allocation and unoserver's bind) is retried
    up to ``max_attempts`` times with a freshly allocated distinct port pair;
    each failed attempt tears down its own process and profile first. Every other
    startup failure - a broken runtime, a crash, a timeout - is reported
    immediately rather than retried blindly (N-11).
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

    for attempt in range(max_attempts):
        port, uno_port = _allocate_distinct_port_pair()
        try:
            return _launch_conversion_server_once(
                soffice, lo_python, port, uno_port, start_timeout)
        except OfficeRuntimeError as exc:
            # _launch_conversion_server_once has already torn down its own
            # process and profile. Retry only a port collision, and only while
            # attempts remain - never attach to an unrelated listener, and never
            # retry a genuine runtime failure.
            if _is_address_in_use(exc) and attempt < max_attempts - 1:
                logger.warning(
                    "The conversion server port was taken at startup; retrying "
                    "with a fresh port pair (attempt %d of %d).",
                    attempt + 1, max_attempts)
                continue
            raise
    # Unreachable: the loop above always returns a server or raises.
    raise OfficeRuntimeError("The conversion server could not be started.")


def _launch_conversion_server_once(
    soffice: Path, lo_python: Path, port: int, uno_port: int, start_timeout: int,
) -> ConversionServer:
    """One start attempt on the given ports; cleans up its own process and
    profile on any failure before raising, so the caller can retry cleanly."""
    profile_dir = Path(tempfile.mkdtemp(prefix="pdfforge_loprofile_"))
    # Always hardened unless explicitly disabled for debugging. Beyond the
    # safety it enforces, disabling link/index updates is what keeps Writer
    # exports from crashing the UNO bridge in this runtime (measured).
    if os.environ.get("PDF_FORGE_HARDEN_PROFILE") != "0":
        # Hardening writes into the profile and may fail (read-only or full
        # temp). Every later failure path below removes the directory; this one
        # has to as well, or a refused start silently leaves a profile behind.
        try:
            _harden_profile(profile_dir)
        except OSError as exc:
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise OfficeRuntimeError(
                f"The conversion profile could not be hardened ({exc})."
            ) from exc

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
        # The server-side cap must be the *ceiling*, not the base allowance:
        # the per-file timeout is scaled by input size on the client, and a
        # fixed server cap would cut a large document off first.
        "--conversion-timeout", str(CONVERT_TIMEOUT_MAX),
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
    popen_kwargs = dict(env=env, stdout=log_handle, stderr=subprocess.STDOUT,
                        creationflags=creationflags)
    if os.name != "nt":
        # Own session/process group, so _terminate can killpg the whole tree
        # (the launcher plus the soffice.bin it spawns) on POSIX.
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(cmd, **popen_kwargs)
    except OSError as exc:
        log_handle.close()
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise OfficeRuntimeError(f"Could not start the conversion server: {exc}") from exc

    # Capture the process-group id now, while the launcher is alive, so a child
    # that outlives it can still be reaped (N-12). start_new_session made the
    # launcher its own group leader, so its pgid is its pid.
    pgid = None
    if os.name != "nt":
        try:
            pgid = os.getpgid(process.pid)
        except (OSError, AttributeError):
            # AttributeError only for a process double without a real pid; a real
            # Popen always has one. Degrade to "no group" rather than guessing.
            pgid = None

    server = ConversionServer(process, port, profile_dir, soffice, log_handle,
                              log_path, pgid=pgid)
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
            # an opaque exit code. Read the LOG FILE, not process.stdout: the
            # child's output was redirected to a file, so process.stdout is
            # always None and the old read produced nothing - the real cause
            # (e.g. "port already in use") sat unread in the log until stop()
            # deleted it with the profile.
            detail = server.read_log(800).strip()
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
    timeout: Optional[int] = None,
) -> None:
    """Convert one source file to PDF through the running server.

    The password (when given) is passed via the unoserver Python API in memory,
    never on a command line, in the environment, or in a filename. Raises
    :class:`OfficeRuntimeError` on failure so the caller can classify it.
    """
    from unoserver.client import UnoClient

    if timeout is None:
        timeout = conversion_timeout_for(in_path)
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

    # A server whose process has exited cannot answer, but the XMLRPC client
    # still spends ~50 seconds retrying the closed port before giving up - paid
    # again on every file in a batch. Check first and report the loss straight
    # away; the caller either restarts the runtime or takes the CLI fallback.
    if not server.is_alive():
        logger.info(
            "The conversion server is no longer running; not attempting the "
            "in-process path for '%s'.", Path(in_path).name,
        )
        outcome["error"] = OfficeRuntimeError(BRIDGE_LOST_SENTINEL)
        worker = None
    else:
        worker = threading.Thread(
            target=_call, name=f"pdfforge-convert-{Path(in_path).name}",
            daemon=True,
        )
        worker.start()
        # Wait in short slices and watch the process, rather than blocking for
        # the whole timeout. When LibreOffice dies mid-export the XMLRPC client
        # keeps retrying the now-closed port for ~50 seconds before raising -
        # time spent learning something the exit code already told us. Noticing
        # the exit ends the wait immediately and lets the caller fall back.
        deadline = time.monotonic() + timeout
        while worker.is_alive() and time.monotonic() < deadline:
            worker.join(0.25)
            if not worker.is_alive():
                break
            if not server.is_alive():
                logger.info(
                    "LibreOffice exited while converting '%s'; abandoning the "
                    "in-process attempt.", Path(in_path).name,
                )
                outcome["error"] = OfficeRuntimeError(BRIDGE_LOST_SENTINEL)
                worker = None
                break
    if worker is not None and worker.is_alive():
        logger.error("Conversion of '%s' timed out after %ss.", in_path, timeout)
        # Abandon the thread; the caller replaces the runtime, which kills the
        # LibreOffice process the call is blocked on.
        raise OfficeRuntimeError(BRIDGE_LOST_SENTINEL)
    error = outcome.get("error")
    if error is not None:
        classified = (
            BRIDGE_LOST_SENTINEL
            if isinstance(error, OfficeRuntimeError)
            and str(error) == BRIDGE_LOST_SENTINEL
            else _classify_convert_error(error)
        )
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
                # Report the bridge loss, not the rescue attempt's own failure:
                # the caller's recovery keys on "the runtime is gone, replace
                # it", and swapping in the fallback's error would hide that.
                logger.error("CLI fallback also failed: %s", cli_exc)
                raise OfficeRuntimeError(BRIDGE_LOST_SENTINEL) from cli_exc
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
            # Success is not proof the server survived. The cold-start crash
            # this function exists to absorb kills the process, and the CLI
            # fallback inside convert_to_pdf then completes the job - so no
            # exception arrives here and a DEAD server used to be returned.
            # Every later conversion then spent ~50s discovering the corpse
            # before falling back again, which is where the whole runtime cost
            # of a conversion batch came from.
            if server.is_alive():
                logger.info("Conversion runtime warmed up.")
            else:
                # The warm-up conversion succeeded, but through the CLI fallback
                # after LibreOffice exited. Restarting is not worth it: on a
                # runtime where the export always kills the server, a fresh one
                # dies the same way and costs another ~50s of startup for
                # nothing. Returning the dead handle is now cheap and honest -
                # convert_to_pdf sees is_alive() == False and goes straight to
                # the command-line path instead of waiting out a closed port.
                logger.info(
                    "LibreOffice exited during warm-up; conversions will use "
                    "the command-line path."
                )
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
                            timeout: Optional[int] = None) -> None:
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
    if timeout is None:
        timeout = conversion_timeout_for(in_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="pdfforge_cliprof_"))
    outdir = Path(tempfile.mkdtemp(prefix="pdfforge_cliout_"))
    # This fallback runs automatically on bridge loss, so it must carry exactly
    # the same macro and link-update lockdown as the server path. A fresh empty
    # profile means LibreOffice defaults, i.e. macros enabled - the retry would
    # open the very document the bridge just died on, unprotected (C-12). Fail
    # closed: an unhardened profile is not an acceptable degraded mode.
    try:
        _harden_profile(profile)
    except Exception as exc:  # noqa: BLE001 - surfaced, never ignored
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)
        raise OfficeRuntimeError(
            f"The conversion profile could not be hardened ({exc}); the "
            "document was not opened."
        ) from exc
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    cmd = [
        str(soffice), "--headless", "--invisible", "--nologo", "--nofirststartwizard",
        "--norestore", "--nodefault", "--nocrashreport",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to", "pdf", "--outdir", str(outdir), str(in_path),
    ]
    # Popen + a log file, not subprocess.run(capture_output=True): soffice
    # launches a separate soffice.bin child, and subprocess.run's timeout kills
    # only the launcher, orphaning that child (holding the profile open) exactly
    # as the server path did before it was reaped. A pipe can also fill and hang
    # the child (PF-014), so the child's output goes to a file in the profile.
    log_path = profile / "cli.log"
    proc = None
    cli_pgid = None
    try:
        log_handle = open(log_path, "wb")
    except OSError as exc:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)
        raise OfficeRuntimeError(f"Could not open the CLI log: {exc}") from exc
    cli_popen_kwargs = dict(stdout=log_handle, stderr=subprocess.STDOUT,
                            creationflags=creationflags)
    if os.name != "nt":
        cli_popen_kwargs["start_new_session"] = True   # enable killpg tree-kill
    try:
        proc = subprocess.Popen(cmd, **cli_popen_kwargs)
        # Capture the group id while the launcher is alive, so a soffice.bin that
        # outlives it can still be reaped on POSIX even after the launcher exits
        # (N-12); os.getpgid on a dead launcher would fail.
        if os.name != "nt":
            try:
                cli_pgid = os.getpgid(proc.pid)
            except (OSError, AttributeError):
                cli_pgid = None
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Take the whole tree down - the launcher AND the soffice.bin child
            # that carries this profile on its command line - then remove the
            # profile only after nothing owns it.
            _terminate(proc)
            _kill_profile_owners(profile)
            _reap_process_group(cli_pgid)
            raise OfficeRuntimeError(
                f"LibreOffice timed out converting this document after {timeout}s."
            ) from exc
        produced = list(outdir.glob("*.pdf"))
        if not produced:
            try:
                detail = log_path.read_text("utf-8", "replace").strip()[:300]
            except OSError:
                detail = ""
            raise OfficeRuntimeError(
                "LibreOffice produced no PDF for this document"
                + (f": {detail}" if detail else ".")
            )
        shutil.move(str(produced[0]), str(out_path))
    finally:
        try:
            log_handle.close()
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass
        # A launcher still alive here means an error path that did not already
        # reap it; take it and its profile-owning child down. The timeout path
        # already reaped, so on success/normal-error this is a no-op and no
        # per-conversion PowerShell cost is paid. _remove_profile retries, which
        # absorbs a child that is a few milliseconds from exiting on its own.
        if proc is not None and proc.poll() is None:
            _terminate(proc)
            _kill_profile_owners(profile)
        # POSIX: reap a child that outlived an already-exited launcher (the block
        # above only fires while the launcher is still alive). No-op on Windows,
        # where _remove_profile's own stuck-profile reap covers that case.
        _reap_process_group(cli_pgid)
        _remove_profile(profile)
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


