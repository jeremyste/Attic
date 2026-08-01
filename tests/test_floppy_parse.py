from attic.controllers.floppy import (
    TRACK_CLEAN,
    TRACK_FAILED,
    TRACK_RETRIED,
    build_gw_read_cmd,
    parse_gw_track_line,
)


def test_clean_track():
    tr = parse_gw_track_line("T0.0: IBM MFM (18/18 sectors)")
    assert tr is not None
    assert (tr.cyl, tr.head) == (0, 0)
    assert tr.status == TRACK_CLEAN
    assert tr.sectors_got == 18 and tr.sectors_total == 18


def test_missing_sectors_is_retried():
    tr = parse_gw_track_line("T12.1: IBM MFM (17/18 sectors)")
    assert tr.status == TRACK_RETRIED
    assert (tr.cyl, tr.head) == (12, 1)


def test_zero_sectors_is_failed():
    tr = parse_gw_track_line("T39.0: IBM MFM (0/18 sectors)")
    assert tr.status == TRACK_FAILED


def test_retry_keyword_forces_retried_even_if_full():
    tr = parse_gw_track_line("T5.0: retrying... IBM MFM (18/18 sectors)")
    assert tr.status == TRACK_RETRIED


def test_non_track_line_returns_none():
    assert parse_gw_track_line("Reading cyls 0-79...") is None
    assert parse_gw_track_line("") is None


def test_track_line_with_flux_suffix_and_retry_counter():
    # Real gw 1.23 output: "T<c>.<h>: <sector summary> from <flux summary>"
    # with an optional " (Retry #n.m)" tail.
    line = "T3.1: IBM MFM (17/18 sectors) from 250kbps, 300rpm (Retry #0.1)"
    tr = parse_gw_track_line(line)
    assert (tr.cyl, tr.head) == (3, 1)
    assert tr.status == TRACK_RETRIED
    assert (tr.sectors_got, tr.sectors_total) == (17, 18)


def test_track_line_with_physical_drive_remap():
    tr = parse_gw_track_line("T40.0 <- Drive 20.0: IBM MFM (9/9 sectors) from 250kbps")
    assert (tr.cyl, tr.head) == (40, 0)
    assert tr.status == TRACK_CLEAN


class TestBuildGwReadCmd:
    def test_format_is_always_passed(self):
        # gw refuses to write a sector image without --format.
        cmd = build_gw_read_cmd("/tmp/floppy.img")
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "ibm.scan"
        assert cmd[-1] == "/tmp/floppy.img"

    def test_blank_format_falls_back_to_scan(self):
        cmd = build_gw_read_cmd("/tmp/f.img", disk_format="  ")
        assert cmd[cmd.index("--format") + 1] == "ibm.scan"

    def test_custom_format_and_device(self):
        cmd = build_gw_read_cmd(
            "/tmp/f.img", disk_format="amiga.amigados", device="/dev/ttyACM0"
        )
        assert cmd[cmd.index("--format") + 1] == "amiga.amigados"
        assert cmd[cmd.index("--device") + 1] == "/dev/ttyACM0"

    def test_device_omitted_when_blank(self):
        assert "--device" not in build_gw_read_cmd("/tmp/f.img", device="")
