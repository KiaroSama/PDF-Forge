# -*- coding: utf-8 -*-
"""The evidence a stalled conversion server leaves behind (BUGS.md B-01).

B-01 is intermittent and has never been explained; upstream has no answer
either. It will therefore be solved by whatever the next occurrence records, so
these tests guard the recording itself - the discriminator that says which phase
wedged, the CPU delta that separates a stall from slowness, and the process
filter that has already been got wrong once in a way that silently matched
nothing.
"""

from pathlib import Path

import pytest

from pdf_forge.office_diagnostics import stall_report
from pdf_forge.office_processes import SofficeSample, profile_glob

# The two markers unoserver actually logs: the first right after its socket
# bind, the second once it is serving. Hard-coded here on purpose - if a future
# unoserver renames them, this file is where that shows up.
BOUND = "INFO:unoserver:Starting UnoConverter."
SERVING = "INFO:unoserver:Started."


@pytest.fixture
def no_live_query(monkeypatch):
    """Stub the process query for tests that assert on something else.

    It shells out to PowerShell, which cost these tests 4.2 s to be told what a
    fresh tmp_path profile guarantees anyway - that nothing owns it. The tests
    that DO assert on process state override this with their own samples.
    """
    monkeypatch.setattr("pdf_forge.office_diagnostics.soffice_processes",
                        lambda _p: [])


def test_the_phase_discriminator_separates_a_startup_wedge_from_a_conversion_one(
        tmp_path, no_live_query):
    """The single question B-01 says to ask of the log, in both directions."""
    startup = stall_report(tmp_path, f"Starting unoserver\n{BOUND}\n")
    assert "STARTUP" in startup
    assert "CONVERSION" not in startup

    conversion = stall_report(tmp_path, f"{BOUND}\n{SERVING}\nPOST /RPC2 200\n")
    assert "CONVERSION" in conversion
    assert "wedged during STARTUP" not in conversion

    never = stall_report(tmp_path, "Starting unoserver 3.7\n")
    assert "never bound its port" in never


def test_flat_cpu_across_the_wait_is_reported_as_a_stall(tmp_path, monkeypatch):
    """The finding itself: a wedged LibreOffice does not move, a slow one does.

    The baseline is taken when the wait begins and the sample when it times out,
    so the window is the whole start timeout - which is why 0.2 s of CPU across
    it is flat and not merely quiet.
    """
    baseline = [SofficeSample(pid=4242, cpu_seconds=6.62, rss_bytes=259 * 1024 * 1024)]
    stalled = [SofficeSample(pid=4242, cpu_seconds=6.70, rss_bytes=259 * 1024 * 1024)]
    monkeypatch.setattr("pdf_forge.office_diagnostics.soffice_processes",
                        lambda _p: stalled)

    report = stall_report(tmp_path, BOUND, baseline)
    assert "FLAT - stalled" in report
    assert "pid 4242" in report

    busy = [SofficeSample(pid=4242, cpu_seconds=41.0, rss_bytes=259 * 1024 * 1024)]
    monkeypatch.setattr("pdf_forge.office_diagnostics.soffice_processes",
                        lambda _p: busy)
    assert "advancing" in stall_report(tmp_path, BOUND, baseline)


def test_a_vanished_process_is_reported_rather_than_passed_over(tmp_path, monkeypatch):
    """Silence is not an all-clear: no process at all is itself the finding."""
    monkeypatch.setattr("pdf_forge.office_diagnostics.soffice_processes",
                        lambda _p: [])
    assert "no LibreOffice is using our profile" in stall_report(tmp_path, BOUND)


def test_the_profile_filter_is_the_uri_form_not_the_native_path():
    """Measured once: the native form matched 0 live processes, this matched 2.

    unoserver converts ``--user-installation`` with ``Path(...).as_uri()`` before
    launching soffice, so the URI is what sits on the command line, and
    PowerShell's ``-like`` treats the backslashes of a native path as escapes.
    A "simplification" back to ``str(path)`` would filter nothing out and reap
    nothing - failing silently, which is this codebase's signature defect class.
    """
    glob = profile_glob(Path(__file__).resolve().parent)
    assert glob.startswith("*file:///") and glob.endswith("*")
    assert "\\" not in glob


def test_the_report_carries_the_log_tail_and_points_at_the_bug(tmp_path, no_live_query):
    """Whoever reads the failure gets the evidence, not a second errand."""
    log = "\n".join(f"line {n}" for n in range(20)) + f"\n{BOUND}\n"
    report = stall_report(tmp_path, log)
    assert BOUND in report
    assert "line 19" in report      # the tail is present
    assert "line 0" not in report   # and it is bounded
    assert "B-01" in report


def test_a_server_without_a_profile_still_raises_the_real_error(monkeypatch):
    """A diagnostic must never become the failure. CI caught this one.

    The first version sampled ``server.profile_dir`` directly at the top of
    ``_wait_until_ready``, which runs on the HAPPY path. Every existing test
    double lacks that attribute, so six suites turned an
    "it did not become ready" error into an ``AttributeError`` - the diagnostic
    reporting itself instead of the thing it exists to observe.
    """
    from pdf_forge.office_diagnostics import baseline_for, report_for

    class ServerDouble:
        """No profile_dir, and a log that raises - the hostile shape."""

        def read_log(self, *_args):
            raise OSError("no log here")

    double = ServerDouble()
    assert baseline_for(double) == []
    report = report_for(double, None)
    assert "no profile to inspect" in report


def test_an_unreadable_profile_degrades_instead_of_raising(tmp_path, monkeypatch):
    """Same contract on the failure path: report what is missing, raise nothing."""
    from pdf_forge.office_diagnostics import report_for

    def explode(*_args, **_kwargs):
        raise RuntimeError("WMI is unavailable")

    monkeypatch.setattr("pdf_forge.office_diagnostics.soffice_processes", explode)

    class ServerDouble:
        profile_dir = tmp_path

        def read_log(self, *_args):
            return BOUND

    assert "unavailable" in report_for(ServerDouble(), None)
