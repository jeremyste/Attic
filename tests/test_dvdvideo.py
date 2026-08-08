"""Tests for DVD-Video (VIDEO_TS) detection and title-to-mp4 conversion."""

from __future__ import annotations

import os

import attic.core.subprocess_util as su
from attic.core.dvdvideo import (
    Title,
    convert,
    convert_title,
    discover_titles,
    find_video_ts_dir,
)
from attic.core.subprocess_util import CmdResult


def _touch(path: str, content: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)


def _fake_ffmpeg_ok(monkeypatch):
    """ffmpeg succeeds and actually 'produces' its output file (last argv)."""
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        _touch(argv[-1])
        return CmdResult(argv=argv, returncode=0)

    monkeypatch.setattr(su, "run", fake)
    monkeypatch.setattr(su, "has_tool", lambda name: True)
    return calls


def _fake_ffmpeg_fails(monkeypatch, stderr="ffmpeg: invalid data"):
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        return CmdResult(argv=argv, returncode=1, stderr=stderr)

    monkeypatch.setattr(su, "run", fake)
    monkeypatch.setattr(su, "has_tool", lambda name: True)
    return calls


# --- find_video_ts_dir -------------------------------------------------------


def test_finds_video_ts_at_root(tmp_path):
    vts = tmp_path / "VIDEO_TS"
    _touch(str(vts / "VTS_01_1.VOB"))
    assert find_video_ts_dir(str(tmp_path)) == str(vts)


def test_finds_video_ts_case_insensitively(tmp_path):
    vts = tmp_path / "video_ts"
    _touch(str(vts / "vts_01_1.vob"))
    assert find_video_ts_dir(str(tmp_path)) == str(vts)


def test_finds_video_ts_one_level_nested(tmp_path):
    vts = tmp_path / "DISC1" / "VIDEO_TS"
    _touch(str(vts / "VTS_01_1.VOB"))
    assert find_video_ts_dir(str(tmp_path)) == str(vts)


def test_empty_video_ts_folder_is_not_a_match(tmp_path):
    # A folder merely named VIDEO_TS with no .VOB inside isn't a DVD-Video disc.
    (tmp_path / "VIDEO_TS").mkdir()
    assert find_video_ts_dir(str(tmp_path)) is None


def test_no_video_ts_anywhere(tmp_path):
    _touch(str(tmp_path / "photo.jpg"))
    assert find_video_ts_dir(str(tmp_path)) is None


# --- discover_titles ----------------------------------------------------------


def test_discovers_single_title_and_excludes_menu_vob(tmp_path):
    vts = tmp_path / "VIDEO_TS"
    _touch(str(vts / "VTS_01_0.VOB"))  # menu -- must be excluded
    _touch(str(vts / "VTS_01_1.VOB"))
    titles = discover_titles(str(vts))
    assert len(titles) == 1
    assert titles[0].number == 1
    assert titles[0].parts == [str(vts / "VTS_01_1.VOB")]


def test_discovers_multiple_titles_in_order(tmp_path):
    vts = tmp_path / "VIDEO_TS"
    for n in ("01", "02", "03"):
        _touch(str(vts / f"VTS_{n}_0.VOB"))
        _touch(str(vts / f"VTS_{n}_1.VOB"))
    titles = discover_titles(str(vts))
    assert [t.number for t in titles] == [1, 2, 3]


def test_multi_part_title_parts_are_ordered(tmp_path):
    vts = tmp_path / "VIDEO_TS"
    # Written out of order on disk; parts must still come back 1, 2, 3.
    _touch(str(vts / "VTS_01_3.VOB"))
    _touch(str(vts / "VTS_01_1.VOB"))
    _touch(str(vts / "VTS_01_2.VOB"))
    titles = discover_titles(str(vts))
    assert len(titles) == 1
    assert titles[0].parts == [
        str(vts / "VTS_01_1.VOB"),
        str(vts / "VTS_01_2.VOB"),
        str(vts / "VTS_01_3.VOB"),
    ]


def test_no_matching_vobs_yields_no_titles(tmp_path):
    vts = tmp_path / "VIDEO_TS"
    _touch(str(vts / "VIDEO_TS.VOB"))  # top-level menu VOB, not a VTS_nn_x one
    assert discover_titles(str(vts)) == []


# --- convert_title --------------------------------------------------------


def test_convert_title_single_part_uses_direct_input(tmp_path, monkeypatch):
    part = str(tmp_path / "VTS_01_1.VOB")
    _touch(part)
    out = str(tmp_path / "out" / "Movie.mp4")
    calls = _fake_ffmpeg_ok(monkeypatch)

    result = convert_title(Title(number=1, parts=[part]), out)

    assert result.ok
    argv = calls[0]
    assert argv[0] == "ffmpeg"
    assert part in argv  # not wrapped in a concat: string
    assert "-c:v" in argv and "libx264" in argv
    assert os.path.exists(out)


