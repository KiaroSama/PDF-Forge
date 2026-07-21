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
from typing import Dict, Optional

from . import msoffice
from . import office_runtime as ort
from .constants import LOG_PREFIX

logger = logging.getLogger(LOG_PREFIX)

__all__ = ['MSOFFICE', 'LIBREOFFICE', 'BackendChoice', 'detect_backend',
           'plan_batch', 'msoffice_backend', 'libreoffice_backend',
           'backend_label', 'runtime_download_size_mb']

MSOFFICE = "msoffice"
LIBREOFFICE = "libreoffice"


class BackendChoice:
    """A backend, plus the source families it can actually convert.

    ``families`` matters because Microsoft Office availability is per
    application: a machine with Word registered but not Excel can convert a
    .docx and cannot convert a .xlsx. Treating "some Office exists" as "Office
    handles everything" routed spreadsheet jobs at a missing application and
    failed them outright, even with a ready LibreOffice standing by (C-10).
    """

    __slots__ = ("kind", "detail", "families")

    def __init__(self, kind: str, detail: str = "", families=()) -> None:
        self.kind = kind
        self.detail = detail
        self.families = tuple(families)

    def __bool__(self) -> bool:
        return self.kind in (MSOFFICE, LIBREOFFICE)

    def handles(self, family: str) -> bool:
        return bool(self) and (not self.families or family in self.families)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"BackendChoice({self.kind!r}, {self.detail!r}, "
                f"{self.families!r})")


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


def libreoffice_backend() -> BackendChoice:
    """The project-local LibreOffice, when it is ready. Falsy otherwise.

    LibreOffice converts every supported family, so its ``families`` is empty -
    meaning "no restriction".
    """
    status = ort.runtime_status()
    if status.get("ready"):
        return BackendChoice(LIBREOFFICE,
                             str(status.get("libreoffice_version") or ""))
    return BackendChoice("none")


def msoffice_backend() -> BackendChoice:
    """An installed Microsoft Office, restricted to the families it can convert
    *under the offline contract*.

    PowerPoint is deliberately excluded even when it is installed: Microsoft
    PowerPoint fetches linked media while ``Presentations.Open`` runs and cannot
    suppress it (see ``msoffice._convert_powerpoint``), so it cannot honour the
    "external updates disabled" guarantee. Only the hardened LibreOffice profile
    (``BlockUntrustedRefererLinks``) can, so ``.ppt``/``.pptx`` routes there.
    Word and Excel pass real link-suppression options at open time and stay here.
    """
    detected = msoffice.detect_office()
    if not detected:
        return BackendChoice("none")
    families = tuple(f for f in (detected.get("families") or ())
                     if f != "powerpoint")
    if not families:
        # A PowerPoint-only install covers nothing under the contract. An empty
        # ``families`` reads as "handles everything" (see ``handles``), so this
        # must be reported as no Office backend at all, not an unrestricted one.
        return BackendChoice("none")
    return BackendChoice(MSOFFICE, msoffice.describe_office(detected),
                         families=families)


def detect_backend() -> BackendChoice:
    """Pick a backend without installing anything and without asking.

    Callers that may install (the interactive path) handle the LibreOffice
    prompt themselves; this function only reports what is usable right now.
    """
    office = msoffice_backend()
    if office:
        return office
    return libreoffice_backend()


def plan_batch(families) -> Dict[str, BackendChoice]:
    """Choose a backend per source family.

    Microsoft Office is preferred for the families it can actually open, and the
    ready LibreOffice covers the rest. A family that neither backend can convert
    maps to a falsy choice, so the caller can report it instead of routing the
    job at a missing application.
    """
    office = msoffice_backend()
    libre = libreoffice_backend()
    plan: Dict[str, BackendChoice] = {}
    for family in families:
        if office.handles(family):
            plan[family] = office
        elif libre.handles(family):
            plan[family] = libre
        else:
            plan[family] = BackendChoice("none")
    return plan
