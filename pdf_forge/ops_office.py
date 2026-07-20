"""Interactive "Convert documents/spreadsheets/presentations to PDF" tool.

Main-menu item 11. Drives the project-local, CLI-only LibreOffice + unoserver
runtime (see :mod:`pdf_forge.office_runtime`) to convert Word / PowerPoint /
Excel / CSV sources to PDF, entirely offline, with the source opened read-only,
macros and external updates disabled, and encrypted files handled via the
in-memory UNO password API with unlimited retries.
"""
from __future__ import annotations

import csv
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .safeio import promote_atomically
from .encrypt import *  # noqa: F401,F403
from .office import *  # noqa: F401,F403
from . import office_runtime as ort
from . import msoffice
from . import convert_backend as cb
from .office_decrypt import (
    DecryptError, DecryptPasswordError, decrypt_to_temp,
)
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403

__all__ = [
    '_show_convert_menu', 'convert_menu', 'prompt_office_source_files',
    'prompt_office_source_folder', 'operation_convert_files',
    'operation_convert_folder', '_validate_pdf_output',
]


def _show_convert_menu() -> None:
    print()
    print(colorize(f"{APP_NAME} Convert to PDF:", Color.BOLD + Color.LIGHT_BLUE))
    print(f"  {colorize('1.', Color.LIGHT_BLUE)} Add supported files one by one "
          f"{colorize('[1]', Color.GREEN)}")
    print(f"  {colorize('2.', Color.LIGHT_BLUE)} Use all supported files from a folder")
    print(f"  {colorize('0.', Color.LIGHT_BLUE)} Back")
    print()


def convert_menu() -> None:
    """Run the convert-to-PDF submenu loop (mirrors the other submenus)."""
    while True:
        _show_convert_menu()
        choice = _input(
            colorize("Select an option ", Color.BOLD)
            + colorize("[1]", Color.GREEN)
            + " "
            + back_text("back=0, quit=exit")
            + colorize(": ", Color.WHITE)
        ).strip().lower()

        if choice == "":
            choice = "1"
        if choice == "0":
            return
        if choice in ("exit", "quit"):
            raise _ExitRequested()

        logger.debug("Convert menu selection: '%s'", choice)
        set_operation_prompt(choice)  # numbering prefix = selected submenu item.
        try:
            if choice == "1":
                operation_convert_files()
            elif choice == "2":
                operation_convert_folder()
            else:
                print_error("Invalid option. Please choose 1, 2, or 0.")
                continue
        except KeyboardInterrupt:
            print_warning("\nOperation interrupted. Returning to menu.")
            logger.warning("Convert operation interrupted (KeyboardInterrupt).")


def _family_label(fam: str) -> str:
    return {
        "word": "Word", "powerpoint": "PowerPoint", "excel": "Excel", "csv": "CSV",
    }.get(fam, fam)


