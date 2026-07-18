"""Interactive "Convert documents/spreadsheets/presentations to PDF" tool.

Main-menu item 11. Drives the project-local, CLI-only LibreOffice + unoserver
runtime (see :mod:`pdf_forge.office_runtime`) to convert Word / PowerPoint /
Excel / CSV sources to PDF, entirely offline, with the source opened read-only,
macros and external updates disabled, and encrypted files handled via the
in-memory UNO password API with unlimited retries.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .encrypt import *  # noqa: F401,F403
from .office import *  # noqa: F401,F403
from . import office_runtime as ort
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
        plan.accepted.append({"src": src, "family": fam, "out": out,
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


def _apply_output_protection(out_path: Path, mode: str, password: str) -> None:
    """Re-encrypt a produced PDF in place (via temp) using the AES-256 protector."""
    pymupdf = _import_pymupdf()
    doc = pymupdf.open(str(out_path))
    try:
        tmp = out_path.with_suffix(".protect.tmp")
        save_encrypted_pdf(doc, tmp, user_pw=password, owner_pw=password,
                           permissions=all_permissions())
    finally:
        close_doc(doc)
    promote_atomically(tmp, out_path)
    # Deliberately NOT recorded in the generated-output manifest: that manifest
    # stops a PDF folder tool from re-processing its *own* PDF output. A PDF
    # converted from a document is a brand-new source the user will usually want
    # to compress, split, or protect next, so it must stay discoverable.


def _run_conversion(jobs, source_families) -> None:
    """Execute the whole conversion batch through one task-owned server."""
    status = ort.runtime_status()
    if not status["ready"]:
        print_error(
            "The conversion runtime is not ready. Missing: "
            + _missing_runtime(status)
            + ". See the README 'Convert to PDF' setup notes."
        )
        logger.error("Convert aborted; runtime not ready: %s", status)
        return

    print_info(
        f"Starting local LibreOffice {status['libreoffice_version'] or '?'} "
        f"(unoserver {status['unoserver_version'] or '?'})..."
    )
    try:
        server = ort.start_conversion_server()
        server = ort.warm_up(server)
    except ort.OfficeRuntimeError as exc:
        print_error(f"Could not start the conversion runtime: {exc}")
        logger.error("Convert server start failed: %s", exc)
        return

    ok = failed = skipped = 0
    try:
        for index, job in enumerate(jobs, start=1):
            src, fam, out = job["src"], job["family"], job["out"]
            print_info(f"[{index}/{len(jobs)}] {src.name} ({_family_label(fam)})")
            result, server = _convert_with_restart(server, job)
            if server is None:
                print_error(
                    "  The conversion runtime could not be restarted; stopping."
                )
                failed += len(jobs) - index + 1
                break
            if result == "ok":
                ok += 1
                print_success(f"  -> {out.name}")
            elif result == "skip":
                skipped += 1
            else:
                failed += 1
    finally:
        if server is not None:
            server.stop()

    counts = family_counts([j["src"] for j in jobs])
    print_success(
        f"Done. Converted {ok}, failed {failed}, skipped {skipped}. "
        f"(Word {counts['word']}, PowerPoint {counts['powerpoint']}, "
        f"Excel {counts['excel']}, CSV {counts['csv']}.)"
    )
    logger.info(
        "Convert batch complete: ok=%d failed=%d skipped=%d lo=%s unoserver=%s",
        ok, failed, skipped, status["libreoffice_version"],
        status["unoserver_version"],
    )


def _convert_with_restart(server, job, attempts: int = 3):
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
            return _convert_one(server, job), server
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


def _convert_one(server, job) -> str:
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
            except (OSError, UnicodeError, ValueError) as exc:
                logger.warning("CSV normalization failed for '%s': %s", src, exc)
                source_for_convert = src
        return _convert_one_body(server, job, source_for_convert)
    finally:
        if csv_dir is not None:
            shutil.rmtree(csv_dir, ignore_errors=True)


def _convert_one_body(server, job, source_for_convert) -> str:
    """Password + conversion + finalization for one already-prepared source."""
    src, out = job["src"], job["out"]
    password: Optional[str] = None
    attempted = False
    try:
        # Ask for the password *before* converting when the container is visibly
        # encrypted, rather than relying on the converter's error path.
        if is_encrypted_office_file(src):
            password = _prompt_convert_password(src.name, False)
            if password is None:
                print_note(f"  Skipped (password not provided): {src.name}")
                return "skip"
            attempted = True

        while True:
            tmp = out.with_suffix(".convert.tmp")
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                ort.convert_to_pdf(server, source_for_convert, tmp, password=password)
                _validate_pdf_output(tmp)
            except ort.OfficeRuntimeError as exc:
                _safe_unlink(tmp)
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
                _safe_unlink(tmp)
                print_error(f"  Failed output validation: {exc}")
                logger.error("Convert output invalid for '%s': %s", src, exc)
                return "fail"

            # Success: optional output protection for encrypted sources.
            try:
                if password:
                    choice = _prompt_output_protection(src.name)
                    if choice and choice[0] != "none":
                        mode, new_pw = choice
                        _apply_output_protection(
                            tmp, mode, password if mode == "same" else new_pw
                        )
                promote_atomically(tmp, out)
            except Exception as exc:  # noqa: BLE001
                _safe_unlink(tmp)
                print_error(f"  Failed finalizing output: {exc}")
                logger.exception("Convert finalize failed for '%s'", src)
                return "fail"
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


def _warn_if_runtime_missing() -> bool:
    """Warn early when the conversion runtime is incomplete. False = not ready.

    Checked while configuring rather than only at run time, so the user is not
    left with a queued task that is certain to fail.
    """
    status = ort.runtime_status()
    if status["ready"]:
        return True
    print_warning(
        "The convert-to-PDF runtime is not ready yet - missing: "
        + _missing_runtime(status) + "."
    )
    print_note(
        "Set it up once with:\n"
        "  .\\.venv\\Scripts\\python.exe -m pdf_forge --setup-office\n"
        "It downloads the pinned official LibreOffice build into this project "
        "folder only (no system install, no GUI). Every other tool works "
        "without it."
    )
    return False


def _configure_and_queue(files, mode: str) -> None:
    runtime_ready = _warn_if_runtime_missing()
    if not runtime_ready and not ask_yes_no(
        "Queue the conversion anyway (it will fail until the runtime is set up)?",
        default_yes=False,
    ):
        print_warning("Returning to menu.")
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
        _run_conversion(jobs, source_families)

    label = (
        f"Convert {len(jobs)} file(s) to PDF"
        + (f" in {files[0].parent.name}" if mode == "folder" else "")
    )
    queue_task(label, _run)
