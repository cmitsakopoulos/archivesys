"""Tests for the collapsed archive model: one document, one timeline entry."""

# pyrefly: ignore [missing-import]
import pytest
from pathlib import Path
from src.database.db_manager import DatabaseManager, normalize_kind, is_textual_kind
from src.database.edtf_parser import EDTFParser, NotADate


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test_archive.db"
    manager = DatabaseManager(db_path)
    manager.init_db()
    return manager


def test_add_person_and_document(db):
    p_id = db.add_person("Γεώργιος", "Παπαδόπουλος")
    assert p_id > 0

    doc_id = db.add_document(
        file_path="scans/test.png",
        kind="certificate",
        language="el-poly",
        transcription="Βασίλειον τῆς Ἑλλάδος, Ἀθῆναι 1923.",
    )
    assert doc_id > 0

    db.link_document_person(doc_id, p_id, role="subject")

    doc = db.get_document(doc_id)
    assert doc["reviewed"] is False
    assert doc["excluded"] is False
    assert "Βασίλειον" in doc["transcription"]


def test_fts5_triggers_and_search(db):
    doc_id = db.add_document(
        file_path="scans/doc_poly.png",
        transcription="«Ἐν τῷ κήπῳ τοῦ οἴκου ἡμῶν, ὁ λόγος ἦν σαφής»",
    )

    matches = db.search_documents("λογος")
    assert len(matches) == 1
    assert matches[0]["id"] == doc_id

    db.update_document(
        doc_id,
        fields={
            "transcription": "Δημαρχία Ἀθηναίων, Πιστοποιητικὸν Γάμου.",
            "language": "el-poly",
            "kind": "certificate",
        },
        reviewed=True,
    )

    matches_after = db.search_documents("πιστοποιητικον")
    assert len(matches_after) == 1
    assert matches_after[0]["id"] == doc_id


# ---------------------------------------------------------------------------
# The taxonomy: one closed axis, with legacy vocabularies folded onto it
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("legacy,expected", [
    ("magazine", "press"),
    ("newspaper", "press"),
    ("manifest", "document"),
    ("portrait", "photograph"),
    ("map", "document"),
    ("artifact", "document"),
    ("correspondence", "letter"),
    ("life_event", "document"),
    ("certificate", "certificate"),
    ("something nobody has ever typed", "document"),
    (None, "document"),
])
def test_legacy_types_fold_onto_the_seven_kinds(legacy, expected):
    assert normalize_kind(legacy) == expected


def test_only_photographs_skip_ocr():
    assert is_textual_kind("certificate") is True
    assert is_textual_kind("letter") is True
    assert is_textual_kind("press") is True
    assert is_textual_kind("photograph") is False
    # The v1 studio keyed this off values its own dropdown could not produce.
    assert is_textual_kind("portrait") is False


def test_kinds_are_a_closed_list(db):
    keys = [k["key"] for k in db.list_kinds()]
    assert keys == ["certificate", "letter", "photograph", "passport", "press", "book", "document"]


# ---------------------------------------------------------------------------
# A document IS a timeline entry
# ---------------------------------------------------------------------------
def test_a_dated_document_is_a_timeline_entry(db):
    p_id = db.add_person("Κωνσταντῖνος", "Θεοδώρου")
    doc_id = db.add_document(
        file_path="scans/undated_doc.png",
        transcription="Βεβαίωσις ἐγγραφῆς",
        title="Βεβαίωσις 1928",
        edtf_date="1928-06-15",
        location="Πάτραι",
    )
    db.link_document_person(doc_id, p_id)

    export = db.get_timeline_export_data()
    assert len(export["events"]) == 1
    entry = export["events"][0]
    assert entry["source"] == "document"
    assert entry["title"] == "Βεβαίωσις 1928"
    assert entry["start"] == "1928-06-15"
    assert entry["location"] == "Πάτραι"
    # v1 joined event_participants, which the studio never wrote, so every
    # studio-made entry reached the timeline with nobody on it.
    assert entry["people"][0]["name"] == "Κωνσταντῖνος Θεοδώρου"


def test_an_undated_document_stays_off_the_timeline(db):
    db.add_document(file_path="scans/no_date.png", transcription="No date anywhere")
    export = db.get_timeline_export_data()
    assert export["events"] == []
    # v1 fabricated a date from created_at, so this scan published at import time.


def test_an_excluded_document_stays_off_the_timeline(db):
    doc_id = db.add_document(
        file_path="scans/blurred.png", title="Blurred duplicate", edtf_date="1928",
    )
    assert len(db.get_timeline_export_data()["events"]) == 1

    db.update_document(doc_id, fields={"excluded": True})
    assert db.get_timeline_export_data()["events"] == []
    # ...and it is still in the archive, and still searchable.
    assert db.get_document(doc_id) is not None