def prompt_office_source_files() -> Optional[List[Path]]:
    """Collect supported office paths one at a time (mixed families allowed).

    After at least one valid file, ``done`` (or a blank Enter) ends input, and
    ``b`` drops the file added last so it can be entered again. Duplicates
    rejected; manual order preserved; each accepted file's detected family is
    shown. Returns the list, or ``None`` to go back.
    """
    print_note(
        "Add one or more Word / PowerPoint / Excel / CSV files. After at least "
        "one file, type 'done' to finish. Type 'b' to re-enter the previous file."
    )
    selected: List[Path] = []
    while True:
        prompt = question_prompt(
            f"File #{len(selected) + 1}",
            details=guidance_text(
                drag_drop_guidance(repeated=True), GUIDANCE_KEYWORDS
            ),
        )
        raw = _input(prompt)
        cleaned = strip_surrounding_quotes(raw)

        if cleaned == "" or cleaned.lower() == "done":
            if selected:
                return selected
            print_error("Add at least 1 file before finishing.")
            continue
        if cleaned.lower() == "b":
            if not selected:
                print_error("There is no previous file to re-enter yet.")
                continue
            removed = selected.pop()
            print_warning(f"Removed: {removed.name}. Enter file #{len(selected) + 1} again.")
            continue
        if cleaned == "0":
            return None
        if cleaned.lower() in ("exit", "quit"):
            raise _ExitRequested()

        path = Path(cleaned)
        if not path.exists() or not path.is_file():
            print_error(f"Path does not exist or is not a file: {cleaned}")
            continue
        fam = classify_office_file(path)
        if fam is None:
            print_error(
                "Unsupported type. Supported: "
                + ", ".join(SUPPORTED_OFFICE_EXTS) + "."
            )
            continue
        ok, reason = validate_office_file(path)
        if not ok:
            print_error(f"Not a valid {_family_label(fam)} file: {reason}")
            continue
        if any(resolves_to_same_file(path, e) for e in selected):
            print_warning("That file is already in the list; duplicates are not allowed.")
            continue
        selected.append(path)
        print_success(
            f"Added ({_family_label(fam)}): {path.name}  (total: {len(selected)})"
        )


def prompt_office_source_folder() -> Optional[List[Path]]:
    """Discover supported office files directly inside a folder (non-recursive).

    Ignores Office lock files (``~$*``); natural, case-insensitive order.
    Returns the list, or ``None`` to go back.
    """
    prompt = question_prompt(
        "Folder containing documents",
        details=guidance_text(drag_drop_guidance(kind="folder"), GUIDANCE_KEYWORDS),
    )
    while True:
        raw = _input(prompt)
        cleaned = strip_surrounding_quotes(raw)
        if cleaned == "0":
            return None
        if cleaned == "":
            print_error("No folder entered. Please try again.")
            continue
        if cleaned.lower() in ("exit", "quit"):
            raise _ExitRequested()
        folder = Path(cleaned)
        if not folder.exists() or not folder.is_dir():
            print_error(f"Not a folder: {cleaned}")
            continue
        files = discover_office_files(folder)
        if not files:
            print_error(
                "No supported files found (non-recursive). Supported: "
                + ", ".join(SUPPORTED_OFFICE_EXTS) + "."
            )
            continue
        return files


def _print_family_counts(files) -> None:
    counts = family_counts(files)
    print_kv("Word", counts["word"], Color.SKY)
    print_kv("PowerPoint", counts["powerpoint"], Color.CORAL)
    print_kv("Excel", counts["excel"], Color.LIME)
    print_kv("CSV", counts["csv"], Color.VIOLET)


def _prompt_csv_correction(path: Path, dialect: "CsvDialect") -> "CsvDialect":
    """Show one compact correction prompt for a low-confidence CSV detection."""
    print_warning(
        f"CSV '{path.name}': delimiter detection is uncertain "
        f"(guessed {dialect.delimiter_label}, {dialect.encoding})."
    )
    prompt = question_prompt(
        "CSV delimiter",
        details="1=comma 2=semicolon 3=tab 4=colon 5=space",
        default=_csv_default_choice(dialect.delimiter),
    )
    mapping = {"1": ",", "2": ";", "3": "\t", "4": ":", "5": " "}
    while True:
        raw = _input(prompt).strip().lower()
        if raw == "":
            return dialect  # accept the guess
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        if raw in mapping:
            dialect.delimiter = mapping[raw]
            dialect.confidence = "high"
            return dialect
        print_error("Choose 1-5, or Enter to accept the guess.")


def _csv_default_choice(delimiter: str) -> str:
    return {",": "1", ";": "2", "\t": "3", ":": "4", " ": "5"}.get(delimiter, "1")


