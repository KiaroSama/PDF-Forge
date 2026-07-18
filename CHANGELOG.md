# Changelog

All notable changes to PDF Forge are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.1] - 2026-07-19

### Fixed
- **Piped input is read as UTF-8**, so a path containing non-Latin
  characters is no longer mangled by the Windows ANSI code page and
  reported as missing.
- The watermark output suffix is `_no_watermark` (was `_nowatermark`,
  which reads as "now atermark").
- **Output promotion can no longer overwrite a destination** that appeared
  between configuring a task and running it; a unique name is allocated instead.
  Proven with a multi-process race test.
- **Queued extract/split/delete no longer hold an open document**, so a source
  can be renamed or deleted immediately after queueing, and discarding a queue
  leaks nothing. Batch delete closes every document on every path.
- **A source replaced or edited after configuration is refused** with no output.
- **Encrypted OOXML files reach the password prompt** instead of being rejected
  as "not a ZIP"; OOXML packages are validated per family and reject fake ZIPs,
  cross-family renames, traversal members and ZIP bombs. OLE detection parses
  the directory (olefile) rather than scanning the first megabyte.
- **CSV**: a sample cut mid-code-point no longer turns valid UTF-8 into
  Windows-1252; normalization streams instead of loading the file; delimiter
  detection ranks row consistency ahead of column count; temp directories are
  cleaned on every failure path.
- **Generated-output tracking** moved out of the checkout into per-user state,
  written atomically under a cross-process lock, with strong file identity and
  visible warnings when it is unavailable. Image-only PDFs are now tracked.
- **Queue and reservation cleanup run in a `finally`**, including on SystemExit.
- **Prompt numbers are allocated when displayed**, so retries and nested prompts
  stay strictly monotonic instead of repeating a used number.
- Folder iteration errors, retry-count messages and `exit_code` binding fixed.
- Conversion runs in a hardened profile with macros and link updates disabled.

### Added
- `pyproject.toml` (pytest/ruff/mypy/coverage), pinned dev dependencies, and CI
  gates for lint, type checking, coverage, dependency audit, secret scanning,
  PowerShell analysis and checkout cleanliness.
- A dedicated Windows workflow that provisions the pinned LibreOffice with
  production code and runs real conversion end-to-end tests.

## [2.0.0] - 2026-07-18

### Changed (breaking)
- **Main menu reordered and renumbered.** The tools are now grouped as page/merge/
  delete, then the image tools, then compress/protect/unlock, then convert:

  `1` Page tools · `2` Merge · `3` Delete pages · `4` PDF to images (PNG) ·
  `5` PDF to image-only PDF · `6` Remove image watermark · `7` Extract images ·
  `8` Compress PDF · `9` Protect PDF · `10` Unlock PDF · `11` Convert to PDF ·
  `0` Exit.

  Old numbers now open different tools; invalid input reports `1-11 or 0`.
  Pressing **Enter** at the main menu still opens Page tools.
- **Hierarchical prompt numbering.** Prompts read `<operation>-<question>`
  (`1-1`, `1-2`, `1-3`, …). The prefix is the menu item you chose and never
  changes inside an operation; only the local counter advances, including on
  validation retries. Previously a single global counter made a retry look like
  a new question (`1.`, `2.`, `3.` for the same prompt).
- **`_folder_dpi_stats()` now always returns a dict** (with `files_with_images`,
  `files_text_only`, `files_not_scanned`) instead of `None` for an all-text
  folder, so callers can tell "no images" apart from "not inspected".

### Added
- **Convert documents/spreadsheets/presentations to PDF** (main menu `11`) —
  `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`, `.csv` in one menu with
  automatic family detection. Add files one by one (`done` to finish, `b` to
  re-enter the previous file) or take a whole folder (non-recursive, Office `~$` lock files skipped).
  Conversion is local, offline, and command-line only: the `.venv`-installed
  `unoserver` client drives a **project-local headless LibreOffice** under
  `.tools/libreoffice/` (pinned version + SHA-256 in the tracked
  `office_runtime_meta.json`). LibreOffice is never installed system-wide, no
  PATH/registry/shortcut/service is touched, and no GUI appears. Each run owns
  its `soffice` process on a random localhost port with an isolated temporary
  profile and terminates only that process on every exit path.
  Encrypted sources are handled through the **in-memory** UNO password API with
  **unlimited** retries, and the produced PDF can stay unencrypted, reuse the
  source password, or take a new one.
- `--diagnose`, `--setup-office`, `--clean-office` command-line modes. The
  diagnostic mode prints the resolved package files, repository root, commit,
  interpreter, and runtime versions, so it is obvious which checkout a launcher
  is really running.
