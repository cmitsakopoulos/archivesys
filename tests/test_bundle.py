"""Saving the whole archive to one .archivesys file, and opening it again.

Each test encodes a promise the format makes: the files come back byte for
byte, the records come back exactly, the curator comes back to where they were,
and a damaged or hostile file changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from src import config
from src.bundle import BundleError, inspect_bundle, restore_bundle, save_bundle, verify_bundle
from src.database.db_manager import DatabaseManager
from src.session import SessionError, load_session, save_session, session_path

TABLES = ("documents", "people", "document_people", "events", "event_sources", "event_participants")


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Opening an archive writes settings; never let that reach the project's own file."""
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_cached", None)
    yield
    config._cached = None


def make_scan(path: Path, color=(200, 190, 170), size=(64, 48)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(db_path: Path, drop=("file_path", "thumbnail_path")) -> dict:
    """Every row of every table, minus the columns that legitimately change on a move."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    out = {}
    for table in TABLES:
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for row in rows:
            for col in drop:
                row.pop(col, None)
        out[table] = rows
    conn.close()
    return out


def paths(db: DatabaseManager) -> dict:
    with db.get_connection() as conn:
        return {r["id"]: r["file_path"] for r in conn.execute("SELECT id, file_path FROM documents")}


@pytest.fixture
def archive(tmp_path):
    """A small archive with every kind of record: links, a cluster, a pinned
    portrait, tags, review state, polytonic text, a Greek file name."""
    scans = tmp_path / "scans"
    s1 = make_scan(scans / "yellow_folder_dad" / "Scan 4.png", (200, 180, 150))
    s2 = make_scan(scans / "album" / "1923 birth.png", (120, 130, 140))
    s3 = make_scan(scans / "album" / "Πιστοποιητικό.png", (90, 90, 90))

    db = DatabaseManager(tmp_path / "ws" / "archive.db")
    db.init_db()
    d1 = db.add_document(file_path=str(s1), kind="letter", title="Letter from Smyrna",
                         edtf_date="1922-09", transcription="Ἐν Σμύρνῃ, τῇ 12ῃ Σεπτεμβρίου")
    d2 = db.add_document(file_path=str(s2), kind="certificate", title="Birth of Christos",
                         edtf_date="1923-05-14")
    d3 = db.add_document(file_path=str(s3), kind="photograph", title="Garden, Piraeus", edtf_date="1935~")
    db.update_document(d1, fields={"summary": "Evacuation.", "tags": ["migration"], "notes": "From the yellow folder"},
                       reviewed=True, reviewed_by="tester")
    george = db.add_person("Γεώργιος", "Θεοδώρου", birth_edtf="1890")
    eleni = db.add_person("Ἑλένη", "Θεοδώρου")
    db.link_document_person(d1, george, role="author")
    db.link_document_person(d2, eleni, role="subject")
    db.link_document_person(d3, george, role="subject")
    db.add_event("Leaving Smyrna", "1922-09", source_doc_ids=[d1, d2], participant_ids=[george, eleni])
    db.set_portrait(george, d3, mode="pinned")

    save_session(session_path(db.db_path), {
        "view": "curation", "doc_id": d2,
        "catalog": {"review": "todo", "people": [george]},
        "viewer": {"doc_id": d2, "zoom": 2.5, "cx": 0.3, "cy": 0.6},
    })
    config.save_settings({"clahe_clip_limit": 2.2, "refine_enabled": False})
    return {"db": db, "scans": scans, "files": [s1, s2, s3], "docs": [d1, d2, d3],
            "people": [george, eleni], "tmp": tmp_path}


def fresh_db(tmp_path: Path, name: str = "other") -> DatabaseManager:
    db = DatabaseManager(tmp_path / name / "archive.db")
    db.init_db()
    return db


def rewrite(src: Path, dest: Path, edit) -> Path:
    """Copy a saved archive member by member, letting `edit(name, data)` change one."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w") as zout:
        for item in zin.infolist():
            data = edit(item.filename, zin.read(item.filename))
            if data is not None:
                zout.writestr(item, data)
    return dest


