"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _work_in_a_scratch_folder(tmp_path, monkeypatch):
    """Run every test from its own temporary folder.

    Ingest writes thumbnails and uploads under data/ relative to the working
    directory. Run from the project, every test run left scans and thumbnails
    in the real archive's data/ folder.
    """
    monkeypatch.chdir(tmp_path)
