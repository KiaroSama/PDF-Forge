# -*- coding: utf-8 -*-
"""F-04: does a converted document actually reach the network?

The previous evidence for "external links are suppressed" was a source-text
grep for the word ``Update`` in :mod:`pdf_forge.office_server`. That proves a
string exists, not that a request is never made. These tests replace it with a
measurement: a loopback-only HTTP server counts the requests LibreOffice makes
while converting a document whose picture is an ``http://127.0.0.1:<port>/``
reference.

The positive control is the load-bearing part. A conversion that makes zero
requests is only meaningful once the *same* fixture has been shown to make one
under a deliberately unhardened profile - otherwise "0 requests" and "inert
fixture" look identical.

No Internet access is used or needed: the server binds 127.0.0.1 on port 0 and
is always shut down in a ``finally`` block.
"""

from __future__ import annotations

import base64
import gc
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from pdf_forge import msoffice, office_server  # noqa: E402

e2e_only = pytest.mark.skipif(
    os.environ.get("PDF_FORGE_E2E") != "1"
    or not app.office_runtime.runtime_status()["ready"],
    reason="requires PDF_FORGE_E2E=1 and a provisioned LibreOffice runtime",
)

# History, because it explains why these tests exist in this shape: on
# LibreOffice 25.8.7.3 the hardened profile suppressed macros and Writer/Calc
# link *updating*, yet a linked graphic (docx r:link with TargetMode=External,
# or ODF draw:image xlink:href) was still fetched over HTTP while the document
# loaded - 1 request on both hardened paths, the same count as an unhardened
# profile. BlockUntrustedRefererLinks closed it. These tests are the standing
# proof, and they only mean anything while the positive control below still
# fires: if the fixture ever stops being able to fetch, a zero count proves
# nothing at all.

# A 1x1 transparent PNG - a real image so LibreOffice accepts the response.
_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    b"DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# --------------------------------------------------------------------------- #
# Loopback request recorder
# --------------------------------------------------------------------------- #

class _Recorder(http.server.BaseHTTPRequestHandler):
    paths: list = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).paths.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(_PNG)))
        self.end_headers()
        self.wfile.write(_PNG)

    do_HEAD = do_GET

    def log_message(self, *_args) -> None:
        """Silence the default stderr access log."""


@contextmanager
def link_probe():
    """A 127.0.0.1 server on a random port that records every request it gets.

    Yields ``(url_for, hits)`` where ``hits`` is the live list of request paths.
    Always shut down, so a failing assertion cannot leave a listening socket or
    a serving thread behind.
    """
    hits: list = []
    handler = type("_ProbeHandler", (_Recorder,), {"paths": hits})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (lambda name: f"http://127.0.0.1:{port}/{name}"), hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def make_linked_image_docx(path: Path, url: str) -> Path:
    """A real .docx whose only picture is an *external* relationship.

    This is the ordinary web-bug shape: ``TargetMode="External"`` pointing at an
    http URL, referenced from the drawing with ``a:blip r:link``. LibreOffice
    resolves it while loading the document, before any export happens.
    """
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdImg" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        f'Target="{url}" TargetMode="External"/>'
        '</Relationships>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<w:body><w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        '<wp:extent cx="360000" cy="360000"/><wp:docPr id="1" name="linked"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="linked"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:link="rIdImg"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="360000" cy="360000"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p></w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/document.xml", document)
    return path


