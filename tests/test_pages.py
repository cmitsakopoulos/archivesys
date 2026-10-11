"""Items scanned as several files: "12345", "12345 1", "12345 2" are one item."""

from pathlib import Path

import pytest
from PIL import Image

from src.database.db_manager import DatabaseManager
from src.ingestion.pages import group_pages, page_of


# =============================================================================
# The naming rule
# =============================================================================
def test_a_space_and_a_number_is_a_page():
    assert page_of("/box/12345.png") == ("/box", "12345", 0)
    assert page_of("/box/12345 1.png") == ("/box", "12345", 1)
    assert page_of("/box/Passeport 2.jpg") == ("/box", "Passeport", 2)


def test_a_year_is_not_a_page_number():
    assert page_of("/box/wedding 1952.png") == ("/box", "wedding 1952", 0)
    assert page_of("/box/grades_1980_1981.png")[2] == 0


def test_pages_group_in_order_within_one_folder():
    groups = group_pages(["/a/14600 2.png", "/a/14600.png", "/a/14600 10.png", "/a/14600 1.png",
                          "/a/other.png", "/b/14600 1.png"])
    assert len(groups) == 1
    assert groups[0]["paths"] == ["/a/14600.png", "/a/14600 1.png", "/a/14600 2.png", "/a/14600 10.png"]
    assert groups[0]["counter"] is False


def test_the_scanners_own_counter_is_marked():
    (group,) = group_pages(["/p/Scan.png", "/p/Scan 1.png", "/p/Scan 2.png"])
    assert group["counter"] is True


# =============================================================================
# Pages in the archive
# =============================================================================
@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(tmp_path / "archive.db")
    manager.init_db()
    return manager


