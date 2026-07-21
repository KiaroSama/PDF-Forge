"""Race-safe output promotion and the generated-output state store.

Two responsibilities that every writer in the application shares:

1. :func:`promote_atomically` - move a validated temporary file onto its final
   name **without ever overwriting** anything. Queue-time reservation only
   protects one process; between configuration and execution another process
   (or another PDF Forge instance) can create the destination. ``os.replace()``
   would silently clobber it, so promotion instead claims the name atomically
   and, on collision, allocates the next free suffix and retries.

2. :class:`StateStore` / the generated-output manifest - remembers which PDFs
   this application produced, so folder tools never reprocess their own output.
   It lives in the per-user application-data directory (never the repository
   checkout, which may be read-only), is written atomically, and is guarded by a
   cross-process lock so concurrent instances cannot lose each other's entries.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional, Set

from .constants import *  # noqa: F401,F403

__all__ = [
    'PromotionError', 'OutputResult', 'promote_atomically', 'claim_unique_path',
    'state_dir', 'manifest_path', 'file_identity', 'FileLock',
    'LockError', 'LockTimeout', 'LockUnavailable',
    'load_generated_outputs', 'record_generated_output',
    'forget_generated_outputs',
]


class PromotionError(OSError):
    """Raised when a validated output cannot be promoted to a final name."""


class LockError(OSError):
    """Base: the manifest lock could not be acquired, so nothing was written."""


class LockTimeout(LockError):
    """Another process held the lock for the whole bounded wait. Retryable."""


class LockUnavailable(LockError):
    """Lock storage is unusable (not a directory, unwritable). Not retryable."""


# --------------------------------------------------------------------------- #
# Atomic, no-clobber promotion
# --------------------------------------------------------------------------- #

def _suffixed(path: Path, counter: int) -> Path:
    return path.parent / f"{path.stem}_{counter}{path.suffix}"


def _claim(path: Path) -> bool:
    """Atomically create ``path`` as an empty file. False when it already exists.

    ``O_CREAT | O_EXCL`` is atomic on both Windows and POSIX, so two processes
    racing for the same name cannot both succeed.
    """
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno in (errno.EEXIST,):
            return False
        raise
    os.close(fd)
    return True


def claim_unique_path(path: Path, max_attempts: int = 10000) -> Path:
    """Atomically claim ``path`` (or the next free ``_2``, ``_3``, ... variant).

    Returns the claimed path, which exists as an empty placeholder owned by this
    process. The caller must overwrite it (see :func:`promote_atomically`) or
    remove it. Because the claim is atomic, two concurrent PDF Forge processes
    can never select the same final name.
    """
    path = Path(path)
    if _claim(path):
        return path
    for counter in range(2, max_attempts):
        candidate = _suffixed(path, counter)
        if _claim(candidate):
            return candidate
    raise PromotionError(f"Could not allocate a free name near '{path}'.")


@dataclass(frozen=True)
class OutputResult:
    """What a writer actually produced.

    ``path`` is the path on disk, which is NOT always the path that was
    configured: when the requested name appeared between configuration and
    execution, no-clobber promotion allocates a ``_2``/``_3`` sibling. Callers
    must use this value for validation, manifest recording, success messages,
    logs, created-file lists and statistics - a writer that reports the
    configured path can name a file it did not write.

    ``count`` is the operation's own measure (pages written, pages modified);
    ``stats`` carries anything else an operation needs to report.
    """

    path: Path
    count: int = 0
    stats: Mapping[str, object] = field(default_factory=dict)


def _discard(path: Path, description: str) -> None:
    """Remove one artifact we own, never raising in a cleanup path."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("Could not remove %s: %s", description, path)


