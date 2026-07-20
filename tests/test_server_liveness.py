# -*- coding: utf-8 -*-
"""Liveness must be reported truthfully, and a dead server must cost nothing.

Measured chain before this fix:

  start_conversion_server()  ->  process up, port open        (~8s)
  warm_up()                  ->  process EXITED, port dead    (~2s)
  every later conversion     ->  ~50s waiting on the closed port
                                 then ~8s in the CLI fallback

warm_up converts a throwaway .txt. When LibreOffice dies on that export,
convert_to_pdf catches the lost bridge and succeeds through the CLI fallback -
so no exception reaches warm_up, which then returned a server whose process was
gone. Every later conversion paid a ~50s client timeout to rediscover the same
corpse.

The export crash is intermittent, not universal: with the liveness check in
place a surviving server converts in ~0.7s. So neither "always alive" nor
"always dead" is a contract these tests can assert - what they require is that
the reported state matches reality and that a dead one is noticed at once.
"""
from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_forge as app  # noqa: E402
from pdf_forge import office_server  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("PDF_FORGE_E2E") != "1"
    or not app.office_runtime.runtime_status(verify_version=False)["soffice"],
    reason="needs the provisioned LibreOffice runtime (PDF_FORGE_E2E=1)",
)


def _port_open(port: int, timeout: float = 1.0) -> bool:
    probe = socket.socket()
    probe.settimeout(timeout)
    try:
        probe.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


@pytest.fixture
def raw_server():
    srv = app.office_runtime.start_conversion_server()
    try:
        yield srv
    finally:
        srv.stop()


def test_warm_up_leaves_an_honest_runtime_state(raw_server):
    """After warming, liveness must be reported truthfully.

    The export crash is intermittent, so a warmed server cannot be guaranteed
    alive - nor guaranteed dead. What must hold is that the reported state
    matches reality, because that is what lets a conversion skip a dead server
    instead of waiting ~50s to rediscover it. A stale "alive" was the defect.
    """
    warmed = app.office_runtime.warm_up(raw_server)
    try:
        assert warmed.is_alive() == (warmed.process.poll() is None)
        if warmed.is_alive():
            assert _port_open(warmed.port), (
                "reported alive but the port is closed"
            )
    finally:
        if warmed is not raw_server:
            warmed.stop()


def test_a_conversion_never_waits_out_a_dead_server(tmp_path, raw_server,
                                                    monkeypatch):
    """Whichever path is taken, no time may be spent on a corpse.

    unoserver 3.7 on this pinned LibreOffice sometimes loses the bridge on
    export, so either path may be taken and this test does not prescribe one.
    What it does require is that a loss is noticed as soon as the process
    exits: the client used to keep retrying the closed port for ~50s first, on
    every single file.
    """
    from test_office_e2e import make_docx

    used = {"cli": 0}
    real = office_server.convert_via_soffice_cli

    def counting(*args, **kwargs):
        used["cli"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(office_server, "convert_via_soffice_cli", counting)

    warmed = app.office_runtime.warm_up(raw_server)
    try:
        src = make_docx(tmp_path / "d.docx")
        started = time.perf_counter()
        app.office_runtime.convert_to_pdf(warmed, src, tmp_path / "d.pdf")
        elapsed = time.perf_counter() - started
    finally:
        if warmed is not raw_server:
            warmed.stop()

    assert (tmp_path / "d.pdf").exists(), "the conversion must still succeed"
    assert elapsed < 25, (
        f"the conversion took {elapsed:.1f}s (CLI used: {used['cli']}); a dead "
        "server must be noticed immediately, not waited out"
    )


def test_a_dead_server_fails_fast_instead_of_waiting_out_the_timeout(
        tmp_path, raw_server):
    """Rediscovering a corpse must cost milliseconds, not the full timeout."""
    from test_office_e2e import make_docx

    src = make_docx(tmp_path / "d.docx")
    raw_server.process.kill()
    raw_server.process.wait(timeout=30)
    assert not raw_server.is_alive(), "the server process must be gone"

    started = time.perf_counter()
    try:
        app.office_runtime.convert_to_pdf(raw_server, src, tmp_path / "out.pdf",
                                          password="x")
    except app.office_runtime.OfficeRuntimeError:
        pass
    elapsed = time.perf_counter() - started

    # A password is supplied so the CLI fallback is deliberately not taken -
    # this measures only how long the dead-server discovery costs.
    assert elapsed < 15, (
        f"discovering a dead server took {elapsed:.1f}s; it should be immediate"
    )