def _validate_pdf_output(path: Path) -> None:
    """Validate a freshly written PDF: exists, non-empty, opens, >=1 page,
    not unexpectedly encrypted. Raises PdfOpenError on any failure."""
    pymupdf = _import_pymupdf()
    if not path.exists() or path.stat().st_size <= 0:
        raise PdfOpenError("the converted PDF is missing or empty.")
    check = pymupdf.open(str(path))
    try:
        if check.needs_pass:
            raise PdfOpenError("the converted PDF is unexpectedly encrypted.")
        if check.page_count < 1:
            raise PdfOpenError("the converted PDF has no pages.")
    finally:
        check.close()


@dataclass
class JobPlan:
    """Result of configuring a conversion batch.

    ``accepted`` are runnable jobs; ``skipped`` are ``(path, reason)`` pairs
    shown to the user *before* anything is queued.
    """

    accepted: List[dict] = field(default_factory=list)
    skipped: List[tuple] = field(default_factory=list)

    def __iter__(self):
        # Iterating a plan yields its runnable jobs, so callers that just want
        # the work list stay simple.
        return iter(self.accepted)

    def __len__(self) -> int:
        return len(self.accepted)


def _build_jobs(files) -> JobPlan:
    """Configure per-file conversion jobs: validate, sniff CSV, reserve output.

    This is the single validation authority for **both** the manual picker and
    folder discovery (PF-020), so the two flows can never disagree about which
    files are convertible. A file that fails structural validation is skipped
    with an exact reason instead of being handed to LibreOffice.

    Output paths are reserved through the central queue reservation system so
    nothing collides.
    """
    plan = JobPlan()
    for src in files:
        fam = classify_office_file(src)
        if fam is None:
            plan.skipped.append((src, f"unsupported type '{src.suffix}'"))
            continue

        ok, reason = validate_office_file(src)
        if not ok:
            plan.skipped.append((src, reason))
            continue

        csv_dialect = None
        if fam == "csv":
            try:
                dialect = detect_csv_dialect(src)
            except OSError as exc:
                plan.skipped.append((src, f"cannot read CSV ({exc})"))
                continue
            if dialect.confidence == "low":
                dialect = _prompt_csv_correction(src, dialect)
            csv_dialect = dialect

        out = reserve_unique_file(src.with_suffix(".pdf"))
        # A job runs later than it is configured, so it carries an identity for
        # its source - size, mtime, file id and a content digest - and the
        # runner proves the file is still the one the user chose before handing
        # it to a converter (C-06).
        plan.accepted.append({"src": src, "family": fam, "out": out,
                              "ref": capture_file_source(src, family=fam),
                              "csv_dialect": csv_dialect})
    return plan


def _prompt_convert_password(filename: str, previous_failed: bool) -> Optional[str]:
    """Hidden password prompt for an encrypted source (unlimited retries).

    Returns the password, or ``None`` when the user types 0/back/skip.
    exit/quit raise _ExitRequested. Never echoes or logs the password.
    """
    import getpass

    if previous_failed:
        print_error("Incorrect password. Try again, or type 0/back/skip.")
    try:
        entry = getpass.getpass(
            colorize(
                f'Password for "{filename}" (hidden; 0/back/skip to skip): ',
                Color.CYAN,
            )
        )
    except (EOFError, KeyboardInterrupt):
        return None
    nav = entry.strip().lower()
    if nav in ("0", "back", "skip"):
        return None
    if nav in ("exit", "quit"):
        raise _ExitRequested()
    return entry


def _prompt_output_protection(filename: str):
    """After an encrypted source, ask how to protect the PDF output.

    Returns ``("none", None)``, ``("same", pw)``, ``("different", pw)``, or
    ``None`` (skip protection / back). ``pw`` is the source password for "same".
    """
    prompt = question_prompt(
        f"Protect the PDF from \"{filename}\"",
        details="1=unencrypted 2=same password 3=different password",
        default="1",
    )
    while True:
        raw = _input(prompt).strip().lower()
        if raw in ("", "1"):
            return "none", None
        if raw == "0":
            return None
        if raw in ("exit", "quit"):
            raise _ExitRequested()
        if raw == "2":
            return "same", None
        if raw == "3":
            new = prompt_new_password("to open the converted PDF")
            if new is None:
                continue
            return "different", new
        print_error("Choose 1, 2, or 3.")