def promote_atomically(tmp_path: Path, final_path: Path,
                       record: bool = True) -> Path:
    """Promote a validated temp file to its final name without clobbering.

    Returns the path actually written, which may carry a ``_2``/``_3`` suffix if
    the requested name was taken between configuration and execution. The
    temporary file is always consumed (moved or removed).

    ``record=True`` registers the result in the generated-output manifest, so
    folder tools skip it on a later run. Recording happens here - in the single
    place every PDF output is finalized - so no writer can forget it.
    """
    tmp_path, final_path = Path(tmp_path), Path(final_path)
    claimed = None
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        claimed = claim_unique_path(final_path)
        # The claim is an empty placeholder we own; replacing it is safe and
        # atomic. os.replace is correct *here* precisely because the target was
        # created by us microseconds earlier and nobody else can hold that name.
        os.replace(str(tmp_path), str(claimed))
    except Exception:
        # Clean up BOTH artifacts, independently: a failure to remove one must
        # not abandon the other. The claim is an empty placeholder wearing the
        # user's expected output name, so leaving it behind hands them a 0-byte
        # file and pushes every later run onto a _2 suffix. Only the placeholder
        # this call created is removed - never a pre-existing external file,
        # which claim_unique_path by construction never returns.
        _discard(tmp_path, "temporary file")
        if claimed is not None:
            _discard(claimed, "claimed output placeholder")
        raise
    if record:
        record_generated_output(claimed)
    if claimed != final_path:
        logger.info(
            "Destination '%s' appeared before promotion; wrote '%s' instead.",
            final_path.name, claimed.name,
        )
    return claimed


# --------------------------------------------------------------------------- #
# State store location
# --------------------------------------------------------------------------- #

_STATE_ENV = "PDF_FORGE_STATE_DIR"
_warning_shown = False


def state_dir() -> Path:
    """Writable per-user directory for machine-local state.

    Never the repository checkout: a checkout can be read-only, shared, or on
    removable media. ``PDF_FORGE_STATE_DIR`` overrides it (used by tests).
    """
    override = os.environ.get(_STATE_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "PDF Forge"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "pdf-forge"
    return Path.home() / ".local" / "state" / "pdf-forge"


def manifest_path() -> Path:
    return state_dir() / "generated-outputs.json"


# --------------------------------------------------------------------------- #
# Cross-process lock
# --------------------------------------------------------------------------- #

_HOST = platform.node()  # a PID only identifies a process on its own machine
_ALIVE_UNKNOWN = "?"  # the process exists, but its start time is unreadable


def _process_start(pid: int) -> Optional[str]:
    """Start-time identity of ``pid``, or ``None`` when no such process exists.

    A bare PID is not an identity: PIDs are recycled, so a dead lock owner's
    number may belong to an unrelated live process minutes later. Pairing it
    with the process creation time is what makes "provably gone" provable.

    Returns ``_ALIVE_UNKNOWN`` when the process is running but its start time
    cannot be read, so an unreadable owner is never mistaken for a dead one.
    """
    # sys.platform (not os.name) is the form a type checker narrows on, so the
    # Windows-only ctypes attributes below are not analysed when linting on
    # Linux - where ctypes.WinDLL genuinely does not exist.
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint32]
        handle = kernel32.OpenProcess(0x1000,  # QUERY_LIMITED_INFORMATION
                                      False, pid)
        if not handle:
            # 87 = ERROR_INVALID_PARAMETER: there is no process with that id.
            # Anything else (access denied, ...) means it exists but is opaque.
            return None if ctypes.get_last_error() == 87 else _ALIVE_UNKNOWN
        try:
            created = ctypes.c_ulonglong()
            spare = (ctypes.c_ulonglong * 3)()
            ok = kernel32.GetProcessTimes(
                ctypes.c_void_p(handle), ctypes.byref(created),
                ctypes.byref(spare, 0), ctypes.byref(spare, 8),
                ctypes.byref(spare, 16))
            return str(created.value) if ok else _ALIVE_UNKNOWN
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned by another user
    except OSError:
        return _ALIVE_UNKNOWN
    try:  # Linux: field 22 of /proc/<pid>/stat, past the parenthesised comm
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b")", 1)[1].split()[19].decode("ascii")
    except (OSError, IndexError):
        return _ALIVE_UNKNOWN  # no procfs (macOS/BSD): alive is all we know


