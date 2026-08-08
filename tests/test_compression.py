import pytest

import attic.core.compression as compression_mod
from attic.core.checksums import sha256_file
from attic.core.compression import (
    CompressionCancelled,
    build_zstd_argv,
    compress_and_checksum,
    compressed_path_for,
    raw_only_result,
)
from attic.core.subprocess_util import CommandError


def test_build_zstd_argv_has_required_flags():
    argv = build_zstd_argv("in.img", "out.img.zst")
    assert argv[0] == "zstd"
    assert "-19" in argv
    assert "--long" in argv
    assert "-T0" in argv
    # Output path is explicit and raw path is the final positional after `--`.
    assert argv[argv.index("-o") + 1] == "out.img.zst"
    assert argv[-1] == "in.img"


def test_compressed_path_for():
    assert compressed_path_for("/x/foo.img") == "/x/foo.img.zst"
    assert compressed_path_for("/x/foo.raw") == "/x/foo.raw.zst"


def test_sha256_file_parses_first_token(fake_run):
    fake_run.when("sha256sum", stdout="abc123  somefile.img\n")
    assert sha256_file("somefile.img") == "abc123"


def test_sha256_file_raises_on_failure(fake_run):
    fake_run.when("sha256sum", returncode=1, stderr="No such file")
    with pytest.raises(CommandError):
        sha256_file("missing.img")


def test_compress_and_checksum_flow(tmp_path, fake_run):
    raw = tmp_path / "disk.img"
    raw.write_bytes(b"x" * 1000)
    out = tmp_path / "disk.img.zst"

    # zstd "runs" — emulate it by creating the output file the code will stat.
    def zstd_side_effect(argv):
        out.write_bytes(b"y" * 200)
        from attic.core.subprocess_util import CmdResult
        return CmdResult(argv=argv, returncode=0)

    # Register a zstd rule, then swap its result-maker for our side-effecting one
    # so the "compressed" output file actually exists for the size/hash step.
    fake_run.when(lambda a: a and a[0] == "zstd")
    fake_run.rules[-1].result = zstd_side_effect
    fake_run.when("sha256sum", program="sha256sum", stdout="deadbeef  x\n")

    result = compress_and_checksum(str(raw), str(out))

    assert result.raw_size_bytes == 1000
    assert result.compressed_size_bytes == 200
    assert result.sha256_raw == "deadbeef"
    assert result.sha256_compressed == "deadbeef"
    assert fake_run.ran("zstd")
    assert fake_run.ran("sha256sum")


def test_compress_raises_when_zstd_fails(tmp_path, fake_run):
    raw = tmp_path / "disk.img"
    raw.write_bytes(b"x")
    fake_run.when(lambda a: a and a[0] == "zstd", returncode=1, stderr="zstd boom")
    with pytest.raises(CommandError):
        compress_and_checksum(str(raw), str(tmp_path / "disk.img.zst"))


# --- cancellable compression (should_cancel) ----------------------------------


class _FakePopen:
    """Stands in for subprocess.Popen. Reports "still running" for
    ``polls_until_done`` calls to poll(), then exits 0 and (optionally)
    writes ``out_path`` -- unless terminate() is called first, after which
    it reports as killed (-15)."""

    def __init__(self, polls_until_done=1, out_path=None):
        self._polls_left = polls_until_done
        self._terminated = False
        self.returncode = None
        self.stderr = None
        self._out_path = out_path

    def __call__(self, argv, **kw):  # used directly as the Popen replacement
        return self

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self._terminated:
            self.returncode = -15
            return self.returncode
        if self._polls_left <= 0:
            self.returncode = 0
            if self._out_path:
                with open(self._out_path, "wb") as fh:
                    fh.write(b"z" * 50)
            return self.returncode
        self._polls_left -= 1
        return None

    def terminate(self):
        self._terminated = True

    def wait(self, timeout=None):
        if self.returncode is None:
            self.poll()
        return self.returncode


def test_compress_and_checksum_succeeds_when_never_cancelled(tmp_path, monkeypatch, fake_run):
    raw = tmp_path / "disk.img"
    raw.write_bytes(b"x" * 10)
    out = tmp_path / "disk.img.zst"
    fake = _FakePopen(polls_until_done=2, out_path=str(out))
    monkeypatch.setattr(compression_mod.subprocess, "Popen", fake)
    fake_run.when("sha256sum", stdout="deadbeef  x\n")

    result = compress_and_checksum(
        str(raw), str(out), should_cancel=lambda: False, poll_interval=0,
    )

    assert result.compressed_size_bytes == 50
    assert out.exists()


def test_compress_and_checksum_cancelled_terminates_and_raises(tmp_path, monkeypatch):
    raw = tmp_path / "disk.img"
    raw.write_bytes(b"x" * 10)
    out = tmp_path / "disk.img.zst"
    fake = _FakePopen(polls_until_done=5, out_path=str(out))
    monkeypatch.setattr(compression_mod.subprocess, "Popen", fake)

    with pytest.raises(CompressionCancelled):
        compress_and_checksum(
            str(raw), str(out), should_cancel=lambda: True, poll_interval=0,
        )

    assert fake._terminated
    assert not out.exists()  # zstd never got to "finish" writing it


def test_compress_and_checksum_cancel_fires_after_a_few_polls(tmp_path, monkeypatch):
    raw = tmp_path / "disk.img"
    raw.write_bytes(b"x" * 10)
    out = tmp_path / "disk.img.zst"
    fake = _FakePopen(polls_until_done=100, out_path=str(out))
    monkeypatch.setattr(compression_mod.subprocess, "Popen", fake)

    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] >= 3

    with pytest.raises(CompressionCancelled):
        compress_and_checksum(str(raw), str(out), should_cancel=should_cancel, poll_interval=0)

    assert fake._terminated
    assert calls["n"] == 3


# --- raw_only_result (skip-compression path) ----------------------------------


def test_raw_only_result_uses_the_same_file_for_both(tmp_path, fake_run):
    raw = tmp_path / "disk.img"
    raw.write_bytes(b"x" * 500)
    fake_run.when("sha256sum", stdout="cafef00d  disk.img\n")

    result = raw_only_result(str(raw))

    assert result.raw_path == str(raw)
    assert result.compressed_path == str(raw)
    assert result.raw_size_bytes == 500
    assert result.compressed_size_bytes == 500
    assert result.sha256_raw == "cafef00d"
    assert result.sha256_compressed == "cafef00d"
