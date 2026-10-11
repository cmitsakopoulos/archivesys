"""Tests for reading the date a document states about itself.

Arabic numerals are shared by every language this archive transcribes, so a
year is findable even when the surrounding words are not understood. Month
names are matched per language, with Greek genitive forms included because a
Katharevousa dateline uses the genitive: "τῇ 14ῃ Μαΐου 1923".
"""

import pytest
from src.ingestion.date_extract import extract_dates
from src.database.edtf_parser import EDTFParser


def top(text, filename=None):
    out = extract_dates(text, filename=filename)
    return out[0] if out else None


@pytest.mark.parametrize("text,expected", [
    # Greek, Katharevousa, genitive month in a dateline
    ("Πιστοποιοῦμεν ὅτι ἐν Ἀθήναις τῇ 14ῃ Μαΐου 1923 ἐγεννήθη ἄρρεν τέκνον.", "1923-05-14"),
    ("Ἐν Θεσσαλονίκῃ, τῇ 5ῃ Μαΐου 1912", "1912-05-05"),
    ("Ἐξεδόθη ἐν Πειραιεῖ τὴν 3ην Σεπτεμβρίου 1928", "1928-09-03"),
    # French
    ("Délivré à Smyrne, le 12 Septembre 1922.", "1922-09-12"),
    ("Fait à Paris le 1 janvier 1930", "1930-01-01"),
    # English, month first with an ordinal
    ("Arriving at Port of New York, October 24th, 1928 from Piraeus.", "1928-10-24"),
    ("Issued 7 August 1945 at the consulate", "1945-08-07"),
    # Numeric, D/M/Y as all three languages write it
    ("Reçu le 03/11/1931 au bureau central.", "1931-11-03"),
    ("Dated 14-05-1923", "1923-05-14"),
    ("ISO in the text: 1923-05-14", "1923-05-14"),
])
def test_reads_a_full_date_off_the_page(text, expected):
    best = top(text)
    assert best is not None, f"nothing found in {text!r}"
    assert best["edtf"] == expected
    assert best["precision"] == "day"


def test_month_and_year_without_a_day():
    best = top("ΦΩΤΟΓΡΑΦΙΑ ΕΛΗΦΘΗ ΤΟΝ ΜΑΙΟΝ 1935 ΕΝ ΠΕΙΡΑΙΕΙ")
    assert best["edtf"].startswith("1935-05")
    assert best["precision"] == "month"


def test_approximate_wording_becomes_an_approximate_date():
    best = top("ΦΩΤΟΓΡΑΦΙΑ ΕΝ ΤΩι ΚΗΠΩι (περὶ τὸ 1935)")
    assert best["edtf"] == "1935~"
    assert best["precision"] == "year"

    assert top("Photograph taken circa 1912")["edtf"] == "1912~"
    assert top("Cliché pris vers 1908")["edtf"].startswith("1908")


def test_a_bare_year_is_the_fallback_in_any_language():
    best = top("Ἀριθ. πρωτ. 1428 — ἔτος 1923")
    assert best["edtf"] == "1923"
    assert best["precision"] == "year"


def test_nothing_is_proposed_when_the_page_states_no_date():
    assert extract_dates("Ἀγαπητέ μου ἀδελφέ, ἐλπίζω νὰ εἶσαι καλά.") == []
    assert extract_dates("") == []
    assert extract_dates(None) == []


def test_numbers_that_are_not_years_are_ignored():
    """Reference numbers, page counts and sums of money are not dates."""
    assert extract_dates("Invoice no. 4500, page 12 of 99, amount 1.250 drachmas, ref 8492") == []
    assert extract_dates("Serial 999999 and 12345") == []


def test_years_outside_a_family_archive_are_ignored():
    assert extract_dates("In the year 1200 the empire fell") == []
    assert extract_dates("Projected to 2190") == []


def test_impossible_calendar_dates_are_not_proposed():
    assert not any(c["precision"] == "day" for c in extract_dates("Dated 32/13/1923"))


def test_the_filename_is_a_weak_last_resort():
    best = top("", filename="scan_letter_1912_smyrna.jpg")
    assert best["edtf"] == "1912"
    assert best["source"] == "filename"
    assert best["confidence"] < 0.5

    # Anything the page itself says outranks the filename.
    best2 = top("Délivré le 12 Septembre 1922.", filename="scan_1912.jpg")
    assert best2["edtf"] == "1922-09-12"


def test_a_specific_reading_supersedes_its_own_bare_year():
    out = extract_dates("Ἐν Ἀθήναις τῇ 14ῃ Μαΐου 1923. Ἀριθ. 1428 / 1923")
    assert out[0]["edtf"] == "1923-05-14"
    # The loose "1923" is the same date said less precisely, not a rival.
    assert "1923" not in [c["edtf"] for c in out]


def test_a_repeated_date_is_more_confident_than_one_mentioned_once():
    once = top("Signed 4 June 1931.")
    twice = top("Signed 4 June 1931. Countersigned 4 June 1931.")
    assert twice["confidence"] >= once["confidence"]


def test_every_proposal_carries_its_evidence():
    for c in extract_dates("Ἐν Ἀθήναις τῇ 14ῃ Μαΐου 1923 ἐγεννήθη."):
        assert c["evidence"].strip()
        assert "1923" in c["evidence"]


def test_every_proposal_is_storable_edtf():
    """A suggestion the archive would then refuse would be a cruel joke."""
    samples = [
        "Ἐν Ἀθήναις τῇ 14ῃ Μαΐου 1923", "le 12 Septembre 1922", "October 24th, 1928",
        "περὶ τὸ 1935", "circa 1912", "03/11/1931", "ΜΑΙΟΝ 1935", "ἔτος 1923",
    ]
    for text in samples:
        for c in extract_dates(text):
            assert EDTFParser.is_valid(c["edtf"]), f"{c['edtf']!r} from {text!r} is not storable"
