"""Static integrity checks for the curation studio's front-end assets.

These exist because of a real regression: deleting a block of studio.js also
removed five unrelated functions, one of which (`setupPanzoom`) was called on
the second line of the DOMContentLoaded handler. That threw, aborting the whole
init — no directory listing, no selection, no activity panel — and the page
looked frozen. Nothing failed at build time and no Python test noticed.

The checks below are deliberately crude string/regex analysis rather than a real
JS parse: they need no new dependency and no browser, and they would each have
caught that bug.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "src" / "curation" / "static"
JS = STATIC / "studio.js"
HTML = STATIC / "studio.html"
CSS = STATIC / "studio.css"


@pytest.fixture(scope="module")
def js() -> str:
    """Every script the studio page loads, in order: studio.js plus the date
    reader it shares with the timeline inspector (dates.js)."""
    page = HTML.read_text(encoding="utf-8")
    sources = re.findall(r'<script src="/static/([^"?]+)', page)
    assert "studio.js" in sources, "studio.html no longer loads studio.js"
    return "\n".join((STATIC / name).read_text(encoding="utf-8") for name in sources)


@pytest.fixture(scope="module")
def html() -> str:
    return HTML.read_text(encoding="utf-8")


def declared_functions(source: str) -> set[str]:
    return set(re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", source, re.M))


def test_every_function_called_during_init_exists(js):
    """The init handler must not reference a function that no longer exists.

    A single missing name here aborts startup entirely, so this is the check
    that matters most.
    """
    match = re.search(
        r'document\.addEventListener\("DOMContentLoaded",\s*async\s*\(\)\s*=>\s*\{(.*?)\n\}\);',
        js, re.S,
    )
    assert match, "could not locate the DOMContentLoaded init block"

    called = set(re.findall(r"(?<![\w.$])([a-z][A-Za-z0-9_$]*)\s*\(", match.group(1)))
    called -= {"await", "if", "for", "while", "return", "fetch", "setTimeout"}
    missing = sorted(called - declared_functions(js))
    assert not missing, f"init calls undefined function(s): {missing}"


@pytest.mark.parametrize("name", [
    # viewer
    "setupPanzoom", "resetPanzoom", "setupFilterControls",
    # curation
    "setupDiacriticPalette", "setupKeyboardShortcuts", "setupRecordEditor",
    "setupTagsInput", "setupModals", "populateRecord", "saveRecord", "finishRecord",
    "parseHistoricalDate", "renderKindChips", "updateProjectionReadout",
    "loadDateSuggestions",
    # explorer + queue
    "renderScanCards", "setupSelection", "toggleSelection", "clearSelection",
    "enqueueJobs", "setupActivityPanel", "pollActivity",
    # settings
    "setupSettingsView", "loadSettingsView", "runBenchmark",
    # session + saving / opening the whole archive
    "loadSession", "captureSession", "applySessionBeforeLoad", "applySessionAfterLoad",
    "applyPendingViewerRestore", "startSessionTracking", "stopSessionTracking",
    "setupArchiveFile", "saveArchive", "openArchiveFile", "confirmArchiveOpen",
    # correcting people
    "openPersonEditor", "savePersonEdits", "deletePerson", "forgetPerson",
    "mergePeople", "openMergeWith", "roleOptionsHtml", "dismissDuplicateGroups",
    # adding a whole folder
    "setupImportWizard", "openImportWizard", "renderImportReview", "advanceImport",
    "watchImport", "renderImportSummary", "isUndecided", "renderSortingNote",
])
def test_required_functions_are_defined(js, name):
    assert name in declared_functions(js), f"{name} is missing from studio.js"


def _function_body(js: str, name: str) -> str:
    match = re.search(rf"^(?:async\s+)?function\s+{name}\s*\(.*?^\}}", js, re.S | re.M)
    assert match, f"could not find {name}"
    return match.group(0)


def test_saving_the_archive_flushes_the_open_record_first(js):
    """A record edited under a second ago is still in the autosave timer; the
    saved file must include it, not the version before."""
    body = _function_body(js, "saveArchive")
    assert body.index("await flushPendingSave()") < body.index('"/api/bundle/save"')


def test_opening_an_archive_stops_saving_the_old_position_first(js):
    """Otherwise the reload's pagehide beacon, or a late debounced save, writes
    the pre-open position over the one that was just restored."""
    body = _function_body(js, "confirmArchiveOpen")
    assert body.index("stopSessionTracking()") < body.index("/api/bundle/restore/")
    assert "if (!sessionTracking) return;" in _function_body(js, "sendSession")


def test_the_timeline_preview_is_covered_while_it_reloads(js, html):
    """Every load recompiles the timeline on the server. Until the fresh page
    arrived, the old one stayed clickable -- and an inspector opened on it was
    replaced mid-edit when the new page landed."""
    assert 'id="timeline-loading"' in html
    assert 'id="timeline-iframe" src="about:blank"' in html, "no wasted compile at startup"
    assert js.count('iframe.src = "/timeline') == 1, "reload only through reloadTimelineFrame()"
    helper = _function_body(js, "reloadTimelineFrame")
    assert helper.index("cover.hidden = false") < helper.index("iframe.src =")


def test_a_person_can_be_corrected_from_their_page(html):
    """People could be created and given a picture, and nothing else: a typo
    in a name, a wrong date or a person added by mistake stayed for good."""
    for el_id in ("btn-person-edit", "btn-person-merge", "btn-person-delete",
                  "modal-edit-person", "modal-merge-with"):
        assert f'id="{el_id}"' in html, f"the person page lost {el_id}"


def test_changing_people_lets_the_open_record_save_first(js):
    """The editor autosaves its whole people list. Saving it after a merge or a
    deletion would drop a link the server just moved, so it goes first."""
    for name, request in (("deletePerson", 'method: "DELETE"'), ("mergePeople", '"/api/people/merge"')):
        body = _function_body(js, name)
        assert body.index("await flushPendingSave()") < body.index(request), name
    assert "forgetPerson(mergeId)" in _function_body(js, "mergePeople")


def test_a_linked_persons_role_is_changed_in_place(js):
    body = _function_body(js, "renderLinkedPeople")
    assert "person-role-select" in body and "roleOptionsHtml(p.role)" in body


def test_dismissing_likely_duplicates_is_not_forever(js):
    """One click on the banner's x used to hide every future duplicate too."""
    assert 'setItem("studio_dupes_dismissed", "true")' not in js
    assert "duplicateGroupKey" in _function_body(js, "loadDuplicates")