class FileLock:
    """A simple cross-process advisory lock built on atomic file creation.

    Portable (Windows + POSIX) and good enough for the short manifest
    read-modify-write critical section.

    It fails CLOSED. ``__enter__`` either holds the lock or raises: contention
    that outlasts ``timeout`` raises :class:`LockTimeout`, unusable lock storage
    raises :class:`LockUnavailable`. It never hands back an unlocked lock,
    because a caller that then rewrites shared state has no mutual exclusion at
    all and silently loses concurrent writers' work.

    A lock abandoned by a killed process is recovered, but only when its owner
    is *provably* gone - the lock records the owner's PID, host and process
    start time, and all three are checked. ``stale_after`` merely delays that
    probe; an old timestamp is never on its own a reason to break a lock, since
    a long-running owner legitimately holds one for as long as it needs.
    """

    def __init__(self, path: Path, timeout: float = 10.0,
                 stale_after: float = 5.0):
        self.path = Path(path)
        self.timeout = timeout
        self.stale_after = stale_after
        self._fd: Optional[int] = None

    def __enter__(self) -> "FileLock":
        # The lock directory must exist before we can lock at all. Any failure
        # here (including a *file* sitting where the directory should be, which
        # raises FileExistsError) means locking is impossible - not that another
        # process holds the lock - so report that distinctly and immediately.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LockUnavailable(
                f"cannot create the lock directory '{self.path.parent}': {exc}"
            ) from exc

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                os.write(self._fd, json.dumps({
                    "pid": os.getpid(),
                    "host": _HOST,
                    "start": _process_start(os.getpid()),
                }).encode("utf-8"))
                return self
            except FileExistsError:
                # Genuine contention. Recover only from an owner we can prove is
                # gone, then retry - always honouring the deadline so this loop
                # can never spin forever.
                self._break_if_owner_is_gone()
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"another process held '{self.path.name}' for the whole "
                        f"{self.timeout:g}s wait"
                    ) from None
                time.sleep(0.02)
            except OSError as exc:
                # Includes a write that failed after the open succeeded: drop
                # the half-created lock rather than orphan it, since our own
                # process is alive and no stale-owner probe would ever clear it.
                self.__exit__()
                raise LockUnavailable(
                    f"cannot create the lock file '{self.path}': {exc}") from exc

    def _break_if_owner_is_gone(self) -> bool:
        """Remove a lock whose owner is provably dead. True when removed."""
        try:
            if time.time() - self.path.stat().st_mtime <= self.stale_after:
                return False  # too fresh to be worth probing the owner
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            pid, host = owner["pid"], owner["host"]
            recorded = owner["start"]
        except (OSError, ValueError, KeyError, TypeError):
            # Vanished, unreadable, or written by another version: we cannot
            # prove anything about its owner, so we must not break it.
            return False
        if host != _HOST or not isinstance(pid, int):
            return False  # a foreign machine's PID means nothing here
        current = _process_start(pid)
        if current is not None and (current == _ALIVE_UNKNOWN
                                    or current == recorded):
            return False  # still running, or running and unreadable
        # Re-read immediately before unlinking. Between the read above and here,
        # another waiter recovering the same dead lock could have unlinked it
        # and acquired a fresh one under the same path - unlinking now would
        # delete that live lock and put two writers in the critical section.
        # Windows' mandatory sharing blocks the unlink so this is a POSIX race,
        # but the guard is cheap and correct on both.
        try:
            confirm = json.loads(self.path.read_text(encoding="utf-8"))
            if (confirm.get("pid") != pid
                    or confirm.get("start") != recorded
                    or confirm.get("host") != host):
                return False  # a different owner now holds it; leave it alone
        except (OSError, ValueError, KeyError, TypeError):
            return False  # gone or unreadable: nothing safe to break
        try:
            self.path.unlink()
            logger.warning("Recovered a manifest lock left by dead process %d.",
                           pid)
            return True
        except OSError:
            return False

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            try:
                self.path.unlink()
            except OSError:
                pass
            self._fd = None


# --------------------------------------------------------------------------- #
# Generated-output manifest
# --------------------------------------------------------------------------- #

_MANIFEST_LIMIT = 5000


