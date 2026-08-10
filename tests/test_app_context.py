"""Routing logic in AppContext (the shared post-capture orchestration).

Focuses on the non-interactive fallback path (unlabeled volume) which needs no
dialog: it must finalize under the fallback name AND queue a pending-label entry.
"""

import os

from attic.controllers.base import CaptureArtifacts, JobRequest
from attic.core import catalog
from attic.core.catalog import CatalogRow
from attic.core.config import MediaType, Status
from attic.core.staging import create_staging


class FakePool:
    def __init__(self):
        self.submitted = []

    def submit(self, req):
        self.submitted.append(req)


def _make_context(qapp, working_folder, settings=None):
    from attic.core.settings import AppSettings
    from attic.ui.app_context import AppContext
    from attic.ui.pending_labels_panel import PendingLabelsPanel
    from attic.ui.processing_panel import ProcessingPanel
    from attic.ui.session import Session

    session = Session(working_folder)
    session.ensure_skeleton()
    panel = PendingLabelsPanel()
    # A real Processing panel, so routing's calls into it are exercised rather
    # than skipped by the None guard.
    ctx = AppContext(
        session=session, finalize_pool=FakePool(), pending_panel=panel,
        processing_panel=ProcessingPanel(),
        settings=settings or AppSettings(),
    )
    return ctx, panel


def test_unlabeled_volume_finalizes_and_queues_pending(qapp, tmp_path):
    wf = str(tmp_path)
    ctx, panel = _make_context(qapp, wf)
    staging = create_staging(wf, MediaType.FLOPPY, "s1")
    request = JobRequest(staging_root=wf, media_type=MediaType.FLOPPY, source_id="floppy")
    artifacts = CaptureArtifacts(
        raw_image_path=staging.child("floppy.img"),
        detected_label="",  # unlabeled
        fallback_date="1999-05-05",
        filesystem_detected="vfat",
        status=Status.OK,
    )

    ctx.route_single(request, staging, artifacts)

    # One finalize job submitted, under the fallback name.
    assert len(ctx.finalize_pool.submitted) == 1
    req = ctx.finalize_pool.submitted[0]
    assert req.chosen_name == "floppy_001_1999-05-05"
    assert len(req.rows) == 1

    # Pending item queued but not yet shown until finalize completes.
    assert panel.list.count() == 0
    ctx.on_finalize_done("HDD/whatever", req.rows)
    assert panel.list.count() == 1


def test_auto_accept_skips_pending_queue(qapp, tmp_path):
    from attic.core.settings import AppSettings

    wf = str(tmp_path)
    ctx, panel = _make_context(qapp, wf, settings=AppSettings(auto_accept_fallback_names=True))
    staging = create_staging(wf, MediaType.FLOPPY, "s1")
    request = JobRequest(staging_root=wf, media_type=MediaType.FLOPPY, source_id="floppy")
    artifacts = CaptureArtifacts(
        raw_image_path=staging.child("floppy.img"),
        detected_label="", fallback_date="1999-05-05",
        filesystem_detected="vfat", status=Status.OK,
    )

    ctx.route_single(request, staging, artifacts)

    req = ctx.finalize_pool.submitted[0]
    assert req.chosen_name == "floppy_001_1999-05-05"  # still auto-named
    # But NOT queued in pending, even after finalize completes.
    ctx.on_finalize_done("x", req.rows)
    assert panel.list.count() == 0


def test_settings_flow_into_finalize_request(qapp, tmp_path):
    from attic.core.settings import AppSettings

    wf = str(tmp_path)
    ctx, _ = _make_context(qapp, wf, settings=AppSettings(
        zstd_level=12, zstd_long=False, keep_raw_image=True,
        auto_accept_fallback_names=True,
    ))
    staging = create_staging(wf, MediaType.OPTICAL, "s1")
    request = JobRequest(staging_root=wf, media_type=MediaType.OPTICAL, source_id="/dev/sr0")
    artifacts = CaptureArtifacts(
        raw_image_path=staging.child("disc.img"), status=Status.OK, filesystem_detected="iso9660",
    )
    ctx.route_single(request, staging, artifacts)
    req = ctx.finalize_pool.submitted[0]
    assert req.zstd_level == 12
    assert req.zstd_long is False
    assert req.keep_raw is True