- **Drag-and-drop path guidance** on every file/folder prompt:
  `(drag and drop a file here or paste a path)`. Prompts that collect several
  files add `b=re-enter previous file; type done when finished`, with the
  typeable keywords picked out in light blue inside the hint-coloured guidance
  (the same split FFmWiz uses). Quoted paths keep working.
- **`b` re-enters the previous file** in the merge and convert file lists:
  it drops the file added last and asks for it again.
- Documented **protection policy** for encrypted sources (see README).
- The conversion runtime is **self-healing and self-diagnosing**: an encrypted
  source is detected from its container *before* conversion (so the password is
  requested up front rather than inferred from an opaque converter error), and
  if LibreOffice dies mid-run the server is restarted with a **fresh** profile
  and the file retried once. Provisioning caches the verified download under
  `.tools/cache/`, so a retry or repair never re-downloads ~360 MB, and it
  aborts with an actionable message instead of hanging when Windows Installer
  cannot service the request.

### Fixed
- **Merge → "Add PDF files one by one" now finishes reliably.** `done` is
  accepted (case-insensitively) alongside a blank Enter; before, the word was
  treated as a file path and the prompt repeated forever. The `[finish]` default
  marker is gone - the guidance itself now names the keyword.
- **Queued tasks can no longer collide on an output path.** Output files and
  directories are reserved at queue time (normalized, case-insensitively on
  Windows, files and directories tracked separately), so two tasks configured
  with the same default output get distinct names instead of one silently
  overwriting the other. Reservations are released when the queue runs, is
  discarded, or the app exits.
- **Transformations no longer silently strip protection.** A source that needs
  an open password produces an output re-encrypted with the same password and
  permissions; an owner-restricted source (whose owner password cannot be
  recovered) now warns and asks instead of quietly producing an unprotected
  file; a merge never invents a policy.
- **Watermark detection uses a real content identity.** Grouping is by MuPDF's
  decoded-image digest plus pixel size instead of `(width, height, raw stream
  length)`, which could merge two unrelated images and delete the wrong one.
- **Watermarks painted through Form XObjects are found and removed.** Removal
  now edits the form's content stream as well as the page's, so such a
  watermark no longer reports a misleading "0 pages modified" success.
- **Only painted images are reported.** Scanning uses actual painted
  occurrences, so unused resource entries are no longer offered as watermark
  candidates or extractable images.
- **Extract images deduplicates by content**, so one picture stored under
  several xrefs is extracted once, as "distinct images" promises.
- **Transparency is preserved when extracting.** An image with a soft mask is
  rebuilt as a PNG with its alpha channel; in JPEG mode it is composited over
  white rather than silently losing the mask.
- **Pathological page ranges are rejected instantly.** `1-999999999` no longer
  tries to materialize a billion integers; ranges are validated against the
  document length (or a sanity ceiling in batch mode) *before* expansion.
- **Password prompts have no attempt limit anywhere.** A wrong password shows a
  clear message and asks again indefinitely, until the password is accepted or
  you type `0`/`back`/`skip` (or `exit`/`quit`). Blank input never counts
  against anything, and failed passwords are never retained.
- **Queued tasks no longer ask for a password while running.** Single-file
  operations authenticate during configuration and reopen silently with the
  captured password; batch operations, which can only discover an encrypted
  file when they open it, now disclose that up front.
- **No file handles survive the queue.** Operations carry immutable paths and
  settings instead of open documents, so discarding a queue leaks nothing and a
  source can be renamed or deleted right after an operation.
- **`exit`/`quit` at `Start now?` is a clean exit** — the queue is discarded,
  reservations released, and the app returns a normal exit code instead of
  surfacing an unexpected top-level error.
- **Encrypted files are no longer dropped from the batch compression preflight.**
  They are counted as "not scanned", and an all-encrypted folder no longer
  claims "no raster images in any file".
- **Batch compression reports growth honestly.** When the outputs are larger in
  total, it says "increased by" with a warning instead of feeding a negative
  byte count to the size formatter and calling it "saved".
- **Configuration no longer creates directories.** Output folders are created by
  the task runner, so cancelling or discarding a queue leaves nothing behind.
- **Folder tools never reprocess their own output.** Generated PDFs are recorded
  by exact normalized path (with size/mtime), so a second run over the same
  folder skips them — without the substring guesswork that would also hide a
  user's own file named `..._compressed.pdf`.

## [1.5.0] - 2026-07-11

