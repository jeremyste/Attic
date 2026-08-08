"""Protocol-framing tests for the root-side privileged helper.

Drives ``priv_helper.py``'s request handlers directly (never through a real
``pkexec``/root subprocess) by monkeypatching ``_send`` to capture outbound
messages and ``subprocess.Popen`` with a small controllable fake, so the
allow-list, id-correlation, and start/cancel/done lifecycle are all testable
without touching a real device or requiring privilege.
"""

from __future__ import annotations

import subprocess
import threading
import time

import attic.core.priv_helper as helper


class FakePopen:
    """A Popen stand-in whose exit is controlled by the test, not real time."""

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.returncode: int | None = None
        self._exit_event = threading.Event()

    def wait(self, timeout=None):
        if not self._exit_event.wait(timeout=timeout):
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout or 0)
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self._exit_event.set()

    def kill(self):
        self.returncode = -9
        self._exit_event.set()

    def finish(self, returncode=0):
        self.returncode = returncode
        self._exit_event.set()


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- allow-list ---------------------------------------------------------------


def test_validate_rejects_disallowed_command():
    assert helper._validate(["rm", "-rf", "/"]) != ""
    assert helper._validate(["python3", "-c", "evil"]) != ""


def test_validate_accepts_allowed_commands():
    assert helper._validate(["mount", "-o", "ro"]) == ""
    assert helper._validate(["umount", "/mnt/x"]) == ""
    assert helper._validate(["ddrescue", "/dev/sdb", "img", "map"]) == ""


def test_validate_rejects_malformed_argv():
    assert helper._validate([]) != ""
    assert helper._validate(None) != ""
    assert helper._validate(["mount", 1, 2]) != ""


def test_handle_run_rejects_disallowed_command(monkeypatch):
    sent = []
    monkeypatch.setattr(helper, "_send", sent.append)
    helper._handle_run(1, ["rm", "-rf", "/"])
    assert sent == [{
        "id": 1, "returncode": 126, "stdout": "",
        "stderr": "command not allowed: 'rm'",
    }]


def test_handle_run_executes_and_reports_result(monkeypatch):
    sent = []
    monkeypatch.setattr(helper, "_send", sent.append)

    class FakeCompleted:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    monkeypatch.setattr(helper.subprocess, "run", lambda argv, **k: FakeCompleted())
    helper._handle_run(2, ["mount", "--version"])
    assert sent == [{"id": 2, "returncode": 0, "stdout": "ok\n", "stderr": ""}]


# --- start / cancel / done lifecycle ------------------------------------------


def test_handle_start_acks_then_reports_done_on_exit(monkeypatch):
    sent = []
    monkeypatch.setattr(helper, "_send", sent.append)
    fake = FakePopen(["ddrescue", "x"])
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **k: fake)
    helper._children.clear()

    helper._handle_start(10, ["ddrescue", "/dev/x", "img", "map"], None)
    assert sent == [{"id": 10, "started": True}]
    assert helper._children[10] is fake

    fake.finish(returncode=0)
    assert _wait_for(lambda: len(sent) == 2)
    assert sent[1] == {"id": 10, "done": True, "returncode": 0}
    assert 10 not in helper._children


def test_handle_start_rejects_disallowed_command_without_spawning(monkeypatch):
    sent = []
    monkeypatch.setattr(helper, "_send", sent.append)
    spawned = []
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **k: spawned.append(1))
    helper._children.clear()

    helper._handle_start(11, ["rm", "-rf", "/"], None)
    assert sent[0]["id"] == 11
    assert sent[0]["started"] is False
    assert not spawned


def test_handle_cancel_terminates_running_child(monkeypatch):
    sent = []
    monkeypatch.setattr(helper, "_send", sent.append)
    fake = FakePopen(["ddrescue", "x"])
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **k: fake)
    helper._children.clear()

    helper._handle_start(12, ["ddrescue", "/dev/x", "img", "map"], None)
    helper._handle_cancel(12)

    assert {"id": 12, "cancelled": True} in sent
    assert _wait_for(lambda: any(m.get("id") == 12 and m.get("done") for m in sent))
    done_msg = next(m for m in sent if m.get("id") == 12 and m.get("done"))
    assert done_msg["returncode"] == -15  # terminate(), not kill() -- exited promptly


def test_handle_cancel_unknown_id_reports_failure(monkeypatch):
    sent = []
    monkeypatch.setattr(helper, "_send", sent.append)
    helper._children.clear()
    helper._handle_cancel(999)
    assert sent == [{"id": 999, "cancelled": False, "error": "no such running request"}]


def test_multiple_in_flight_requests_are_independent(monkeypatch):
    """Two concurrent 'start' requests (e.g. an HDD + optical ddrescue) must
    each get their own started/done pair, keyed by id -- not cross-talk."""
    sent = []
    monkeypatch.setattr(helper, "_send", sent.append)
    fakes = {}

    def fake_popen(argv, **kwargs):
        fake = FakePopen(argv)
        fakes[len(fakes)] = fake
        return fake

    monkeypatch.setattr(helper.subprocess, "Popen", fake_popen)
    helper._children.clear()

    helper._handle_start(20, ["ddrescue", "/dev/a", "imgA", "mapA"], None)
    helper._handle_start(21, ["ddrescue", "/dev/b", "imgB", "mapB"], None)
    assert {"id": 20, "started": True} in sent
    assert {"id": 21, "started": True} in sent
    assert set(helper._children) == {20, 21}

    helper._children[21].finish(returncode=0)
    assert _wait_for(lambda: {"id": 21, "done": True, "returncode": 0} in sent)
    assert 20 in helper._children  # the other request is untouched
    assert 21 not in helper._children
