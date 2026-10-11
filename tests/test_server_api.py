"""Integration tests for Dashboard and Curation Server API."""

import pytest
from pathlib import Path
from starlette.testclient import TestClient

from src.database.db_manager import DatabaseManager
from src.curation.server import create_curation_app


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_api_archive.db"
    db = DatabaseManager(db_path)
    db.init_db()

    scans_dir = tmp_path / "scans"
    scans_dir.mkdir(parents=True, exist_ok=True)

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    app = create_curation_app(db, scans_dir, dist_dir=dist_dir)
    return TestClient(app)


def test_serve_dashboard(client):
    res = client.get("/curate")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_serve_timeline(client):
    res = client.get("/timeline")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "triple-vertical-timeline" in res.text or "timeline-container" in res.text


def test_recompile_timeline_api(client):
    res = client.post("/api/timeline/recompile")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"


def test_browse_filesystem_and_bookmarks(client, tmp_path):
    from PIL import Image
    # Create test folders and files
    folder1 = tmp_path / "album_1920"
    folder1.mkdir()
    img1 = folder1 / "scan_1.png"
    Image.new("RGB", (50, 50), (200, 200, 200)).save(img1)

    res = client.get(f"/api/fs/browse?path={folder1}")
    assert res.status_code == 200
    data = res.json()
    assert data["current_path"] == str(folder1.resolve())
    assert len(data["files"]) == 1
    assert data["files"][0]["name"] == "scan_1.png"
    assert data["files"][0]["is_indexed"] is False


def test_kinds_api_is_the_single_vocabulary(client):
    res = client.get("/api/kinds")
    assert res.status_code == 200
    kinds = res.json()
    assert [k["key"] for k in kinds] == [
        "certificate", "letter", "photograph", "passport", "press", "book", "document",
    ]
    # The studio reads OCR behaviour from here rather than keeping its own copy.
    assert {k["key"]: k["textual"] for k in kinds}["photograph"] is False
    assert {k["key"]: k["textual"] for k in kinds}["certificate"] is True


def test_tags_api(client, tmp_path):
    from PIL import Image
    img = tmp_path / "tagged.png"
    Image.new("RGB", (20, 20)).save(img)
    doc_id = client.post("/api/documents", json={"paths": [str(img)]}).json()["indexed"][0]["doc_id"]
    client.post(f"/api/documents/{doc_id}", json={"fields": {"tags": ["migration", "military"]}})

    res = client.get("/api/tags")
    assert res.status_code == 200
    assert {t["tag"] for t in res.json()} == {"migration", "military"}


def test_people_api(client):
    # Create person
    res = client.post("/api/people", json={
        "first_name": "Nikolaos",
        "last_name": "Theodorou",
        "birth_edtf": "1895",
        "notes": "Merchant and diplomat"
    })
    assert res.status_code == 200
    person_data = res.json()
    assert person_data["id"] > 0
    assert person_data["first_name"] == "Nikolaos"

    # List people
    res_list = client.get("/api/people")
    assert res_list.status_code == 200
    people = res_list.json()
    assert any(p["first_name"] == "Nikolaos" for p in people)


