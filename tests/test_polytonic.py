"""Tests for polytonic Greek and multilingual Unicode normalizer."""

import pytest
from src.ingestion.polytonic import PolytonicNormalizer


def test_polytonic_preservation():
    """Verify that exact NFC polytonic Greek and French accents are preserved verbatim."""
    greek_text = "Βασίλειον τῆς Ἑλλάδος, Ἀθῆναι 1923. Τῷ καιρῷ ἐκείνῳ."
    french_text = "République Française — Évacuation sanitaire à Smyrne."
    
    exact_greek = PolytonicNormalizer.normalize_exact(greek_text)
    exact_french = PolytonicNormalizer.normalize_exact(french_text)
    
    assert exact_greek == greek_text
    assert exact_french == french_text


def test_polytonic_search_normalization():
    """Verify stripping of combining marks, case folding, and Greek sigma folding."""
    # Polytonic text with dasia, oxia, perispomeni, iota subscript, and final sigma
    raw = "«Ἐν τῷ κήπῳ τοῦ οἴκου ἡμῶν, ὁ λόγος ἦν σαφής»"
    normalized = PolytonicNormalizer.normalize_search(raw)
    
    # Should be lowercase, no accents, no iota subscript, final sigma folded to medial sigma
    # "εν τω κηπω του οικου ημων, ο λογοσ ην σαφησ"
    assert "εν τω κηπω του οικου ημων" in normalized
    assert "λογοσ" in normalized  # final sigma folded to medial sigma
    assert "σαφησ" in normalized  # final sigma folded to medial sigma
    assert "ό" not in normalized
    assert "ῆ" not in normalized


def test_cross_lingual_diacritic_folding():
    """Verify French and Latin accents are folded for search index."""
    french = "Évacuation, Rapatriement, Hélène, garçon"
    normalized = PolytonicNormalizer.normalize_search(french)
    
    assert "evacuation" in normalized
    assert "rapatriement" in normalized
    assert "helene" in normalized
    assert "garcon" in normalized


def test_is_polytonic_detector():
    polytonic = "Τὸ βρέφος ὠνομάσθη Χρῆστος"
    monotonic = "Το βρεφος ονομαστηκε Χρηστος"
    english = "Birth certificate of Christos"
    
    assert PolytonicNormalizer.is_polytonic(polytonic) is True
    assert PolytonicNormalizer.is_polytonic(monotonic) is False
    assert PolytonicNormalizer.is_polytonic(english) is False
