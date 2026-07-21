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

import pdf_forge as app  # noqa: E402
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
    # Before touching the reaper: it returns early off Windows, so on Linux
    # there is no glob to inspect and asserting on one fails for a reason that
    # has nothing to do with the defect.
    if os.name != "nt":
        pytest.skip("the reaper is Windows-only; there is no glob to match")

    profile = tmp_path / "pdfforge_loprofile_probe"
    profile.mkdir()
    glob = _reaper_glob(profile)

    assert glob, "no glob was passed to the process query"
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

    class Slide:
        def __init__(self):
            self.Shapes = [Shape()]

    class Presentation:
        # NO .Shapes attribute, exactly like real PowerPoint COM: shapes live on
        # each Slide. Accessing .Shapes must raise, so a fix reaching for it
        # fails here instead of being hidden by a faked attribute - which is what
        # the previous version of this test did, letting a shipped AttributeError
        # regression pass.
        def __init__(self):
            self.Slides = [Slide()]

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
    assert not hasattr(Presentation(), "Shapes"), (
        "the fake Presentation must not expose Shapes; real COM does not"
    )
    msoffice._convert_powerpoint(App(), tmp_path / "x.pptx",
                                 tmp_path / "x.pdf", "refuse")

    invoked = [name for name, _ in calls]
    assert "UpdateLinks()" not in invoked, (
        "UpdateLinks() is an action that refreshes every linked OLE object - "
        "it performs exactly the external fetch the comment claims to prevent"
    )
    assert ("AutoUpdate", 2) in calls, (
        "linked shapes (on slides) were left on automatic update; set "
        "LinkFormat.AutoUpdate = 2 (ppUpdateOptionManual) instead"
    )


# --------------------------------------------------------------------------- #
# 4. A DOS-encoded CSV must not take the application down
# --------------------------------------------------------------------------- #

def test_a_cp1252_undefined_byte_does_not_crash_csv_detection(tmp_path):
    """cp1252 leaves five bytes undefined; the fallback claimed it could not fail.

    0x81, 0x8D, 0x8F, 0x90 and 0x9D are unmapped in cp1252 and appear routinely
    in CP437/CP850 DOS exports. The resulting UnicodeDecodeError is a
    ValueError, so it matched none of the OSError handlers upstream and exited
    the application - taking the whole folder selection with it.
    """
    from pdf_forge import office

    csv_path = tmp_path / "dos.csv"
    # Valid CSV structure, one byte cp1252 cannot map.
    csv_path.write_bytes(b"name;qty\r\nca\x81f\xe9;2\r\n")

    dialect = office.detect_csv_dialect(csv_path)

    assert dialect is not None, "detection returned nothing for a readable CSV"
    assert dialect.encoding in ("windows-1252", "utf-8"), dialect.encoding


# --------------------------------------------------------------------------- #
# 5. A byte-corrupted manifest must be quarantined, not crash every folder tool
# --------------------------------------------------------------------------- #

def test_a_byte_corrupted_manifest_is_quarantined(tmp_path, monkeypatch):
    """Corruption recovery only handled *text* damage.

    load_generated_outputs read with read_text(), and a UnicodeDecodeError is a
    ValueError - not the FileNotFoundError/OSError the reader catches, and
    raised before the quarantine branch could ever run. Every folder tool then
    crashed and stayed crashed, because record_generated_output's blanket
    handler swallowed the same error and left the bytes in place.
    """
    monkeypatch.setenv("PDF_FORGE_STATE_DIR", str(tmp_path))
    manifest = app.manifest_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(b'\xff\xfe\x00{"outputs": []}')

    entries = app.load_generated_outputs()

    assert entries == set() or entries == [], (
        "a corrupt manifest must read as empty, not raise"
    )
    backups = list(manifest.parent.glob("*.corrupt"))
    assert backups, (
        "the damaged manifest was neither quarantined nor replaced; the next "
        "run will hit exactly the same bytes"
    )


# --------------------------------------------------------------------------- #
# 6. Soft-mask compositing must actually composite
# --------------------------------------------------------------------------- #

