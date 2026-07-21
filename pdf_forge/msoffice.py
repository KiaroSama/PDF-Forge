"""Convert documents to PDF through an installed Microsoft Office (Windows).

This is the *preferred* conversion backend when Microsoft Office is present: it
costs no extra disk space, it is the native renderer for these formats, and it
avoids a 1.6 GB LibreOffice runtime entirely. The project-local LibreOffice in
``office_runtime`` stays as the fallback for machines without Office.

The session object here plays the same role as the unoserver handle so both
backends plug into one conversion loop: create it once per batch, convert many
files through it, stop it at the end.

Safety notes:

* Macros are force-disabled in every application before a file is opened, and
  automatic link/index updates are suppressed - the same guarantees the
  LibreOffice path makes.
* Passwords are passed as in-process COM arguments. They never reach a command
  line, an environment variable, or a log record.
* A file is opened read-only; nothing is written back to the source.
* Every application is quit in a ``finally`` path, so a failed conversion does
  not leave a headless Office process behind.
"""
from __future__ import annotations

import codecs
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional

from .constants import LOG_PREFIX
from .office import validate_office_file
from .office_decrypt import (
    DecryptError, DecryptPasswordError, decrypt_to_temp,
)

logger = logging.getLogger(LOG_PREFIX)

__all__ = [
    'MsOfficeError', 'MsOfficePasswordError', 'MsOfficeSession', 'detect_office',
    'is_available', 'start_session', 'convert_to_pdf', 'describe_office',
    'office_families',
]

# msoAutomationSecurityForceDisable: open with macros disabled regardless of the
# user's trust-centre configuration.
_MSO_FORCE_DISABLE = 3

# ppUpdateOptionManual: a linked shape refreshes only when asked, never on its
# own during open or export.
_PP_UPDATE_MANUAL = 2

# Supplied to PowerPoint's "path::password::" open form for a file believed to
# be unencrypted. It only has to be the wrong password, so an unexpectedly
# encrypted presentation fails instead of raising a modal prompt that would
# hang a headless run. No NUL bytes: those would truncate the path string.
_PP_WRONG_PASSWORD = "pdfforge-no-prompt"

# Chunk size for streaming file copies.
_COPY_CHUNK = 1 << 20

# A password that no real file uses. Supplying it makes Office fail with an
# error on an encrypted file instead of blocking on a modal password dialog,
# which would hang a headless run forever.
_REFUSE_PROMPT_PASSWORD = "\0pdfforge-no-prompt\0"

_PROGIDS = {
    "word": "Word.Application",
    "excel": "Excel.Application",
    "powerpoint": "PowerPoint.Application",
}

# csv is opened by Excel; it has no application of its own.
_FAMILY_APP = {"word": "word", "excel": "excel", "csv": "excel",
               "powerpoint": "powerpoint"}


class MsOfficeError(RuntimeError):
    """Microsoft Office is unusable, or a conversion failed."""


class MsOfficePasswordError(MsOfficeError):
    """The supplied password does not open this file."""


def _csv_with_bom(path: Path, temp_dir: Path) -> Path:
    """Give Excel a CSV it will decode as UTF-8 instead of the ANSI code page.

    Excel guesses a plain CSV's encoding from the system code page, so a UTF-8
    file without a byte-order mark comes out as mojibake for anything non-Latin
    (measured: "سلام" imported as "Ø³Ù„Ø§Ù…"). A BOM is the documented signal that
    makes Excel decode UTF-8, so a prefixed copy is handed over instead. The
    source file is never modified.

    A file that already starts with a BOM is passed through untouched, and a
    file that is not valid UTF-8 is left alone as well - guessing a different
    encoding here would corrupt data the CSV pipeline already resolved.
    """
    path = Path(path)
    target = Path(temp_dir) / path.name
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        with path.open("rb") as source:
            head = source.read(3)
            if head.startswith(b"\xef\xbb\xbf"):
                return path
            # Copy in chunks, validating as we go. Reading the whole file peaked
            # at roughly three times its size (measured: 22 MB for a 5.6 MB CSV)
            # - exactly the memory problem the main normalizer was made
            # streaming to avoid (C-16).
            with target.open("wb") as out:
                out.write(b"\xef\xbb\xbf")
                chunk = head
                while chunk:
                    try:
                        decoder.decode(chunk, False)
                    except UnicodeDecodeError:
                        # Not UTF-8 after all: leave the original alone rather
                        # than mislabelling its encoding with a BOM.
                        return _abandon(target, path)
                    out.write(chunk)
                    chunk = source.read(_COPY_CHUNK)
                try:
                    decoder.decode(b"", True)
                except UnicodeDecodeError:
                    return _abandon(target, path)
    except OSError as exc:
        _abandon(target, path)
        raise MsOfficeError(f"The CSV copy could not be written: {exc}") from exc
    return target


