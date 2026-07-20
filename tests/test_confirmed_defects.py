# -*- coding: utf-8 -*-
"""Three defects a repo-wide sweep found, each verified before being fixed.

None of them could be caught by the existing suite, and the reason is the same
in all three: the test conditions never matched the production ones.

  - the MS Office backend staged its output in %TEMP% and finished with
    os.replace(), which cannot cross volumes on Windows. Every test writes to
    pytest's tmp_path, which lives under %TEMP% - the same volume - so the move
    never crossed anything. A user converting to a second drive did.
  - the orphan reaper matched processes on a glob built from a native Windows
    path, while the value on soffice's command line is a file:// URI, because
    unoserver converts it with Path.as_uri(). Measured against a live process:
    the shipped glob matched 0, the URI form matched 2. Nothing covered it, and
    the leak it was written to fix had stopped for a different reason.
  - PowerPoint called Presentation.UpdateLinks() under a comment claiming it
    suppressed link updates. It is an argument-less action method: it performs
    the refresh. Word and Excel pass real suppression parameters. The guard test
    asserted only that "Update" appeared in the function body, which the
    offending call satisfies.
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf_forge import msoffice, office_server  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. The MS Office backend must deliver its output across volumes
# --------------------------------------------------------------------------- #

class _FakeApp:
    """Stands in for a COM application object; writes the PDF Office would."""

    def __init__(self):
        self.quit_called = False

    def Quit(self):
        self.quit_called = True


def test_msoffice_delivers_its_output_across_volumes(tmp_path, monkeypatch):
    """Staging on one volume and delivering to another must still work.

    os.replace() raises OSError(EXDEV / WinError 17) across volumes. Rather than
    require a second physical drive to be present on whatever machine runs this,
    the failure is injected exactly where the real one occurs - so the test
    proves the *fallback path* works, which is the thing that was missing.
    """
    session = msoffice.MsOfficeSession()
    fake = _FakeApp()
    monkeypatch.setattr(session, "_app", lambda family: fake)

    def fake_handler(app_obj, src, produced, secret):
        Path(produced).write_bytes(b"%PDF-1.4\n%%EOF\n")

    monkeypatch.setattr(msoffice, "_convert_word", fake_handler)

    crossed = {"n": 0}

    def refusing(real):
        def refuse(a, b, *args, **kwargs):
            # Only the staging -> destination delivery crosses volumes.
            if str(a).endswith("converted.pdf"):
                crossed["n"] += 1
                # errno.EXDEV, not the bare 17 from the Windows message: Python
                # maps errno 17 to FileExistsError, which is not what the real
                # call raises. Measured here, os.replace across C: -> G: gives a
                # plain OSError whose winerror is 17 and whose strerror is below.
                raise OSError(errno.EXDEV,
                              "The system cannot move the file to a different "
                              "disk drive")
            return real(a, b, *args, **kwargs)
        return refuse

    # Both primitives, so the test does not encode which one the code picks:
    # os.replace was the original, os.rename is what shutil.move tries first.
    # Whatever the implementation, a rename across volumes is refused and the
    # file must still arrive.
    monkeypatch.setattr(os, "replace", refusing(os.replace))
    monkeypatch.setattr(os, "rename", refusing(os.rename))

    src = tmp_path / "doc.docx"
    src.write_bytes(b"PK\x03\x04placeholder")
    out = tmp_path / "out" / "doc.pdf"

    session.convert(src, out, "word")

    assert crossed["n"] == 1, "the cross-volume delivery was never attempted"
    assert out.exists(), (
        "the conversion was lost because delivery used a same-volume-only move"
    )
    assert out.read_bytes().startswith(b"%PDF"), "the delivered file is not the PDF"


# --------------------------------------------------------------------------- #
# 2. The orphan reaper must actually match the process it is meant to kill
# --------------------------------------------------------------------------- #

def _reaper_glob(profile_dir: Path) -> str:
    """The glob _kill_profile_owners hands to PowerShell, captured."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["glob"] = kwargs.get("env", {}).get("PDFFORGE_PROFILE_GLOB", "")

        class R:
            returncode = 0
        return R()

    real_run = subprocess.run
    subprocess.run = fake_run
    try:
        office_server._kill_profile_owners(profile_dir)
    finally:
        subprocess.run = real_run
    return captured.get("glob", "")


def test_the_orphan_reaper_matches_the_form_on_the_command_line(tmp_path):
    """The glob must match what soffice's command line actually contains.

    unoserver converts --user-installation with Path(...).as_uri() before
    launching LibreOffice, so the command line carries
    file:///C:/Users/.../pdfforge_loprofile_x, not C:\\Users\\...  PowerShell's
    -like treats a backslash literally, so a glob built from the native path
    cannot match - measured against a live process: 0 matches for the shipped
    form, 2 for the URI form. The reaper was therefore inert, and the leak it
    was written to fix had stopped because of _remove_profile's retry loop.
    """
    profile = tmp_path / "pdfforge_loprofile_probe"
    profile.mkdir()
    glob = _reaper_glob(profile)

    assert glob, "no glob was passed to the process query"
    if os.name != "nt":
        pytest.skip("the reaper is a Windows path; nothing to match elsewhere")

    command_line_form = profile.resolve().as_uri()
    needle = glob.strip("*")
    assert needle in command_line_form, (
        "the reaper looks for\n  "
        f"{needle}\nbut soffice's command line carries\n  "
        f"{command_line_form}\nso Stop-Process is never reached"
    )


# --------------------------------------------------------------------------- #
# 3. PowerPoint must suppress link updates, not perform them
# --------------------------------------------------------------------------- #

def test_powerpoint_does_not_call_the_update_action(tmp_path, monkeypatch):
    """Presentation.UpdateLinks() refreshes links; it does not suppress them.

    Behavioural, not source-text: the previous guard asserted "Update" appeared
    somewhere in the function, which the offending call satisfied. This records
    what is actually invoked on the presentation object.
    """
    calls = []

    class Shape:
        def __init__(self):
            self.LinkFormat = self

        _auto = None

        @property
        def AutoUpdate(self):
            return Shape._auto

        @AutoUpdate.setter
        def AutoUpdate(self, value):
            Shape._auto = value
            calls.append(("AutoUpdate", value))

    class Presentation:
        def __init__(self):
            self.Shapes = [Shape()]

        def UpdateLinks(self):
            calls.append(("UpdateLinks()", None))

        def SaveAs(self, path, fmt):
            calls.append(("SaveAs", fmt))
            Path(path).write_bytes(b"%PDF-1.4\n%%EOF\n")

        def Close(self):
            calls.append(("Close", None))

    class App:
        class Presentations:
            @staticmethod
            def Open(path, **kwargs):
                calls.append(("Open", kwargs))
                return Presentation()

    Shape._auto = None
    msoffice._convert_powerpoint(App(), tmp_path / "x.pptx",
                                 tmp_path / "x.pdf", "refuse")

    invoked = [name for name, _ in calls]
    assert "UpdateLinks()" not in invoked, (
        "UpdateLinks() is an action that refreshes every linked OLE object - "
        "it performs exactly the external fetch the comment claims to prevent"
    )
    assert ("AutoUpdate", 2) in calls, (
        "linked shapes were left on automatic update; set "
        "LinkFormat.AutoUpdate = 2 (ppUpdateOptionManual) instead"
    )
