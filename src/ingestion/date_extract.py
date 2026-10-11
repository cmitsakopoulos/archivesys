"""Find the dates a document states about itself.

Historical records almost always carry their own date, written on the page.
Arabic numerals are used across every language this archive transcribes --
Katharevousa, Dimotiki, French and English all write "1923" as 1923 -- so a
year is findable even when the surrounding words are not understood. The month
names differ, so they are matched per language, diacritics folded, with Greek
genitive forms (Μαΐου, Ἰανουαρίου) included because that is how a Greek
certificate actually dates itself: "τῇ 14ῃ Μαΐου 1923".

Nothing here writes to the archive. It proposes; the curator disposes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from datetime import date
from typing import Any, Dict, List, Optional

# A family archive is not a geological record. Anything outside this window is
# a page number, a document reference or a sum of money, not a year.
MIN_YEAR = 1500
MAX_YEAR = date.today().year


def _fold(text: str) -> str:
    """Strip diacritics and lowercase, so ΜΑΐΟΥ, Μαΐου and μαιου all match."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", stripped).lower().replace("ς", "σ")


# Month vocabularies, folded. Greek entries cover nominative and genitive,
# because a dateline uses the genitive and a heading uses the nominative.
_MONTH_WORDS: Dict[str, int] = {}


def _register(names: Dict[int, List[str]]) -> None:
    for num, words in names.items():
        for w in words:
            _MONTH_WORDS[_fold(w)] = num


_register({
    1: ["January", "Jan", "janvier", "janv", "Ιανουάριος", "Ιανουαρίου", "Ιαν"],
    2: ["February", "Feb", "février", "fevrier", "févr", "Φεβρουάριος", "Φεβρουαρίου", "Φεβ"],
    3: ["March", "Mar", "mars", "Μάρτιος", "Μαρτίου", "Μαρ"],
    4: ["April", "Apr", "avril", "Απρίλιος", "Απριλίου", "Απρ"],
    5: ["May", "mai", "Μάιος", "Μαΐου", "Μάϊος", "Μαΐου", "Μαι"],
    6: ["June", "Jun", "juin", "Ιούνιος", "Ιουνίου", "Ιουν"],
    7: ["July", "Jul", "juillet", "juil", "Ιούλιος", "Ιουλίου", "Ιουλ"],
    8: ["August", "Aug", "août", "aout", "Αύγουστος", "Αυγούστου", "Αυγ"],
    9: ["September", "Sept", "Sep", "septembre", "Σεπτέμβριος", "Σεπτεμβρίου", "Σεπ"],
    10: ["October", "Oct", "octobre", "Οκτώβριος", "Οκτωβρίου", "Οκτ"],
    11: ["November", "Nov", "novembre", "Νοέμβριος", "Νοεμβρίου", "Νοε"],
    12: ["December", "Dec", "décembre", "decembre", "déc", "Δεκέμβριος", "Δεκεμβρίου", "Δεκ"],
})

# Greek inflects its month names -- Μάιος, Μαΐου, Μάιον, Μαΐῳ -- and which form
# appears depends on the sentence. Matching the stem and letting the ending
# vary covers the whole paradigm without listing it.
_GREEK_STEMS: Dict[str, int] = {
    "ιανουαρι": 1, "φεβρουαρι": 2, "μαρτι": 3, "απριλι": 4, "μαι": 5, "μαϊ": 5,
    "ιουνι": 6, "ιουλι": 7, "αυγουστ": 8, "σεπτεμβρι": 9, "σεπτεμβρ": 9,
    "οκτωβρι": 10, "νοεμβρι": 11, "δεκεμβρι": 12,
}

_MONTH_ALTERNATION = "|".join(
    sorted(
        [re.escape(w) for w in _MONTH_WORDS]
        + [re.escape(stem) + r"[\u0370-\u03ff]{0,4}" for stem in _GREEK_STEMS],
        key=len, reverse=True,
    )
)

# Greek ordinals attach letters to the numeral: 14η, 3ην, 5ῃ, 1ον. English and
# French do the same with st/nd/rd/th and er/e.
_ORDINAL_TAIL = r"(?:[\u0370-\u03ff]{1,3}|st|nd|rd|th|er|e)?"