# =============================================================================
# The format
# =============================================================================
def test_saved_file_is_a_valid_bagit_bag(archive, tmp_path):
    report = save_bundle(archive["db"], tmp_path, name="Theodorou Family")
    assert report.path.suffix == ".archivesys"
    assert report.files == 3 and report.documents == 3 and report.people == 2

    with zipfile.ZipFile(report.path) as zf:
        names = zf.namelist()
        root = {n.split("/")[0] for n in names}
        assert len(root) == 1, "a serialized bag has exactly one top-level directory"
        root = root.pop() + "/"
        for tag in ("bagit.txt", "bag-info.txt", "manifest-sha256.txt", "tagmanifest-sha256.txt"):
            assert root + tag in names
        assert zf.read(root + "bagit.txt").decode().startswith("BagIt-Version: 1.0\n")

        manifest = dict(reversed(line.split(None, 1))
                        for line in zf.read(root + "manifest-sha256.txt").decode().splitlines())
        payload = [n[len(root):] for n in names if n.startswith(root + "data/")]
        assert sorted(manifest) == sorted(payload), "every payload file is listed, and only those"
        for rel, digest in manifest.items():
            assert hashlib.sha256(zf.read(root + rel)).hexdigest() == digest

        # Provenance kept, home directory not: the folder a scan was in, and its name.
        assert "data/files/yellow_folder_dad/Scan 4.png" in manifest
        assert "data/files/album/Πιστοποιητικό.png" in manifest
        assert not any("scans" in rel or str(tmp_path) in rel for rel in manifest)

        # Scans go in as they are: already compressed, recompressing gains nothing.
        png = zf.getinfo(root + "data/files/album/1923 birth.png")
        assert png.compress_type == zipfile.ZIP_STORED

    result = verify_bundle(report.path)
    assert result["ok"], result["problems"]
    assert result["files"] == 5                         # 3 scans + archive.db + archivesys.json


def test_saving_only_reads_the_live_archive(archive, tmp_path):
    live = archive["db"].db_path
    before = sha(live)
    save_bundle(archive["db"], tmp_path)
    assert sha(live) == before


def test_inspect_summarises_without_unpacking(archive, tmp_path):
    report = save_bundle(archive["db"], tmp_path, name="Theodorou Family")
    info = inspect_bundle(report.path)
    assert info["name"] == "Theodorou Family"
    assert info["counts"]["documents"] == 3 and info["counts"]["files"] == 3
    assert info["scan_bytes"] == sum(f.stat().st_size for f in archive["files"])
    assert info["resume"]["view"] == "curation"
    assert info["resume"]["document"]["title"] == "Birth of Christos"


def test_same_name_in_same_named_folders_does_not_collide(tmp_path):
    a = make_scan(tmp_path / "a" / "album" / "Scan 1.png", (10, 10, 10))
    b = make_scan(tmp_path / "b" / "album" / "scan 1.PNG", (240, 240, 240))
    db = fresh_db(tmp_path, "ws")
    db.add_document(file_path=str(a))
    db.add_document(file_path=str(b))
    report = save_bundle(db, tmp_path)
    with zipfile.ZipFile(report.path) as zf:
        files = sorted(n.split("/data/files/")[1] for n in zf.namelist() if "/data/files/" in n)
    assert files == ["album/Scan 1.png", "album/scan 1 (2).PNG"]

    digests = sorted(sha(p) for p in (a, b))
    for p in (a, b):
        p.unlink()
    dst = fresh_db(tmp_path)
    assert restore_bundle(report.path, dst).unpacked == 2
    restored = [Path(p) for p in paths(dst).values()]
    assert sorted(sha(p) for p in restored) == digests
    assert sorted(p.name for p in restored) == ["Scan 1.png", "scan 1 (2).PNG"]


def test_saving_into_a_folder_never_overwrites_an_earlier_save(archive, tmp_path):
    first = save_bundle(archive["db"], tmp_path, name="Same Name")
    second = save_bundle(archive["db"], tmp_path, name="Same Name")
    assert first.path != second.path
    assert first.path.exists() and second.path.exists()


