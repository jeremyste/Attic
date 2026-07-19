from attic.core import catalog
from attic.core.catalog import CatalogRow
from attic.core.config import MediaType
from attic.core.naming import (
    build_fallback_name,
    dedupe_name,
    resolve_name,
)


def test_build_fallback_name():
    assert build_fallback_name(MediaType.FLOPPY, 1, "2001-05-06") == "floppy_001_2001-05-06"
    assert build_fallback_name(MediaType.HDD, 12, "1999-01-01") == "drive_012_1999-01-01"
    assert build_fallback_name(MediaType.OPTICAL, 7, "") == "disc_007"


def test_dedupe_name():
    assert dedupe_name("Docs", set()) == "Docs"
    assert dedupe_name("Docs", {"Docs"}) == "Docs_2"
    assert dedupe_name("Docs", {"Docs", "Docs_2"}) == "Docs_3"


def test_physical_label_wins(tmp_path):
    r = resolve_name(
        str(tmp_path), MediaType.FLOPPY,
        physical_label="My Sticker", detected_label="VOLNAME", fallback_date="2000-01-01",
    )
    assert r.chosen_name == "My Sticker"
    assert r.physical_label_entered == "My Sticker"
    assert r.detected_label == "VOLNAME"  # preserved even though it didn't win
    assert not r.used_fallback
    assert r.sequence_number == 1


def test_detected_label_used_when_no_physical(tmp_path):
    r = resolve_name(
        str(tmp_path), MediaType.FLOPPY,
        physical_label="", detected_label="WIN98", fallback_date="1998-06-25",
    )
    assert r.chosen_name == "WIN98"
    assert not r.used_fallback


def test_fallback_when_nothing(tmp_path):
    r = resolve_name(
        str(tmp_path), MediaType.OPTICAL,
        physical_label="", detected_label="", fallback_date="2003-04-05",
    )
    assert r.chosen_name == "disc_001_2003-04-05"
    assert r.used_fallback
    assert r.fallback_date == "2003-04-05"


def test_chosen_name_sanitized(tmp_path):
    r = resolve_name(str(tmp_path), MediaType.FLOPPY, physical_label='bad/name:here')
    assert r.chosen_name == "bad_name_here"


def test_sequence_increments_from_catalog(tmp_path):
    catalog.append_rows(
        str(tmp_path),
        [
            CatalogRow(media_type="floppy", sequence_number="3"),
            CatalogRow(media_type="disc", sequence_number="10"),
        ],
    )
    r = resolve_name(str(tmp_path), MediaType.FLOPPY)
    assert r.sequence_number == 4  # floppy scope, not affected by disc's 10


def test_dedup_scoped_per_media_type(tmp_path):
    catalog.append_row(str(tmp_path), CatalogRow(media_type="floppy", chosen_name="Docs"))
    # Same name on a disc does NOT collide with the floppy.
    disc = resolve_name(str(tmp_path), MediaType.OPTICAL, physical_label="Docs")
    assert disc.chosen_name == "Docs"
    # Same name on another floppy DOES collide.
    flop = resolve_name(str(tmp_path), MediaType.FLOPPY, physical_label="Docs")
    assert flop.chosen_name == "Docs_2"


def test_extra_taken_prevents_intra_batch_collision(tmp_path):
    r = resolve_name(
        str(tmp_path), MediaType.HDD,
        physical_label="Data", extra_taken={"Data"},
    )
    assert r.chosen_name == "Data_2"
