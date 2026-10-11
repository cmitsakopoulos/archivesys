"""Persisted, user-tunable settings for the transcription pipeline.

Every value here changes OCR accuracy, and the right value is collection-specific:
what suits a 1969 typewritten Katharevousa letter is not what suits a 1923
letterpress certificate. Rather than baking constants into the engine, they are
stored in a small JSON file and exposed through the Settings view of the studio,
so a choice can be measured on a real scan and then kept.

Defaults below are the settings measured best on a faded polytonic typescript
(see docs in preprocess.py and ocr_engine.py for the supporting numbers).
"""

from __future__ import annotations
import json
import threading
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).parent.parent
SETTINGS_PATH = PROJECT_ROOT / "data" / "settings.json"

# Which OpenCV output is handed to the OCR engine. 'binarized' is retained only
# because it is occasionally right for very clean high-contrast print; it is a
# poor default and the UI labels it as such.
OCR_INPUTS = ["ocr_ready", "enhanced", "gray", "binarized"]

# How exports carry their scans (see EXPORT_QUALITIES in visualizer/compiler.py).
# Exports are passed between people, so the default keeps every original intact.
EXPORT_QUALITY_OPTIONS = ["original", "high", "compact"]


def available_model_sets() -> List[Dict[str, Any]]:
    """List the tessdata directories present in the project, newest style first."""
    found = []
    for name, label in (
        ("tessdata", "Standard (integerised, faster)"),
        ("tessdata_best", "Best (float, slower)"),
    ):
        d = PROJECT_ROOT / name
        if d.exists():
            langs = sorted(p.stem for p in d.glob("*.traineddata"))
            if langs:
                found.append({"name": name, "label": label, "languages": langs})
    return found


@dataclass
class PipelineSettings:
    """Tunable knobs for preprocessing, model routing and the refinement pass."""

    # --- model routing ---
    model_set: str = "tessdata"
    default_lang: str = "el-poly"

    # --- preprocessing ---
    ocr_input: str = "ocr_ready"
    clahe_clip_limit: float = 1.5
    deskew_enabled: bool = True
    deskew_deadband: float = 1.5

    # --- second pass ---
    refine_enabled: bool = True
    refine_threshold: float = 82.0
    refine_margin: float = 6.0
    drop_noise_lines: bool = True

    # --- explorer ---
    explorer_page_size: int = 60

    # --- export ---
    export_quality: str = "original"

    def clamped(self) -> "PipelineSettings":
        """Return a copy with every value forced into a sane range.

        Settings arrive from a browser form, so they are untrusted input; a
        negative CLAHE limit or a 10000-item page size should not reach OpenCV.
        """
        sets = {m["name"] for m in available_model_sets()} or {"tessdata"}
        return PipelineSettings(
            model_set=self.model_set if self.model_set in sets else sorted(sets)[0],
            default_lang=self.default_lang if self.default_lang in
                ("el-poly", "el", "fr", "en") else "el-poly",
            ocr_input=self.ocr_input if self.ocr_input in OCR_INPUTS else "ocr_ready",
            clahe_clip_limit=min(8.0, max(0.5, float(self.clahe_clip_limit))),
            deskew_enabled=bool(self.deskew_enabled),
            deskew_deadband=min(10.0, max(0.0, float(self.deskew_deadband))),
            refine_enabled=bool(self.refine_enabled),
            refine_threshold=min(100.0, max(0.0, float(self.refine_threshold))),
            refine_margin=min(50.0, max(0.0, float(self.refine_margin))),
            drop_noise_lines=bool(self.drop_noise_lines),
            explorer_page_size=min(500, max(12, int(self.explorer_page_size))),
            export_quality=self.export_quality if self.export_quality in
                EXPORT_QUALITY_OPTIONS else "original",
        )

    def tessdata_path(self) -> Path:
        return PROJECT_ROOT / self.model_set


_lock = threading.Lock()
_cached: PipelineSettings | None = None


def load_settings() -> PipelineSettings:
    """Read settings from disk, falling back to defaults for anything missing."""
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        data: Dict[str, Any] = {}
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        known = {f.name for f in fields(PipelineSettings)}
        _cached = PipelineSettings(**{k: v for k, v in data.items() if k in known}).clamped()
        return _cached


def save_settings(values: Dict[str, Any]) -> PipelineSettings:
    """Merge values over the current settings, persist, and return the result."""
    global _cached
    current = asdict(load_settings())
    known = {f.name for f in fields(PipelineSettings)}
    current.update({k: v for k, v in values.items() if k in known})
    merged = PipelineSettings(**current).clamped()
    with _lock:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(asdict(merged), indent=2), encoding="utf-8")
        _cached = merged
    return merged


def reset_settings() -> PipelineSettings:
    """Restore factory defaults."""
    global _cached
    with _lock:
        _cached = None
        if SETTINGS_PATH.exists():
            SETTINGS_PATH.unlink()
    return load_settings()
