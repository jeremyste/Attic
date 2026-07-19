"""Front/back photo capture: slot selection + finalize copy."""

import os

from attic.core.config import (
    MediaType,
    PHOTO_BACK_SUFFIX,
    PHOTO_FRONT_SUFFIX,
    PHOTO_SUFFIX,
)


def test_optical_has_single_photo_slot():
    from attic.ui.label_dialog import _photo_slots

    slots = _photo_slots(MediaType.OPTICAL)
    assert [s for _, s in slots] == [PHOTO_SUFFIX]


def test_rectangular_media_have_front_and_back_slots():
    from attic.ui.label_dialog import _photo_slots

    for mt in (MediaType.FLOPPY, MediaType.HDD):
        suffixes = [s for _, s in _photo_slots(mt)]
        assert suffixes == [PHOTO_FRONT_SUFFIX, PHOTO_BACK_SUFFIX]


def test_finalize_copies_all_photos(qapp, tmp_path):
    from attic.controllers.compress_pool import FinalizePool, FinalizeRequest
    from attic.core import staging
    from attic.core.catalog import CatalogRow
    from attic.ui.session import Session

    wf = str(tmp_path)
    Session(wf).ensure_skeleton()
    st = staging.create_staging(wf, MediaType.FLOPPY, "s1")
    with open(st.child("floppy.img"), "wb") as f:
        f.write(b"x" * 4096)
    front = tmp_path / "f.jpg"; front.write_bytes(b"FRONT")
    back = tmp_path / "b.jpg"; back.write_bytes(b"BACK")

    pool = FinalizePool()
    done = {}
    pool.signals.done.connect(lambda d, rows: done.setdefault("d", d))
    pool.submit(FinalizeRequest(
        working_folder=wf, media_type=MediaType.FLOPPY, staging=st,
        raw_image_path=st.child("floppy.img"), chosen_name="Disk1",
        rows=[CatalogRow(media_type="floppy", chosen_name="Disk1", status="ok")],
        photos={PHOTO_FRONT_SUFFIX: str(front), PHOTO_BACK_SUFFIX: str(back)},
    ))
    for _ in range(100):
        qapp.processEvents()
        if pool.wait(50):
            break
    qapp.processEvents()

    final = os.path.join(wf, "Floppy", "Disk1")
    assert os.path.isfile(os.path.join(final, "Disk1_photo_front.jpg"))
    assert os.path.isfile(os.path.join(final, "Disk1_photo_back.jpg"))
