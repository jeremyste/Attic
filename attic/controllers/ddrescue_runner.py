"""Run ddrescue as a live subprocess while polling its mapfile for progress.

ddrescue needs root (raw device access) and runs for minutes/hours with live
progress wanted, so it's driven through the persistent privileged helper
(``core.priv_client``) rather than ``subprocess_util.run``'s blocking one-shot
call. ``priv_client.start`` returns a Popen-alike (``RemoteProcessHandle``)
that this module drives exactly like a local ``Popen`` -- ddrescue writes its
own progress to the mapfile on disk, which is polled directly here (no need to
relay anything through the helper). ``argv`` should be the bare ddrescue
command, with no ``pkexec``/privilege prefix -- the helper is already root.
The mapfile doubles as the run's logfile and is always kept uncompressed.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from ..core import priv_client
from ..core.ddrescue import MapSummary, parse_mapfile


@dataclass
class DdrescueOutcome:
    returncode: int
    stderr_tail: str
    last_summary: MapSummary | None


def run_ddrescue(
    argv: list[str],
    mapfile_path: str,
    *,
    stderr_path: str,
    on_progress: Callable[[MapSummary], None] | None = None,
    poll_interval: float = 1.0,
    should_cancel: Callable[[], bool] | None = None,
) -> DdrescueOutcome:
    """Launch ``argv``, polling ``mapfile_path`` until the process exits.

    Progress summaries are delivered to ``on_progress``. ddrescue's own textual
    meter (stdout) is discarded; stderr is redirected to ``stderr_path`` so a
    real failure can be summarized. Returns a :class:`DdrescueOutcome`.
    """
    last: MapSummary | None = None
    proc = priv_client.start([str(a) for a in argv], stderr_path=stderr_path)
    try:
        while proc.poll() is None:
            time.sleep(poll_interval)
            if should_cancel and should_cancel():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            last = _poll(mapfile_path, on_progress) or last
    finally:
        proc.wait()

    # One final read after completion to capture the finished state.
    last = _poll(mapfile_path, on_progress) or last
    return DdrescueOutcome(
        returncode=proc.returncode,
        stderr_tail=_tail(stderr_path),
        last_summary=last,
    )


def _poll(mapfile_path: str, on_progress) -> MapSummary | None:
    try:
        with open(mapfile_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    summary = parse_mapfile(text)
    if on_progress:
        on_progress(summary)
    return summary


def _tail(path: str, lines: int = 5) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).strip()
    except OSError:
        return ""
