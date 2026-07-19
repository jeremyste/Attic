import os

from attic.core import catalog
from attic.core.catalog import CatalogRow
from attic.core.config import TMP_DIRNAME
from attic.ui.session import Session


def test_rename_item_updates_all_matching_rows(tmp_path):
    wf = str(tmp_path)
    catalog.append_rows(wf, [
        CatalogRow(media_type="hdd", chosen_name="drive_001", partition_label="A",
                   folder_path="HDD/drive_001"),
        CatalogRow(media_type="hdd", chosen_name="drive_001", partition_label="B",
                   folder_path="HDD/drive_001"),
        CatalogRow(media_type="floppy", chosen_name="drive_001",
                   folder_path="Floppy/drive_001"),  # different type, untouched
    ])
    changed = catalog.rename_item(
        wf, "hdd", "drive_001", "Family PC",
        old_folder_path="HDD/drive_001", new_folder_path="HDD/Family PC",
    )
    assert changed == 2
    rows = catalog.read_rows(wf)
    hdd = [r for r in rows if r["media_type"] == "hdd"]
    assert all(r["chosen_name"] == "Family PC" for r in hdd)
    assert all(r["folder_path"] == "HDD/Family PC" for r in hdd)
    # The floppy row with the same name is untouched (scoped per media type).
    flop = [r for r in rows if r["media_type"] == "floppy"][0]
    assert flop["chosen_name"] == "drive_001"


def test_rename_item_no_catalog(tmp_path):
    assert catalog.rename_item(
        str(tmp_path), "hdd", "x", "y", old_folder_path="a", new_folder_path="b"
    ) == 0


def test_session_ensure_skeleton(tmp_path):
    wf = str(tmp_path / "work")
    Session(wf).ensure_skeleton()
    assert os.path.isfile(os.path.join(wf, "catalog.csv"))
    assert os.path.isdir(os.path.join(wf, "Floppy"))
    assert os.path.isdir(os.path.join(wf, "HDD"))
    assert os.path.isdir(os.path.join(wf, "CD"))
    assert os.path.isdir(os.path.join(wf, TMP_DIRNAME))


def test_session_ensure_skeleton_idempotent(tmp_path):
    wf = str(tmp_path / "work")
    s = Session(wf)
    s.ensure_skeleton()
    catalog.append_rows(wf, [CatalogRow(media_type="floppy", chosen_name="keep")])
    s.ensure_skeleton()  # must not wipe existing catalog
    assert len(catalog.read_rows(wf)) == 1