def test_editing_a_document_moves_its_timeline_entry(db):
    """The point of the collapse: there is no second record to fall out of step."""
    doc_id = db.add_document(file_path="scans/x.png", title="First guess", edtf_date="1930")
    db.update_document(doc_id, fields={"title": "Corrected title", "edtf_date": "1912-05"})

    entry = db.get_timeline_export_data()["events"][0]
    assert entry["title"] == "Corrected title"
    assert entry["start"] == "1912-05-01"


# ---------------------------------------------------------------------------
# Clusters: the only thing a single document cannot express
# ---------------------------------------------------------------------------
def test_a_one_document_event_is_just_that_document(db):
    p_id = db.add_person("Χρῆστος", "Παπαδόπουλος")
    doc_id = db.add_document(file_path="scans/birth.png", transcription="Πιστοποιητικὸν Γεννήσεως")

    returned = db.add_event(
        title="Γέννησις Χρήστου",
        edtf_date="1923-05-14",
        category="life_event",
        location="Ἀθῆναι",
        source_doc_ids=[doc_id],
        participant_ids=[p_id],
    )
    # It describes the document instead of shadowing it.
    assert returned == doc_id
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

    doc = db.get_document_full(doc_id)
    assert doc["title"] == "Γέννησις Χρήστου"
    assert doc["edtf_date"] == "1923-05-14"
    assert doc["location"] == "Ἀθῆναι"
    assert "life event" in doc["tags"]   # the old category, kept as a subject

    export = db.get_timeline_export_data()
    assert len(export["events"]) == 1
    assert export["events"][0]["people"][0]["name"] == "Χρῆστος Παπαδόπουλος"


def test_a_multi_document_event_survives_as_a_cluster(db, tmp_path):
    a = db.add_document(file_path="scans/wed_a.png", title="Wedding photo", edtf_date="1935-06")
    b = db.add_document(file_path="scans/wed_b.png", title="Wedding certificate", edtf_date="1935-06")
    p = db.add_person("Maria", "Kostas")
    db.link_document_person(a, p)

    ev = db.add_event(
        title="The wedding day", edtf_date="1935-06",
        source_doc_ids=[a, b], participant_ids=[],
    )
    assert ev > 0

    export = db.get_timeline_export_data()
    # One card for the occurrence, not three.
    assert len(export["events"]) == 1
    entry = export["events"][0]
    assert entry["source"] == "cluster"
    assert entry["title"] == "The wedding day"
    assert {d["id"] for d in entry["docs"]} == {a, b}
    # Participants come from the member documents, not a separate table.
    assert entry["people"][0]["name"] == "Maria Kostas"


def test_a_sourceless_cluster_survives(db):
    """A birth remembered by the family, evidenced by no scan at all."""
    p = db.add_person("Eleni", "Theodorou")
    ev = db.add_event(title="Born in Smyrna", edtf_date="1898", participant_ids=[p])
    assert ev > 0
    export = db.get_timeline_export_data()
    assert len(export["events"]) == 1
    assert export["events"][0]["docs"] == []


# ---------------------------------------------------------------------------
# Dates are refused, never invented
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["May 1923", "14/5/1923", "unknown", "n.d.", "abc", "1923-13", "1923-05-99"])
def test_an_unreadable_date_is_refused_not_rounded_to_1900(db, bad):
    doc_id = db.add_document(file_path="scans/d.png")
    with pytest.raises(NotADate):
        db.update_document(doc_id, fields={"edtf_date": bad})
    # And the document is left honestly undated rather than dated 1900.
    assert db.get_document(doc_id)["edtf_date"] is None


@pytest.mark.parametrize("good,expect_start", [
    ("1923", "1923-01-01"),
    ("1923-05", "1923-05-01"),
    ("1923-05-14", "1923-05-14"),
    ("1912~", "1912-01-01"),
    ("192X", "1920-01-01"),
    ("1912/1915", "1912-01-01"),
])
def test_real_edtf_is_accepted(db, good, expect_start):
    doc_id = db.add_document(file_path=f"scans/{good.replace('/', '_')}.png")
    db.update_document(doc_id, fields={"edtf_date": good})
    assert db.get_document(doc_id)["edtf_date"] == good
    assert EDTFParser.parse(good).iso_start == expect_start


def test_clearing_a_date_is_allowed(db):
    doc_id = db.add_document(file_path="scans/c.png", edtf_date="1923")
    db.update_document(doc_id, fields={"edtf_date": ""})
    doc = db.get_document(doc_id)
    assert doc["edtf_date"] is None
    assert doc["sort_start"] is None


