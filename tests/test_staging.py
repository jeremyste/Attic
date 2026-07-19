import os
from datetime import datetime

import pytest

from attic.core.config import MediaType, TMP_DIRNAME
from attic.core.staging import (
    create_staging,
    discard,
    final_dir,
    new_session_id,
    promote,
)


def test_session_id_format_and_uniqueness():
    a = new_session_id(datetime(2026, 7, 18, 14, 32, 1, 5))
    assert a == "20260718_143201_000005"
    # Distinct microseconds -> distinct ids.
    b = new_session_id(datetime(2026, 7, 18, 14, 32, 1, 6))
    assert a != b


def test_isolation_per_type_and_session(tmp_path):
    wf = str(tmp_path)
    f1 = create_staging(wf, MediaType.FLOPPY, "s1")
    f2 = create_staging(wf, MediaType.FLOPPY, "s2")
    h1 = create_staging(wf, MediaType.HDD, "s1")
    # All four are distinct real directories.
    paths = {f1.path, f2.path, h1.path}
    assert len(paths) == 3
    for s in (f1, f2, h1):
        assert s.exists()
    # Layout: .tmp/<type>/<session>
    assert f1.path == os.path.join(wf, TMP_DIRNAME, "floppy", "s1")
    assert h1.path == os.path.join(wf, TMP_DIRNAME, "hdd", "s1")


def test_create_refuses_duplicate_session(tmp_path):
    create_staging(str(tmp_path), MediaType.OPTICAL, "dup")
    with pytest.raises(FileExistsError):
        create_staging(str(tmp_path), MediaType.OPTICAL, "dup")


def test_promote_moves_atomically_and_cleans_tmp(tmp_path):
    wf = str(tmp_path)
    staging = create_staging(wf, MediaType.OPTICAL, "s1")
    # Put content in the staging dir.
    with open(staging.child("disc.img.zst"), "w") as fh:
        fh.write("data")
    os.makedirs(staging.child("Extracted Files"))

    dest = final_dir(wf, MediaType.OPTICAL, "Backup")
    result = promote(staging, dest)

    assert result == dest
    assert os.path.isfile(os.path.join(dest, "disc.img.zst"))
    assert os.path.isdir(os.path.join(dest, "Extracted Files"))
    # Staging path is gone...
    assert not os.path.exists(staging.path)
    # ...and the now-empty .tmp tree was pruned.
    assert not os.path.exists(os.path.join(wf, TMP_DIRNAME))


def test_promote_does_not_prune_tmp_with_other_jobs(tmp_path):
    wf = str(tmp_path)
    done = create_staging(wf, MediaType.FLOPPY, "done")
    other = create_staging(wf, MediaType.FLOPPY, "still-running")
    with open(done.child("a.img.zst"), "w") as fh:
        fh.write("x")

    promote(done, final_dir(wf, MediaType.FLOPPY, "Disk1"))

    # The other job's staging dir and the .tmp tree survive.
    assert other.exists()
    assert os.path.isdir(os.path.join(wf, TMP_DIRNAME, "floppy"))


def test_promote_refuses_existing_destination(tmp_path):
    wf = str(tmp_path)
    staging = create_staging(wf, MediaType.HDD, "s1")
    dest = final_dir(wf, MediaType.HDD, "Data")
    os.makedirs(dest)
    with pytest.raises(FileExistsError):
        promote(staging, dest)
    # On failure the staging dir is left in place for inspection.
    assert staging.exists()


def test_failure_leaves_staging_in_place(tmp_path):
    # Simulate a failed job: we simply never promote. The dir and its path must
    # remain available for the catalog to reference.
    wf = str(tmp_path)
    staging = create_staging(wf, MediaType.HDD, "boom")
    with open(staging.child("partial.img"), "w") as fh:
        fh.write("half")
    assert staging.exists()
    assert staging.rel_path() == os.path.join(TMP_DIRNAME, "hdd", "boom")


def test_discard_removes_staging(tmp_path):
    wf = str(tmp_path)
    staging = create_staging(wf, MediaType.FLOPPY, "gone")
    discard(staging)
    assert not staging.exists()
