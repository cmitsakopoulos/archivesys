"""Tests for the static timeline compiler."""

import pytest
from src.database.db_manager import DatabaseManager
from src.visualizer.compiler import DashboardCompiler


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(tmp_path / "test_compile.db")
    manager.init_db()
    return manager


def test_dashboard_compiler(db, tmp_path):
    p_id = db.add_person("Georges", "Theodorou")
    doc_id = db.add_document(
        file_path="scans/passport.png",
        kind="passport",
        language="fr",
        title="Evacuation de Smyrne",
        edtf_date="1922-09-12",
        location="Smyrne",
        transcription="Passeport Consulaire N° 8492 délivré à Smyrne",
    )
    db.link_document_person(doc_id, p_id)
    db.update_document(doc_id, fields={"summary": "Evacuation maritime vers le Piree."}, reviewed=True)

    out_file = DashboardCompiler(db, output_dir=tmp_path / "dist").compile(copy_scans=False)

    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "Timeline Projection" in content
    assert "Evacuation de Smyrne" in content
    assert "triple-vertical-timeline" in content
    assert "inspection-dialog" in content
    assert "rail-years" in content


def test_excluded_documents_do_not_publish(db, tmp_path):
    """v1's filter tested a key the exporter never emitted, so the condition was
    dead and every rejected scan published to the dashboard anyway."""
    keep = db.add_document(
        file_path="scans/keep.png", title="Kept record", edtf_date="1930",
        transcription="KEEP_THIS_MARKER",
    )
    drop = db.add_document(
        file_path="scans/drop.png", title="Blurred duplicate", edtf_date="1931",
        transcription="DROP_THIS_MARKER",
    )
    db.update_document(drop, fields={"excluded": True})
    assert keep and drop

    out_file = DashboardCompiler(db, output_dir=tmp_path / "dist").compile(copy_scans=False)
    content = out_file.read_text(encoding="utf-8")

    assert "Kept record" in content
    assert "Blurred duplicate" not in content
    assert "DROP_THIS_MARKER" not in content


def test_undated_documents_do_not_publish(db, tmp_path):
    """v1 fabricated a date from created_at, so undated scans landed on the
    timeline at their import date -- which for this archive meant 2026."""
    db.add_document(file_path="scans/undated.png", title="No date here",
                    transcription="UNDATED_MARKER")
    db.add_document(file_path="scans/dated.png", title="Has a date", edtf_date="1930")

    DashboardCompiler(db, output_dir=tmp_path / "dist").compile(copy_scans=False)

    entries = db.get_timeline_export_data()["events"]
    assert [e["title"] for e in entries] == ["Has a date"]
    # The undated scan is still in the archive and still in the deep inspector,
    # it simply has no place on a timeline.
    assert any(d["title"] == "No date here" for d in db.get_timeline_export_data()["documents"])


def test_a_cluster_replaces_its_member_documents(db, tmp_path):
    a = db.add_document(file_path="scans/wed_a.png", title="Wedding photo", edtf_date="1935-06")
    b = db.add_document(file_path="scans/wed_b.png", title="Wedding certificate", edtf_date="1935-06")
    db.add_event(title="The wedding day", edtf_date="1935-06", source_doc_ids=[a, b])

    out_file = DashboardCompiler(db, output_dir=tmp_path / "dist").compile(copy_scans=False)
    content = out_file.read_text(encoding="utf-8")

    # One card for the occurrence; the member scans are attached to it, not
    # competing with it for a place on the timeline.
    assert "The wedding day" in content
    assert content.count('"source": "cluster"') == 1


def test_render_is_the_page_without_writing_it(db, tmp_path):
    """The studio's preview renders on every visit; writing each one over
    dist/index.html raced whatever was reading that file."""
    db.add_document(file_path="scans/letter.png", title="Letter from Smyrna", edtf_date="1922")
    compiler = DashboardCompiler(db, output_dir=tmp_path / "dist")

    page = compiler.render(copy_scans=False)

    assert "Letter from Smyrna" in page
    assert not (tmp_path / "dist" / "index.html").exists()


def test_compile_swaps_the_page_in_whole(db, tmp_path):
    """Written beside itself and renamed into place, so a reader sees the old
    page or the new one; nothing half-written, and no temporary files left."""
    compiler = DashboardCompiler(db, output_dir=tmp_path / "dist")
    first = compiler.compile(copy_scans=False)
    db.add_document(file_path="scans/letter.png", title="Letter from Smyrna", edtf_date="1922")
    second = compiler.compile(copy_scans=False)

    assert first == second and "Letter from Smyrna" in second.read_text(encoding="utf-8")
    assert oct(second.stat().st_mode & 0o777) == oct(0o644)
    assert not [p.name for p in second.parent.iterdir() if p.name.endswith(".tmp")]


# =============================================================================
# Scans in an export: full quality by default, smaller only by choice
# =============================================================================
import base64
import io
from pathlib import Path
from PIL import Image

import src.visualizer.compiler as compiler_module


def _scan(path: Path, colour, size=(1800, 2400)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, colour)
    for x in range(0, size[0], 90):          # something for a compressor to chew on
        img.paste((20, 20, 20), (x, 0, x + 3, size[1]))
    img.save(path)
    return path


def _dated(db, path: Path, title: str) -> int:
    return db.index_scan(path, kind="document") and db.update_document(
        db.get_document_by_path(path)["id"], fields={"title": title, "edtf_date": "1930"})["id"]