# =============================================================================
# Opening: the files, the records, the curator
# =============================================================================
def test_opening_on_another_computer_restores_everything(archive, tmp_path):
    report = save_bundle(archive["db"], tmp_path)
    originals = {p.name: sha(p) for p in archive["files"]}
    expected_rows = dump(archive["db"].db_path)

    # Another computer: none of the original paths exist, and settings differ.
    archive["scans"].rename(tmp_path / "scans-elsewhere")
    config.reset_settings()

    dst = fresh_db(tmp_path)
    result = restore_bundle(report.path, dst)
    assert (result.relinked, result.unpacked, result.missing) == (0, 3, [])

    # Records: identical, row for row, except where the scans now live.
    assert dump(dst.db_path) == expected_rows

    # Files: byte for byte, unpacked into the library, keeping their folder.
    restored = paths(dst)
    for doc_id, p in restored.items():
        p = Path(p)
        assert p.is_relative_to(tmp_path / "other" / "library")
        assert sha(p) == originals[p.name]
    assert Path(restored[archive["docs"][0]]).parent.name == "yellow_folder_dad"

    # The curator: same view, same record, same filters, same zoom.
    session = load_session(session_path(dst.db_path))
    assert session["view"] == "curation" and session["doc_id"] == archive["docs"][1]
    assert session["viewer"]["zoom"] == 2.5

    # Settings come back too.
    assert config.load_settings().clahe_clip_limit == 2.2
    assert config.load_settings().refine_enabled is False


def test_opening_on_the_same_computer_links_scans_where_they_are(archive, tmp_path):
    report = save_bundle(archive["db"], tmp_path)
    before = paths(archive["db"])
    dst = fresh_db(tmp_path)
    result = restore_bundle(report.path, dst)
    assert (result.relinked, result.unpacked) == (3, 0)
    assert paths(dst) == before
    assert not (tmp_path / "other" / "library").exists(), "nothing duplicated"


def test_a_changed_original_is_not_trusted(archive, tmp_path):
    report = save_bundle(archive["db"], tmp_path)
    edited = archive["files"][1]
    saved_digest = sha(edited)
    make_scan(edited, (1, 2, 3))                      # cropped, recoloured, replaced...

    dst = fresh_db(tmp_path)
    result = restore_bundle(report.path, dst)
    assert (result.relinked, result.unpacked) == (2, 1)
    restored = Path(paths(dst)[archive["docs"][1]])
    assert restored != edited and sha(restored) == saved_digest


def test_opening_the_same_archive_twice_reuses_what_it_unpacked(archive, tmp_path):
    report = save_bundle(archive["db"], tmp_path)
    archive["scans"].rename(tmp_path / "gone")
    dst = fresh_db(tmp_path)
    restore_bundle(report.path, dst)
    first = paths(dst)
    restore_bundle(report.path, dst)
    assert paths(dst) == first, "no '(2)' copies of identical files"


def test_opening_backs_up_what_was_open_and_the_backup_goes_back(archive, tmp_path):
    other_scan = make_scan(tmp_path / "mine" / "mine.png", (5, 50, 150))
    dst = fresh_db(tmp_path)
    mine = dst.add_document(file_path=str(other_scan), title="My own document")
    save_session(session_path(dst.db_path), {"view": "people"})
    mine_rows = dump(dst.db_path)

    result = restore_bundle(save_bundle(archive["db"], tmp_path).path, dst)
    backup = Path(result.backup)
    assert backup.parent == tmp_path / "other" / "backups" and backup.suffix == ".archivesys"
    assert inspect_bundle(backup)["scans_included"] is False
    assert backup.stat().st_size < 64 * 1024, "the backup records scans, it does not copy them"
    with dst.get_connection() as conn:
        titles = {r[0] for r in conn.execute("SELECT title FROM documents")}
    assert "My own document" not in titles and "Letter from Smyrna" in titles

    # Undo is just opening the backup.
    restore_bundle(backup, dst)
    assert dump(dst.db_path) == mine_rows
    assert paths(dst)[mine] == str(other_scan)
    assert load_session(session_path(dst.db_path))["view"] == "people"


def test_an_empty_archive_is_not_backed_up(archive, tmp_path):
    result = restore_bundle(save_bundle(archive["db"], tmp_path).path, fresh_db(tmp_path))
    assert result.backup is None


