"""Project-local, CLI-only LibreOffice + unoserver runtime for PDF conversion.

Design (see README "Convert to PDF"):
  * ``unoserver`` is installed into the project ``.venv`` like any other Python
    dependency; its client talks to the server over a private localhost XMLRPC
    port, so the client itself never imports ``uno``.
  * LibreOffice is a native renderer that cannot live inside a venv, so it is
    provisioned project-locally under ``.tools/libreoffice/`` (git-ignored) via
    an official administrative extraction - never a system-wide desktop install,
    no PATH/registry/shortcut/service changes, no GUI.
  * The server runs under LibreOffice's *bundled* Python (which can ``import
    uno``) with the venv's ``site-packages`` on ``PYTHONPATH`` so it still loads
    the venv-installed ``unoserver``.
  * Every conversion run starts a dedicated headless ``soffice`` on a random
    localhost port with an isolated temporary user profile, and terminates only
    that process on success, failure, timeout, cancellation, or exit.

This module is the public face of three implementation modules, so callers keep
a single ``office_runtime`` namespace:

  * :mod:`pdf_forge.office_discovery`  - paths, pinned metadata, timeouts, and
    deciding which runtime is usable.
  * :mod:`pdf_forge.office_server`     - starting a task-owned headless server
    and converting documents through it.
  * :mod:`pdf_forge.office_provision`  - downloading, verifying, extracting,
    trimming and removing the runtime.

It holds no interactive UI; :mod:`pdf_forge.ops_office` drives it.

Patching note: a test that replaces a function here only affects callers that
reach it through this namespace. To also affect calls made *inside* the
implementation modules, patch the defining module (for example
``office_discovery.libreoffice_dir``).
"""
from __future__ import annotations

from .office_discovery import (  # noqa: F401
    CONVERT_SECONDS_PER_MB,
    CONVERT_TIMEOUT,
    CONVERT_TIMEOUT_MAX,
    EXTRACT_TIMEOUT,
    NO_PROGRESS_TIMEOUT,
    OfficeRuntimeError,
    RuntimeCandidate,
    SERVER_START_TIMEOUT,
    conversion_timeout_for,
    find_soffice,
    find_soffice_python,
    libreoffice_dir,
    libreoffice_version,
    load_runtime_meta,
    marker_version,
    probe_soffice_version,
    random_localhost_port,
    resolve_runtime_candidates,
    runtime_root,
    runtime_status,
    select_runtime,
    unoserver_installed,
    unoserver_version,
    venv_site_packages,
)
from .office_provision import (  # noqa: F401
    clean_runtime,
    provision_runtime,
    verify_runtime_directory,
    _trim_runtime,
)
from .office_server import (  # noqa: F401
    BRIDGE_LOST_SENTINEL,
    ConversionServer,
    PASSWORD_SENTINEL,
    convert_to_pdf,
    convert_via_soffice_cli,
    is_bridge_lost,
    save_with_password,
    start_conversion_server,
    warm_up,
    _classify_convert_error,
)

__all__ = [
    'OfficeRuntimeError', 'runtime_root', 'libreoffice_dir', 'load_runtime_meta',
    'find_soffice', 'find_soffice_python', 'venv_site_packages',
    'unoserver_installed', 'unoserver_version', 'libreoffice_version',
    'runtime_status', 'random_localhost_port', 'RuntimeCandidate',
    'verify_runtime_directory', '_trim_runtime',
    'resolve_runtime_candidates', 'select_runtime', 'probe_soffice_version',
    'marker_version',
    'ConversionServer', 'convert_to_pdf', 'provision_runtime', 'clean_runtime',
    'start_conversion_server', 'is_bridge_lost', 'convert_via_soffice_cli',
    'PASSWORD_SENTINEL', 'BRIDGE_LOST_SENTINEL', 'warm_up', 'save_with_password',
    'conversion_timeout_for',
]
