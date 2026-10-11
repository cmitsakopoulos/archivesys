"""Local OCR engine abstraction for historical multilingual text extraction.

Implements:
- BaseOCREngine: Abstract contract.
- LocalTesseractEngine: Invokes Tesseract 5 with multi-pack language routing.
- LocalKrakenEngine: Pluggable interface for digital humanists' Kraken HTR.
- MockHistoricalEngine: High-fidelity offline engine for testing, development,
  and seeding without requiring model downloads or API keys.
"""

from __future__ import annotations
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, List, Optional
import numpy as np
import pytesseract
from PIL import Image

from src.ingestion.polytonic import PolytonicNormalizer
from src.ingestion.language_detect import LanguageDetector


@dataclass
class OCRExtractionResult:
    """Structured result from an OCR extraction run."""
    transcription: str
    search_content: str
    language: str
    confidence: float
    document_type: str
    suggested_date_edtf: Optional[str] = None
    suggested_location: Optional[str] = None
    suggested_people: List[str] = field(default_factory=list)
    engine_name: str = "unknown"
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


class BaseOCREngine(ABC):
    """Abstract base class for all OCR engines."""

    @abstractmethod
    def extract(
        self,
        image_or_path: np.ndarray | str | Path,
        preferred_lang: Optional[str] = None,
    ) -> OCRExtractionResult:
        """Transcribe an image and propose metadata."""
        pass


PROJECT_TESSDATA = Path(__file__).parent.parent.parent / "tessdata"


class OCRCancelled(Exception):
    """Raised when a caller asks a running extraction to stop."""


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, used to test how far a re-read strayed from the first pass."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