### Added
- **Compress PDF** main-menu tool (option 7) with a **single-file and a batch
  folder** submenu: shrinks a PDF into a new file while never modifying the
  original. Every run applies lossless optimization (object
  deduplication/garbage collection, stream recompression, PDF object streams,
  and font subsetting). All levels except Ultra additionally downsample
  embedded images **above the chosen DPI cap** (never below it and never above
  the original) and re-encode them as JPEG. Bitonal (fax/scan black-and-white)
  images are never re-encoded. Single-file reports old/new size and the saving;
  batch reports a per-file line plus a grand total.
- **Seven quality levels everywhere**: Very low, Low, Medium, High, Very high,
  Ultra, and Custom. For the conversion tools (PDF to PNG, image-only PDF)
  they map to render DPI (72/96/150/300/450/600, Custom 30-1200; Enter still
  selects Medium 150). For compression they map to JPEG quality + image DPI
  cap (Ultra = lossless only, zero quality change; Enter selects Very high;
  Custom asks for quality 1-100 and DPI 50-600).
- **Current image DPI detection.** The compress tool measures and shows the
  effective resolution of the images as placed on the pages (median/min/max) -
  in batch mode aggregated across the whole folder (min/median/max plus how
  many files are image-based vs text/vector) - or notes that the document is
  text/vector (where compression is always lossless for the text). Two smart
  warnings follow from it: compressing with a DPI cap at or above the maximum
  image resolution present warns that no downsampling will occur, and rendering
  a scanned/image-only PDF to PNG or image-only PDF above its own scan DPI
  warns that no detail can be gained (the conversion warning is single-file
  only; batch conversion skips the per-file analysis).
- **`Install-pdf-forgeCommand.ps1`**: registers a `pdf-forge` function in the
  user's PowerShell profile(s), so `pdf-forge` typed in any new PowerShell
  window launches the app. No `.cmd` shim and nothing added to PATH.
  User-level, idempotent, reversible; re-run after moving the project folder.
- **Extract images from PDF** main-menu tool (option 8): saves the distinct
  raster images embedded in a PDF into a folder. Repeated placements (e.g. a
  watermark on every page) are extracted once, named `p<page>_<n>.<ext>` after
  the first page they appear on; tiny placeholders are skipped. Default
  quality is **Original** - the raw embedded bytes are copied untouched in
  their native format (zero loss); the seven JPEG levels (Very low 40 ..
  Ultra 95, plus Custom 1-100) re-encode instead, with alpha/CMYK images
  converted safely.
- **Protect PDF** main-menu tool (option 9) with a submenu (AES-256, output is
  a new copy, original never modified):
  - *Password to open (view)* — encrypts so the file needs a password just to
    open; the password is entered twice to confirm.
  - *Restrict editing (owner password + permissions)* — the file opens freely
    but blocks the actions you choose (printing, copying, editing, annotating,
    form filling, page assembly; Enter blocks editing + copying), behind an
    owner password required to change them. Accessibility is always allowed.
- **Unlock PDF** main-menu tool (option 10): removes a PDF's open password and
  permission restrictions into a new copy (the original is never modified).
  An open password must be entered by the user (this is not password
  cracking); owner-only restrictions - forbidding printing, copying, editing,
  annotating, form filling, or page assembly - are lifted without a password,
  and the summary lists exactly which actions were restricted. A PDF that is
  not locked reports that there is nothing to unlock.

### Changed
- **Single PDF engine: PyMuPDF (MuPDF).** Every operation - page tools
  (extract, split, delete), merging, PNG rendering, image-only PDF,
  compression, and watermark removal - now runs on PyMuPDF (previously
  pypdf + pypdfium2). Faster on large documents, with the same safety pattern
  (temp file -> validate -> atomic rename) and identical menu behavior.
- **Watermark removal reimplemented on PyMuPDF.** Repeated images are grouped
  by a content signature (so the same watermark stored as a different object on
  each page is caught). Removal deletes only the chosen image's paint call
  (`/Name Do`) from each page's content stream and then sanitizes the page so
  the now-unused image object is dropped and garbage-collected - it targets
  *only* that image, leaving text, vector graphics, and any other image
  (even one the watermark is stamped on top of) untouched, and the output no
  longer re-detects the removed watermark. Verified on a real 196-page book:
  watermark gone from every page, 12 full-page illustrations kept, file
  slightly smaller.
- Every output PDF is now saved with lossless stream compression and object
  deduplication, so extract/split/merge outputs are often slightly smaller.
- Image-only PDFs keep each page's original physical size explicitly (the
  rasterized image is placed on a page of the same dimensions).
