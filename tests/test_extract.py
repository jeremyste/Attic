from attic.core.extract import extract, extract_fat, extract_mount


def test_fat_dispatch_uses_mcopy(tmp_path, fake_run):
    fake_run.when("mcopy", returncode=0)
    r = extract(str(tmp_path / "d.img"), str(tmp_path / "out"), "vfat")
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
    # mount succeeds; nothing on the (empty tmp) mountpoint to copy; umount runs.
    fake_run.when("mount", returncode=0)
    fake_run.when("umount", returncode=0)
    r = extract(str(tmp_path / "d.img"), str(tmp_path / "out"), "ext2")
    assert r.ok
    mount_argv = fake_run.find("mount")
    assert mount_argv[0] == "pkexec"
    assert "ro,loop" in mount_argv
    assert fake_run.ran("umount")


def test_mount_failure_reported(tmp_path, fake_run):
    fake_run.when("mount", returncode=32, stderr="mount: wrong fs type")
    r = extract_mount(str(tmp_path / "d.img"), str(tmp_path / "out"), "ntfs")
    assert not r.ok
    assert "mount" in r.error_summary