def test_capture_failure_records_failed_row_no_finalize(qapp, tmp_path):
    wf = str(tmp_path)
    ctx, _panel = _make_context(qapp, wf)
    staging = create_staging(wf, MediaType.OPTICAL, "s1")
    request = JobRequest(staging_root=wf, media_type=MediaType.OPTICAL, source_id="/dev/sr0")
    artifacts = CaptureArtifacts(
        raw_image_path=staging.child("disc.img"),
        status=Status.FAILED,
        error_summary="ddrescue failed",
    )

    ctx.route_single(request, staging, artifacts)

    assert ctx.finalize_pool.submitted == []
    rows = catalog.read_rows(wf)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "ddrescue failed" in rows[0]["error_summary"]
    assert ".tmp" in rows[0]["notes"]  # points at staging dir


def test_pending_rename_updates_catalog_and_folder(qapp, tmp_path):
    wf = str(tmp_path)
    ctx, panel = _make_context(qapp, wf)
    # Simulate a finalized fallback item on disk + in catalog.
    from attic.core.staging import final_dir
    from attic.ui.pending_labels_panel import PendingItem

    name = "floppy_001_1999-05-05"
    os.makedirs(final_dir(wf, MediaType.FLOPPY, name))
    catalog.append_row(wf, CatalogRow(
        media_type="floppy", chosen_name=name,
        folder_path=os.path.relpath(final_dir(wf, MediaType.FLOPPY, name), wf),
    ))
    item = PendingItem(wf, MediaType.FLOPPY, name)
    panel.add_pending(item)

    panel._apply_rename(item, "Recipes", row=0)

    assert os.path.isdir(final_dir(wf, MediaType.FLOPPY, "Recipes"))
    assert not os.path.exists(final_dir(wf, MediaType.FLOPPY, name))
    rows = catalog.read_rows(wf)
    assert rows[0]["chosen_name"] == "Recipes"
    assert rows[0]["folder_path"] == "Floppy/Recipes"


def test_unattended_naming_never_opens_a_dialog(qapp, tmp_path, monkeypatch):
    """A labeled disk must not block on a confirmation prompt when unattended.

    Regression: the dialog was gated only on "did we fall back to a generated
    name", so typing a physical label still produced a modal prompt even with
    auto-accept on -- which stalls the queue the user is trying to keep fed.
    """
    from attic.core.settings import AppSettings

    def _boom(*a, **k):
        raise AssertionError("NamingDialog must not be constructed")

    monkeypatch.setattr("attic.ui.app_context.NamingDialog", _boom)

    wf = str(tmp_path)
    ctx, panel = _make_context(
        qapp, wf, settings=AppSettings(auto_accept_fallback_names=True)
    )
    staging = create_staging(wf, MediaType.FLOPPY, "s1")
    request = JobRequest(
        staging_root=wf, media_type=MediaType.FLOPPY,
        physical_label="test2", source_id="floppy",
    )
    artifacts = CaptureArtifacts(
        raw_image_path=staging.child("floppy.img"),
        detected_label="VOLNAME", filesystem_detected="vfat", status=Status.OK,
    )

    ctx.route_single(request, staging, artifacts)

    req = ctx.finalize_pool.submitted[0]
    assert req.chosen_name == "test2"  # the typed label wins, silently
    assert panel.list.count() == 0


def test_naming_dialog_still_shown_when_not_unattended(qapp, tmp_path, monkeypatch):
    """With auto-accept off, a labeled disk is still confirmed as before."""
    from attic.core.settings import AppSettings

    shown = []

    class _Dlg:
        def __init__(self, *a, **k):
            shown.append(k.get("suggested_name"))

        def exec(self):
            return 0  # user dismissed; keeps the suggested name

    monkeypatch.setattr("attic.ui.app_context.NamingDialog", _Dlg)

    wf = str(tmp_path)
    ctx, _ = _make_context(
        qapp, wf, settings=AppSettings(auto_accept_fallback_names=False)
    )
    staging = create_staging(wf, MediaType.FLOPPY, "s1")
    request = JobRequest(
        staging_root=wf, media_type=MediaType.FLOPPY,
        physical_label="test2", source_id="floppy",
    )
    ctx.route_single(request, staging, CaptureArtifacts(
        raw_image_path=staging.child("floppy.img"),
        detected_label="", filesystem_detected="vfat", status=Status.OK,
    ))

    assert shown == ["test2"]
