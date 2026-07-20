# -*- coding: utf-8 -*-
"""Regression tests for race-safe output promotion and the state store.

Covers PF-003 (promotion must never clobber a destination that appeared after
configuration), PF-009 (every promoted PDF is tracked), PF-027 (atomic, locked,
corruption-aware manifest), PF-028 (state lives outside the checkout and
degrades visibly), and PF-046 (strong file identity).
"""

import json
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
# PF-028 - state lives outside the checkout and degrades visibly
# --------------------------------------------------------------------------- #

def test_state_is_not_stored_in_the_repository_checkout():
    checkout = Path(app.__file__).resolve().parent.parent
    store = app.state_dir().resolve()
    assert checkout not in store.parents and store != checkout, \
        "machine-local state must not live in the checkout"


def test_unwritable_state_dir_warns_and_does_not_crash(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")  # mkdir will fail
    monkeypatch.setenv("PDF_FORGE_STATE_DIR", str(blocked / "state"))
    warning = app.state_store_warning()
    assert warning and "tracking is unavailable" in warning
    # Recording must not raise, and discovery must still work.
    app.record_generated_output(make_pdf(tmp_path / "x.pdf", 1))
    assert isinstance(app.load_generated_outputs(), set)
    assert app.discover_pdfs_in_folder(tmp_path)