- Dependencies: `pymupdf` (AGPL-licensed; noted in the README) is now the only
  runtime PDF library alongside `pillow`; `pypdf` and `pypdfium2` are removed
  from the runtime (`pypdf` remains a test-only dependency for independent
  output verification).

## [1.4.0] - 2026-07-11

### Added
- **Batch task queue.** Operations no longer run one at a time. Each tool now
  collects all of its inputs, shows its summary, and adds the task to a queue
  instead of writing immediately. After a task is queued you are asked
  `Do you want to queue another task? [y/N]` (default **No**, Enter = No);
  answering `y` returns to the main menu to configure the next task. When you
  finish, a **complete summary** of every queued task is shown followed by a
  single `Start now? [Y/n]` (default **Yes**) that runs the whole queue in order.
  Answering `n` discards the queue. This lets one session, for example, delete
  pages *and* remove a watermark together. Choosing Exit with tasks still queued
  also shows the summary and the start prompt before closing.

### Changed
- The per-operation confirmation prompts (`Create this PDF now?`,
  `Create merged PDF?`, `Convert all these PDFs?`, etc.) were replaced by the
  single `Start now?` confirmation shown once for the whole queue.
- A task that fails while the queue runs is reported and skipped without stopping
  the remaining tasks; each task prints its own result as it completes.
- Output paths are resolved when a task is queued.
- **Internal restructure (no behavior change):** the single `pdf_forge.py`
  (~3.7k lines) was split by responsibility into a `pdf_forge/` package
  (`core`, `pdf_io`, `render`, `watermark`, `ui`, `prompts`, `taskqueue`,
  `ops_*`, `menus`, `app`). Run it with `python -m pdf_forge`; `Run.ps1` was
  updated accordingly. The public API used by the tests is unchanged.

### Removed
- Unused internal helpers with no call sites (`_format_pages`, `print_prompt_line`,
  `print_accent`) and one unreferenced color constant.

## [1.3.0] - 2026-07-03

### Added
- **PDF to images (PNG)** main-menu tool with a submenu:
  - All pages to PNG — renders every page to its own PNG.
  - Selected pages to PNG — renders a chosen page selection.
  - Each PNG is named after its page number (page 2 -> `2.png`), written into a
    `<source>_images` folder beside the source.
- **PDF to image-only PDF** main-menu tool (submenu: single file or batch)
  that rasterizes every page and rebuilds the document so the text is no longer
  selectable or editable.
- **Batch folder mode** for both conversion tools: point at a folder and convert
  every PDF in it (natural order, non-recursive) in one run. Each PDF is
  processed independently into its own `<name>_images` folder or
  `<name>_image.pdf`; a file that fails to open is reported and skipped without
  stopping the batch.
- Three image-quality levels for both conversion tools: Low (96 DPI), Medium
  (150 DPI, default), and High (300 DPI).
- **Remove image watermark** main-menu tool. It detects image XObjects that
  repeat across pages (grouped by a cheap width/height/raw-length signature),
  ranks them by page coverage, writes a PNG preview of each candidate to the
  project-local `temp` folder for visual confirmation, and removes the chosen
  image's paint calls from every page while preserving the text and all other
  content. The preview folder is removed automatically when the operation
  finishes, and the `temp` folder is cleared at startup so nothing is left
  behind after an unexpected exit.
  Content streams are recompressed and the output is written safely; the
  original PDF is never modified. Only repeated image watermarks are supported
  (not text watermarks, optional-content layers, or flattened scans).
  The removed watermark image is physically dropped (not left as an unused
  object) and duplicate objects are merged, so the output is typically smaller
  than the source. Removal is visually lossless: retained page images are never
  re-encoded (verified byte-identical), only the watermark's paint call and its
  now-unused object are removed.
- **Delete pages** main-menu tool (submenu: single file or batch). Pages to
  delete use the extraction syntax (single values and combined ranges, e.g.
  `10-20,25,30-50`). Single-file mode rejects pages that do not exist. Batch
  mode processes every PDF in a folder **per file**: only the requested pages
  that exist in a given file are deleted (each becomes its own
  `<name>_deleted_....pdf`), pages beyond a file's length are skipped for that
  file with a note, and files with none of the requested pages (or where the
  request would remove every page) are skipped with a note; a final summary
  reports processed/skipped/failed counts. Deletion is lossless (kept pages are
  copied, not re-encoded) and the original files are never modified.
- New runtime dependencies `pypdfium2` (page rendering) and `Pillow` (image
  encoding), both permissively licensed prebuilt wheels with no external tools.
