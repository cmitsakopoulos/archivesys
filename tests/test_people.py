"""Tests for people as a first-class axis: identity, portraits and grouping.

Each test here encodes something the archive got wrong before people were
browsable, so that making them browsable cannot quietly reintroduce it.
"""

import pytest
from PIL import Image

from src.database.db_manager import DatabaseManager, person_name_key, DOCUMENT_ROLES
from src.database.edtf_parser import NotADate


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(tmp_path / "people.db")
    manager.init_db()
    return manager


@pytest.fixture
def photo(tmp_path):
    def make(name):
        p = tmp_path / name
        Image.new("RGB", (40, 40), (180, 170, 150)).save(p)
        return str(p)
    return make


# ---------------------------------------------------------------------------
# Lifespans are validated as strictly as a document's date
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["around 1895", "n.d.", "unknown", "circa 1912", "14/5/1923"])
def test_an_unreadable_lifespan_is_refused(db, bad):
    """v2 fixed this for documents and left people accepting anything, which
    the parser then answered with the year 1900."""
    with pytest.raises(NotADate):
        db.add_person("Bad", "Date", birth_edtf=bad)


def test_a_death_cannot_precede_a_birth(db):
    with pytest.raises(NotADate):
        db.add_person("Impossible", "Person", birth_edtf="1950", death_edtf="1940")


def test_a_valid_lifespan_is_stored_and_sortable(db):
    pid = db.add_person("Γεώργιος", "Θεοδώρου", birth_edtf="1895", death_edtf="1962")
    person = db.get_person(pid)
    assert person["birth_edtf"] == "1895"
    assert person["birth_sort"] == 1895.0
    assert person["death_sort"] == 1962.0


def test_a_person_may_have_no_dates_at_all(db):
    pid = db.add_person("Unknown", "Dates")
    person = db.get_person(pid)
    assert person["birth_edtf"] is None and person["birth_sort"] is None


# ---------------------------------------------------------------------------
# Identity: one person, not three
# ---------------------------------------------------------------------------
def test_spelling_variants_are_one_person(db):
    a = db.get_or_create_person("Γεώργιος", "Θεοδώρου")
    b = db.get_or_create_person("Γεωργιος", "Θεοδωρου")   # no diacritics
    assert a == b, "polytonic and monotonic spellings must be the same man"


def test_transliteration_is_honestly_not_folded(db):
    """Documented limitation: Latin and Greek do not fold together, and the
    duplicate finder catches those by lifespan instead."""
    a = db.get_or_create_person("Γεώργιος", "Θεοδώρου")
    b = db.get_or_create_person("Georgios", "Theodorou")
    assert a != b


def test_name_key_folds_diacritics_and_sigma():
    assert person_name_key("Γεώργιος", "Θεοδώρου") == person_name_key("Γεωργιος", "Θεοδωρου")
    assert person_name_key("  Ἑλένη ", "Θεοδώρου") == person_name_key("Ελενη", "Θεοδωρου")


def test_duplicates_are_found_by_name_and_by_lifespan(db):
    a = db.add_person("Γεώργιος", "Θεοδώρου", birth_edtf="1895")
    b = db.add_person("Georgios", "Theodorou", birth_edtf="1895")
    groups = db.find_duplicate_people()
    reasons = {g["reason"] for g in groups}
    assert "same lifespan" in reasons
    ids = {p["id"] for g in groups for p in g["people"]}
    assert {a, b} <= ids


def test_merge_moves_documents_and_leaves_a_note(db, photo):
    keep = db.add_person("Ἑλένη", "Θεοδώρου", birth_edtf="1901")
    gone = db.add_person("Eleni", "Theodorou", death_edtf="1984")
    d1 = db.add_document(file_path=photo("a.jpg"), kind="photograph", edtf_date="1930")
    d2 = db.add_document(file_path=photo("b.jpg"), kind="certificate", edtf_date="1931")
    db.link_document_person(d1, keep, role="subject")
    db.link_document_person(d2, gone, role="witness")

    merged = db.merge_people(keep, gone)

    assert db.get_person(gone) is None
    assert {d["id"] for d in db.list_person_documents(keep)} == {d1, d2}
    # The survivor takes what it was missing, and says where it came from.
    assert merged["death_edtf"] == "1984"
    assert f"#{gone}" in merged["notes"]


def test_merge_keeps_the_more_specific_role(db, photo):
    keep = db.add_person("A", "Person")
    gone = db.add_person("A", "Personn")
    doc = db.add_document(file_path=photo("c.jpg"))
    db.link_document_person(doc, keep, role="subject")
    db.link_document_person(doc, gone, role="photographer")
    db.merge_people(keep, gone)
    assert db.get_document_full(doc)["people"][0]["role"] == "photographer"


