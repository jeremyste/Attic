import pytest

from attic.core.checksums import sha256_file
from attic.core.compression import (
    build_zstd_argv,
    compress_and_checksum,
    compressed_path_for,
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
