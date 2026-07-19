"""Choose how documents get converted to PDF: Microsoft Office or LibreOffice.

Two backends produce the same result, so the tool picks one instead of forcing
a choice on the user:

1. **Microsoft Office**, when it is installed. It is the native renderer for
   these formats, it needs no download, and it costs no disk space.
2. **A project-local LibreOffice**, provisioned on demand. It is only offered
   when Office is absent, and it is never installed just because the
   application started - the user opts in at the moment they actually convert
   something.

Everything else in PDF Forge works without either backend.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import msoffice
from . import office_runtime as ort
from .constants import LOG_PREFIX

logger = logging.getLogger(LOG_PREFIX)

__all__ = ['MSOFFICE', 'LIBREOFFICE', 'BackendChoice', 'detect_backend',
           'backend_label', 'runtime_download_size_mb']

MSOFFICE = "msoffice"
LIBREOFFICE = "libreoffice"


class BackendChoice:
    """The backend a conversion batch will use, plus how to describe it."""

    __slots__ = ("kind", "detail")

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail

    def __bool__(self) -> bool:
        return self.kind in (MSOFFICE, LIBREOFFICE)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"BackendChoice({self.kind!r}, {self.detail!r})"


def backend_label(choice: "BackendChoice") -> str:
    if choice.kind == MSOFFICE:
        return f"Microsoft Office ({choice.detail})"
    if choice.kind == LIBREOFFICE:
        return f"LibreOffice {choice.detail}".strip()
    return "no converter"


def runtime_download_size_mb() -> Optional[int]:
    """Approximate download size of the pinned LibreOffice package, in MB."""
    try:
        meta = ort.load_runtime_meta()
    except Exception:  # noqa: BLE001 - a missing/broken pin must not crash the prompt
        return None
    platform_meta = meta.get("windows") or {}
    size = platform_meta.get("approx_bytes")
    if not size:
        return None
    try:
        return max(1, round(int(size) / (1024 * 1024)))
    except (TypeError, ValueError):
        return None


def detect_backend() -> BackendChoice:
    """Pick a backend without installing anything and without asking.

    Callers that may install (the interactive path) handle the LibreOffice
    prompt themselves; this function only reports what is usable right now.
    """
    detected = msoffice.detect_office()
    if detected:
        return BackendChoice(MSOFFICE, msoffice.describe_office(detected))

    status = ort.runtime_status()
    if status.get("ready"):
        return BackendChoice(LIBREOFFICE, str(status.get("libreoffice_version") or ""))
    return BackendChoice("none")
