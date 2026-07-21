from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .constants import *  # noqa: F401,F403
from .safeio import OutputResult, promote_atomically
from .core import *  # noqa: F401,F403

__all__ = ['_import_pymupdf', 'PdfOpenError', 'PdfPasswordCancelled',
           'open_source_pdf', 'source_password',
           'close_doc', '_authenticate_doc',
           'ProtectionPolicy', 'detect_protection',
           'SourceRef', 'SourceChangedError', 'capture_source', 'capture_file_source',
           'source_fingerprint',
           'write_pages_to_pdf', '_validate_written_pdf', '_validate_merged_pdf',
           'validate_page_selection_output', 'validate_protection_postcondition',
           'validate_watermark_removed', '_page_fingerprints',
           'write_merged_pdfs_to_pdf', 'resolves_to_same_file',
           'scan_image_dpi_stats', 'has_meaningful_text',
           'permission_bits', 'all_permissions', 'denied_permissions',
           'permissions_match', 'promote_atomically']


def _import_pymupdf():
    """Import PyMuPDF lazily so the core module imports without the dependency."""
    try:
        import pymupdf  # type: ignore
        return pymupdf
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'pymupdf' library is required but not installed. "
            "Run the application through Run.ps1 to install dependencies."
        ) from exc


class PdfOpenError(Exception):
    """Raised when a PDF cannot be opened or is unusable."""


class PdfPasswordCancelled(PdfOpenError):
    """Raised when the user navigates away (0/back/skip) from a password prompt.

    A subclass of :class:`PdfOpenError` so existing ``except PdfOpenError``
    handlers (single-file cancel, batch skip-and-continue) keep working.
    """


def close_doc(doc) -> None:
    """Close a PyMuPDF document if it is still open (idempotent, never raises)."""
    if doc is None:
        return
    try:
        if not getattr(doc, "is_closed", False):
            doc.close()
    except Exception:  # noqa: BLE001 - closing must never raise
        pass


def source_password(doc) -> str:
    """Return the working password captured when the document was opened.

    Empty string when the source was not encrypted. Used to reopen the same
    source silently (no prompt) inside a queued task runner. Never logged.
    """
    return getattr(doc, "_pdfforge_password", "") or ""


def _authenticate_doc(doc, password_prompt, password):
    """Authenticate an encrypted, already-opened document.

    Tries ``password`` (or the empty password) once silently. If that fails and
    ``password_prompt`` is provided, prompts **without any attempt limit**: it
    keeps asking until a correct password is entered or the user navigates away
    (the prompt returns ``None`` for 0/back/skip). ``exit``/``quit`` inside the
    prompt raise their own signal, which propagates.

    Returns the working password string on success, or ``None`` when the user
    cancels. The password is only ever held locally here and by the caller for
    a silent reopen; it is never logged.
    """
    if doc.authenticate(password or "") > 0:
        return password or ""
    if password_prompt is None:
        return None
    attempts = 0
    while True:
        entry = password_prompt(attempts > 0)  # True => a prior attempt failed
        if entry is None:
            return None  # user chose 0 / back / skip
        attempts += 1
        if doc.authenticate(entry) > 0:
            working = entry
            del entry
            return working
        del entry
        # Loop again: no maximum attempt count, no lockout, no backoff.