def _real_path(path) -> str:
    """The absolute path in its true case - always openable by os.stat."""
    return os.path.abspath(str(path))


def _normalized(path) -> str:
    """A case-folded key for *comparing* two paths on a case-insensitive OS.

    Only ever a comparison key: never store this and try to open it. On Windows
    ``str.lower()`` maps a few characters to a different length (the Turkish
    dotted capital I gains a combining dot), so a lowercased path can fail to
    name the file it came from - which silently dropped such an output from the
    manifest and made folder tools reprocess it. The stored path is
    :func:`_real_path`; this is compared against it after folding both.
    """
    key = _real_path(path)
    return key.lower() if os.name == "nt" else key


def _content_hash(path) -> Optional[str]:
    """SHA-256 of the whole file, or ``None`` when it cannot be read.

    Whole-file, not sampled: a sampled digest can only be trusted by falling
    back to the full hash whenever the samples match, which is exactly the
    common case (an untouched output), so sampling would buy nothing. The cost
    is paid once per generated output and again only for a candidate whose
    metadata already matches - never for files we do not believe are ours.
    """
    digest = hashlib.sha256()
    try:
        with open(str(path), "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def file_identity(path: Path, content: bool = True) -> Optional[dict]:
    """Strong identity for an output file, or ``None`` when unavailable.

    Records nanosecond mtime, size, the OS file id where the platform provides
    one, and a SHA-256 of the contents. Metadata alone is not identity: a
    same-size in-place rewrite keeps the inode, and a user (or a tool) can
    restore ``mtime_ns`` afterwards, leaving a file that is byte-for-byte
    different yet metadata-identical to our output. Only the content hash tells
    those apart, so the file stops being excluded from folder tools.

    ``content=False`` skips the hash for callers that only need the cheap
    metadata (see :func:`_matches`, which hashes lazily).
    """
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    identity = {
        # Real case, so os.stat / open on this stored value always finds the
        # file. Case-folding for comparison happens on read, not here.
        "path": _real_path(path),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }
    file_id = getattr(st, "st_ino", 0)
    if file_id:
        identity["file_id"] = int(file_id)
        identity["volume"] = int(getattr(st, "st_dev", 0))
    if content:
        digest = _content_hash(path)
        if digest is not None:
            identity["sha256"] = digest
    return identity


def _matches(entry: dict, current: dict) -> bool:
    """True when the on-disk file is still the exact output we recorded."""
    if entry.get("size") != current.get("size"):
        return False
    if "mtime_ns" in entry and "mtime_ns" in current:
        if entry["mtime_ns"] != current["mtime_ns"]:
            return False
    elif "mtime" in entry:  # legacy 1-second entry: migrate leniently
        if int(current.get("mtime_ns", 0) // 1_000_000_000) != int(entry["mtime"]):
            return False
    if "file_id" in entry and "file_id" in current:
        if entry["file_id"] != current["file_id"]:
            return False
    expected = entry.get("sha256")
    if expected is None:
        # MIGRATION RULE for entries written before version 3, which carry no
        # content identity. Metadata alone cannot distinguish our output from a
        # same-size in-place replacement whose timestamp was restored, so such
        # an entry is *retired* rather than trusted: the file becomes visible to
        # folder tools again and is re-recorded, with a hash, the next time we
        # generate it. The cost is that one folder run may reprocess outputs
        # created before the upgrade; the alternative - trusting them forever -
        # is exactly the defect, and would exclude a user's own file with no way
        # for them to get it back.
        return False
    # Only reached when every cheap check already passed, i.e. for a file we
    # still believe is ours; this is where a restored-timestamp replacement is
    # caught.
    return _content_hash(entry["path"]) == expected


def _read_entries() -> List[dict]:
    """Load manifest entries, recovering from a truncated or corrupt file.

    Corruption is never silently treated as "empty": the damaged file is kept
    as ``*.corrupt`` and a warning is logged so the loss is visible.
    """
    path = manifest_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Could not read the generated-output manifest: %s", exc)
        return []
    try:
        # Decoded here, not above: a UnicodeDecodeError is a ValueError, so
        # read_text() sent byte-level corruption straight past both handlers
        # and out to the caller - every folder tool then crashed, permanently,
        # because nothing quarantined the file. Inside this block it lands in
        # the corruption branch like any other damage.
        data = json.loads(raw.decode("utf-8"))
        entries = data["outputs"]
        if not isinstance(entries, list):
            raise ValueError("outputs is not a list")
        return [e for e in entries if isinstance(e, dict) and "path" in e]
    except (ValueError, KeyError, TypeError) as exc:
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            os.replace(str(path), str(backup))
        except OSError:
            pass
        logger.warning(
            "The generated-output manifest was corrupt (%s); kept a copy at "
            "'%s' and started a new one. Folder tools may reprocess outputs "
            "created before this point.", exc, backup.name,
        )
        return []


def _write_entries(entries: List[dict]) -> None:
    """Atomically persist manifest entries (temp file -> fsync -> replace)."""
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    payload = json.dumps({"version": 3, "outputs": entries[-_MANIFEST_LIMIT:]},
                         indent=1)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp), str(path))  # replacing our own state file is correct


