from attic.core.config import UNRECOGNIZED_FS_LABEL
from attic.core.fsdetect import detect_filesystem


def test_blkid_resolves_type_and_label(fake_run):
    fake_run.when("blkid", stdout="TYPE=vfat\nLABEL=WIN98\n")
    d = detect_filesystem("/img")
    assert d.recognized
    assert d.fstype == "vfat"
    assert d.label == "WIN98"
    assert d.method == "blkid"
    assert d.is_fat


def test_blkid_type_but_no_label_falls_back_to_mlabel(fake_run):
    fake_run.when("blkid", stdout="TYPE=vfat\n")
    fake_run.when("mlabel", stdout="Volume label is MYDISK (abcd-1234)\n")
    d = detect_filesystem("/img")
    assert d.fstype == "vfat"
    assert d.label == "MYDISK"


def test_file_signature_when_blkid_silent(fake_run):
    fake_run.when("blkid", returncode=2, stdout="")  # blkid found nothing
    fake_run.when("mlabel", returncode=1)             # not FAT / mtools failed
    fake_run.when("file", stdout="DOS/MBR boot sector, code offset ...\n")
    d = detect_filesystem("/img")
    assert d.recognized
    assert d.fstype == "vfat"
    assert d.method == "file-s"


def test_ntfs_via_file_signature(fake_run):
    fake_run.when("blkid", returncode=2)
    fake_run.when("mlabel", returncode=1)
    fake_run.when("file", stdout="NTFS filesystem, sectors/cluster 8 ...\n")
    d = detect_filesystem("/img")
    assert d.fstype == "ntfs"
    assert not d.is_fat


def test_candidate_mount_when_signature_inconclusive(fake_run):
    fake_run.when("blkid", returncode=2)
    fake_run.when("mlabel", returncode=1)
    fake_run.when("file", stdout="data\n")  # nothing recognizable

    probed = []

    def probe(image, fstype):
        probed.append(fstype)
        return fstype == "ext2"  # only ext2 mounts

    d = detect_filesystem("/img", mount_probe=probe)
    assert d.recognized
    assert d.fstype == "ext2"
    assert d.method == "mount-probe"
    # It tried candidates in order until one worked.
    assert probed == ["vfat", "msdos", "ntfs", "ext2"]


def test_unrecognized_when_all_fail(fake_run):
    fake_run.when("blkid", returncode=2)
    fake_run.when("mlabel", returncode=1)
    fake_run.when("file", stdout="data\n")

    d = detect_filesystem("/img", mount_probe=lambda i, fs: False)
    assert not d.recognized
    assert d.fstype == UNRECOGNIZED_FS_LABEL
    assert d.method == "none"


def test_unrecognized_without_mount_probe(fake_run):
    fake_run.when("blkid", returncode=2)
    fake_run.when("mlabel", returncode=1)
    fake_run.when("file", stdout="data\n")
    d = detect_filesystem("/img")  # no probe -> step 3 skipped
    assert not d.recognized


def test_initial_label_seeded_for_optical(fake_run):
    # Optical passes the ISO9660 volume id in; blkid confirms iso9660.
    fake_run.when("blkid", stdout="TYPE=iso9660\n")
    d = detect_filesystem("/img", initial_label="BACKUP_2003")
    assert d.fstype == "iso9660"
    assert d.label == "BACKUP_2003"
