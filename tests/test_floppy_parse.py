from attic.controllers.floppy import (
    TRACK_CLEAN,
    TRACK_FAILED,
    TRACK_RETRIED,
    TrackResult,
    build_gw_read_cmd,
    infer_uniform_format,
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

    def test_retries_omitted_when_zero(self):
        cmd = build_gw_read_cmd("/tmp/f.img", retries=0)
        assert "--retries" not in cmd

    def test_retries_passed_through(self):
        cmd = build_gw_read_cmd("/tmp/f.img", retries=8)
        assert cmd[cmd.index("--retries") + 1] == "8"

    def test_seek_retries_omitted_when_zero(self):
        cmd = build_gw_read_cmd("/tmp/f.img", seek_retries=0)
        assert "--seek-retries" not in cmd

    def test_seek_retries_passed_through(self):
        cmd = build_gw_read_cmd("/tmp/f.img", seek_retries=2)
        assert cmd[cmd.index("--seek-retries") + 1] == "2"


def _tracks(*totals: int) -> dict[tuple[int, int], TrackResult]:
    """Build a track_results dict of the shape infer_uniform_format expects,
    one synthetic (cyl, 0) entry per total given."""
    return {
        (i, 0): TrackResult(cyl=i, head=0, status=TRACK_CLEAN, sectors_got=t, sectors_total=t)
        for i, t in enumerate(totals)
    }


class TestInferUniformFormat:
    def test_real_world_case_two_short_tracks_out_of_160(self):
        # Reproduces the actual bug: c0h0 and c1h0 scanned as 17 sectors,
        # every other one of 160 tracks scanned as the true 18.
        totals = [17, 17] + [18] * 158
        assert infer_uniform_format(_tracks(*totals), "ibm.scan") == "ibm.1440"

    def test_no_override_when_already_uniform(self):
        totals = [18] * 160
        assert infer_uniform_format(_tracks(*totals), "ibm.scan") is None

    def test_no_override_for_an_explicit_non_scan_format(self):
        # The user already pinned geometry themselves -- nothing to correct.
        totals = [17, 17] + [18] * 158
        assert infer_uniform_format(_tracks(*totals), "ibm.1440") is None

    def test_no_override_without_a_clear_majority(self):
        # A genuinely mixed/exotic disk -- don't guess.
        totals = [18] * 5 + [21] * 4
        assert infer_uniform_format(_tracks(*totals), "ibm.scan") is None

    def test_no_override_with_too_little_data(self):
        assert infer_uniform_format(_tracks(18), "ibm.scan") is None
        assert infer_uniform_format({}, "ibm.scan") is None

    def test_zero_sector_tracks_are_ignored_not_counted_as_a_vote(self):
        # Totally unreadable tracks (0/0) shouldn't dilute the majority.
        totals = [17, 17] + [18] * 158 + [0] * 20
        assert infer_uniform_format(_tracks(*totals), "ibm.scan") == "ibm.1440"

    def test_unmapped_majority_falls_back_to_no_override(self):
        # A majority exists but doesn't match any known standard secs count.
        totals = [17] * 3 + [23] * 20
        assert infer_uniform_format(_tracks(*totals), "ibm.scan") is None
