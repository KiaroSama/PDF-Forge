# PDF Forge

[![CI](https://github.com/KiaroSama/PDF-Forge/actions/workflows/ci.yml/badge.svg)](https://github.com/KiaroSama/PDF-Forge/actions/workflows/ci.yml)

PDF Forge is a safe local CLI tool to extract, split, merge, and convert PDF
files. It is an interactive, Windows-friendly command-line utility: pull a
custom selection of pages into a new document, break a large PDF into
fixed-size page-range files, combine several PDFs into one, or convert pages to
images. The original PDFs are never modified, overwritten, or deleted.

## What it does

- **Extract selected pages** into a single new PDF using flexible expressions
  such as `10-20,25,30-50`.
- **Split into fixed-size chunks**, for example every 50 pages, into a dedicated
  output folder.
- **Merge multiple PDFs** into one new file, either by adding paths one by one
  or by combining every PDF in a folder.
- **Convert a PDF to images (PNG)** — export all pages, or a chosen selection,
  as PNG files (one file per page, named after the page number).
- **Convert a PDF to an image-only PDF** — rasterize every page and rebuild the
  document so the text is no longer selectable or editable.
- **Remove an image watermark** — detect images that repeat across pages,
  preview them, and remove the one you choose from every page while keeping the
  text and all other content intact.
- **Delete pages** — remove one or more pages (single values or combined ranges)
  from a single PDF or, in batch, from every PDF in a folder.
- **Compress PDF** — reduce the file size into a new file. Ultra mode is fully
  lossless (structure optimization and font subsetting only, zero quality
  change); the other levels also downsample and re-encode embedded images.
- Seven quality levels everywhere a quality choice appears (Very low, Low,
  Medium, High, Very high, Ultra, Custom) — for the conversion tools they map
  to render DPI (72/96/150/300/450/600/custom), for compression to
  image-recompression strength.
- Preserves the original page content for extract/split/merge (no rasterizing or
  re-encoding).
- Writes output safely using temporary files and atomic renames, and never
  overwrites existing files.

## Requirements

- Windows with PowerShell.
- Python 3.10 or newer (`py` or `python` on the `PATH`).
- The launcher creates a local virtual environment and installs the
  dependencies automatically on first run:
  [`PyMuPDF`](https://pypi.org/project/PyMuPDF/) (the single PDF engine for
  every operation — page tools, merging, rendering, compression, and watermark
  removal; AGPL-licensed) and
  [`Pillow`](https://pypi.org/project/pillow/) (image validation and previews).
  Both ship as prebuilt wheels, so no external tools are required.

## How to run it

The recommended way is the PowerShell launcher:

1. Open the project folder.
2. Right-click `Run.ps1` and choose **Run with PowerShell**, or run it from a
   PowerShell prompt:

   ```powershell
   .\Run.ps1
   ```

On the first run the launcher will:

- Verify that Python 3.10+ is available.
- Create a `.venv` virtual environment in the project folder.
- Install dependencies from `requirements.txt` (only once, unless the
  requirements change).
- Start the interactive menu.

You can also run the application directly once the environment exists:

```powershell
.\.venv\Scripts\python.exe -m pdf_forge
```

### Install the `pdf-forge` command (optional)

Run `Install-pdf-forgeCommand.ps1` (right-click → **Run with PowerShell**) to
add the project's `bin` folder to your **user** PATH. After that, typing
`pdf-forge` in any new terminal — PowerShell or cmd — launches the app from
anywhere:

```
> pdf-forge
```

- User-level only (no administrator rights), idempotent (safe to re-run), and
  reversible (remove the single PATH entry it prints).
- If you move the project folder, run the installer again from the new
  location.

## Menus

```
PDF Forge Main menu:
  1. Page tools [1]
  2. Merge multiple PDFs
  3. PDF to images (PNG)
  4. PDF to image-only PDF
  5. Remove image watermark
  6. Delete pages
  7. Compress PDF (reduce file size)
  0. Exit
```

- Option `1` is the default; pressing **Enter** opens **Page tools**.
- Type `exit` or `quit` at any prompt to close the app immediately.
- In the main menu, `0` exits. In submenus, `0` means **Back**.

### Batch queue (run several tasks together)

PDF Forge collects tasks and runs them **together at the end** rather than one at
a time. Each operation asks all its questions up front, shows a short summary,
and **adds itself to a queue** instead of writing files immediately. So a single
session can, for example, delete some pages *and* remove a watermark in one go.

The flow is:

1. Pick an operation from the menu and configure it to the end. Its summary is
   shown and it is added to the queue (`Added to queue (#1): ...`).
2. You are asked **`Do you want to queue another task? [y/N]`** — the default is
   **No** (pressing **Enter** means No).
   - **`y`** returns you to the main menu to configure the next task.
   - **`n`** / **Enter** finishes queueing.
3. When you finish, a **complete summary** of every queued task is shown, then
   **`Start now? [Y/n]`** (default **Yes**):
   - **`y`** / **Enter** runs the whole queue in order; each task prints its own
     result, and a task that fails is reported without stopping the rest.
   - **`n`** cancels and discards the queued tasks.

Choosing `Exit` (or typing `exit`) with tasks still queued also shows the
complete summary and the `Start now?` prompt before the app closes. Cancelling or
backing out of an operation adds nothing to the queue.

Page tools submenu:

```
PDF Forge Page tools:
  1. Extract selected pages [1]
  2. Split PDF into fixed-size chunks
  0. Back
```

### Page tools → Extract selected pages

1. Enter the full path to the source PDF (quotes are accepted and stripped).
2. The total page count is shown.
3. Enter a page-selection expression.
4. Review the summary; the task is added to the queue (see **Batch queue**).

Use a comma `,` to combine pages into one file, and a vertical bar `|` to
produce several separate files in a single run:

- `6-37,39-85,353-375` -> one file containing all those pages.
- `6-37|39-85|353-375` -> three separate files (pages 6-37, 39-85, 353-375).
- `6-37,39-85|353-375` -> two files: the first combines 6-37 and 39-85, the
  second contains 353-375.

### Page tools → Split into fixed-size chunks

1. Enter the source PDF path.
2. Enter a positive whole number of pages per file.
3. Optionally set a **start page** and **end page** (asked separately). Press
   Enter to keep the document's natural boundaries (start `1`, end last page).
4. Review the preview of output ranges; the task is added to the queue.

If the chunk size is greater than or equal to the selected span, you are warned
that only one output file will be created and asked to confirm before continuing.

### Merge multiple PDFs

Selecting `2` in the main menu opens the merge submenu (same style as Page
tools):

```
PDF Forge Merge:
  1. Add PDF files one by one [1]
  2. Use all PDFs from a folder
  0. Back
```

**Mode 1 — Add PDF files one by one**

1. Enter PDF paths one at a time (quotes are accepted and stripped).
2. At least 2 valid PDFs are required.
3. Press **Enter** on an empty prompt (after at least 2 files) to finish.
4. Enter `0` to cancel and go back.
5. The merge order matches exactly the order you enter.
6. Duplicate files are rejected with a clear message.

**Mode 2 — Use all PDFs from a folder**

1. Enter a folder path.
2. PDF Forge discovers every `*.pdf` directly inside that folder
   (non-recursive).
3. At least 2 PDFs must be found, otherwise a clear error is shown.
4. Files are ordered by **natural** name order: case-insensitive and stable, so
   `1.pdf`, `2.pdf`, and `10.pdf` are ordered 1, 2, 10 (not the lexical
   1, 10, 2).

**Merge summary and confirmation (both modes)**

After the sources are chosen and you pick the output path, PDF Forge opens every
source (failing before it is queued if one cannot be opened), then shows a
**merge summary** and adds the merge to the queue (see **Batch queue**). The
summary includes:

- total PDF count,
- total page count,
- the sorting mode (`natural, case-insensitive, stable` for folder mode;
  `manual (exact order entered)` for file-by-file mode),
- the resolved output path,
- the final merge order — the full list for small sets, or the first items and
  the last few with a `... (+N more) ...` gap indicator for long lists.

#### Merge examples

File-by-file merge (one combined PDF beside the first source):

```
Main menu -> 2 (Merge) -> 1 (Add files one by one)
  PDF file #1: C:\docs\chapter1.pdf
  PDF file #2: C:\docs\chapter2.pdf
  PDF file #3: <Enter to finish>
  -> C:\docs\chapter1_merged.pdf
```

Folder-based merge (combine every PDF in a folder):

```
Main menu -> 2 (Merge) -> 2 (Use all PDFs from a folder)
  Folder containing PDFs: C:\docs\report-parts
  -> C:\docs\report-parts\report-parts_merged.pdf
```

### PDF to images (PNG)

Selecting `3` in the main menu opens the image-export submenu (same style as
Page tools):

```
PDF Forge PDF to images:
  1. All pages to PNG [1]
  2. Selected pages to PNG
  3. Batch: all PDFs in a folder to PNG
  0. Back
```

**Sub-option 1 — All pages to PNG**

1. Enter the source PDF path.
2. Choose the output image quality — seven levels: `1` Very low (72 DPI),
   `2` Low (96), `3` Medium (150), `4` High (300), `5` Very high (450),
   `6` Ultra (600), `7` Custom (any DPI from 30 to 1200). Enter = Medium.
   For scanned/image-only sources, choosing a DPI above the scan's own
   resolution shows a warning — rendering higher cannot add detail, it only
   produces larger files (text/vector PDFs do sharpen at higher DPI, so no
   warning there).
3. Review the summary (source, total pages, quality, output folder) and pick the
   output folder (Enter accepts the default beside the source).
4. The task is added to the queue; every page is rendered to its own PNG when
   the queue runs.

**Sub-option 2 — Selected pages to PNG**

1. Enter the source PDF path.
2. Choose the output image quality.
3. Enter a page-selection expression (same syntax as extract, without the `|`
   separator), e.g. `5`, `10-20`, or `10-20,25,30-50`.
4. Review the summary and pick the output folder.
5. Confirm. Each selected page is rendered to its own PNG.

**Sub-option 3 — Batch: all PDFs in a folder to PNG**

1. Enter a folder path. Every `*.pdf` directly inside it (non-recursive, natural
   order) is processed.
2. Choose the output image quality (applied to all files).
3. Review the summary (folder, file count, quality); the task is queued.
4. Every page of every PDF is rendered. Each PDF gets its own
   `<name>_images` folder beside it. A file that cannot be opened is reported
   and skipped; the batch continues, and totals are shown at the end.

Each PNG is named after its page number, so page 2 becomes `2.png`. Images are
written into a folder named `<source>_images` beside the source PDF (a unique
folder such as `<source>_images_2` is used if one already exists).

```
Main menu -> 3 (PDF to images) -> 1 (All pages to PNG)
  Source PDF path: C:\docs\report.pdf
  Output image quality [2]: 3
  -> C:\docs\report_images\1.png, 2.png, 3.png, ...
```

### PDF to image-only PDF

Selecting `4` in the main menu rasterizes PDFs and rebuilds them as image-only
documents. This makes the content non-editable: the text becomes images and is
no longer selectable or searchable. The output is typically larger than the
source. It opens a submenu (same style as Page tools):

```
PDF Forge PDF to image-only PDF:
  1. Single PDF [1]
  2. Batch: all PDFs in a folder
  0. Back
```

**Sub-option 1 — Single PDF**

1. Enter the source PDF path.
2. Choose the output image quality.
3. Review the summary and pick the output path (Enter accepts
   `<source>_image.pdf` beside the source).
4. Confirm. Every page is rasterized at the chosen quality and combined into one
   PDF.

**Sub-option 2 — Batch: all PDFs in a folder**

1. Enter a folder path. Every `*.pdf` directly inside it (non-recursive, natural
   order) is processed.
2. Choose the output image quality (applied to all files).
3. Review the summary; the task is added to the queue.
4. Each PDF is rasterized into its own `<name>_image.pdf` beside it. A file that
   cannot be opened is reported and skipped; the batch continues, and totals are
   shown at the end.

```
Main menu -> 4 (PDF to image-only PDF) -> 1 (Single PDF)
  Source PDF path: C:\docs\contract.pdf
  Output image quality [2]: 2
  -> C:\docs\contract_image.pdf   (rasterized, not editable)
```

### Remove image watermark

Selecting `5` in the main menu removes an **image-based** watermark that repeats
across pages (for example a site/scanlation badge stamped on every page). The
text layer and all other content are preserved.

How it works:

1. Enter the source PDF path.
2. PDF Forge scans the document and lists the images that repeat across pages,
   ranked by how many pages they cover (the watermark is usually the top
   candidate at or near 100%).
3. A **preview PNG** of each candidate is written to the project-local `temp`
   folder (`PDF Forge/temp`) so you can open them and confirm which image is the
   watermark before removing anything. That folder is removed automatically when
   the operation finishes, and cleared at startup if anything was left behind.
4. Choose the candidate(s) to remove (e.g. `1`, or `1,3` for several).
5. Review the summary and pick the output path (Enter accepts
   `<source>_nowatermark.pdf` beside the source); the task is added to the queue.
6. When the queue runs, the watermark's paint calls are removed from every page,
   the now-unused watermark image is physically dropped, duplicate objects are
   merged, and a new file is written. The original is never modified.

Removal is **visually lossless**: the page images and text are preserved exactly
(retained images are never re-encoded), so only the watermark disappears. Because
the unused watermark image is deleted and objects are deduplicated, the output is
usually a little smaller than the source.

```
Main menu -> 5 (Remove image watermark)
  Source PDF path: C:\books\volume.pdf
  Watermark candidates:
    [1] 899x674px  on 231/231 pages (100%) - preview: candidate_1.png
  Watermark(s) to remove: 1
  -> C:\books\volume_nowatermark.pdf
```

**Limits:** this only removes image watermarks that repeat across pages. It
cannot remove text-based watermarks, optional-content layers, or a watermark
that is baked into a scanned/flattened page image. Because you confirm the
candidate from a preview, legitimate repeated logos are not removed by accident.

### Delete pages

Selecting `6` in the main menu removes pages from PDFs. It opens a submenu (same
style as the others):

```
PDF Forge Delete pages:
  1. Single PDF [1]
  2. Batch: all PDFs in a folder
  0. Back
```

The pages to delete use the same syntax as extraction: single values and
combined ranges, e.g. `5`, `10-20`, or `10-20,25,30-50`.

**Sub-option 1 — Single PDF**

1. Enter the source PDF path.
2. Enter the pages to delete. Pages that do not exist in the document are
   rejected so you can re-enter.
3. Review the summary (pages to delete, pages remaining) and pick the output
   path (Enter accepts `<source>_deleted_....pdf` beside the source).
4. Confirm. A new PDF is written with those pages removed; the original is never
   modified.

**Sub-option 2 — Batch: all PDFs in a folder**

1. Enter a folder path (every `*.pdf` directly inside it is processed).
2. Enter the pages to delete.
3. Review the summary; the task is added to the queue.
4. Each PDF is handled **per file**: only the requested pages that actually
   exist in that file are deleted, and each file becomes its own
   `<name>_deleted_....pdf`. When some requested pages are beyond a file's
   length, they are skipped for that file and a **note** explains what happened.
   Files that contain none of the requested pages, or where the request would
   remove every page, are skipped with a note. A per-file progress line and a
   final summary (processed / skipped / failed and total pages deleted) are
   shown.

```
Main menu -> 6 (Delete pages) -> 2 (Batch), delete 4-6, on a/b/c.pdf:
  [1/3] a.pdf  -> deleted 3 page(s) [4-6]; kept 7 -> a_deleted_4-6.pdf
  [2/3] b.pdf  -> deleted 2 page(s) [4-5]; kept 3 -> b_deleted_4-5.pdf
                 Note: pages not in this file were skipped: 6 (has 5 page(s)).
  [3/3] c.pdf  Note: none of the requested pages exist here (has 3 page(s)); skipped.
  Done. Processed 2 file(s), skipped 1, failed 0; 5 page(s) deleted in total.
```

Deletion is lossless: the kept pages are copied as-is (no re-encoding), so image
and text quality is preserved.

### Compress PDF (reduce file size)

Selecting `7` in the main menu compresses a PDF into a smaller new file. The
original is never modified.

What each level does:

- **Always applied (lossless, zero quality change):** duplicate/unused objects
  are removed, streams are recompressed, PDF object streams are generated, and
  embedded fonts are subset to only the glyphs the document actually uses.
- **Ultra** stops there — the pages are pixel-for-pixel identical.
- **Very high → Very low** additionally downsample embedded images above a DPI
  cap and re-encode them as JPEG:

  | Level     | JPEG quality | Image DPI cap | Typical use                     |
  |-----------|--------------|---------------|---------------------------------|
  | Ultra     | untouched    | untouched     | zero quality change             |
  | Very high | 90           | 250           | near-invisible change (default) |
  | High      | 85           | 200           | prints and reports              |
  | Medium    | 75           | 150           | screen reading                  |
  | Low       | 60           | 120           | email attachments               |
  | Very low  | 40           | 96            | smallest possible file          |
  | Custom    | 1-100        | 50-600        | your own trade-off              |

- Black-and-white (fax/scan bitonal) images are never re-encoded — recompressing
  them usually makes quality worse for no gain.

**If the PDF is a scanned/image-only document**, the entire page content *is*
an image: Ultra saves little, while the lossy levels act on the whole page —
savings are large but quality loss is visible at the lower levels.

**The DPI value is the only criterion for images**, and no image is ever
enlarged:

- If your DPI cap is **higher than** the document's maximum image DPI → a
  warning tells you nothing can be downsampled (only re-encoding applies).
- If it **equals** the maximum → no image changes resolution.
- If it is **lower** → each image above the cap comes down toward it (in
  quality-preserving, roughly-halving steps, never below the cap), and images
  already at or below the cap are left untouched.

Selecting `7` opens a submenu — **single file** or **batch folder**:

Single file:

1. Enter the source PDF path (size and page count are shown), along with the
   document's **current image resolution** — the median/min/max effective DPI
   of the raster images as placed on the pages (a text/vector PDF shows a note
   instead: text is never degraded, all levels are effectively lossless there).
2. Pick the compression level (`Enter` = Very high; `7` = Custom asks for a
   JPEG quality and a target image DPI).
3. Review the summary and pick the output path (Enter accepts
   `<source>_compressed.pdf` beside the source); the task is added to the queue.
4. When the queue runs, the result line shows the old size, the new size, and
   the saving (e.g. `126.5 KB -> 39.0 KB (saved 87.4 KB, 69.1%)`).

Batch folder:

1. Enter a folder path. PDF Forge scans every PDF and shows the **folder-wide
   image DPI range** (min / median / max) plus how many files are image-based
   vs text/vector.
2. Pick one compression level applied to all files (the same DPI-cap warning
   applies against the folder's maximum).
3. Each PDF becomes `<name>_compressed.pdf` beside it; a per-file line and a
   grand total (bytes saved) are shown. A file that fails is reported and
   skipped without stopping the batch.

```
Main menu -> 7 (Compress PDF) -> 1 (Single PDF)
  Source PDF path: C:\docs\report.pdf
  Loaded 'report.pdf' - 48 page(s), 12.40 MB.
  Current image DPI: ~300 median (min 300, max 300; 48 image(s) measured)
  Compression level [5]: <Enter>
  Output Path [report_compressed.pdf beside source]: <Enter>
  Added to queue (#1): Compress report.pdf (very high) -> report_compressed.pdf
```

## Page-selection examples

| Input              | Result                                             |
|--------------------|----------------------------------------------------|
| `5`                | Page 5                                             |
| `1,2`              | Pages 1 and 2                                       |
| `10-20`            | Pages 10 through 20 (inclusive)                     |
| `10-20,25,30-50`   | Pages 10-20, page 25, and pages 30-50 (one file)    |
| `1, 2, 5-10`       | Spaces are allowed                                  |
| `10 - 20, 25`      | Spaces around the dash are allowed                  |
| `6-37\|39-85`       | Two separate files (`\|` splits into files)         |

Page numbers are 1-based and inclusive. The order you type is preserved.
Duplicate pages are removed (first occurrence kept) and a warning is shown.
Invalid input (empty elements, non-numeric values, reversed ranges like
`20-10`, zero/negative pages, or pages beyond the document) produces a clear
error message instead of a crash.

## Output naming examples

**Extract** – saved next to the source PDF by default:

- `OriginalName_pages_10-20_25_30-50.pdf`
- Compact fallback when the name would be too long:
  `OriginalName_selected_37_pages.pdf`
- If a file already exists, a unique name is generated:
  `OriginalName_pages_1-10_2.pdf`
- With the `|` separator, one file per group is created, each named after its
  group, e.g. `OriginalName_pages_6-37.pdf`, `OriginalName_pages_39-85.pdf`.

**Split** – saved into a dedicated folder next to the source PDF:

- Folder: `OriginalName_split_50_pages` (a unique folder such as
  `OriginalName_split_50_pages_2` is used if needed).
- When a custom start/end range is used, the span is added to the folder name,
  e.g. `OriginalName_split_50_pages_20-280`.
- Files (zero-padded to the document length):
  `OriginalName_pages_001-050.pdf`, `OriginalName_pages_051-100.pdf`.

**Merge** – a single combined file:

- File-by-file mode: `<first-stem>_merged.pdf` beside the first source PDF.
- Folder mode: `<folder-name>_merged.pdf` inside the selected folder.
- If the default already exists, a unique name such as `..._merged_2.pdf` is
  used.

**PDF to images (PNG)** – saved into a folder beside the source PDF:

- Folder: `<source>_images` (a unique folder such as `<source>_images_2` is used
  if needed).
- Files are named after the page number: `1.png`, `2.png`, `10.png` (no
  zero-padding). Selected-page exports keep each page's real number, so
  exporting pages 1, 3, 5 produces `1.png`, `3.png`, `5.png`.

**PDF to image-only PDF** – a single rasterized file:

- `<source>_image.pdf` beside the source PDF (a unique name such as
  `<source>_image_2.pdf` is used if needed).

## Safety guarantees

- Source PDFs are opened read-only and never written to.
- Any output path that resolves to a source PDF is rejected (including every
  source in a merge).
- Output is written to a temporary file, validated, then atomically renamed.
- Existing files are never overwritten; a unique name is generated instead.
- On a partial failure during a split, already-completed valid files are kept
  and only the current operation's temporary files are removed.
- A merge opens all sources first and fails before writing if any source cannot
  be opened, so no partial output is ever produced.

## Logging

Operational logs are written to the `logs` directory next to the script. Each
run creates a uniquely named file using UTC timestamps to the second, for
example `PDF Forge_2026-06-30_14-32-08_UTC.log`. Console output stays concise;
detailed diagnostics go to the log. PDF passwords are never logged.

Logging is intentionally thorough. Each run records the application version,
Python runtime and platform, resolved script/working directories, the log path,
menu navigation, the selected operation, every source PDF opened (with its page
count), the merge sorting mode and full summary, per-source page totals during a
merge, temporary-file creation and validation, write durations, completion
results, any errors with stack traces, and the final exit code. Standard log
levels are used (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`); the file
captures `DEBUG` and above while the console stays quiet.

## Encrypted PDFs

- Encrypted PDFs are detected automatically.
- If the file opens with an empty password, processing continues.
- Otherwise you are prompted for a password (input hidden where supported).
- The password is never stored or logged. If decryption fails, a clear error is
  shown and you return to the menu. PDF Forge does not attempt to bypass
  encryption.

## Project structure

```
Run.ps1               PowerShell launcher (the only launcher)
Install-pdf-forgeCommand.ps1   Adds the pdf-forge command to the user PATH
bin/pdf-forge.cmd     Command shim used by the PATH installer
pdf_forge/            Main application package (run with: python -m pdf_forge)
  __main__.py         Entry point
  app.py              main()
  menus.py            Menu rendering and the main loop
  taskqueue.py        Batch task queue (queue, summary, run)
  ops_*.py            Operations: pages, merge, convert, watermark, compress
  prompts.py          Interactive prompts and output-path pickers
  core.py             Pure logic: page parsing, chunking, filename rules
  pdf_io.py render.py compress.py watermark.py   I/O adapters (PyMuPDF engine)
  ui.py logsetup.py constants.py     Terminal UI, logging, constants
requirements.txt      Python runtime dependencies
requirements-dev.txt  Development dependencies (pytest)
README.md             This file
CHANGELOG.md          Version history
LICENSE               MIT license
.gitignore
.github/              GitHub Actions CI and Dependabot config
tests/                Automated tests
logs/                 Created at runtime
```

## Development and testing

Set up a development environment and run the test suite:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

## Continuous integration

Every push and pull request to `main` runs the test suite on GitHub Actions
(`.github/workflows/ci.yml`) across Linux and Windows on Python 3.10, 3.11,
3.12, and 3.13. The badge at the top of this file shows the current status. The
workflow simply installs the runtime and development dependencies and runs
`pytest`, the same command you can run locally. Dependabot
(`.github/dependabot.yml`) keeps the Actions and Python dependencies current.

## Troubleshooting

- **Python not found / too old**: Install Python 3.10+ from
  <https://www.python.org/downloads/> and ensure `py` or `python` is on your
  `PATH`. The launcher shows a red error and waits for Enter if Python is
  missing.
- **Permission errors**: Choose an output directory you can write to, and make
  sure the source PDF is not open in another program. The launcher does not
  require administrator privileges.
- **Encrypted PDFs**: Provide the correct password when prompted. PDF Forge
  cannot open a PDF whose password is unknown.
- **Corrupted PDFs**: If a file cannot be parsed, PDF Forge reports the problem
  and returns to the menu without crashing.
- **Output folder conflicts**: PDF Forge never overwrites existing files. It
  generates a unique filename or output folder automatically.

## License

PDF Forge is free and open-source software released under the
[MIT License](LICENSE). You are free to use, modify, and distribute it.

Copyright (c) 2026 Kiaro Sama

## Attribution

Author: Kiaro Sama  
GitHub: https://github.com/KiaroSama

## Donate

If this project helps you, donations are appreciated.

| Currency | Network | Address |
| --- | --- | --- |
| Bitcoin (BTC) | Bitcoin | `bc1qmth5m03pu5hujw5xw5jmywam3jj3sqwqupesdt` |
| USDT, BNB, USDC, etc. | BEP20 | `0x0Bd0BA443a8B9cf15922bf7f0Bb0a4b495fD06Ef` |
| USDT, TRX, USDC, etc. | TRC20 | `TWBA3xFTqgZAeAYMxqo85xWnzvty3DcAhw` |
| Ethereum (ETH) | ERC20 | `0x0Bd0BA443a8B9cf15922bf7f0Bb0a4b495fD06Ef` |
| TON | TON | `UQCN8Umo_OfOWqImZetQsrNStPcmLkMAKajFyiCOhso23NDb` |
| Litecoin (LTC) | LTC | `ltc1qntqnnrunadurnw4cshv3qgspywrueyyeyngwuy` |
| Solana (SOL) | Solana | `7B2wkczUjmkDhETwQuknBL8sUsbuV7nErxc317TmQuwR` |
| Polygon (POL) | Polygon | `0x0Bd0BA443a8B9cf15922bf7f0Bb0a4b495fD06Ef` |