def _exported(out_file: Path):
    import json, re
    page = out_file.read_text(encoding="utf-8") if isinstance(out_file, Path) else out_file
    docs = json.loads(re.search(r"rawDocuments = (\[\{.*?\}\]);\n", page, re.S).group(1))
    return {d["title"]: d for d in docs}


def test_an_export_publishes_every_original_and_the_inspector_opens_it(db, tmp_path):
    """The export copied the originals beside the page, but the inspector showed
    only the 600 px preview carried in the page."""
    master = _scan(tmp_path / "box" / "letter.png", (230, 220, 200))
    _dated(db, master, "Letter")

    out = DashboardCompiler(db, output_dir=tmp_path / "dist", quality="original").compile()
    doc = _exported(out)["Letter"]

    published = out.parent / doc["view_url"]
    assert doc["view_url"] == doc["file_path"] and doc["view_url"].startswith("assets/scans/")
    assert published.read_bytes() == master.read_bytes(), "the original, byte for byte"
    page = (out.parent / "index.html").read_text(encoding="utf-8")
    assert "doc.view_url" in page


def test_two_scans_with_one_name_publish_as_two_files(db, tmp_path):
    """Two "Scan 1.png" from different folders were one file in the export, so the
    page showed the same scan for both records."""
    a = _scan(tmp_path / "box a" / "Scan 1.png", (240, 230, 210))
    b = _scan(tmp_path / "box b" / "Scan 1.png", (90, 70, 50))
    _dated(db, a, "First")
    _dated(db, b, "Second")

    out = DashboardCompiler(db, output_dir=tmp_path / "dist", quality="original").compile()
    docs = _exported(out)

    assert docs["First"]["view_url"] != docs["Second"]["view_url"]
    assert (out.parent / docs["First"]["view_url"]).read_bytes() == a.read_bytes()
    assert (out.parent / docs["Second"]["view_url"]).read_bytes() == b.read_bytes()


def test_a_tiff_gets_a_full_resolution_copy_a_browser_can_draw(db, tmp_path):
    master = tmp_path / "box" / "deed.tif"
    _scan(tmp_path / "box" / "deed.png", (220, 210, 190)).rename(master.with_suffix(".png"))
    Image.open(master.with_suffix(".png")).save(master)
    _dated(db, master, "Deed")

    out = DashboardCompiler(db, output_dir=tmp_path / "dist", quality="original").compile()
    doc = _exported(out)["Deed"]

    assert (out.parent / doc["file_path"]).read_bytes() == master.read_bytes()
    assert doc["view_url"].endswith(".view.jpg")
    assert Image.open(out.parent / doc["view_url"]).size == Image.open(master).size


def test_high_keeps_every_pixel_and_compact_shrinks(db, tmp_path):
    master = _scan(tmp_path / "box" / "photo.png", (200, 190, 170), size=(3000, 4000))
    _dated(db, master, "Photo")
    dist = tmp_path / "dist"

    high = _exported(DashboardCompiler(db, output_dir=dist, quality="high").compile())["Photo"]
    assert high["view_url"].endswith(".full.jpg")
    assert Image.open(dist / high["view_url"]).size == (3000, 4000)

    compact = _exported(DashboardCompiler(db, output_dir=dist, quality="compact").compile())["Photo"]
    assert Image.open(dist / compact["view_url"]).size == (1800, 2400)
    preview = Image.open(io.BytesIO(base64.b64decode(compact["preview_url"].split(",", 1)[1])))
    assert max(preview.size) == 600
    # The high-quality copy was the old choice: it is gone, not left behind.
    assert not (dist / high["view_url"]).exists()


def test_the_page_carries_previews_sharp_enough_to_read(db, tmp_path):
    _dated(db, _scan(tmp_path / "box" / "page.png", (235, 225, 205)), "Page")
    out = DashboardCompiler(db, output_dir=tmp_path / "dist", quality="original").compile()
    data = _exported(out)["Page"]["preview_url"].split(",", 1)[1]
    assert max(Image.open(io.BytesIO(base64.b64decode(data))).size) == 1000


def test_previews_are_made_once(db, tmp_path, monkeypatch):
    """Every visit to the studio's timeline re-encoded every scan: 150 s for 416."""
    _dated(db, _scan(tmp_path / "box" / "page.png", (235, 225, 205)), "Page")
    compiler = DashboardCompiler(db, output_dir=tmp_path / "dist", quality="original",
                                 preview_cache=tmp_path / "previews")
    first = compiler.render()

    opened = []
    real_open = compiler_module._open_page
    monkeypatch.setattr(compiler_module, "_open_page", lambda *a, **k: opened.append(a) or real_open(*a, **k))
    second = compiler.render()

    assert first == second and not opened, "an unchanged scan should come from the cache"
    assert len(list((tmp_path / "previews").iterdir())) == 1


def test_the_studio_preview_points_at_thumbnails_instead_of_carrying_scans(db, tmp_path):
    _dated(db, _scan(tmp_path / "box" / "page.png", (235, 225, 205)), "Page")
    page = DashboardCompiler(db, output_dir=tmp_path / "dist").render(live=True)
    assert "/api/thumb?doc_id=" in page
    assert "data:image/webp" not in page


def test_the_default_is_original_and_unknown_choices_fall_back_to_it(db, tmp_path):
    from src import config
    assert config.PipelineSettings().export_quality == "original"
    assert config.PipelineSettings(export_quality="tiny").clamped().export_quality == "original"
    assert DashboardCompiler(db, output_dir=tmp_path / "dist", quality="tiny").quality == "original"
