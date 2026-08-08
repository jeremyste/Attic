"""Detect DVD-Video discs and turn their titles into ordinary video files.

A home-burned "video DVD" presents as a UDF/ISO9660 volume containing a
VIDEO_TS/ directory of MPEG-2 program-stream .VOB files, rather than ordinary
documents or photos. Attic's normal extraction (mount + copy) faithfully
copies that directory out, but the result is a DVD-authoring folder structure
nobody can just double-click and watch. This module recognizes that shape and
transcodes each title into a single .mp4 alongside the raw VIDEO_TS copy.

A "title" is one DVD-Video title set: VTS_<nn>_0.VOB is that title's menu (no
content of interest -- skipped), VTS_<nn>_1.VOB through VTS_<nn>_9.VOB are its
content, byte-concatenable in order (that's how a DVD player itself reads
them: each is just a >1GB chunk of one continuous MPEG-2 program stream). A
disc with one content title yields a single output file; a disc with several
yields one file per title, so a multi-segment disc (each title dropped in
during authoring) doesn't get silently merged into one file in an order nobody
chose.

Needs ffmpeg on PATH. If it's missing, :func:`convert` reports that rather
than raising -- the raw VIDEO_TS copy (already produced by normal extraction)
is left as the recovery result, same as any other tier that can't run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import subprocess_util as su

VIDEO_TS_DIRNAME = "VIDEO_TS"

_TITLE_VOB_RE = re.compile(r"^VTS_(\d{2})_(\d)\.VOB$", re.IGNORECASE)


@dataclass
class Title:
    number: int
    parts: list[str]  # ordered .VOB paths: VTS_nn_1.VOB .. VTS_nn_9.VOB


@dataclass
class TitleResult:
    title: Title
    out_path: str
    ok: bool
    error_summary: str = ""


@dataclass
class ConvertResult:
    ok: bool
    video_ts_dir: str = ""
    titles: list[TitleResult] = field(default_factory=list)
    error_summary: str = ""

    @property
    def converted_count(self) -> int:
        return sum(1 for t in self.titles if t.ok)


def _has_vob(video_ts_dir: str) -> bool:
    try:
        return any(f.upper().endswith(".VOB") for f in os.listdir(video_ts_dir))
    except OSError:
        return False


def find_video_ts_dir(extracted_dir: str) -> str | None:
    """Locate a VIDEO_TS directory within an extracted disc tree, if any.

    Checks the extraction root itself and its immediate children (some discs
    put everything under one wrapper folder), matching the directory name
    case-insensitively. Requires at least one .VOB file inside -- a folder
    that merely happens to be named VIDEO_TS isn't a DVD-Video disc.
    """
    candidates = [extracted_dir]
    try:
        candidates += [
            os.path.join(extracted_dir, e) for e in os.listdir(extracted_dir)
            if os.path.isdir(os.path.join(extracted_dir, e))
        ]
    except OSError:
        return None
    for base in candidates:
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for entry in entries:
            if entry.upper() == VIDEO_TS_DIRNAME:
                video_ts = os.path.join(base, entry)
                if _has_vob(video_ts):
                    return video_ts
    return None


def discover_titles(video_ts_dir: str) -> list[Title]:
    """Group content VOBs (VTS_<nn>_1..9, never the _0 menu VOB) by title."""
    by_title: dict[int, dict[int, str]] = {}
    try:
        entries = os.listdir(video_ts_dir)
    except OSError:
        return []
    for entry in entries:
        m = _TITLE_VOB_RE.match(entry)
        if not m:
            continue
        num, part = int(m.group(1)), int(m.group(2))
        if part == 0:
            continue  # menu-only VOB, no title content
        by_title.setdefault(num, {})[part] = os.path.join(video_ts_dir, entry)
    titles = []
    for num in sorted(by_title):
        parts_by_index = by_title[num]
        parts = [parts_by_index[i] for i in sorted(parts_by_index)]
        titles.append(Title(number=num, parts=parts))
    return titles


def _ffmpeg_input_arg(parts: list[str]) -> str:
    # A title's content VOBs are one continuous MPEG-2 program stream split
    # at the DVD spec's ~1GB-per-file limit -- ffmpeg's concat protocol reads
    # them back as if they were never split, no re-encode-time stitching logic
    # needed.
    if len(parts) == 1:
        return parts[0]
    return "concat:" + "|".join(parts)


def convert_title(
    title: Title, out_path: str, *, crf: int = 18, timeout: float | None = None,
) -> TitleResult:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    argv = [
        "ffmpeg", "-y", "-i", _ffmpeg_input_arg(title.parts),
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "256k",
        out_path,
    ]
    result = su.run(argv, timeout=timeout)
    produced = os.path.exists(out_path) and os.path.getsize(out_path) > 0
    if not (result.ok and produced):
        return TitleResult(
            title=title, out_path=out_path, ok=False,
            error_summary=result.error_summary() or "ffmpeg produced no output",
        )
    return TitleResult(title=title, out_path=out_path, ok=True)


def convert(
    extracted_dir: str, dest_dir: str, base_name: str, *,
    crf: int = 18, timeout: float | None = None,
) -> ConvertResult | None:
    """Detect and convert a DVD-Video disc's titles.

    Returns ``None`` when ``extracted_dir`` isn't a DVD-Video disc at all
    (no VIDEO_TS found) -- the caller's normal, non-video handling applies.
    Returns a (possibly failed) :class:`ConvertResult` once VIDEO_TS is
    found, even if nothing could actually be converted, so the caller can
    record why.
    """
    video_ts_dir = find_video_ts_dir(extracted_dir)
    if video_ts_dir is None:
        return None

    if not su.has_tool("ffmpeg"):
        return ConvertResult(
            ok=False, video_ts_dir=video_ts_dir,
            error_summary=(
                "DVD-Video (VIDEO_TS) detected but ffmpeg is not installed -- "
                "raw VIDEO_TS copy kept, nothing transcoded"
            ),
        )

    titles = discover_titles(video_ts_dir)
    if not titles:
        return ConvertResult(
            ok=False, video_ts_dir=video_ts_dir,
            error_summary="VIDEO_TS present but no title content VOBs (VTS_nn_1.VOB+) found",
        )

    os.makedirs(dest_dir, exist_ok=True)
    multi = len(titles) > 1
    results = []
    for title in titles:
        name = f"{base_name} - Title {title.number:02d}.mp4" if multi else f"{base_name}.mp4"
        out_path = os.path.join(dest_dir, name)
        results.append(convert_title(title, out_path, crf=crf, timeout=timeout))

    ok = any(r.ok for r in results)
    failed = [r for r in results if not r.ok]
    error_summary = ""
    if failed:
        detail = "; ".join(f"title {r.title.number}: {r.error_summary}" for r in failed)
        n_ok = len(results) - len(failed)
        error_summary = f"{n_ok}/{len(results)} title(s) converted; {detail}" if ok else detail

    return ConvertResult(
        ok=ok, video_ts_dir=video_ts_dir, titles=results, error_summary=error_summary,
    )
