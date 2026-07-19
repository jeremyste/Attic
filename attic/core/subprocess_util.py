"""Central subprocess wrapper used by every core module that shells out.

Everything in Attic runs external tools (ddrescue, mtools, parted, blkid, file,
zstd, sha256sum, mount, gw, ...) rather than reimplementing them. Routing all of
those through one ``run()`` gives us:

- uniform capture of stdout/stderr/returncode in a structured result,
- a single place tests can monkeypatch to avoid touching real devices,
- one implementation of the ``pkexec`` privilege-escalation prefix.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence


class CommandError(RuntimeError):
    """Raised when a command run with ``check=True`` exits non-zero (or is missing)."""

    def __init__(self, result: "CmdResult"):
        self.result = result
        super().__init__(result.error_summary())


@dataclass
class CmdResult:
    """Structured outcome of a single subprocess invocation."""

    argv: Sequence[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    # Set when the executable itself could not be found/launched.
    launch_error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.launch_error

    def error_summary(self) -> str:
        """Short human/catalog-friendly description of a failure."""
        cmd = " ".join(str(a) for a in self.argv)
        if self.launch_error:
            return f"failed to launch `{cmd}`: {self.launch_error}"
        if self.timed_out:
            return f"`{cmd}` timed out"
        detail = (self.stderr or self.stdout).strip().splitlines()
        tail = detail[-1] if detail else ""
        return f"`{cmd}` exited {self.returncode}: {tail}".rstrip(": ")


def has_tool(name: str) -> bool:
    """True if ``name`` resolves on PATH."""
    return shutil.which(name) is not None


def with_pkexec(argv: Sequence[str]) -> list[str]:
    """Prefix ``argv`` with ``pkexec`` for privileged operations.

    Used for raw-device ddrescue reads and mount/umount. The GUI itself stays
    unprivileged; pkexec prompts for auth as needed.
    """
    return ["pkexec", *[str(a) for a in argv]]


def run(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float | None = None,
    check: bool = False,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> CmdResult:
    """Run ``argv`` and return a :class:`CmdResult`.

    Never raises on a non-zero exit unless ``check=True``; a missing executable
    or timeout is captured in the result rather than surfaced as a raw OSError,
    so callers get a consistent failure object to record in the catalog.
    """
    argv = [str(a) for a in argv]
    try:
        proc = subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as exc:
        result = CmdResult(argv=argv, returncode=127, launch_error=str(exc))
    except PermissionError as exc:
        result = CmdResult(argv=argv, returncode=126, launch_error=str(exc))
    except subprocess.TimeoutExpired as exc:
        result = CmdResult(
            argv=argv,
            returncode=124,
            stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
            stderr=exc.stderr or "" if isinstance(exc.stderr, str) else "",
            timed_out=True,
        )
    else:
        result = CmdResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )

    if check and not result.ok:
        raise CommandError(result)
    return result