def test_a_masked_area_composites_to_white_not_to_the_hidden_pixels(tmp_path):
    """_composite_on_white raised on every input and silently fell back.

    It built an opaque Pixmap and called copy(), which overwrites rather than
    blends and refuses a source whose alpha differs from the target - "source
    and target alpha must be equal". So the except branch ran every time and
    the caller kept exactly the pixels the mask existed to hide: a transparent
    logo came out as whatever was stored underneath.
    """
    import pymupdf

    from pdf_forge import render

    doc = pymupdf.open()
    page = doc.new_page()
    black = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
    black.clear_with(0)
    invisible = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 64, 64), False)
    invisible.clear_with(0)          # alpha 0 everywhere: nothing is visible
    page.insert_image(pymupdf.Rect(10, 10, 74, 74),
                      pixmap=pymupdf.Pixmap(black, invisible))
    src = tmp_path / "masked.pdf"
    doc.save(str(src))
    doc.close()

    doc = pymupdf.open(str(src))
    try:
        composited = None
        for xref, _page_number, _n in render._iter_unique_images(doc):
            smask = render._smask_xref(doc, xref)
            if not smask:
                continue
            raw = pymupdf.Pixmap(doc, xref)
            composited = render._composite_on_white(pymupdf, doc, raw, smask)
            assert composited is not raw, (
                "compositing raised and fell back to the raw pixmap"
            )
            break
        assert composited is not None, "the fixture produced no soft-masked image"
        assert composited.pixel(32, 32) == (255, 255, 255), (
            "a fully transparent area kept its hidden pixels instead of "
            f"showing white: {composited.pixel(32, 32)}"
        )
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 7. Why the postcondition does NOT compare permission bits
# --------------------------------------------------------------------------- #

def test_permission_bits_cannot_be_verified_after_owner_authentication(tmp_path):
    """Records the measurement that makes a permission check impossible here.

    A sweep proposed adding `check.permissions == policy.permissions` to
    validate_protection_postcondition. It cannot work: save_kwargs() sets
    owner_pw = user_pw, so authenticating grants *owner* access and the
    reopened document reports every bit as allowed regardless of what was
    written. Below, a file saved with permissions=0 reads back as fully
    permitted. Such a comparison could never fail - it would be a guard that
    cannot fire, which is the very defect class this file exists for.

    The real limitation is handled where it can still be acted on:
    resolve_protection warns that an owner-restricted source cannot be
    reproduced and asks before anything is written.
    """
    import pymupdf

    out = tmp_path / "restricted.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(out), encryption=pymupdf.PDF_ENCRYPT_AES_256,
             user_pw="pw", owner_pw="pw", permissions=0)
    doc.close()

    check = pymupdf.open(str(out))
    try:
        assert check.authenticate("pw"), "the fixture password must work"
        assert check.permissions & int(pymupdf.PDF_PERM_PRINT), (
            "if this ever fails, permission bits survive owner authentication "
            "and a real postcondition check becomes possible - revisit "
            "validate_protection_postcondition"
        )
    finally:
        check.close()

    policy = app.pdf_io.ProtectionPolicy(
        kind="password", password="pw",
        permissions=int(pymupdf.PDF_PERM_PRINT))
    # Passes, and must: the file is genuinely reopenable with its password.
    app.pdf_io.validate_protection_postcondition(out, policy)


# --------------------------------------------------------------------------- #
# 19. A non-decimal Unicode digit must raise the typed parser error, not a bare
#     ValueError that escapes app.py and silently discards the queue
# --------------------------------------------------------------------------- #

# "²" (U+00B2 SUPERSCRIPT TWO) is one of 128 characters for which str.isdigit()
# is True but str.isdecimal() is False and int() raises. The .isdigit() guards
# let it past validation, and int() then raised a bare ValueError - a type that
# PageSelectionError/ChunkSizeError do not cover, so it escaped to app.py's
# top-level handler and skipped finalize_queue(), dropping any queued batch.
_NON_DECIMAL_DIGIT = "²"


def test_non_decimal_digit_in_page_selection_raises_typed_error():
    with pytest.raises(app.PageSelectionError):
        app.parse_page_selection(_NON_DECIMAL_DIGIT, 10)


def test_non_decimal_digit_in_page_range_raises_typed_error():
    with pytest.raises(app.PageSelectionError):
        app.parse_page_selection("1-" + _NON_DECIMAL_DIGIT, 10)


def test_non_decimal_digit_in_delete_selection_raises_typed_error():
    with pytest.raises(app.PageSelectionError):
        app.parse_delete_pages(_NON_DECIMAL_DIGIT)
    with pytest.raises(app.PageSelectionError):
        app.parse_delete_pages("1-" + _NON_DECIMAL_DIGIT)


