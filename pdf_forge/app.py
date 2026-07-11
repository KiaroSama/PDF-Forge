from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .logsetup import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .watermark import *  # noqa: F401,F403
from .ops_watermark import *  # noqa: F401,F403
from .menus import *  # noqa: F401,F403

__all__ = ['main']

def main(argv: Optional[Sequence[str]] = None) -> int:
    """Application entry point."""
    enable_ansi_colors()
    # __file__ is inside the pdf_forge/ package; the project root (where logs/
    # and temp/ live) is its parent.
    script_dir = Path(__file__).resolve().parent.parent
    log_path = setup_logging(script_dir)

    logger.info("=== %s v%s starting ===", APP_NAME, APP_VERSION)
    logger.info("Python %s on %s", sys.version.split()[0], sys.platform)
    logger.info("Executable: %s", sys.executable)
    logger.info("Operating system: %s", os.name)
    logger.info("Script directory: %s", script_dir)
    logger.info("Working directory: %s", os.getcwd())
    if log_path is not None:
        logger.info("Log file: %s", log_path)
    else:
        logger.warning("Persistent file logging is unavailable; using console fallback.")

    # Clear the project-local temp folder (e.g. leftover preview images).
    cleanup_temp_dir()

    print_banner(APP_NAME)
    if log_path is not None:
        print_note(f"Logging to: {log_path}")

    # Verify the PDF backend early for a friendly message.
    try:
        _import_pypdf()
    except RuntimeError as exc:
        print_error(str(exc))
        logger.critical("pypdf import failed: %s", exc)
        return 2

    try:
        exit_code = main_menu()
    except KeyboardInterrupt:
        print_warning("\nInterrupted. Exiting.")
        logger.warning("Application interrupted at top level.")
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 - log any unexpected top-level error
        print_error(f"Unexpected error: {exc}")
        logger.exception("Unhandled top-level exception.")
        exit_code = 1
    finally:
        logger.info("=== %s shutting down (exit code %s) ===", APP_NAME, exit_code)
        logging.shutdown()

    return exit_code
