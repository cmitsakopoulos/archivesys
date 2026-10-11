"""Tests for OpenCV historical scan preprocessor."""

import numpy as np
import cv2
import pytest
from src.ingestion.preprocess import HistoricalPreprocessor


def test_clahe_and_adaptive_binarization(tmp_path):
    preprocessor = HistoricalPreprocessor()

    # Create synthetic test image with text and uneven background
    img = np.full((300, 400, 3), 230, dtype=np.uint8)
    cv2.putText(img, "TEST POLYTONIC 1923", (50, 150), cv2.FONT_HERSHEY_SIMPLEX, 1, (20, 20, 20), 2)
    test_path = tmp_path / "test_scan.png"
    cv2.imwrite(str(test_path), img)

    result = preprocessor.process(test_path, deskew=True)

    assert "original" in result
    assert "enhanced" in result
    assert "binarized" in result
    assert result["binarized"].shape == (300, 400)
    # Binarized should only contain 0 and 255
    unique_vals = set(np.unique(result["binarized"]))
    assert unique_vals.issubset({0, 255})


def test_thumbnail_creation():
    preprocessor = HistoricalPreprocessor()
    large = np.zeros((1600, 1200, 3), dtype=np.uint8)
    thumb = preprocessor.create_thumbnail(large, max_dim=400)
    assert max(thumb.shape[:2]) == 400