# =============================================================================
# Missing scans, and refusing to bind a record to the wrong file
# =============================================================================
def test_a_scan_missing_when_saved_stays_missing(tmp_path):
    db = fresh_db(tmp_path, "ws")
    ghost = tmp_path / "unplugged-drive" / "ghost.png"
    doc = db.add_document(file_path=str(ghost), title="On a drive that was not plugged in")
    report = save_bundle(db, tmp_path)
    assert [m["doc_id"] for m in report.missing] == [doc]

    dst = fresh_db(tmp_path)
    result = restore_bundle(report.path, dst)
    assert result.missing == [{"doc_id": doc, "title": "On a drive that was not plugged in",
                               "path": str(ghost), "reason": "not found"}]
    assert paths(dst)[doc] == str(ghost), "kept, so it reconnects when the drive is back"


def test_a_stranger_file_at_a_missing_scans_path_is_not_adopted(tmp_path):
    """Regression: init_db hashes whatever is at each recorded path. Verifying
    against that hash would have bound this record to someone else's file."""
    db = fresh_db(tmp_path, "ws")
    ghost = tmp_path / "shared" / "tax-return.png"
    doc = db.add_document(file_path=str(ghost), title="Recorded but not present")
    report = save_bundle(db, tmp_path)

    make_scan(ghost, (255, 0, 0))                     # an unrelated file now sits there
    dst = fresh_db(tmp_path)
    result = restore_bundle(report.path, dst)

    assert result.missing[0]["reason"] == "a different file is at its old location"
    new_path = Path(paths(dst)[doc])
    assert new_path != ghost and not new_path.exists()
    assert new_path.is_relative_to(tmp_path / "other" / "library")
    with dst.get_connection() as conn:
        assert conn.execute("SELECT sha256 FROM documents WHERE id = ?", (doc,)).fetchone()[0] is None


# =============================================================================
# A damaged or hostile file changes nothing
# =============================================================================
def assert_untouched(dst: DatabaseManager, rows_before: dict, tmp_path: Path):
    assert dump(dst.db_path) == rows_before
    assert not (tmp_path / "other" / "library").exists()
    assert not list((tmp_path / "other").glob(".restore-*")), "staging is always cleaned up"
    assert not (tmp_path / "other" / "backups").exists()


def test_a_flipped_byte_is_caught_and_nothing_changes(archive, tmp_path):
    report = save_bundle(archive["db"], tmp_path)
    archive["scans"].rename(tmp_path / "gone")        # force the scans to be unpacked
    raw = bytearray(report.path.read_bytes())
    marker = raw.find(b"IDAT")                        # inside a stored PNG
    raw[marker + 10] ^= 0xFF
    damaged = tmp_path / "damaged.archivesys"
    damaged.write_bytes(bytes(raw))

    dst = fresh_db(tmp_path)
    before = dump(dst.db_path)
    with pytest.raises(BundleError, match="damaged"):
        restore_bundle(damaged, dst)
    assert_untouched(dst, before, tmp_path)
    assert not verify_bundle(damaged)["ok"]


def test_altered_content_fails_its_fingerprint(archive, tmp_path):
    """Valid ZIP, valid CRC, different bytes: only the SHA-256 manifest can tell."""
    report = save_bundle(archive["db"], tmp_path)
    archive["scans"].rename(tmp_path / "gone")
    tampered = rewrite(report.path, tmp_path / "tampered.archivesys",
                       lambda name, data: data[:-4] + b"\x00\x00\x00\x00" if name.endswith("1923 birth.png") else data)
    dst = fresh_db(tmp_path)
    before = dump(dst.db_path)
    with pytest.raises(BundleError, match="fingerprint"):
        restore_bundle(tampered, dst)
    assert_untouched(dst, before, tmp_path)


def test_a_path_escaping_the_library_is_refused(archive, tmp_path):
    """A crafted file that is internally consistent -- every fingerprint
    matches -- but whose file table points outside the library (zip-slip)."""
    report = save_bundle(archive["db"], tmp_path)
    evil_rel = "data/files/../../../../evil.png"
    payload = b"not really a png"
    digest = hashlib.sha256(payload).hexdigest()
    new_info = {}

    def edit(name, data):
        if name.endswith("data/archivesys.json"):
            info = json.loads(data)
            info["files"][0].update(path=evil_rel, sha256=digest, size=len(payload))
            new_info["old"] = hashlib.sha256(data).hexdigest()
            new_info["bytes"] = json.dumps(info).encode()
            return new_info["bytes"]
        if name.endswith("manifest-sha256.txt") and not name.endswith("tagmanifest-sha256.txt"):
            patched = data.decode().replace(new_info["old"], hashlib.sha256(new_info["bytes"]).hexdigest())
            return (patched + f"{digest}  {evil_rel}\n").encode()
        return data

    hostile = rewrite(report.path, tmp_path / "hostile.archivesys", edit)
    with zipfile.ZipFile(hostile, "a") as zf:
        root = zf.namelist()[0].split("/")[0]
        zf.writestr(f"{root}/{evil_rel}", payload)

    dst = fresh_db(tmp_path)
    before = dump(dst.db_path)
    with pytest.raises(BundleError, match="unsafe"):
        restore_bundle(hostile, dst)
    assert_untouched(dst, before, tmp_path)
    assert not any(tmp_path.rglob("evil.png"))