def _month_from(word: str) -> Optional[int]:
    """Resolve a matched month word, by exact name or by Greek stem."""
    folded = _fold(word)
    if folded in _MONTH_WORDS:
        return _MONTH_WORDS[folded]
    for stem, num in sorted(_GREEK_STEMS.items(), key=lambda kv: -len(kv[0])):
        if folded.startswith(stem):
            return num
    return None

# Words that mark a date as the document's own, rather than one it mentions.
_DATELINE_HINTS = [
    # Greek
    "εγεννηθη", "γεννησεωσ", "εξεδοθη", "εκδοθεν", "τη", "εν αθηναισ", "ληξιαρχ",
    # French
    "delivre", "delivree", "ne le", "nee le", "fait a", "le",
    # English
    "issued", "dated", "born", "arriving", "arrived", "this day of",
]

_APPROX_HINTS = ["circa", "ca.", "ca ", "about", "vers", "περι το", "περιπου", "~"]


@dataclass
class DateCandidate:
    """A date the page appears to state, with the words that suggested it."""
    edtf: str
    display: str
    precision: str          # "day" | "month" | "year"
    confidence: float       # 0..1
    evidence: str           # the snippet it came from
    source: str             # "transcription" | "filename"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _snippet(text: str, start: int, end: int, width: int = 34) -> str:
    a = max(0, start - width)
    b = min(len(text), end + width)
    out = text[a:b].replace("\n", " ").strip()
    return ("…" if a > 0 else "") + re.sub(r"\s+", " ", out) + ("…" if b < len(text) else "")


def _plausible_year(y: int) -> bool:
    return MIN_YEAR <= y <= MAX_YEAR


def _context_bonus(folded_context: str) -> float:
    return 0.15 if any(h in folded_context for h in _DATELINE_HINTS) else 0.0


def _is_approx(folded_context: str) -> bool:
    return any(h in folded_context for h in _APPROX_HINTS)


