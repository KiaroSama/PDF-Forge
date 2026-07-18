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
import json
import os
import time
from pathlib import Path
from typing import List, Optional, Set

from .constants import *  # noqa: F401,F403

__all__ = [
    'PromotionError', 'promote_atomically', 'claim_unique_path',
    'state_dir', 'manifest_path', 'file_identity', 'FileLock',
    'load_generated_outputs', 'record_generated_output',
    'forget_generated_outputs', 'state_store_warning',
]


class PromotionError(OSError):
    """Raised when a validated output cannot be promoted to a final name."""


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
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        claimed = claim_unique_path(final_path)
        # The claim is an empty placeholder we own; replacing it is safe and
        # atomic. os.replace is correct *here* precisely because the target was
        # created by us microseconds earlier and nobody else can hold that name.
        os.replace(str(tmp_path), str(claimed))
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            logger.warning("Could not remove temporary file: %s", tmp_path)
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


def state_store_warning() -> Optional[str]:
    """A user-visible warning when state cannot be persisted, else ``None``."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        probe = state_dir() / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return None
    except OSError as exc:
        return (
            f"Generated-output tracking is unavailable ({exc.strerror or exc}). "
            "Folder tools cannot skip files this application created earlier, so "
            "a repeated folder run may reprocess its own output."
        )


# --------------------------------------------------------------------------- #
# Cross-process lock
# --------------------------------------------------------------------------- #

class FileLock:
    """A simple cross-process advisory lock built on atomic file creation.

    Portable (Windows + POSIX) and good enough for the short manifest
    read-modify-write critical section. A stale lock left by a killed process is
    broken after ``stale_after`` seconds so the store can never wedge.
    """

    def __init__(self, path: Path, timeout: float = 10.0,
                 stale_after: float = 60.0):
        self.path = Path(path)
        self.timeout = timeout
        self.stale_after = stale_after
        self._fd: Optional[int] = None

    def __enter__(self) -> "FileLock":
        # The lock directory must exist before we can lock at all. Any failure
        # here (including a *file* sitting where the directory should be, which
        # raises FileExistsError) means locking is impossible - not that another
        # process holds the lock - so give up immediately and run unlocked.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._fd = None
            return self

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                os.write(self._fd, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                # Genuine contention. Break a lock abandoned by a killed
                # process, then retry - but always honour the deadline so this
                # loop can never spin forever.
                self._break_if_stale()
                if time.monotonic() >= deadline:
                    logger.warning("Manifest lock timed out; continuing unlocked.")
                    self._fd = None
                    return self
                time.sleep(0.02)
            except OSError:
                self._fd = None
                return self

    def _break_if_stale(self) -> bool:
        """Remove a lock left behind by a dead process. True when removed."""
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            # Vanished between the failed open and now: nothing to break.
            return False
        if age <= self.stale_after:
            return False
        try:
            self.path.unlink()
            logger.warning("Removed a stale manifest lock (%.0fs old).", age)
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


# --------------------------------------------------------------------------- #
# Generated-output manifest
# --------------------------------------------------------------------------- #

_MANIFEST_LIMIT = 5000


def _normalized(path) -> str:
    key = os.path.abspath(str(path))
    return key.lower() if os.name == "nt" else key


def file_identity(path: Path) -> Optional[dict]:
    """Strong identity for an output file, or ``None`` when unavailable.

    Records nanosecond mtime, size, and the OS file id where the platform
    provides one. A user replacing the file at the same path - even with the
    same size within the same second - changes ``mtime_ns`` (and usually the
    file id), so the replacement is correctly treated as a *user* file again.
    """
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    identity = {
        "path": _normalized(path),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }
    file_id = getattr(st, "st_ino", 0)
    if file_id:
        identity["file_id"] = int(file_id)
        identity["volume"] = int(getattr(st, "st_dev", 0))
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
    return True


def _read_entries() -> List[dict]:
    """Load manifest entries, recovering from a truncated or corrupt file.

    Corruption is never silently treated as "empty": the damaged file is kept
    as ``*.corrupt`` and a warning is logged so the loss is visible.
    """
    path = manifest_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Could not read the generated-output manifest: %s", exc)
        return []
    try:
        data = json.loads(raw)
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
    payload = json.dumps({"version": 2, "outputs": entries[-_MANIFEST_LIMIT:]},
                         indent=1)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp), str(path))  # replacing our own state file is correct


def load_generated_outputs() -> Set[str]:
    """Normalized paths of outputs this application created and still owns."""
    live: Set[str] = set()
    for entry in _read_entries():
        current = file_identity(Path(entry["path"]))
        if current is not None and _matches(entry, current):
            live.add(entry["path"])
    return live


def record_generated_output(path: Path) -> None:
    """Record ``path`` as an application-generated output.

    Locked read-modify-write so concurrent PDF Forge processes merge instead of
    losing each other's entries. Never raises: bookkeeping must not fail a
    completed user operation.
    """
    try:
        identity = file_identity(path)
        if identity is None:
            return
        with FileLock(manifest_path().with_suffix(".lock")):
            entries = [e for e in _read_entries() if e["path"] != identity["path"]]
            entries.append(identity)
            _write_entries(entries)
    except Exception as exc:  # noqa: BLE001 - never fail a write over bookkeeping
        global _warning_shown
        if not _warning_shown:
            _warning_shown = True
            logger.warning("Generated-output tracking unavailable: %s", exc)
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
