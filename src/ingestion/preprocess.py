"""Historical document image preprocessor powered by OpenCV.

Implements CLAHE contrast adjustment, automatic deskewing via contour moments,
and local adaptive binarization specifically tuned to preserve delicate
polytonic Greek accents (oxia, varia, perispomeni, dasia, psili).
"""

from __future__ import annotations
import math
from pathlib import Path
from typing import Tuple, Optional
import cv2
import numpy as np
import pymupdf


class HistoricalPreprocessor:
    """Production OpenCV preprocessor for historical archival scans."""

    def __init__(
        self,
        clahe_clip_limit: float = 1.5,
        clahe_grid_size: Tuple[int, int] = (8, 8),
        adaptive_block_size: int = 25,
        adaptive_c: int = 11,
        deskew_deadband: float = 1.5,
        deskew_max: float = 8.0,
    ):
        self.clahe_clip_limit = clahe_clip_limit
        self.clahe_grid_size = clahe_grid_size
        self.adaptive_block_size = adaptive_block_size
        self.adaptive_c = adaptive_c
        # Rotation resampling costs more accuracy than a small tilt does: Tesseract
        # tolerates a couple of degrees natively, so only correct beyond the deadband.
        self.deskew_deadband = deskew_deadband
        self.deskew_max = deskew_max

    SUPPORTED_EXTENSIONS = {
        ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".webp", ".bmp", ".pdf", ".heic", ".heif"
    }

    def load_image(self, input_path: str | Path) -> np.ndarray:
        """Load image from path (supports PNG, JPG, TIFF, WebP, BMP, HEIC, and PDF)."""
        path = Path(input_path)
        if not path.exists():
            raise FileNotFoundError(f"Scan file not found: {path}")

        ext = path.suffix.lower()

        # 1. PDF handling via PyMuPDF at 300 DPI
        if ext == ".pdf":
            doc = pymupdf.open(str(path))
            if len(doc) == 0:
                raise ValueError(f"Empty PDF document: {path}")
            page = doc[0]
            zoom = 300 / 72.0
            mat = pymupdf.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        # 2. HEIC / HEIF handling via macOS built-in sips tool
        if ext in {".heic", ".heif"}:
            import tempfile
            import subprocess
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                subprocess.run(["/usr/bin/sips", "-s", "format", "png", str(path), "--out", tmp_path],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                image = cv2.imread(tmp_path)
            finally:
                if Path(tmp_path).exists():
                    Path(tmp_path).unlink()
            if image is not None:
                return image

        # 3. Direct OpenCV load
        image = cv2.imread(str(path))
        if image is not None:
            return image

        # 4. Fallback to PIL for exotic TIFF color profiles (e.g. CMYK magazine scans)
        try:
            from PIL import Image
            pil_img = Image.open(str(path)).convert("RGB")
            return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        except Exception as e:
            raise ValueError(f"Could not decode image file '{path}': {e}")

    def apply_clahe(self, gray: np.ndarray) -> np.ndarray:
        """Apply Contrast-Limited Adaptive Histogram Equalization.
        
        Amplifies low-contrast diacritics and faded ink against yellowed parchment.
        """
        clahe = cv2.createCLAHE(
            clipLimit=self.clahe_clip_limit,
            tileGridSize=self.clahe_grid_size,
        )
        return clahe.apply(gray)

    def estimate_skew_angle(self, gray: np.ndarray) -> float:
        """Estimate document skew angle in degrees via horizontal projection profiles.

        When a page of text is rotated to true horizontal, its row-sum profile shows
        maximum variance: dense peaks on the text lines, near-zero troughs in the
        leading between them. Sweeping candidate angles and keeping the variance
        maximum recovers the baseline angle directly.

        This replaces a ``cv2.minAreaRect`` estimate over the whole text point cloud,
        which measured the shape of the text block rather than the baselines: it was
        non-monotonic under injected tilt and reported 0.0 for genuinely skewed pages.
        """
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        if int(np.count_nonzero(thresh)) < 100:
            return 0.0

        # Work on a downscaled copy: baseline angle is a global property and the
        # sweep is O(angles), so full resolution buys nothing but time.
        scale = min(1.0, 1000.0 / max(thresh.shape))
        small = cv2.resize(thresh, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        h, w = small.shape
        center = (w // 2, h // 2)

        def profile_variance(angle: float) -> float:
            m = cv2.getRotationMatrix2D(center, angle, 1.0)
            rotated = cv2.warpAffine(
                small, m, (w, h), flags=cv2.INTER_NEAREST,
                borderMode=cv2.BORDER_CONSTANT, borderValue=0,
            )
            return float(np.var(rotated.sum(axis=1, dtype=np.float64)))

        # Coarse sweep across the plausible range, then refine around the winner.
        limit = self.deskew_max
        best_angle = max(np.arange(-limit, limit + 0.5, 0.5), key=profile_variance)
        best_angle = max(
            np.arange(best_angle - 0.5, best_angle + 0.55, 0.1), key=profile_variance
        )

        angle = float(best_angle)
        if abs(angle) > limit or abs(angle) < self.deskew_deadband:
            return 0.0
        return angle

    def rotate_image(self, image: np.ndarray, angle: float) -> np.ndarray:
        """Rotate image by angle in degrees with white border fill."""
        if abs(angle) < 0.2:
            return image

        (h, w) = image.shape[:2]
        center = (w // 2, h // 2)
        m = cv2.getRotationMatrix2D(center, angle, 1.0)
        
        cos = np.abs(m[0, 0])
        sin = np.abs(m[0, 1])
        new_w = int((h * sin) + (w * cos))
        new_h = int((h * cos) + (w * sin))

        m[0, 2] += (new_w / 2) - center[0]
        m[1, 2] += (new_h / 2) - center[1]

        border_val = 255 if len(image.shape) == 2 else (255, 255, 255)
        rotated = cv2.warpAffine(
            image, m, (new_w, new_h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=border_val,
        )
        return rotated

    def adaptive_binarize(self, gray: np.ndarray) -> np.ndarray:
        """Perform local adaptive thresholding to retain breathing marks and accents."""
        bs = self.adaptive_block_size
        if bs % 2 == 0:
            bs += 1
        binarized = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            bs,
            self.adaptive_c,
        )
        return binarized

    def process(self, input_path: str | Path, deskew: bool = True) -> dict[str, np.ndarray]:
        """Run full preprocessing pipeline on a scan.
        
        Returns a dictionary with:
          - 'original': loaded BGR image
          - 'enhanced': CLAHE contrast-enhanced grayscale
          - 'ocr_ready': bilateral-filtered grayscale — THIS is what OCR should consume
          - 'binarized': local adaptive binarized image, for human preview only
          - 'deskew_angle': estimated skew angle in degrees

        Note on 'binarized': adaptive thresholding is a display aid, not an OCR input.
        On faded typescript it converts smooth paper into salt-and-pepper speckle
        (measured: 13.6% black pixels against 7.7% actual ink), which wrecks
        Tesseract's line segmentation. Tesseract 5's LSTM binarizes internally and
        performs far better on the grayscale image. Feed it 'ocr_ready'.
        """
        bgr = self.load_image(input_path)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        angle = 0.0
        if deskew:
            angle = self.estimate_skew_angle(gray)
            if abs(angle) >= self.deskew_deadband:
                bgr = self.rotate_image(bgr, angle)
                gray = self.rotate_image(gray, angle)

        enhanced = self.apply_clahe(gray)
        # Bilateral filter removes halftone print dots and noise while keeping character strokes sharp
        ocr_ready = cv2.bilateralFilter(enhanced, 5, 40, 40)
        binarized = self.adaptive_binarize(enhanced)

        return {
            "original": bgr,
            "enhanced": enhanced,
            "ocr_ready": ocr_ready,
            "binarized": binarized,
            "deskew_angle": angle,
        }

    def create_thumbnail(self, image: np.ndarray, max_dim: int = 400) -> np.ndarray:
        """Create a downscaled thumbnail preserving aspect ratio."""
        h, w = image.shape[:2]
        if max(h, w) <= max_dim:
            return image
        scale = max_dim / float(max(h, w))
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)

    def save_image(self, image: np.ndarray, output_path: str | Path) -> Path:
        """Save an OpenCV image array to disk."""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), image)
        return out
