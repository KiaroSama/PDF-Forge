"""PDF Forge - interactive PDF page tools and merge utility (package)."""
from __future__ import annotations

from .constants import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .safeio import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .logsetup import *  # noqa: F401,F403
from .pdf_io import *  # noqa: F401,F403
from .render import *  # noqa: F401,F403
from .watermark import *  # noqa: F401,F403
from .compress import *  # noqa: F401,F403
from .unlock import *  # noqa: F401,F403
from .encrypt import *  # noqa: F401,F403
from .office import *  # noqa: F401,F403
from .prompts import *  # noqa: F401,F403
from .taskqueue import *  # noqa: F401,F403
from .ops_pages import *  # noqa: F401,F403
from .ops_merge import *  # noqa: F401,F403
from .ops_convert import *  # noqa: F401,F403
from .ops_watermark import *  # noqa: F401,F403
from .ops_compress import *  # noqa: F401,F403
from .ops_unlock import *  # noqa: F401,F403
from .ops_encrypt import *  # noqa: F401,F403
from .ops_office import *  # noqa: F401,F403
from .menus import *  # noqa: F401,F403
from .app import *  # noqa: F401,F403

# Submodules referenced directly (e.g. tests monkeypatch pdf_forge.pdf_io.*).
from . import (compress, core, encrypt, menus, office, office_runtime,  # noqa: F401
               safeio,
               ops_compress, ops_office, pdf_io, render, taskqueue, ui,
               unlock, watermark)
