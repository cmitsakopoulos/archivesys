"""Tests for tunable pipeline settings and OCR homoglyph repair."""

import pytest

from src.config import PipelineSettings
from src.ingestion.polytonic import PolytonicNormalizer


def test_settings_clamp_hostile_values():
    """Settings arrive from a browser form, so out-of-range values must be caged."""
    wild = PipelineSettings(
        clahe_clip_limit=-99.0,
        deskew_deadband=500.0,
        refine_threshold=10_000.0,
        refine_margin=-5.0,
        explorer_page_size=999_999,
        ocr_input="../../etc/passwd",
        model_set="../../etc",
        default_lang="klingon",
    ).clamped()

    assert 0.5 <= wild.clahe_clip_limit <= 8.0
    assert 0.0 <= wild.deskew_deadband <= 10.0
    assert 0.0 <= wild.refine_threshold <= 100.0
    assert 0.0 <= wild.refine_margin <= 50.0
    assert 12 <= wild.explorer_page_size <= 500
    assert wild.ocr_input == "ocr_ready"
    assert wild.default_lang == "el-poly"
    # An unknown model set must never become a filesystem path outside the project.
    assert "/" not in wild.model_set and ".." not in wild.model_set


def test_settings_defaults_are_the_measured_ones():
    """The defaults encode what measured best on faded polytonic typescript."""
    s = PipelineSettings().clamped()
    assert s.ocr_input == "ocr_ready", "binarized input measured ~14x worse"
    assert s.clahe_clip_limit <= 2.0, "aggressive CLAHE amplifies paper grain"
    assert s.deskew_deadband > 0, "rotating a near-straight page costs sharpness"


def test_micro_sign_is_repaired_to_greek_mu():
    """Tesseract's 'ell' pack emits U+00B5; NFC does not fix it, so we must.

    Left unrepaired this silently breaks search: the stored transcription can
    never match a user typing a real mu.
    """
    from_ocr = "µέρους"   # µέρους with MICRO SIGN
    correct = "μέρους"    # μέρους with GREEK MU

    assert PolytonicNormalizer.normalize_exact(from_ocr) == correct
    assert (PolytonicNormalizer.normalize_search(from_ocr)
            == PolytonicNormalizer.normalize_search(correct))


@pytest.mark.parametrize("lookalike,canonical", [
    ("ϐ", "β"),  # GREEK BETA SYMBOL  -> beta
    ("ϑ", "θ"),  # GREEK THETA SYMBOL -> theta
    ("ϲ", "σ"),  # LUNATE SIGMA       -> sigma
    ("Ω", "Ω"),  # OHM SIGN           -> capital omega
])
def test_greek_symbol_variants_folded(lookalike, canonical):
    assert PolytonicNormalizer.repair_ocr_homoglyphs(lookalike) == canonical


def test_latin_homoglyphs_repaired_only_inside_greek_words():
    """'eng' in the language string makes Tesseract emit Latin capitals in Greek."""
    # Koptov: Latin K, o, p, v standing in for Κύριον's letterforms.
    repaired = PolytonicNormalizer.repair_ocr_homoglyphs("Kύpιoν")
    assert repaired == "Κύριον"


def test_genuinely_latin_text_is_left_alone():
    """A multilingual archive holds French and English records; don't corrupt them."""
    for text in ("RÉPUBLIQUE FRANÇAISE", "Passport", "Kop", "New York, October 1928"):
        assert PolytonicNormalizer.repair_ocr_homoglyphs(text) == text


def test_repair_is_idempotent():
    once = PolytonicNormalizer.repair_ocr_homoglyphs("µέρoυς")
    assert PolytonicNormalizer.repair_ocr_homoglyphs(once) == once


# --------------------------------------------------------------------------
# Noise-line filtering
# --------------------------------------------------------------------------

