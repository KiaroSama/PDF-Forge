# Changelog

All notable changes to PDF Forge are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.3.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.3.0
[1.2.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.2.0
[1.1.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.1.0
[1.0.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.0.0