def extract_dates(text: Optional[str], filename: Optional[str] = None) -> List[Dict[str, Any]]:
    """Propose dates this document states, best first.

    Ranked by how specific the date is and how much it reads like a dateline.
    Returns plain dicts so the result can go straight out over the API.
    """
    candidates: List[DateCandidate] = []
    body = (text or "").strip()

    if body:
        folded = _fold(body)

        # --- 1. A full written date: "14 Μαΐου 1923", "le 12 Septembre 1922" ---
        pattern = re.compile(
            rf"(?:(\d{{1,2}}){_ORDINAL_TAIL}\s+)?({_MONTH_ALTERNATION})\s*,?\s*(\d{{4}})",
            re.IGNORECASE,
        )
        for m in pattern.finditer(folded):
            day_s, month_word, year_s = m.group(1), m.group(2), m.group(3)
            year = int(year_s)
            if not _plausible_year(year):
                continue
            month = _month_from(month_word)
            if not month:
                continue
            ctx = folded[max(0, m.start() - 40):m.end() + 10]
            approx = _is_approx(ctx)
            mark = "~" if approx else ""
            if day_s and 1 <= int(day_s) <= 31:
                candidates.append(DateCandidate(
                    edtf=f"{year:04d}-{month:02d}-{int(day_s):02d}{mark}",
                    display=f"{int(day_s)} {_MONTH_TITLE[month]} {year}",
                    precision="day",
                    confidence=min(1.0, 0.90 + _context_bonus(ctx)),
                    evidence=_snippet(body, m.start(), m.end()),
                    source="transcription",
                ))
            else:
                candidates.append(DateCandidate(
                    edtf=f"{year:04d}-{month:02d}{mark}",
                    display=f"{_MONTH_TITLE[month]} {year}",
                    precision="month",
                    confidence=min(1.0, 0.78 + _context_bonus(ctx)),
                    evidence=_snippet(body, m.start(), m.end()),
                    source="transcription",
                ))

        # --- 1b. Month-first, as English writes it: "October 24th, 1928" ---
        pattern_mf = re.compile(
            rf"({_MONTH_ALTERNATION})\s+(\d{{1,2}}){_ORDINAL_TAIL}\s*,?\s*(\d{{4}})",
            re.IGNORECASE,
        )
        for m in pattern_mf.finditer(folded):
            month_word, day_s, year_s = m.group(1), m.group(2), m.group(3)
            year, day_i = int(year_s), int(day_s)
            month = _month_from(month_word)
            if not month or not _plausible_year(year) or not (1 <= day_i <= 31):
                continue
            ctx = folded[max(0, m.start() - 40):m.end() + 10]
            candidates.append(DateCandidate(
                edtf=f"{year:04d}-{month:02d}-{day_i:02d}{'~' if _is_approx(ctx) else ''}",
                display=f"{day_i} {_MONTH_TITLE[month]} {year}",
                precision="day",
                confidence=min(1.0, 0.90 + _context_bonus(ctx)),
                evidence=_snippet(body, m.start(), m.end()),
                source="transcription",
            ))

        # --- 2. Numeric dates: 14/5/1923, 14-05-1923, 1923-05-14 ---
        for m in re.finditer(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", body):
            d_s, mo_s, y_s = m.groups()
            d_i, mo_i, y_i = int(d_s), int(mo_s), int(y_s)
            # Day/month order is ambiguous; every language here writes D/M/Y.
            if not (_plausible_year(y_i) and 1 <= mo_i <= 12 and 1 <= d_i <= 31):
                continue
            ctx = _fold(body[max(0, m.start() - 40):m.end() + 10])
            candidates.append(DateCandidate(
                edtf=f"{y_i:04d}-{mo_i:02d}-{d_i:02d}",
                display=f"{d_i} {_MONTH_TITLE[mo_i]} {y_i}",
                precision="day",
                confidence=min(1.0, 0.82 + _context_bonus(ctx)),
                evidence=_snippet(body, m.start(), m.end()),
                source="transcription",
            ))

        for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", body):
            y_i, mo_i, d_i = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if not (_plausible_year(y_i) and 1 <= mo_i <= 12 and 1 <= d_i <= 31):
                continue
            candidates.append(DateCandidate(
                edtf=f"{y_i:04d}-{mo_i:02d}-{d_i:02d}",
                display=f"{d_i} {_MONTH_TITLE[mo_i]} {y_i}",
                precision="day",
                confidence=0.88,
                evidence=_snippet(body, m.start(), m.end()),
                source="transcription",
            ))

        # --- 3. Bare years: the fallback that works in every language ---
        for m in re.finditer(r"\b(1[5-9]\d{2}|20\d{2})\b", body):
            year = int(m.group(1))
            if not _plausible_year(year):
                continue
            ctx = _fold(body[max(0, m.start() - 45):m.end() + 12])
            approx = _is_approx(ctx)
            candidates.append(DateCandidate(
                edtf=f"{year:04d}{'~' if approx else ''}",
                display=f"{'ca. ' if approx else ''}{year}",
                precision="year",
                confidence=min(1.0, 0.45 + _context_bonus(ctx) + (0.05 if approx else 0.0)),
                evidence=_snippet(body, m.start(), m.end()),
                source="transcription",
            ))

    # --- 4. The filename, which archivists date more often than they admit ---
    if filename:
        for m in re.finditer(r"(1[5-9]\d{2}|20\d{2})", filename):
            year = int(m.group(1))
            if _plausible_year(year):
                candidates.append(DateCandidate(
                    edtf=f"{year:04d}",
                    display=str(year),
                    precision="year",
                    confidence=0.40,
                    evidence=filename,
                    source="filename",
                ))

    return _rank(candidates)


_MONTH_TITLE = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]

_PRECISION_RANK = {"day": 3, "month": 2, "year": 1}


def _rank(candidates: List[DateCandidate]) -> List[Dict[str, Any]]:
    """Merge duplicates, prefer the most specific and best-supported reading."""
    best: Dict[str, DateCandidate] = {}
    counts: Dict[str, int] = {}
    for c in candidates:
        counts[c.edtf] = counts.get(c.edtf, 0) + 1
        prev = best.get(c.edtf)
        if prev is None or c.confidence > prev.confidence:
            best[c.edtf] = c

    # A date the page repeats is a date the page means.
    for edtf, c in best.items():
        if counts[edtf] > 1:
            c.confidence = min(1.0, c.confidence + 0.06 * (counts[edtf] - 1))

    # A year that is merely the prefix of a fuller reading is not a rival to it.
    fuller = {c.edtf[:4] for c in best.values() if c.precision in ("day", "month")}
    ranked = [c for c in best.values() if not (c.precision == "year" and c.edtf[:4] in fuller)]

    ranked.sort(key=lambda c: (-c.confidence, -_PRECISION_RANK[c.precision], c.edtf))
    return [c.to_dict() for c in ranked[:6]]
