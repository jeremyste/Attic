import os
from datetime import date

from attic.core.datescan import scan_tree_date


def _touch(path, when: date):
    path.write_text("x")
    ts = date_to_ts(when)
    os.utime(path, (ts, ts))


def date_to_ts(d: date) -> float:
    from datetime import datetime

    return datetime(d.year, d.month, d.day, 12, 0, 0).timestamp()


TODAY = date(2026, 7, 18)


def test_empty_tree(tmp_path):
    r = scan_tree_date(str(tmp_path), today=TODAY)
    assert r.chosen_date is None
    assert r.files_scanned == 0
    assert not r.suspect
    assert r.date_str == ""


def test_newest_valid_mtime_chosen(tmp_path):
    _touch(tmp_path / "a", date(1998, 3, 1))
    _touch(tmp_path / "b", date(2001, 11, 20))
    _touch(tmp_path / "c", date(1995, 6, 6))
    r = scan_tree_date(str(tmp_path), today=TODAY)
    assert r.chosen_date == date(2001, 11, 20)
    assert r.date_str == "2001-11-20"
    assert not r.suspect
    assert r.files_scanned == 3


def test_recurses_subdirs(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _touch(tmp_path / "a", date(1999, 1, 1))
    _touch(sub / "b", date(2003, 5, 5))
    r = scan_tree_date(str(tmp_path), today=TODAY)
    assert r.chosen_date == date(2003, 5, 5)


def test_future_date_is_suspect_and_excluded(tmp_path):
    _touch(tmp_path / "good", date(2000, 1, 1))
    _touch(tmp_path / "future", date(2099, 1, 1))
    r = scan_tree_date(str(tmp_path), today=TODAY)
    # Future date excluded from chosen, but flags suspect.
    assert r.chosen_date == date(2000, 1, 1)
    assert r.suspect
    assert r.newest_raw == date(2099, 1, 1)


def test_pre_1980_date_is_suspect_and_excluded(tmp_path):
    _touch(tmp_path / "old", date(1970, 1, 2))
    r = scan_tree_date(str(tmp_path), today=TODAY)
    assert r.chosen_date is None
    assert r.suspect
    assert r.files_scanned == 1


def test_boundary_dates_valid(tmp_path):
    _touch(tmp_path / "min", date(1980, 1, 1))
    _touch(tmp_path / "max", TODAY)
    r = scan_tree_date(str(tmp_path), today=TODAY)
    assert r.chosen_date == TODAY
    assert not r.suspect