class TestNoiseLineFilter:
    """Page furniture (logos, punch holes, fold creases) decodes as gibberish.

    All confidences below are values Tesseract actually reported on a 1969
    letterheaded scan, including the awkward ones where it was confidently wrong.
    """

    from src.ingestion.ocr_engine import LocalTesseractEngine as _E

    @pytest.mark.parametrize("text,conf", [
        ("{", 75.0),            # a brace, scored 75 — confidence alone cannot catch it
        ("-", 65.0),            # fold crease, scored 65
        ("“Ὡ᾿ὡὩὩ", 0.0),        # printed logo
        ("[-Ξ- -", 0.0),        # signature flourish
        (": ᾿Ξ Ὅν", 28.0),
        ("κος", 18.0),
        ('»»"', 30.0),
        ("", 90.0),
    ])
    def test_debris_is_dropped(self, text, conf):
        assert self._E.is_noise_line(text, conf) is True

    @pytest.mark.parametrize("text,conf", [
        ("Κύριον", 55.0),                   # short but real
        ("ΣΤΡΥΜΩΝ", 28.0),                  # short, low confidence, still real
        ("Διατελῶ", 45.0),
        ("Μητσακόπουλον", 58.0),
        ("ΤΗΛΕΓΡΑΦΗΜΑΤΑ Β.Ο.Σ.", 50.0),
        ("δίκη δι’ ἀτύχημα 21/5/69).", 55.0),  # digits and punctuation
        ("29.02 & 39-95", 45.0),               # a telephone number, mostly digits
        ("ΕΡΓΟΣΤΑΣΙΟΥ 35.60", 42.0),
        ("ΑΔΕΛΦΩΝ Μ ΓΙ ΤΖΙΔΗ ος", 43.0),       # garbled but genuine
        ("Χαράλαμπος Μπιζίδης", 40.0),
    ])
    def test_real_text_survives(self, text, conf):
        assert self._E.is_noise_line(text, conf) is False, "must not discard real text"

    def test_high_confidence_text_is_never_dropped(self):
        assert self._E.is_noise_line("Πέραν τῶν προσωπικῶν", 92.0) is False


# --------------------------------------------------------------------------
# Job queue
# --------------------------------------------------------------------------

class TestJobQueue:
    def test_jobs_run_serially_and_report_results(self):
        import time
        from src.curation.jobs import JobQueue

        order = []

        def runner(job, progress, is_cancelled):
            order.append(job.id)
            progress("working", 1, 2)
            return {"characters": len(job.label)}

        q = JobQueue(runner)
        a = q.submit(kind="ingest", label="one.png", path="/tmp/one.png")
        b = q.submit(kind="ingest", label="two.png", path="/tmp/two.png")

        deadline = time.time() + 10
        while time.time() < deadline:
            snap = q.snapshot()
            if len({j["id"] for j in snap["recent"]}) == 2:
                break
            time.sleep(0.05)

        assert order == [a.id, b.id], "queue must preserve submission order"
        assert all(j["state"] == "done" for j in q.snapshot()["recent"])

    def test_queued_job_can_be_cancelled_before_it_starts(self):
        import threading
        import time
        from src.curation.jobs import JobQueue

        release = threading.Event()

        def runner(job, progress, is_cancelled):
            release.wait(timeout=5)
            return {}

        q = JobQueue(runner)
        q.submit(kind="ingest", label="blocking.png", path="/tmp/a.png")
        time.sleep(0.2)
        queued = q.submit(kind="ingest", label="second.png", path="/tmp/b.png")

        assert q.cancel(queued.id) is True
        release.set()

        deadline = time.time() + 5
        while time.time() < deadline:
            states = {j["id"]: j["state"] for j in q.snapshot()["recent"]}
            if queued.id in states:
                assert states[queued.id] == "cancelled"
                return
            time.sleep(0.05)
        pytest.fail("cancelled job never appeared in recent")

    def test_failure_is_recorded_not_raised(self):
        import time
        from src.curation.jobs import JobQueue

        def runner(job, progress, is_cancelled):
            raise ValueError("scan file vanished")

        q = JobQueue(runner)
        job = q.submit(kind="ingest", label="gone.png", path="/tmp/gone.png")

        deadline = time.time() + 5
        while time.time() < deadline:
            for j in q.snapshot()["recent"]:
                if j["id"] == job.id:
                    assert j["state"] == "failed"
                    assert "vanished" in j["error"]
                    return
            time.sleep(0.05)
        pytest.fail("failed job never recorded")
