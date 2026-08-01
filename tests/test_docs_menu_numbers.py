# -*- coding: utf-8 -*-
"""The README's menu numbers must name the tools the app actually puts there.

The 2.0.0 renumbering left eight walkthroughs pointing at the wrong tool: the
delete-pages walkthrough opened the watermark remover, the extract-images one
opened compress. The README even carried a callout warning that the old numbers
now open different tools, directly above the old numbers.

Resyncing prose is not a fix - it drifts again on the next menu change. These
assertions read `menus._MAIN_ITEMS` and fail the build instead.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf_forge import menus  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

# Which tool a walkthrough sentence is about, keyed by the *menu label* rather
# than by its number. Keying on the label is what makes this test catch a
# reorder: the expected number is looked up in _MAIN_ITEMS at run time, so
# moving an item changes what the README must say.
#
# Values are distinctive lowercase phrases from the sentence itself - tolerant
# of rewording elsewhere in the paragraph, strict about which tool is meant.
_SENTENCE_KEYWORD = {
    "Merge multiple PDFs": "merge submenu",
    "Delete pages": "removes pages",
    "PDF to images (PNG)": "image-export",
    "PDF to image-only PDF": "image-only",
    "Remove image watermark": "watermark",
    "Extract images from PDF": "extracts the raster images",
    "Compress PDF (reduce file size)": "compresses a pdf",
    "Protect PDF (set password / restrictions)": "encrypts a pdf",
    "Unlock PDF (remove password & restrictions)": "password and permission",
}

# "Page tools" and the convert tool are introduced by their headings rather than
# by a "Selecting `N`" sentence, so they are covered by the second test only.


def _walkthrough_sentences():
    """Every ``Selecting `N` in the main menu ...`` sentence, with its number.

    The captured tail stops at the blank line so a keyword belonging to the
    *next* tool cannot leak into this match.
    """
    pattern = re.compile(r"Selecting `(\d+)` in the main menu(.*?)\n\s*\n", re.S)
    for match in pattern.finditer(README):
        yield int(match.group(1)), match.group(2).lower()


def test_readme_menu_numbers_match_the_app():
    """Every "Selecting `N`" in the README must name the tool the app puts at N."""
    items = list(menus._MAIN_ITEMS)
    unknown = [label for label in _SENTENCE_KEYWORD if label not in items]
    assert not unknown, (
        f"these labels are no longer in the main menu: {unknown}; the menu was "
        "renamed and this table plus the README walkthroughs need updating"
    )

    seen: dict = {}
    for number, tail in _walkthrough_sentences():
        matched = [label for label, keyword in _SENTENCE_KEYWORD.items()
                   if keyword in tail]
        assert len(matched) == 1, (
            f"the sentence for main-menu {number} matches {matched or 'no'} "
            f"tool(s); it must identify exactly one. Sentence: {tail[:120]!r}"
        )
        label = matched[0]
        expected = items.index(label) + 1
        assert number == expected, (
            f"README says selecting {number} opens {label!r}, but the app puts "
            f"it at {expected}"
        )
        assert label not in seen, f"{label!r} has two walkthrough sentences"
        seen[label] = number

    missing = sorted(set(_SENTENCE_KEYWORD) - set(seen))
    assert not missing, (
        f"no walkthrough sentence was found for {missing}; either the README "
        "lost it or the wording changed and this table is now lying about "
        "what it checks"
    )


def test_every_menu_item_has_a_walkthrough_in_menu_order():
    """A reader following the README top to bottom must meet the tools in order.

    Guards the other half of the same rot: the sections themselves used to
    appear in the pre-2.0.0 order even where their numbers were right.
    """
    headings = re.findall(r"^### (.+?)\s*$", README, re.M)

    positions = []
    for label in menus._MAIN_ITEMS:
        # "Page tools" heads two sections ("Page tools -> Extract selected
        # pages" and "... -> Split into fixed-size chunks"); the first one is
        # where the reader meets it.
        where = [i for i, h in enumerate(headings) if h.startswith(label)]
        assert where, f"the README has no walkthrough section for {label!r}"
        positions.append((label, where[0]))

    out_of_order = [
        (positions[i - 1][0], positions[i][0])
        for i in range(1, len(positions))
        if positions[i][1] < positions[i - 1][1]
    ]
    assert not out_of_order, (
        "these walkthrough sections appear before the tool that precedes them "
        f"in the main menu: {out_of_order}"
    )