def open_source_pdf(path: Path, password_prompt=None, password=None):
    """Open and validate a source PDF with PyMuPDF, handling encryption.

    Args:
        path: Path to the source PDF.
        password_prompt: Optional callable ``prompt(previous_failed: bool) ->
            Optional[str]``. Called repeatedly, with no attempt limit, until it
            returns a correct password or ``None`` (the user navigated away).
        password: Optional known password tried silently first (used to reopen a
            source inside a queued runner without prompting again).

    Returns:
        A ``pymupdf.Document`` ready for reading. The working password is stashed
        on it (see :func:`source_password`) so it can be reopened silently. The
        caller may close it with :func:`close_doc`.

    Raises:
        PdfOpenError: on any failure. :class:`PdfPasswordCancelled` specifically
        when the user cancels the password prompt.
    """
    pymupdf = _import_pymupdf()

    logger.debug("Opening source PDF: '%s'", path)
    try:
        doc = pymupdf.open(str(path))
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        logger.error("PDF read error for '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF appears to be corrupted or unreadable: {exc}") from exc
    except OSError as exc:
        logger.error("OS error opening '%s': %s", path, exc)
        raise PdfOpenError(f"Could not open the file: {exc}") from exc

    working_password = ""
    needed_pass = bool(doc.needs_pass)
    if doc.needs_pass:
        logger.info("Source PDF is encrypted; authenticating.")
        try:
            working_password = _authenticate_doc(doc, password_prompt, password)
        except BaseException:
            # Includes the exit/quit signal: close the handle, then re-raise.
            close_doc(doc)
            raise
        if working_password is None:
            close_doc(doc)
            raise PdfPasswordCancelled(
                "The PDF is encrypted and no valid password was provided."
            )
        logger.info("Source PDF decrypted successfully.")

    try:
        page_count = doc.page_count
    except Exception as exc:  # noqa: BLE001
        close_doc(doc)
        logger.error("Could not determine page count for '%s': %s", path, exc)
        raise PdfOpenError(f"The PDF page count could not be determined: {exc}") from exc

    if page_count < 1:
        close_doc(doc)
        logger.error("Source PDF '%s' contains no pages.", path)
        raise PdfOpenError("The PDF contains no pages.")

    # Stash the working password so a queued runner can reopen silently. Never
    # logged, never included in summaries or task reprs. ``needs_pass`` is also
    # captured because PyMuPDF flips it to False once authenticated, so it is no
    # longer a reliable "was this locked?" signal afterwards.
    try:
        doc._pdfforge_password = working_password
        doc._pdfforge_needed_pass = bool(needed_pass)
    except Exception:  # noqa: BLE001 - best effort; silent reopen just re-prompts
        pass

    logger.info("Opened source PDF '%s' (%d page(s)).", path, page_count)
    return doc