def test_triggers_in_an_opened_file_are_not_trusted(archive, tmp_path):
    with archive["db"].get_connection() as conn:
        conn.execute("CREATE TRIGGER wipe AFTER UPDATE ON people BEGIN DELETE FROM documents; END;")
    report = save_bundle(archive["db"], tmp_path)
    dst = fresh_db(tmp_path)
    restore_bundle(report.path, dst)
    with dst.get_connection() as conn:
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert "wipe" not in triggers
    assert {"trg_docs_insert", "trg_docs_update", "trg_docs_delete"} <= triggers
    dst.update_person(archive["people"][0], {"notes": "edited"})
    assert len(paths(dst)) == 3


def test_a_newer_format_is_refused_with_a_reason(archive, tmp_path):
    report = save_bundle(archive["db"], tmp_path)
    newer = rewrite(report.path, tmp_path / "newer.archivesys", lambda name, data: (
        json.dumps({**json.loads(data), "format_version": 99}).encode()
        if name.endswith("data/archivesys.json") else data))
    with pytest.raises(BundleError, match="newer version of ArchiveSys"):
        restore_bundle(newer, fresh_db(tmp_path))


@pytest.mark.parametrize("content", [b"", b"just some text", b"PK\x03\x04 truncated"])
def test_a_file_that_is_not_an_archive_is_refused(tmp_path, content):
    bogus = tmp_path / "bogus.archivesys"
    bogus.write_bytes(content)
    with pytest.raises(BundleError):
        inspect_bundle(bogus)


def test_a_plain_zip_is_refused(tmp_path):
    plain = tmp_path / "holiday.archivesys"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("photo.jpg", b"...")
    with pytest.raises(BundleError, match="does not contain"):
        inspect_bundle(plain)


# =============================================================================
# The session file
# =============================================================================
def test_session_round_trip_and_limits(tmp_path):
    p = session_path(tmp_path / "archive.db")
    assert load_session(p) == {}
    save_session(p, {"view": "people", "doc_id": 3})
    assert load_session(p) == {"view": "people", "doc_id": 3}
    for bad in ([1, 2], "view", None):
        with pytest.raises(SessionError):
            save_session(p, bad)
    with pytest.raises(SessionError, match="too large"):
        save_session(p, {"x": "a" * 70_000})
    assert load_session(p) == {"view": "people", "doc_id": 3}, "a refused write keeps the last good one"
    p.write_text("{not json")
    assert load_session(p) == {}


# =============================================================================
# Through the studio's API
# =============================================================================
def make_client(db: DatabaseManager, tmp_path: Path) -> TestClient:
    from src.curation.server import create_curation_app
    scans, dist = tmp_path / "scans_mount", tmp_path / "dist"
    scans.mkdir(exist_ok=True)
    dist.mkdir(exist_ok=True)
    return TestClient(create_curation_app(db, scans, dist_dir=dist))