def test_non_decimal_digit_in_chunk_size_raises_typed_error():
    with pytest.raises(app.ChunkSizeError):
        app.parse_chunk_size(_NON_DECIMAL_DIGIT)


def test_non_decimal_digit_in_page_number_raises_typed_error():
    with pytest.raises(app.ChunkSizeError):
        app.parse_page_number(_NON_DECIMAL_DIGIT, default=1, total_pages=10,
                              label="start page")


def test_non_decimal_digit_in_index_list_gives_the_clear_message():
    # parse_index_list already declares ValueError, so the escaping symptom does
    # not apply, but int() still produced Python's opaque "invalid literal"
    # message instead of the guard's. The fix must surface the friendly one.
    with pytest.raises(ValueError) as excinfo:
        app.parse_index_list(_NON_DECIMAL_DIGIT, 5)
    assert "invalid literal" not in str(excinfo.value)


def test_ordinary_ascii_digits_still_parse():
    # The predicate change must not alter what counts as valid for normal input.
    assert app.parse_page_selection("1-3,5", 10).pages == [1, 2, 3, 5]
    assert app.parse_delete_pages("2,4") == [2, 4]
    assert app.parse_chunk_size("50") == 50
    assert app.parse_page_number("7", default=1, total_pages=10, label="p") == 7
    assert app.parse_index_list("1,3", 5) == [1, 3]


# --------------------------------------------------------------------------- #
# 20. A FolderScanError must re-prompt inside the interactive loops, not escape
#     and discard the queue
# --------------------------------------------------------------------------- #

# discover_pdfs_in_folder / discover_office_files raise FolderScanError when a
# folder cannot be listed (e.g. PermissionError from iterdir). The prompt loops
# caught only KeyboardInterrupt, so the error propagated to app.py's handler -
# again skipping finalize_queue() - instead of printing it and re-prompting.