class LocalTesseractEngine(BaseOCREngine):
    """Local Tesseract 5 engine supporting multi-language packs and local tessdata."""

    def __init__(
        self,
        psm: int = 3,
        oem: int = 1,
        tessdata_dir: Optional[Path] = None,
        refine: bool = True,
        refine_threshold: float = 82.0,
        refine_margin: float = 6.0,
        drop_noise_lines: bool = True,
        cancelled: Optional[Any] = None,
    ):
        self.psm = psm
        self.oem = oem
        self.tessdata_dir = tessdata_dir or (PROJECT_TESSDATA if PROJECT_TESSDATA.exists() else None)
        self._available_langs = self._probe_languages()
        # Second-pass settings: re-read any line whose mean word confidence falls
        # below refine_threshold, and only accept the retry if it beats the
        # original by at least refine_margin.
        self.refine = refine
        self.refine_threshold = refine_threshold
        self.refine_margin = refine_margin
        self.drop_noise_lines = drop_noise_lines
        # Optional predicate the worker sets so a long run can be stopped.
        self.cancelled = cancelled

    def _probe_languages(self) -> List[str]:
        # If project tessdata directory exists, probe local .traineddata files
        if self.tessdata_dir and self.tessdata_dir.exists():
            local_langs = [p.stem for p in self.tessdata_dir.glob("*.traineddata")]
            if local_langs:
                return local_langs
        try:
            return pytesseract.get_languages()
        except Exception:
            return ["eng"]

    def _determine_lang_string(self, preferred_lang: Optional[str] = None) -> str:
        """Construct multi-language string based on what is locally installed.

        Latin packs are deliberately kept out of the Greek routes. Tesseract scores
        every pack in the string against each glyph, and Greek capitals are visually
        identical to Latin ones, so including 'eng'/'fra' makes it emit Latin
        homoglyphs inside Greek words (``Koptov`` for ``Κύριον``). Measured on a
        1969 Katharevousa letter: 'grc+ell+fra+eng' produced 19 Latin characters in
        an entirely Greek page and scored 7.90% CER against 6.52% for 'grc+ell'.

        'grc' alone is also not the right default for polytonic material of this era.
        It is trained on classical printed editions, whose letterforms differ sharply
        from a 20th-century Greek typewriter; pairing it with 'ell' recovers the
        modern letterforms while keeping polytonic diacritic coverage.
        """
        available = set(self._available_langs)

        def pick(*preferred: str) -> str:
            candidates = [l for l in preferred if l in available]
            if candidates:
                return "+".join(candidates)
            return "eng" if "eng" in available else (sorted(available)[0] if available else "eng")

        # Official Tesseract Polytonic Greek is 'grc' (Greek, Ancient/Polytonic)
        # Modern Greek is 'ell'
        if preferred_lang == "el-poly":
            return pick("grc", "ell")
        elif preferred_lang == "el":
            return pick("ell", "grc")
        elif preferred_lang == "fr":
            return pick("fra")
        elif preferred_lang == "en":
            return pick("eng")
        else:
            # No hint given. Greek is by far the dominant script in this archive and
            # mixing scripts costs accuracy, so default to the Greek pair rather
            # than stacking every installed pack.
            return pick("grc", "ell")

    def _config(self, psm: Optional[int] = None) -> str:
        parts = [f"--oem {self.oem}", f"--psm {psm if psm is not None else self.psm}"]
        if self.tessdata_dir and self.tessdata_dir.exists():
            parts.append(f'--tessdata-dir "{self.tessdata_dir}"')
        return " ".join(parts)

    @staticmethod
    def _to_gray_array(image_or_path: np.ndarray | str | Path) -> Optional[np.ndarray]:
        """Return a single-channel array for line cropping, or None if unavailable."""
        try:
            import cv2
        except ImportError:
            return None
        if isinstance(image_or_path, (str, Path)):
            arr = cv2.imread(str(image_or_path), cv2.IMREAD_GRAYSCALE)
            return arr
        if isinstance(image_or_path, np.ndarray):
            if image_or_path.ndim == 3:
                return cv2.cvtColor(image_or_path, cv2.COLOR_BGR2GRAY)
            return image_or_path
        return None

    def _read_lines(self, img, lang_str: str, psm: Optional[int] = None) -> List[Dict[str, Any]]:
        """Group Tesseract word boxes into lines with mean confidence and a bounding box."""
        data = pytesseract.image_to_data(
            img, lang=lang_str, config=self._config(psm), output_type=pytesseract.Output.DICT
        )
        grouped: Dict[Any, Dict[str, Any]] = {}
        for i, word in enumerate(data["text"]):
            if not word.strip():
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                continue
            if conf < 0:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            entry = grouped.setdefault(
                key, {"words": [], "confs": [], "x0": 1 << 30, "y0": 1 << 30, "x1": 0, "y1": 0}
            )
            entry["words"].append(word)
            entry["confs"].append(conf)
            entry["x0"] = min(entry["x0"], data["left"][i])
            entry["y0"] = min(entry["y0"], data["top"][i])
            entry["x1"] = max(entry["x1"], data["left"][i] + data["width"][i])
            entry["y1"] = max(entry["y1"], data["top"][i] + data["height"][i])

        lines = []
        for key in grouped:
            e = grouped[key]
            lines.append({
                "text": " ".join(e["words"]),
                "conf": sum(e["confs"]) / len(e["confs"]),
                "box": (int(e["x0"]), int(e["y0"]), int(e["x1"]), int(e["y1"])),
            })
        if not lines:
            return lines

        # Order geometrically (top-to-bottom, then left-to-right) rather than by
        # Tesseract's block numbering. A fold crease or a punch hole splits one
        # physical line into two blocks, and block order then emits the tail of a
        # sentence at the very end of the page.
        heights = sorted(box[3] - box[1] for box in (l["box"] for l in lines))
        band = max(1, heights[len(heights) // 2] // 2)
        lines.sort(key=lambda l: (l["box"][1] // band, l["box"][0]))
        return lines

    @staticmethod
    def _is_plausible(candidate: str, baseline: str) -> bool:
        """Reject second-pass output that looks like a hallucination rather than a fix.

        Tesseract's confidence is not trustworthy on its own here: on a line whose
        box clipped a punch hole it rated invented leading glyphs *higher* than the
        correct first-pass reading. These guards encode what a genuine correction
        looks like — it stays the same length, stays in the same script, and differs
        from the first pass by a handful of characters, not a rewrite.
        """
        if not candidate.strip():
            return False
        if baseline:
            ratio = len(candidate) / max(1, len(baseline))
            if not 0.7 <= ratio <= 1.4:
                return False
        letters = [c for c in candidate if c.isalpha()]
        if not letters:
            return False
        greek = sum(1 for c in letters if PolytonicNormalizer.is_polytonic(c) or "Ͱ" <= c <= "Ͽ")
        if greek / len(letters) < 0.8:
            return False
        if re.search(r"(.)\1{3,}", candidate):
            return False
        if baseline and _edit_distance(candidate, baseline) / max(1, len(baseline)) > 0.35:
            return False
        # A run of very short tokens is the signature of margin artefacts (punch
        # holes, staple shadows, fold lines) being decoded as characters.
        run = 0
        for token in candidate.split():
            run = run + 1 if len(token) <= 2 else 0
            if run >= 3:
                return False
        return True

    def _refine_line(self, gray: np.ndarray, box, baseline: str, lang_str: str, pad: int = 10):
        """Re-read one low-confidence line zoomed in, under several enhancements.

        The first pass optimises for layout over a whole page; a single line can
        afford to be read several times. Upscaling gives the LSTM more pixels per
        diacritic, which is where faded polytonic accents are won or lost.
        """
        import cv2

        h, w = gray.shape[:2]
        x0, y0, x1, y1 = box
        crop = gray[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)]
        if crop.size == 0:
            return None

        best = None
        for scale in (2.0, 3.0):
            big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            blurred = cv2.GaussianBlur(big, (0, 0), 1.2)
            variants = {
                "plain": big,
                "unsharp": cv2.addWeighted(big, 2.0, blurred, -1.0, 0),
                "clahe": cv2.bilateralFilter(
                    cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(big), 5, 40, 40
                ),
            }
            for variant_name, variant in variants.items():
                # psm 7 = one text line; psm 13 = raw line, bypassing layout heuristics.
                for psm in (7, 13):
                    try:
                        lines = self._read_lines(variant, lang_str, psm=psm)
                    except Exception:
                        continue
                    if not lines:
                        continue
                    text = PolytonicNormalizer.repair_ocr_homoglyphs(
                        " ".join(l["text"] for l in lines)
                    )
                    conf = sum(l["conf"] for l in lines) / len(lines)
                    if not self._is_plausible(text, baseline):
                        continue
                    if best is None or conf > best[0]:
                        best = (conf, text, f"{scale:g}x/{variant_name}/psm{psm}")
        return best

    @staticmethod
    def is_noise_line(text: str, conf: float, noise_conf: float = 60.0) -> bool:
        """True when a line looks like decoded page furniture rather than text.

        A printed logo, a punch hole, a fold crease or a signature flourish all
        produce short low-confidence gibberish (`{`, `ταν]`, `[-Ξ- -`). Left in,
        they are a fifth of the output on a letterheaded page and make an
        otherwise good transcription look broken.

        Deliberately conservative: a line is only dropped when it is BOTH
        low-confidence AND structurally implausible, so genuine short lines
        ('Κύριον', 'ΣΤΡΥΜΩΝ', 'Διατελῶ') are never discarded on length alone.
        """
        stripped = text.strip()
        if not stripped:
            return True

        # Checked before confidence, because Tesseract is *confidently* wrong on
        # isolated marks: a stray brace scored 75 and a fold crease 65 on a real
        # page. A line with no letter or digit anywhere is never text.
        if not any(c.isalnum() for c in stripped):
            return True

        # Near-zero confidence on a short line is the engine saying it could not
        # read the region at all. Length-gated so a long, genuinely difficult
        # line of text is never discarded on confidence alone.
        if conf < 25.0 and len(stripped) < 8:
            return True

        if conf >= noise_conf:
            return False
        # Keep anything with the shape of a word or a number. A run of three
        # letters covers real short lines; a run of two digits covers the
        # telephone and reference numbers common on letterheads. Debris scatters
        # single glyphs between punctuation and matches neither.
        has_word = re.search(r"[^\W\d_]{3,}", stripped) is not None
        has_number = re.search(r"\d{2,}", stripped) is not None
        return not (has_word or has_number)

    def extract(
        self,
        image_or_path: np.ndarray | str | Path,
        preferred_lang: Optional[str] = None,
        progress: Optional[Any] = None,
    ) -> OCRExtractionResult:
        """Run Tesseract extraction, optionally refining low-confidence lines.

        `progress` is an optional callable (stage: str, done: int, total: int)
        used to report real work. The refinement pass dominates the runtime, so
        it reports per line rather than pretending to advance on a timer.
        """
        def report(stage: str, done: int = 0, total: int = 0):
            if progress:
                try:
                    progress(stage, done, total)
                except Exception:
                    pass

        lang_str = self._determine_lang_string(preferred_lang)
        custom_config = self._config()

        if isinstance(image_or_path, (str, Path)):
            img = Image.open(str(image_or_path))
        elif isinstance(image_or_path, np.ndarray):
            if len(image_or_path.shape) == 3 and image_or_path.shape[2] == 3:
                import cv2
                img = Image.fromarray(cv2.cvtColor(image_or_path, cv2.COLOR_BGR2RGB))
            else:
                img = Image.fromarray(image_or_path)
        else:
            raise TypeError("Unsupported image type for Tesseract")

        refined_count = 0
        dropped_count = 0
        report("Reading page layout")
        try:
            lines = self._read_lines(img, lang_str)
        except Exception:
            lines = []

        if lines:
            gray = self._to_gray_array(image_or_path) if self.refine else None
            candidates = [l for l in lines if gray is not None and l["conf"] < self.refine_threshold]
            total_to_refine = len(candidates)
            done = 0

            texts, confs = [], []
            for line in lines:
                if self.cancelled and self.cancelled():
                    raise OCRCancelled("Cancelled during recognition")
                if gray is None or line["conf"] >= self.refine_threshold:
                    texts.append(line["text"])
                    confs.append(line["conf"])
                    continue
                done += 1
                report("Re-reading unclear lines", done, total_to_refine)
                better = self._refine_line(gray, line["box"], line["text"], lang_str)
                if better and better[0] >= line["conf"] + self.refine_margin:
                    refined_count += 1
                    texts.append(better[1])
                    confs.append(better[0])
                else:
                    texts.append(line["text"])
                    confs.append(line["conf"])

            if self.drop_noise_lines:
                kept = [(t, c) for t, c in zip(texts, confs) if not self.is_noise_line(t, c)]
                dropped_count = len(texts) - len(kept)
                if kept:
                    texts, confs = [t for t, _ in kept], [c for _, c in kept]

            raw_text = "\n".join(texts)
            # Report the confidence of the text actually returned, so the curation
            # queue prioritises by what a reviewer will really see.
            confidence = (sum(confs) / len(confs)) / 100.0
        else:
            # No word boxes (blank page, or image_to_data unavailable): fall back
            # to a plain page read rather than returning nothing.
            raw_text = pytesseract.image_to_string(img, lang=lang_str, config=custom_config)
            # Nothing read is nothing to be confident about: a photograph used
            # to come back empty "at 75%".
            confidence = 0.75 if raw_text.strip() else 0.0

        exact_text = PolytonicNormalizer.normalize_exact(raw_text)
        search_content = PolytonicNormalizer.normalize_search(exact_text)
        lang_info = LanguageDetector.detect(exact_text)

        return OCRExtractionResult(
            transcription=exact_text,
            search_content=search_content,
            language=lang_info["primary_language"],
            confidence=round(confidence, 2),
            document_type="document",
            engine_name=f"tesseract({lang_str})",
            raw_metadata={
                "detected_scripts": lang_info["scripts"],
                "lines": len(lines),
                "refined_lines": refined_count,
                "dropped_noise_lines": dropped_count,
            },
        )


class LocalKrakenEngine(BaseOCREngine):
    """Local Kraken engine interface for historical typography and complex line HTR."""

    def __init__(self, model_path: Optional[str] = None):
        self.model_path = model_path

    def extract(
        self,
        image_or_path: np.ndarray | str | Path,
        preferred_lang: Optional[str] = None,
    ) -> OCRExtractionResult:
        """Extract historical text using Kraken (or graceful fallback to Tesseract)."""
        try:
            import kraken  # type: ignore
            # When Kraken is configured with models:
            raise NotImplementedError("Kraken model inference hook")
        except (ImportError, NotImplementedError):
            # Fall back to local Tesseract if Kraken package or model not present
            tesseract_fallback = LocalTesseractEngine()
            result = tesseract_fallback.extract(image_or_path, preferred_lang)
            result.engine_name = "kraken-fallback-tesseract"
            return result


class MockHistoricalEngine(BaseOCREngine):
    """Deterministic offline engine producing authentic polytonic Greek, French, and English records.
    
    Used for unit testing, CI/CD, demonstration workflows, and initial seeding without
    requiring model downloads or external tools.
    """

    SAMPLE_CORPUS = [
        {
            "match": "birth",
            "transcription": (
                "ΒΑΣΙΛΕΙΟΝ ΤΗΣ ΕΛΛΑΔΟΣ\n"
                "ΔΗΜΑΡΧΙΑ ΑΘΗΝΑΙΩΝ — ΛΗΞΙΑΡΧΕΙΟΝ\n"
                "Πιστοποιητικὸν Γεννήσεως ὑπ' ἀριθ. 1428/1923\n\n"
                "Πιστοποιοῦμεν ὅτι ἐν Ἀθήναις τῇ δεκάτῃ τετάρτῃ (14) Μαΐου τοῦ ἔτους 1923\n"
                "ἐγεννήθη ἄρρεν τέκνον ὑπὸ τῶν νομίμων συζύγων Γεωργίου Θεοδώρου,\n"
                "ἐμπόρου, καὶ Ἑλένης τὸ γένος Βασιλείου Παπαδοπούλου.\n"
                "Τὸ παιδίον ὠνομάσθη Χρῆστος.\n\n"
                "Ἐν Ἀθήναις, τῇ 20ῇ Μαΐου 1923\n"
                "Ὁ Ληξίαρχος Ἀθηνῶν"
            ),
            "language": "el-poly",
            "document_type": "certificate",
            "suggested_date_edtf": "1923-05-14",
            "suggested_location": "Ἀθῆναι (Athens)",
            "suggested_people": ["Χρῆστος Θεοδώρου", "Γεώργιος Θεοδώρου", "Ἑλένη Θεοδώρου"],
            "confidence": 0.98,
        },
        {
            "match": "passport",
            "transcription": (
                "RÉPUBLIQUE FRANÇAISE\n"
                "CONSULAT GÉNÉRAL DE FRANCE À SMYRNE\n"
                "Passeport Consulaire N° 8492 / Année 1922\n\n"
                "Au nom de la République Française,\n"
                "Nous prions les Autorités Civiles et Militaires de laisser passer librement:\n"
                "Monsieur Georges Dupont, négociant, né à Smyrne,\n"
                "accompagné de son épouse Hélène et de sa famille.\n"
                "Motif du voyage: Rapatriement et évacuation sanitaire vers le Pirée.\n\n"
                "Délivré à Smyrne, le 12 Septembre 1922.\n"
                "Le Consul Général de France"
            ),
            "language": "fr",
            "document_type": "passport",
            "suggested_date_edtf": "1922-09-12",
            "suggested_location": "Smyrne (Izmir)",
            "suggested_people": ["Georges Dupont", "Hélène Dupont"],
            "confidence": 0.96,
        },
        {
            "match": "manifest",
            "transcription": (
                "LIST OR MANIFEST OF ALIEN PASSENGERS FOR THE UNITED STATES\n"
                "IMMIGRATION OFFICER AT PORT OF ARRIVAL: NEW YORK, S.S. BYRON\n"
                "Arriving at Port of New York, October 24th, 1928\n\n"
                "Line 17: Papadopoulos, Ioannis. Age: 25 yrs. Sex: Male. Nationality: Greece.\n"
                "Last permanent residence: Athens, Greece.\n"
                "Father: Georgios Papadopoulos, Athens.\n"
                "Final Destination: Chicago, Illinois.\n"
                "Passage paid by: Self."
            ),
            "language": "en",
            "document_type": "immigration_manifest",
            "suggested_date_edtf": "1928-10-24",
            "suggested_location": "New York, USA",
            "suggested_people": ["Ioannis Papadopoulos", "Georgios Papadopoulos"],
            "confidence": 0.95,
        },
        {
            "match": "photo",
            "transcription": (
                "Οἰκογενειακὴ Φωτογραφία ἐν τῷ κήπῳ τοῦ οἴκου ἡμῶν ἐν Πειραιεῖ.\n"
                "Ἀπὸ ἀριστερῶν: Ἄννα Παπαδοπούλου, ὁ μικρὸς Ἰωάννης καὶ ὁ Δημήτριος.\n"
                "«Εἰς ἀνάμνησιν τῆς εἰρηνικῆς περιόδου πρὸ τοῦ μεγάλου πολέμου, περὶ τὸ 1935»."
            ),
            "language": "el-poly",
            "document_type": "photograph",
            "suggested_date_edtf": "1935~",
            "suggested_location": "Πειραιεύς (Piraeus)",
            "suggested_people": ["Δημήτριος Παπαδόπουλος", "Ἄννα Παπαδοπούλου", "Ἰωάννης Παπαδόπουλος"],
            "confidence": 0.92,
        },
    ]

    def extract(
        self,
        image_or_path: np.ndarray | str | Path,
        preferred_lang: Optional[str] = None,
    ) -> OCRExtractionResult:
        """Return structured historical transcription matching file name or cyclical corpus."""
        path_str = str(image_or_path).lower()
        selected = self.SAMPLE_CORPUS[0]

        for sample in self.SAMPLE_CORPUS:
            if sample["match"] in path_str:
                selected = sample
                break
            if preferred_lang and sample["language"] == preferred_lang:
                selected = sample
                break

        exact = PolytonicNormalizer.normalize_exact(selected["transcription"])
        search_key = PolytonicNormalizer.normalize_search(exact)

        return OCRExtractionResult(
            transcription=exact,
            search_content=search_key,
            language=selected["language"],
            confidence=selected["confidence"],
            document_type=selected["document_type"],
            suggested_date_edtf=selected["suggested_date_edtf"],
            suggested_location=selected["suggested_location"],
            suggested_people=list(selected["suggested_people"]),
            engine_name="mock-historical-engine",
            raw_metadata={"is_synthetic_archival": True},
        )


def build_tuned_pipeline(engine_type: str = "auto"):
    """Build a (preprocessor, engine, settings) triple from the saved settings.

    Both the CLI and the curation server go through here, so a change made in the
    Settings view takes effect everywhere instead of only in the browser.
    """
    from src.config import load_settings
    from src.ingestion.preprocess import HistoricalPreprocessor

    cfg = load_settings()
    preprocessor = HistoricalPreprocessor(
        clahe_clip_limit=cfg.clahe_clip_limit,
        deskew_deadband=cfg.deskew_deadband,
    )
    engine = get_ocr_engine(engine_type)
    if isinstance(engine, LocalTesseractEngine):
        tess_dir = cfg.tessdata_path()
        if tess_dir.exists():
            engine.tessdata_dir = tess_dir
            engine._available_langs = engine._probe_languages()
        engine.refine = cfg.refine_enabled
        engine.refine_threshold = cfg.refine_threshold
        engine.refine_margin = cfg.refine_margin
        engine.drop_noise_lines = cfg.drop_noise_lines
    return preprocessor, engine, cfg


def get_ocr_engine(engine_type: str = "auto") -> BaseOCREngine:
    """Factory returning the desired OCR engine."""
    if engine_type == "mock":
        return MockHistoricalEngine()
    if engine_type == "kraken":
        return LocalKrakenEngine()
    if engine_type == "tesseract":
        return LocalTesseractEngine()
    # 'auto' chooses Tesseract if available, otherwise Mock
    try:
        tess = LocalTesseractEngine()
        available = set(tess._available_langs)
        if "grc" in available or "ell" in available or "eng" in available:
            return tess
        return MockHistoricalEngine()
    except Exception:
        return MockHistoricalEngine()