- Pure/core helpers (`image_dpi_for_quality`, `build_page_image_name`,
  `default_images_output_dir`, `default_image_pdf_output`) and an image
  rendering I/O layer, all covered by tests including PNG rendering, image-only
  PDF integrity, temp-file cleanup on failure, and Unicode paths.

### Changed
- Image outputs use the same safety pattern as the rest of the app
  (temporary file -> validate -> atomic rename, never overwrite) and the same
  thorough UTC logging.
- Bumped `pypdf` from 5.1.0 to 6.14.2. The only breaking change in pypdf 6.0
  was dropping Python 3.8 support, which does not affect this project
  (Python 3.10+ is required). All APIs used here are unchanged.
- Operation titles are shown as plain headings (no `== ... ==` decoration).
- Every file-output prompt and summary line is now consistently labelled
  "Output Path" (or "Default Output Path" for the pre-fill summary), across
  extract, merge, image-only PDF, watermark removal, and delete-pages. Output
  *folder*/*directory* labels are unchanged. The watermark candidate selection
  defaults to `1` (Enter picks the top match).

### Fixed
- Consistent Back control: the watermark selection prompt now shows the standard
  orange `back=0` hint (was `cancel=0`), matching every other prompt. `0` goes
  back exactly one level everywhere.
- Merge back navigation: the merge submenu is now the single Back hub for the
  whole operation. Pressing `0` at any step (source picker, output path) or
  cancelling at confirmation returns to the merge submenu instead of jumping
  straight to the main menu. Only `0` at the merge submenu returns to the main
  menu, matching how every other menu behaves (one level back per `0`).

## [1.2.0] - 2026-06-30

### Added
- Folder-based merge now shows a full **merge summary** before confirmation:
  total PDF count, total page count, resolved output path, sorting mode, and the
  final merge order (full list for small sets; first items plus the last few
  with a gap indicator for long lists).
- `natural_sort_key` helper providing natural, case-insensitive, stable
  ordering.
- GitHub Actions CI (`.github/workflows/ci.yml`) running the test suite on Linux
  and Windows across Python 3.10-3.13, and a Dependabot config
  (`.github/dependabot.yml`) for the Actions and pip ecosystems.

### Changed
- Folder-based merge ordering now uses **natural sorting** (case-insensitive and
  stable), so files such as `1.pdf`, `2.pdf`, and `10.pdf` are ordered 1, 2, 10
  instead of the lexical 1, 10, 2.
- The merge flow now resolves the output path before showing the summary, so the
  confirmation reflects the exact output that will be written.
- Significantly expanded file logging: application/runtime/environment details,
  menu navigation, selected operation, every source PDF opened (with page
  count), merge sorting mode and summary, per-source page totals during a merge,
  temporary-file creation and validation, write durations, completion results,
  errors with stack traces, and the final exit code.

## [1.1.0] - 2026-06-30

### Added
- Merge operation that combines multiple PDFs into a single new file, with two
  input modes:
  - Add PDF files one by one (order preserved, duplicates rejected, minimum 2).
  - Use all PDFs from a folder (non-recursive).
- Safe merge writer with output validation (openable, not encrypted, correct
  total page count) using the temporary-file -> validate -> atomic-rename
  pattern.
- Main menu with a **Page tools** submenu containing the extract and split
  operations.
- `requirements-dev.txt` for the `pytest` development dependency.
- Tests for merging, folder discovery, ordering, source integrity, output
  collision, duplicate handling, temporary-file cleanup, and Unicode paths.

## [1.0.0] - 2026-06-20

### Added
- Interactive colored terminal menu with two operations:
  - Extract selected pages into a new PDF.
  - Split a PDF into fixed-size page-range chunks.
- Flexible page-selection syntax: ranges (`10-20`), lists (`1,2`), and mixed
  expressions (`10-20,25,30-50`), with whitespace tolerance.
- Multi-file extraction using the `|` separator (e.g. `6-37|39-85|353-375`)
  to produce one output file per group.
- Optional start/end page range for the split operation; press Enter to use
  the document's natural boundaries.
- Safe output writing via temporary files with atomic rename and validation.
- Never overwrites existing files; generates unique names/folders instead.
- Never modifies the source PDF, including source/output collision detection.
- Encrypted-PDF handling with a hidden password prompt (never logged).
- UTC file logging with a uniquely named log per run.
- PowerShell launcher (`Run.ps1`) that verifies Python 3.10+, manages a local
  virtual environment, and installs dependencies on first run.
- Automated test suite.

[1.5.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.5.0
[1.4.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.4.0
[1.3.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.3.0
[1.2.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.2.0
[1.1.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.1.0
[1.0.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.0.0
