"""The timeline's inspector: full-resolution scans, editing that saves, and
the Curation Studio's way of moving around a page.

The compiled timeline is two things at once: a page published on its own
(read-only, embedded previews) and the studio's Timeline Preview, served by a
live ArchiveSys. These tests hold the inspector to the second role without
breaking the first.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from src.curation.server import create_curation_app
from src.database.db_manager import DatabaseManager
from src.visualizer.compiler import DashboardCompiler

TEMPLATE = Path(__file__).parent.parent / "src" / "visualizer" / "templates" / "index.html.jinja2"


@pytest.fixture(scope="module")
def page() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    """The text between two markers, e.g. one function or one CSS rule."""
    i = source.index(start)
    return source[i:source.index(end, i + len(start))]


# =============================================================================
# Moving around a scan: scroll, pinch, click -- never click-and-drag
# =============================================================================
def test_there_is_no_click_and_drag(page):
    """Dragging a page around is not how any operating system scrolls."""
    assert "cursor: grab" not in page and "cursor: grabbing" not in page
    assert "panning = true" not in page
    mousemove_handlers = re.findall(r'addEventListener\("mousemove"', page)
    assert not mousemove_handlers, "a mousemove handler would bring dragging back"


def test_scrolling_moves_and_only_a_pinch_zooms(page):
    wheel = _block(page, 'scanViewport.addEventListener("wheel"', "{ passive: false });")
    assert "e.ctrlKey || e.metaKey" in wheel, "pinch / Cmd-scroll should zoom"
    assert "panX -= dx" in wheel and "panY -= dy" in wheel, "plain scrolling should move"


def test_a_click_zooms_but_a_moved_press_does_nothing(page):
    release = _block(page, 'window.addEventListener("mouseup"', "});")
    assert "moved > 5" in release and "zoomOnClick(e)" in release


def test_the_scan_is_laid_out_at_native_size(page):
    """Capping the image's CSS size and magnifying that is what made zoom blurry."""
    rule = _block(page, ".scan-img-stage img {", "}")
    assert "max-width: none" in rule and "max-height: none" in rule
    assert "max-width: 85%" not in page


# =============================================================================
# Full resolution, when a live server has it
# =============================================================================
def test_a_live_server_supplies_the_master(page):
    assert "/api/image?doc_id=" in page, "the inspector should load the original from the server"
    assert 'fetch("/api/app"' in page, "and only after checking a live ArchiveSys is there"
    popout = _block(page, "modalBtnPopout.addEventListener", "});")
    assert "masterUrl(doc)" in popout, "Pop Out promises the high-res scan; it must open it"


# =============================================================================
# Editing that saves
# =============================================================================
def test_edits_go_through_the_one_writer_and_touch_nothing_else(page):
    save = _block(page, "function saveInspectorEdits", "\n    }\n")
    assert "/api/documents/${id}" in save and 'method: "POST"' in save
    assert "JSON.stringify({ fields: body })" in save
    assert "post(doc.id, fields)" in save
    # A partial save must never clear who is in the document or its review state.
    assert "people_links" not in save and "reviewed" not in save


def test_a_pages_text_is_saved_to_that_page(page):
    """An item's other pages keep their own transcription; the rest of the record is the item's."""
    save = _block(page, "function saveInspectorEdits", "\n    }\n")
    assert "post(page.id, { transcription })" in save
    assert "post(doc.id, itemFields)" in save
    show = _block(page, "function showPage", "\n    }\n")
    assert show.index("saveInspectorEdits()") < show.index("editPage ="), \
        "text typed on one page must be saved before the next page is shown"


def test_an_unreadable_date_is_not_sent(page):
    assert "date.ok ? date.edtf : undefined" in page
    assert "if (value === undefined) continue;" in page