def _apply_output_protection(staging: Path, password: str,
                             scratch: Path) -> Path:
    """Return a NEW staging file holding the protected version of ``staging``.

    Protection is a transformation of an artifact this operation owns, never a
    promotion onto a user-visible name. The previous version wrote the encrypted
    bytes and then called the no-clobber promoter with the *unencrypted* staging
    file as its destination; because that file still existed, promotion selected
    a suffixed sibling, the encrypted copy landed there as an orphan, and the
    caller went on to promote the unencrypted file as the user's output. The
    user chose a password and received an unprotected PDF (C-08).

    The result is validated with the selected password before it is returned, so
    a protection that did not take effect can never be promoted.
    """
    pymupdf = _import_pymupdf()
    protected = scratch / "protected.pdf"
    doc = pymupdf.open(str(staging))
    try:
        save_encrypted_pdf(doc, protected, user_pw=password, owner_pw=password,
                           permissions=all_permissions())
    finally:
        close_doc(doc)

    check = pymupdf.open(str(protected))
    try:
        if not check.needs_pass or not check.authenticate(password):
            raise PdfOpenError(
                "The protected output could not be reopened with the password "
                "that was selected; it was not promoted."
            )
    finally:
        check.close()
    return protected


def _backend_sessions(plan):
    """Lazily start one session per backend the plan actually needs.

    Returns ``(get, close)``. ``get(kind)`` starts that backend on first use and
    reuses it afterwards, so a batch of spreadsheets never starts Word and a
    batch that Office fully covers never starts LibreOffice.
    """
    sessions = {}

    def replace(kind, session):
        """Record a restarted session so later jobs use the live one."""
        sessions[kind] = session

    def get(kind):
        if kind in sessions:
            return sessions[kind]
        if kind == cb.MSOFFICE:
            print_info("Starting Microsoft Office...")
            session = msoffice.start_session()
        else:
            status = ort.runtime_status()
            if not status["ready"]:
                raise ort.OfficeRuntimeError(
                    "The conversion runtime is not ready. Missing: "
                    + _missing_runtime(status) + "."
                )
            print_info(
                f"Starting local LibreOffice "
                f"{status['libreoffice_version'] or '?'} "
                f"(unoserver {status['unoserver_version'] or '?'})..."
            )
            session = ort.warm_up(ort.start_conversion_server())
        sessions[kind] = session
        return session

    def close():
        for kind, session in sessions.items():
            try:
                session.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning("Could not stop the %s session: %s", kind, exc)

    return get, close, replace