def test_convert_title_multi_part_uses_concat_protocol(tmp_path, monkeypatch):
    p1 = str(tmp_path / "VTS_01_1.VOB")
    p2 = str(tmp_path / "VTS_01_2.VOB")
    _touch(p1)
    _touch(p2)
    out = str(tmp_path / "out" / "Movie.mp4")
    calls = _fake_ffmpeg_ok(monkeypatch)

    convert_title(Title(number=1, parts=[p1, p2]), out)

    argv = calls[0]
    input_idx = argv.index("-i") + 1
    assert argv[input_idx] == f"concat:{p1}|{p2}"


def test_convert_title_crf_is_passed_through(tmp_path, monkeypatch):
    part = str(tmp_path / "VTS_01_1.VOB")
    _touch(part)
    out = str(tmp_path / "out" / "Movie.mp4")
    calls = _fake_ffmpeg_ok(monkeypatch)

    convert_title(Title(number=1, parts=[part]), out, crf=23)

    argv = calls[0]
    assert argv[argv.index("-crf") + 1] == "23"


def test_convert_title_reports_ffmpeg_failure(tmp_path, monkeypatch):
    part = str(tmp_path / "VTS_01_1.VOB")
    _touch(part)
    out = str(tmp_path / "out" / "Movie.mp4")
    _fake_ffmpeg_fails(monkeypatch)

    result = convert_title(Title(number=1, parts=[part]), out)

    assert not result.ok
    assert "ffmpeg" in result.error_summary or "invalid data" in result.error_summary


def test_convert_title_zero_byte_output_is_not_ok(tmp_path, monkeypatch):
    # ffmpeg can exit 0 but leave nothing usable (e.g. killed mid-write in a
    # way that still looks like success) -- an empty output must not pass.
    part = str(tmp_path / "VTS_01_1.VOB")
    _touch(part)
    out = str(tmp_path / "out" / "Movie.mp4")

    def fake(argv, **kw):
        os.makedirs(os.path.dirname(argv[-1]), exist_ok=True)
        open(argv[-1], "wb").close()  # exists, but empty
        return CmdResult(argv=argv, returncode=0)

    monkeypatch.setattr(su, "run", fake)

    result = convert_title(Title(number=1, parts=[part]), out)
    assert not result.ok


# --- convert (top-level orchestration) ----------------------------------------


def test_convert_returns_none_when_not_a_dvd_video_disc(tmp_path):
    _touch(str(tmp_path / "photo.jpg"))
    assert convert(str(tmp_path), str(tmp_path / "video"), "Movie") is None


def test_convert_reports_missing_ffmpeg(tmp_path, monkeypatch):
    vts = tmp_path / "VIDEO_TS"
    _touch(str(vts / "VTS_01_1.VOB"))
    monkeypatch.setattr(su, "has_tool", lambda name: False)

    result = convert(str(tmp_path), str(tmp_path / "video"), "Movie")

    assert result is not None
    assert not result.ok
    assert "ffmpeg" in result.error_summary.lower()


def test_convert_single_title_named_after_base_name(tmp_path, monkeypatch):
    vts = tmp_path / "VIDEO_TS"
    _touch(str(vts / "VTS_01_1.VOB"))
    _fake_ffmpeg_ok(monkeypatch)
    dest = str(tmp_path / "video")

    result = convert(str(tmp_path), dest, "Reunion 2006")

    assert result.ok
    assert result.converted_count == 1
    assert result.titles[0].out_path == os.path.join(dest, "Reunion 2006.mp4")


def test_convert_multi_title_gets_numbered_files(tmp_path, monkeypatch):
    vts = tmp_path / "VIDEO_TS"
    _touch(str(vts / "VTS_01_1.VOB"))
    _touch(str(vts / "VTS_02_1.VOB"))
    _fake_ffmpeg_ok(monkeypatch)
    dest = str(tmp_path / "video")

    result = convert(str(tmp_path), dest, "Reunion")

    assert result.ok
    names = sorted(os.path.basename(t.out_path) for t in result.titles)
    assert names == ["Reunion - Title 01.mp4", "Reunion - Title 02.mp4"]


def test_convert_partial_failure_still_keeps_successful_titles(tmp_path, monkeypatch):
    vts = tmp_path / "VIDEO_TS"
    _touch(str(vts / "VTS_01_1.VOB"))
    _touch(str(vts / "VTS_02_1.VOB"))

    def fake(argv, **kw):
        out = argv[-1]
        if "01" in out:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(b"x")
            return CmdResult(argv=argv, returncode=0)
        return CmdResult(argv=argv, returncode=1, stderr="corrupt input")

    monkeypatch.setattr(su, "run", fake)
    monkeypatch.setattr(su, "has_tool", lambda name: True)

    result = convert(str(tmp_path), str(tmp_path / "video"), "Reunion")

    assert result.ok  # at least one title succeeded
    assert result.converted_count == 1
    assert len(result.titles) == 2
    assert "1/2" in result.error_summary
