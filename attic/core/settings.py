"""Per-working-folder settings, persisted as JSON inside the folder itself.

Settings travel with the working folder (not the machine), so resuming a folder —
possibly on another workstation — keeps the same choices. Qt-free and testable;
the Qt settings dialog reads/writes through here.

Unknown keys in the on-disk file are ignored and missing keys fall back to
defaults, so the file format tolerates version drift in both directions.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

from .config import DEFAULT_STAGING_DIRNAME, ZSTD_LEVEL, ZSTD_LONG

SETTINGS_FILENAME = "attic_settings.json"


@dataclass
class AppSettings:
    # Compression
    zstd_level: int = ZSTD_LEVEL
    zstd_long: bool = ZSTD_LONG
    keep_raw_image: bool = False  # keep the uncompressed .img alongside the .zst

    # Don't archive any image at all (raw or compressed) when the read was
    # fully clean AND extraction fully succeeded -- Extracted Files/ is kept
    # either way, only the image itself is skipped. Checked and, when it
    # applies, acted on *before* compression even runs (not a delete-after),
    # so a large clean HDD image never pays the zstd-19 cost just to be
    # discarded. Overrides keep_raw_image when both are on for a given job,
    # since the point is to keep no image at all in that case. Off by default:
    # this only matters once you've decided the extracted files alone are
    # enough and the image would just be redundant with them.
    hdd_auto_skip_image_when_clean: bool = False
    # Same idea for optical: an optical capture's own status only reaches "ok"
    # when ddrescue read the whole disc clean AND extraction (and DVD-Video
    # conversion, if applicable) fully succeeded -- see OpticalCaptureWorker.
    optical_auto_skip_image_when_clean: bool = False

    # Devices
    optical_device: str = "/dev/sr0"
    ddrescue_retries: int = 3
    # ddrescue's own -T/--timeout: give up once this many minutes have passed
    # since the last successful read (NOT total run time -- a mostly-good disk
    # never trips this; only a stretch of genuinely stuck retries does). 0
    # leaves ddrescue to try as long as it takes (the old, still-default
    # behavior). Paired with ddrescue_retries as the speed/thoroughness dial:
    # lower both for a "best effort" pass on media you don't need perfect, or
    # a large batch you don't want one bad disc to stall for hours.
    ddrescue_timeout_minutes: int = 0
    # Which of ddrescue's four phases (copying -> trimming -> scraping ->
    # retrying; see core.ddrescue.DDRESCUE_STOP_AFTER_CHOICES) to actually run.
    # "full" is today's behavior (all four, ddrescue_retries retry passes).
    # Earlier values give up sooner and are strictly faster but leave more of
    # a damaged disk unrecovered -- e.g. "scraping" still sweeps every bad
    # area sector-by-sector once but skips the (often slowest) repeated retry
    # passes. Unrecognized values (e.g. from an older/newer settings file)
    # fall back to "full" behavior in core.ddrescue.build_ddrescue_argv.
    ddrescue_stop_after: str = "full"
    # Eject the tray once ddrescue has finished imaging the disc -- a physical
    # cue that it's safe to pull the disc and load the next one. Best-effort;
    # a missing `eject` binary or a drive that doesn't support it never fails
    # the capture.
    eject_on_complete: bool = True

    # DVD-Video (VIDEO_TS) discs: after normal extraction, detect the VIDEO_TS
    # folder structure and transcode each title into an ordinary .mp4 (see
    # attic.core.dvdvideo) so the result is directly watchable rather than a
    # DVD-authoring folder. Needs ffmpeg on PATH; if missing, the raw VIDEO_TS
    # copy from normal extraction is kept and nothing is transcoded.
    convert_dvd_video: bool = True
    # x264 CRF (quality/size tradeoff): lower is higher quality/larger file.
    # 18 is visually near-lossless -- plenty for a home-video archive copy.
    dvd_video_crf: int = 18

    # Floppy geometry (the track-grid dimensions)
    floppy_cylinders: int = 80
    floppy_heads: int = 2
    # gw refuses to write a sector image without a format. "ibm.scan" probes the
    # IBM FM/MFM variants per track, which covers the DOS-through-XP era range
    # (160K/360K/720K/1.2M/1.44M) without knowing the disk up front. Override for
    # non-IBM media (e.g. "amiga.amigados", "atarist.720").
    floppy_format: str = "ibm.scan"
    # Comma-separated extra gw --format profiles to also try decoding when
    # floppy_capture_flux is on and floppy_format's own decode doesn't come out
    # fully clean. This only re-decodes the already-captured flux host-side
    # (see FloppyCaptureWorker._decode_flux) -- no extra pass over the drive,
    # so it's cheap to try several. Whichever candidate recovers the most
    # sectors/clean tracks wins. Run `gw formats` to see every profile your gw
    # version knows about (there are families for Apple II, classic Mac, and
    # more beyond the two included here) and add whichever ones you expect to
    # meet. Ignored entirely when floppy_capture_flux is off, since direct-read
    # mode bakes the format into the single hardware pass.
    floppy_format_fallbacks: str = "amiga.amigados,atarist.720"
    # Serial port of the Greaseweazle; blank lets gw auto-detect (the usual case,
    # and the only sane default when the port number moves between plug-ins).
    floppy_device: str = ""
    # Capture the raw flux stream and decode the sector image from it host-side,
    # rather than letting gw decode during the read. Costs ~10-15 MB compressed
    # per disk but preserves a master that can be re-decoded years later without
    # putting fragile media back in the drive. Also frees the drive sooner: only
    # the flux read needs hardware.
    floppy_capture_flux: bool = True
    # Revolutions per track for the flux read. 0 uses the format's own default
    # (2 for ibm.scan). More revolutions give a damaged track more independent
    # samples to recover from, at ~50% more storage per extra revolution.
    floppy_flux_revs: int = 0
    # In-place re-reads of the same track before giving up on it. 0 uses gw's
    # own default (3). gw stops retrying the moment a track fully succeeds, so
    # raising this costs virtually nothing on a healthy disk -- it only adds
    # time on tracks that are already failing, in exchange for more chances
    # at a marginal (not permanently damaged) sector.
    floppy_retries: int = 5
    # Retract the head and re-seek before each retry, rather than re-reading
    # in place. 0 uses gw's own default (0, i.e. off). Can dislodge dust/debris
    # or land the head slightly differently -- a genuinely different recovery
    # attempt than an in-place retry, not just another chance at the same read.
    floppy_seek_retries: int = 2

    # Webcam
    camera_index: int = 0
    skip_photo: bool = False  # never prompt for a photo when True

    # Naming
    # When True, unlabeled volumes silently keep their generated fallback name and
    # are NOT queued in Pending Labels. Leave False if someone will label them.
    auto_accept_fallback_names: bool = False

    # HDD workflow: photograph + label before selecting/docking the drive.
    hdd_photo_before_dock: bool = True

    # Staging: where per-job scratch work (ddrescue's raw image, extraction
    # scratch, zstd's working files) is written while a job is in flight, before
    # being atomically promoted into the working folder's Floppy/HDD/CD archive
    # tree. Kept independent of the working folder so a fast local disk can be
    # used for scratch work even when the working folder itself is a slower or
    # removable archive drive. Blank defaults to a dedicated folder under the
    # home directory (not the home directory itself). Point this at the working
    # folder itself (or leave the working folder on local disk) to restore the
    # old single-location behavior. This is a machine-local path, so it may
    # need re-pointing after moving the working folder to a different computer.
    staging_root: str = ""

    def resolved_staging_root(self) -> str:
        """``staging_root``, or ``~/Attic Staging`` when left blank."""
        return self.staging_root.strip() or os.path.join(
            os.path.expanduser("~"), DEFAULT_STAGING_DIRNAME
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def settings_path(working_folder: str) -> str:
    return os.path.join(working_folder, SETTINGS_FILENAME)


def load_settings(working_folder: str) -> AppSettings:
    """Load settings for ``working_folder``, falling back to defaults.

    Missing file, unreadable file, or malformed JSON all yield defaults; known
    keys present in the file override defaults, unknown keys are ignored.
    """
    path = settings_path(working_folder)
    defaults = AppSettings()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    known = {f.name for f in fields(AppSettings)}
    kwargs = {k: v for k, v in data.items() if k in known}
    try:
        return AppSettings(**{**asdict(defaults), **kwargs})
    except TypeError:
        return defaults


def save_settings(working_folder: str, settings: AppSettings) -> str:
    """Write ``settings`` to the working folder. Returns the path written."""
    path = settings_path(working_folder)
    os.makedirs(working_folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(settings.to_json())
    return path