def load_generated_outputs() -> Set[str]:
    """Case-folded keys of outputs this application created and still owns.

    Folded here, on the real-case stored path, so callers can test membership
    with :func:`_normalized` on a candidate. The stored value stays real-case
    (see :func:`file_identity`) so file_identity below can actually stat it.
    """
    live: Set[str] = set()
    for entry in _read_entries():
        # Cheap metadata first; _matches hashes only if everything else agrees.
        current = file_identity(Path(entry["path"]), content=False)
        if current is not None and _matches(entry, current):
            live.add(_normalized(entry["path"]))
    return live


def _warn_tracking_unavailable(reason: str) -> None:
    """Tell the user once - and truthfully - that tracking is degraded."""
    global _warning_shown
    if _warning_shown:
        return
    _warning_shown = True
    logger.warning(
        "Generated-output tracking is unavailable (%s). This run's output was "
        "written normally, but it was not recorded, so a repeated folder run "
        "may reprocess its own output.", reason)


def record_generated_output(path: Path) -> None:
    """Record ``path`` as an application-generated output.

    Locked read-modify-write so concurrent PDF Forge processes merge instead of
    losing each other's entries. The lock fails closed, so when it cannot be
    held this returns having changed *nothing* - an unlocked read-modify-write
    would silently drop whatever a concurrent writer recorded.

    Never raises: bookkeeping must not fail a completed user operation.
    """
    try:
        identity = file_identity(path)
        if identity is None:
            return
        with FileLock(manifest_path().with_suffix(".lock")):
            # Fold both sides: the stored paths are real-case now, so a rewrite
            # of the same file under a different case must still replace, not
            # duplicate, the existing entry.
            key = _normalized(identity["path"])
            entries = [e for e in _read_entries()
                       if _normalized(e["path"]) != key]
            entries.append(identity)
            _write_entries(entries)
    except LockTimeout as exc:
        # Transient: another PDF Forge process is busy. Nothing was written.
        _warn_tracking_unavailable(
            f"another PDF Forge process is using it and did not finish in time: {exc}")
        logger.debug("Could not record generated output '%s': %s", path, exc)
    except LockUnavailable as exc:
        # Permanent: the state directory itself cannot hold a lock.
        _warn_tracking_unavailable(f"its lock cannot be stored: {exc}")
        logger.debug("Could not record generated output '%s': %s", path, exc)
    except Exception as exc:  # noqa: BLE001 - never fail a write over bookkeeping
        _warn_tracking_unavailable(str(exc))
        logger.debug("Could not record generated output '%s': %s", path, exc)


def forget_generated_outputs() -> None:
    """Clear the manifest (used by tests and a clean/reset action)."""
    for candidate in (manifest_path(),
                      manifest_path().with_suffix(".lock"),
                      manifest_path().with_suffix(
                          manifest_path().suffix + ".corrupt")):
        try:
            candidate.unlink()
        except OSError:
            pass
