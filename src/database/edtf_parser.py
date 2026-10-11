"""Extended Date/Time Format (EDTF) parser for historical timeline projections.

Handles:
- Exact dates (e.g. "1923-05-14", "1923-05", "1923")
- Approximate / Uncertain dates (e.g. "1923~", "1923?", "1923%")
- Intervals and ranges (e.g. "1912-05/1912-08", "1920/1925")
- Unspecified decades (e.g. "192X" -> 1920-1929)
- Open intervals (e.g. "/1940", "1920/")

Produces mathematical float years for sorting and valid ISO strings for Vis-Timeline.
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional, Tuple

MONTH_NAMES = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
]


class NotADate(ValueError):
    """Raised for a string that is not storable EDTF.

    The parser used to answer every unreadable string with 1 January 1900 --
    a real-looking date, with a real sort position, that went on to publish.
    "14/5/1923" was read as the year 14. A date the archive cannot read must
    be refused, so the document stays honestly undated.
    """


@dataclass
class ParsedEDTF:
    """Structured representation of an EDTF date expression."""
    raw: str
    sort_start: float
    sort_end: Optional[float]
    iso_start: str
    iso_end: Optional[str]
    date_display: str
    is_approximate: bool
    is_uncertain: bool
    is_range: bool


class EDTFParser:
    """Parses historical date expressions conforming to EDTF specifications."""

    # What EDTF actually permits in the subset this archive stores.
    _SINGLE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
    _DECADE = re.compile(r"^\d{3}[xX]$")
    _CENTURY = re.compile(r"^\d{2}[xX]{2}$")

    @classmethod
    def is_valid(cls, edtf_string: str) -> bool:
        """Whether this string is EDTF the archive can store without guessing."""
        try:
            cls.parse(edtf_string, strict=True)
            return True
        except NotADate:
            return False

    @classmethod
    def _validate(cls, part: str) -> None:
        """Reject anything that is not a date, rather than inventing one."""
        clean = part.strip().rstrip("~?%")
        if not clean:
            raise NotADate("empty date component")
        if cls._DECADE.match(clean) or cls._CENTURY.match(clean):
            return
        if not cls._SINGLE.match(clean):
            raise NotADate(f"{part!r} is not an EDTF date")
        bits = clean.split("-")
        if len(bits) > 1 and not (1 <= int(bits[1]) <= 12):
            raise NotADate(f"{bits[1]} is not a month")
        if len(bits) > 2 and not (1 <= int(bits[2]) <= 31):
            raise NotADate(f"{bits[2]} is not a day of the month")

    @classmethod
    def _parse_single_date(cls, s: str) -> Tuple[int, int, int, bool, bool]:
        """Parse a single date component like '1923~', '1923-05?', '192X'.
        
        Returns: (year, month, day, is_approx, is_uncertain)
        """
        s = s.strip()
        is_approx = "~" in s or "%" in s
        is_uncertain = "?" in s or "%" in s
        clean = s.rstrip("~?%")

        # Decade handling: 192X -> 1920
        if re.match(r"^\d{3}[xX]$", clean):
            year = int(clean[:3]) * 10
            return (year, 1, 1, True, is_uncertain)

        # Century handling: 19XX -> 1900
        if re.match(r"^\d{2}[xX]{2}$", clean):
            year = int(clean[:2]) * 100
            return (year, 1, 1, True, is_uncertain)

        parts = clean.split("-")
        year = int(parts[0]) if parts[0].lstrip("-").isdigit() else 1900
        month = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        day = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1

        return (year, month, day, is_approx, is_uncertain)

    @classmethod
    def _to_float_year(cls, year: int, month: int = 1, day: int = 1) -> float:
        """Convert year, month, day into a continuous float."""
        month_offset = max(0, min(11, month - 1)) / 12.0
        day_offset = max(0, min(30, day - 1)) / 365.25
        return round(year + month_offset + day_offset, 3)

    @classmethod
    def parse(cls, edtf_string: str, strict: bool = False) -> ParsedEDTF:
        """Parse an EDTF string into a structured ParsedEDTF object.

        With strict=True, raises NotADate rather than answering with a date it
        made up. Callers that write to the database always pass strict=True.
        """
        if strict:
            raw_in = (edtf_string or "").strip()
            if not raw_in:
                raise NotADate("no date given")
            if "/" in raw_in:
                left, right = raw_in.split("/", 1)
                if not left and not right:
                    raise NotADate("an interval needs at least one end")
                for part in (left, right):
                    if part:
                        cls._validate(part)
                if left and right:
                    ly = int(left.strip().rstrip("~?%")[:4])
                    ry = int(right.strip().rstrip("~?%")[:4])
                    if ly > ry:
                        raise NotADate("an interval cannot end before it starts")
            else:
                cls._validate(raw_in)

        if not edtf_string or not edtf_string.strip():
            return ParsedEDTF(
                raw="",
                sort_start=1900.0,
                sort_end=None,
                iso_start="1900-01-01",
                iso_end=None,
                date_display="Date unknown",
                is_approximate=True,
                is_uncertain=True,
                is_range=False,
            )

        raw = edtf_string.strip()

        # Check for range: 'date1/date2'
        if "/" in raw:
            p1, p2 = raw.split("/", 1)
            y1, m1, d1, a1, u1 = cls._parse_single_date(p1) if p1 else (1900, 1, 1, True, False)
            y2, m2, d2, a2, u2 = cls._parse_single_date(p2) if p2 else (1950, 1, 1, True, False)

            sort_start = cls._to_float_year(y1, m1, d1)
            sort_end = cls._to_float_year(y2, m2, d2)
            iso_start = f"{y1:04d}-{m1:02d}-{d1:02d}"
            iso_end = f"{y2:04d}-{m2:02d}-{d2:02d}"

            # Format human display
            if not p1:
                display = f"Before {y2}"
            elif not p2:
                display = f"After {y1}"
            elif y1 == y2:
                display = f"{MONTH_NAMES[m1]} – {MONTH_NAMES[m2]} {y1}"
            else:
                display = f"{y1} – {y2}"

            return ParsedEDTF(
                raw=raw,
                sort_start=sort_start,
                sort_end=sort_end,
                iso_start=iso_start,
                iso_end=iso_end,
                date_display=display,
                is_approximate=a1 or a2,
                is_uncertain=u1 or u2,
                is_range=True,
            )

        # Single date parsing
        y, m, d, is_approx, is_uncertain = cls._parse_single_date(raw)
        sort_start = cls._to_float_year(y, m, d)
        iso_start = f"{y:04d}-{m:02d}-{d:02d}"

        # Unspecified decade
        if re.match(r"^\d{3}[xX]$", raw.rstrip("~?%")):
            decade = (y // 10) * 10
            return ParsedEDTF(
                raw=raw,
                sort_start=float(decade),
                sort_end=float(decade + 9.99),
                iso_start=f"{decade:04d}-01-01",
                iso_end=f"{decade + 9:04d}-12-31",
                date_display=f"{decade}s",
                is_approximate=True,
                is_uncertain=is_uncertain,
                is_range=True,
            )

        # Formatting single date display
        parts = raw.rstrip("~?%").split("-")
        prefix = "ca. " if is_approx else ("?" if is_uncertain else "")

        if len(parts) == 3:
            display = f"{prefix}{d} {MONTH_NAMES[m]} {y}"
        elif len(parts) == 2:
            display = f"{prefix}{MONTH_NAMES[m]} {y}"
        else:
            display = f"{prefix}{y}"

        return ParsedEDTF(
            raw=raw,
            sort_start=sort_start,
            sort_end=None,
            iso_start=iso_start,
            iso_end=None,
            date_display=display,
            is_approximate=is_approx,
            is_uncertain=is_uncertain,
            is_range=False,
        )