def test_a_saved_position_does_not_override_an_explicit_link(js):
    """A #view in the URL is a deliberate link and wins over the saved session."""
    body = _function_body(js, "restoreRequestedView")
    assert "viewFromHash() ||" in body


def test_no_references_to_the_retired_ocr_modal(js, html):
    """The blocking modal and its fabricated progress bar were removed together."""
    for token in ("ocr-modal", "setOCRProgress", "ocr-progress-fill", "ocr-step-"):
        assert token not in js, f"studio.js still references removed modal: {token}"
        assert token not in html, f"studio.html still references removed modal: {token}"


def test_element_ids_used_by_js_exist_in_html(js, html):
    """Catch handlers wired to elements that were never added to the markup.

    Two buttons (recompile / refresh timeline) were bound in JS while absent
    from the HTML, so the timeline had no way to be rebuilt.
    """
    ids_in_html = set(re.findall(r'id="([^"]+)"', html))
    ids_in_js = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    # Ids the JS creates at runtime rather than finding in the static markup.
    runtime_created = {"fs-load-more-row", "btn-fs-load-more", "btn-clear-filters", "import-headline"}
    missing = sorted(ids_in_js - ids_in_html - runtime_created)
    assert not missing, f"JS binds to element ids absent from studio.html: {missing}"


