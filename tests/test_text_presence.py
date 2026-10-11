"""The scan sorter: text pages are documents, photographs are photographs,
and anything it cannot place is filed as a document so it is still read."""

import numpy as np
import pytest
import cv2

from src.ingestion import text_presence
from src.ingestion.text_presence import sort_scan


def typed_page():
    page = np.full((1400, 1000, 3), 238, np.uint8)
    for i, y in enumerate(range(120, 1300, 42)):
        cv2.putText(page, f"Line {i} of a typed letter, dated 14 May 1923", (80, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 40, 40), 2, cv2.LINE_AA)
    return page


def snapshots():
    """Prints laid on a flatbed: dark, continuous-tone, no text."""
    rng = np.random.default_rng(7)
    scan = np.full((1400, 1000, 3), 250, np.uint8)
    for x, y in ((60, 80), (520, 80), (60, 720), (520, 720)):
        tone = cv2.GaussianBlur(rng.integers(0, 120, (560, 400, 3)).astype(np.uint8), (0, 0), 25)
        scan[y:y + 560, x:x + 400] = tone
    return scan


def test_a_typed_page_is_a_document():
    result = sort_scan(typed_page())
    assert result["verdict"] == "text" and result["kind"] == "document"
    assert result["text_area"] >= text_presence.TEXT_AREA_DOCUMENT


def test_prints_on_a_flatbed_are_a_photograph():
    result = sort_scan(snapshots())
    assert result["verdict"] == "photo" and result["kind"] == "photograph"


def test_a_blank_page_is_undecided_and_filed_as_a_document():
    """Neither text nor a picture: it must not be called a photograph, which
    would switch off its reading. It waits in To review instead."""
    result = sort_scan(np.full((1400, 1000, 3), 244, np.uint8))
    assert result["verdict"] == "unsure" and result["kind"] == "document"


def test_the_evidence_travels_with_the_verdict():
    result = sort_scan(typed_page())
    assert set(result) >= {"verdict", "kind", "text_area", "text_pieces", "dark_share", "detector"}


def test_without_the_model_nothing_is_guessed(monkeypatch):
    monkeypatch.setattr(text_presence, "_net", None)
    monkeypatch.setattr(text_presence, "_net_failed", True)
    assert sort_scan(typed_page()) is None


def test_the_model_ships_with_the_code():
    assert text_presence.MODEL_PATH.is_file()
    assert text_presence.MODEL_PATH.stat().st_size < 5_000_000
