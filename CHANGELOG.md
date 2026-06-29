# Changelog

All notable changes to PDF Forge are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-06-30

### Added
- Folder-based merge now shows a full **merge summary** before confirmation:
  total PDF count, total page count, resolved output path, sorting mode, and the
  final merge order (full list for small sets; first items plus the last few
  with a gap indicator for long lists).
- `natural_sort_key` helper providing natural, case-insensitive, stable
  ordering.

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

[1.2.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.2.0
[1.1.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.1.0
[1.0.0]: https://github.com/KiaroSama/PDF-Forge/releases/tag/v1.0.0