def _run_conversion(jobs, source_families, backend=None, plan=None) -> None:
    """Execute the whole conversion batch, routing each family to its backend."""
    # The routing was chosen while configuring; re-check it here because the
    # task may run much later and Office could have been removed since.
    families = sorted({job["family"] for job in jobs})
    plan = cb.plan_batch(families)
    unusable = [fam for fam, choice in plan.items() if not choice]
    if len(unusable) == len(families):
        print_error(
            "No document converter is available any more. Microsoft Office was "
            "not found and the project-local LibreOffice is not ready."
        )
        logger.error("Convert aborted; no backend available.")
        return
    for fam in unusable:
        print_warning(
            f"  {_family_label(fam)} files cannot be converted: no available "
            "backend handles them."
        )

    # Per-family routing, stated up front so the user can see where each kind of
    # file is going rather than inferring it from one batch-wide label (C-10).
    for fam in families:
        if plan[fam]:
            print_info(f"  {_family_label(fam)} -> {cb.backend_label(plan[fam])}")

    get_session, close_sessions, set_session = _backend_sessions(plan)

    ok = failed = skipped = 0
    try:
        for index, job in enumerate(jobs, start=1):
            src, fam, out = job["src"], job["family"], job["out"]
            print_info(f"[{index}/{len(jobs)}] {src.name} ({_family_label(fam)})")
            job_backend = plan.get(fam)
            if not job_backend:
                skipped += 1
                print_note(f"  Skipped ({_family_label(fam)} has no backend).")
                continue
            try:
                server = get_session(job_backend.kind)
            except (msoffice.MsOfficeError, ort.OfficeRuntimeError) as exc:
                print_error(f"  Failed: {exc}")
                logger.error("Backend start failed for '%s': %s", src, exc)
                failed += 1
                continue
            result, server = _convert_with_restart(server, job,
                                                   backend=job_backend)
            if server is not None:
                set_session(job_backend.kind, server)
            if server is None:
                print_error(
                    "  The conversion runtime could not be restarted; stopping."
                )
                failed += len(jobs) - index + 1
                break
            if result == "ok":
                ok += 1
                # Report the file that was actually written: promotion may have
                # allocated a suffixed name (C-01).
                print_success(f"  -> {Path(job.get('written', out)).name}")
            elif result == "skip":
                skipped += 1
            else:
                failed += 1
    finally:
        close_sessions()

    counts = family_counts([j["src"] for j in jobs])
    print_success(
        f"Done. Converted {ok}, failed {failed}, skipped {skipped}. "
        f"(Word {counts['word']}, PowerPoint {counts['powerpoint']}, "
        f"Excel {counts['excel']}, CSV {counts['csv']}.)"
    )
    logger.info(
        "Convert batch complete: ok=%d failed=%d skipped=%d backend=%s",
        ok, failed, skipped,
        "; ".join(f"{f}={cb.backend_label(c)}" for f, c in plan.items()),
    )


def _convert_with_restart(server, job, attempts: int = 3, backend=None):
    # `attempts` counts total tries; `restarts` counts how many times the
    # LibreOffice runtime had to be replaced. They are reported separately so a
    # message can never overstate what actually happened (PF-039).
    """Convert one job, restarting the runtime if LibreOffice dies.

    A crashed LibreOffice leaves a suspect user profile, so the server is
    replaced with a completely fresh one (new profile, new port) rather than
    reused. Returns ``(result, server)``; ``server`` is ``None`` when the
    runtime could not be restarted.
    """
    restarts = 0
    for attempt in range(attempts):
        try:
            return _convert_one(server, job, backend), server
        except ort.OfficeRuntimeError as exc:
            if not ort.is_bridge_lost(exc) or attempt == attempts - 1:
                message = (
                    "LibreOffice stopped responding while converting this file "
                    f"(attempt {attempt + 1} of {attempts}; "
                    f"{restarts} runtime restart(s))."
                    if ort.is_bridge_lost(exc) else str(exc)
                )
                print_error(f"  Failed: {message}")
                logger.error("Convert failed for '%s': %s", job["src"], exc)
                return "fail", server
            print_warning(
                "  The LibreOffice runtime stopped responding; restarting it "
                "with a fresh profile and retrying this file..."
            )
            restarts += 1
            logger.warning(
                "Conversion runtime lost; restarting (restart %d, attempt %d/%d).",
                restarts, attempt + 1, attempts,
            )
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                server = ort.warm_up(ort.start_conversion_server())
            except ort.OfficeRuntimeError as start_exc:
                print_error(f"  Could not restart the runtime: {start_exc}")
                logger.error("Runtime restart failed: %s", start_exc)
                return "fail", None
    return "fail", server


