import errno
import os

import attic.core.extract as extract_mod
from attic.core.extract import ExtractResult, extract, extract_fat, extract_mount


def test_fat_dispatch_uses_mcopy(tmp_path, fake_run):
    fake_run.when("mcopy", returncode=0)
    out = tmp_path / "out"
    out.mkdir()
    (out / "FILE.TXT").write_text("x")  # what mcopy "would have" written
    r = extract(str(tmp_path / "d.img"), str(out), "vfat")
    assert r.ok
    argv = fake_run.find("mcopy")
    assert argv is not None
    assert "-s" in argv  # recursive
    assert "-i" in argv  # image input


def test_fat_failure_is_reported_not_raised(tmp_path, fake_run):
    fake_run.when("mcopy", returncode=1, stderr="mcopy: bad image")
    r = extract_fat(str(tmp_path / "d.img"), str(tmp_path / "out"))
    assert not r.ok
    assert "mcopy" in r.error_summary


def test_non_fat_dispatch_uses_pkexec_mount(tmp_path, fake_run):
    # mount succeeds; nothing on the (empty tmp) mountpoint to copy, so this
    # only checks dispatch/argv correctness -- an empty result correctly
    # isn't "ok" (see test_mount_zero_files_is_flagged_not_ok) and falls
    # through to the recovery tiers, which is exercised separately.
    fake_run.when("mount", returncode=0)
    fake_run.when("umount", returncode=0)
    extract(str(tmp_path / "d.img"), str(tmp_path / "out"), "ext2")
    mount_argv = fake_run.find("mount")
    assert mount_argv[0] == "pkexec"
    assert "ro,loop" in mount_argv
    assert fake_run.ran("umount")


def test_mount_failure_reported(tmp_path, fake_run):
    fake_run.when("mount", returncode=32, stderr="mount: wrong fs type")
    r = extract_mount(str(tmp_path / "d.img"), str(tmp_path / "out"), "ntfs")
    assert not r.ok
    assert "mount" in r.error_summary


# --- 0-files-is-not-ok (floppy_012's silent-empty bug) -----------------------


def test_fat_zero_files_is_flagged_not_ok(tmp_path, fake_run):
    # mcopy exits 0 (no hard error) but the destination stays empty -- this is
    # exactly what a corrupted FAT/root-directory area looks like.
    fake_run.when("mcopy", returncode=0)
    r = extract_fat(str(tmp_path / "d.img"), str(tmp_path / "out"))
    assert not r.ok
    assert r.file_count == 0
    assert "0 files" in r.error_summary


def test_mount_zero_files_is_flagged_not_ok(tmp_path, fake_run):
    fake_run.when("mount", returncode=0)
    fake_run.when("umount", returncode=0)
    r = extract_mount(str(tmp_path / "d.img"), str(tmp_path / "out"), "ext2")
    assert not r.ok
    assert r.file_count == 0
    assert "0 files" in r.error_summary


# --- ENOSPC fail-fast --------------------------------------------------------


