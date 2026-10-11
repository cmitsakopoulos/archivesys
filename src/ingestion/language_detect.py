"""Local script and language identifier for historical multilingual archives.

Distinguishes among:
- 'el-poly': Polytonic Greek (Katharevousa)
- 'el': Modern Greek (Dimotiki / Monotonic)
- 'fr': French
- 'en': English
- 'mixed': Multi-script / Mixed language
"""

from __future__ import annotations
import re
import unicodedata
from typing import Dict, Any
from src.ingestion.polytonic import PolytonicNormalizer

# Common historical Katharevousa markers & prepositions
KATHAREVOUSA_MARKERS = {
    "εν", "του", "της", "των", "ταις", "τοις", "προς", "κατα", "δια",
    "βασιλειον", "πιστοποιητικον", "νομαρχια", "δημαρχια", "ληξιαρχικον",
    "γεννησεως", "γαμου", "θανατου", "προξενειον", "επιστολη", "αριθμος",
    "ετους", "μηνος", "υπογεγραμμενος", "μαρτυρες"
}

# French archival markers
FRENCH_MARKERS = {
    "le", "la", "les", "de", "du", "des", "et", "en", "dans", "pour",
    "republique", "consulat", "passeport", "naissance", "mariage",
    "etat", "civil", "certificat", "soussigne", "temoins"
}

# English archival markers
ENGLISH_MARKERS = {
    "the", "of", "and", "in", "to", "for", "with", "by", "on",
    "certificate", "birth", "marriage", "death", "kingdom", "republic",
    "consulate", "department", "register", "witnesses", "passport"
}


class LanguageDetector:
    """Classifies document language without requiring any external cloud APIs."""

    @classmethod
    def detect(cls, text: str) -> Dict[str, Any]:
        """Detect the primary language, secondary languages, and script breakdown.
        
        Returns a dictionary:
          - 'primary_language': 'el-poly' | 'el' | 'fr' | 'en' | 'mixed'
          - 'has_polytonic': bool
          - 'confidence': float (0.0 - 1.0)
          - 'scripts': list[str]
          - 'languages_detected': list[str]
        """
        if not text or not text.strip():
            return {
                "primary_language": "el-poly",
                "has_polytonic": False,
                "confidence": 0.5,
                "scripts": [],
                "languages_detected": ["el-poly"],
            }

        total_letters = 0
        greek_count = 0
        latin_count = 0
        has_polytonic = PolytonicNormalizer.is_polytonic(text)

        for char in text:
            if not char.isalpha():
                continue
            total_letters += 1
            name = unicodedata.name(char, "")
            if "GREEK" in name:
                greek_count += 1
            elif "LATIN" in name:
                latin_count += 1

        if total_letters == 0:
            return {
                "primary_language": "el-poly",
                "has_polytonic": False,
                "confidence": 0.5,
                "scripts": [],
                "languages_detected": ["el-poly"],
            }

        greek_ratio = greek_count / total_letters
        latin_ratio = latin_count / total_letters

        detected_langs = []
        primary_lang = "el-poly"
        confidence = 0.8

        # Normalized word tokens for marker matching
        search_key = PolytonicNormalizer.normalize_search(text)
        tokens = set(re.findall(r"\b\w+\b", search_key))

        kath_hits = len(tokens.intersection(KATHAREVOUSA_MARKERS))
        fr_hits = len(tokens.intersection(FRENCH_MARKERS))
        en_hits = len(tokens.intersection(ENGLISH_MARKERS))

        # Decision tree
        if greek_ratio > 0.4 and latin_ratio > 0.3:
            # Significant bilingual presence
            primary_lang = "mixed"
            if has_polytonic:
                detected_langs.append("el-poly")
            else:
                detected_langs.append("el")
            if fr_hits > en_hits:
                detected_langs.append("fr")
            else:
                detected_langs.append("en")
            confidence = 0.85
        elif greek_ratio >= 0.5:
            if has_polytonic or kath_hits >= 2:
                primary_lang = "el-poly"
            else:
                primary_lang = "el"
            detected_langs.append(primary_lang)
            confidence = min(0.95, 0.7 + (greek_ratio * 0.25))
        elif latin_ratio >= 0.5:
            # Distinguish French vs English
            if fr_hits > en_hits or any(c in text for c in "éèêëçàâîïôùûÉÈÊËÇÀÂÎÏÔÙÛ"):
                primary_lang = "fr"
                detected_langs.append("fr")
                confidence = 0.85
            else:
                primary_lang = "en"
                detected_langs.append("en")
                confidence = 0.85
        else:
            primary_lang = "el-poly" if has_polytonic else "el"
            detected_langs.append(primary_lang)
            confidence = 0.6

        scripts = []
        if greek_count > 0:
            scripts.append("greek")
        if latin_count > 0:
            scripts.append("latin")

        return {
            "primary_language": primary_lang,
            "has_polytonic": has_polytonic,
            "confidence": round(confidence, 2),
            "scripts": scripts,
            "languages_detected": detected_langs,
        }