def test_nothing_typed_is_lost_when_moving_on(page):
    step = _block(page, "function stepModal", "\n    }\n")
    assert step.index("saveInspectorEdits()") < step.index("populateModalRecord(activeRecord)")
    assert 'inspectionDialog.addEventListener("close", () => { saveInspectorEdits(); })' in page
    assert "keepalive: true" in page, "a reload of the preview must flush the last keystrokes"


def test_shortcuts_do_not_fire_while_typing(page):
    """Arrow keys, + and - belong to the text being edited, not to the viewer."""
    keys = _block(page, "// Keyboard Shortcuts", "\n    });")
    assert keys.index("if (typing) return;") < keys.index('"ArrowLeft"')


def test_the_studio_takes_the_inspectors_version(page):
    """Otherwise the studio's autosave would write its stale copy back."""
    assert '"archivesys:document-saved"' in page
    studio = (TEMPLATE.parents[2] / "curation" / "static" / "studio.js").read_text(encoding="utf-8")
    listener = _block(studio, 'window.addEventListener("message"', "\n});")
    assert "e.origin !== window.location.origin" in listener
    assert "!dirtySinceSave && !savingNow" in listener, "never overwrite typing that is still unsaved"


def test_the_date_reader_is_inlined_into_the_compiled_page(tmp_path):
    db = DatabaseManager(tmp_path / "a.db")
    db.init_db()
    out = DashboardCompiler(db, tmp_path / "dist").compile(copy_scans=False).read_text(encoding="utf-8")
    assert "function parseHistoricalDate" in out
    assert "{{" not in out.split("function parseHistoricalDate")[0][-200:], "the include rendered"


# =============================================================================
# The endpoints the inspector relies on
# =============================================================================
@pytest.fixture
def served(tmp_path):
    db = DatabaseManager(tmp_path / "archive.db")
    db.init_db()
    scan = tmp_path / "letter.png"
    Image.new("RGB", (40, 30), (200, 190, 170)).save(scan)
    doc_id = db.add_document(file_path=str(scan), title="A letter", edtf_date="1923")
    (tmp_path / "scans").mkdir()
    client = TestClient(create_curation_app(db, tmp_path / "scans", dist_dir=tmp_path / "dist"))
    return client, db, doc_id


def test_the_server_identifies_itself(served):
    client, _, _ = served
    assert client.get("/api/app").json()["app"] == "archivesys"


def test_an_edit_lands_where_a_recompile_would_put_it(served):
    client, _, doc_id = served
    before = client.get(f"/api/timeline/entries?doc_id={doc_id}").json()["entries"]
    assert [e["start"] for e in before] == ["1923-01-01"]

    saved = client.post(f"/api/documents/{doc_id}",
                        json={"fields": {"title": "A letter from Smyrna", "edtf_date": "1922-09"}})
    assert saved.status_code == 200

    after = client.get(f"/api/timeline/entries?doc_id={doc_id}").json()["entries"]
    assert after[0]["title"] == "A letter from Smyrna"
    assert (after[0]["start"], after[0]["date_display"]) == ("1922-09-01", "Sep 1922")


def test_an_undated_document_has_no_entry(served):
    client, _, doc_id = served
    client.post(f"/api/documents/{doc_id}", json={"fields": {"edtf_date": ""}})
    assert client.get(f"/api/timeline/entries?doc_id={doc_id}").json()["entries"] == []


def test_a_partial_save_keeps_the_people(served):
    """The inspector sends only the fields it shows; links it does not show stay put."""
    client, db, doc_id = served
    person = db.add_person("Ἑλένη", "Θεοδώρου")
    db.link_document_person(doc_id, person, role="recipient")
    client.post(f"/api/documents/{doc_id}", json={"fields": {"summary": "Written from Smyrna."}})
    full = client.get(f"/api/documents/{doc_id}/full").json()
    assert [(p["id"], p["role"]) for p in full["people"]] == [(person, "recipient")]
    assert full["summary"] == "Written from Smyrna." and full["title"] == "A letter"
