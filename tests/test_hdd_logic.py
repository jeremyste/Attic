from attic.core.config import Status
from attic.controllers.hdd import _worse, normalize_parted_fstype


def test_normalize_known_fstypes():
    assert normalize_parted_fstype("fat32") == "fat32"
    assert normalize_parted_fstype("NTFS") == "ntfs"
    assert normalize_parted_fstype("ext4") == "ext4"


def test_normalize_unknown_returns_blank():
    assert normalize_parted_fstype("linux-swap") == ""
    assert normalize_parted_fstype("") == ""
    assert normalize_parted_fstype(None) == ""


def test_worse_status_rollup():
    assert _worse(Status.OK, Status.PARTIAL) == Status.PARTIAL
    assert _worse(Status.PARTIAL, Status.OK) == Status.PARTIAL
    assert _worse(Status.OK, Status.FAILED) == Status.FAILED
    assert _worse(Status.UNRECOGNIZED_FS, Status.PARTIAL) == Status.UNRECOGNIZED_FS
    assert _worse(Status.FAILED, Status.UNRECOGNIZED_FS) == Status.FAILED
