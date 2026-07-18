# -*- coding: utf-8 -*-
"""Regression tests for prompt numbering, folder errors, retry reporting and exit codes.

Covers PF-030 (visible prompt numbers are allocated at display time and stay
monotonic across retries and nested prompts), PF-029 (folder iteration failures
surface as a clean domain error), PF-039 (retry messages match execution) and
PF-040 (exit_code is always bound).
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402


def label_of(text: str) -> str:
    match = re.search(r"(\S+)\.\s", text)
    return match.group(1) if match else ""


def displayed_labels(prompts_shown):
    return [label_of(p) for p in prompts_shown]


# --------------------------------------------------------------------------- #
# PF-030 - numbering assigned at display, monotonic across retries
# --------------------------------------------------------------------------- #

def test_retry_shows_a_new_monotonic_number(monkeypatch):
    app.set_operation_prompt("3")
    shown = []
    answers = iter(["bad", "bad", "ok"])

    def fake_input(text):
        shown.append(text)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    prompt = app.question_prompt("Pages per file")
    for _ in range(3):
        app.prompts._input(prompt)

    labels = displayed_labels(shown)
    assert labels == ["3-1", "3-2", "3-3"], labels


def test_nested_prompt_does_not_make_the_outer_label_repeat(monkeypatch):
    """The reported failure: an inner prompt advanced the shared counter, so the
    outer prompt redisplayed an already-used number."""
    app.set_operation_prompt("1")
    shown = []
    answers = iter(["x"] * 10)

    def fake_input(text):
        shown.append(text)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    outer = app.question_prompt("Output image quality")
    app.prompts._input(outer)                      # 1-1
    inner = app.question_prompt("Custom DPI")      # nested
    app.prompts._input(inner)                      # 1-2
    app.prompts._input(outer)                      # must be 1-3, not 1-1

    labels = displayed_labels(shown)
    assert labels == ["1-1", "1-2", "1-3"], labels
    assert len(set(labels)) == len(labels), "a visible label was reused"


def test_labels_are_strictly_monotonic_over_a_long_flow(monkeypatch):
    app.set_operation_prompt("2")
    shown = []
    answers = iter(["v"] * 30)
    monkeypatch.setattr("builtins.input",
                        lambda text: (shown.append(text), next(answers))[1])
    prompts = [app.question_prompt(f"Q{i}") for i in range(5)]
    for _ in range(4):
        for prompt in prompts:
            app.prompts._input(prompt)

    numbers = [int(label_of(p).split("-")[1]) for p in shown]
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)


def test_prefix_stays_stable_while_the_local_counter_advances(monkeypatch):
    app.set_operation_prompt("7")
    shown = []
    answers = iter(["a", "b", "c"])
    monkeypatch.setattr("builtins.input",
                        lambda text: (shown.append(text), next(answers))[1])
    prompt = app.question_prompt("Repeated")
    for _ in range(3):
        app.prompts._input(prompt)
    assert all(label_of(p).startswith("7-") for p in shown)


def test_reentering_an_operation_restarts_numbering(monkeypatch):
    shown = []
    answers = iter(["a", "b"])
    monkeypatch.setattr("builtins.input",
                        lambda text: (shown.append(text), next(answers))[1])
    app.set_operation_prompt("1")
    app.prompts._input(app.question_prompt("First"))
    app.set_operation_prompt("1")  # left and re-entered
    app.prompts._input(app.question_prompt("First again"))
    assert displayed_labels(shown) == ["1-1", "1-1"]


# --------------------------------------------------------------------------- #
# PF-029 - folder iteration errors
# --------------------------------------------------------------------------- #

def test_missing_folder_raises_a_domain_error(tmp_path):
    missing = tmp_path / "gone"
    with pytest.raises(app.FolderScanError) as excinfo:
        app.discover_pdfs_in_folder(missing)
    assert "no longer exists" in str(excinfo.value)


def test_not_a_directory_raises_a_domain_error(tmp_path):
    plain = tmp_path / "file.txt"
    plain.write_text("hi", encoding="utf-8")
    with pytest.raises(app.FolderScanError):
        app.discover_pdfs_in_folder(plain)


def test_io_failure_is_wrapped(tmp_path, monkeypatch):
    def boom(_self):
        raise OSError("device not ready")

    monkeypatch.setattr(Path, "iterdir", boom)
    with pytest.raises(app.FolderScanError) as excinfo:
        app.discover_pdfs_in_folder(tmp_path)
    assert "device not ready" in str(excinfo.value)


def test_office_discovery_uses_the_same_guard(tmp_path):
    with pytest.raises(app.FolderScanError):
        app.discover_office_files(tmp_path / "nope")


@pytest.mark.skipif(os.name != "nt", reason="ACL-based denial is Windows-specific")
def test_permission_denied_is_reported_cleanly(tmp_path):
    import subprocess

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    # Deny listing for the current user.
    subprocess.run(["icacls", str(blocked), "/deny", f"{os.environ['USERNAME']}:(RD)"],
                   capture_output=True)
    try:
        try:
            app.discover_pdfs_in_folder(blocked)
        except app.FolderScanError as exc:
            assert "Permission denied" in str(exc) or "Could not read" in str(exc)
    finally:
        subprocess.run(["icacls", str(blocked), "/remove:d", os.environ["USERNAME"]],
                       capture_output=True)


# --------------------------------------------------------------------------- #
# PF-039 - retry messages match what actually happened
# --------------------------------------------------------------------------- #

def test_success_on_first_attempt_reports_no_restart(monkeypatch, capsys):
    calls = {"n": 0}

    def convert(_server, job):
        calls["n"] += 1
        return "ok"

    monkeypatch.setattr(app.ops_office, "_convert_one", convert)
    result = app.ops_office._convert_with_restart(object(), {"src": Path("a.docx")})
    assert result[0] if isinstance(result, tuple) else result == "ok"
    assert calls["n"] == 1
    assert "restart" not in capsys.readouterr().out.lower()


def test_final_failure_message_matches_configured_attempts(monkeypatch, capsys):
    def always_lost(_server, _job):
        raise app.office_runtime.OfficeRuntimeError(
            app.office_runtime.BRIDGE_LOST_SENTINEL)

    monkeypatch.setattr(app.ops_office, "_convert_one", always_lost)
    # Never touch the real runtime: restarting it would wait out the native
    # startup timeout (minutes) instead of testing the message.
    monkeypatch.setattr(app.office_runtime, "start_conversion_server",
                        lambda *a, **k: object())
    monkeypatch.setattr(app.office_runtime, "warm_up", lambda server: server)

    result, _server = app.ops_office._convert_with_restart(
        object(), {"src": Path("a.docx")}, attempts=3
    )
    assert result == "fail"
    out = capsys.readouterr().out
    # The message must state the real attempt count and restart count, not a
    # fixed "retried N times" claim.
    assert "attempt 3 of 3" in out, out
    assert "2 runtime restart(s)" in out, out


# --------------------------------------------------------------------------- #
# PF-040 - exit_code is always bound
# --------------------------------------------------------------------------- #

def test_exit_code_is_bound_before_the_protected_block(monkeypatch):
    """A BaseException raised before the assignment must not cause
    UnboundLocalError in the finally block (which would mask the real cause)."""
    def explode():
        raise SystemExit(7)

    monkeypatch.setattr(app.app, "main_menu", explode)
    monkeypatch.setattr(app.app, "setup_logging", lambda _d: None)
    monkeypatch.setattr(app.app, "cleanup_temp_dir", lambda: None)
    with pytest.raises(SystemExit) as excinfo:
        app.app.main([])
    assert excinfo.value.code == 7, "the original exception must survive cleanup"


def test_logging_failure_does_not_mask_the_outcome(monkeypatch):
    monkeypatch.setattr(app.app, "main_menu", lambda: 0)
    monkeypatch.setattr(app.app, "setup_logging", lambda _d: None)
    monkeypatch.setattr(app.app, "cleanup_temp_dir", lambda: None)

    import logging as _logging

    def boom():
        raise RuntimeError("logging is broken")

    monkeypatch.setattr(_logging, "shutdown", boom)
    assert app.app.main([]) == 0