def _file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    """SHA-256 of a file, read in chunks so a large source stays bounded."""
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(path: Path, content: bool = True) -> dict:
    """Identity of a source file at configuration time.

    Size, nanosecond mtime, the OS file id where available - and a content
    digest. Metadata alone is not enough: an in-place rewrite of the same length
    keeps the inode, and a restored timestamp keeps ``mtime_ns``, so a source
    edited between configuring an operation and running it would verify as
    unchanged and be processed as if it were the file the user chose (C-06).

    ``content=False`` skips the digest for callers that only need the cheap
    metadata (a first-pass comparison before deciding to hash).
    """
    st = os.stat(str(path))
    fingerprint = {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
    file_id = getattr(st, "st_ino", 0)
    if file_id:
        fingerprint["file_id"] = int(file_id)
        fingerprint["volume"] = int(getattr(st, "st_dev", 0))
    if content:
        fingerprint["sha256"] = _file_sha256(path)
    return fingerprint


class SourceChangedError(PdfOpenError):
    """Raised when a source changed between configuration and execution."""


@dataclass
class SourceRef:
    """Immutable description of a configured source PDF.

    Queued tasks carry one of these instead of a live ``Document``: a path, the
    captured password, the expected page count, and a fingerprint. Nothing is
    held open, so a queued (or discarded) task never keeps a file handle, and
    the runner can prove it is still operating on the same file.
    """

    path: Path
    password: str = ""
    page_count: int = 0
    fingerprint: dict = field(default_factory=dict)
    family: str = ""

    def verify_unchanged(self) -> None:
        """Raise :class:`SourceChangedError` when the file is not what we configured."""
        try:
            # Cheap metadata first; the digest is only computed when the
            # metadata still matches, which is the only case where it can tell
            # us anything new.
            current = source_fingerprint(self.path, content=False)
        except OSError as exc:
            raise SourceChangedError(
                f"'{self.path.name}' is no longer available: {exc}"
            ) from exc
        if not self.fingerprint:
            return
        for key in ("size", "mtime_ns", "file_id"):
            if key in self.fingerprint and key in current:
                if self.fingerprint[key] != current[key]:
                    raise SourceChangedError(
                        f"'{self.path.name}' changed after this task was "
                        "configured; nothing was written. Re-run the operation."
                    )
        expected = self.fingerprint.get("sha256")
        if expected:
            try:
                actual = _file_sha256(self.path)
            except OSError as exc:
                raise SourceChangedError(
                    f"'{self.path.name}' could not be re-read: {exc}"
                ) from exc
            if actual != expected:
                raise SourceChangedError(
                    f"'{self.path.name}' was edited in place after this task "
                    "was configured; nothing was written. Re-run the operation."
                )

    def open(self):
        """Reopen the source for a runner: verify, authenticate silently, check pages."""
        self.verify_unchanged()
        doc = open_source_pdf(self.path, password=self.password)
        if self.page_count and doc.page_count != self.page_count:
            actual = doc.page_count
            close_doc(doc)
            raise SourceChangedError(
                f"'{self.path.name}' now has {actual} page(s) instead of "
                f"{self.page_count}; nothing was written."
            )
        return doc

    def __repr__(self) -> str:  # never leak the password
        return f"SourceRef({self.path.name!r}, pages={self.page_count})"


def capture_file_source(path: Path, family: str = "") -> SourceRef:
    """Identity for a source PDF Forge does not open as a PDF.

    Office documents and CSV files are handed to an external converter rather
    than opened here, so there is no page count to capture - but they need the
    same protection against being edited between configuration and execution.
    ``family`` records what the file was classified as, so a source that is
    later swapped for a different kind of document is rejected as well.
    """
    return SourceRef(
        path=Path(path),
        page_count=0,
        fingerprint=source_fingerprint(path),
        family=family,
    )


def capture_source(doc, path: Path) -> SourceRef:
    """Build a :class:`SourceRef` from an open, authenticated configuration doc."""
    return SourceRef(
        path=Path(path),
        password=source_password(doc),
        page_count=doc.page_count,
        fingerprint=source_fingerprint(path),
    )


@dataclass
class ProtectionPolicy:
    """How a produced PDF should be protected (see README "Protection policy").

    ``kind`` is one of:
      * ``"none"``       - the source was unprotected; the output is unprotected.
      * ``"password"``   - the source needed an open password, which the user
        supplied, so the output is re-encrypted AES-256 with that same password
        and the source's permission bits. This is the default for single-source
        transformations: technically safe because the password is known.
      * ``"restricted"`` - the source opens freely but carries owner
        restrictions. The owner password is *not* recoverable, so the policy
        cannot be reproduced faithfully; the caller must ask the user what to do
        rather than silently dropping or inventing protection.
    """

    kind: str
    password: str = ""
    permissions: int = 0
    denied: tuple = ()

    @property
    def can_preserve(self) -> bool:
        """True when the output can faithfully reproduce the source policy."""
        return self.kind == "password"

    @property
    def is_protected(self) -> bool:
        return self.kind in ("password", "restricted")

    def save_kwargs(self) -> dict:
        """PyMuPDF ``save()`` kwargs that reproduce this policy (or ``{}``)."""
        if self.kind != "password":
            return {}
        pymupdf = _import_pymupdf()
        return {
            "encryption": pymupdf.PDF_ENCRYPT_AES_256,
            "user_pw": self.password,
            "owner_pw": self.password,
            "permissions": int(self.permissions),
        }


def permission_bits() -> dict:
    """The single PDF permission table: human-readable action -> permission bit.

    Every module that needs permission names or bits derives them from here, so
    the eight ``PDF_PERM_*`` flags are listed exactly once.
    """
    pymupdf = _import_pymupdf()
    return {
        "printing": pymupdf.PDF_PERM_PRINT,
        "high-quality printing": pymupdf.PDF_PERM_PRINT_HQ,
        "copying text/images": pymupdf.PDF_PERM_COPY,
        "editing content": pymupdf.PDF_PERM_MODIFY,
        "annotating / comments": pymupdf.PDF_PERM_ANNOTATE,
        "filling form fields": pymupdf.PDF_PERM_FORM,
        "assembling pages": pymupdf.PDF_PERM_ASSEMBLE,
        "accessibility extraction": pymupdf.PDF_PERM_ACCESSIBILITY,
    }


def all_permissions() -> int:
    """The permission bitmask that allows every action."""
    bits = 0
    for bit in permission_bits().values():
        bits |= int(bit)
    return bits


def denied_permissions(doc) -> list:
    """Human-readable actions the (opened) document forbids."""
    return [name for name, bit in permission_bits().items()
            if not (doc.permissions & bit)]


def permissions_match(observed: int, expected: int) -> bool:
    """True when every supported permission bit agrees (bit-by-bit).

    Compares only the eight ``PDF_PERM_*`` bits, never the raw signed permission
    word: PDF permission integers carry required high bits set to 1, so an
    int-equality compare would falsely reject a correct file (``doc.permissions``
    reads negative for a normal restricted PDF).
    """
    return all(bool(observed & int(bit)) == bool(expected & int(bit))
               for bit in permission_bits().values())


def detect_protection(doc) -> ProtectionPolicy:
    """Classify an opened source document's protection into a policy.

    Uses the ``needs_pass`` value captured at open time (PyMuPDF clears it after
    authentication) plus the live permission bits.
    """
    needed_pass = bool(getattr(doc, "_pdfforge_needed_pass", False))
    try:
        permissions = int(doc.permissions)
    except Exception:  # noqa: BLE001
        permissions = all_permissions()
    all_bits = all_permissions()
    denied = tuple(
        name for name, bit in permission_bits().items() if not (permissions & bit)
    )
    if needed_pass:
        return ProtectionPolicy(
            kind="password",
            password=source_password(doc),
            permissions=permissions,
            denied=denied,
        )
    if permissions != all_bits and denied:
        return ProtectionPolicy(
            kind="restricted", permissions=permissions, denied=denied
        )
    return ProtectionPolicy(kind="none", permissions=all_bits)


def _save_doc_to_path_safely(out_doc, out_path: Path, expected_pages: int,
                             validate, protection: "ProtectionPolicy" = None,
                             deep_validate=None) -> Path:
    """Save a document via temp file -> validate -> atomic rename.

    Returns the path actually written, which is not always ``out_path``: when
    the requested name appeared between configuration and execution, no-clobber
    promotion allocates a suffixed sibling. The caller must use the returned
    value for reporting, logging and any further validation.

    ``protection`` (optional) re-applies the source's encryption policy to the
    output so a transformation never silently strips protection.

    ``deep_validate`` (optional) receives the STAGING path and raises to reject
    the output. It runs before the final name is claimed, so an output that
    fails a semantic check never becomes a user-visible file and never enters
    the generated-output manifest. Validating after promotion would leave the
    rejected file on disk, and - when the requested name was taken - would
    inspect a file this run did not write.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    logger.debug("Temporary write file: '%s'", tmp_path)
    extra = protection.save_kwargs() if protection is not None else {}
    try:
        # garbage=3 deduplicates unused/identical objects; deflate compresses
        # streams. Cheap wins on every output, identical visual result.
        out_doc.save(str(tmp_path), garbage=3, deflate=True, **extra)
        validate(tmp_path, expected_pages=expected_pages,
                 password=(protection.password if extra else None))
        logger.debug("Validated temporary output (%d page(s)).", expected_pages)
        if deep_validate is not None:
            deep_validate(tmp_path)
        # Atomic promotion, and the manifest entry, happen only once every
        # check has passed against the bytes that will become the output.
        return promote_atomically(tmp_path, out_path)
    except BaseException:  # incl. Ctrl+C: a leaked temp survives otherwise
        try:
            if tmp_path.exists():
                tmp_path.unlink()
                logger.debug("Removed temporary file after failure: '%s'", tmp_path)
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise


def write_pages_to_pdf(doc, pages_zero_based: Sequence[int], out_path: Path,
                       progress=None,
                       protection: "ProtectionPolicy" = None) -> OutputResult:
    """Write the given 0-based pages to ``out_path`` using a safe temp file.

    The data is written to a temporary file in the destination directory, fully
    validated there - shallow structure, exact page selection and order, and the
    decided protection - and only then atomically promoted. Temporary files are
    removed on failure. ``protection`` re-applies the source's encryption policy
    so a page transformation never silently strips it.

    Returns an :class:`OutputResult` whose ``path`` is the file actually
    written; it differs from ``out_path`` when the requested name was taken.
    """
    pymupdf = _import_pymupdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    total = len(pages_zero_based)
    logger.debug("Writing %d page(s) to '%s'.", total, out_path)

    out_doc = pymupdf.open()
    try:
        for index, page_index in enumerate(pages_zero_based, start=1):
            out_doc.insert_pdf(doc, from_page=page_index, to_page=page_index)
            if progress is not None:
                progress(index, total)
        def _deep(staging: Path) -> None:
            # The output must hold exactly the selected pages, in order, with
            # exactly the protection that was decided - checked on the staging
            # bytes, before any user-visible name exists (PF-035, C-02).
            validate_page_selection_output(
                staging, doc, pages_zero_based,
                password=getattr(protection, "password", "") or None,
            )
            validate_protection_postcondition(staging, protection)

        written = _save_doc_to_path_safely(
            out_doc, out_path, total, _validate_written_pdf,
            protection=protection, deep_validate=_deep,
        )
    finally:
        out_doc.close()

    elapsed = time.perf_counter() - started
    logger.info("Wrote '%s' (%d page(s)) in %.2fs.", written, total, elapsed)
    return OutputResult(path=written, count=total)


def _page_fingerprints(doc):
    """A cheap per-page identity used to verify page *order*, not just count.

    Folds together the page's media box, rotation, extracted text, the
    content-stream *bytes*, and a low-DPI render of the page:

      * the content stream is hashed as bytes, not by length - two textless
        vector pages whose draw commands are the same byte length differ;
      * a content stream references resources by NAME (``/Im0 Do``), and two
        pages can carry byte-identical streams while those names resolve to
        different - or swapped - images (page A draws red-left/green-right, page
        B green-left/red-right). Folding in the *rendered* pixels captures the
        actual name->object binding and layout that a hash of the resource
        digests (however ordered) cannot (N-07).

    The render is safe here precisely because the validator only ever compares a
    SOURCE page against an OUTPUT page in the SAME process and the SAME PyMuPDF
    build: an extracted page renders byte-identically to its source (verified
    round-trip), so a correct output is never falsely rejected, while a swapped
    or wrong page renders differently and is caught. Cross-machine raster
    differences do not matter because no cross-machine comparison ever happens.
    Byte-identical blank pages still compare equal. 36 DPI keeps a 300-page sweep
    well under a tenth of a second.
    """
    import hashlib

    pymupdf = _import_pymupdf()
    prints = []
    for index in range(doc.page_count):
        page = doc[index]
        try:
            text = page.get_text()
        except Exception:  # noqa: BLE001 - a broken page must not kill validation
            text = ""
        try:
            stream = b"".join(doc.xref_stream(x) or b""
                              for x in page.get_contents())
        except Exception:  # noqa: BLE001
            stream = b""
        try:
            render = page.get_pixmap(
                dpi=36, colorspace=pymupdf.csRGB, alpha=False).samples
        except Exception:  # noqa: BLE001 - an unrenderable page degrades, not aborts
            render = b""
        rect = page.rect
        digest = hashlib.sha256()
        digest.update(
            f"{round(rect.width, 2)}x{round(rect.height, 2)}|{page.rotation}|"
            .encode("utf-8"))
        digest.update(hashlib.sha256(stream).digest())
        digest.update(text.encode("utf-8", "replace"))
        digest.update(hashlib.sha256(render).digest())
        prints.append(digest.hexdigest())
    return prints


def validate_page_selection_output(out_path: Path, source_doc, pages_zero_based,
                                   password=None) -> None:
    """Verify an extract/split/delete output really holds the selected pages.

    A page-count check alone passes for a file with the *right number* of the
    *wrong* pages - a silent data defect. This compares per-page fingerprints
    against the source, in order (PF-035).
    """
    pymupdf = _import_pymupdf()
    # Hoisted: _page_fingerprints sweeps the whole document, so calling it
    # inside the comprehension re-swept once per selected page. Measured on a
    # 300-page source with 299 pages selected: 14.25s vs 0.04s, 364x, with no
    # progress output - long enough to look like a hang.
    source_prints = _page_fingerprints(source_doc)
    expected = [source_prints[i] for i in pages_zero_based]
    check = pymupdf.open(str(out_path))
    try:
        if check.needs_pass and not check.authenticate(password or ""):
            raise PdfOpenError(
                "Output validation failed: the protected output could not be "
                "reopened with its own password."
            )
        actual = _page_fingerprints(check)
    finally:
        check.close()
    if len(actual) != len(expected):
        raise PdfOpenError(
            f"Output validation failed: expected {len(expected)} page(s), "
            f"found {len(actual)}."
        )
    for position, (want, got) in enumerate(zip(expected, actual), start=1):
        if want != got:
            raise PdfOpenError(
                f"Output validation failed: page {position} of the output does "
                "not match the selected source page (wrong page or wrong order)."
            )


def validate_protection_postcondition(out_path: Path, policy) -> None:
    """Verify the produced file carries exactly the protection that was decided.

    Catches both directions: protection silently dropped, and protection
    unexpectedly present.
    """
    pymupdf = _import_pymupdf()
    check = pymupdf.open(str(out_path))
    try:
        needs_pass = bool(check.needs_pass)
        expects_password = bool(policy is not None and getattr(policy, "password", ""))
        if expects_password and not needs_pass:
            raise PdfOpenError(
                "Output validation failed: the output should require a password "
                "but does not - protection was lost."
            )
        if not expects_password and needs_pass:
            raise PdfOpenError(
                "Output validation failed: the output unexpectedly requires a "
                "password."
            )
        if needs_pass and not check.authenticate(policy.password):
            raise PdfOpenError(
                "Output validation failed: the protected output could not be "
                "reopened with its own password."
            )
        # Deliberately no permission-bit comparison here. save_kwargs() sets
        # owner_pw = user_pw, so authenticating above grants *owner* access and
        # check.permissions then reports every bit as allowed no matter what
        # the file was written with - a comparison against policy.permissions
        # can never fail, and adding one would be a guard that cannot fire.
        # The genuine limitation (an owner-restricted source cannot be
        # reproduced, because its owner password is unrecoverable) is surfaced
        # where it can still be acted on: resolve_protection warns and asks
        # before anything is written. See test_confirmed_defects.py.
    finally:
        check.close()


def validate_watermark_removed(out_path: Path, signatures, password=None) -> None:
    """Verify the chosen watermark signature is absent from the saved file.

    Removal reporting a page count is not evidence; this reopens the result and
    fails if the target image is still painted anywhere (PF-013/PF-035).
    """
    from .watermark import _image_identity, _painted_images

    pymupdf = _import_pymupdf()
    targets = set(signatures)
    check = pymupdf.open(str(out_path))
    try:
        if check.needs_pass and not check.authenticate(password or ""):
            raise PdfOpenError(
                "Output validation failed: the protected output could not be "
                "reopened with its own password."
            )
        for page_index in range(check.page_count):
            for item in _painted_images(check, page_index):
                if _image_identity(item) in targets:
                    raise PdfOpenError(
                        "Output validation failed: the selected watermark is "
                        f"still present on page {page_index + 1}."
                    )
    finally:
        check.close()


def _validate_written_pdf(path: Path, expected_pages: int, password=None) -> None:
    """Reopen a freshly written PDF and confirm its page count.

    ``password`` is supplied when the output was deliberately encrypted (the
    preserved source policy), so validation can authenticate before reading.
    """
    pymupdf = _import_pymupdf()
    check = pymupdf.open(str(path))
    try:
        if check.needs_pass and not check.authenticate(password or ""):
            raise PdfOpenError(
                "Output validation failed: the protected output could not be "
                "reopened with its own password."
            )
        actual = check.page_count
    finally:
        check.close()
    if actual != expected_pages:
        raise PdfOpenError(
            f"Output validation failed: expected {expected_pages} pages, "
            f"found {actual}."
        )


def _validate_merged_pdf(path: Path, expected_pages: int, password=None) -> None:
    """Reopen a freshly merged PDF and confirm it is usable.

    Verifies the output can be opened (authenticating when it was deliberately
    protected), is not *unexpectedly* encrypted, and has the expected page count.
    """
    pymupdf = _import_pymupdf()
    check = pymupdf.open(str(path))
    try:
        if check.needs_pass:
            if not password or not check.authenticate(password):
                raise PdfOpenError(
                    "Output validation failed: the merged PDF is encrypted."
                )
        actual = check.page_count
    finally:
        check.close()
    if actual != expected_pages:
        raise PdfOpenError(
            f"Output validation failed: expected {expected_pages} pages, "
            f"found {actual}."
        )


def write_merged_pdfs_to_pdf(docs, out_path: Path, progress=None,
                             protection: "ProtectionPolicy" = None) -> int:
    """Merge already-opened PDF documents into a single PDF at ``out_path``.

    Pages from each document are appended in order. The data is written to a
    temporary file in the destination directory, validated (openable, not
    encrypted, correct page count), then atomically renamed to the final path.
    Temporary files are removed on failure. Returns the total pages written.
    """
    pymupdf = _import_pymupdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total = sum(d.page_count for d in docs)
    started = time.perf_counter()
    logger.debug(
        "Merging %d source document(s), %d total page(s) into '%s'.",
        len(docs), total, out_path,
    )

    out_doc = pymupdf.open()
    written = 0
    try:
        for doc_index, doc in enumerate(docs, start=1):
            page_count = doc.page_count
            out_doc.insert_pdf(doc)
            written += page_count
            if progress is not None:
                progress(written, total)
            logger.debug(
                "Appended source %d/%d (%d page(s); running total=%d).",
                doc_index, len(docs), page_count, written,
            )
        written_path = _save_doc_to_path_safely(
            out_doc, out_path, total, _validate_merged_pdf,
            protection=protection,
        )
    finally:
        out_doc.close()

    elapsed = time.perf_counter() - started
    logger.info(
        "Merged %d source(s) -> '%s' (%d page(s)) in %.2fs.",
        len(docs), written_path, total, elapsed,
    )
    return OutputResult(path=written_path, count=total)


def scan_image_dpi_stats(doc, max_pages: int = 40):
    """Measure the effective DPI of raster images as placed on the pages.

    A PDF has no single DPI; only placed raster images have one:
    ``image_pixels / displayed_inches``. Pages are sampled evenly (up to
    ``max_pages``) to stay fast on huge documents.

    Returns a dict ``{min, max, median, count, pages_scanned}`` (DPI values
    rounded to int), or ``None`` when no meaningful raster image is found
    (i.e. a text/vector PDF).
    """
    dpis = []
    page_count = doc.page_count
    step = max(1, -(-page_count // max_pages))  # ceil division
    pages_scanned = 0
    for page_index in range(0, page_count, step):
        pages_scanned += 1
        try:
            infos = doc[page_index].get_image_info()
        except Exception:  # noqa: BLE001 - a broken page must not kill the scan
            continue
        for info in infos:
            x0, y0, x1, y1 = info["bbox"]
            width_in, height_in = (x1 - x0) / 72.0, (y1 - y0) / 72.0
            # Ignore tiny placements (icons/artifacts) - not meaningful for DPI.
            if width_in < 0.15 or height_in < 0.15:
                continue
            dpis.append(min(info["width"] / width_in, info["height"] / height_in))
    if not dpis:
        return None
    dpis.sort()
    return {
        "min": round(dpis[0]),
        "max": round(dpis[-1]),
        "median": round(dpis[len(dpis) // 2]),
        "count": len(dpis),
        "pages_scanned": pages_scanned,
    }


def has_meaningful_text(doc, max_pages: int = 10, min_chars: int = 40) -> bool:
    """Return True when sampled pages contain real extractable text.

    Used to tell text/vector PDFs (rendering DPI adds sharpness) apart from
    scanned/image-only PDFs (rendering above the scan DPI adds nothing).
    """
    page_count = doc.page_count
    step = max(1, -(-page_count // max_pages))
    for page_index in range(0, page_count, step):
        try:
            if len(doc[page_index].get_text().strip()) >= min_chars:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def resolves_to_same_file(a: Path, b: Path) -> bool:
    """Return True when two paths resolve to the same file on disk."""
    try:
        return os.path.realpath(str(a)).lower() == os.path.realpath(str(b)).lower() \
            if os.name == "nt" else \
            os.path.realpath(str(a)) == os.path.realpath(str(b))
    except OSError:
        return False
