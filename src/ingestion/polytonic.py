"""Polytonic Greek and multilingual Unicode normalization engine.

Preserves exact polytonic Katharevousa, Modern Greek, French, and English
transcriptions in NFC format while compiling canonical, diacritic-insensitive,
sigma-folded search keys for SQLite FTS5 and client-side search.
"""

from __future__ import annotations
import re
import unicodedata
from typing import Set

# Specific Greek characters
GREEK_FINAL_SIGMA = "\u03c2"  # ς
GREEK_SMALL_SIGMA = "\u03c3"  # σ
GREEK_ANO_TELEIA = "\u0387"   # ·
GREEK_QUESTION_MARK = "\u037e"  # ;

# Polytonic Greek Unicode blocks
POLYTONIC_RANGES = [
    (0x0370, 0x03FF),  # Greek and Coptic
    (0x1F00, 0x1FFF),  # Greek Extended (Polytonic vowels, breathings, accents)
]

# Codepoints that render as Greek letters but are not the canonical Greek letter.
# Tesseract's 'ell' pack emits MICRO SIGN for mu; NFC does not repair it (only the
# lossy NFKC does), so an unrepaired transcription silently fails to match a user
# who types a real mu in the FTS5 search box.
GREEK_LOOKALIKES = {
    "µ": "μ",  # MICRO SIGN            -> GREEK SMALL LETTER MU
    "ϐ": "β",  # GREEK BETA SYMBOL     -> BETA
    "ϑ": "θ",  # GREEK THETA SYMBOL    -> THETA
    "ϕ": "φ",  # GREEK PHI SYMBOL      -> PHI
    "ϰ": "κ",  # GREEK KAPPA SYMBOL    -> KAPPA
    "ϱ": "ρ",  # GREEK RHO SYMBOL      -> RHO
    "ϲ": "σ",  # GREEK LUNATE SIGMA    -> SIGMA
    "Ω": "Ω",  # OHM SIGN              -> OMEGA
}

# Latin letters whose glyphs are indistinguishable from Greek capitals. Tesseract
# emits these whenever 'eng'/'fra' share the language string with a Greek pack.
# Only ever applied inside a token that is already predominantly Greek.
LATIN_TO_GREEK = {
    "A": "Α", "B": "Β", "E": "Ε", "H": "Η", "I": "Ι", "K": "Κ", "M": "Μ",
    "N": "Ν", "O": "Ο", "P": "Ρ", "T": "Τ", "X": "Χ", "Y": "Υ", "Z": "Ζ",
    "o": "ο", "v": "ν", "p": "ρ", "u": "υ", "y": "γ",
}

_GREEK_CHAR = re.compile(r"[Ͱ-Ͽἀ-῿]")


class PolytonicNormalizer:
    """Normalizes multilingual historical text across Polytonic Greek, French, and English."""

    @staticmethod
    def repair_ocr_homoglyphs(text: str) -> str:
        """Fold OCR lookalike codepoints onto their canonical Greek letters.

        Two distinct failure modes, both invisible on screen:

        1. Symbol codepoints that render identically to a Greek letter (MICRO SIGN
           for mu, BETA SYMBOL for beta). NFC leaves these alone.
        2. Latin capitals standing in for Greek ones (``Koptov`` for ``Κύριον``),
           emitted when a Latin pack shares the OCR language string.

        The Latin substitution is applied only within tokens that are already
        majority-Greek, so genuinely Latin words in a mixed-language document
        (French and English records in the same archive) are left untouched.
        """
        if not text:
            return ""

        for lookalike, canonical in GREEK_LOOKALIKES.items():
            text = text.replace(lookalike, canonical)

        repaired = []
        for token in re.split(r"(\s+)", text):
            letters = [c for c in token if c.isalpha()]
            if letters:
                greek = sum(1 for c in letters if _GREEK_CHAR.match(c))
                if greek and greek / len(letters) >= 0.5:
                    token = "".join(LATIN_TO_GREEK.get(c, c) for c in token)
            repaired.append(token)
        return "".join(repaired)

    @classmethod
    def normalize_exact(cls, text: str) -> str:
        """Return the exact, canonical precomposed Unicode representation (NFC).

        Preserves all historical diacritics (oxia, varia, perispomeni, dasia, psili,
        iota subscript) and Latin accents verbatim for archival preservation.
        """
        if not text:
            return ""
        return unicodedata.normalize("NFC", cls.repair_ocr_homoglyphs(text)).strip()

    @classmethod
    def normalize_search(cls, text: str) -> str:
        """Generate a diacritic-folded, case-folded, sigma-normalized search index.
        
        Steps:
        1. NFD decomposition to separate base letters from combining diacritics.
        2. Filter out nonspacing combining marks (category 'Mn') to strip accents,
           breathing marks, and iota subscripts (including Greek, French, Latin).
        3. Convert Greek final sigma (ς) to medial sigma (σ).
        4. Normalize Greek punctuation (ano teleia to colon, Greek question to semicolon).
        5. Lowercase and collapse consecutive whitespace.
        """
        if not text:
            return ""

        # Step 0: fold OCR lookalikes, so a search key built from an unrepaired
        # transcription still matches one built from a repaired one.
        text = cls.repair_ocr_homoglyphs(text)

        # Step 1 & 2: NFD decomposition & remove combining marks
        decomposed = unicodedata.normalize("NFD", text)
        stripped = "".join(
            c for c in decomposed if unicodedata.category(c) != "Mn"
        )

        # Step 3: Greek sigma folding & lowercase
        folded = stripped.lower()
        folded = folded.replace(GREEK_FINAL_SIGMA, GREEK_SMALL_SIGMA)

        # Step 4: Punctuation normalization
        folded = folded.replace(GREEK_ANO_TELEIA, ":")
        folded = folded.replace(GREEK_QUESTION_MARK, ";")

        # Step 5: Collapse whitespace
        cleaned = re.sub(r"\s+", " ", folded).strip()
        return cleaned

    @classmethod
    def is_polytonic(cls, text: str) -> bool:
        """Check whether the given text contains polytonic Greek extended characters or diacritics."""
        if not text:
            return False
        for char in text:
            code = ord(char)
            # Greek Extended block
            if 0x1F00 <= code <= 0x1FFF:
                return True
        # Check for decomposed polytonic marks in NFD
        decomposed = unicodedata.normalize("NFD", text)
        for char in decomposed:
            # Combining Greek Perispomeni, Dasia, Psili, etc.
            if ord(char) in {0x0313, 0x0314, 0x0342, 0x0345}:
                return True
        return False

    @classmethod
    def detect_scripts(cls, text: str) -> Set[str]:
        """Identify which writing systems are present in the text."""
        scripts: Set[str] = set()
        if not text:
            return scripts

        for char in text:
            name = unicodedata.name(char, "")
            if "GREEK" in name:
                scripts.add("greek")
            elif "LATIN" in name:
                scripts.add("latin")
            elif "CYRILLIC" in name:
                scripts.add("cyrillic")

        return scripts
