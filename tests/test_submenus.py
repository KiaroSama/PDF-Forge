# -*- coding: utf-8 -*-
"""Characterization tests for the six near-identical submenu loops.

These pin the exact behaviour BEFORE the six duplicated loops are consolidated
into one table-driven runner, so the consolidation can be proven to change
nothing: the rendered lines, the Enter=1 default, the per-item dispatch, the
0=Back / exit=quit handling, and the exact invalid-choice message that depends
on how many options the menu has.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from pdf_forge import menus  # noqa: E402


# Each entry: the loop function, its renderer, the operation names it dispatches
# to in order (choice "1", "2", ...), the title fragment, and the exact invalid
# message (which lists the valid option numbers).
SUBMENUS = [
    (menus.page_tools_menu, menus.show_page_tools_menu,
     ["operation_extract_pages", "operation_split_chunks"],
     "Page tools:", "Please choose 1, 2, or 0."),
    (menus.pdf_to_images_menu, menus._show_pdf_to_images_menu,
     ["operation_images_all_pages", "operation_images_selected_pages",
      "operation_images_batch_folder"],
     "PDF to images:", "Please choose 1, 2, 3, or 0."),
    (menus.pdf_to_image_pdf_menu, menus._show_image_pdf_menu,
     ["operation_pdf_to_image_pdf", "operation_image_pdf_batch_folder"],
     "PDF to image-only PDF:", "Please choose 1, 2, or 0."),
    (menus.delete_pages_menu, menus._show_delete_pages_menu,
     ["operation_delete_pages_single", "operation_delete_pages_batch"],
     "Delete pages:", "Please choose 1, 2, or 0."),
    (menus.compress_menu, menus._show_compress_menu,
     ["operation_compress_pdf", "operation_compress_pdf_batch"],
     "Compress PDF:", "Please choose 1, 2, or 0."),
    (menus.protect_menu, menus._show_protect_menu,
     ["operation_protect_open_password", "operation_protect_restrict"],
     "Protect PDF:", "Please choose 1, 2, or 0."),
]

IDS = [s[3].rstrip(":") for s in SUBMENUS]


def _feed(monkeypatch, answers):
    supplied = iter(answers)
    monkeypatch.setattr(menus, "_input", lambda _p: next(supplied))


def _stub_operations(monkeypatch, op_names, log):
    """Replace each dispatched operation with a recorder."""
    for name in op_names:
        monkeypatch.setattr(menus, name, (lambda n=name: log.append(n)),
                            raising=False)


@pytest.mark.parametrize("loop,render,ops,title,invalid", SUBMENUS, ids=IDS)
def test_submenu_renders_its_title_and_options(loop, render, ops, title,
                                               invalid, capsys):
    render()
    out = capsys.readouterr().out
    assert title in out, f"the title '{title}' was not rendered"
    assert "0." in out and "Back" in out, "the Back option is missing"
    # One numbered line per option, plus the [1] default marker on the first.
    for i in range(1, len(ops) + 1):
        assert f"{i}." in out, f"option {i} was not rendered"
    assert "[1]" in out, "the Enter=1 default marker is missing"


@pytest.mark.parametrize("loop,render,ops,title,invalid", SUBMENUS, ids=IDS)
def test_enter_selects_option_one(loop, render, ops, title, invalid,
                                  monkeypatch):
    log = []
    _stub_operations(monkeypatch, ops, log)
    # Enter (empty) runs option 1, then 0 returns.
    _feed(monkeypatch, ["", "0"])
    loop()
    assert log == [ops[0]], f"Enter did not select option 1: ran {log}"


@pytest.mark.parametrize("loop,render,ops,title,invalid", SUBMENUS, ids=IDS)
def test_each_number_dispatches_to_its_operation(loop, render, ops, title,
                                                 invalid, monkeypatch):
    log = []
    _stub_operations(monkeypatch, ops, log)
    answers = [str(i) for i in range(1, len(ops) + 1)] + ["0"]
    _feed(monkeypatch, answers)
    loop()
    assert log == ops, f"dispatch order wrong: {log} != {ops}"


@pytest.mark.parametrize("loop,render,ops,title,invalid", SUBMENUS, ids=IDS)
def test_zero_returns(loop, render, ops, title, invalid, monkeypatch):
    _stub_operations(monkeypatch, ops, [])
    _feed(monkeypatch, ["0"])
    loop()  # must simply return, no exception


@pytest.mark.parametrize("word", ["exit", "quit"])
@pytest.mark.parametrize("loop,render,ops,title,invalid", SUBMENUS, ids=IDS)
def test_exit_and_quit_raise(loop, render, ops, title, invalid, word,
                             monkeypatch):
    _stub_operations(monkeypatch, ops, [])
    _feed(monkeypatch, [word])
    with pytest.raises(app.prompts._ExitRequested):
        loop()


@pytest.mark.parametrize("loop,render,ops,title,invalid", SUBMENUS, ids=IDS)
def test_invalid_choice_prints_the_exact_message(loop, render, ops, title,
                                                 invalid, monkeypatch, capsys):
    _stub_operations(monkeypatch, ops, [])
    # An out-of-range number is invalid; then 0 to leave.
    _feed(monkeypatch, [str(len(ops) + 5), "0"])
    loop()
    assert invalid in capsys.readouterr().out, (
        f"the invalid-choice message changed; expected {invalid!r}"
    )


@pytest.mark.parametrize("loop,render,ops,title,invalid", SUBMENUS, ids=IDS)
def test_keyboardinterrupt_returns_to_the_loop(loop, render, ops, title,
                                               invalid, monkeypatch, capsys):
    def boom():
        raise KeyboardInterrupt

    monkeypatch.setattr(menus, ops[0], boom, raising=False)
    for name in ops[1:]:
        monkeypatch.setattr(menus, name, lambda: None, raising=False)
    # Interrupt on option 1 is caught and the loop continues to accept 0.
    _feed(monkeypatch, ["1", "0"])
    loop()
    assert "interrupted" in capsys.readouterr().out.lower(), (
        "a KeyboardInterrupt must be caught and reported, not propagated"
    )