def _scan(folder: Path, name: str, shade: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    Image.new("RGB", (60, 80), (shade, shade, shade)).save(path)
    return path


def _item(db, tmp_path, n=3):
    ids = [db.index_scan(_scan(tmp_path / "box", "14600.png" if i == 0 else f"14600 {i}.png", 40 + i * 30),
                         kind="document") for i in range(n)]
    return ids


def test_combined_pages_are_one_item(db, tmp_path):
    lead, p1, p2 = _item(db, tmp_path)
    db.update_document(p1, fields={"transcription": "second page words"})
    db.combine_pages(lead, [p1, p2])

    listed = db.list_documents()
    assert [d["id"] for d in listed] == [lead] and listed[0]["page_count"] == 3
    full = db.get_document_full(lead)
    assert [p["id"] for p in full["pages"]] == [lead, p1, p2]
    assert [p["page_no"] for p in full["pages"]] == [0, 1, 2]
    assert full["pages"][1]["transcription"] == "second page words"


def test_nothing_described_on_a_page_is_lost(db, tmp_path):
    lead, p1, p2 = _item(db, tmp_path)
    person = db.add_person("Eleni", "Theodorou")
    db.update_document(p1, fields={"title": "Birth certificate", "edtf_date": "1923-05-14",
                                   "tags": ["birth"], "notes": "Folded in three"},
                       people_links=[{"id": person, "role": "subject"}], reviewed=True)
    db.combine_pages(lead, [p1, p2])

    item = db.get_document_full(lead)
    assert item["title"] == "Birth certificate" and item["edtf_date"] == "1923-05-14"
    assert item["tags"] == ["birth"] and item["notes"] == "Folded in three"
    assert [p["id"] for p in item["people"]] == [person]
    assert not item["reviewed"], "two of its three records were never reviewed"


def test_a_page_is_found_through_its_item(db, tmp_path):
    lead, p1, p2 = _item(db, tmp_path)
    db.combine_pages(lead, [p1, p2])
    db.update_document(p2, fields={"transcription": "Σμύρνη"})
    hits = db.search_documents("σμυρνη")
    assert [h["id"] for h in hits] == [lead]


def test_a_page_takes_only_its_own_text(db, tmp_path):
    lead, p1 = _item(db, tmp_path, 2)
    db.combine_pages(lead, [p1])
    db.update_document(p1, fields={"title": "Not the item's title", "transcription": "page text"},
                       reviewed=True)
    page = db.get_document(p1)
    assert page["transcription"] == "page text"
    assert page["title"] == "14600 1" and not page["reviewed"]


def test_splitting_a_page_off_makes_it_a_record_again(db, tmp_path):
    lead, p1, p2 = _item(db, tmp_path)
    db.combine_pages(lead, [p1, p2])
    db.split_page(p1)

    assert sorted(d["id"] for d in db.list_documents()) == [lead, p1]
    item = db.get_document_full(lead)
    assert [(p["id"], p["page_no"]) for p in item["pages"]] == [(lead, 0), (p2, 1)]


def test_deleting_an_item_removes_its_pages_but_never_the_files(db, tmp_path):
    lead, p1, p2 = _item(db, tmp_path)
    db.combine_pages(lead, [p1, p2])
    db.delete_document(lead)
    assert db.list_documents() == [] and db.get_document(p1) is None
    assert (tmp_path / "box" / "14600 1.png").exists()


def test_a_missing_first_page_hands_the_item_to_the_next(db, tmp_path):
    lead, p1, p2 = _item(db, tmp_path)
    db.update_document(lead, fields={"title": "Deed", "edtf_date": "1930"})
    db.combine_pages(lead, [p1, p2])
    (tmp_path / "box" / "14600.png").unlink()

    db.purge_missing_documents()
    (item,) = db.list_documents()
    assert item["id"] == p1 and item["title"] == "Deed" and item["page_count"] == 2


def test_proposals_find_split_items_and_skip_combined_ones(db, tmp_path):
    lead, p1, p2 = _item(db, tmp_path)
    for i in range(3):
        db.index_scan(_scan(tmp_path / "pictures", "Scan.png" if i == 0 else f"Scan {i}.png", 90 + i * 20),
                      kind="document")
    db.index_scan(_scan(tmp_path / "box", "letter.png", 200), kind="letter")

    proposals = {p["name"]: p for p in db.page_proposals()}
    assert set(proposals) == {"14600", "Scan"}
    assert proposals["14600"]["doc_ids"] == [lead, p1, p2] and not proposals["14600"]["counter"]
    assert proposals["Scan"]["counter"]

    db.combine_pages(lead, [p1, p2])
    assert {p["name"] for p in db.page_proposals()} == {"Scan"}


def test_the_timeline_shows_an_item_once_with_all_its_pages(db, tmp_path):
    lead, p1, p2 = _item(db, tmp_path)
    db.update_document(lead, fields={"title": "Deed", "edtf_date": "1930"})
    db.combine_pages(lead, [p1, p2])
    data = db.get_timeline_export_data()
    assert [e["title"] for e in data["events"]] == ["Deed"]
    (doc,) = data["documents"]
    assert [p["id"] for p in doc["pages"]] == [lead, p1, p2]


def test_an_export_carries_every_page(db, tmp_path):
    import json, re
    from src.visualizer.compiler import DashboardCompiler
    lead, p1, p2 = _item(db, tmp_path)
    db.update_document(lead, fields={"title": "Deed", "edtf_date": "1930"})
    db.combine_pages(lead, [p1, p2])

    out = DashboardCompiler(db, output_dir=tmp_path / "dist", quality="original",
                            preview_cache=tmp_path / "previews").compile()
    docs = json.loads(re.search(r"rawDocuments = (\[\{.*?\}\]);\n", out.read_text(encoding="utf-8"), re.S).group(1))
    (doc,) = docs
    assert len(doc["pages"]) == 3
    for page, name in zip(doc["pages"], ["14600.png", "14600 1.png", "14600 2.png"]):
        assert page["preview_url"].startswith("data:image/webp")
        published = out.parent / page["view_url"]
        assert published.read_bytes() == (tmp_path / "box" / name).read_bytes()

    live = DashboardCompiler(db, output_dir=tmp_path / "dist").render(live=True)
    assert f"/api/thumb?doc_id={p2}" in live