# ---------------------------------------------------------------------------
# Review state: a timestamp and a flag, not a ladder of strings
# ---------------------------------------------------------------------------
def test_review_is_a_timestamp_not_a_status(db):
    doc_id = db.add_document(file_path="scans/r.png")
    assert db.get_document(doc_id)["reviewed"] is False

    db.update_document(doc_id, fields={}, reviewed=True, reviewed_by="tester")
    doc = db.get_document(doc_id)
    assert doc["reviewed"] is True
    assert doc["reviewed_at"] is not None
    assert doc["reviewed_by"] == "tester"

    db.update_document(doc_id, fields={}, reviewed=False)
    assert db.get_document(doc_id)["reviewed"] is False


def test_listing_by_review_state(db):
    a = db.add_document(file_path="scans/a.png")
    db.add_document(file_path="scans/b.png")
    db.update_document(a, fields={}, reviewed=True)

    assert [d["id"] for d in db.list_documents(reviewed=True)] == [a]
    assert len(db.list_documents(reviewed=False)) == 1
    assert len(db.list_documents()) == 2


def test_ocr_does_not_mark_a_document_reviewed(db):
    doc_id = db.add_document(file_path="scans/o.png")
    db.update_document_ocr(doc_id, transcription="machine text", language="el-poly")
    assert db.get_document(doc_id)["reviewed"] is False


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
def test_purge_missing_documents(db, tmp_path):
    real_file = tmp_path / "real.png"
    real_file.write_bytes(b"image data")
    real_id = db.add_document(file_path=str(real_file), title="Real", edtf_date="1930")
    fake_id = db.add_document(file_path=str(tmp_path / "phantom.png"), title="Ghost", edtf_date="1931")

    res = db.purge_missing_documents()
    assert res["purged_documents"] == 1

    assert db.get_document(real_id) is not None
    assert db.get_document(fake_id) is None

    export = db.get_timeline_export_data()
    assert len(export["documents"]) == 1
    assert len(export["events"]) == 1
    assert export["events"][0]["id"] == real_id


def test_delete_document_prunes_its_cluster(db, tmp_path):
    a = db.add_document(file_path=str(tmp_path / "a.png"), edtf_date="1940")
    b = db.add_document(file_path=str(tmp_path / "b.png"), edtf_date="1940")
    db.add_event(title="Cluster", edtf_date="1940", source_doc_ids=[a, b])

    assert db.delete_document(a) is True
    assert db.delete_document(b) is True
    assert db.get_timeline_export_data()["events"] == []


def test_update_document_writes_every_field_once(db, tmp_path):
    doc_path = tmp_path / "meta_test.png"
    doc_path.write_bytes(b"scan content")
    p1 = db.add_person("Konstantinos", "Theodorou")
    doc_id = db.add_document(file_path=str(doc_path), kind="document", transcription="Original OCR text")

    updated = db.update_document(
        doc_id,
        fields={
            "title": "Historical Royal Decree",
            "edtf_date": "1912-05",
            "location": "Thessaloniki",
            "creator": "Prefecture Office",
            "tags": ["Balkan Wars", "Decree"],
            "notes": "Original archive copy preserved in good condition.",
            "summary": "Issuance of administrative protocol after liberation.",
            "transcription": "Ἐν Θεσσαλονίκῃ, τῇ 5ῃ Μαΐου 1912",
            "language": "el-poly",
            "kind": "document",
        },
        people_links=[{"id": p1, "role": "recipient"}],
        reviewed=True,
    )

    assert updated["title"] == "Historical Royal Decree"
    assert updated["location"] == "Thessaloniki"
    assert "Decree" in updated["tags"]
    assert updated["language"] == "el-poly"
    assert updated["reviewed"] is True
    assert updated["people"][0]["role"] == "recipient"
    # The timeline reads these very columns -- there is no second copy.
    assert updated["clusters"] == []
    entry = db.get_timeline_export_data()["events"][0]
    assert entry["title"] == "Historical Royal Decree"
    assert entry["description"] == "Issuance of administrative protocol after liberation."


def test_get_document_by_path_resolution(db, tmp_path):
    test_file = tmp_path / "1923_birth_cert.png"
    test_file.write_bytes(b"image")

    rel_path = f"tmp_scans/{test_file.name}"
    doc_id = db.add_document(file_path=rel_path, kind="certificate", transcription="Sample test transcription")

    assert db.get_document_by_path(rel_path)["id"] == doc_id
    assert db.get_document_by_path(Path(rel_path).resolve())["id"] == doc_id
    assert db.get_document_by_path("nonexistent_file_9999.png") is None
    # The same name in another folder is another file. Matching on the name
    # made a second "Scan 1.png" count as indexed, so it was never added.
    assert db.get_document_by_path(test_file) is None


