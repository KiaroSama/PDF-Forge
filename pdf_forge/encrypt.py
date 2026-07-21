from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .constants import *  # noqa: F401,F403
from .safeio import OutputResult, promote_atomically
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403

__all__ = ['restrictable_actions', 'save_encrypted_pdf']


def restrictable_actions() -> List[Tuple[str, int]]:
    """User-selectable actions that can be blocked, as ``(label, bits)`` pairs.

    Derived from :func:`permission_bits`. Both print bits are offered as one
    "printing" choice, and accessibility (screen-reader text extraction) is
    intentionally omitted: it is always kept allowed so protected files stay
    accessible.
    """
    bits = permission_bits()
    return [
        ("printing", int(bits["printing"] | bits["high-quality printing"])),
        ("copying text/images", int(bits["copying text/images"])),
        ("editing content", int(bits["editing content"])),
        ("annotating / comments", int(bits["annotating / comments"])),
        ("filling form fields", int(bits["filling form fields"])),
        ("assembling pages", int(bits["assembling pages"])),
    ]


def _validate_encrypted(path: Path, expected_pages: int, password: Optional[str],
                        expected_permissions: Optional[int] = None) -> None:
    """Reopen an encrypted output; verify auth, page count and (when given) that
    the requested permission mask actually took effect.

    The permission check is bit-by-bit (:func:`permissions_match`): for a
    Restrict output (no open password) the bits are read in the public,
    non-owner state a reader actually sees, which is exactly where a writer that
    silently dropped the restrictions would be caught. For an open-password
    output where ``user_pw == owner_pw`` the reopen authenticates into the owner
    state, so the mask read back is the (all-allowed) owner view - which is why
    the current open-password flow only ever requests an all-allowed mask.
    """
    pymupdf = _import_pymupdf()
    check = pymupdf.open(str(path))
    try:
        if check.needs_pass and not check.authenticate(password or ""):
            raise PdfOpenError(
                "Output validation failed: the encrypted PDF could not be "
                "reopened with its own password."
            )
        actual = check.page_count
        actual_permissions = int(check.permissions)
    finally:
        check.close()
    if actual != expected_pages:
        raise PdfOpenError(
            f"Output validation failed: expected {expected_pages} pages, "
            f"found {actual}."
        )
    if expected_permissions is not None and not permissions_match(
            actual_permissions, expected_permissions):
        raise PdfOpenError(
            "Output validation failed: the encrypted PDF's permission bits do "
            "not match the requested restrictions."
        )


def save_encrypted_pdf(doc, out_path: Path, user_pw: Optional[str] = None,
                       owner_pw: Optional[str] = None,
                       permissions: Optional[int] = None) -> OutputResult:
    """Save an already-opened document as an AES-256 encrypted PDF.

    ``user_pw`` is the password required to open the file; ``owner_pw`` guards
    the permission settings. ``permissions`` is the bitmask of allowed actions
    (defaults to all allowed). The source is never modified. Written safely
    (temporary file -> validate -> atomic rename). Returns an
    :class:`OutputResult` carrying the path actually written and the page count.
    """
    pymupdf = _import_pymupdf()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    total = doc.page_count
    if permissions is None:
        permissions = all_permissions()

    save_kwargs = dict(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        permissions=int(permissions),
        garbage=3,
        deflate=True,
        use_objstms=1,
    )
    if user_pw:
        save_kwargs["user_pw"] = user_pw
    if owner_pw:
        save_kwargs["owner_pw"] = owner_pw

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp", prefix=".pdfforge_", dir=str(out_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        doc.save(str(tmp_path), **save_kwargs)
        _validate_encrypted(tmp_path, total, user_pw or owner_pw,
                            expected_permissions=int(permissions))
        # Never rebind out_path: the caller must be told the written name.
        written = promote_atomically(tmp_path, out_path)
    except BaseException:  # incl. Ctrl+C: an orphaned temp can hold decrypted bytes
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise

    elapsed = time.perf_counter() - started
    logger.info(
        "Encrypted '%s' (%d page(s), user_pw=%s owner_pw=%s) in %.2fs.",
        written, total, bool(user_pw), bool(owner_pw), elapsed,
    )
    return OutputResult(path=written, count=total)
