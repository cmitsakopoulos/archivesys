"""Tell a page of text from a photograph, without reading either.

A scan is sorted when it enters the archive: text pages are documents (and
will want reading), photographs are not. The question "is there text here?"
is a detection question, so it is answered by a text *detector* -- PP-OCRv3,
2.4 MB, run through OpenCV's own DNN module -- rather than by OCR. OCR
answers a harder question in one language at a time; measured on 76 scans
from this archive, a fast Tesseract pass took 1.3 s a page and still could not
separate the two, while the detector took 0.1 s and separated every one: the
emptiest document had 26 times the text area of the most text-like photograph.

When it cannot tell (a blank page, a faint scrap), it says so, and the scan is
filed as a document: that way it is still read, and it waits in "To review"
for a person to decide. Filing a document as a photograph would suppress its
reading, which is exactly the mistake v1 made with every scan it saw.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

MODEL_PATH = Path(__file__).parent / "models" / "text_detection_ppocrv3.onnx"
DETECTOR_NAME = "ppocrv3-det"

# Share of the page covered by detected text. Measured on this archive:
# documents 0.020-0.27 (0.020 a near-blank page), photographs at most 0.0008.
TEXT_AREA_DOCUMENT = 0.008
TEXT_AREA_PHOTOGRAPH = 0.002
# Share of dark pixels. Scans of prints: at least 0.21; paper documents: at
# most 0.084. Below this, a page without text is blank, not a photograph.
DARK_PHOTOGRAPH = 0.12

_DETECT_SIDE = 960
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)

_net = None
_net_failed = False
_lock = threading.Lock()      # an OpenCV Net is not safe to share across threads
log = logging.getLogger(__name__)


def _load():
    global _net, _net_failed
    if _net is None and not _net_failed:
        try:
            import cv2
            _net = cv2.dnn.readNetFromONNX(str(MODEL_PATH))
        except Exception as exc:  # a missing model means no sorting, not a failed import
            _net_failed = True
            log.warning("Text detector unavailable (%s); scans will not be sorted.", exc)
    return _net


def _text_area(bgr: np.ndarray) -> Dict[str, float]:
    import cv2

    h, w = bgr.shape[:2]
    scale = min(1.0, _DETECT_SIDE / max(h, w))
    img = cv2.resize(bgr, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    H, W = max(32, round(img.shape[0] / 32) * 32), max(32, round(img.shape[1] / 32) * 32)
    x = (cv2.resize(img, (W, H)).astype(np.float32) / 255.0 - _MEAN) / _STD
    with _lock:
        net = _load()
        if net is None:
            raise RuntimeError("text detector unavailable")
        net.setInput(x.transpose(2, 0, 1)[None].copy())
        prob = net.forward()[0, 0]

    # Differentiable-binarisation post-processing, reduced to what is needed
    # here: how much of the page is confidently text, in how many pieces.
    n, labels, stats, _ = cv2.connectedComponentsWithStats((prob > 0.3).astype(np.uint8), connectivity=4)
    area, pieces = 0, 0
    for k in range(1, n):
        if stats[k, cv2.CC_STAT_AREA] < 12:
            continue
        if prob[labels == k].mean() >= 0.6:
            area += stats[k, cv2.CC_STAT_AREA]
            pieces += 1
    return {"text_area": area / float(H * W), "text_pieces": pieces}


def _dark_share(bgr: np.ndarray) -> float:
    import cv2

    h, w = bgr.shape[:2]
    scale = min(1.0, 512 / max(h, w))
    small = cv2.resize(bgr, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    value = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[..., 2]
    return float((value < 0.35 * 255).mean())


def sort_scan(bgr: np.ndarray) -> Optional[Dict[str, Any]]:
    """Decide whether a scan is text or a photograph.

    `bgr` is the page as OpenCV holds it (any size; it is scaled down here).
    Returns the verdict with its evidence -- "text", "photo" or "unsure", and
    the kind to file it under -- or None when the detector is unavailable, in
    which case nothing should be guessed.
    """
    if bgr is None or bgr.size == 0:
        return None
    if bgr.ndim == 2:
        import cv2
        bgr = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
    try:
        found = _text_area(bgr)
    except Exception as exc:
        log.warning("Could not sort a scan: %s", exc)
        return None
    dark = _dark_share(bgr)

    if found["text_area"] >= TEXT_AREA_DOCUMENT:
        verdict, kind = "text", "document"
    elif found["text_area"] <= TEXT_AREA_PHOTOGRAPH and dark >= DARK_PHOTOGRAPH:
        verdict, kind = "photo", "photograph"
    else:
        verdict, kind = "unsure", "document"

    return {
        "verdict": verdict,
        "kind": kind,
        "text_area": round(float(found["text_area"]), 4),
        "text_pieces": found["text_pieces"],
        "dark_share": round(dark, 3),
        "detector": DETECTOR_NAME,
    }


def sort_pil(image) -> Optional[Dict[str, Any]]:
    """sort_scan for a PIL image (the form index_scan already has open)."""
    rgb = np.asarray(image.convert("RGB"))
    return sort_scan(np.ascontiguousarray(rgb[..., ::-1]))