def _convert_one(server, job, backend=None) -> str:
    """Convert a single job (with password + protection flow).

    Returns ``'ok'`` | ``'fail'`` | ``'skip'``. The temporary directory holding
    a normalized CSV copy is owned by exactly one ``try/finally`` here, so it is
    removed on success, password cancel, bridge loss, runtime error, output
    validation failure, timeout, exit, and KeyboardInterrupt alike.
    """
    src = job["src"]
    csv_dir = None
    try:
        source_for_convert = src
        if job.get("csv_dialect") is not None:
            # Apply the sniffed CSV dialect by converting a canonical copy (the
            # converter API cannot take import-filter options). Source untouched.
            try:
                csv_dir = Path(tempfile.mkdtemp(prefix="pdfforge_csv_"))
                source_for_convert = normalize_csv_for_import(
                    src, job["csv_dialect"], csv_dir / src.name
                )
            except (OSError, UnicodeError, ValueError, csv.Error) as exc:
                # Converting the raw source instead would silently discard the
                # detected (and possibly user-corrected) delimiter and encoding,
                # and the backend would re-guess them - a wrong PDF reported as
                # a success. Fail the file with a visible reason (C-16).
                print_error(
                    f"  Failed: the CSV could not be prepared for conversion "
                    f"({exc}); it was not converted."
                )
                logger.error("CSV normalization failed for '%s': %s", src, exc)
                return "fail"
        return _convert_one_body(server, job, source_for_convert, backend)
    finally:
        if csv_dir is not None:
            shutil.rmtree(csv_dir, ignore_errors=True)


def _discard_stage(stage_dir: Path) -> None:
    """Remove a whole task-owned staging directory, never raising."""
    shutil.rmtree(stage_dir, ignore_errors=True)


def _require_matching_family(plain: Path, expected_family: str,
                             display_name: str) -> None:
    """Reject a decrypted package whose real family is not the one claimed.

    Before decryption every encrypted OOXML container looks identical - an OLE2
    holding ``EncryptionInfo``/``EncryptedPackage`` - so the extension is the
    only family evidence available, and it is attacker-controlled. An encrypted
    workbook renamed ``.docx`` would otherwise be decrypted and handed straight
    to Word (C-11). Validate the plaintext instead, against the family the job
    was actually planned for.
    """
    ok, reason = validate_office_file(plain, expected_family=expected_family)
    if not ok:
        raise DecryptError(
            f"'{display_name}' decrypted successfully but is not a "
            f"{_family_label(expected_family)} file ({reason}); it was not "
            "converted."
        )