def test_hidden_attribute_is_enforced(js):
    """Components toggled with el.hidden set an explicit display, which wins.

    Without a [hidden] override those panels can never actually hide.
    """
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), \
        "studio.css needs a [hidden] { display: none !important } rule"


def test_thumbnail_grids_do_not_stream_full_scans(js):
    """Grid <img> tags must use the cached thumbnail endpoint.

    /api/image streams the original file — ~10 MB per scan here — which stalled
    the explorer grid entirely. Only the pan/zoom viewer should use it.
    """
    for pattern in (r"file-card-thumb-wrap[\s\S]{0,200}?/api/image",
                    r"doc-catalog-thumb-wrap[\s\S]{0,200}?/api/image"):
        assert not re.search(pattern, js), "grid thumbnails must use /api/thumb"
    assert "/api/thumb" in js


def test_viewer_zoom_and_pan_controls(js, html):
    """The document viewer must provide fit page, fit width, zoom badge, and hint overlay."""
    for el_id in ("zoom-fit-page", "zoom-fit-width", "zoom-level-badge", "viewport-hint"):
        assert f'id="{el_id}"' in html, f"missing viewer element {el_id} in studio.html"
    assert "fitToViewport" in declared_functions(js)
    css = CSS.read_text(encoding="utf-8")
    assert ".zoom-level-badge" in css
    assert ".viewport.is-zoomed" in css
    assert ".panzoom-container.smooth-transform" in css
    assert ".viewport-hint" in css


# ---------------------------------------------------------------------------
# Guards on the collapsed design.
#
# Each of these encodes something the v1 studio actually did wrong, so that a
# future edit cannot quietly reintroduce it.
# ---------------------------------------------------------------------------
def test_the_record_has_exactly_one_date_control(html):
    """v1 carried two identical date widgets: one for the document, one for the
    event that copied it. Twelve controls for one date."""
    assert html.count('id="doc-date"') == 1
    for retired in ("meta-date-edtf", "event-date-edtf", "meta-date-picker",
                    "event-date-picker", "meta-date-precision", "event-date-precision",
                    "meta-date-approx", "event-date-approx"):
        assert retired not in html, f"the duplicated date widget is back: {retired}"


def test_there_is_one_way_to_finish_a_document(html, js):
    """v1 had Save Draft, Verify & Save, Reject and an Auto-Save toggle."""
    assert 'id="btn-done"' in html
    for retired in ("btn-save-draft", "btn-approve", "btn-reject", "toggle-autosave"):
        assert retired not in html, f"the three-button save is back: {retired}"
        assert retired not in js, f"the three-button save is back in js: {retired}"


def test_review_state_is_not_a_status_string(js, html):
    """`verification_status` with pending/verified/rejected is gone from the UI."""
    for token in ("verification_status", '"verified"', '"rejected"'):
        assert token not in js, f"studio.js still speaks the v1 status ladder: {token}"
    assert "verification_status" not in html


def test_the_editor_has_no_tabs(html, js):
    """Three tabs hid two thirds of the record behind clicks."""
    for retired in ("tab-btn", "tab-content", "editor-tabs-header"):
        assert retired not in html, f"the tab bar is back: {retired}"


def test_the_projection_toggle_is_gone(html):
    """Projection is a consequence of the fields, not a second form behind a switch."""
    for retired in ("check-project-event", "projection-fields", "event-title",
                    "event-category-select", "event-desc", "event-location"):
        assert retired not in html, f"the duplicate event form is back: {retired}"
    assert 'id="projection-text"' in html, "the projection readout should remain"


def test_there_is_one_ingest_endpoint(js):
    """v1 called three endpoints that all reached the same function."""
    for retired in ("/api/fs/quick-index", "/api/fs/batch-quick-index", "/api/fs/ingest"):
        assert retired not in js, f"studio.js still calls the retired endpoint {retired}"
    assert '"/api/documents"' in js