def test_a_person_cannot_be_merged_into_themselves(db):
    pid = db.add_person("Solo", "Person")
    with pytest.raises(ValueError):
        db.merge_people(pid, pid)


def test_two_people_in_the_same_cluster_can_be_merged(db, photo):
    """Re-pointing the merged person's cluster rows collided with the
    survivor's own row, and the whole merge failed."""
    keep = db.add_person("Γεώργιος", "Θεοδώρου")
    gone = db.add_person("Georgios", "Theodorou")
    ev = db.add_event("Emigration", "1923", participant_ids=[keep, gone])

    db.merge_people(keep, gone)

    entry = next(e for e in db.get_timeline_export_data()["events"] if e["id"] == f"c{ev}")
    assert [p["id"] for p in entry["people"]] == [keep]


# ---------------------------------------------------------------------------
# Taking a person out again. People could be created but never deleted, so
# one typed by mistake -- or the seed's sample family -- stayed for good.
# ---------------------------------------------------------------------------
def test_a_person_can_be_deleted_and_their_documents_stay(db, photo):
    person = db.add_person("Wrong", "Person")
    other = db.add_person("Ἑλένη", "Θεοδώρου")
    d1 = db.add_document(file_path=photo("a.jpg"))
    d2 = db.add_document(file_path=photo("b.jpg"))
    for doc in (d1, d2):
        db.link_document_person(doc, person)
    db.link_document_person(d2, other)

    removed = db.delete_person(person)

    assert removed == {"id": person, "name": "Wrong Person", "documents": 2, "clusters_removed": 0}
    assert db.get_person(person) is None
    assert {d["id"] for d in db.list_documents()} == {d1, d2}
    assert [p["id"] for p in db.get_document_full(d2)["people"]] == [other]


def test_deleting_nobody_says_so(db):
    assert db.delete_person(999) is None


def test_a_deleted_persons_name_no_longer_finds_their_documents(db, photo):
    person = db.add_person("Χρῆστος", "Θεοδώρου")
    doc = db.add_document(file_path=photo("a.jpg"), title="A record")
    db.link_document_person(doc, person)
    assert db.search_documents("Χρηστος")

    db.delete_person(person)
    assert not db.search_documents("Χρηστος")


def test_a_cluster_known_only_through_the_deleted_person_goes_with_them(db, photo):
    person = db.add_person("Χρῆστος", "Θεοδώρου")
    other = db.add_person("Ἑλένη", "Θεοδώρου")
    remembered = db.add_event("Birth", "1923", participant_ids=[person])
    shared = db.add_event("Wedding", "1950", participant_ids=[person, other])
    d1 = db.add_document(file_path=photo("a.jpg"))
    d2 = db.add_document(file_path=photo("b.jpg"))
    sourced = db.add_event("Crossing", "1924", source_doc_ids=[d1, d2], participant_ids=[person])

    assert db.delete_person(person)["clusters_removed"] == 1

    left = {e["id"] for e in db.get_timeline_export_data()["events"] if e["source"] == "cluster"}
    assert left == {f"c{shared}", f"c{sourced}"}
    assert f"c{remembered}" not in left


def test_a_blank_name_is_refused(db):
    with pytest.raises(ValueError):
        db.add_person("  ", "Θεοδώρου")
    pid = db.add_person("Ἑλένη", "Θεοδώρου")
    with pytest.raises(ValueError):
        db.update_person(pid, {"last_name": ""})
    assert db.get_person(pid)["last_name"] == "Θεοδώρου"


def test_names_are_trimmed_and_cleared_fields_are_empty(db):
    pid = db.add_person("Ἑλένη", "Θεοδώρου", maiden_name="Παπαδοπούλου")
    person = db.update_person(pid, {"first_name": "  Ἑλένη ", "maiden_name": "  ", "notes": ""})
    assert person["first_name"] == "Ἑλένη"
    assert person["maiden_name"] is None and person["notes"] is None


def test_a_save_that_still_names_a_deleted_person_lands(db, photo):
    """An editor open since before the deletion sends the old link. A foreign
    key is not covered by OR IGNORE, so it used to fail the whole save."""
    kept = db.add_person("Ἑλένη", "Θεοδώρου")
    gone = db.add_person("Wrong", "Person")
    doc = db.add_document(file_path=photo("a.jpg"))
    db.delete_person(gone)

    saved = db.update_document(doc, fields={"title": "Kept"},
                               people_links=[{"id": kept, "role": "subject"}, {"id": gone, "role": "witness"}])
    assert saved["title"] == "Kept"
    assert [p["id"] for p in saved["people"]] == [kept]