def test_mount_enospc_stops_immediately_and_reports_free_space(
    tmp_path, fake_run, monkeypatch,
):
    fake_run.when("mount", returncode=0)
    fake_run.when("umount", returncode=0)

    mnt = tmp_path / "mnt"
    mnt.mkdir()
    (mnt / "a.txt").write_text("hello")
    (mnt / "b.txt").write_text("world")
    monkeypatch.setattr(extract_mod.tempfile, "mkdtemp", lambda prefix="": str(mnt))

    def fake_copy2(src, dst, *a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(extract_mod.shutil, "copy2", fake_copy2)

    dest = tmp_path / "out"
    r = extract_mount(str(tmp_path / "d.img"), str(dest), "ntfs")
    assert not r.ok
    assert "free" in r.error_summary.lower()
    assert "space" in r.error_summary.lower()
    # Stopped on the first entry ("a.txt" sorts first) -- "b.txt" never attempted.
    assert not (dest / "b.txt").exists()


def test_mount_non_enospc_error_skips_entry_and_continues(tmp_path, fake_run, monkeypatch):
    fake_run.when("mount", returncode=0)
    fake_run.when("umount", returncode=0)

    mnt = tmp_path / "mnt"
    mnt.mkdir()
    (mnt / "bad.txt").write_text("x")
    (mnt / "good.txt").write_text("y")
    monkeypatch.setattr(extract_mod.tempfile, "mkdtemp", lambda prefix="": str(mnt))

    real_copy2 = extract_mod.shutil.copy2

    def flaky_copy2(src, dst, *a, **k):
        if "bad.txt" in src:
            raise OSError(errno.EACCES, "permission denied")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(extract_mod.shutil, "copy2", flaky_copy2)

    dest = tmp_path / "out"
    r = extract_mount(str(tmp_path / "d.img"), str(dest), "ntfs")
    assert not r.ok  # one entry failed
    assert r.file_count == 1  # but the other one still made it
    assert (dest / "good.txt").exists()
    assert "bad.txt" in r.error_summary


# --- Tier 2/2b: tsk_recover / 7z ---------------------------------------------


def test_tsk_recover_allocated_pass_success(tmp_path, fake_run, monkeypatch):
    monkeypatch.setattr(extract_mod.su, "has_tool", lambda name: True)
    dest = tmp_path / "out"
    fake_run.when(lambda argv: argv[:1] == ["tsk_recover"], returncode=0)
    # FakeRun has no filesystem side-effect hook, so seed the file tsk_recover
    # "would have" written before invoking -- this tests the count/ok logic,
    # not tsk_recover itself (verified separately by hand against a real
    # damaged floppy image, where it genuinely recovered files).
    os.makedirs(dest, exist_ok=True)
    (dest / "recovered.txt").write_text("x")

    r = extract_mod.extract_tsk(str(tmp_path / "d.img"), str(dest))
    assert r.ok
    assert r.file_count == 1
    assert "tsk_recover -a" in r.notes
    argv = fake_run.find("tsk_recover")
    assert "-e" not in argv  # allocated-only pass was enough, no need for -e


def test_tsk_recover_falls_back_to_deleted_pass_when_allocated_empty(
    tmp_path, fake_run, monkeypatch,
):
    monkeypatch.setattr(extract_mod.su, "has_tool", lambda name: True)
    dest = tmp_path / "out"
    fake_run.when(lambda argv: argv[:1] == ["tsk_recover"], returncode=0)

    # First (-a) call: nothing written, count stays 0, so the code re-runs
    # with -e -- simulate that by creating the file only after the first call.
    calls = {"n": 0}

    def run_and_maybe_seed(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            os.makedirs(dest, exist_ok=True)
            (dest / "undeleted.txt").write_text("x")
        return extract_mod.su.CmdResult(argv=list(a[0]), returncode=0)

    monkeypatch.setattr(extract_mod.su, "run", run_and_maybe_seed)

    r = extract_mod.extract_tsk(str(tmp_path / "d.img"), str(dest))
    assert r.ok
    assert r.file_count == 1
    assert "-e" in r.notes
    assert calls["n"] == 2


def test_tsk_recover_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_mod.su, "has_tool", lambda name: False)
    r = extract_mod.extract_tsk(str(tmp_path / "d.img"), str(tmp_path / "out"))
    assert not r.ok
    assert "not installed" in r.error_summary


def test_7z_extracts_optical_image(tmp_path, fake_run, monkeypatch):
    monkeypatch.setattr(extract_mod.su, "has_tool", lambda name: True)
    dest = tmp_path / "out"
    fake_run.when("7z", returncode=0)
    os.makedirs(dest, exist_ok=True)
    (dest / "movie.mp4").write_text("x")  # what 7z "would have" written

    r = extract_mod.extract_7z(str(tmp_path / "d.iso"), str(dest))
    assert r.ok
    assert r.file_count == 1
    argv = fake_run.find("7z")
    assert argv[0] == "7z"
    assert "x" in argv


# --- Tier 3: photorec carving -------------------------------------------------


def test_photorec_moves_carved_files_into_dest_and_drops_report(
    tmp_path, fake_run, monkeypatch,
):
    monkeypatch.setattr(extract_mod.su, "has_tool", lambda name: True)
    work_base = tmp_path / "work"
    work_base.mkdir()
    monkeypatch.setattr(extract_mod.tempfile, "mkdtemp", lambda prefix="": str(work_base))
    fake_run.when("photorec", returncode=0)

    # What a real photorec run leaves behind (verified by hand against a real
    # damaged floppy image: output lands in "<given /d path>.1", generic
    # filenames, plus a report.xml that isn't a recovered file).
    produced = work_base / "out.1"
    produced.mkdir()
    (produced / "f0000001.jpg").write_bytes(b"\xff\xd8\xff")
    (produced / "report.xml").write_text("<report/>")

    dest = tmp_path / "Extracted Files (carved)"
    r = extract_mod.extract_photorec(str(tmp_path / "d.img"), str(dest))
    assert r.ok
    assert r.file_count == 1
    assert (dest / "f0000001.jpg").exists()
    assert not (dest / "report.xml").exists()
    assert "carved" in r.notes.lower()


def test_photorec_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_mod.su, "has_tool", lambda name: False)
    r = extract_mod.extract_photorec(str(tmp_path / "d.img"), str(tmp_path / "out"))
    assert not r.ok
    assert "not installed" in r.error_summary


# --- extract() dispatch: the tiered fallback chain ---------------------------