def _convert_one_body(server, job, source_for_convert, backend=None) -> str:
    """Password + conversion + finalization for one already-prepared source."""
    src, out = job["src"], job["out"]
    password: Optional[str] = None
    attempted = False
    try:
        ref = job.get("ref")
        if ref is not None:
            # Before the first staging byte: a source edited or replaced since
            # configuration must not be converted (C-06).
            try:
                ref.verify_unchanged()
            except SourceChangedError as exc:
                print_error(f"  Failed: {exc}")
                logger.error("Convert source changed for '%s': %s", src, exc)
                return "fail"

        # Ask for the password *before* converting when the container is visibly
        # encrypted, rather than relying on the converter's error path.
        if is_encrypted_office_file(src):
            password = _prompt_convert_password(src.name, False)
            if password is None:
                print_note(f"  Skipped (password not provided): {src.name}")
                return "skip"
            attempted = True

        use_msoffice = backend is not None and backend.kind == cb.MSOFFICE
        while True:
            # Staging lives in a task-owned unique directory beside the
            # destination, never a deterministic sibling of the output: two
            # processes converting into the same folder used to collide on
            # "<out>.convert.tmp" before any no-clobber check applied (C-07).
            out.parent.mkdir(parents=True, exist_ok=True)
            stage_dir = Path(tempfile.mkdtemp(prefix=".pdfforge_convert_",
                                              dir=str(out.parent)))
            tmp = stage_dir / "converted.pdf"
            try:
                if use_msoffice:
                    msoffice.convert_to_pdf(
                        server, source_for_convert, tmp, job["family"],
                        password=password, encrypted=bool(password),
                    )
                elif password:
                    # LibreOffice cannot open a document encrypted by Microsoft
                    # Office - it loses the UNO bridge instead of converting -
                    # so the source is decrypted locally first and the server
                    # only ever sees a plain document. The decrypted copy lives
                    # in a temporary directory that is removed straight after.
                    plain = decrypt_to_temp(source_for_convert, password,
                                            stage_dir)
                    _require_matching_family(plain, job["family"], src.name)
                    ort.convert_to_pdf(server, plain, tmp)
                else:
                    ort.convert_to_pdf(server, source_for_convert, tmp)
                _validate_pdf_output(tmp)
            except (msoffice.MsOfficePasswordError, DecryptPasswordError):
                _discard_stage(stage_dir)
                pw = _prompt_convert_password(src.name, attempted)
                if pw is None:
                    print_note(f"  Skipped (password not provided): {src.name}")
                    return "skip"
                password = pw
                attempted = True
                continue
            except (msoffice.MsOfficeError, DecryptError) as exc:
                _discard_stage(stage_dir)
                print_error(f"  Failed: {exc}")
                logger.error("Convert failed for '%s': %s", src, exc)
                return "fail"
            except ort.OfficeRuntimeError as exc:
                _discard_stage(stage_dir)
                if str(exc) == ort.PASSWORD_SENTINEL:
                    # Encrypted / wrong password: prompt (unlimited retries).
                    pw = _prompt_convert_password(src.name, attempted)
                    if pw is None:
                        print_note(f"  Skipped (password not provided): {src.name}")
                        return "skip"
                    password = pw
                    attempted = True
                    continue
                if ort.is_bridge_lost(exc):
                    # The runtime died: the caller restarts it and retries.
                    raise
                print_error(f"  Failed: {exc}")
                logger.error("Convert failed for '%s': %s", src, exc)
                return "fail"
            except PdfOpenError as exc:
                _discard_stage(stage_dir)
                print_error(f"  Failed output validation: {exc}")
                logger.error("Convert output invalid for '%s': %s", src, exc)
                return "fail"

            # Success: optional output protection for encrypted sources.
            try:
                final_staging = tmp
                if password:
                    choice = _prompt_output_protection(src.name)
                    if choice and choice[0] != "none":
                        mode, new_pw = choice
                        # Protection replaces the artifact about to be promoted;
                        # it never promotes anything itself (C-08).
                        final_staging = _apply_output_protection(
                            tmp, password if mode == "same" else new_pw,
                            stage_dir,
                        )
                # One promotion, once, with an explicit recording policy: a
                # converted PDF is a brand-new source the user will usually want
                # to split, compress or protect next, so it stays visible to the
                # PDF folder tools and is deliberately NOT recorded (C-09).
                job["written"] = promote_atomically(final_staging, out,
                                                    record=False)
            except Exception as exc:  # noqa: BLE001
                print_error(f"  Failed finalizing output: {exc}")
                logger.exception("Convert finalize failed for '%s'", src)
                return "fail"
            finally:
                _discard_stage(stage_dir)
            return "ok"
    finally:
        password = None  # drop the source password as soon as this file ends


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("Could not remove temp file: %s", path)


def _missing_runtime(status: dict) -> str:
    missing = []
    if not status["unoserver_installed"]:
        missing.append("unoserver (.venv)")
    if not status["soffice"]:
        missing.append("project-local LibreOffice (.tools/libreoffice)")
    if status["soffice"] and not status["soffice_python"]:
        missing.append("LibreOffice bundled Python")
    return ", ".join(missing) or "unknown"


def operation_convert_files() -> None:
    """Convert one or more manually chosen office files to PDF."""
    reset_questions()
    print_heading("\nConvert to PDF: add files")
    logger.info("Operation started: Convert to PDF (files).")

    files = prompt_office_source_files()
    if files is None:
        return
    _configure_and_queue(files, mode="files")


def operation_convert_folder() -> None:
    """Convert every supported office file in a folder to PDF (non-recursive)."""
    reset_questions()
    print_heading("\nConvert to PDF: folder")
    logger.info("Operation started: Convert to PDF (folder).")

    files = prompt_office_source_folder()
    if files is None:
        return
    _configure_and_queue(files, mode="folder")