def test_the_single_write_path(client, tmp_path):
    from PIL import Image
    test_img = tmp_path / "consular_letter.png"
    Image.new("RGB", (100, 100), (240, 240, 240)).save(test_img)

    res_ingest = client.post("/api/documents", json={"paths": [str(test_img)]})
    assert res_ingest.status_code == 200
    doc_id = res_ingest.json()["indexed"][0]["doc_id"]

    p_id = client.post("/api/people", json={"first_name": "Helene", "last_name": "Theodorou"}).json()["id"]

    # One payload. v1 needed a metadata dict, a transcription, a file_type, a
    # verification_status and a separate event_data block repeating four of the
    # same fields.
    res = client.post(f"/api/documents/{doc_id}", json={
        "fields": {
            "title": "Consular Telegram from Smyrna",
            "edtf_date": "1922-09-05",
            "location": "Smyrna",
            "creator": "Greek Consulate General",
            "tags": ["Evacuation", "Consulate"],
            "notes": "Dispatched urgent telegram requesting transport.",
            "summary": "Consulate requests evacuation vessel support.",
            "transcription": "URGENT DISPATCH - SMYRNA 1922",
            "language": "en",
            "kind": "letter",
        },
        "people_links": [{"id": p_id, "role": "recipient"}],
        "reviewed": True,
    })
    assert res.status_code == 200
    saved = res.json()["document"]
    assert saved["title"] == "Consular Telegram from Smyrna"
    assert saved["kind"] == "letter"
    assert saved["date_display"] == "5 Sep 1922"
    assert saved["reviewed"] is True
    assert saved["people"][0]["role"] == "recipient"
    assert saved["clusters"] == []

    full = client.get(f"/api/documents/{doc_id}/full").json()
    assert full["location"] == "Smyrna"
    assert full["on_timeline"] is True


def test_an_unstorable_date_is_refused(client, tmp_path):
    from PIL import Image
    img = tmp_path / "d.png"
    Image.new("RGB", (20, 20)).save(img)
    doc_id = client.post("/api/documents", json={"paths": [str(img)]}).json()["indexed"][0]["doc_id"]

    for bad in ("May 1923", "14/5/1923", "n.d.", "1923-13"):
        res = client.post(f"/api/documents/{doc_id}", json={"fields": {"edtf_date": bad}})
        assert res.status_code == 400, bad
        assert "cannot be stored" in res.json()["detail"]

    # Refused, so the document is still undated rather than dated 1900.
    assert client.get(f"/api/documents/{doc_id}").json()["edtf_date"] is None

    res_ok = client.post(f"/api/documents/{doc_id}", json={"fields": {"edtf_date": "1923-05"}})
    assert res_ok.status_code == 200


def test_date_suggestions_read_the_page(client, tmp_path):
    from PIL import Image
    img = tmp_path / "greek_cert.png"
    Image.new("RGB", (20, 20)).save(img)
    doc_id = client.post("/api/documents", json={"paths": [str(img)]}).json()["indexed"][0]["doc_id"]

    client.post(f"/api/documents/{doc_id}", json={"fields": {
        "transcription": "Πιστοποιοῦμεν ὅτι ἐν Ἀθήναις τῇ 14ῃ Μαΐου 1923 ἐγεννήθη ἄρρεν τέκνον.",
    }})

    res = client.get(f"/api/documents/{doc_id}/date-suggestions")
    assert res.status_code == 200
    body = res.json()
    assert body["current"] is None
    assert body["suggestions"][0]["edtf"] == "1923-05-14"
    assert body["suggestions"][0]["precision"] == "day"
    # The proposal carries the words it came from, so it can be checked.
    assert "1923" in body["suggestions"][0]["evidence"]


def test_excluding_a_document_keeps_it_off_the_timeline(client, tmp_path):
    from PIL import Image
    img = tmp_path / "blurred.png"
    Image.new("RGB", (20, 20)).save(img)
    doc_id = client.post("/api/documents", json={"paths": [str(img)]}).json()["indexed"][0]["doc_id"]
    client.post(f"/api/documents/{doc_id}", json={"fields": {"edtf_date": "1928", "excluded": True}})

    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["excluded"] is True
    assert doc["on_timeline"] is False
    # v1 called this "rejected" and published it anyway.


def test_listing_filters_by_review_and_kind(client, tmp_path):
    from PIL import Image
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    for f in (a, b):
        Image.new("RGB", (20, 20)).save(f)
    ids = [x["doc_id"] for x in client.post("/api/documents", json={"paths": [str(a), str(b)]}).json()["indexed"]]
    client.post(f"/api/documents/{ids[0]}", json={"fields": {"kind": "letter"}, "reviewed": True})

    assert len(client.get("/api/documents?reviewed=true").json()) == 1
    assert len(client.get("/api/documents?reviewed=false").json()) == 1
    assert len(client.get("/api/documents?kind=letter").json()) == 1
    assert len(client.get("/api/documents").json()) == 2