def test_extract_returns_immediately_when_tier1_succeeds(tmp_path, monkeypatch):
    def fake_fat(image, dest_dir, *, offset=0, timeout=None):
        os.makedirs(dest_dir, exist_ok=True)
        open(os.path.join(dest_dir, "a.txt"), "w").close()
        return ExtractResult(dest_dir=dest_dir, file_count=1, ok=True)

    def must_not_run(*a, **k):
        raise AssertionError("tier 2 must not run when tier 1 already succeeded")

    monkeypatch.setattr(extract_mod, "extract_fat", fake_fat)
    monkeypatch.setattr(extract_mod, "extract_tsk", must_not_run)

    r = extract(str(tmp_path / "d.img"), str(tmp_path / "out"), "vfat")
    assert r.ok
    assert r.file_count == 1


def test_extract_falls_through_to_tsk_when_tier1_empty(tmp_path, monkeypatch):
    def fake_fat(image, dest_dir, *, offset=0, timeout=None):
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False, error_summary="empty")

    def fake_tsk(image, dest_dir, *, offset=0, timeout=None):
        os.makedirs(dest_dir, exist_ok=True)
        open(os.path.join(dest_dir, "recovered.txt"), "w").close()
        return ExtractResult(dest_dir=dest_dir, file_count=1, ok=True, notes="tsk_recover -a")

    monkeypatch.setattr(extract_mod, "extract_fat", fake_fat)
    monkeypatch.setattr(extract_mod, "extract_tsk", fake_tsk)

    r = extract(str(tmp_path / "d.img"), str(tmp_path / "out"), "vfat")
    assert r.ok
    assert r.file_count == 1
    assert "tier1" in r.error_summary
    assert "tsk_recover" in r.notes


def test_extract_tries_7z_for_optical_but_not_other_fstypes(tmp_path, monkeypatch):
    calls = []

    def fake_mount(image, dest_dir, fstype, *, offset=0, size=0, timeout=None):
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False, error_summary="empty")

    def fake_tsk(image, dest_dir, *, offset=0, timeout=None):
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False, error_summary="empty")

    def fake_7z(image, dest_dir, *, timeout=None):
        calls.append("7z")
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False, error_summary="empty")

    def fake_photorec(image, dest_dir, *, offset=0, size=0, timeout=None):
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False, error_summary="empty")

    monkeypatch.setattr(extract_mod, "extract_mount", fake_mount)
    monkeypatch.setattr(extract_mod, "extract_tsk", fake_tsk)
    monkeypatch.setattr(extract_mod, "extract_7z", fake_7z)
    monkeypatch.setattr(extract_mod, "extract_photorec", fake_photorec)

    extract(str(tmp_path / "d.img"), str(tmp_path / "out1"), "iso9660")
    assert calls == ["7z"]

    calls.clear()
    extract(str(tmp_path / "d.img"), str(tmp_path / "out2"), "ntfs")
    assert calls == []


def test_extract_falls_all_the_way_through_to_carving(tmp_path, monkeypatch):
    def all_empty(*a, **k):
        dest_dir = a[1] if len(a) > 1 else k["dest_dir"]
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False, error_summary="empty")

    def fake_photorec(image, dest_dir, *, offset=0, size=0, timeout=None):
        os.makedirs(dest_dir, exist_ok=True)
        open(os.path.join(dest_dir, "f0000001.jpg"), "w").close()
        return ExtractResult(dest_dir=dest_dir, file_count=1, ok=True, notes="carved")

    monkeypatch.setattr(extract_mod, "extract_fat", all_empty)
    monkeypatch.setattr(extract_mod, "extract_tsk", all_empty)
    monkeypatch.setattr(extract_mod, "extract_photorec", fake_photorec)

    dest = tmp_path / "Extracted Files"
    r = extract(str(tmp_path / "d.img"), str(dest), "vfat")
    assert r.ok
    assert r.file_count == 1
    assert r.dest_dir == f"{dest} (carved)"


def test_extract_reports_failure_when_every_tier_comes_up_empty(tmp_path, monkeypatch):
    def all_empty(*a, **k):
        dest_dir = a[1] if len(a) > 1 else k["dest_dir"]
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False, error_summary="empty")

    monkeypatch.setattr(extract_mod, "extract_fat", all_empty)
    monkeypatch.setattr(extract_mod, "extract_tsk", all_empty)
    monkeypatch.setattr(extract_mod, "extract_photorec", all_empty)

    r = extract(str(tmp_path / "d.img"), str(tmp_path / "out"), "vfat")
    assert not r.ok
    assert r.file_count == 0
    assert "tier1" in r.error_summary and "tsk_recover" in r.error_summary and "photorec" in r.error_summary
