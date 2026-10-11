"""Static dashboard compiler rendering SQLite archive records into standalone portable HTML."""

from __future__ import annotations
import base64
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, Dict, Any
from PIL import Image
from jinja2 import Environment, FileSystemLoader

from src.database.db_manager import DatabaseManager, compute_sha256_and_size

TEMPLATES_DIR = Path(__file__).parent / "templates"
VENDOR_DIR = Path(__file__).parent.parent / "curation" / "static" / "vendor"
# The studio's date reader, inlined so the timeline inspector reads dates identically.
DATES_JS = Path(__file__).parent.parent / "curation" / "static" / "dates.js"
# Previews are made once per scan and kept; making them on every build took
# 150 s for a 416-scan archive, and the studio builds on every visit.
PREVIEW_CACHE = Path("data") / "thumbnails" / "previews"

log = logging.getLogger(__name__)

# How an export carries its scans. The page always holds a preview of each one,
# so it can be read on its own; the scans themselves are written beside it and
# the inspector opens them at full size. Exports travel between people, so the
# default gives up nothing -- smaller is a choice, made in Settings.
#
#   preview      the (long side, WebP quality) embedded in the page
#   scan_side    the long side of the published scan; None keeps every pixel
#   scan_quality JPEG quality of the published scan; None publishes the
#                original file, byte for byte
EXPORT_QUALITIES: Dict[str, Dict[str, Any]] = {
    "original": {"preview": (1000, 85), "scan_side": None, "scan_quality": None},
    "high":     {"preview": (1000, 85), "scan_side": None, "scan_quality": 92},
    "compact":  {"preview": (600, 75),  "scan_side": 2400, "scan_quality": 85},
}
DEFAULT_EXPORT_QUALITY = "original"

# What a browser can draw. An original in any other format (TIFF, PDF) is
# published with a full-resolution JPEG beside it for the inspector to show.
BROWSER_FORMATS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# Already lossy: copied as they are rather than compressed a second time.
LOSSY_FORMATS = {".jpg", ".jpeg", ".webp"}
RENDITION_QUALITY = 92
RENDITION_PDF_DPI = 300


def _open_page(p: Path, pdf_dpi: int = 150) -> Optional[Image.Image]:
    """The scan as an RGB image; for a PDF, its first page."""
    if p.suffix.lower() == ".pdf":
        import pymupdf
        doc = pymupdf.open(str(p))
        if len(doc) == 0:
            return None
        pix = doc[0].get_pixmap(dpi=pdf_dpi)
        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    img = Image.open(p)
    return img if img.mode == "RGB" else img.convert("RGB")


def write_rendition(master: Path, dest: Path, side: Optional[int] = None,
                    quality: int = RENDITION_QUALITY) -> None:
    """A JPEG of the master for browsers: every pixel, or `side` on the long edge."""
    img = _open_page(master, pdf_dpi=RENDITION_PDF_DPI)
    if img is None:
        raise ValueError(f"{master.name} has no page to render")
    if side:
        img.thumbnail((side, side), Image.Resampling.LANCZOS)
    # 4:4:4: chroma subsampling smears the thin coloured strokes of ink and seals.
    img.save(dest, format="JPEG", quality=quality, subsampling=0, optimize=True)


def generate_micro_preview(image_path: str | Path, max_dim: int = 1000, quality: int = 85,
                           cache_dir: Optional[Path] = None) -> Optional[str]:
    """A base64 WebP data URL of the scan, for the page to carry.

    With `cache_dir` the WebP is kept there, keyed by the file's path, size and
    modification time, so an unchanged scan is encoded once and an edited one
    again.
    """
    p = Path(image_path)
    if not p.exists() or not p.is_file():
        return None
    try:
        cached = None
        if cache_dir is not None:
            st = p.stat()
            key = f"{p.resolve()}|{st.st_mtime_ns}|{st.st_size}|{max_dim}|{quality}"
            cached = Path(cache_dir) / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()}.webp"
            if cached.exists():
                return "data:image/webp;base64," + base64.b64encode(cached.read_bytes()).decode("ascii")

        img = _open_page(p)
        if img is None:
            return None
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=quality, method=4)
        data = buf.getvalue()

        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            replace_atomically(cached, lambda tmp: tmp.write_bytes(data), mode=0o644)
        return "data:image/webp;base64," + base64.b64encode(data).decode("ascii")
    except Exception as exc:
        log.warning("Failed to generate micro preview for %s: %s", image_path, exc)
        return None


def app_version() -> str:
    """The installed version, as pyproject.toml declares it."""
    try:
        from importlib.metadata import version
        return version("archivesys")
    except Exception:
        return "unknown"


def replace_atomically(dest: Path, write, mode: Optional[int] = None) -> None:
    """Build dest beside itself, then swap it in with one rename.

    A reader -- the server streaming a scan, a browser opening the page -- sees
    the old file or the new one, never one cut off halfway through a rewrite.
    `mode` replaces the owner-only permissions the temporary file starts with.
    """
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    os.close(fd)
    try:
        write(Path(tmp))
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _stale(dest: Path, source: Path) -> bool:
    return not dest.exists() or source.stat().st_mtime > dest.stat().st_mtime


