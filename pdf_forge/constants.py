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


__all__ = ['APP_NAME', 'LOG_PREFIX', 'APP_VERSION', 'IMAGE_QUALITY_DPI', 'DEFAULT_IMAGE_QUALITY', 'logger']

APP_NAME = "PDF Forge"            # User-facing application name (never change spelling).


LOG_PREFIX = "PDF Forge"          # Log filename prefix.


APP_VERSION = "1.4.0"


IMAGE_QUALITY_DPI = {"low": 96, "medium": 150, "high": 300}


DEFAULT_IMAGE_QUALITY = "medium"


logger = logging.getLogger("pdf_forge")