def test_ingest_does_not_guess_a_document_kind(js):
    """Every ingest call in v1 hardcoded file_type: "photograph", so every
    document in the archive claimed to be a photo and skipped its own OCR."""
    assert 'file_type: "photograph"' not in js
    assert '"file_type"' not in js


def test_the_kind_vocabulary_is_served_not_hardcoded(js):
    """v1 kept three disagreeing copies of the type list in JavaScript."""
    assert '"/api/kinds"' in js
    # The OCR-skip heuristic reads `textual` from the server rather than a
    # local list that named values the dropdown could not produce.
    assert '["photograph", "portrait", "map", "artifact"]' not in js
    assert "meta-category-select" not in js


def test_the_phantom_category_modal_is_gone(html, js):
    """It pushed to a JS array and persisted nothing; a category you created
    but did not immediately apply vanished on reload."""
    for retired in ("modal-new-category", "new-category-name", "btn-save-new-cat",
                    "btn-create-category-modal"):
        assert retired not in html, f"the phantom category modal is back: {retired}"
        assert retired not in js


def test_one_date_reader_serves_both_editors(html):
    """The studio and the timeline inspector must read "c. 1912" the same way,
    so there is exactly one parser: dates.js, loaded before studio.js here and
    inlined into the compiled timeline by the compiler."""
    dates = (STATIC / "dates.js").read_text(encoding="utf-8")
    assert "function parseHistoricalDate" in dates
    assert "function parseHistoricalDate" not in JS.read_text(encoding="utf-8"), \
        "studio.js has its own copy of the date parser again"
    assert html.index("/static/dates.js") < html.index("/static/studio.js")


def test_dates_are_parsed_before_they_are_sent(js):
    """The browser converts human input to EDTF; the server refuses anything
    else rather than rounding it to 1900."""
    assert "function parseHistoricalDate" in js
    assert "function currentDateEdtf" in js
    assert "currentDateEdtf()" in js, "the save payload must send parsed EDTF"


def test_a_whole_folder_is_added_through_the_wizard(js, html):
    """"Select all" + "Index selected" only ever saw the 60 cards loaded so far;
    the wizard plans the whole folder on the server, then imports in the background."""
    assert 'id="btn-import-folder"' in html and 'id="modal-import"' in html
    assert '"/api/import/plan"' in js and '"/api/import/start"' in js
    review = _function_body(js, "renderImportReview")
    # Copies are listed by name and left unticked; new scans are ticked.
    assert "plan.duplicates.map(item => row(item, false" in review
    assert "if (!g) return row(item, true);" in review


def test_pages_of_one_item_are_kept_together(js, html):
    """"14600", "14600 1", "14600 2" are one item: one row in the wizard, one record
    in the archive, a page strip in the studio, and each page's text its own."""
    review = _function_body(js, "renderImportReview")
    assert "importWizard.combine" in review and "!g.counter" in review, \
        "named pages combine by default; a scanner's own numbering does not"
    assert "groups: pickedImportGroups()" in js
    save = _function_body(js, "saveRecord")
    assert "delete payload.fields.transcription" in save, "a later page's text must not overwrite the item's"
    go = _function_body(js, "goToPage")
    assert go.index("flushPendingSave") < go.index("currentPage = next")
    assert 'id="page-strip"' in html and 'id="btn-split-page"' in html and 'id="btn-run-ocr-all"' in html
    banner = _function_body(js, "renderPagesBanner")
    assert "!p.counter" in banner and "banner.hidden = true" in banner, \
        "the notice appears only while named pages are left to combine"


def test_undecided_scans_lead_to_review(js):
    body = _function_body(js, "getFilteredDatabaseDocs")
    assert 'catalogReviewFilter === "todo"' in body and "isUndecided" in body
