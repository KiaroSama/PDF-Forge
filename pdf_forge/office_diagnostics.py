# -*- coding: utf-8 -*-
"""Evidence capture for a conversion server that stalls instead of starting.

`BUGS.md` B-01 records a LibreOffice stall that has never been explained: two
`soffice.bin` with flat CPU, one server stuck before `Started.` and one that
served a single RPC and went silent. Upstream has no answer either - unoserver
issue #16 ("Parallel Unoserver with different ports hangs/gets stuck") is open
with no maintainer response, and every workaround it documents (a distinct
``--port``, ``--uno-port`` and ``--user-installation`` per instance) this project
already does.

An intermittent fault that cannot be summoned on demand is proven by the next
occurrence or not at all, so the point of this module is that the next
occurrence arrives with its evidence already attached. It turns B-01's manual
"if it recurs" procedure into something that runs at the moment of failure,
while the processes are still alive and the profile directory still exists -
``stop()`` deletes both moments later, which is why the original stall had to be
diagnosed by hand before anything was allowed to clean up.

Every function here is best-effort and swallows its own errors: a diagnostic
that raises would replace the real failure with its own, and the caller is
already on a failure path.
"""

import time
from pathlib import Path
from typing import Optional

from .office_processes import SofficeSample, soffice_processes

__all__ = ['stall_report']

def _profile_silence(profile_dir: Path) -> Optional[float]:
    """Seconds since anything was last written anywhere under the profile.

    A stalled LibreOffice stops touching its profile entirely, so this is the
    cheapest single number that separates a stall from slow progress. ``None``
    when the directory cannot be walked.
    """
    newest: Optional[float] = None
    try:
        for path in profile_dir.rglob('*'):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    except OSError:
        return None
    if newest is None:
        return None
    return max(0.0, time.time() - newest)


def _classify(log_text: str) -> str:
    """Which phase the server died in, read from its own log.

    The discriminator B-01 established by hand: unoserver logs
    ``Starting UnoConverter.`` right after its socket bind and ``Started.`` once
    it is serving, so stopping between the two is a startup wedge and stopping
    after it is a conversion wedge.
    """
    if 'Started.' in log_text:
        return ('reached "Started." and then went silent - wedged during a '
                'CONVERSION, not startup')
    if 'Starting UnoConverter.' in log_text:
        return ('bound its port but never reached "Started." - wedged during '
                'STARTUP')
    return 'never bound its port - it did not get as far as UnoConverter'


def stall_report(
    profile_dir: Path,
    log_text: str,
    baseline: Optional[list[SofficeSample]] = None,
) -> str:
    """Describe a stalled conversion server from evidence taken right now.

    *baseline* is the same sample taken when the wait began. The whole start
    timeout has elapsed between the two, which makes the CPU delta a long,
    free measurement - better than the 20 s hand-sampling B-01 used, and
    costing no extra wall time on a path that has already failed.

    Flat CPU across that window is the finding: a LibreOffice that is merely
    slow burns CPU continuously, and one that is wedged does not move at all.
    """
    lines = [f'  phase: the server {_classify(log_text)}.']

    silence = _profile_silence(profile_dir)
    if silence is not None:
        lines.append(f'  profile: last written to {silence:.0f}s ago.')

    current = soffice_processes(profile_dir)
    if not current:
        lines.append('  processes: no LibreOffice is using our profile '
                     '(it exited without being reaped, or never started).')
    else:
        before = {s.pid: s.cpu_seconds for s in (baseline or [])}
        for sample in current:
            rss_mb = sample.rss_bytes / (1024 * 1024)
            if sample.pid in before:
                delta = sample.cpu_seconds - before[sample.pid]
                verdict = 'FLAT - stalled' if delta < 0.5 else 'advancing'
                lines.append(
                    f'  pid {sample.pid}: CPU +{delta:.2f}s over the wait '
                    f'({verdict}), RSS {rss_mb:.0f} MB.')
            else:
                lines.append(
                    f'  pid {sample.pid}: CPU {sample.cpu_seconds:.2f}s total '
                    f'(started mid-wait), RSS {rss_mb:.0f} MB.')

    tail = '\n'.join(log_text.strip().splitlines()[-8:])
    if tail:
        lines.append('  last log lines:')
        lines.extend(f'    {line}' for line in tail.splitlines())

    lines.append('  See BUGS.md B-01. This is the evidence that bug asks for; '
                 'attach it there rather than re-deriving it by hand.')
    return '\n'.join(lines)