def _abandon(target: Path, original: Path) -> Path:
    """Drop a partial copy and fall back to the original file."""
    try:
        target.unlink()
    except OSError:
        pass
    return original


def _quit_quietly(app, what: str) -> None:
    """Release an application object during error handling; never raises."""
    try:
        app.Quit()
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the cause
        logger.debug("Could not quit Microsoft %s: %s", what, exc)


def _com():
    """Import the COM bindings, or explain precisely what is missing."""
    try:
        import pythoncom  # noqa: F401
        import win32com.client as win32
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MsOfficeError(
            "The Windows COM bindings (pywin32) are not installed, so an "
            "installed Microsoft Office cannot be used."
        ) from exc
    return win32


def _com_error():
    """The pywintypes.com_error type, or a class that never matches off Windows.

    A tuple of exception types is always valid in an ``except`` clause, so on a
    machine without pywin32 this degrades to catching nothing rather than
    raising at import time.
    """
    try:
        import pywintypes
        return pywintypes.com_error
    except ImportError:  # pragma: no cover - environment dependent
        return ()


# HRESULTs Office raises when a document needs a password to open. Word/Excel
# surface 0x800A1651 (wdRejected / "password is incorrect"); the generic
# E_ACCESSDENIED shows up for some protected containers.
_PASSWORD_HRESULTS = frozenset({-2146822575, -2147024891})


def _is_password_hresult(exc) -> bool:
    """True when a com_error is Office refusing a password-protected document."""
    code = getattr(exc, "hresult", None)
    if code is None:
        args = getattr(exc, "args", ())
        code = args[0] if args else None
    try:
        return int(code) in _PASSWORD_HRESULTS
    except (TypeError, ValueError):
        return False


def _com_message(exc) -> str:
    """A short, non-sensitive description of a com_error for the user."""
    args = getattr(exc, "args", ())
    if len(args) >= 2 and args[1]:
        return str(args[1])
    return "the application reported an error"


