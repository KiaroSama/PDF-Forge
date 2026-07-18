from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .constants import *  # noqa: F401,F403
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


def _validate_encrypted(path: Path, expected_pages: int, password: Optional[str]) -> None:
    """Reopen an encrypted output, authenticate if needed, verify page count."""
    pymupdf = _import_pymupdf()
    check = pymupdf.open(str(path))
    try:
        if check.needs_pass and not check.authenticate(password or ""):
            raise PdfOpenError(
                "Output validation failed: the encrypted PDF could not be "
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


def save_encrypted_pdf(doc, out_path: Path, user_pw: Optional[str] = None,
                       owner_pw: Optional[str] = None,
                       permissions: Optional[int] = None) -> int:
    """Save an already-opened document as an AES-256 encrypted PDF.

    ``user_pw`` is the password required to open the file; ``owner_pw`` guards
    the permission settings. ``permissions`` is the bitmask of allowed actions
    (defaults to all allowed). The source is never modified. Written safely
    (temporary file -> validate -> atomic rename). Returns the page count.
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
        _validate_encrypted(tmp_path, total, user_pw or owner_pw)
        os.replace(tmp_path, out_path)
        record_generated_output(out_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            logger.warning("Failed to remove temporary file: %s", tmp_path)
        raise

    elapsed = time.perf_counter() - started
    logger.info(
        "Encrypted '%s' (%d page(s), user_pw=%s owner_pw=%s) in %.2fs.",
        out_path, total, bool(user_pw), bool(owner_pw), elapsed,
    )
    return total
