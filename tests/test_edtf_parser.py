"""Tests for Extended Date/Time Format (EDTF) parser."""

import pytest
from src.database.edtf_parser import EDTFParser


def test_exact_dates():
    p1 = EDTFParser.parse("1923-05-14")
    assert p1.iso_start == "1923-05-14"
    assert p1.sort_start == pytest.approx(1923.369, rel=1e-3)
    assert p1.date_display == "14 May 1923"
    assert p1.is_approximate is False
    assert p1.is_uncertain is False


def test_approximate_and_uncertain_dates():
    p_approx = EDTFParser.parse("1923~")
    assert p_approx.is_approximate is True
    assert "ca. 1923" in p_approx.date_display

    p_unc = EDTFParser.parse("1935?")
    assert p_unc.is_uncertain is True
    assert "?" in p_unc.date_display


def test_date_ranges():
    p_range = EDTFParser.parse("1912-05/1912-08")
    assert p_range.is_range is True
    assert p_range.iso_start == "1912-05-01"
    assert p_range.iso_end == "1912-08-01"
    assert p_range.sort_start < p_range.sort_end
    assert "May – Aug 1912" in p_range.date_display


def test_decade_parsing():
    p_dec = EDTFParser.parse("192X")
    assert p_dec.is_range is True
    assert p_dec.iso_start == "1920-01-01"
    assert p_dec.iso_end == "1929-12-31"
    assert p_dec.date_display == "1920s"
    assert p_dec.sort_start == 1920.0
