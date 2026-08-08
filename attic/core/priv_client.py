"""Unprivileged client for the persistent privileged helper (``priv_helper.py``).

One ``pkexec`` prompt authorizes a single long-lived root helper process; every
mount/umount/ddrescue call for the rest of the session is sent to it over a pipe
instead of triggering its own pkexec prompt. Call :func:`ensure_running` once at
app startup ("grab sudo early"); every other function here calls it too, so a
script that never did (e.g. a CLI entry point) still gets the one-prompt
behavior on first use.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Sequence

from . import subprocess_util as su
from .subprocess_util import CmdResult, with_pkexec

# Invoked by absolute file path, not `-m attic.core.priv_helper`: pkexec does
# not reliably preserve the caller's cwd (observed in practice -- `python -m`
# resolves modules relative to cwd, and under pkexec the `attic` package
# turned up unimportable), and priv_helper.py has zero package imports of its
# own (stdlib only), so a plain script path sidesteps the whole question.
_HELPER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "priv_helper.py")

# How long to wait for the helper to prove it's alive/authorized before giving
# up, and for a "start" request's synchronous ack. Generous because pkexec's
# GUI auth prompt is a human-speed wait.
_STARTUP_GRACE = 120.0
_ACK_TIMEOUT = 30.0


class HelperUnavailable(RuntimeError):
    """The privileged helper could not be started, authorized, or reached."""


@dataclass
class _PendingRun:
    argv: list
    kind: str  # "run" or "start"
    event: threading.Event = field(default_factory=threading.Event)
    result: CmdResult | None = None


@dataclass
class RemoteProcessHandle:
    """Popen-alike for a long-running command started inside the helper.

    Exposes exactly what ``ddrescue_runner.run_ddrescue`` needs (``poll``,
    ``terminate``, ``wait``, ``.returncode``) so that module needs no other
    changes to run its child through the helper instead of a local Popen.
    """

    _client: "PrivClient"
    _req_id: int
    returncode: int | None = None
    _done_event: threading.Event = field(default_factory=threading.Event)

    def poll(self) -> int | None:
        return self.returncode if self._done_event.is_set() else None

    def terminate(self) -> None:
        self._client._cancel(self._req_id)

    def kill(self) -> None:
        self._client._cancel(self._req_id)

    def wait(self, timeout: float | None = None) -> int:
        if not self._done_event.wait(timeout=timeout):
            raise subprocess.TimeoutExpired(cmd="<privileged>", timeout=timeout or 0)
        assert self.returncode is not None
        return self.returncode

    def _mark_done(self, returncode: int) -> None:
        self.returncode = returncode
        self._done_event.set()


class PrivClient:
    """Owns the helper subprocess and multiplexes requests/replies over it."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, _PendingRun] = {}
        self._handles: dict[int, RemoteProcessHandle] = {}

    # --- lifecycle -----------------------------------------------------------

    def ensure_running(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._start_locked()
        # pkexec's own process spawns instantly; it's the GUI auth dialog
        # behind it that takes human-scale time, and the helper only starts
        # reading requests once the user responds. Block on real proof of
        # life (a harmless allow-listed command) rather than just "a process
        # exists" -- that's what makes this the one point a dismissal/failure
        # surfaces, instead of a hang deep inside some later operation.
        ping = self._run_locked(["mount", "--version"], timeout=_STARTUP_GRACE)
        if not ping.ok:
            with self._lock:
                self._proc = None
            raise HelperUnavailable(
                ping.launch_error or ping.stderr or "privileged helper failed to start"
            )

    def _start_locked(self) -> None:
        argv = ["pkexec", sys.executable, _HELPER_SCRIPT]
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
        except OSError as exc:
            raise HelperUnavailable(f"failed to launch privileged helper: {exc}") from exc

        self._proc = proc
        self._pending.clear()
        self._handles.clear()
        threading.Thread(target=self._read_loop, args=(proc,), daemon=True).start()

    def _read_loop(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._dispatch(msg)

        # EOF: helper process ended (auth dismissed, crashed, app quitting).
        # Wake up everything still waiting rather than hanging forever.
        with self._lock:
            pending = list(self._pending.values())
            handles = list(self._handles.values())
            self._pending.clear()
            self._handles.clear()
            if self._proc is proc:
                self._proc = None
        stderr = ""
        try:
            if proc.stderr:
                stderr = proc.stderr.read()
        except (OSError, ValueError):
            pass
        for p in pending:
            p.result = CmdResult(
                argv=p.argv, returncode=126,
                launch_error=f"privileged helper exited: {stderr.strip()}",
            )
            p.event.set()
        for h in handles:
            h._mark_done(-1)

    def _dispatch(self, msg: dict) -> None:
        req_id = msg.get("id")
        with self._lock:
            pending = self._pending.get(req_id)
            handle = self._handles.get(req_id)

        if pending is not None and pending.kind == "run":
            pending.result = CmdResult(
                argv=pending.argv, returncode=msg.get("returncode", 126),
                stdout=msg.get("stdout", ""), stderr=msg.get("stderr", ""),
            )
            with self._lock:
                self._pending.pop(req_id, None)
            pending.event.set()
            return

        if pending is not None and pending.kind == "start":
            ok = bool(msg.get("started"))
            pending.result = CmdResult(
                argv=pending.argv, returncode=0 if ok else 126,
                stderr="" if ok else str(msg.get("error", "")),
            )
            with self._lock:
                self._pending.pop(req_id, None)
            pending.event.set()
            if not ok and handle is not None:
                with self._lock:
                    self._handles.pop(req_id, None)
                handle._mark_done(126)
            return

        if msg.get("done") and handle is not None:
            with self._lock:
                self._handles.pop(req_id, None)
            handle._mark_done(msg.get("returncode", -1))
            return

    def _next_req_id(self) -> int:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            return req_id

    # --- public API ------------------------------------------------------------

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CmdResult:
        self.ensure_running()
        return self._run_locked(argv, timeout=timeout)

    def _run_locked(self, argv: Sequence[str], *, timeout: float | None = None) -> CmdResult:
        argv = [str(a) for a in argv]
        req_id = self._next_req_id()
        pending = _PendingRun(argv=argv, kind="run")
        proc = self._register(req_id, pending=pending)
        self._write(proc, {"id": req_id, "op": "run", "argv": argv})
        if not pending.event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            return CmdResult(argv=argv, returncode=124, timed_out=True)
        assert pending.result is not None
        return pending.result

    def start(
        self, argv: Sequence[str], *, stderr_path: str | None = None,
    ) -> RemoteProcessHandle:
        self.ensure_running()
        argv = [str(a) for a in argv]
        req_id = self._next_req_id()
        handle = RemoteProcessHandle(_client=self, _req_id=req_id)
        pending = _PendingRun(argv=argv, kind="start")
        proc = self._register(req_id, pending=pending, handle=handle)

        req = {"id": req_id, "op": "start", "argv": argv}
        if stderr_path:
            req["stderr_path"] = stderr_path
        self._write(proc, req)

        if not pending.event.wait(timeout=_ACK_TIMEOUT):
            with self._lock:
                self._pending.pop(req_id, None)
                self._handles.pop(req_id, None)
            raise HelperUnavailable("privileged helper did not acknowledge start in time")
        assert pending.result is not None
        if not pending.result.ok:
            raise HelperUnavailable(
                pending.result.stderr or "failed to start privileged process"
            )
        return handle

    def _register(
        self, req_id: int, *, pending: _PendingRun, handle: RemoteProcessHandle | None = None,
    ) -> subprocess.Popen:
        with self._lock:
            proc = self._proc
            # Guard against the helper dying in the gap between ensure_running()
            # returning and this call (e.g. a fast pkexec auth rejection) --
            # without this check the reader thread may have already flushed and
            # cleared _pending before this entry was added, leaving it to wait
            # forever instead of failing.
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise HelperUnavailable("privileged helper is not running")
            self._pending[req_id] = pending
            if handle is not None:
                self._handles[req_id] = handle
        return proc

    def _cancel(self, req_id: int) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            self._write(proc, {"id": req_id, "op": "cancel"})
        except HelperUnavailable:
            pass

    def _write(self, proc: subprocess.Popen, msg: dict) -> None:
        line = json.dumps(msg) + "\n"
        try:
            assert proc.stdin is not None
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise HelperUnavailable(f"privileged helper pipe closed: {exc}") from exc


_client = PrivClient()


def ensure_running() -> None:
    """Start (or confirm already-running) the privileged helper.

    Call this once, early (app startup or a script's first privileged need) --
    it's the one point where a pkexec GUI prompt can appear. Raises
    :class:`HelperUnavailable` if authorization fails or is dismissed.
    """
    _client.ensure_running()


def run(argv: Sequence[str], *, timeout: float | None = None) -> CmdResult:
    """Run a one-shot privileged command (mount/umount) via the helper."""
    return _client.run(argv, timeout=timeout)


def start(argv: Sequence[str], *, stderr_path: str | None = None) -> RemoteProcessHandle:
    """Start a long-running privileged command (ddrescue) via the helper."""
    return _client.start(argv, stderr_path=stderr_path)


def run_with_fallback(argv: Sequence[str], *, timeout: float | None = None) -> CmdResult:
    """Prefer the persistent helper; fall back to a one-off ``pkexec`` call.

    The fallback covers running core functions directly (tests aside) without
    the app/CLI ever having called :func:`ensure_running` -- rare, but it
    should degrade to today's per-call-prompt behavior rather than fail.
    """
    try:
        return run(argv, timeout=timeout)
    except HelperUnavailable:
        return su.run(with_pkexec(argv), timeout=timeout)
