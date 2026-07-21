from __future__ import annotations

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403

__all__ = ['_batch_protection_preflight']


def _batch_protection_preflight(pdfs):
    """Inspect every source and decide ONE protection policy before queueing.

    PF-008: a batch must never create a downgraded file and warn afterwards.
    Every readable file is classified up front; if any source carries owner
    restrictions that cannot be reproduced, the user chooses once - skip those
    files, cancel, or knowingly write them unprotected. The decision is stored
    per file, so execution never prompts and never re-decides.

    Shared by every batch that writes derived PDFs (compress, delete-pages,
    image-only PDF), so the consent gate is defined and audited in one place.

    Returns ``(decisions, cancelled)`` where ``decisions`` maps a normalized
    path to a policy (or ``None`` meaning "skip this file").
    """
    restricted, unreadable = [], []
    decisions = {}
    for src in pdfs:
        try:
            doc = open_source_pdf(src)
        except (PdfOpenError, RuntimeError):
            unreadable.append(src)
            decisions[normalized_path_key(src)] = "unreadable"
            continue
        try:
            policy = detect_protection(doc)
        finally:
            close_doc(doc)
        if policy.kind == "restricted":
            restricted.append(src)
        decisions[normalized_path_key(src)] = policy

    if unreadable:
        print_warning(
            f"{len(unreadable)} file(s) could not be inspected (encrypted or "
            "unreadable); they will be attempted individually and may ask for a "
            "password while the queue runs."
        )
    if not restricted:
        return decisions, False

    print_warning(
        f"{len(restricted)} file(s) open freely but restrict actions that "
        "cannot be reproduced (the owner password is not recoverable):"
    )
    for src in restricted[:5]:
        print(colorize(f"    - {src.name}", Color.YELLOW))
    if len(restricted) > 5:
        print(colorize(f"    ... (+{len(restricted) - 5} more)", Color.DIM))

    prompt = question_prompt(
        "Restricted files",
        details="1=skip them, 2=cancel the batch, 3=write them unprotected",
        default="1",
    )
    while True:
        raw = _input(prompt).strip().lower()
        if raw in ("", "1"):
            for src in restricted:
                decisions[normalized_path_key(src)] = None   # skip
            logger.info("Batch protection: skipping %d restricted file(s).",
                        len(restricted))
            return decisions, False
        if raw == "2" or raw == "0":
            logger.info("Batch cancelled at the protection decision.")
            return decisions, True
        if raw == "3":
            for src in restricted:
                decisions[normalized_path_key(src)] = ProtectionPolicy(kind="none")
            print_warning(
                "Their output copies will be UNPROTECTED. The originals are "
                "unchanged."
            )
            logger.info("Batch protection: %d restricted file(s) -> unprotected.",
                        len(restricted))
            return decisions, False
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        print_error("Choose 1, 2, or 3.")