def detect_office() -> Optional[Dict[str, object]]:
    """Report which Microsoft Office applications can actually be automated.

    Availability is decided by COM registration (the ProgID -> CLSID mapping in
    HKEY_CLASSES_ROOT), not by looking for files on disk: a leftover Office
    folder from a removed installation must not read as usable. The lookup is a
    registry read, so detection costs nothing and never starts an application.

    Returns ``None`` when Office cannot be used at all.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return None
    # COM automation itself needs pywin32; without it Office is unusable even
    # when installed, and reporting it as available would mislead the user.
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return None

    available = []
    for family, progid in _PROGIDS.items():
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID"):
                available.append(family)
        except OSError:
            continue
    if not available:
        return None
    return {"apps": available, "families": office_families(available)}


def office_families(apps) -> list:
    """Map available applications to the source families they can convert."""
    apps = set(apps)
    return sorted({fam for fam, app in _FAMILY_APP.items() if app in apps})


def is_available() -> bool:
    return detect_office() is not None


def describe_office(detected: Optional[Dict[str, object]] = None) -> str:
    """One-line human description, e.g. "Word, Excel, PowerPoint"."""
    detected = detected or detect_office()
    if not detected:
        return "not installed"
    names = {"word": "Word", "excel": "Excel", "powerpoint": "PowerPoint"}
    return ", ".join(names[a] for a in detected["apps"])  # type: ignore[index]


class MsOfficeSession:
    """Holds the Office applications used by one conversion batch.

    Applications are created lazily - converting only spreadsheets never starts
    Word - and reused across the batch, because starting an Office application
    costs several seconds.
    """

    def __init__(self) -> None:
        self._apps: Dict[str, object] = {}
        self._co_initialised = False

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> "MsOfficeSession":
        import pythoncom

        # A queued task runs on a worker thread, and COM must be initialised on
        # the thread that will use it.
        try:
            pythoncom.CoInitialize()
            self._co_initialised = True
        except Exception as exc:  # noqa: BLE001 - already initialised is fine
            logger.debug("CoInitialize returned: %s", exc)
        return self

    def stop(self) -> None:
        for family, app in list(self._apps.items()):
            try:
                app.Quit()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning("Could not quit Microsoft %s: %s", family, exc)
            self._apps.pop(family, None)
        if self._co_initialised:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception as exc:  # noqa: BLE001
                logger.debug("CoUninitialize returned: %s", exc)
            self._co_initialised = False

    def __enter__(self) -> "MsOfficeSession":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- applications ---------------------------------------------------- #
    def _app(self, family: str):
        app_name = _FAMILY_APP.get(family)
        if app_name is None:
            raise MsOfficeError(f"Unsupported source family: {family}")
        if app_name in self._apps:
            return self._apps[app_name]

        win32 = _com()
        try:
            # DispatchEx forces a dedicated process instead of attaching to an
            # Office instance the user already has open, so the user's own
            # documents and settings are never touched.
            app = win32.DispatchEx(_PROGIDS[app_name])
        except Exception as exc:  # noqa: BLE001
            raise MsOfficeError(
                f"Microsoft {app_name.title()} could not be started: {exc}"
            ) from exc

        # Cosmetic settings may legitimately fail: PowerPoint rejects
        # Visible=False on some builds, and a failure there costs nothing.
        for attribute, value in (("Visible", False), ("DisplayAlerts", False)):
            try:
                setattr(app, attribute, value)
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s.%s could not be set: %s", app_name, attribute, exc)

        # AutomationSecurity is not cosmetic: it is the setting that stops a
        # macro running when the document is opened. Swallowing a failure here
        # meant a document could be opened under the machine's Trust Center
        # default while the tool still claimed macros were disabled (C-12). Fail
        # closed instead - the application is discarded and the job aborts
        # before any source is opened.
        try:
            app.AutomationSecurity = _MSO_FORCE_DISABLE
            applied = int(app.AutomationSecurity)
        except Exception as exc:  # noqa: BLE001
            _quit_quietly(app, app_name)
            raise MsOfficeError(
                f"Macro security could not be enforced on Microsoft "
                f"{app_name.title()} ({exc}); no document was opened."
            ) from exc
        if applied != _MSO_FORCE_DISABLE:
            _quit_quietly(app, app_name)
            raise MsOfficeError(
                f"Microsoft {app_name.title()} did not accept the macro-disable "
                f"setting (reported {applied}); no document was opened."
            )
        self._apps[app_name] = app
        return app

    # -- conversion ------------------------------------------------------ #
    def convert(self, src: Path, out: Path, family: str,
                password: Optional[str] = None, encrypted: bool = False) -> None:
        src, out = Path(src), Path(out)
        app_name = _FAMILY_APP.get(family)
        if app_name is None:
            raise MsOfficeError(f"Unsupported source family: {family}")
        handler = {"word": _convert_word, "excel": _convert_excel,
                   "powerpoint": _convert_powerpoint}[app_name]
        with tempfile.TemporaryDirectory(prefix="pdfforge_msoffice_") as scratch:
            scratch_dir = Path(scratch)
            if encrypted:
                # Office never sees the encrypted file - see office_decrypt.
                try:
                    src = decrypt_to_temp(src, password, scratch_dir)
                except DecryptPasswordError as exc:
                    raise MsOfficePasswordError(str(exc)) from None
                except DecryptError as exc:
                    raise MsOfficeError(str(exc)) from None
                # The decrypted bytes may be a different family than the
                # extension claimed (an xlsx saved as .docx). The LibreOffice
                # backend rejects that; this one must too, or Office is handed a
                # spreadsheet and told it is a Word document. Non-CSV only: a
                # decrypted CSV is plain text with no container to match.
                if family != "csv":
                    ok, reason = validate_office_file(src, expected_family=family)
                    if not ok:
                        raise MsOfficeError(
                            f"'{Path(out).name}' decrypted but is not a {family} "
                            f"file ({reason}); it was not converted."
                        )
            if family == "csv":
                src = _csv_with_bom(src, scratch_dir)
            # Office *renames* an export target whose extension is not .pdf
            # (asking for "x.convert.tmp" silently writes "x.convert.tmp.pdf"),
            # so it always exports to a .pdf path we control and the result is
            # moved to whatever the caller asked for.
            produced = scratch_dir / "converted.pdf"
            app = self._app(family)
            # Even a file believed to be unencrypted must not be able to raise a
            # password dialog; the sentinel makes Office fail instead of block.
            #
            # Translate COM failures here, in the one place all three handlers
            # funnel through. A pywintypes.com_error is a bare Exception, not an
            # MsOfficeError, so an untranslated one escaped every caller's
            # except and aborted the whole batch with a raw HRESULT and a leaked
            # scratch dir. A password-to-open legacy .doc reaches Office with
            # the refusal sentinel and fails exactly this way.
            try:
                handler(app, src, produced, _REFUSE_PROMPT_PASSWORD)
            except _com_error() as exc:
                if _is_password_hresult(exc):
                    raise MsOfficePasswordError(
                        f"'{Path(out).name}' is password-protected and could "
                        "not be opened."
                    ) from exc
                raise MsOfficeError(
                    f"Microsoft {app_name.title()} could not convert "
                    f"'{Path(out).name}': {_com_message(exc)}"
                ) from exc
            if not produced.exists() or produced.stat().st_size == 0:
                raise MsOfficeError(
                    "Microsoft Office reported success but produced no output "
                    "file."
                )
            out.parent.mkdir(parents=True, exist_ok=True)
            # shutil.move, not os.replace: the scratch directory lives in %TEMP%
            # and the destination is wherever the user's file is, so the two are
            # routinely on different volumes - os.replace then raises "cannot
            # move the file to a different disk drive" and the whole batch dies.
            # Every test writes under tmp_path, which is on the same volume as
            # %TEMP%, which is why the suite never saw it.
            shutil.move(str(produced), str(out))


# RPC_E_DISCONNECTED: after ExportAsFixedFormat, Office sometimes releases the
# document object itself. The export has already succeeded and the process does
# not leak (verified), so this specific failure means "already closed".
_ALREADY_CLOSED = -2147417848


def _close_quietly(handle, what: str, close) -> None:
    """Close a document/workbook/presentation, tolerating an already-closed one."""
    if handle is None:
        return
    try:
        close()
    except Exception as exc:  # noqa: BLE001 - a close failure must not lose the output
        code = getattr(exc, "hresult", None) or (exc.args[0] if exc.args else None)
        if code == _ALREADY_CLOSED:
            logger.debug("%s was already released by Office.", what)
            return
        logger.warning("Could not close the %s: %s", what, exc)


def _convert_word(app, src: Path, out: Path, secret: str) -> None:
    doc = None
    try:
        # wdUpdateLinksNever = 0. Passed to Open, and the application-level
        # options are cleared as well, so neither a linked field nor an OLE link
        # can reach out while the document is exported (C-12).
        for option, value in (("UpdateLinksAtOpen", False),
                              ("UpdateFieldsAtPrint", False),
                              ("UpdateLinksAtPrint", False)):
            try:
                setattr(app.Options, option, value)
            except Exception as exc:  # noqa: BLE001 - option set varies by build
                logger.debug("Word option %s could not be set: %s", option, exc)
        doc = app.Documents.Open(
            str(src), ConfirmConversions=False, ReadOnly=True,
            AddToRecentFiles=False, PasswordDocument=secret,
            WritePasswordDocument=secret, Visible=False,
        )
        doc.ExportAsFixedFormat(str(out), 17)  # wdExportFormatPDF
    finally:
        _close_quietly(doc, "Word document", lambda: doc.Close(0))


def _convert_excel(app, src: Path, out: Path, secret: str) -> None:
    book = None
    try:
        book = app.Workbooks.Open(
            str(src), UpdateLinks=0, ReadOnly=True, Password=secret,
            WriteResPassword=secret, IgnoreReadOnlyRecommended=True,
        )
        book.ExportAsFixedFormat(0, str(out))  # xlTypePDF
    finally:
        _close_quietly(book, "workbook", lambda: book.Close(False))


def _convert_powerpoint(app, src: Path, out: Path, secret: str) -> None:
    presentation = None
    try:
        # PowerPoint's Open has no password parameter, so the anti-hang sentinel
        # Word and Excel receive cannot be passed as a keyword - it was simply
        # dropped, and an unexpectedly encrypted legacy .ppt (which
        # is_encrypted_office_file does not detect) would raise a modal prompt
        # and hang a headless run. The documented channel is the filename form
        # "path::password::". The real sentinel carries NUL bytes, which would
        # truncate that string, so a plain unguessable token is used here: its
        # only job is to be wrong, so Office fails instead of prompting.
        guarded = f"{src}::{_PP_WRONG_PASSWORD}::"
        presentation = app.Presentations.Open(
            guarded, ReadOnly=True, Untitled=True, WithWindow=False,
        )
        # Stop linked pictures and OLE objects updating during export (C-12).
        #
        # NOT Presentation.UpdateLinks(): despite the name that is an
        # argument-less *action* that refreshes every linked object, i.e. it
        # performs the outbound fetch this is meant to prevent. Word and Excel
        # take real suppression parameters (UpdateLinksAtOpen=False,
        # UpdateLinks=0); PowerPoint's Open has no equivalent, so each linked
        # shape is switched to manual updating instead.
        for shape in presentation.Shapes:
            try:
                shape.LinkFormat.AutoUpdate = _PP_UPDATE_MANUAL
            except Exception:  # noqa: BLE001 - unlinked shapes have no LinkFormat
                continue
        presentation.SaveAs(str(out), 32)  # ppSaveAsPDF
    finally:
        _close_quietly(presentation, "presentation", lambda: presentation.Close())


def start_session() -> MsOfficeSession:
    return MsOfficeSession().start()


def convert_to_pdf(session: MsOfficeSession, src: Path, out: Path, family: str,
                   password: Optional[str] = None, encrypted: bool = False) -> None:
    """Convert one file, mirroring ``office_runtime.convert_to_pdf``."""
    session.convert(src, out, family, password=password, encrypted=encrypted)