def _resolve_backend():
    """Pick the conversion backend, offering to install LibreOffice only if needed.

    Two steps, in this order:

    1. If Microsoft Office is installed, use it and say so. It is the native
       renderer for these formats and needs no download at all.
    2. Otherwise offer the project-local LibreOffice. It is *not* a startup
       prerequisite: nothing is downloaded until the user actually converts
       something and agrees here.

    Returns a ``BackendChoice`` (falsy when no backend is available).
    """
    choice = cb.detect_backend()
    if choice.kind == cb.MSOFFICE:
        print_success(
            f"Using the Microsoft Office already installed on this PC "
            f"({choice.detail}). No download or extra disk space is needed."
        )
        logger.info("Convert backend: Microsoft Office (%s).", choice.detail)
        return choice
    if choice.kind == cb.LIBREOFFICE:
        logger.info("Convert backend: project-local LibreOffice %s.", choice.detail)
        return choice

    size = cb.runtime_download_size_mb()
    print_warning(
        "Microsoft Office was not found on this PC, so PDF Forge needs its own "
        "converter for this tool."
    )
    print_note(
        "It installs a trimmed, CLI-only LibreOffice into this project folder "
        f"only{f' (about {size} MB to download)' if size else ''}: no system "
        "install, no PATH, registry, shortcut or service change, and no GUI. "
        "Only the conversion components are kept - interface translations, "
        "help, clipart and spelling dictionaries are not installed. Every "
        "other tool in PDF Forge works without it."
    )
    if not ask_yes_no("Install the converter now?", default_yes=True):
        print_warning("Nothing was installed; returning to menu.")
        logger.info("User declined the LibreOffice install.")
        return cb.BackendChoice("none")

    try:
        result = ort.provision_runtime(progress=lambda msg: print_info(f"  {msg}"))
    except ort.OfficeRuntimeError as exc:
        print_error(f"The converter could not be installed: {exc}")
        logger.error("Provisioning failed: %s", exc)
        return cb.BackendChoice("none")
    print_success(f"Converter ready ({result.get('status')}).")

    choice = cb.detect_backend()
    if not choice:
        print_error(
            "The converter was installed but still does not report as ready."
        )
        logger.error("Post-install backend detection failed.")
    return choice


def _configure_and_queue(files, mode: str) -> None:
    backend = _resolve_backend()
    if not backend:
        return

    plan = _build_jobs(files)
    if plan.skipped:
        # Report exactly why each file was rejected, before anything is queued.
        print_warning(f"{len(plan.skipped)} file(s) will be skipped:")
        for path, reason in plan.skipped:
            print(colorize(f"    - {path.name}: {reason}", Color.YELLOW))
    jobs = plan.accepted
    if not jobs:
        print_warning(
            "No convertible files remained; nothing was queued and LibreOffice "
            "was not started. Returning to menu."
        )
        return

    print_heading("\nSummary")
    _print_family_counts([j["src"] for j in jobs])
    print_kv("Files to convert", len(jobs), Color.MAGENTA)
    print_kv("Per-file output", "<name>.pdf beside each source", Color.AQUA)
    print_note(
        "Conversion is local and offline; sources are opened read-only with "
        "macros and external updates disabled. Encrypted files will ask for a "
        "password during conversion (unlimited attempts; 0/back/skip to skip)."
    )
    source_families = family_counts([j["src"] for j in jobs])

    def _run():
        _run_conversion(jobs, source_families, backend)

    label = (
        f"Convert {len(jobs)} file(s) to PDF"
        + (f" in {files[0].parent.name}" if mode == "folder" else "")
    )
    # Each job re-verifies its own source before converting too; this
    # check stops the batch early when one is already gone.
    queue_task(label, _run,
               sources=[j["ref"] for j in jobs if j.get("ref")])