def make_plain_docx(path: Path, text: str = "Hello PDF Forge") -> Path:
    """The same package shape with no external reference at all."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return path


def assert_valid_pdf(path: Path) -> None:
    app.ops_office._validate_pdf_output(path)
    from pypdf import PdfReader

    assert len(PdfReader(str(path)).pages) >= 1


def convert_with_bare_profile(soffice: Path, src: Path, out_dir: Path) -> bool:
    """Convert through soffice with a fresh, deliberately UNhardened profile.

    This is the positive control's engine: identical to
    :func:`office_server.convert_via_soffice_cli` except that
    ``_harden_profile`` is not called. It stays inside the project-local runtime
    and its own throwaway profile, so it cannot touch the user's LibreOffice.
    """
    profile = Path(tempfile.mkdtemp(prefix="pdfforge_unhardened_"))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    cmd = [
        str(soffice), "--headless", "--invisible", "--nologo", "--nofirststartwizard",
        "--norestore", "--nodefault", "--nocrashreport",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to", "pdf", "--outdir", str(out_dir), str(src),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                       creationflags=creationflags)
        return bool(list(Path(out_dir).glob("*.pdf")))
    finally:
        import shutil

        shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
def server():
    """A task-owned, warmed conversion server, stopped on every path."""
    srv = app.office_runtime.start_conversion_server()
    try:
        # Inside the try: warm_up can raise, and a server started but never
        # stopped leaves its profile directory behind. The E2E job fails on
        # exactly that, which is how this was found.
        srv = app.office_runtime.warm_up(srv)
        yield srv
    finally:
        srv.stop()


# --------------------------------------------------------------------------- #
# 1. Positive control - the fixture is genuinely capable of fetching
# --------------------------------------------------------------------------- #

@e2e_only
def test_positive_control_the_fixture_really_fetches(tmp_path):
    """Without hardening, the document must reach the loopback server.

    If this fails, every "zero requests" result below is worthless: it would
    mean the fixture could never have made a request in the first place.
    """
    soffice = app.office_runtime.find_soffice()
    assert soffice is not None
    with link_probe() as (url_for, hits):
        src = make_linked_image_docx(tmp_path / "linked.docx", url_for("control.png"))
        out_dir = tmp_path / "control_out"
        out_dir.mkdir()
        produced = convert_with_bare_profile(soffice, src, out_dir)
        observed = list(hits)
    assert produced, "the unhardened control conversion produced no PDF"
    assert observed, (
        "POSITIVE CONTROL FAILED: even an unhardened LibreOffice profile made no "
        "request for the linked image, so this fixture cannot prove anything "
        "about the hardened paths."
    )
    assert any("control.png" in path for path in observed), observed


# --------------------------------------------------------------------------- #
# 2. The hardened paths must make no request at all
# --------------------------------------------------------------------------- #

@e2e_only
def test_hardened_unoserver_conversion_makes_no_request(tmp_path, server):
    """The normal conversion path: hardened profile, UNO bridge."""
    with link_probe() as (url_for, hits):
        src = make_linked_image_docx(tmp_path / "linked_uno.docx", url_for("uno.png"))
        out = tmp_path / "linked_uno.pdf"
        app.office_runtime.convert_to_pdf(server, src, out)
        observed = list(hits)
    assert_valid_pdf(out)
    assert observed == [], f"the hardened conversion fetched {observed}"


@e2e_only
def test_hardened_cli_fallback_makes_no_request(tmp_path):
    """The bridge-loss fallback, which builds and hardens its own profile.

    Calling :func:`office_server.convert_via_soffice_cli` directly is the forced
    fallback: it is the exact function ``convert_to_pdf`` retries through, with
    its own ``_harden_profile`` call, and it fires deterministically instead of
    depending on the bridge happening to die.
    """
    soffice = app.office_runtime.find_soffice()
    assert soffice is not None
    with link_probe() as (url_for, hits):
        src = make_linked_image_docx(tmp_path / "linked_cli.docx", url_for("cli.png"))
        out = tmp_path / "linked_cli.pdf"
        office_server.convert_via_soffice_cli(soffice, src, out)
        observed = list(hits)
    assert_valid_pdf(out)
    assert observed == [], f"the hardened CLI fallback fetched {observed}"


# --------------------------------------------------------------------------- #
# 3. The hardening must not have broken ordinary conversion
# --------------------------------------------------------------------------- #

@e2e_only
def test_an_ordinary_document_still_converts_on_both_paths(tmp_path, server):
    src = make_plain_docx(tmp_path / "plain.docx")
    with link_probe() as (_url_for, hits):
        uno_out = tmp_path / "plain_uno.pdf"
        app.office_runtime.convert_to_pdf(server, src, uno_out)
        cli_out = tmp_path / "plain_cli.pdf"
        office_server.convert_via_soffice_cli(
            app.office_runtime.find_soffice(), src, cli_out
        )
        observed = list(hits)
    assert_valid_pdf(uno_out)
    assert_valid_pdf(cli_out)
    assert observed == [], "a document with no external reference fetched something"


# --------------------------------------------------------------------------- #
# 4. Microsoft Office - only when one is really installed and automatable
# --------------------------------------------------------------------------- #

def _word_pids() -> set:
    listing = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
    ).stdout
    return {line.split('","')[1] for line in listing.splitlines() if line.startswith('"')}


def _macro_security(session) -> int:
    """Read back the enforced setting without keeping the COM object alive.

    A surviving reference to the application object keeps WINWORD.EXE running
    after ``Quit()``, which would make the leak check below fail for a reason
    that has nothing to do with the production code.
    """
    return int(session._app("word").AutomationSecurity)


def test_microsoft_office_word_conversion_is_hardened_and_leaves_no_process(tmp_path):
    detected = msoffice.detect_office()
    if not detected or "word" not in detected["apps"]:
        pytest.skip(
            "no automatable Microsoft Word on this machine "
            f"(detect_office() -> {detected!r})"
        )

    before = _word_pids()
    src = make_plain_docx(tmp_path / "msoffice.docx")
    out = tmp_path / "msoffice.pdf"
    session = msoffice.start_session()
    try:
        # Macro security is set when the application is created, and the
        # production code refuses to open anything if it did not take.
        assert _macro_security(session) == msoffice._MSO_FORCE_DISABLE, (
            "Word was started without forced macro disabling"
        )
        # _convert_word opens the source ReadOnly with link updates off.
        msoffice.convert_to_pdf(session, src, out, "word")
    finally:
        session.stop()

    assert_valid_pdf(out)
    assert src.exists(), "the source must survive a read-only conversion"

    # Quit() is asynchronous; give the process a bounded moment to disappear.
    gc.collect()
    deadline = time.monotonic() + 15
    leaked = _word_pids() - before
    while leaked and time.monotonic() < deadline:
        time.sleep(0.5)
        leaked = _word_pids() - before
    assert not leaked, f"the test left Word processes behind: {sorted(leaked)}"
