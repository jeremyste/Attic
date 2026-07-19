from attic.controllers.floppy import (
    TRACK_CLEAN,
    TRACK_FAILED,
    TRACK_RETRIED,
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
