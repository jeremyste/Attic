"""Shared pytest fixtures.

The core subprocess modules all funnel external commands through
``attic.core.subprocess_util.run``. ``fake_run`` monkeypatches that single
function so tests exercise the command-assembly and result-parsing logic without
touching real devices/tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pytest

from attic.core import subprocess_util
from attic.core.subprocess_util import CmdResult


@dataclass
class _Rule:
    matcher: Callable[[list[str]], bool]
    result: CmdResult


@dataclass
class FakeRun:
    """A programmable stand-in for ``subprocess_util.run``.

    Register responses with :meth:`when` (substring or predicate) and inspect
    what was invoked via :attr:`calls`. Unmatched commands return a benign
    success by default, or raise if :attr:`strict` is set.
    """

    rules: list[_Rule] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    strict: bool = False
    default_result: CmdResult | None = None

    def when(self, needle, *, returncode=0, stdout="", stderr="", timed_out=False,
             launch_error="", program: str | None = None):
        """Register a canned result for commands matching ``needle``.

        ``needle`` may be a substring (matched against the joined argv), a regex
        pattern object, or a predicate ``list[str] -> bool``. ``program`` is a
        convenience: match when argv[0] endswith the given program name.
        """
        if program is not None:
            def matcher(argv, _p=program):
                return bool(argv) and argv[0].split("/")[-1] == _p
        elif callable(needle):
            matcher = needle
        elif hasattr(needle, "search"):
            matcher = lambda argv, _n=needle: bool(_n.search(" ".join(argv)))
        else:
            matcher = lambda argv, _n=needle: _n in " ".join(argv)

        def make(argv):
            return CmdResult(
                argv=list(argv), returncode=returncode, stdout=stdout,
                stderr=stderr, timed_out=timed_out, launch_error=launch_error,
            )

        self.rules.append(_Rule(matcher, make))  # type: ignore[arg-type]
        return self

    def __call__(self, argv, *, input_text=None, timeout=None, check=False,
                 cwd=None, env=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for rule in self.rules:
            if rule.matcher(argv):
                result = rule.result(argv) if callable(rule.result) else rule.result
                if check and not result.ok:
                    from attic.core.subprocess_util import CommandError
                    raise CommandError(result)
                return result
        if self.strict:
            raise AssertionError(f"unexpected command: {' '.join(argv)}")
        result = self.default_result or CmdResult(argv=argv, returncode=0)
        return result

    # --- assertions helpers -------------------------------------------------

    def ran(self, needle) -> bool:
        return any(needle in " ".join(c) for c in self.calls)

    def find(self, needle) -> list[str] | None:
        for c in self.calls:
            if needle in " ".join(c):
                return c
        return None


@pytest.fixture
def fake_run(monkeypatch):
    """Patch ``subprocess_util.run`` everywhere it's referenced as ``su.run``."""
    fake = FakeRun()
    monkeypatch.setattr(subprocess_util, "run", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_real_priv_helper(monkeypatch):
    """Never let a test spawn a real ``pkexec``/privileged-helper subprocess.

    ``run_privileged()`` (used by ``extract.py``'s mount/umount calls) falls
    back to a one-off ``pkexec <argv>`` call -- captured by ``fake_run`` like
    any other ``subprocess_util.run`` call -- whenever the persistent helper
    can't be reached. Forcing "can't be reached" here means tests exercise
    that documented fallback path deterministically instead of racing a real
    pkexec/polkit prompt. ``run_ddrescue`` has no such fallback (ddrescue
    always needs privilege); tests that reach it patch ``priv_client.start``
    directly instead of relying on this fixture.
    """
    from attic.core import priv_client

    def _unavailable():
        raise priv_client.HelperUnavailable("no privileged helper in tests")

    monkeypatch.setattr(priv_client._client, "ensure_running", _unavailable)


@pytest.fixture(scope="session")
def qapp():
    """A single offscreen QApplication for widget-touching tests."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