def test_a_second_scan_with_the_same_name_is_added(db, tmp_path):
    from PIL import Image
    for box, colour in (("box1", (200, 30, 30)), ("box2", (30, 30, 200))):
        (tmp_path / box).mkdir()
        Image.new("RGB", (60, 60), colour).save(tmp_path / box / "Scan 1.png")
    first = db.index_scan(tmp_path / "box1" / "Scan 1.png")
    second = db.index_scan(tmp_path / "box2" / "Scan 1.png")
    assert first != second
    assert len(db.list_documents()) == 2


def test_sha256_fingerprint_and_lookup(db, tmp_path):
    import hashlib
    scan_file = tmp_path / "1923_birth_certificate.png"
    sample_content = b"ARCHIVAL_SCAN_BINARY_BYTES_1923"
    scan_file.write_bytes(sample_content)
    expected_sha = hashlib.sha256(sample_content).hexdigest()

    doc_id = db.add_document(
        file_path=str(scan_file), kind="certificate", transcription="Πιστοποιητικὸν Γεννήσεως",
    )
    doc = db.get_document(doc_id)
    assert doc["sha256"] == expected_sha
    assert doc["file_size"] == len(sample_content)

    found = db.get_document_by_sha256(expected_sha)
    assert found["id"] == doc_id
    assert found["transcription"] == "Πιστοποιητικὸν Γεννήσεως"

    renamed_file = tmp_path / "renamed_arbitrary_filename.png"
    scan_file.rename(renamed_file)
    assert hashlib.sha256(renamed_file.read_bytes()).hexdigest() == expected_sha


def test_update_document_ocr(db):
    doc_id = db.add_document(
        file_path="scans/ocr_test.png", transcription="Old initial draft text",
        language="el", confidence_score=40.0,
    )

    db.update_document_ocr(
        doc_id=doc_id,
        transcription="Νέον ἐξευγενισμένον κείμενον ἐκ τοῦ ἀρχείου",
        language="el-poly",
        confidence_score=92.5,
        ocr_metadata={"lines": 14, "refined_lines": 3, "deskew_angle": 0.42},
    )

    doc = db.get_document_full(doc_id)
    assert doc["transcription"] == "Νέον ἐξευγενισμένον κείμενον ἐκ τοῦ ἀρχείου"
    assert doc["language"] == "el-poly"
    assert doc["confidence_score"] == 92.5
    assert doc["metadata"]["lines"] == 14

    results = db.search_documents("εξευγενισμενον")
    assert results[0]["id"] == doc_id


def test_index_scan_is_idempotent_and_does_not_guess(db, tmp_path):
    from PIL import Image
    img_path = tmp_path / "family_portrait_corfu_1932.jpg"
    Image.new("RGB", (200, 300), color=(120, 100, 80)).save(img_path)

    doc_id = db.index_scan(img_path, kind="photograph")
    assert doc_id > 0

    doc = db.get_document_full(doc_id)
    assert doc["kind"] == "photograph"
    assert doc["transcription"] is None
    assert doc["reviewed"] is False
    assert doc["sha256"] is not None
    assert doc["metadata"]["dimensions"] == {"width": 200, "height": 300}

    assert db.index_scan(img_path) == doc_id

    # v1's ingest hardcoded "photograph" for every file, so everything that
    # entered the archive claimed to be a photo and skipped its own OCR. Now
    # nothing is a photograph without evidence: a page the detector cannot
    # place is filed as a document, and says it was undecided.
    other = tmp_path / "deed.jpg"
    Image.new("RGB", (300, 400), (245, 242, 235)).save(other)
    deed = db.get_document(db.index_scan(other))
    assert deed["kind"] == "document"
    assert deed["metadata"]["sorting"]["verdict"] == "unsure"


def test_search_covers_metadata_tags_and_people(db):
    doc_id = db.add_document(
        file_path="scans/athens_olympia_trip_1928.png",
        kind="photograph",
        transcription=None,
        title="Trip to Ancient Olympia",
        location="Olympia",
    )
    p = db.add_person("Eleni", "Papadopoulou")
    db.update_document(doc_id, fields={"tags": ["olympia", "vacation"]},
                       people_links=[{"id": p, "role": "subject"}])

    for term in ("athens", "ancient", "vacation", "Papadopoulou"):
        assert any(d["id"] == doc_id for d in db.search_documents(term)), term


def test_tags_are_counted_by_use(db):
    a = db.add_document(file_path="scans/t1.png")
    b = db.add_document(file_path="scans/t2.png")
    db.update_document(a, fields={"tags": ["migration", "military"]})
    db.update_document(b, fields={"tags": ["migration"]})
    assert db.list_tags()[0] == {"tag": "migration", "count": 2}