def test_save_download_upload_and_open_through_the_api(archive, tmp_path):
    src = make_client(archive["db"], tmp_path)
    saved = src.post("/api/bundle/save", json={"session": {"view": "person", "person_id": archive["people"][0]}})
    assert saved.status_code == 200, saved.text
    data = saved.json()
    assert data["files"] == 3 and data["filename"].endswith(".archivesys")

    download = src.get(data["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/octet-stream", "Safari unpacks zips"
    assert ".archivesys" in download.headers["content-disposition"]
    assert src.get(data["download_url"]).status_code == 404, "each download link works once"

    dst_db = fresh_db(tmp_path)
    dst = make_client(dst_db, tmp_path)
    uploaded = dst.post("/api/bundle/upload?filename=family.archivesys", content=download.content)
    assert uploaded.status_code == 200, uploaded.text
    up = uploaded.json()
    assert up["bundle"]["counts"]["documents"] == 3
    assert up["bundle"]["resume"]["person"]["name"] == "Γεώργιος Θεοδώρου"
    assert up["current"] == {"documents": 0, "people": 0, "clusters": 0}

    opened = dst.post(f"/api/bundle/restore/{up['token']}")
    assert opened.status_code == 200, opened.text
    assert opened.json()["relinked"] == 3
    assert dst.get("/api/session").json() == {"view": "person", "person_id": archive["people"][0]}
    assert sorted(d["title"] for d in dst.get("/api/documents").json()) == [
        "Birth of Christos", "Garden, Piraeus", "Letter from Smyrna"]
    assert dst.post(f"/api/bundle/restore/{up['token']}").status_code == 404, "tokens are single use"


def test_uploading_something_else_is_refused(archive, tmp_path):
    client = make_client(archive["db"], tmp_path)
    res = client.post("/api/bundle/upload?filename=x.archivesys", content=b"hello")
    assert res.status_code == 400
    assert "not a saved ArchiveSys archive" in res.json()["detail"]


def test_opening_waits_for_pages_being_read(archive, tmp_path, monkeypatch):
    """Swapping the database under a running OCR job would file its text under
    whatever record now has that id."""
    import src.curation.server as server

    class BusyQueue:
        busy = True

        def __init__(self, runner):
            pass

        def snapshot(self):
            active = {"id": 1} if BusyQueue.busy else None
            return {"active": active, "queued": [], "recent": [], "queued_count": 0}

    monkeypatch.setattr(server, "JobQueue", BusyQueue)
    report = save_bundle(archive["db"], tmp_path)
    client = make_client(fresh_db(tmp_path), tmp_path)
    token = client.post("/api/bundle/upload", content=report.path.read_bytes()).json()["token"]

    blocked = client.post(f"/api/bundle/restore/{token}")
    assert blocked.status_code == 409
    BusyQueue.busy = False
    assert client.post(f"/api/bundle/restore/{token}").status_code == 200, "the upload was kept for a retry"


def test_session_api(tmp_path):
    client = make_client(fresh_db(tmp_path, "ws"), tmp_path)
    assert client.get("/api/session").json() == {}
    assert client.put("/api/session", json={"view": "database", "doc_id": 4}).status_code == 200
    assert client.get("/api/session").json() == {"view": "database", "doc_id": 4}
    # navigator.sendBeacon can only POST.
    assert client.post("/api/session", json={"view": "people"}).status_code == 200
    assert client.get("/api/session").json() == {"view": "people"}
    assert client.put("/api/session", json=[1, 2]).status_code == 400
    assert client.put("/api/session", content=b"{nope", headers={"content-type": "application/json"}).status_code == 400


def test_an_item_with_pages_travels_as_one_item(tmp_path):
    """Pages are records of their own, so every page's file is packed, fingerprinted
    and put back -- and still belongs to its item on the other computer."""
    scans = tmp_path / "scans" / "box"
    files = [make_scan(scans / name, (60 + i * 50, 70, 80)) for i, name in
             enumerate(["14600.png", "14600 1.png", "14600 2.png"])]
    db = DatabaseManager(tmp_path / "ws" / "archive.db")
    db.init_db()
    ids = [db.index_scan(f, kind="document") for f in files]
    db.update_document(ids[1], fields={"transcription": "δεύτερη σελίδα"})
    db.combine_pages(ids[0], ids[1:])
    report = save_bundle(db, tmp_path)

    (tmp_path / "scans").rename(tmp_path / "scans-elsewhere")
    dst = fresh_db(tmp_path)
    result = restore_bundle(report.path, dst)
    assert (result.unpacked, result.missing) == (3, [])

    (item,) = dst.list_documents()
    pages = dst.get_document_full(item["id"])["pages"]
    assert [Path(p["file_path"]).name for p in pages] == ["14600.png", "14600 1.png", "14600 2.png"]
    assert all(Path(p["file_path"]).is_relative_to(tmp_path / "other" / "library") for p in pages)
    assert pages[1]["transcription"] == "δεύτερη σελίδα"