def test_upload_document_and_export_api(client, tmp_path, monkeypatch):
    import io
    from PIL import Image

    # Uploads land in data/uploads relative to the working directory; without
    # this every run left a scan and a thumbnail in the project's own data/.
    monkeypatch.chdir(tmp_path)

    # Create dummy scan in memory
    img = Image.new("RGB", (100, 100), color=(240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    # 1. Upload scan via multipart
    res_upload = client.post(
        "/api/documents/upload",
        files={"file": ("birth_cert_1923.png", buf, "image/png")},
        data={"preferred_lang": "el-poly", "kind": "certificate"},
    )
    assert res_upload.status_code == 200
    up_data = res_upload.json()
    assert up_data["status"] == "success"
    doc_id = up_data["doc_id"]
    assert doc_id > 0

    # 2. Verify image streaming by doc_id and by sha256
    res_full = client.get(f"/api/documents/{doc_id}/full")
    assert res_full.status_code == 200
    doc = res_full.json()
    sha = doc.get("sha256")
    assert sha is not None and len(sha) == 64

    res_img_sha = client.get(f"/api/image?sha256={sha}")
    assert res_img_sha.status_code == 200
    assert "image" in res_img_sha.headers["content-type"]

    # 3. Export archive
    res_export = client.post("/api/export")
    assert res_export.status_code == 200
    exp_data = res_export.json()
    assert exp_data["status"] == "success"
    assert exp_data["output_file"] == "index.html"


def test_browse_filtering_and_search_api(client, tmp_path):
    from PIL import Image

    # Create folder structure with test images
    test_folder = tmp_path / "browse_test"
    test_folder.mkdir()
    sub_folder = test_folder / "athens_1920"
    sub_folder.mkdir()

    img1 = test_folder / "portrait_grandfather.jpg"
    img2 = test_folder / "landscape_corfu.png"
    img3 = test_folder / "document_passport.png"
    for p in (img1, img2, img3):
        Image.new("RGB", (50, 50), color=(200, 200, 200)).save(p)

    # 1. Browse without filter
    res = client.get(f"/api/fs/browse?path={test_folder}")
    assert res.status_code == 200
    data = res.json()
    assert len(data["directories"]) == 1
    assert len(data["files"]) == 3

    # 2. Query filter on name
    res_q = client.get(f"/api/fs/browse?path={test_folder}&query=portrait")
    assert res_q.status_code == 200
    data_q = res_q.json()
    assert len(data_q["files"]) == 1
    assert data_q["files"][0]["name"] == "portrait_grandfather.jpg"
    assert len(data_q["directories"]) == 0

    # 3. Index img1
    client.post("/api/documents", json={"paths": [str(img1)]})

    # 4. Filter by status: unindexed vs indexed
    res_unidx = client.get(f"/api/fs/browse?path={test_folder}&filter_status=unindexed")
    assert res_unidx.status_code == 200
    assert len(res_unidx.json()["files"]) == 2

    res_idx = client.get(f"/api/fs/browse?path={test_folder}&filter_status=indexed")
    assert res_idx.status_code == 200
    assert len(res_idx.json()["files"]) == 1
    assert res_idx.json()["files"][0]["name"] == "portrait_grandfather.jpg"


def test_one_ingest_endpoint(client, tmp_path):
    """v1 had four ingest endpoints reaching the same function.

    Three of them defaulted the type to "photograph", so every document that
    entered the archive claimed to be a photo and skipped its own OCR.
    """
    from PIL import Image
    p1, p2 = tmp_path / "photo1.jpg", tmp_path / "deed.jpg"
    for f in (p1, p2):
        Image.new("RGB", (60, 60), color=(100, 150, 200)).save(f)
    bad = tmp_path / "notes.txt"
    bad.write_text("not a scan")

    res = client.post("/api/documents", json={"paths": [str(p1), str(p2), str(bad), "/no/such/file.png"]})
    assert res.status_code == 200
    data = res.json()
    assert data["indexed_count"] == 2
    assert {s["reason"] for s in data["skipped"]} == {"unsupported format .txt", "not found"}

    # Nothing is guessed at the door: an unclassified scan is a "document".
    doc = client.get(f"/api/documents/{data['indexed'][0]['doc_id']}/full").json()
    assert doc["kind"] == "document"
    assert doc["transcription"] is None
    assert doc["reviewed"] is False

    # Indexing the same path again returns the same record, not a duplicate.
    again = client.post("/api/documents", json={"paths": [str(p1)]}).json()
    assert again["indexed"][0]["doc_id"] == data["indexed"][0]["doc_id"]

    # The endpoints it replaced are gone.
    for dead in ("/api/fs/quick-index", "/api/fs/batch-quick-index", "/api/fs/ingest"):
        assert client.post(dead, json={}).status_code == 404, dead


def test_the_dead_curate_twin_is_gone(client, tmp_path):
    from PIL import Image
    img = tmp_path / "x.png"
    Image.new("RGB", (20, 20)).save(img)
    doc_id = client.post("/api/documents", json={"paths": [str(img)]}).json()["indexed"][0]["doc_id"]
    assert client.post(f"/api/documents/{doc_id}/curate", json={}).status_code == 404


# ---------------------------------------------------------------------------
# People as a first-class axis, over HTTP
# ---------------------------------------------------------------------------
def _scan(tmp_path, name):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", (30, 30)).save(p)
    return str(p)


def test_people_api_carries_counts_and_portrait(client, tmp_path):
    pid = client.post("/api/people", json={"first_name": "Ἑλένη", "last_name": "Θεοδώρου",
                                           "birth_edtf": "1901"}).json()["id"]
    doc_id = client.post("/api/documents", json={"paths": [_scan(tmp_path, "a.png")]}).json()["indexed"][0]["doc_id"]
    client.post(f"/api/documents/{doc_id}", json={
        "fields": {"kind": "photograph", "edtf_date": "1935"},
        "people_links": [{"id": pid, "role": "subject"}],
    })

    person = [p for p in client.get("/api/people").json() if p["id"] == pid][0]
    assert person["doc_count"] == 1
    assert person["timeline_count"] == 1
    assert person["portrait"] == doc_id      # derived, nobody had to choose it


def test_an_unreadable_lifespan_is_refused_over_http(client):
    res = client.post("/api/people", json={"first_name": "Bad", "last_name": "Date",
                                           "birth_edtf": "around 1895"})
    assert res.status_code == 400
    assert "cannot be stored" in res.json()["detail"]


def test_portrait_modes_over_http(client, tmp_path):
    pid = client.post("/api/people", json={"first_name": "A", "last_name": "Person"}).json()["id"]
    ids = [x["doc_id"] for x in client.post("/api/documents", json={
        "paths": [_scan(tmp_path, "p1.png"), _scan(tmp_path, "p2.png")]}).json()["indexed"]]
    for i, doc_id in enumerate(ids):
        client.post(f"/api/documents/{doc_id}", json={
            "fields": {"kind": "photograph", "edtf_date": f"19{30 + i}"},
            "people_links": [{"id": pid, "role": "subject"}]})

    assert client.get(f"/api/people/{pid}").json()["portrait"] == ids[0]

    pinned = client.post(f"/api/people/{pid}/portrait",
                         json={"mode": "pinned", "document_id": ids[1]}).json()["person"]
    assert pinned["portrait"] == ids[1]

    none = client.post(f"/api/people/{pid}/portrait", json={"mode": "none"}).json()["person"]
    assert none["portrait"] is None and none["portrait_mode"] == "none"

    back = client.post(f"/api/people/{pid}/portrait", json={"mode": "auto"}).json()["person"]
    assert back["portrait"] == ids[0]


def test_portrait_options_endpoint(client, tmp_path):
    pid = client.post("/api/people", json={"first_name": "A", "last_name": "Person"}).json()["id"]
    doc_id = client.post("/api/documents", json={"paths": [_scan(tmp_path, "x.png")]}).json()["indexed"][0]["doc_id"]
    client.post(f"/api/documents/{doc_id}", json={
        "fields": {"kind": "photograph", "title": "A portrait"},
        "people_links": [{"id": pid, "role": "subject"}]})

    body = client.get(f"/api/people/{pid}/portrait-options").json()
    assert body["suggested"] == doc_id
    assert body["options"][0]["title"] == "A portrait"


def test_filtering_documents_by_person(client, tmp_path):
    a = client.post("/api/people", json={"first_name": "A", "last_name": "One"}).json()["id"]
    b = client.post("/api/people", json={"first_name": "B", "last_name": "Two"}).json()["id"]
    ids = [x["doc_id"] for x in client.post("/api/documents", json={
        "paths": [_scan(tmp_path, "1.png"), _scan(tmp_path, "2.png")]}).json()["indexed"]]
    client.post(f"/api/documents/{ids[0]}", json={"fields": {}, "people_links": [{"id": a, "role": "subject"}]})
    client.post(f"/api/documents/{ids[1]}", json={"fields": {}, "people_links": [
        {"id": a, "role": "subject"}, {"id": b, "role": "witness"}]})

    any_of = client.get(f"/api/documents?person={a}&person={b}&person_match=any").json()
    assert len(any_of) == 2
    all_of = client.get(f"/api/documents?person={a}&person={b}&person_match=all").json()
    assert [d["id"] for d in all_of] == [ids[1]]

    # Every row carries its people, so the catalog can group without refetching.
    assert len(all_of[0]["people"]) == 2


def test_duplicates_and_merge_over_http(client, tmp_path):
    a = client.post("/api/people", json={"first_name": "Γεώργιος", "last_name": "Θεοδώρου",
                                         "birth_edtf": "1895"}).json()["id"]
    b = client.post("/api/people", json={"first_name": "Georgios", "last_name": "Theodorou",
                                         "birth_edtf": "1895"}).json()["id"]
    doc_id = client.post("/api/documents", json={"paths": [_scan(tmp_path, "d.png")]}).json()["indexed"][0]["doc_id"]
    client.post(f"/api/documents/{doc_id}", json={"fields": {}, "people_links": [{"id": b, "role": "subject"}]})

    groups = client.get("/api/people/duplicates").json()
    assert any({a, b} <= {p["id"] for p in g["people"]} for g in groups)

    merged = client.post("/api/people/merge", json={"keep_id": a, "merge_id": b}).json()["person"]
    assert merged["id"] == a
    assert client.get(f"/api/people/{b}").status_code == 404
    assert len(client.get(f"/api/people/{a}/documents").json()) == 1


def test_roles_endpoint_excludes_relationships(client):
    roles = client.get("/api/people/roles").json()
    assert roles == ["subject", "author", "recipient", "witness", "photographer"]
    assert "father" not in roles


# ---------------------------------------------------------------------------
# Correcting people over HTTP: they could be created, never fixed or removed
# ---------------------------------------------------------------------------
def test_deleting_a_person_keeps_their_documents(client, tmp_path):
    pid = client.post("/api/people", json={"first_name": "Wrong", "last_name": "Person"}).json()["id"]
    doc_id = client.post("/api/documents", json={"paths": [_scan(tmp_path, "d.png")]}).json()["indexed"][0]["doc_id"]
    client.post(f"/api/documents/{doc_id}", json={"fields": {}, "people_links": [{"id": pid, "role": "subject"}]})

    res = client.delete(f"/api/people/{pid}")
    assert res.status_code == 200
    assert res.json()["deleted"]["documents"] == 1

    assert client.get(f"/api/people/{pid}").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/full").json()["people"] == []
    assert client.delete(f"/api/people/{pid}").status_code == 404


def test_editing_a_person_over_http(client):
    pid = client.post("/api/people", json={"first_name": "Georgois", "last_name": "Theodoru"}).json()["id"]

    res = client.post(f"/api/people/{pid}", json={"fields": {
        "first_name": "Georgios", "last_name": "Theodorou", "maiden_name": "",
        "birth_edtf": "1895~", "death_edtf": "1961-05", "notes": "Merchant in Piraeus.",
    }})
    assert res.status_code == 200
    person = res.json()["person"]
    assert (person["first_name"], person["last_name"]) == ("Georgios", "Theodorou")
    assert (person["birth_edtf"], person["death_edtf"]) == ("1895~", "1961-05")

    assert client.post(f"/api/people/{pid}", json={"fields": {"first_name": " "}}).status_code == 400
    assert client.post(f"/api/people/{pid}", json={"fields": {"death_edtf": "1890"}}).status_code == 400
    assert client.post("/api/people/999", json={"fields": {"first_name": "X"}}).status_code == 404


def test_a_blank_name_is_refused_over_http(client):
    res = client.post("/api/people", json={"first_name": "", "last_name": "Θεοδώρου"})
    assert res.status_code == 400
    assert "first and a last name" in res.json()["detail"]


def test_an_open_record_still_saves_after_someone_in_it_was_deleted(client, tmp_path):
    """The studio's editor sends the whole people list with every autosave. A
    link to someone deleted meanwhile used to fail it with a 500, forever."""
    kept = client.post("/api/people", json={"first_name": "Ἑλένη", "last_name": "Θεοδώρου"}).json()["id"]
    gone = client.post("/api/people", json={"first_name": "Wrong", "last_name": "Person"}).json()["id"]
    doc_id = client.post("/api/documents", json={"paths": [_scan(tmp_path, "d.png")]}).json()["indexed"][0]["doc_id"]
    links = [{"id": kept, "role": "subject"}, {"id": gone, "role": "witness"}]
    client.post(f"/api/documents/{doc_id}", json={"fields": {}, "people_links": links})
    client.delete(f"/api/people/{gone}")

    res = client.post(f"/api/documents/{doc_id}", json={"fields": {"title": "Still saves"}, "people_links": links})
    assert res.status_code == 200
    assert res.json()["document"]["title"] == "Still saves"
    assert [p["id"] for p in res.json()["document"]["people"]] == [kept]


# ---------------------------------------------------------------------------
# The Timeline Preview is built in memory, never streamed from dist/
# ---------------------------------------------------------------------------
def test_the_preview_is_sent_from_memory_and_writes_nothing(client, tmp_path):
    """It used to be written over dist/index.html and streamed from there, so a
    Recompile landing mid-stream cut it short ("Response content shorter than
    Content-Length") -- and every visit replaced the published copy."""
    page = tmp_path / "dist" / "index.html"
    res = client.get("/timeline")
    assert res.status_code == 200
    assert int(res.headers["content-length"]) == len(res.content)
    assert not page.exists(), "a preview must not write the published page"


def test_a_preview_leaves_the_published_copy_alone(client, tmp_path):
    client.post("/api/documents", json={"paths": [_scan(tmp_path, "d.png")]})
    assert client.post("/api/timeline/recompile").status_code == 200
    page = tmp_path / "dist" / "index.html"
    published = page.read_bytes()
    assert b"assets/scans/d_" in published          # the portable, published form

    assert client.get("/timeline").status_code == 200
    assert page.read_bytes() == published
    assert not [p.name for p in page.parent.iterdir() if p.name.endswith(".tmp")]


# ---------------------------------------------------------------------------
# Adding a whole folder: plan (nothing written), then a background import
# ---------------------------------------------------------------------------
def _folder_with_scans(root):
    import cv2
    import numpy as np
    from PIL import Image

    def typed(path, words):
        page = np.full((1400, 1000, 3), 238, np.uint8)
        for i, y in enumerate(range(120, 1300, 42)):
            cv2.putText(page, f"{words} line {i}", (80, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 40, 40), 2)
        cv2.imwrite(str(path), page)

    (root / "box" / "inner").mkdir(parents=True)
    typed(root / "box" / "Scan 1.png", "A letter")
    Image.new("RGB", (800, 600), (40, 30, 25)).save(root / "box" / "snapshot.png")
    (root / "box" / "copy of scan.png").write_bytes((root / "box" / "Scan 1.png").read_bytes())
    typed(root / "box" / "inner" / "Scan 1.png", "Another letter")      # same name, other scan
    return root / "box"


def test_planning_a_folder_writes_nothing_and_finds_copies(client, tmp_path):
    box = _folder_with_scans(tmp_path)
    plan = client.post("/api/import/plan", json={"path": str(box)}).json()

    assert plan["subfolders"] == [{"name": "inner", "count": 1}]
    # Of two identical files, the shorter name is the original and the other
    # its copy -- even though "copy of scan.png" sorts first.
    assert sorted(i["rel"] for i in plan["new"]) == ["Scan 1.png", "snapshot.png"]
    [copy] = plan["duplicates"]
    assert (copy["rel"], copy["same_as"]["name"]) == ("copy of scan.png", "Scan 1.png")
    assert copy["same_as"]["in_archive"] is False
    assert client.get("/api/documents").json() == []

    deep = client.post("/api/import/plan", json={"path": str(box), "include_subfolders": True}).json()
    assert "inner/Scan 1.png" in [i["rel"] for i in deep["new"]]


def test_importing_sorts_each_scan_and_reports_it(client, tmp_path):
    import time
    box = _folder_with_scans(tmp_path)
    plan = client.post("/api/import/plan", json={"path": str(box), "include_subfolders": True}).json()
    job = client.post("/api/import/start", json={
        "folder": plan["folder"], "paths": [i["path"] for i in plan["new"]]}).json()["job"]

    for _ in range(200):
        done = [j for j in client.get("/api/jobs").json()["recent"] if j["id"] == job["id"]]
        if done:
            break
        time.sleep(0.05)
    result = done[0]["result"]
    assert done[0]["state"] == "done"
    assert (result["added"], result["documents"], result["photographs"]) == (3, 2, 1)

    kinds = {Path(d["file_path"]).relative_to(box).as_posix(): d["kind"] for d in client.get("/api/documents").json()}
    assert kinds == {"Scan 1.png": "document", "inner/Scan 1.png": "document", "snapshot.png": "photograph"}

    # Planned again, everything is already there, and the copy points at the archive.
    again = client.post("/api/import/plan", json={"path": str(box), "include_subfolders": True}).json()
    assert again["new"] == [] and len(again["already"]) == 3
    assert again["duplicates"][0]["same_as"]["in_archive"] is True


def test_a_folder_that_is_not_there_is_refused(client, tmp_path):
    assert client.post("/api/import/plan", json={"path": str(tmp_path / "nope")}).status_code == 404
    assert client.post("/api/import/start", json={"folder": "x", "paths": [str(tmp_path / "nope.png")]}).status_code == 400


# ---------------------------------------------------------------------------
# Pages: "14600", "14600 1", "14600 2" are one item
# ---------------------------------------------------------------------------
def _item_folder(root):
    from PIL import Image
    box = root / "papers"
    box.mkdir()
    for i, name in enumerate(["14600.png", "14600 1.png", "14600 2.png", "letter.png"]):
        Image.new("RGB", (120, 160), (60 + i * 40, 50, 40)).save(box / name)
    return box


def _finish(client, job_id):
    import time
    for _ in range(200):
        done = [j for j in client.get("/api/jobs").json()["recent"] if j["id"] == job_id]
        if done:
            return done[0]
        time.sleep(0.05)
    raise AssertionError("the import never finished")


def test_the_plan_groups_pages_and_the_import_makes_one_item(client, tmp_path):
    box = _item_folder(tmp_path)
    plan = client.post("/api/import/plan", json={"path": str(box)}).json()

    (group,) = plan["groups"]
    assert group["name"] == "14600" and not group["counter"]
    assert [Path(p).name for p in group["paths"]] == ["14600.png", "14600 1.png", "14600 2.png"]
    assert {i["rel"]: i.get("group") for i in plan["new"]}["letter.png"] is None

    job = client.post("/api/import/start", json={
        "folder": plan["folder"], "paths": [i["path"] for i in plan["new"]],
        "groups": [group["paths"]]}).json()["job"]
    result = _finish(client, job["id"])["result"]
    assert (result["added"], result["items"], result["pages"]) == (4, 2, 2)

    docs = client.get("/api/documents").json()
    assert sorted((Path(d["file_path"]).name, d["page_count"]) for d in docs) == [("14600.png", 3), ("letter.png", 1)]
    lead = next(d for d in docs if d["page_count"] == 3)
    full = client.get(f"/api/documents/{lead['id']}/full").json()
    assert [p["name"] for p in full["pages"]] == ["14600.png", "14600 1.png", "14600 2.png"]

    # In the explorer every page is indexed and opens its item.
    files = client.get("/api/fs/browse", params={"path": str(box)}).json()["files"]
    assert {f["name"]: (f["doc_id"], f["page_no"]) for f in files}["14600 2.png"] == (lead["id"], 2)


def test_a_new_page_joins_its_item_already_in_the_archive(client, tmp_path):
    box = _item_folder(tmp_path)
    (box / "14600 2.png").rename(tmp_path / "later.png")
    plan = client.post("/api/import/plan", json={"path": str(box)}).json()
    job = client.post("/api/import/start", json={"folder": plan["folder"], "paths": [i["path"] for i in plan["new"]],
                                                 "groups": [g["paths"] for g in plan["groups"]]}).json()["job"]
    _finish(client, job["id"])

    (tmp_path / "later.png").rename(box / "14600 2.png")
    plan = client.post("/api/import/plan", json={"path": str(box)}).json()
    (group,) = plan["groups"]
    assert group["in_archive"] == 2
    job = client.post("/api/import/start", json={"folder": plan["folder"], "paths": [i["path"] for i in plan["new"]],
                                                 "groups": [group["paths"]]}).json()["job"]
    _finish(client, job["id"])
    assert sorted(d["page_count"] for d in client.get("/api/documents").json()) == [1, 3]


def test_split_records_are_proposed_then_combined_and_can_be_split_again(client, tmp_path):
    box = _item_folder(tmp_path)
    ids = [client.post("/api/documents", json={"paths": [str(p)]}).json()["indexed"][0]["doc_id"]
           for p in sorted(box.iterdir())]
    (proposal,) = client.get("/api/pages/proposals").json()["proposals"]
    assert [p["name"] for p in proposal["pages"]] == ["14600.png", "14600 1.png", "14600 2.png"]

    res = client.post("/api/pages/combine", json={"groups": [proposal["doc_ids"]]}).json()
    assert res["combined"] == [proposal["lead_id"]]
    assert client.get("/api/pages/proposals").json()["proposals"] == []
    assert len(client.get("/api/documents").json()) == 2

    second = proposal["doc_ids"][1]
    assert client.post(f"/api/documents/{second}/split").status_code == 200
    assert len(client.get("/api/documents").json()) == 3
    assert client.post(f"/api/documents/{proposal['lead_id']}/split").status_code == 400
    assert ids
