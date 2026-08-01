# -*- coding: utf-8 -*-
"""Regression tests for race-safe output promotion and the state store.

Covers PF-003 (promotion must never clobber a destination that appeared after
configuration), PF-009 (every promoted PDF is tracked), PF-027 (atomic, locked,
corruption-aware manifest), PF-028 (state lives outside the checkout and
degrades visibly), and PF-046 (strong file identity).
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
import pymupdf  # noqa: E402


def make_pdf(path: Path, pages: int = 2) -> Path:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()
    return path


def write_tmp(path: Path, data: bytes = b"%PDF-1.7 new output\n") -> Path:
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------- #
# N-04 - the POSIX manifest lock uses fcntl.flock: a lock left by a dead process
# is free at once (no stale-file read-confirm-unlink race), yet it stays
# mutually exclusive between live holders.
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl.flock acquisition path")
def test_posix_lock_recovers_a_dead_owner_and_stays_exclusive(tmp_path):
    from pdf_forge.safeio import FileLock, LockTimeout

    lock_path = tmp_path / "manifest.lock"
    # A lock file left by a DEAD process: content present, but nothing holds an
    # flock on it. A fresh mtime would make the old read-confirm-unlink scheme
    # wait out stale_after and then time out; flock acquires it immediately
    # because the kernel already released the dead owner's lock.
    lock_path.write_text('{"pid": 999999, "host": "gone", "start": "0"}',
                         encoding="utf-8")

    with FileLock(lock_path, timeout=1.0):
        # Held now: a second acquirer (a separate open file description) must be
        # excluded and time out - proving mutual exclusion still holds.
        with pytest.raises(LockTimeout):
            with FileLock(lock_path, timeout=0.2):
                pass

    # Released cleanly: it can be taken again.
    with FileLock(lock_path, timeout=1.0):
        pass


# --------------------------------------------------------------------------- #
# PF-003 - promotion must never overwrite
# --------------------------------------------------------------------------- #

def test_promotion_never_overwrites_a_destination_created_after_configuration(tmp_path):
    """The exact reported race: destination appears between config and promotion."""
    final = tmp_path / "out.pdf"
    original = b"USER DATA THAT MUST SURVIVE"
    final.write_bytes(original)  # another process created it after configuration

    tmp = write_tmp(tmp_path / "staged.tmp")
    written = app.promote_atomically(tmp, final)

    assert written != final, "must not select the taken name"
    assert final.read_bytes() == original, "external file was overwritten"
    assert written.read_bytes() == b"%PDF-1.7 new output\n"
    assert not tmp.exists(), "temporary file must be consumed"


def test_promotion_leaves_no_temp_or_lock_files(tmp_path):
    final = tmp_path / "out.pdf"
    app.promote_atomically(write_tmp(tmp_path / "a.tmp"), final)
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.suffix in (".tmp", ".lock") or ".tmp" in p.name]
    assert leftovers == [], f"leftover files: {leftovers}"


def test_promotion_cleans_temp_on_failure(tmp_path):
    tmp = write_tmp(tmp_path / "b.tmp")
    # A *file* where the parent directory should be makes mkdir fail, so the
    # promotion cannot proceed.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    with pytest.raises(OSError):
        app.promote_atomically(tmp, blocker / "nested" / "y.pdf")
    assert not tmp.exists(), "temp file must be removed on failure"


# --------------------------------------------------------------------------- #
# OW-1 - promotion can replace a destination the user explicitly approved, and
# only then. The default stays no-clobber, and a failed overwrite must leave the
# user's existing file exactly as it was.
# --------------------------------------------------------------------------- #

def test_promote_without_overwrite_still_never_clobbers(tmp_path):
    """The default path is unchanged: an occupied name yields a _2 sibling."""
    final = tmp_path / "out.pdf"
    original = b"USER DATA THAT MUST SURVIVE"
    final.write_bytes(original)

    tmp = write_tmp(tmp_path / "staged.tmp")
    written = app.promote_atomically(tmp, final)

    assert written == tmp_path / "out_2.pdf", "default must allocate a suffix"
    assert final.read_bytes() == original, "default path overwrote the original"
    assert written.read_bytes() == b"%PDF-1.7 new output\n"
    assert not tmp.exists(), "temporary file must be consumed"


def test_promote_with_overwrite_replaces_the_destination(tmp_path):
    """Approved overwrite lands on the exact name and replaces its contents."""
    final = tmp_path / "out.pdf"
    final.write_bytes(b"OLD CONTENT THE USER ASKED US TO REPLACE")

    tmp = write_tmp(tmp_path / "staged.tmp")
    written = app.promote_atomically(tmp, final, overwrite=True)

    assert written == final, "overwrite must not allocate a _2 suffix"
    assert written.read_bytes() == b"%PDF-1.7 new output\n"
    assert not (tmp_path / "out_2.pdf").exists(), "no sibling may be created"
    assert not tmp.exists(), "temporary file must be consumed"


def test_overwrite_refuses_a_directory(tmp_path):
    """A directory destination is never replaceable, approved or not."""
    target = tmp_path / "out.pdf"
    target.mkdir()
    (target / "keep.txt").write_text("user data inside", encoding="utf-8")

    tmp = write_tmp(tmp_path / "staged.tmp")
    with pytest.raises(IsADirectoryError):
        app.promote_atomically(tmp, target, overwrite=True)

    assert target.is_dir(), "the directory must survive"
    assert (target / "keep.txt").read_text(encoding="utf-8") == "user data inside"
    assert not tmp.exists(), "temporary file must be consumed"


def test_overwrite_failure_leaves_the_original_intact(tmp_path, monkeypatch):
    """A failed replace must not cost the user the file they already had.

    The destination is theirs, not a placeholder this call created, so the
    cleanup path must never discard it (OW-1).
    """
    final = tmp_path / "out.pdf"
    original = b"THE ONLY COPY THE USER HAS"
    final.write_bytes(original)

    real_replace = os.replace

    def boom(src, dst, *args, **kwargs):
        # Scoped to this destination only: os.replace is process-global, and
        # breaking every caller would take the manifest writer down with it.
        if Path(dst) == final:
            raise OSError("replace failed midway")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", boom)

    tmp = write_tmp(tmp_path / "staged.tmp")
    with pytest.raises(OSError):
        app.promote_atomically(tmp, final, overwrite=True)

    assert final.exists(), "a failed overwrite deleted the user's file"
    assert final.read_bytes() == original, "the original bytes must be intact"
    assert not tmp.exists(), "temporary file must still be consumed"


def test_overwrite_still_records_the_generated_output(tmp_path):
    """Bookkeeping is not skipped just because the name already existed."""
    make_pdf(tmp_path / "src.pdf", 1)
    final = tmp_path / "out.pdf"
    make_pdf(final, 1)  # a real PDF, so folder discovery would otherwise see it

    app.promote_atomically(write_tmp(tmp_path / "staged.tmp"), final,
                           overwrite=True)

    discovered = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
    assert "src.pdf" in discovered
    assert final.name not in discovered, "overwritten output must be recorded"


def _lock_outcome(lock, wait: float = 15.0):
    """Enter ``lock`` on a worker thread; return 'acquired' or the error name.

    A worker thread keeps a regression in the retry loop from hanging the whole
    suite instead of failing this one test.
    """
    import threading

    done = threading.Event()
    result = []

    def run():
        try:
            with lock:
                result.append("acquired")
        except OSError as exc:
            result.append(type(exc).__name__)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(wait), "FileLock never returned"
    return result[0]


def test_file_lock_reports_unusable_storage_instead_of_spinning(tmp_path):
    """A lock path that can never be created must fail fast and fail CLOSED.

    The lock directory sits under a plain file, so mkdir raises FileExistsError.
    That is 'locking is impossible', not 'someone holds the lock' - treating it
    as contention previously spun forever. Giving up is right; giving back an
    *unlocked* lock is not, because the caller then rewrites the manifest with
    no mutual exclusion at all.
    """
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    lock = app.safeio.FileLock(blocker / "sub" / "m.lock", timeout=30)
    assert _lock_outcome(lock, wait=10) == "LockUnavailable"


def test_contended_lock_fails_closed_when_its_timeout_expires(tmp_path):
    """A held lock must raise once its bounded wait expires.

    It must never block forever *and* never continue unlocked: the caller's
    read-modify-write is only safe while the lock is genuinely held.
    """
    import time as _time

    lock_path = tmp_path / "held.lock"
    holder = app.safeio.FileLock(lock_path, timeout=30)
    holder.__enter__()  # keep it held for the duration of the test
    try:
        started = _time.monotonic()
        assert _lock_outcome(app.safeio.FileLock(lock_path, timeout=0.5)) == \
            "LockTimeout"
        assert _time.monotonic() - started < 15
        assert lock_path.exists(), "the holder's lock must survive"
    finally:
        holder.__exit__()


def test_claim_is_exclusive(tmp_path):
    target = tmp_path / "claim.pdf"
    first = app.claim_unique_path(target)
    second = app.claim_unique_path(target)
    assert first == target and second != target
    assert first.exists() and second.exists()


def test_concurrent_processes_cannot_choose_the_same_final_path(tmp_path):
    """Real multi-process proof: N processes racing for one name all differ."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from pathlib import Path
        import pdf_forge as app
        print(app.claim_unique_path(Path({str(tmp_path / "race.pdf")!r})))
    """)
    procs = [
        subprocess.Popen([sys.executable, "-c", script],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    claimed = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        claimed.append(out.strip())
    assert len(set(claimed)) == len(claimed), f"two processes claimed the same path: {claimed}"


# --------------------------------------------------------------------------- #
# PF-009 - every promoted PDF is tracked
# --------------------------------------------------------------------------- #

def test_image_only_pdf_writer_records_its_output(tmp_path):
    """PF-009: call the REAL writer, then prove folder discovery excludes it."""
    src = make_pdf(tmp_path / "src.pdf", 2)
    doc = app.open_source_pdf(src)
    try:
        out = tmp_path / "src_image.pdf"
        app.render_pdf_to_image_pdf(doc, 2, out, dpi=40)
    finally:
        app.close_doc(doc)

    assert out.exists()
    discovered = [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
    assert "src.pdf" in discovered
    assert out.name not in discovered, "image-only output must not be reprocessed"


def test_promotion_failure_records_nothing(tmp_path):
    before = set(app.load_generated_outputs())
    tmp = write_tmp(tmp_path / "c.tmp")
    with pytest.raises((OSError, ValueError)):
        app.promote_atomically(tmp, tmp_path / "no" / "such" / "dir" / "\0bad.pdf")
    assert set(app.load_generated_outputs()) == before


def test_deleted_output_becomes_discoverable_again(tmp_path):
    make_pdf(tmp_path / "s.pdf", 1)
    out = app.promote_atomically(write_tmp(tmp_path / "d.tmp"), tmp_path / "gen.pdf")
    assert out.name not in [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
    out.unlink()
    make_pdf(out, 1)  # user puts their own file at that path
    assert out.name in [p.name for p in app.discover_pdfs_in_folder(tmp_path)]


# --------------------------------------------------------------------------- #
# PF-046 - strong identity
# --------------------------------------------------------------------------- #

def test_same_size_replacement_within_one_second_is_detected(tmp_path):
    """Weak size+1s-mtime identity would wrongly keep excluding the new file."""
    out = app.promote_atomically(write_tmp(tmp_path / "e.tmp", b"AAAA"),
                                 tmp_path / "gen.pdf")
    assert out.name not in [p.name for p in app.discover_pdfs_in_folder(tmp_path)]
    # Replace immediately with identical byte count (same second).
    out.write_bytes(b"BBBB")
    assert out.name in [p.name for p in app.discover_pdfs_in_folder(tmp_path)], \
        "a user replacement must be treated as a user file again"


def test_untouched_output_stays_excluded(tmp_path):
    out = app.promote_atomically(write_tmp(tmp_path / "f.tmp"), tmp_path / "gen.pdf")
    for _ in range(3):
        assert out.name not in [p.name for p in app.discover_pdfs_in_folder(tmp_path)]


def test_identity_includes_mtime_ns(tmp_path):
    target = make_pdf(tmp_path / "id.pdf", 1)
    identity = app.file_identity(target)
    assert "mtime_ns" in identity and identity["size"] > 0


# --------------------------------------------------------------------------- #
# PF-027 - atomic, locked, corruption-aware manifest
# --------------------------------------------------------------------------- #

def test_corrupt_manifest_is_preserved_not_silently_emptied(tmp_path):
    app.promote_atomically(write_tmp(tmp_path / "g.tmp"), tmp_path / "gen.pdf")
    manifest = app.manifest_path()
    assert manifest.exists()
    manifest.write_text('{"outputs": [{"path": "x"', encoding="utf-8")  # truncated
    assert app.load_generated_outputs() == set()
    backup = manifest.with_suffix(manifest.suffix + ".corrupt")
    assert backup.exists(), "corrupt manifest must be kept, not silently dropped"


def test_duplicate_record_is_idempotent(tmp_path):
    out = make_pdf(tmp_path / "dup.pdf", 1)
    for _ in range(5):
        app.record_generated_output(out)
    entries = json.loads(app.manifest_path().read_text(encoding="utf-8"))["outputs"]
    keys = [e["path"] for e in entries]
    assert len(keys) == len(set(keys)) == 1


def test_concurrent_writers_do_not_lose_entries(tmp_path):
    """Separate processes recording different outputs must all survive."""
    targets = [make_pdf(tmp_path / f"c{i}.pdf", 1) for i in range(5)]
    script = textwrap.dedent(f"""
        import sys, os
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        os.environ["PDF_FORGE_STATE_DIR"] = {str(app.state_dir())!r}
        from pathlib import Path
        import pdf_forge as app
        app.record_generated_output(Path(sys.argv[1]))
    """)
    procs = [(t, subprocess.Popen([sys.executable, "-c", script, str(t)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True))
             for t in targets]
    said = {}
    for target, proc in procs:
        _o, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        said[target] = err

    recorded = app.load_generated_outputs()
    for target in targets:
        if app.safeio._normalized(target) in recorded:
            continue
        # An entry may be absent only when that writer *said* so. Recording is
        # deliberately best-effort - record_generated_output never raises,
        # because a busy manifest lock must not fail a PDF that was written
        # correctly - so under contention it can warn and record nothing. What
        # must never happen is losing an entry silently, and that is what this
        # asserts. The writer's own stderr is quoted, so a failure here names
        # the reason instead of being an unexplained flake.
        assert "tracking is unavailable" in said[target], (
            f"lost entry for {target.name} with no warning to the user; "
            f"that writer's output was:\n{said[target] or '(nothing)'}"
        )


def test_interrupted_write_leaves_previous_manifest_readable(tmp_path):
    out = make_pdf(tmp_path / "keep.pdf", 1)
    app.record_generated_output(out)
    good = app.manifest_path().read_text(encoding="utf-8")
    # A crashed writer leaves its temp behind; the real manifest must be intact.
    stray = app.manifest_path().with_suffix(".json.99999.tmp")
    stray.write_text("garbage", encoding="utf-8")
    assert app.manifest_path().read_text(encoding="utf-8") == good
    assert app.load_generated_outputs()


# --------------------------------------------------------------------------- #
# PF-028 - state is project-local (portable) but never committed, and degrades
# visibly when the project cannot be written
# --------------------------------------------------------------------------- #

def test_state_lives_in_the_project_but_is_gitignored(monkeypatch):
    # Clear the override so the real default is exercised (with the env var set
    # the assertion would measure the env var, not the default it checks).
    monkeypatch.delenv("PDF_FORGE_STATE_DIR", raising=False)
    monkeypatch.setattr(app.safeio, "_project_state_ok", None, raising=False)

    checkout = Path(app.__file__).resolve().parent.parent
    store = app.state_dir().resolve()
    # Inside the project, so a portable copy carries its own state.
    assert checkout == store or checkout in store.parents, (
        "state must live inside the project folder for a portable checkout"
    )
    # ...but git-ignored, so it is never committed. (This is what actually
    # protects the repository, replacing the old "outside the checkout" rule.)
    import subprocess
    result = subprocess.run(
        ["git", "check-ignore", str(store)],
        capture_output=True, text=True, cwd=str(checkout), timeout=60,
    )
    assert result.returncode == 0, (
        f"the state directory {store} is NOT git-ignored; it could be committed"
    )


def test_state_falls_back_off_the_project_when_it_is_read_only(monkeypatch,
                                                               tmp_path):
    """A read-only/shared/removable checkout must still work, off-project."""
    monkeypatch.delenv("PDF_FORGE_STATE_DIR", raising=False)
    # Force the project-local location to look unwritable, and give the per-user
    # fallback a definite home.
    monkeypatch.setattr(app.safeio, "_project_state_ok", None, raising=False)
    monkeypatch.setattr(app.safeio, "_is_writable_dir", lambda _p: False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    checkout = Path(app.__file__).resolve().parent.parent
    store = app.state_dir().resolve()
    assert checkout != store and checkout not in store.parents, (
        "when the project is read-only, state must fall back off the checkout"
    )


def test_unwritable_state_dir_warns_and_does_not_crash(tmp_path, monkeypatch,
                                                       caplog):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")  # mkdir will fail
    monkeypatch.setenv("PDF_FORGE_STATE_DIR", str(blocked / "state"))
    # Reset the once-per-process warning latch so this run actually emits it.
    monkeypatch.setattr(app.safeio, "_warning_shown", False, raising=False)
    # Recording must not raise, must warn the user, and discovery must work.
    with caplog.at_level("WARNING"):
        app.record_generated_output(make_pdf(tmp_path / "x.pdf", 1))
    assert any("tracking is unavailable" in r.message for r in caplog.records), (
        "the user was not warned that output tracking is degraded"
    )
    assert isinstance(app.load_generated_outputs(), set)
    assert app.discover_pdfs_in_folder(tmp_path)