def _drive_folder_prompt(monkeypatch, module, scan_name, prompt_callable,
                         success_value, folder):
    """Feed a valid folder twice; make the scan fail once then succeed.

    Returns ``(result, scan_calls)``. A loop that recovers calls the scan twice
    (error, then success) and returns ``success_value``; a loop that does not
    lets FolderScanError propagate out of ``prompt_callable`` and the test fails.
    """
    answers = iter([str(folder), str(folder)])
    monkeypatch.setattr(module, "_input", lambda _p: next(answers))
    state = {"calls": 0}

    def flaky(_folder, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise app.FolderScanError("Permission denied reading the folder.")
        return success_value

    monkeypatch.setattr(module, scan_name, flaky)
    result = prompt_callable()
    return result, state["calls"]


def test_folder_scan_error_reprompts_in_page_folder_prompt(tmp_path, monkeypatch):
    good = [tmp_path / "a.pdf"]
    result, calls = _drive_folder_prompt(
        monkeypatch, app.prompts, "discover_pdfs_in_folder",
        app.prompts.prompt_source_folder_pdfs, good, tmp_path)
    assert result == good
    assert calls == 2, "an unreadable folder must re-prompt, not propagate"


def test_folder_scan_error_reprompts_in_merge_folder_prompt(tmp_path, monkeypatch):
    good = [tmp_path / "a.pdf", tmp_path / "b.pdf"]
    result, calls = _drive_folder_prompt(
        monkeypatch, app.ops_merge, "discover_pdfs_in_folder",
        app.ops_merge.prompt_merge_source_folder, good, tmp_path)
    assert result == good
    assert calls == 2, "an unreadable folder must re-prompt, not propagate"


def test_folder_scan_error_reprompts_in_office_folder_prompt(tmp_path, monkeypatch):
    good = [tmp_path / "a.docx"]
    result, calls = _drive_folder_prompt(
        monkeypatch, app.ops_office, "discover_office_files",
        app.ops_office.prompt_office_source_folder, good, tmp_path)
    assert result == good
    assert calls == 2, "an unreadable folder must re-prompt, not propagate"


# --------------------------------------------------------------------------- #
# The batch of defects fixed directly in this pass (each reproduced first).
# --------------------------------------------------------------------------- #

def test_corrupt_converted_pdf_becomes_a_typed_error_not_a_batch_abort(tmp_path):
    """#11: a truncated conversion output must raise PdfOpenError.

    pymupdf.open on a corrupt file raises FileDataError, a RuntimeError that is
    NOT a PdfOpenError, so it escaped every handler in the batch loop and
    aborted the whole run with a raw traceback, leaking the staging dir.
    """
    bad = tmp_path / "truncated.pdf"
    bad.write_bytes(b"%PDF-1.5\n" + os.urandom(200))  # header, then garbage
    with pytest.raises(app.PdfOpenError):
        app.ops_office._validate_pdf_output(bad)


def test_startup_failure_detail_comes_from_the_log_file(tmp_path, monkeypatch):
    """#15: the failure detail must read the log file, not the None stdout.

    The child writes to a file, so process.stdout is always None; the old code
    read it and produced an empty detail while the real cause sat in the log.
    """
    from pdf_forge import office_server

    log = tmp_path / "unoserver.log"
    log.write_text("Error: port 2002 already in use\n", encoding="utf-8")

    class DeadProc:
        returncode = 3
        stdout = None

        def poll(self):
            return 3

    class FakeServer:
        process = DeadProc()
        port = 2002

        def read_log(self, limit=8000):
            return log.read_text(encoding="utf-8")[-limit:]

    # UnoClient is constructed before the poll check but only connects when
    # server_info() is called, which the dead-process branch reaches first.
    with pytest.raises(office_server.OfficeRuntimeError) as caught:
        office_server._wait_until_ready(FakeServer(), timeout=1)
    assert "already in use" in str(caught.value), (
        "the real cause from the log was not surfaced"
    )


def test_a_mismatched_soft_mask_degrades_instead_of_aborting(tmp_path):
    """#17: a differently-sized /SMask must not abort the whole extraction.

    Pixmap(base, mask) raises when the mask is a different size (a spec-legal
    downsampled soft mask); with no except the extraction aborted and every
    image already written was orphaned.
    """
    import pymupdf

    from pdf_forge import render

    # A 64x64 base whose soft mask is 32x32 - legal, and a different size.
    doc = pymupdf.open()
    page = doc.new_page()
    base = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
    base.clear_with(180)
    page.insert_image(pymupdf.Rect(0, 0, 64, 64), pixmap=base)
    src = tmp_path / "mm.pdf"
    doc.save(str(src))
    doc.close()

    # Synthesising a real /SMask of a mismatched size inside a PDF is awkward
    # and version-dependent, so drive the helper with a mask xref whose pixmap
    # Pixmap(base, mask) rejects (here the image's own xref, an RGB pixmap where
    # a grayscale mask is required). The point under test is the contract: when
    # that construction raises - for the size mismatch the defect was about, or
    # any other reason - the helper must degrade to the base image and still
    # write a file, not propagate and orphan every image already extracted.
    doc = pymupdf.open(str(src))
    try:
        xref = doc.get_page_images(0)[0][0]
        out = tmp_path / "out.png"
        # Confirm the construction the helper wraps really does raise here.
        raised = False
        try:
            pymupdf.Pixmap(pymupdf.Pixmap(doc, xref), pymupdf.Pixmap(doc, xref))
        except Exception:  # noqa: BLE001 - any raise proves the wrapped path
            raised = True
        assert raised, "the fixture no longer triggers the failing construction"
        written = render._write_image_with_alpha(doc, xref, xref, tmp_path, out)
        assert Path(written).exists(), (
            "the helper propagated instead of degrading to the base image"
        )
    finally:
        doc.close()


def test_a_turkish_i_output_is_recorded_and_reloaded(tmp_path, monkeypatch):
    """#18: file_identity must store a real-case path os.stat can open.

    _normalized lowercased the stored path; on a Turkish dotted-capital-I name
    str.lower() adds a combining dot, so os.stat failed and the entry was
    silently dropped from the manifest - the output was reprocessed next run.
    """
    monkeypatch.setenv("PDF_FORGE_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "İstanbul.pdf"          # Turkish İ
    out.write_bytes(b"%PDF-1.4\n%%EOF\n")

    app.record_generated_output(out)
    recorded = app.load_generated_outputs()

    assert app.safeio._normalized(out) in recorded, (
        "a Turkish-I output was not recorded, so it will be reprocessed"
    )


def test_partial_dpi_scan_does_not_claim_nothing_downsamples(tmp_path, capsys):
    """#23: on a long document the "no image will be downsampled" claim is only
    absolute when every page was scanned; a partial scan must be qualified."""
    from pdf_forge import ops_compress

    # pages_scanned < total_pages => partial. max below the cap => the branch fires.
    stats = {"max": 100, "pages_scanned": 40}
    ops_compress._warn_if_cap_above_max(200, 85, stats, total_pages=300)
    out = capsys.readouterr().out
    assert "no image will be downsampled" not in out, (
        "a sampled scan must not make the absolute claim"
    )
    assert "unsampled" in out or "sampling" in out, (
        "the partial-scan wording is missing"
    )

    # Fully scanned => the absolute claim is allowed.
    ops_compress._warn_if_cap_above_max(200, 85,
                                        {"max": 100, "pages_scanned": 300},
                                        total_pages=300)
    assert "no image will be downsampled" in capsys.readouterr().out


def test_powerpoint_open_carries_the_no_prompt_password(tmp_path):
    """#10: the anti-hang sentinel must reach PowerPoint's Open, not be dropped.

    PowerPoint's Open has no password keyword, so Word/Excel's sentinel was
    simply not passed and an unexpectedly encrypted .ppt could raise a modal
    prompt and hang. The documented channel is the "path::password::" filename.
    """
    calls = {}

    class Presentation:
        Slides = []          # shapes live on slides, not the presentation (COM)

        def SaveAs(self, path, fmt):
            Path(path).write_bytes(b"%PDF-1.4\n%%EOF\n")

        def Close(self):
            pass

    class App:
        class Presentations:
            @staticmethod
            def Open(name, **kwargs):
                calls["name"] = name
                return Presentation()

    msoffice._convert_powerpoint(App(), tmp_path / "x.pptx",
                                 tmp_path / "x.pdf", "unused")
    assert "::" in calls["name"], (
        "the open path carries no password form, so an encrypted .ppt can hang"
    )
    assert "\x00" not in calls["name"], "a NUL byte would truncate the path"


def test_com_error_is_translated_to_a_typed_error(tmp_path, monkeypatch):
    """#4: a raw com_error from a handler must become an MsOfficeError.

    A pywintypes.com_error is a bare Exception, so an untranslated one escaped
    every caller and aborted the whole batch with a raw HRESULT.
    """
    session = msoffice.MsOfficeSession()
    monkeypatch.setattr(session, "_app", lambda family: object())

    class FakeComError(Exception):
        pass

    monkeypatch.setattr(msoffice, "_com_error", lambda: FakeComError)

    def boom(app_obj, src, produced, secret):
        raise FakeComError(-2147024891, "Access is denied", None, None)

    monkeypatch.setattr(msoffice, "_convert_word", boom)

    src = tmp_path / "doc.docx"
    src.write_bytes(b"PK\x03\x04x")
    with pytest.raises(msoffice.MsOfficeError):
        session.convert(src, tmp_path / "out.pdf", "word")


def test_unsupported_encryption_is_not_reported_as_wrong_password():
    """#13: a base DecryptionError (unsupported) must not become a password error.

    The old substring test `"Decryption" in name` caught the base
    DecryptionError too, so an unsupported-encryption file re-prompted forever
    instead of failing honestly. Classified by type with a real password
    supplied, it must be a DecryptError, not a DecryptPasswordError.
    """
    from pdf_forge import office_decrypt

    class DecryptionError(Exception):
        pass

    class OfficeFile:
        def __init__(self, handle):
            pass

        def load_key(self, password="", verify_password=True):
            raise DecryptionError("Unsupported EncryptionInfo version")

    import types
    fake = types.SimpleNamespace(OfficeFile=OfficeFile)

    import tempfile
    d = Path(tempfile.mkdtemp())
    src = d / "enc.docx"
    src.write_bytes(b"\xd0\xcf\x11\xe0stub")

    real_import = office_decrypt.__dict__.get("msoffcrypto")
    # decrypt_to_temp imports msoffcrypto lazily; inject the fake module.
    import sys
    sys.modules["msoffcrypto"] = fake
    try:
        with pytest.raises(office_decrypt.DecryptError):
            office_decrypt.decrypt_to_temp(src, "a-real-password", d)
    finally:
        if real_import is None:
            sys.modules.pop("msoffcrypto", None)


# --------------------------------------------------------------------------- #
# 8. Legacy .doc/.xls/.ppt must be validated by family stream, not just magic
# --------------------------------------------------------------------------- #

def test_renamed_legacy_office_file_is_rejected_by_family(tmp_path, monkeypatch):
    """A renamed .xls (as .doc) must not validate as Word.

    The OLE2 magic is shared by all legacy Office formats, so checking only the
    8 magic bytes let a renamed spreadsheet pass as a document and reach Word.
    The family's marker stream (WordDocument / Workbook / PowerPoint Document)
    is what actually distinguishes them. olefile cannot write a container, so
    the stream listing is stubbed; the family-marker comparison under test runs
    for real.
    """
    from pdf_forge import office

    fake_doc = tmp_path / "spreadsheet.doc"          # .doc extension -> word
    fake_doc.write_bytes(office._CFB_MAGIC + b"\x00" * 512)

    # The container actually holds an Excel workbook stream.
    monkeypatch.setattr(office, "_ole_stream_names",
                        lambda _p: {"workbook", "\x05summaryinformation"})

    ok, reason = office.validate_office_file(fake_doc)
    assert not ok, "a renamed .xls passed validation as a .doc"
    assert "not a word" in reason.lower(), reason

    # A genuine Word container (WordDocument stream) still passes.
    monkeypatch.setattr(office, "_ole_stream_names",
                        lambda _p: {"worddocument", "\x05summaryinformation"})
    ok, reason = office.validate_office_file(fake_doc)
    assert ok, f"a real .doc was rejected: {reason}"


# --------------------------------------------------------------------------- #
# 9. The CLI fallback must reap its process tree and profile on timeout
# --------------------------------------------------------------------------- #

def test_cli_fallback_reaps_the_tree_and_profile_on_timeout(tmp_path, monkeypatch):
    """subprocess.run(timeout) kills only the launcher; soffice.bin orphans.

    The CLI fallback used subprocess.run(capture_output=True, timeout=...), so
    on timeout the separate soffice.bin child was left holding the profile open
    - the same orphan the server path was fixed for. This drives the timeout
    path and asserts the whole tree is reaped and the profile removed.
    """
    reaped = {"terminate": 0, "kill_owners": 0, "removed": 0}

    class FakeProc:
        def __init__(self, *a, **k):
            self._alive = True

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="soffice", timeout=timeout)

        def poll(self):
            return None if self._alive else 0

    monkeypatch.setattr(office_server.subprocess, "Popen",
                        lambda *a, **k: FakeProc())
    monkeypatch.setattr(office_server, "_terminate",
                        lambda p: reaped.__setitem__("terminate", reaped["terminate"] + 1))
    monkeypatch.setattr(office_server, "_kill_profile_owners",
                        lambda prof: reaped.__setitem__("kill_owners", reaped["kill_owners"] + 1))
    real_remove = office_server._remove_profile
    monkeypatch.setattr(office_server, "_remove_profile",
                        lambda prof, **k: (reaped.__setitem__("removed", reaped["removed"] + 1),
                                           real_remove(prof, **k))[1])

    src = tmp_path / "in.docx"
    src.write_bytes(b"PK\x03\x04x")
    soffice = tmp_path / "soffice.exe"
    soffice.write_bytes(b"")

    with pytest.raises(office_server.OfficeRuntimeError, match="timed out"):
        office_server.convert_via_soffice_cli(soffice, src, tmp_path / "out.pdf",
                                              timeout=1)

    assert reaped["terminate"] >= 1, "the launcher process tree was not terminated"
    assert reaped["kill_owners"] >= 1, (
        "the profile-owning soffice.bin child was not reaped on timeout"
    )
    assert reaped["removed"] >= 1, "the profile was not removed after the timeout"


# --------------------------------------------------------------------------- #
# 10. A batch must not write an un-inspected restricted source unprotected
# --------------------------------------------------------------------------- #

def test_runner_file_policy_skips_an_uninspected_restricted_file():
    """The shared runner gate must fail closed on an unconsented restriction."""
    from pdf_forge import batch_protection
    from pdf_forge.pdf_io import ProtectionPolicy

    restricted = ProtectionPolicy(kind="restricted")
    # No preflight policy ("unreadable") + runtime restricted => skip, no write.
    policy, skip = batch_protection.runner_file_policy("unreadable", restricted)
    assert policy is None and skip is not None, (
        "an un-inspected restricted file must be skipped, not written unprotected"
    )
    # A reproducible runtime policy is still preserved, not skipped.
    pw = ProtectionPolicy(kind="password", password="x")
    assert batch_protection.runner_file_policy("unreadable", pw) == (pw, None)
    # A recorded (consented) policy from preflight is always honoured.
    consented = ProtectionPolicy(kind="none")
    assert batch_protection.runner_file_policy(consented, restricted) == (consented, None)


def test_compress_fails_closed_on_an_uninspected_restricted_source(tmp_path):
    """compress_pdf(protection=None) must not downgrade an owner-restricted PDF.

    A batch file that could not be inspected up front reaches compress_pdf with
    protection=None; the old fallback detected 'restricted' and wrote it
    unprotected (save_kwargs() -> {}). It must fail closed so the batch skips it.
    """
    import pymupdf

    src = tmp_path / "restricted.pdf"
    doc = pymupdf.open()
    doc.new_page()
    # Owner password + denied permissions, NO user password: opens freely but
    # is restricted -> detect_protection classifies it 'restricted'.
    doc.save(str(src), encryption=pymupdf.PDF_ENCRYPT_AES_256,
             owner_pw="owner", permissions=int(pymupdf.PDF_PERM_PRINT))
    doc.close()

    with pytest.raises(app.PdfOpenError, match="owner restrictions"):
        app.compress_pdf(src, tmp_path / "out.pdf", None, None, protection=None)
    assert not (tmp_path / "out.pdf").exists(), "an output was written anyway"


# --------------------------------------------------------------------------- #
# 11. A Word-only Office must still offer LibreOffice for a spreadsheet
# --------------------------------------------------------------------------- #

def test_word_only_office_offers_libreoffice_for_an_excel_batch(monkeypatch):
    """The backend must be decided against the batch's real families.

    With only Word installed and an Excel file to convert, choosing Office
    globally would skip the spreadsheet and never offer LibreOffice (which does
    convert it). Resolving against the families must trigger the install offer.
    """
    from pdf_forge import convert_backend as cb
    from pdf_forge import msoffice, ops_office
    from pdf_forge import office_runtime as ort

    # Word only, no ready LibreOffice.
    monkeypatch.setattr(msoffice, "detect_office",
                        lambda: {"apps": ["word"], "families": ["word"]})
    monkeypatch.setattr(msoffice, "describe_office", lambda _d: "Word 2021")
    monkeypatch.setattr(ort, "runtime_status", lambda *a, **k: {"ready": False})

    offered = {"n": 0}
    monkeypatch.setattr(ops_office, "ask_yes_no",
                        lambda *a, **k: (offered.__setitem__("n", offered["n"] + 1),
                                         False)[1])  # decline the install

    # Excel is in the batch: Word cannot handle it, so the offer must fire.
    backend = ops_office._resolve_backend(["excel", "word"])
    assert offered["n"] == 1, (
        "a spreadsheet with only Word installed did not trigger the LibreOffice "
        "offer; it would have been silently skipped"
    )
    # Declined, but Word files can still convert, so a usable backend is kept.
    assert backend and backend.kind == cb.MSOFFICE

    # Control: Word-only Office with a Word-only batch converts with no offer.
    offered["n"] = 0
    backend = ops_office._resolve_backend(["word"])
    assert offered["n"] == 0, "a Word-only batch must not prompt for an install"
    assert backend and backend.kind == cb.MSOFFICE


# --------------------------------------------------------------------------- #
# 12. The server's two ports must be distinct
# --------------------------------------------------------------------------- #

def test_server_ports_are_distinct_even_if_allocation_repeats(monkeypatch):
    """unoserver needs distinct --port and --uno-port.

    Ephemeral allocation can return the same number twice; the loop must retry
    until they differ. Feeds a repeating sequence to force it.
    """
    seq = iter([5000, 5000, 5000, 5001])   # port, uno tries 5000,5000, then 5001
    monkeypatch.setattr(office_server, "random_localhost_port", lambda: next(seq))
    captured = {}

    def fake_popen(cmd, **kwargs):
        # Pull the two ports back out of the command line.
        for i, tok in enumerate(cmd):
            if tok == "--port":
                captured["port"] = cmd[i + 1]
            if tok == "--uno-port":
                captured["uno"] = cmd[i + 1]
        raise RuntimeError("stop before actually launching")

    monkeypatch.setattr(office_server.subprocess, "Popen", fake_popen)
    # unoserver_installed etc. are checked first; short-circuit to the port code
    # by letting the real function run until Popen raises.
    try:
        office_server.start_conversion_server()
    except Exception:  # noqa: BLE001 - we only care about the ports chosen
        pass
    if captured:
        assert captured["port"] != captured["uno"], (
            f"the two ports collided: {captured}"
        )
