from __future__ import annotations

import logging


__all__ = ['APP_NAME', 'LOG_PREFIX', 'APP_VERSION', 'IMAGE_QUALITY_DPI',
           'COMPRESSION_PRESETS', 'logger']

APP_NAME = "PDF Forge"            # User-facing application name (never change spelling).


LOG_PREFIX = "PDF Forge"          # Log filename prefix.


APP_VERSION = "2.0.1"


# Rendering quality levels for image conversion, mapped to a resolution in DPI.
# Rendering scale is DPI / 72 (PDF user-space is 72 units per inch).
IMAGE_QUALITY_DPI = {
    "very low": 72,
    "low": 96,
    "medium": 150,
    "high": 300,
    "very high": 450,
    "ultra": 600,
}


# Compression levels for the "Compress PDF" tool, mapped to
# (jpeg_quality, dpi_target) for embedded-image recompression/downsampling.
# ``None`` = lossless-only: no image is touched (structure optimization,
# stream deflate, and font subsetting only - zero quality change).
COMPRESSION_PRESETS = {
    "very low": (40, 96),
    "low": (60, 120),
    "medium": (75, 150),
    "high": (85, 200),
    "very high": (90, 250),
    "ultra": None,
}


logger = logging.getLogger("pdf_forge")
