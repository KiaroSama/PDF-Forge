# -*- coding: utf-8 -*-
"""Regression tests for the generated-output state store.

C-04 - the manifest lock must fail CLOSED. A writer that cannot hold the lock
never performs the read-modify-write, says so once and truthfully, distinguishes
contention from unusable lock storage, and never breaks a lock whose owner is
still alive.

C-05 - a manifest entry must carry content identity, so a same-path, same-size,
same-inode replacement whose timestamp was restored is recognised as a user file
again instead of staying excluded from folder tools forever.
"""

import gc
import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_VENV = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = str(_VENV) if _VENV.exists() else sys.executable

_PRELUDE = textwrap.dedent(f"""
    import os, sys, time
    sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import pdf_forge as app
""")


def _child(body: str, *args: str) -> subprocess.Popen:
    """Run ``body`` in a real separate interpreter, inheriting the state dir."""
    return subprocess.Popen(
        [PYTHON, "-c", _PRELUDE + textwrap.dedent(body), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _shorten_lock_timeout(monkeypatch, timeout: float = 0.2) -> None:
    """Make the lock give up quickly so contention tests stay fast."""
    original = app.safeio.FileLock.__init__

    def patched(self, path, timeout=timeout, **kw):
        original(self, path, timeout, **kw)

    monkeypatch.setattr(app.safeio.FileLock, "__init__", patched)


def _manifest_bytes() -> bytes:
    try:
        return app.manifest_path().read_bytes()
    except OSError:
        return b""


def _seed(tmp_path: Path, name: str = "seed.pdf") -> Path:
    target = tmp_path / name
    target.write_bytes(b"%PDF-1.7 seed\n")
    app.record_generated_output(target)
    return target


def _lock_path() -> Path:
    return app.manifest_path().with_suffix(".lock")


# --------------------------------------------------------------------------- #
# C-04 - the manifest lock fails closed
# --------------------------------------------------------------------------- #

def test_held_lock_past_timeout_does_not_write_unlocked(tmp_path, monkeypatch):
    """The defect: a timed-out writer used to do its read-modify-write anyway."""
    _seed(tmp_path)
    holder = app.safeio.FileLock(_lock_path(), timeout=60)
    holder.__enter__()
    try:
        before = _manifest_bytes()
        _shorten_lock_timeout(monkeypatch, 0.2)
        victim = tmp_path / "victim.pdf"
        victim.write_bytes(b"%PDF-1.7 victim\n")
        app.record_generated_output(victim)  # bookkeeping must never raise
        assert _manifest_bytes() == before, "manifest was mutated without the lock"

        # And a real promotion must still hand the user their finished file:
        # failed bookkeeping may not break a completed output.
        staged = tmp_path / "staged.tmp"
        staged.write_bytes(b"%PDF-1.7 promoted\n")
        written = app.promote_atomically(staged, tmp_path / "promoted.pdf")
        assert written.read_bytes() == b"%PDF-1.7 promoted\n"
        assert _manifest_bytes() == before, "manifest was mutated without the lock"
    finally:
        holder.__exit__()


def test_no_lost_updates_under_real_process_concurrency(tmp_path):
    """Six real processes recording 30 distinct outputs: every entry survives.

    The lock timeout is squeezed so contention is guaranteed rather than
    theoretical; a writer that reacts to contention by writing unlocked loses
    the entries its peers wrote between its own read and its own write.
    """
    groups = []
    for worker in range(6):
        group = []
        for index in range(5):
            target = tmp_path / f"conc{worker}_{index}.pdf"
            target.write_bytes(b"%PDF-1.7 " + target.name.encode() + b"\n")
            group.append(target)
        groups.append(group)

    go = tmp_path / "go"
    body = """
        original = app.safeio.FileLock.__init__

        def patched(self, path, timeout=0.005, **kw):
            original(self, path, timeout, **kw)

        app.safeio.FileLock.__init__ = patched

        go = Path(sys.argv[1])
        mine = [Path(raw) for raw in sys.argv[2:]]
        while not go.exists():          # start together, so they really collide
            time.sleep(0.005)

        for _ in range(40):             # hammer the lock: guarantee contention
            for target in mine:
                app.record_generated_output(target)

        for target in mine:             # then make sure our own work landed
            key = app.safeio._normalized(target)
            for _ in range(400):
                if key in app.load_generated_outputs():
                    break
                app.record_generated_output(target)
                time.sleep(0.003)
            else:
                sys.exit("never recorded " + str(target))
    """
    procs = [_child(body, str(go), *[str(t) for t in group]) for group in groups]
    time.sleep(1.5)  # let every child finish importing before the barrier drops
    go.write_text("go", encoding="utf-8")
    for proc in procs:
        _out, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, err

    recorded = app.load_generated_outputs()
    missing = [t.name for group in groups for t in group
               if app.safeio._normalized(t) not in recorded]
    assert not missing, f"lost entries: {missing}"


def test_unusable_lock_storage_raises_promptly_and_leaves_no_lock(tmp_path):
    """A file where the lock's directory belongs is not contention."""
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    lock = blocker / "sub" / "m.lock"

    started = time.monotonic()
    with pytest.raises(OSError) as caught:
        with app.safeio.FileLock(lock, timeout=30):
            pass
    assert type(caught.value).__name__ == "LockUnavailable", caught.value
    assert time.monotonic() - started < 5, "must not burn the whole timeout"
    assert not lock.exists()


def test_stale_lock_of_a_provably_dead_owner_is_recovered(tmp_path):
    """A killed owner must not wedge the store - and must be *provably* dead."""
    lock = tmp_path / "dead.lock"
    proc = _child("""
        app.safeio.FileLock(Path(sys.argv[1]), timeout=10).__enter__()
        os._exit(0)  # killed owner: never releases the lock
    """, str(lock))
    _out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err
    del proc
    gc.collect()  # drop the OS process handle so the PID is truly released

    assert lock.exists(), "the dead owner must have left its lock behind"
    owner = json.loads(lock.read_text(encoding="utf-8"))
    assert owner["pid"] and owner["start"], \
        "the lock must record owner PID *and* a process-start identity"

    time.sleep(0.2)
    with app.safeio.FileLock(lock, timeout=10, stale_after=0.1):
        pass  # recovered


def test_a_live_long_running_owner_is_never_treated_as_stale(tmp_path):
    """The defect: an old mtime used to be enough to break a live owner's lock."""
    lock = tmp_path / "live.lock"
    proc = _child("""
        with app.safeio.FileLock(Path(sys.argv[1]), timeout=30):
            print("held", flush=True)
            time.sleep(20)
    """, str(lock))
    try:
        assert proc.stdout.readline().strip() == "held", proc.stderr.read()
        time.sleep(0.4)  # far past the stale_after used below

        with pytest.raises(OSError) as caught:
            with app.safeio.FileLock(lock, timeout=0.5, stale_after=0.05):
                pass
        assert type(caught.value).__name__ == "LockTimeout", caught.value
        assert lock.exists(), "a live owner's lock must never be broken"
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_contention_warning_appears_exactly_once_and_is_truthful(
        tmp_path, monkeypatch, caplog):
    _seed(tmp_path)
    monkeypatch.setattr(app.safeio, "_warning_shown", False, raising=False)
    holder = app.safeio.FileLock(_lock_path(), timeout=60)
    holder.__enter__()
    try:
        _shorten_lock_timeout(monkeypatch, 0.2)
        caplog.set_level(logging.WARNING, logger="pdf_forge")
        for index in range(3):
            target = tmp_path / f"warn{index}.pdf"
            target.write_bytes(b"%PDF-1.7 w\n")
            app.record_generated_output(target)
    finally:
        holder.__exit__()

    said = [r.getMessage() for r in caplog.records
            if "tracking is unavailable" in r.getMessage()]
    assert len(said) == 1, said
    assert "another PDF Forge process" in said[0], said[0]
    assert "may reprocess its own output" in said[0], said[0]


def test_unusable_storage_is_reported_differently_from_contention(
        tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(app.safeio, "manifest_path",
                        lambda: blocker / "generated-outputs.json")
    monkeypatch.setattr(app.safeio, "_warning_shown", False, raising=False)
    caplog.set_level(logging.WARNING, logger="pdf_forge")

    target = tmp_path / "s.pdf"
    target.write_bytes(b"%PDF-1.7 s\n")
    app.record_generated_output(target)

    said = [r.getMessage() for r in caplog.records
            if "tracking is unavailable" in r.getMessage()]
    assert len(said) == 1, said
    assert "cannot be stored" in said[0], said[0]
    assert "another PDF Forge process" not in said[0], said[0]


# --------------------------------------------------------------------------- #
# C-05 - generated-output identity includes content identity
# --------------------------------------------------------------------------- #

def _promote(tmp_path: Path, payload: bytes, name: str = "gen.pdf") -> Path:
    staged = tmp_path / "staged.tmp"
    staged.write_bytes(payload)
    return app.promote_atomically(staged, tmp_path / name)


def test_same_inode_same_size_restored_mtime_replacement_is_detected(tmp_path):
    """Metadata identity alone cannot see this; content identity must."""
    out = _promote(tmp_path, b"AAAAAAAA")
    before = os.stat(out)
    assert app.safeio._normalized(out) in app.load_generated_outputs()

    with open(out, "r+b") as handle:  # in place: keeps the inode and the size
        handle.write(b"BBBBBBBB")
    os.utime(out, ns=(before.st_atime_ns, before.st_mtime_ns))

    after = os.stat(out)
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    if before.st_ino:
        assert after.st_ino == before.st_ino, "test needs an in-place rewrite"
    assert app.safeio._normalized(out) not in app.load_generated_outputs(), \
        "a replaced file must be treated as a user file again"


def test_copied_timestamp_replacement_is_detected(tmp_path):
    """The user copies another file's bytes in and restores our timestamps."""
    import shutil

    out = _promote(tmp_path, b"ORIGINAL")
    stash = tmp_path / "stash.stat"
    stash.write_bytes(b"x")
    shutil.copystat(out, stash)  # remember our timestamps

    with open(out, "r+b") as handle:
        handle.write(b"REPLACED")  # same length
    shutil.copystat(stash, out)  # put them back

    assert app.safeio._normalized(out) not in app.load_generated_outputs(), \
        "a copied-timestamp replacement must not stay excluded"


def test_untouched_generated_output_is_still_recognised(tmp_path):
    out = _promote(tmp_path, b"%PDF-1.7 untouched\n")
    for _ in range(3):
        assert app.safeio._normalized(out) in app.load_generated_outputs()
        assert out.name not in [p.name for p in app.discover_pdfs_in_folder(tmp_path)]


def test_version_2_entry_without_a_content_hash_is_not_trusted(tmp_path):
    """Migration: a pre-content-identity entry must not exclude a file forever."""
    target = tmp_path / "legacy.pdf"
    target.write_bytes(b"%PDF-1.7 legacy\n")
    st = os.stat(target)
    entry = {
        "path": app.safeio._normalized(target),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }
    file_id = getattr(st, "st_ino", 0)
    if file_id:
        entry["file_id"] = int(file_id)
        entry["volume"] = int(getattr(st, "st_dev", 0))

    app.manifest_path().parent.mkdir(parents=True, exist_ok=True)
    app.manifest_path().write_text(
        json.dumps({"version": 2, "outputs": [entry]}), encoding="utf-8")

    assert app.safeio._normalized(target) not in app.load_generated_outputs(), \
        "an entry with no content identity must be retired, not trusted forever"
