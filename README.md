# PDF Forge

[![CI](https://github.com/KiaroSama/PDF-Forge/actions/workflows/ci.yml/badge.svg)](https://github.com/KiaroSama/PDF-Forge/actions/workflows/ci.yml)

PDF Forge is a safe local CLI tool to extract, split, and merge PDF files. It is
an interactive, Windows-friendly command-line utility: pull a custom selection
of pages into a new document, break a large PDF into fixed-size page-range
files, or combine several PDFs into one. The original PDFs are never modified,
overwritten, or deleted.

## What it does

- **Extract selected pages** into a single new PDF using flexible expressions
  such as `10-20,25,30-50`.
- **Split into fixed-size chunks**, for example every 50 pages, into a dedicated
  output folder.
- **Merge multiple PDFs** into one new file, either by adding paths one by one
  or by combining every PDF in a folder.
- Preserves the original page content (no rasterizing or re-encoding).
- Writes output safely using temporary files and atomic renames, and never
  overwrites existing files.

## Requirements

- Windows with PowerShell.
- Python 3.10 or newer (`py` or `python` on the `PATH`).
- The launcher creates a local virtual environment and installs
  [`pypdf`](https://pypi.org/project/pypdf/) automatically on first run.

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
.\.venv\Scripts\python.exe pdf_forge.py
```

## Menus

```
PDF Forge Main menu:
  1. Page tools [1]
  2. Merge multiple PDFs
  0. Exit
```

- Option `1` is the default; pressing **Enter** opens **Page tools**.
- Type `exit` or `quit` at any prompt to close the app immediately.
- In the main menu, `0` exits. In submenus, `0` means **Back**.
- After each operation you return to the menu you came from.

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
4. Review the summary and confirm. Pressing Enter confirms (default Yes).

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
4. Review the preview of output ranges and confirm.

If the chunk size is greater than or equal to the selected span, you are warned
that only one output file will be created and asked to confirm.

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
source (failing before any write if one cannot be opened), then shows a **merge
summary** and asks `Create merged PDF? [Y/n]` (Enter confirms). The summary
includes:

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
pdf_forge.py          Main application
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