def test_purging_samples_takes_the_seeds_people_but_nobody_else(db, photo, tmp_path):
    (tmp_path / "sample_data").mkdir()
    sample = db.add_document(file_path=photo("sample_data/cert.jpg"))
    real = db.add_document(file_path=photo("real.jpg"))
    lost = db.add_document(file_path=str(tmp_path / "moved-away.jpg"))   # file not there

    seed_only = db.add_person("Χρῆστος", "Θεοδώρου")
    also_real = db.add_person("Ἑλένη", "Θεοδώρου")
    unlinked = db.add_person("Added", "ByHand")
    in_lost_file = db.add_person("Γεώργιος", "Θεοδώρου")
    db.link_document_person(sample, seed_only)
    db.link_document_person(sample, also_real)
    db.link_document_person(real, also_real)
    db.link_document_person(lost, in_lost_file)

    report = db.purge_missing_documents(purge_sample_data=True)

    assert report["purged_documents"] == 2 and report["purged_people"] == 1
    assert db.get_person(seed_only) is None
    for survivor in (also_real, unlinked, in_lost_file):
        assert db.get_person(survivor) is not None


# ---------------------------------------------------------------------------
# Portraits: auto, pinned, none
# ---------------------------------------------------------------------------
def test_auto_prefers_a_portrait_over_an_earlier_group_shot(db, photo):
    person = db.add_person("Ἑλένη", "Θεοδώρου")
    other = db.add_person("Γεώργιος", "Θεοδώρου")

    group = db.add_document(file_path=photo("group.jpg"), kind="photograph", edtf_date="1930")
    solo = db.add_document(file_path=photo("solo.jpg"), kind="photograph", edtf_date="1935")
    for pid in (person, other):
        db.link_document_person(group, pid, role="subject")
    db.link_document_person(solo, person, role="subject")

    # The later solo photograph wins: a portrait beats a crowd.
    assert db.get_person(person)["portrait"] == solo


def test_auto_ignores_documents_that_are_not_photographs(db, photo):
    person = db.add_person("Χρῆστος", "Θεοδώρου")
    cert = db.add_document(file_path=photo("cert.jpg"), kind="certificate", edtf_date="1923")
    db.link_document_person(cert, person, role="subject")
    assert db.get_person(person)["portrait"] is None


def test_auto_ignores_excluded_photographs(db, photo):
    person = db.add_person("Χρῆστος", "Θεοδώρου")
    shot = db.add_document(file_path=photo("blur.jpg"), kind="photograph", edtf_date="1923")
    db.link_document_person(shot, person, role="subject")
    assert db.get_person(person)["portrait"] == shot
    db.update_document(shot, fields={"excluded": True})
    assert db.get_person(person)["portrait"] is None


def test_a_pinned_portrait_overrides_the_archive(db, photo):
    person = db.add_person("Ἑλένη", "Θεοδώρου")
    a = db.add_document(file_path=photo("a.jpg"), kind="photograph", edtf_date="1930")
    b = db.add_document(file_path=photo("b.jpg"), kind="photograph", edtf_date="1940")
    for doc in (a, b):
        db.link_document_person(doc, person, role="subject")
    assert db.get_person(person)["portrait"] == a

    db.set_portrait(person, b, "pinned")
    assert db.get_person(person)["portrait"] == b
    assert db.get_person(person)["portrait_mode"] == "pinned"


def test_no_picture_is_a_decision_the_archive_respects(db, photo):
    """The third state. Without it the auto-picker keeps overriding a curator
    who has said there should be no picture."""
    person = db.add_person("Ἑλένη", "Θεοδώρου")
    shot = db.add_document(file_path=photo("a.jpg"), kind="photograph", edtf_date="1930")
    db.link_document_person(shot, person, role="subject")
    assert db.get_person(person)["portrait"] == shot

    db.set_portrait(person, None, "none")
    person_row = db.get_person(person)
    assert person_row["portrait"] is None
    assert person_row["portrait_mode"] == "none"

    # Adding another photograph does not undo the decision.
    more = db.add_document(file_path=photo("b.jpg"), kind="photograph", edtf_date="1931")
    db.link_document_person(more, person, role="subject")
    assert db.get_person(person)["portrait"] is None


def test_returning_to_auto_restores_the_archives_choice(db, photo):
    person = db.add_person("Ἑλένη", "Θεοδώρου")
    shot = db.add_document(file_path=photo("a.jpg"), kind="photograph", edtf_date="1930")
    db.link_document_person(shot, person, role="subject")
    db.set_portrait(person, None, "none")
    db.set_portrait(person, None, "auto")
    assert db.get_person(person)["portrait"] == shot


