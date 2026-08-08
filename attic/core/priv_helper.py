"""Root-side privileged helper.

Runs mount/umount/ddrescue on behalf of the unprivileged app, authorized once
via a single ``pkexec`` call at startup instead of once per operation. Launched
as ``pkexec <python> -m attic.core.priv_helper`` by ``priv_client.py``; never
invoked directly by a user.

Wire protocol: newline-delimited JSON on stdin/stdout. Every request carries a
client-chosen integer ``id`` so multiple in-flight requests (e.g. an HDD and an
optical ddrescue running at once) can share the one pipe.

  {"id": N, "op": "run", "argv": [...]}
      One-shot command (mount/umount). Reply, once:
      {"id": N, "returncode": ..., "stdout": ..., "stderr": ...}

  {"id": N, "op": "start", "argv": [...], "stderr_path": "..."}
      Long-running command (ddrescue). ``stderr_path``, if given, is opened
      here (root-side) and the child's stderr redirected to it -- ddrescue's
      own progress already goes to its mapfile on disk, which the unprivileged
      caller reads directly, so stdout is always discarded.
      Reply immediately: {"id": N, "started": true} (or false + "error").
      Reply again on exit: {"id": N, "done": true, "returncode": ...}.

  {"id": N, "op": "cancel"}
      Terminates the process started under that id (SIGTERM, then SIGKILL
      after a 10s grace period if it hasn't exited). Only root can signal a
      root-owned process, which is why cancellation has to go through here
      rather than the caller calling ``Popen.terminate()`` itself.
      Reply: {"id": N, "cancelled": true} (or false + "error").

Unknown ops are ignored rather than erroring, so a newer client talking to an
older helper degrades rather than crashing it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

# Defense in depth: pkexec already grants the whole process root, but the
# helper only ever executes these three tools regardless of what a request asks for.
ALLOWED_COMMANDS = {"mount", "umount", "ddrescue"}

_out_lock = threading.Lock()
_children: dict[int, subprocess.Popen] = {}
_children_lock = threading.Lock()


def _send(msg: dict) -> None:
    line = json.dumps(msg)
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _validate(argv) -> str:
    """Return an error string if ``argv`` isn't an allowed command, else ''."""
    if not argv or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return "argv must be a non-empty list of strings"
    if argv[0] not in ALLOWED_COMMANDS:
        return f"command not allowed: {argv[0]!r}"
    return ""


def _handle_run(req_id, argv) -> None:
    err = _validate(argv)
    if err:
        _send({"id": req_id, "returncode": 126, "stdout": "", "stderr": err})
        return
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:
        _send({"id": req_id, "returncode": 127, "stdout": "", "stderr": str(exc)})
        return
    _send({
        "id": req_id, "returncode": proc.returncode,
        "stdout": proc.stdout, "stderr": proc.stderr,
    })


def _handle_start(req_id, argv, stderr_path) -> None:
    err = _validate(argv)
    if err:
        _send({"id": req_id, "started": False, "error": err})
        return

    errfh = None
    try:
        if stderr_path:
            errfh = open(stderr_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL,
            stderr=errfh if errfh else subprocess.DEVNULL,
        )
    except OSError as exc:
        if errfh:
            errfh.close()
        _send({"id": req_id, "started": False, "error": str(exc)})
        return

    with _children_lock:
        _children[req_id] = proc
    _send({"id": req_id, "started": True})

    def _wait() -> None:
        proc.wait()
        if errfh:
            errfh.close()
        with _children_lock:
            _children.pop(req_id, None)
        _send({"id": req_id, "done": True, "returncode": proc.returncode})

    threading.Thread(target=_wait, daemon=True).start()


def _handle_cancel(req_id) -> None:
    with _children_lock:
        proc = _children.get(req_id)
    if proc is None:
        _send({"id": req_id, "cancelled": False, "error": "no such running request"})
        return
    proc.terminate()

    def _force_kill() -> None:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    threading.Thread(target=_force_kill, daemon=True).start()
    _send({"id": req_id, "cancelled": True})


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = req.get("id")
        op = req.get("op")
        if op == "run":
            threading.Thread(
                target=_handle_run, args=(req_id, req.get("argv")), daemon=True,
            ).start()
        elif op == "start":
            _handle_start(req_id, req.get("argv"), req.get("stderr_path"))
        elif op == "cancel":
            _handle_cancel(req_id)

    # Parent's stdin closed (app exited): don't leave rescues running as
    # orphaned root processes.
    with _children_lock:
        procs = list(_children.values())
    for proc in procs:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