class DashboardCompiler:
    """Compiles SQLite archive data into a self-contained static HTML dashboard with content-addressable manifests."""

    def __init__(self, db_manager: DatabaseManager, output_dir: str | Path = "dist",
                 quality: Optional[str] = None, preview_cache: Optional[Path] = PREVIEW_CACHE):
        self.db = db_manager
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
        if quality is None:
            from src.config import load_settings
            quality = load_settings().export_quality
        self.quality = quality if quality in EXPORT_QUALITIES else DEFAULT_EXPORT_QUALITY
        self.preview_cache = preview_cache

    def compile(self, copy_scans: bool = True) -> Path:
        """Query archive database and generate dist/index.html with local assets and content-addressable manifest."""
        # Copy local vendor assets (vis-timeline, etc.) to dist/assets/vendor/
        dist_vendor = self.output_dir / "assets" / "vendor"
        if VENDOR_DIR.exists():
            shutil.copytree(VENDOR_DIR, dist_vendor, dirs_exist_ok=True)

        rendered = self.render(copy_scans=copy_scans)
        output_file = self.output_dir / "index.html"
        replace_atomically(output_file, lambda tmp: tmp.write_text(rendered, encoding="utf-8"), mode=0o644)
        return output_file

    def _publish_scan(self, orig_path: Path, sha: Optional[str], dist_scans: Path) -> Dict[str, str]:
        """Write one scan into assets/scans/ at the chosen quality.

        Returns the published file ("file") and what the inspector shows
        ("view", empty when the browser has nothing it can draw). Names carry
        the fingerprint: two "Scan 1.png" from different folders were one file
        here, the later overwriting the earlier, so one scan showed for both.
        """
        tier = EXPORT_QUALITIES[self.quality]
        stem = f"{orig_path.stem}_{sha[:8]}" if sha else orig_path.stem
        suffix = orig_path.suffix.lower()
        written = []

        def rendition(name: str, side: Optional[int], quality: int) -> Optional[Path]:
            dest = dist_scans / name
            try:
                if _stale(dest, orig_path):
                    replace_atomically(dest, lambda tmp: write_rendition(orig_path, tmp, side, quality), mode=0o644)
            except Exception as exc:
                log.warning("Could not publish %s: %s", orig_path, exc)
                return None
            written.append(dest.name)
            return dest

        if tier["scan_quality"] is None or (suffix in LOSSY_FORMATS and tier["scan_side"] is None):
            # The original, byte for byte.
            published = dist_scans / f"{stem}{orig_path.suffix}"
            if _stale(published, orig_path):
                replace_atomically(published, lambda tmp: shutil.copy2(orig_path, tmp))
            written.append(published.name)
            view = published
            if suffix not in BROWSER_FORMATS:
                view = rendition(f"{stem}.view.jpg", None, RENDITION_QUALITY)
        else:
            side = tier["scan_side"]
            published = rendition(f"{stem}.{side or 'full'}.jpg", side, tier["scan_quality"])
            view = published

        with self._written_lock:
            self._written.update(written)
        rel = lambda p: f"assets/scans/{p.name}" if p is not None else ""
        return {"file": rel(published), "view": rel(view)}

    def render(self, copy_scans: bool = False, live: bool = False) -> str:
        """The dashboard page itself, as a string.

        With copy_scans the scans are published beside it into assets/scans/
        and the page links to those copies: the publishing half of compile().
        Without it nothing on disk changes -- which is what the studio's live
        preview needs. It is rebuilt on every visit, and writing it over
        dist/index.html each time both replaced the published copy with one
        pointing at local paths, and cut short any request still reading that
        file ("Response content shorter than Content-Length").

        `live` is that preview: served by the studio, which has every scan at
        hand, so the page points at the studio's thumbnails instead of carrying
        its own copy of every scan.
        """
        data = self.db.get_timeline_export_data()
        preview_side, preview_quality = EXPORT_QUALITIES[self.quality]["preview"]

        dist_scans = self.output_dir / "assets" / "scans"
        if copy_scans:
            dist_scans.mkdir(parents=True, exist_ok=True)

        valid_documents = []
        valid_doc_ids = set()
        self._written = set()
        self._written_lock = threading.Lock()
        path_mapping = {}

        def prepare(doc: Dict[str, Any]) -> Dict[str, Any]:
            """Everything one scan needs: its fingerprint, preview and published copy."""
            orig_path = Path(doc["file_path"])
            out: Dict[str, Any] = {"exists": orig_path.exists() and orig_path.is_file()}
            if not out["exists"]:
                return out
            sha, sz = doc.get("sha256"), doc.get("file_size")
            if (not sha or not sz) and not live:
                sha, sz = compute_sha256_and_size(orig_path)
            out.update(sha256=sha, file_size=sz)
            if live:
                out["preview_url"] = f"/api/thumb?doc_id={doc['id']}"
            else:
                out["preview_url"] = generate_micro_preview(orig_path, preview_side, preview_quality,
                                                            cache_dir=self.preview_cache)
            if copy_scans:
                out["published"] = self._publish_scan(orig_path, sha, dist_scans)
            # The item's other pages travel the same way as its first.
            out["pages"] = {}
            for page in (doc.get("pages") or [])[1:]:
                page_path = Path(page["file_path"])
                if not page_path.is_file():
                    continue
                ready: Dict[str, Any] = {}
                if live:
                    ready["preview_url"] = f"/api/thumb?doc_id={page['id']}"
                else:
                    ready["preview_url"] = generate_micro_preview(
                        page_path, preview_side, preview_quality, cache_dir=self.preview_cache) or ""
                if copy_scans:
                    published_page = self._publish_scan(page_path, page.get("sha256"), dist_scans)
                    ready["file_path"] = published_page["file"]
                    ready["view_url"] = published_page["view"]
                out["pages"][page["id"]] = ready
            return out

        # Decoding a 10 MB scan and encoding it again is most of an export's
        # time, and PIL does it outside the GIL: one scan per core.
        if live:
            prepared = [prepare(d) for d in data["documents"]]
        else:
            with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
                prepared = list(pool.map(prepare, data["documents"]))

        for doc, ready in zip(data["documents"], prepared):
            orig_path = Path(doc["file_path"])
            if ready["exists"]:
                doc["sha256"], doc["file_size"] = ready["sha256"], ready["file_size"]
            doc["preview_url"] = ready.get("preview_url") or ""
            doc["orig_filename"] = orig_path.name

            pages = doc.get("pages") or []
            for page in pages[1:]:
                page.update(ready.get("pages", {}).get(page["id"], {}))

            if ready.get("published"):
                published = ready["published"]
                path_mapping[str(orig_path)] = published["file"]
                path_mapping[doc["file_path"]] = published["file"]
                doc["file_path"] = published["file"]
                doc["thumbnail_path"] = published["file"]
                doc["view_url"] = published["view"]
            if pages:
                # The first page is the item's own file, shown as the item shows it.
                pages[0].update({k: doc.get(k, "") for k in ("preview_url", "view_url", "file_path")})

            valid_documents.append(doc)
            valid_doc_ids.add(doc["id"])

        data["documents"] = valid_documents
        by_id = {d["id"]: d for d in valid_documents}

        # Filter events to prune references to missing documents
        valid_events = []
        for ev in data["events"]:
            filtered_docs = []
            for d in ev.get("docs", []):
                if d.get("id") in valid_doc_ids:
                    old_path = d.get("file_path")
                    if old_path in path_mapping:
                        d["file_path"] = path_mapping[old_path]
                        d["thumbnail_path"] = path_mapping[old_path]
                    # Attach preview_url and sha256 to event doc summary
                    matching_doc = by_id.get(d["id"])
                    if matching_doc:
                        d["preview_url"] = matching_doc.get("preview_url")
                        d["view_url"] = matching_doc.get("view_url", "")
                        d["sha256"] = matching_doc.get("sha256")
                        d["file_size"] = matching_doc.get("file_size")
                    filtered_docs.append(d)
            ev["docs"] = filtered_docs
            # A cluster survives on its people alone (a remembered birth leaves no
            # scan); a document entry does not exist without its document.
            # v1 tested ev["verification_status"], a key the exporter never emits,
            # so this condition was dead and rejected scans published.
            if filtered_docs or (ev.get("source") == "cluster" and ev.get("people")):
                valid_events.append(ev)

        data["events"] = valid_events

        # Clean stale scan assets in dist/assets/scans/ -- including scans
        # published at a quality no longer chosen.
        if copy_scans and dist_scans.exists():
            for item in dist_scans.iterdir():
                if item.is_file() and item.name not in self._written:
                    try:
                        item.unlink()
                    except OSError as exc:
                        log.warning("Failed to delete stale scan asset %s: %s", item, exc)

        # Construct full manifest metadata
        manifest = {
            "generator": f"ArchiveSys {app_version()}",
            "total_events": len(data["events"]),
            "total_documents": len(data["documents"]),
            "total_people": len(data["people"]),
            "has_embedded_previews": not live,
            "image_quality": self.quality,
        }

        # Render Jinja2 template
        template = self.env.get_template("index.html.jinja2")
        return template.render(
            events_json=json.dumps(data["events"], ensure_ascii=False),
            documents_json=json.dumps(data["documents"], ensure_ascii=False),
            people_json=json.dumps(data["people"], ensure_ascii=False),
            manifest_json=json.dumps(manifest, ensure_ascii=False),
            dates_js=DATES_JS.read_text(encoding="utf-8"),
        )