def test_pinning_needs_a_document(db):
    person = db.add_person("A", "Person")
    with pytest.raises(ValueError):
        db.set_portrait(person, None, "pinned")


def test_deleting_the_pinned_scan_returns_the_person_to_auto(db, photo):
    person = db.add_person("Ἑλένη", "Θεοδώρου")
    a = db.add_document(file_path=photo("a.jpg"), kind="photograph", edtf_date="1930")
    b = db.add_document(file_path=photo("b.jpg"), kind="photograph", edtf_date="1940")
    for doc in (a, b):
        db.link_document_person(doc, person, role="subject")
    db.set_portrait(person, b, "pinned")

    db.delete_document(b)
    # ON DELETE SET NULL fires, so the person is not left pointing at a ghost.
    assert db.get_person(person)["portrait_document_id"] is None
    assert db.get_person(person)["portrait"] == a
    # ...and is not left "pinned" to nothing, either.
    assert db.get_person(person)["portrait_mode"] == "auto"


# ---------------------------------------------------------------------------
# Many-to-many, in both directions
# ---------------------------------------------------------------------------
def test_a_document_carries_everyone_in_it(db, photo):
    child = db.add_person("Χρῆστος", "Θεοδώρου")
    father = db.add_person("Γεώργιος", "Θεοδώρου")
    mother = db.add_person("Ἑλένη", "Θεοδώρου")
    cert = db.add_document(file_path=photo("cert.jpg"), kind="certificate", edtf_date="1923")
    for pid in (child, father, mother):
        db.link_document_person(cert, pid, role="subject")

    doc = db.list_documents()[0]
    assert len(doc["people"]) == 3


def test_filtering_by_any_of_several_people(db, photo):
    a = db.add_person("A", "One")
    b = db.add_person("B", "Two")
    d1 = db.add_document(file_path=photo("1.jpg"))
    d2 = db.add_document(file_path=photo("2.jpg"))
    d3 = db.add_document(file_path=photo("3.jpg"))
    db.link_document_person(d1, a)
    db.link_document_person(d2, b)
    db.link_document_person(d3, a)
    db.link_document_person(d3, b)

    got = {d["id"] for d in db.list_documents(people=[a, b], people_match="any")}
    assert got == {d1, d2, d3}


def test_filtering_by_all_of_several_people(db, photo):
    """The genealogically interesting one: where do these two appear together."""
    a = db.add_person("A", "One")
    b = db.add_person("B", "Two")
    d1 = db.add_document(file_path=photo("1.jpg"))
    d2 = db.add_document(file_path=photo("2.jpg"))
    db.link_document_person(d1, a)
    db.link_document_person(d2, a)
    db.link_document_person(d2, b)

    got = {d["id"] for d in db.list_documents(people=[a, b], people_match="all")}
    assert got == {d2}


def test_people_are_listed_with_their_counts(db, photo):
    person = db.add_person("Ἑλένη", "Θεοδώρου")
    dated = db.add_document(file_path=photo("a.jpg"), edtf_date="1930")
    undated = db.add_document(file_path=photo("b.jpg"))
    for doc in (dated, undated):
        db.link_document_person(doc, person, role="subject")

    row = db.list_people()[0]
    assert row["doc_count"] == 2
    assert row["timeline_count"] == 1     # only the dated one projects


def test_people_can_be_sorted_by_how_well_attested_they_are(db, photo):
    quiet = db.add_person("Quiet", "Person")
    busy = db.add_person("Busy", "Person")
    for i in range(3):
        doc = db.add_document(file_path=photo(f"{i}.jpg"))
        db.link_document_person(doc, busy)
    db.link_document_person(db.add_document(file_path=photo("q.jpg")), quiet)

    order = [p["id"] for p in db.list_people(sort="documents")]
    assert order[0] == busy


def test_renaming_a_person_keeps_search_working(db, photo):
    person = db.add_person("Wrong", "Name")
    doc = db.add_document(file_path=photo("a.jpg"), title="A record")
    db.link_document_person(doc, person, role="subject")
    assert db.search_documents("Wrong")

    db.update_person(person, {"first_name": "Γεώργιος", "last_name": "Θεοδώρου"})
    assert not db.search_documents("Wrong")
    assert db.search_documents("Θεοδωρου")


# ---------------------------------------------------------------------------
# Roles on a document are not relationships between people
# ---------------------------------------------------------------------------
def test_document_roles_are_a_closed_list_without_relationships():
    assert DOCUMENT_ROLES == ["subject", "author", "recipient", "witness", "photographer"]
    for relationship in ("father", "mother", "spouse", "sibling"):
        assert relationship not in DOCUMENT_ROLES, (
            "a relationship between people cannot be a role on a document: "
            "'father' does not say father of whom"
        )
