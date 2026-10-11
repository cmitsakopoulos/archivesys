"""FastAPI server for the Historical Archive Dashboard & Curation Suite."""

from __future__ import annotations
import hashlib
import os
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from src.bundle import BUNDLE_SUFFIX, BundleError, app_version, inspect_bundle, restore_bundle, save_bundle
from src.config import (
    OCR_INPUTS, available_model_sets, load_settings, reset_settings, save_settings,
)
from src.curation.jobs import JobQueue
from src.session import SessionError, load_session, save_session, session_path
from src.database.edtf_parser import NotADate
from src.database.db_manager import DatabaseManager, DOCUMENT_ROLES, compute_sha256_and_size
from src.ingestion.date_extract import extract_dates
from src.ingestion.pages import group_pages
from src.ingestion.polytonic import PolytonicNormalizer
from src.ingestion.ocr_engine import build_tuned_pipeline, get_ocr_engine
from src.ingestion.preprocess import HistoricalPreprocessor
from src.visualizer.compiler import DashboardCompiler, replace_atomically

STATIC_DIR = Path(__file__).parent / "static"
THUMB_DIR = Path("data") / "thumbnails"
THUMB_MAX_DIM = 420
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".webp", ".bmp", ".pdf"}


def _character_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance between reference and OCR output, per reference character.

    Both sides are put through the archive's own search normalisation first, so
    the score measures recognition rather than differences in how a diacritic
    happens to be encoded.
    """
    ref = PolytonicNormalizer.normalize_search(reference)
    hyp = PolytonicNormalizer.normalize_search(hypothesis)
    if not ref:
        return 0.0
    previous = list(range(len(hyp) + 1))
    for i, rc in enumerate(ref, 1):
        current = [i]
        for j, hc in enumerate(hyp, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (rc != hc)))
        previous = current
    return previous[-1] / len(ref)


class JobSubmitRequest(BaseModel):
    paths: Optional[List[str]] = None
    doc_id: Optional[int] = None
    preferred_lang: Optional[str] = None


class BenchmarkRequest(BaseModel):
    doc_id: Optional[int] = None
    file_path: Optional[str] = None
    reference_text: Optional[str] = None
    # Each entry is a partial settings override applied over the saved settings.
    variants: Optional[List[Dict[str, Any]]] = None


class DocumentUpdateRequest(BaseModel):
    """The one shape the studio sends when a curator changes anything.

    `fields` carries the document's own columns -- the same columns the timeline
    projects. There is no separate event payload to keep in step, and no status
    string: `reviewed` is a boolean the Done button sets, and nothing else.
    """
    fields: Dict[str, Any] = Field(default_factory=dict)
    people_links: Optional[List[Dict[str, Any]]] = None
    reviewed: Optional[bool] = None
    reviewed_by: str = "studio"


class PersonUpdateRequest(BaseModel):
    fields: Dict[str, Any] = Field(default_factory=dict)


class PortraitRequest(BaseModel):
    """Three states, because "no picture" is a decision, not an empty field."""
    mode: str = "pinned"                      # 'auto' | 'pinned' | 'none'
    document_id: Optional[int] = None


class MergeRequest(BaseModel):
    keep_id: int
    merge_id: int


class PersonCreateRequest(BaseModel):
    first_name: str
    last_name: str
    maiden_name: Optional[str] = None
    birth_edtf: Optional[str] = None
    death_edtf: Optional[str] = None
    notes: Optional[str] = None


class IndexRequest(BaseModel):
    """One ingest shape for one file or a thousand."""
    paths: List[str]
    kind: Optional[str] = None          # None: sorted by the text detector


class ImportPlanRequest(BaseModel):
    """What adding a folder would do, before anything is added."""
    path: str
    include_subfolders: bool = False


class ImportStartRequest(BaseModel):
    """The scans the curator confirmed, from one plan."""
    folder: str
    paths: List[str]
    # Scans to keep together as the pages of one item, each list in page
    # order. It may name pages already in the archive: new pages join them.
    groups: List[List[str]] = []


class CombinePagesRequest(BaseModel):
    """Items to make of separate records: each is [first page, page 1, page 2, ...]."""
    groups: List[List[int]]


class OCRRunRequest(BaseModel):
    preferred_lang: Optional[str] = None
    engine_type: str = "auto"


class BundleSaveRequest(BaseModel):
    """Sent with the studio's position at the moment of saving, so the file
    reopens exactly there rather than wherever the last autosave caught it."""
    session: Optional[Dict[str, Any]] = None
    name: Optional[str] = None


def create_curation_app(db_manager: DatabaseManager, scans_dir: Path, dist_dir: Optional[Path] = None) -> FastAPI:
    """Create FastAPI application configured with database manager."""
    app = FastAPI(title="Historical Archive Dashboard")

    if dist_dir is None:
        dist_dir = Path("dist").resolve()
    else:
        dist_dir = Path(dist_dir).resolve()

    dist_assets = dist_dir / "assets"
    dist_assets.mkdir(parents=True, exist_ok=True)

    # Static web assets
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Serve compiled static timeline assets (vis-timeline bundle, scans)
    app.mount("/assets", StaticFiles(directory=str(dist_assets)), name="assets")

    # Serve raw scans for inspection if present
    if scans_dir.exists():
        app.mount("/scans", StaticFiles(directory=str(scans_dir)), name="scans")

    @app.get("/", response_class=RedirectResponse)
    async def index_redirect():
        return RedirectResponse(url="/curate")

    @app.get("/curate", response_class=HTMLResponse)
    async def serve_studio():
        studio_file = STATIC_DIR / "studio.html"
        return FileResponse(str(studio_file))

    # One timeline build at a time. Recompile and Export write into dist/, and
    # two at once would copy over each other; every build is heavy enough that
    # running several side by side would only slow each of them down.
    timeline_lock = threading.Lock()

    @app.get("/timeline", response_class=HTMLResponse)
    def serve_timeline():
        """The live timeline, built fresh for this visit and sent from memory.

        It used to be written over dist/index.html and then streamed from that
        file, so a second build landing mid-stream (a Recompile, another visit)
        rewrote the file under the response, which then came up short of the
        length it had promised. A plain def: the build runs off the event loop,
        so the studio keeps answering while it happens.
        """
        with timeline_lock:
            page = DashboardCompiler(db_manager, output_dir=dist_dir).render(copy_scans=False, live=True)
        return HTMLResponse(page)

    @app.get("/api/app")
    async def about_app():
        """Identifies a live ArchiveSys to the pages it serves.

        The compiled timeline is also published on its own (GitHub Pages, a
        file on disk), where there is nothing to save to. It asks here first:
        only a live server gets full-resolution scans and editing.
        """
        return {"app": "archivesys", "version": app_version()}

    @app.get("/api/timeline/entries")
    async def timeline_entries_for_document(doc_id: int = Query(...)):
        """Every timeline entry a document appears in, projected exactly as the
        compiled page projects them -- so an edit made in the inspector lands
        where a recompile would put it, without recompiling. Empty when the
        document is off the timeline (undated or excluded)."""
        events = db_manager.get_timeline_export_data()["events"]
        return {"entries": [e for e in events if any(d.get("id") == doc_id for d in e.get("docs", []))]}

    @app.post("/api/timeline/recompile")
    def recompile_timeline():
        """Recompile SQLite archive data into the standalone interactive timeline visualization."""
        try:
            with timeline_lock:
                compiler = DashboardCompiler(db_manager, output_dir=dist_dir)
                out_file = compiler.compile(copy_scans=True)
            return {"status": "ok", "message": "Timeline recompiled successfully",
                    "path": str(out_file.resolve()), "quality": compiler.quality}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to recompile timeline: {str(e)}")

    # -------------------------------------------------------------------------
    # Image Streaming
    # -------------------------------------------------------------------------
    @app.get("/api/image")
    async def get_document_image(
        doc_id: Optional[int] = Query(None),
        path: Optional[str] = Query(None),
        sha256: Optional[str] = Query(None),
    ):
        """Stream an image safely by document ID, SHA-256 fingerprint, or absolute path."""
        target_path: Optional[Path] = None
        if sha256:
            doc = db_manager.get_document_by_sha256(sha256)
            if doc and doc.get("file_path"):
                target_path = Path(doc["file_path"])
        elif doc_id is not None:
            doc = db_manager.get_document(doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="Document ID not found")
            target_path = Path(doc["file_path"])
        elif path:
            target_path = Path(path).expanduser().resolve()

        if not target_path or not target_path.exists() or not target_path.is_file():
            raise HTTPException(status_code=404, detail=f"Image file not found: {target_path}")

        ext = target_path.suffix.lower()
        if ext not in SUPPORTED_IMAGE_EXTS:
            raise HTTPException(status_code=400, detail="Unsupported file format")

        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".tiff": "image/tiff",
            ".tif": "image/tiff",
            ".bmp": "image/bmp",
            ".pdf": "application/pdf",
        }
        media_type = mime_types.get(ext, "application/octet-stream")
        return FileResponse(str(target_path), media_type=media_type)

    @app.get("/api/thumb")
    async def get_thumbnail(doc_id: Optional[int] = Query(None), path: Optional[str] = Query(None)):
        """Serve a small cached JPEG preview of a scan.

        The catalog and explorer grids used to point their <img> tags at
        /api/image, which streams the original file — for this archive that is a
        ~10 MB PNG per card, so a screen of 30 cards asked the browser to decode
        roughly 300 MB and the grid simply never painted. Thumbnails are generated
        once and cached on disk, keyed by source path plus mtime and size so an
        edited scan regenerates automatically.
        """
        target_path = _resolve_scan_path(doc_id, path)
        try:
            stat = target_path.stat()
        except OSError:
            raise HTTPException(status_code=404, detail="Scan file not readable")

        key = f"{target_path}|{stat.st_mtime_ns}|{stat.st_size}|{THUMB_MAX_DIM}"
        cache_file = THUMB_DIR / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()}.jpg"

        if not cache_file.exists():
            try:
                _render_thumbnail(target_path, cache_file)
            except Exception as e:
                raise HTTPException(status_code=415, detail=f"Cannot preview this file: {e}")

        return FileResponse(
            str(cache_file),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    def _resolve_scan_path(doc_id: Optional[int], path: Optional[str]) -> Path:
        if doc_id is not None:
            doc = db_manager.get_document(doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="Document ID not found")
            target = Path(doc["file_path"])
        elif path:
            target = Path(path).expanduser().resolve()
        else:
            raise HTTPException(status_code=400, detail="Provide doc_id or path")
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail=f"Scan not found: {target}")
        if target.suffix.lower() not in SUPPORTED_IMAGE_EXTS:
            raise HTTPException(status_code=400, detail="Unsupported file format")
        return target

    def _render_thumbnail(source: Path, destination: Path) -> None:
        """Downscale a scan (or the first page of a PDF) into a cached JPEG."""
        from PIL import Image

        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".pdf":
            import pymupdf
            doc = pymupdf.open(str(source))
            if doc.page_count == 0:
                raise ValueError("empty PDF")
            pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        else:
            img = Image.open(str(source))
            # draft() lets the JPEG decoder skip most of the work when we only
            # want a small preview; it is a no-op for other formats.
            try:
                img.draft("RGB", (THUMB_MAX_DIM, THUMB_MAX_DIM))
            except (AttributeError, ValueError):
                pass
            img = img.convert("RGB")

        img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM), Image.LANCZOS)
        # Renamed into place: the cache trusts any file it finds, so one cut
        # short by a crash mid-write would be served broken from then on.
        replace_atomically(
            destination, lambda tmp: img.save(str(tmp), "JPEG", quality=82, optimize=True), mode=0o644,
        )

    # -------------------------------------------------------------------------
    # Filesystem Explorer API
    # -------------------------------------------------------------------------
    @app.get("/api/fs/browse")
    async def browse_filesystem(
        path: Optional[str] = Query(None),
        offset: int = Query(0, ge=0),
        limit: Optional[int] = Query(None, ge=1, le=500),
        query: Optional[str] = Query(None),
        filter_status: Optional[str] = Query(None),
    ):
        """Browse local directories, returning folders and a page of scan files.

        Results are paginated and can be filtered in real time by query string
        or indexing status (all, unindexed, indexed).
        """
        page_size = limit if limit is not None else load_settings().explorer_page_size
        default_pictures = Path.home() / "Pictures"
        default_root = default_pictures if default_pictures.exists() else Path.home()

        if path and path.strip():
            req_path = Path(path.strip()).expanduser().resolve()
            if req_path.exists() and req_path.is_dir():
                target_dir = req_path
            else:
                target_dir = default_root
        else:
            target_dir = default_root

        # Safety: avoid system critical directories if attempted
        parent_dir = target_dir.parent if target_dir.parent != target_dir else None

        directories = []
        files = []

        try:
            entries = list(os.scandir(target_dir))
        except (PermissionError, OSError) as e:
            return {
                "current_path": str(target_dir),
                "parent_path": str(parent_dir) if parent_dir else None,
                "error": f"Unable to access directory: {e}",
                "directories": [],
                "files": [],
                "quick_bookmarks": get_quick_bookmarks(),
            }

        q_clean = query.strip().lower() if query and query.strip() else None

        scan_entries = []
        for entry in sorted(entries, key=lambda e: e.name.lower()):
            # Filter hidden / dot files
            if entry.name.startswith("."):
                continue

            try:
                if entry.is_dir(follow_symlinks=False):
                    if not q_clean or q_clean in entry.name.lower():
                        directories.append({
                            "name": entry.name,
                            "path": entry.path,
                        })
                elif entry.is_file(follow_symlinks=False):
                    if Path(entry.name).suffix.lower() in SUPPORTED_IMAGE_EXTS:
                        if not q_clean or q_clean in entry.name.lower():
                            scan_entries.append(entry)
            except (PermissionError, OSError):
                continue

        # One query for the folder rather than a lookup per file.
        indexed = db_manager.indexed_paths()

        def record_for(entry) -> Optional[Dict[str, Any]]:
            try:
                return indexed.get(str(Path(entry.path).resolve()))
            except OSError:
                return None

        if filter_status in ("unindexed", "indexed"):
            scan_entries = [e for e in scan_entries
                            if (record_for(e) is not None) == (filter_status == "indexed")]

        total_files = len(scan_entries)
        page = scan_entries[offset:offset + page_size]

        # Only the entries actually being returned are stat'd and looked up, so
        # the cost of opening a large folder no longer scales with its size.
        for entry in page:
            try:
                stat = entry.stat()
            except OSError:
                continue
            existing_doc = record_for(entry)
            files.append({
                "name": entry.name,
                "path": entry.path,
                "size_bytes": stat.st_size,
                "is_indexed": existing_doc is not None,
                # A page opens its item, at that page.
                "doc_id": (existing_doc.get("parent_id") or existing_doc["id"]) if existing_doc else None,
                "page_no": (existing_doc.get("page_no") or 0) if existing_doc else None,
                "reviewed": bool(existing_doc.get("reviewed")) if existing_doc else False,
                "kind": existing_doc.get("kind") if existing_doc else None,
                "language": existing_doc.get("language") if existing_doc else "el-poly",
            })

        return {
            "current_path": str(target_dir),
            "parent_path": str(parent_dir) if parent_dir else None,
            "directories": directories,
            "files": files,
            "total_files": total_files,
            "offset": offset,
            "limit": page_size,
            "has_more": offset + len(files) < total_files,
            "quick_bookmarks": get_quick_bookmarks(),
        }

    def get_quick_bookmarks():
        bms = []
        pics = Path.home() / "Pictures"
        if pics.exists():
            bms.append({"name": "Pictures", "path": str(pics)})
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            bms.append({"name": "Desktop", "path": str(desktop)})
        data_dir = Path("data").resolve()
        bms.append({"name": "Workspace Data", "path": str(data_dir)})
        home_dir = Path.home()
        bms.append({"name": "Home", "path": str(home_dir)})
        return bms

    @app.post("/api/documents")
    def index_scans(req: IndexRequest):
        """Bring scans into the archive. The only ingest path there is.

        v1 had four of these -- quick-index, batch-quick-index, ingest and
        upload -- all reaching the same function, three of them defaulting the
        type to "photograph". Every document in the archive therefore claimed to
        be a photograph, which suppressed its own OCR.
        """
        if not req.paths:
            raise HTTPException(status_code=400, detail="No paths provided")

        indexed, skipped = [], []
        for raw in req.paths:
            p = Path(raw).expanduser().resolve()
            if not p.exists() or not p.is_file():
                skipped.append({"path": raw, "reason": "not found"})
                continue
            if p.suffix.lower() not in SUPPORTED_IMAGE_EXTS:
                skipped.append({"path": raw, "reason": f"unsupported format {p.suffix.lower()}"})
                continue
            try:
                doc_id = db_manager.index_scan(str(p), kind=req.kind)
            except Exception as exc:
                skipped.append({"path": raw, "reason": str(exc)})
                continue
            indexed.append({"doc_id": doc_id, "file_path": str(p)})

        return {
            "status": "success",
            "indexed": indexed,
            "skipped": skipped,
            "indexed_count": len(indexed),
        }

    @app.post("/api/documents/upload")
    async def upload_document(
        file: UploadFile = File(...),
        preferred_lang: Optional[str] = "el-poly",
        kind: Optional[str] = None,
    ):
        """Direct file upload into the archive from the browser UI."""
        # The name only: a client-supplied "../x.png" must not leave data/uploads.
        filename = Path(file.filename or "").name or f"scan_{int(time.time())}.png"
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_IMAGE_EXTS:
            raise HTTPException(status_code=400, detail=f"Unsupported file format: {ext}")

        upload_dir = Path("data/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest_path = upload_dir / filename

        if dest_path.exists():
            dest_path = upload_dir / f"{dest_path.stem}_{int(time.time())}{dest_path.suffix}"

        with dest_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        preproc, engine, cfg = build_tuned_pipeline("auto")
        proc_result = preproc.process(dest_path, deskew=cfg.deskew_enabled)
        dims = {"width": int(proc_result["original"].shape[1]), "height": int(proc_result["original"].shape[0])}

        thumb_img = preproc.create_thumbnail(proc_result["original"], max_dim=400)
        thumb_dir = Path("data/thumbnails")
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumb_dir / f"{dest_path.stem}_thumb.jpg"
        preproc.save_image(thumb_img, thumb_path)

        ocr_image = proc_result.get(cfg.ocr_input, proc_result["ocr_ready"])
        ocr_res = engine.extract(ocr_image, preferred_lang=preferred_lang or cfg.default_lang)

        doc_id = db_manager.add_document(
            file_path=str(dest_path),
            thumbnail_path=str(thumb_path),
            kind=kind or ocr_res.document_type or "document",
            language=ocr_res.language,
            transcription=ocr_res.transcription,
            metadata={"dimensions": dims, "orig_filename": filename},
            confidence_score=ocr_res.confidence,
        )

        return {
            "status": "success",
            "doc_id": doc_id,
            "filename": filename,
            "file_path": str(dest_path),
            "language": ocr_res.language,
            "confidence": ocr_res.confidence,
            "transcription": ocr_res.transcription,
        }

    @app.post("/api/export")
    def export_archive():
        """Export standalone portable dashboard HTML with embedded previews and manifest."""
        try:
            with timeline_lock:
                out_file = DashboardCompiler(db_manager, output_dir=dist_dir).compile(copy_scans=True)
            return {
                "status": "success",
                "message": "Archive successfully compiled to standalone HTML",
                "output_path": str(out_file),
                "output_file": out_file.name,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")

    # -------------------------------------------------------------------------
    # Documents API
    # -------------------------------------------------------------------------
    @app.get("/api/documents")
    async def get_documents(
        reviewed: Optional[bool] = Query(None),
        kind: Optional[str] = Query(None),
        search: Optional[str] = Query(None),
        person: Optional[List[int]] = Query(None),
        person_match: str = Query("any"),
    ):
        """List documents, narrowed by review state, kind and the people in them."""
        docs = db_manager.list_documents(
            reviewed=reviewed, kind=kind, people=person, people_match=person_match,
        )
        if search and search.strip():
            q = search.strip().lower()
            docs = [
                d for d in docs
                if q in (d.get("file_path", "")).lower()
                or q in (d.get("transcription") or "").lower()
                or q in (d.get("title") or "").lower()
                or q in " ".join(d.get("tags") or []).lower()
                or q == str(d.get("id"))
            ]
        return docs

    @app.get("/api/documents/{doc_id}")
    async def get_document_details(doc_id: int):
        doc = db_manager.get_document(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    @app.get("/api/documents/{doc_id}/full")
    async def get_document_full_details(doc_id: int):
        doc = db_manager.get_document_full(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc

    @app.post("/api/documents/{doc_id}")
    async def update_document_route(doc_id: int, req: DocumentUpdateRequest):
        """The single write path. v1 had this plus a dead /curate twin."""
        if not db_manager.get_document(doc_id):
            raise HTTPException(status_code=404, detail="Document not found")
        try:
            updated = db_manager.update_document(
                doc_id=doc_id,
                fields=req.fields,
                people_links=req.people_links,
                reviewed=req.reviewed,
                reviewed_by=req.reviewed_by,
            )
        except NotADate as exc:
            # Refused, not rounded off to 1900.
            raise HTTPException(status_code=400, detail=f"That date cannot be stored: {exc}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "success", "document": updated}

    @app.delete("/api/documents/{doc_id}")
    async def delete_document_route(doc_id: int):
        success = db_manager.delete_document(doc_id)
        if not success:
            raise HTTPException(status_code=404, detail="Document not found")
        return {"status": "success", "deleted_doc_id": doc_id}

    @app.post("/api/documents/{doc_id}/split")
    def split_page_route(doc_id: int):
        """Take one page out of its item; it becomes a record of its own."""
        try:
            return {"status": "ok", **db_manager.split_page(doc_id)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/pages/proposals")
    def page_proposals_route():
        """Records whose names say they are pages of one item, not yet combined."""
        return {"proposals": db_manager.page_proposals()}

    @app.post("/api/pages/combine")
    def combine_pages_route(req: CombinePagesRequest):
        """Combine each group into one item, its first record first."""
        combined, failed = [], []
        for group in req.groups:
            if len(group) < 2:
                continue
            try:
                combined.append(db_manager.combine_pages(group[0], group[1:])["id"])
            except ValueError as exc:
                failed.append({"lead_id": group[0], "reason": str(exc)})
        return {"status": "ok", "combined": combined, "failed": failed}

    @app.get("/api/documents/{doc_id}/date-suggestions")
    async def suggest_dates(doc_id: int):
        """Dates this document states about itself, read off its own text.

        Most historical records carry their date on the page, and Arabic
        numerals survive every language here, so a year is findable even where
        the words are not. Proposals only -- nothing is written until the
        curator accepts one.
        """
        doc = db_manager.get_document_full(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        text = "\n\n".join(p["transcription"] for p in doc.get("pages") or [] if p.get("transcription")) \
            or doc.get("transcription")
        return {
            "current": doc.get("edtf_date"),
            "suggestions": extract_dates(text, filename=Path(doc["file_path"]).name),
        }

    @app.post("/api/documents/{doc_id}/ocr")
    async def rerun_ocr(doc_id: int, req: OCRRunRequest):
        doc = db_manager.get_document(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        preproc, engine, cfg = build_tuned_pipeline(req.engine_type)
        processed = preproc.process(doc["file_path"], deskew=cfg.deskew_enabled)
        image = processed.get(cfg.ocr_input, processed["ocr_ready"])
        result = engine.extract(image, preferred_lang=req.preferred_lang or cfg.default_lang)
        return {
            "transcription": result.transcription,
            "language": result.language,
            "document_type": result.document_type,
            "confidence": result.confidence,
            "engine": result.engine_name,
            "lines": result.raw_metadata.get("lines", 0),
            "refined_lines": result.raw_metadata.get("refined_lines", 0),
            "deskew_angle": round(float(processed["deskew_angle"]), 2),
        }

    # -------------------------------------------------------------------------
    # People & Entities API
    # -------------------------------------------------------------------------
    @app.get("/api/people")
    async def get_people(sort: str = Query("name")):
        """Everyone, with document counts and a resolved portrait."""
        return db_manager.list_people(sort=sort)

    @app.get("/api/people/duplicates")
    async def get_duplicate_people():
        """People who are probably one person. Declared before /{person_id}
        so that "duplicates" is not read as an id."""
        return db_manager.find_duplicate_people()

    @app.get("/api/people/roles")
    async def get_person_roles():
        """The roles a person can play ON a document -- not relationships."""
        return DOCUMENT_ROLES

    @app.post("/api/people/merge")
    async def merge_people_route(req: MergeRequest):
        try:
            return {"status": "success", "person": db_manager.merge_people(req.keep_id, req.merge_id)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/people/{person_id}")
    async def get_person_route(person_id: int):
        person = db_manager.get_person(person_id)
        if not person:
            raise HTTPException(status_code=404, detail="Person not found")
        return person

    @app.get("/api/people/{person_id}/documents")
    async def get_person_documents(person_id: int):
        if not db_manager.get_person(person_id):
            raise HTTPException(status_code=404, detail="Person not found")
        return db_manager.list_person_documents(person_id)

    @app.get("/api/people/{person_id}/portrait-options")
    async def get_portrait_options(person_id: int):
        """Photographs this person appears in, best candidate first."""
        if not db_manager.get_person(person_id):
            raise HTTPException(status_code=404, detail="Person not found")
        docs = [d for d in db_manager.list_person_documents(person_id)
                if d.get("kind") == "photograph" and not d.get("excluded")]
        suggested = db_manager.suggest_portrait(person_id)
        return {
            "suggested": suggested,
            "options": [{"id": d["id"], "title": d.get("title"),
                         "date_display": d.get("date_display"), "role": d.get("role")}
                        for d in docs],
        }

    @app.post("/api/people/{person_id}")
    async def update_person_route(person_id: int, req: PersonUpdateRequest):
        if not db_manager.get_person(person_id):
            raise HTTPException(status_code=404, detail="Person not found")
        try:
            return {"status": "success", "person": db_manager.update_person(person_id, req.fields)}
        except NotADate as exc:
            raise HTTPException(status_code=400, detail=f"That date cannot be stored: {exc}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/people/{person_id}/portrait")
    async def set_portrait_route(person_id: int, req: PortraitRequest):
        try:
            person = db_manager.set_portrait(person_id, req.document_id, req.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "success", "person": person}

    @app.delete("/api/people/{person_id}")
    async def delete_person_route(person_id: int):
        """Take someone out of the archive. The documents they were in stay."""
        removed = db_manager.delete_person(person_id)
        if removed is None:
            raise HTTPException(status_code=404, detail="Person not found")
        return {"status": "success", "deleted": removed}

    @app.post("/api/people")
    async def create_person_route(req: PersonCreateRequest):
        try:
            p_id = db_manager.add_person(
                first_name=req.first_name,
                last_name=req.last_name,
                maiden_name=req.maiden_name,
                birth_edtf=req.birth_edtf,
                death_edtf=req.death_edtf,
                notes=req.notes,
            )
        except NotADate as exc:
            raise HTTPException(status_code=400, detail=f"That date cannot be stored: {exc}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return db_manager.get_person(p_id)

    # -------------------------------------------------------------------------
    # Taxonomies & Categories API
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # Background OCR Job Queue
    # -------------------------------------------------------------------------
    def _run_job(job, progress, is_cancelled) -> Dict[str, Any]:
        """Execute one queued OCR job, reporting real progress as it goes."""
        from src.ingestion.ocr_engine import LocalTesseractEngine

        if job.kind == "import":
            return _run_import(job, progress, is_cancelled)

        preproc, engine, cfg = build_tuned_pipeline("auto")
        if isinstance(engine, LocalTesseractEngine):
            engine.cancelled = is_cancelled

        if job.kind == "reocr":
            doc = db_manager.get_document(job.doc_id)
            if not doc:
                raise ValueError(f"Document #{job.doc_id} no longer exists")
            source = Path(doc["file_path"])
        else:
            source = Path(job.path).expanduser().resolve()
            if not source.exists():
                raise ValueError(f"File not found: {source}")

        progress("Preparing image", 0, 0)
        processed = preproc.process(source, deskew=cfg.deskew_enabled)
        if is_cancelled():
            return {}

        image = processed.get(cfg.ocr_input, processed["ocr_ready"])
        result = engine.extract(
            image,
            preferred_lang=job.preferred_lang or cfg.default_lang,
            progress=progress,
        )
        if is_cancelled():
            return {}

        payload = {
            "transcription": result.transcription,
            "language": result.language,
            "confidence": result.confidence,
            "engine": result.engine_name,
            "characters": len(result.transcription),
            "lines": result.raw_metadata.get("lines", 0),
            "refined_lines": result.raw_metadata.get("refined_lines", 0),
            "dropped_noise_lines": result.raw_metadata.get("dropped_noise_lines", 0),
            "deskew_angle": round(float(processed["deskew_angle"]), 2),
        }

        if job.kind == "ingest":
            progress("Saving to archive", 0, 0)
            thumb_dir = Path("data/thumbnails")
            thumb_dir.mkdir(parents=True, exist_ok=True)
            thumb_path = thumb_dir / f"{source.stem}_thumb.jpg"
            preproc.save_image(preproc.create_thumbnail(processed["original"], 400), thumb_path)
            existing = db_manager.get_document_by_path(str(source))
            if existing:
                payload["doc_id"] = existing["id"]
                payload["already_indexed"] = True
            else:
                payload["doc_id"] = db_manager.add_document(
                    file_path=str(source),
                    thumbnail_path=str(thumb_path),
                    kind=result.document_type or "document",
                    language=result.language,
                    transcription=result.transcription,
                    metadata={"dimensions": {
                        "width": int(processed["original"].shape[1]),
                        "height": int(processed["original"].shape[0]),
                    }},
                    confidence_score=result.confidence,
                )
            job.doc_id = payload["doc_id"]
        else:
            payload["doc_id"] = job.doc_id
            if job.kind == "reocr" and job.doc_id:
                progress("Saving transcription", 0, 0)
                db_manager.update_document_ocr(
                    doc_id=job.doc_id,
                    transcription=result.transcription,
                    language=result.language,
                    confidence_score=result.confidence,
                    ocr_metadata={
                        "lines": result.raw_metadata.get("lines", 0),
                        "refined_lines": result.raw_metadata.get("refined_lines", 0),
                        "dropped_noise_lines": result.raw_metadata.get("dropped_noise_lines", 0),
                        "deskew_angle": round(float(processed["deskew_angle"]), 2),
                    },
                )
        return payload

    def _run_import(job, progress, is_cancelled) -> Dict[str, Any]:
        """Add a confirmed list of scans, sorting each as text or photograph.

        Stopping keeps what was already added; the summary says how far it got.
        """
        total = len(job.paths)
        summary: Dict[str, Any] = {
            "kind": "import", "total": total, "added": 0, "documents": 0, "photographs": 0,
            "undecided": 0, "unsorted": 0, "already": 0, "failed": [], "doc_ids": [],
        }
        for i, raw in enumerate(job.paths):
            if is_cancelled():
                break
            path = Path(raw)
            progress(f"Adding {path.name}", i, total)
            try:
                if db_manager.get_document_by_path(str(path)):
                    summary["already"] += 1
                    continue
                doc_id = db_manager.index_scan(str(path))
            except Exception as exc:
                summary["failed"].append({"name": path.name, "reason": str(exc)})
                continue
            doc = db_manager.get_document(doc_id) or {}
            verdict = ((doc.get("metadata") or {}).get("sorting") or {}).get("verdict")
            summary["added"] += 1
            summary["doc_ids"].append(doc_id)
            key = {"text": "documents", "photo": "photographs", "unsure": "undecided"}.get(verdict, "unsorted")
            summary[key] += 1

        # Pages of one item become one record. Counted by item from here on:
        # the written back of a print is not a second document.
        summary["items"] = summary["added"]
        summary["pages"] = 0
        for group in job.groups:
            if is_cancelled():
                break
            docs = [d for d in (db_manager.get_document_by_path(p) for p in group) if d]
            if len(docs) < 2:
                continue
            lead_id = next((d["parent_id"] for d in docs if d.get("parent_id")), docs[0]["id"])
            joining = [d["id"] for d in docs if d["id"] != lead_id and d.get("parent_id") != lead_id]
            if not joining:
                continue
            try:
                db_manager.combine_pages(lead_id, joining)
            except ValueError as exc:
                summary["failed"].append({"name": Path(group[0]).name, "reason": str(exc)})
                continue
            added_pages = [i for i in joining if i in summary["doc_ids"]]
            summary["pages"] += len(added_pages)
            for i in added_pages:
                summary["doc_ids"].remove(i)
        if summary["pages"]:
            for key in ("documents", "photographs", "undecided", "unsorted"):
                summary[key] = 0
            for doc_id in summary["doc_ids"]:
                doc = db_manager.get_document(doc_id) or {}
                verdict = ((doc.get("metadata") or {}).get("sorting") or {}).get("verdict")
                summary[{"text": "documents", "photo": "photographs", "unsure": "undecided"}.get(verdict, "unsorted")] += 1
            summary["items"] = len(summary["doc_ids"])
        progress("Finished", summary["added"] + summary["already"] + len(summary["failed"]), total)
        return summary

    job_queue = JobQueue(_run_job)

    # -------------------------------------------------------------------------
    # Adding a whole folder: plan first, then a background import
    # -------------------------------------------------------------------------
    def _scans_in(folder: Path, recursive: bool) -> List[Path]:
        found: List[Path] = []

        def walk(directory: Path) -> None:
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
            except OSError:
                return
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if recursive:
                            walk(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and \
                            Path(entry.name).suffix.lower() in SUPPORTED_IMAGE_EXTS:
                        found.append(Path(entry.path))
                except OSError:
                    continue

        walk(folder)
        return found

    @app.post("/api/import/plan")
    def plan_folder_import(req: ImportPlanRequest):
        """What "Add this folder" would do, file by file, before anything is added.

        Nothing is written. A file is `already` in the archive when its path is,
        a `duplicate` when its bytes match a record or an earlier file in this
        list (it is shown with what it copies, and only added if the curator
        ticks it), and `new` otherwise. Only files whose size matches something
        are fingerprinted, so planning a large folder stays quick.
        """
        folder = Path(req.path).expanduser().resolve()
        if not folder.is_dir():
            raise HTTPException(status_code=404, detail=f"Not a folder: {folder}")

        subfolders = []
        try:
            children = sorted((e for e in os.scandir(folder) if not e.name.startswith(".")),
                              key=lambda e: e.name.lower())
        except OSError:
            children = []
        for entry in children:
            try:
                if entry.is_dir(follow_symlinks=False):
                    count = len(_scans_in(Path(entry.path), recursive=True))
                    if count:
                        subfolders.append({"name": entry.name, "count": count})
            except OSError:
                continue

        indexed = db_manager.indexed_paths()
        archived_by_size: Dict[int, List[Dict[str, Any]]] = {}
        for doc in indexed.values():
            if doc.get("file_size"):
                archived_by_size.setdefault(doc["file_size"], []).append(doc)

        def rel(p: Path) -> str:
            try:
                return str(p.relative_to(folder))
            except ValueError:
                return p.name

        already, candidates = [], []
        for p in _scans_in(folder, req.include_subfolders):
            doc = indexed.get(str(p))
            if doc:
                already.append({"rel": rel(p), "doc_id": doc["id"]})
                continue
            try:
                candidates.append((p, p.stat().st_size))
            except OSError:
                continue

        size_counts: Dict[int, int] = {}
        for _, size in candidates:
            size_counts[size] = size_counts.get(size, 0) + 1

        hashed: Dict[Path, str] = {}
        for p, size in candidates:
            if size in archived_by_size or size_counts[size] > 1:
                hashed[p] = compute_sha256_and_size(p)[0]
        # Of identical files in the list, the shortest name is taken as the
        # original: "letter.png", not "letter copy.png" -- whichever sorts first.
        original: Dict[str, Path] = {}
        for p in sorted(hashed, key=lambda q: (len(q.name), rel(q).lower())):
            original.setdefault(hashed[p], p)

        new, duplicates = [], []
        for p, size in candidates:
            same_as = None
            sha = hashed.get(p)
            if sha:
                twin = next((d for d in archived_by_size.get(size, []) if d.get("sha256") == sha), None)
                if twin:
                    same_as = {"in_archive": True, "doc_id": twin["id"],
                               "name": twin.get("title") or Path(twin["file_path"]).name,
                               "path": twin["file_path"]}
                elif original[sha] != p:
                    same_as = {"in_archive": False, "name": rel(original[sha]), "path": str(original[sha])}
            item = {"path": str(p), "rel": rel(p), "size": size}
            if same_as:
                duplicates.append({**item, "same_as": same_as})
            else:
                new.append(item)

        # Pages of one item, by name. A group may reach into the archive: a
        # new "12345 3" joins the "12345" already there.
        in_list = {i["path"]: i for i in [*new, *duplicates]}
        archived = {str(p): d for p, d in ((Path(k), v) for k, v in indexed.items())}
        groups = []
        for g in group_pages([*in_list, *(str(Path(folder) / a["rel"]) for a in already)]):
            if not any(p in in_list for p in g["paths"]):
                continue
            key = f"{g['folder']}|{str(g['name']).casefold()}"
            for p in g["paths"]:
                if p in in_list:
                    in_list[p]["group"] = key
            groups.append({
                "key": key, "name": g["name"], "counter": g["counter"],
                "folder": rel(Path(str(g["folder"]))) if Path(str(g["folder"])) != folder else "",
                "paths": g["paths"],
                "in_archive": sum(1 for p in g["paths"] if p in archived),
            })

        return {
            "folder": str(folder),
            "folder_name": folder.name or str(folder),
            "include_subfolders": req.include_subfolders,
            "subfolders": subfolders,
            "new": new,
            "duplicates": duplicates,
            "already": already,
            "groups": groups,
        }

    @app.post("/api/import/start")
    def start_folder_import(req: ImportStartRequest):
        """Add the confirmed scans in the background; progress shows in Activity."""
        paths = []
        for raw in req.paths:
            p = Path(raw).expanduser().resolve()
            if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTS:
                paths.append(str(p))
        if not paths:
            raise HTTPException(status_code=400, detail="Nothing to add: none of those files can be read.")
        name = Path(req.folder).name or req.folder
        groups = [[str(Path(p).expanduser().resolve()) for p in g] for g in req.groups if len(g) > 1]
        job = job_queue.submit(kind="import", label=f"Adding {len(paths)} scans from {name}",
                               paths=paths, groups=groups)
        return {"status": "ok", "job": job.as_dict()}

    @app.post("/api/jobs")
    async def enqueue_jobs(req: JobSubmitRequest):
        """Queue one or more scans for OCR. Returns immediately with job ids."""
        jobs = []
        for raw in (req.paths or []):
            p = Path(raw).expanduser().resolve()
            if p.suffix.lower() not in SUPPORTED_IMAGE_EXTS:
                continue
            jobs.append(job_queue.submit(
                kind="ingest", label=p.name, path=str(p), preferred_lang=req.preferred_lang,
            ))
        if req.doc_id is not None:
            doc = db_manager.get_document(req.doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="Document not found")
            jobs.append(job_queue.submit(
                kind="reocr", label=Path(doc["file_path"]).name,
                doc_id=req.doc_id, preferred_lang=req.preferred_lang,
            ))
        if not jobs:
            raise HTTPException(status_code=400, detail="Nothing queued: no supported files given")
        return {"status": "ok", "jobs": [j.as_dict() for j in jobs]}

    @app.get("/api/jobs")
    async def list_jobs():
        return job_queue.snapshot()

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int):
        return {"status": "ok", "cancelled": job_queue.cancel(job_id)}

    @app.post("/api/jobs/cancel-all")
    async def cancel_all_jobs():
        return {"status": "ok", "cancelled": job_queue.cancel_all()}

    # -------------------------------------------------------------------------
    # Session: where the curator is
    # -------------------------------------------------------------------------
    def _session_file() -> Path:
        return session_path(db_manager.db_path)

    @app.get("/api/session")
    async def get_session():
        return load_session(_session_file())

    # POST as well as PUT: navigator.sendBeacon, which records the final
    # position as a tab closes, can only POST.
    @app.api_route("/api/session", methods=["PUT", "POST"])
    async def put_session(request: Request):
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="The session must be JSON")
        try:
            save_session(_session_file(), data)
        except SessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "ok"}

    # -------------------------------------------------------------------------
    # Saving and opening the whole archive (.archivesys)
    #
    # Both directions go through a temporary file with a one-time token rather
    # than a single request: the save must finish (and report what it found)
    # before the browser's download starts, and an open must show what the file
    # holds before the curator agrees to replace what is open now.
    # -------------------------------------------------------------------------
    pending_downloads: Dict[str, Dict[str, Any]] = {}
    pending_uploads: Dict[str, Dict[str, Any]] = {}

    def _sweep_pending(table: Dict[str, Dict[str, Any]], max_age: float = 3600) -> None:
        for token, entry in list(table.items()):
            if time.time() - entry["at"] > max_age:
                table.pop(token, None)
                shutil.rmtree(entry["dir"], ignore_errors=True)

    def _jobs_pending() -> int:
        snap = job_queue.snapshot()
        return len(snap["queued"]) + (1 if snap["active"] else 0)

    @app.post("/api/bundle/save")
    def save_archive_bundle(req: BundleSaveRequest):
        """Pack the whole archive into one .archivesys file, ready to download."""
        if req.session is not None:
            try:
                save_session(_session_file(), req.session)
            except SessionError:
                pass    # a malformed position must not stop the archive being saved
        _sweep_pending(pending_downloads)
        work = Path(tempfile.mkdtemp(prefix="archivesys-save-"))
        try:
            report = save_bundle(db_manager, work, name=req.name)
        except (BundleError, OSError) as exc:
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Could not save the archive: {exc}")
        token = secrets.token_urlsafe(16)
        pending_downloads[token] = {"path": report.path, "dir": work, "at": time.time()}
        return {
            **report.as_dict(),
            "filename": report.path.name,
            "download_url": f"/api/bundle/download/{token}",
            "jobs_pending": _jobs_pending(),
        }

    @app.get("/api/bundle/download/{token}")
    def download_archive_bundle(token: str):
        entry = pending_downloads.pop(token, None)
        if not entry or not entry["path"].is_file():
            raise HTTPException(status_code=404, detail="That saved archive has expired. Save it again.")
        # octet-stream, not zip: Safari unpacks "safe" zip downloads on arrival.
        return FileResponse(
            str(entry["path"]),
            filename=entry["path"].name,
            media_type="application/octet-stream",
            background=BackgroundTask(shutil.rmtree, entry["dir"], ignore_errors=True),
        )

    @app.post("/api/bundle/upload")
    async def upload_archive_bundle(request: Request, filename: str = Query("archive" + BUNDLE_SUFFIX)):
        """Receive an .archivesys file and say what it holds, without opening it yet.

        The body is the raw file rather than a multipart form, so a large
        archive is written to disk once instead of being spooled twice.
        """
        _sweep_pending(pending_uploads)
        work = Path(tempfile.mkdtemp(prefix="archivesys-open-"))
        path = work / (Path(filename).name or "archive" + BUNDLE_SUFFIX)
        try:
            with path.open("wb") as out:
                async for chunk in request.stream():
                    out.write(chunk)
            info = inspect_bundle(path)
        except BundleError as exc:
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(status_code=400, detail=str(exc))
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        token = secrets.token_urlsafe(16)
        pending_uploads[token] = {"path": path, "dir": work, "at": time.time()}
        return {
            "token": token,
            "filename": path.name,
            "bundle": info,
            "current": db_manager.archive_counts(),
        }

    @app.delete("/api/bundle/upload/{token}")
    async def discard_archive_upload(token: str):
        entry = pending_uploads.pop(token, None)
        if entry:
            shutil.rmtree(entry["dir"], ignore_errors=True)
        return {"status": "ok"}

    @app.post("/api/bundle/restore/{token}")
    def restore_archive_bundle(token: str):
        """Open an uploaded archive in place of the current one."""
        entry = pending_uploads.get(token)
        if not entry:
            raise HTTPException(status_code=404, detail="That file is no longer here. Choose it again.")
        # OCR writes into the database; swapping it underneath a running job
        # would land that page's text on whatever now has the same id.
        if _jobs_pending():
            raise HTTPException(
                status_code=409,
                detail="Pages are still being read. Let Activity finish, or stop it, then open the archive.",
            )
        pending_uploads.pop(token, None)
        try:
            report = restore_bundle(entry["path"], db_manager)
        except BundleError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            shutil.rmtree(entry["dir"], ignore_errors=True)
        return {"status": "ok", **report.as_dict()}

    # -------------------------------------------------------------------------
    # Pipeline Settings & Tuning Bench
    # -------------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings():
        """Current pipeline settings plus the option lists the UI needs to render."""
        return {
            "settings": asdict(load_settings()),
            "model_sets": available_model_sets(),
            "ocr_inputs": OCR_INPUTS,
        }

    @app.post("/api/settings")
    async def update_settings(payload: Dict[str, Any]):
        return {"status": "ok", "settings": asdict(save_settings(payload or {}))}

    @app.post("/api/settings/reset")
    async def restore_settings():
        return {"status": "ok", "settings": asdict(reset_settings())}

    @app.post("/api/settings/benchmark")
    async def benchmark_settings(req: BenchmarkRequest):
        """Run one scan through candidate settings and report what each produced.

        Which model set, which preprocessing and whether the second pass earns its
        cost are all collection-specific questions. This runs the comparison on a
        real page from the archive so the choice can be made from evidence rather
        than assumption. If reference text is supplied, character error rate is
        reported against it; otherwise engine confidence and timing are.
        """
        target = _resolve_scan_path(req.doc_id, req.file_path)
        base = asdict(load_settings())
        variants = req.variants or [{}]
        if len(variants) > 6:
            raise HTTPException(status_code=400, detail="At most 6 variants per run")

        results = []
        for overrides in variants:
            trial = {**base, **(overrides or {})}
            try:
                results.append(_run_trial(target, trial, req.reference_text))
            except Exception as e:
                results.append({"label": _trial_label(trial), "error": str(e)})
        return {"scan": str(target), "results": results}

    def _trial_label(trial: Dict[str, Any]) -> str:
        return (f"{trial['model_set']} / {trial['ocr_input']} / "
                f"{'two-pass' if trial['refine_enabled'] else 'single'}")

    def _run_trial(target: Path, trial: Dict[str, Any], reference: Optional[str]) -> Dict[str, Any]:
        from src.config import PipelineSettings
        from src.ingestion.ocr_engine import LocalTesseractEngine

        cfg = PipelineSettings(**trial).clamped()
        pre = HistoricalPreprocessor(
            clahe_clip_limit=cfg.clahe_clip_limit,
            deskew_deadband=cfg.deskew_deadband,
        )
        engine = LocalTesseractEngine(
            tessdata_dir=cfg.tessdata_path() if cfg.tessdata_path().exists() else None,
            refine=cfg.refine_enabled,
            refine_threshold=cfg.refine_threshold,
            refine_margin=cfg.refine_margin,
        )

        started = time.time()
        processed = pre.process(target, deskew=cfg.deskew_enabled)
        image = processed.get(cfg.ocr_input, processed["ocr_ready"])
        result = engine.extract(image, preferred_lang=cfg.default_lang)
        elapsed = time.time() - started

        out = {
            "label": _trial_label(cfg.__dict__),
            "settings": asdict(cfg),
            "seconds": round(elapsed, 2),
            "confidence": result.confidence,
            "engine": result.engine_name,
            "deskew_angle": round(float(processed["deskew_angle"]), 2),
            "lines": result.raw_metadata.get("lines", 0),
            "refined_lines": result.raw_metadata.get("refined_lines", 0),
            "characters": len(result.transcription),
            "transcription": result.transcription,
        }
        if reference and reference.strip():
            out["cer"] = round(_character_error_rate(reference, result.transcription) * 100, 2)
        return out

    @app.get("/api/kinds")
    async def get_kinds():
        """The closed half of the taxonomy: seven kinds, with their OCR behaviour.

        The studio reads `textual` from here rather than keeping its own copy.
        v1's copy listed four values, three of which the UI could not produce.
        """
        return db_manager.list_kinds()

    @app.get("/api/tags")
    async def get_tags():
        """The open half: whatever curators have actually written, by frequency."""
        return db_manager.list_tags()

    return app
