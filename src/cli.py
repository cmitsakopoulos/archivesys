"""Unified Command Line Interface for Family Archive Engine.

Commands:
  open      Launch the Archive Platform in your browser (or open a saved .archivesys file)
  save      Save the whole archive -- scans, records, where you are -- into one .archivesys file
  verify    Check every file in a saved .archivesys against its fingerprint
  export    Compile SQLite archive into standalone portable HTML
  status    Display archive summary tables (documents, people, events)
  ingest    Process a scan or directory using OpenCV and local OCR engine
  clean     Purge missing document records, orphaned events, and stale assets
  seed      Populate archive with authentic multilingual historical sample records
  serve     Serve the static dashboard locally
  help      Show help and usage information
"""

from __future__ import annotations
import argparse
import os
import sys
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn

from src.bundle import BUNDLE_SUFFIX, BundleError, restore_bundle, save_bundle, verify_bundle
from src.database.db_manager import DatabaseManager
from src.ingestion.preprocess import HistoricalPreprocessor
from src.ingestion.ocr_engine import build_tuned_pipeline, get_ocr_engine
from src.curation.server import create_curation_app
from src.visualizer.compiler import DashboardCompiler

console = Console()
DEFAULT_DB_PATH = Path("data/archive.db")
DEFAULT_SCANS_DIR = Path("data")


def get_db() -> DatabaseManager:
    return DatabaseManager(DEFAULT_DB_PATH)


def cmd_init_db(args: argparse.Namespace) -> None:
    """Initialize database schema and triggers."""
    db = get_db()
    db.init_db()
    console.print(f"[bold green][OK] Database initialized successfully at {DEFAULT_DB_PATH}[/bold green]")


def cmd_status(args: argparse.Namespace) -> None:
    """Display summary tables of documents, people, and events in the archive."""
    db = get_db()
    db.init_db()

    docs = db.list_documents()
    people = db.list_people()
    data = db.get_timeline_export_data()
    events = data.get("events", [])

    reviewed_count = sum(1 for d in docs if d.get("reviewed"))
    excluded_count = sum(1 for d in docs if d.get("excluded"))
    undated_count = sum(1 for d in docs if not d.get("edtf_date"))

    # Header Panel
    summary_text = (
        f"[bold]Archive Database:[/bold] {DEFAULT_DB_PATH}\n"
        f"[bold]Documents:[/bold] {len(docs)} total ([green]{reviewed_count} reviewed[/green], "
        f"[yellow]{len(docs) - reviewed_count} to review[/yellow]) | "
        f"[bold]People:[/bold] {len(people)} | [bold]On timeline:[/bold] {len(events)}"
    )
    if undated_count or excluded_count:
        summary_text += (
            f"\n[dim]Off the timeline: {undated_count} undated, {excluded_count} excluded[/dim]"
        )
    console.print(Panel(summary_text, title="Archive Status", border_style="cyan"))

    # Documents Table
    if docs:
        doc_table = Table(title="Archival Documents", show_lines=True)
        doc_table.add_column("ID", justify="right", style="cyan", width=5)
        doc_table.add_column("File Name", style="bold", min_width=24)
        doc_table.add_column("Language", style="magenta", width=10)
        doc_table.add_column("Kind", style="blue", width=12)
        doc_table.add_column("Date", style="blue", width=14)
        doc_table.add_column("Review", width=11)
        doc_table.add_column("Conf.", justify="right", width=6)

        for d in docs:
            if d.get("excluded"):
                review = "[red]EXCLUDED[/red]"
            elif d.get("reviewed"):
                review = "[green]REVIEWED[/green]"
            else:
                review = "[yellow]TO REVIEW[/yellow]"
            doc_table.add_row(
                str(d["id"]),
                Path(d["file_path"]).name,
                d.get("language", "el-poly"),
                d.get("kind", "document"),
                d.get("date_display") or "[dim]undated[/dim]",
                review,
                f"{d.get('confidence_score', 1.0):.2f}",
            )
        console.print(doc_table)

    # People Table
    if people:
        p_table = Table(title="Catalogued People", show_lines=True)
        p_table.add_column("ID", justify="right", style="cyan", width=5)
        p_table.add_column("First Name", style="bold")
        p_table.add_column("Last Name", style="bold")
        p_table.add_column("Birth (EDTF)", style="dim")
        p_table.add_column("Death (EDTF)", style="dim")

        for p in people:
            p_table.add_row(
                str(p["id"]),
                p["first_name"],
                p["last_name"],
                p.get("birth_edtf") or "-",
                p.get("death_edtf") or "-",
            )
        console.print(p_table)


def cmd_clean(args: argparse.Namespace) -> None:
    """Purge missing documents, sample data (optional), orphaned events, and stale assets."""
    db = get_db()
    db.init_db()

    purge_samples = getattr(args, "purge_samples", False)
    console.print("[cyan][INFO] Scanning archive database for missing documents and orphaned records...[/cyan]")
    if purge_samples:
        console.print("[yellow][INFO] Sample data purge enabled: Purging synthetic sample records...[/yellow]")

    # Run DB purge
    results = db.purge_missing_documents(purge_sample_data=purge_samples)
    purged_docs = results["purged_documents"]
    purged_events = results["purged_events"]

    # If purge_samples is True, remove sample files from data/sample_data
    sample_files_removed = 0
    if purge_samples:
        sample_dir = Path("data/sample_data")
        if sample_dir.exists():
            for f in sample_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        sample_files_removed += 1
                    except OSError:
                        pass

    # Clean stale scans in dist/assets/scans
    dist_scans = Path("dist/assets/scans")
    scans_removed = 0
    if dist_scans.exists():
        active_docs = db.list_documents()
        active_names = {Path(d["file_path"]).name for d in active_docs if Path(d["file_path"]).exists()}
        for scan_file in dist_scans.iterdir():
            if scan_file.is_file() and scan_file.name not in active_names:
                try:
                    scan_file.unlink()
                    scans_removed += 1
                except OSError:
                    pass

    # Clean report table
    table = Table(title="Archive Clean Report", show_lines=True)
    table.add_column("Clean Operation", style="bold")
    table.add_column("Count", justify="right", style="cyan")
    table.add_row("Purged Document Records", str(purged_docs))
    table.add_row("Purged Orphaned Events", str(purged_events))
    if purge_samples:
        table.add_row("Purged Sample People", str(results.get("purged_people", 0)))
        table.add_row("Deleted Sample Scans on Disk", str(sample_files_removed))
    table.add_row("Removed Stale Dist Scan Assets", str(scans_removed))
    console.print(table)

    # Automatically recompile dist/index.html if dist/ exists
    if Path("dist/index.html").exists() or Path("dist").exists():
        compiler = DashboardCompiler(db, "dist")
        compiler.compile(copy_scans=True)
        console.print("[green][OK] Recompiled clean static dashboard to dist/index.html[/green]")


def cmd_seed(args: argparse.Namespace) -> None:
    """Seed database with synthetic multilingual historical records and scans."""
    from PIL import Image, ImageDraw
    import numpy as np
    import cv2

    db = get_db()
    db.init_db()

    scans_dir = Path("data/sample_data")
    scans_dir.mkdir(parents=True, exist_ok=True)

    console.print("[cyan][INFO] Generating authentic historical sample scans and archival records...[/cyan]")

    def create_vintage_scan(filename: str, title: str, subtitle: str, body: str, seal_color: tuple) -> Path:
        w, h = 900, 1200
        img = np.full((h, w, 3), (242, 246, 250), dtype=np.uint8)
        noise = np.random.normal(0, 5, (h, w, 3)).astype(np.int16)
        img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)

        cv2.rectangle(img, (30, 30), (w - 30, h - 30), (80, 95, 120), 2)
        cv2.rectangle(img, (38, 38), (w - 38, h - 38), (120, 135, 160), 1)

        pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil_img)

        seal_x, seal_y, seal_r = w - 160, h - 180, 60
        draw.ellipse([seal_x - seal_r, seal_y - seal_r, seal_x + seal_r, seal_y + seal_r], outline=seal_color, width=3)
        draw.text((seal_x - 30, seal_y - 10), "OFFICIAL", fill=seal_color)

        draw.text((70, 70), title, fill=(30, 30, 40))
        draw.text((70, 110), subtitle, fill=(60, 70, 90))
        y_offset = 180
        for line in body.split("\n"):
            draw.text((70, y_offset), line, fill=(20, 25, 35))
            y_offset += 28

        out_path = scans_dir / filename
        pil_img.save(out_path)
        return out_path

    # Document 1: Polytonic Katharevousa Birth Certificate (1923)
    p1 = create_vintage_scan(
        "1923_birth_certificate.png",
        "ΒΑΣΙΛΕΙΟΝ ΤΗΣ ΕΛΛΑΔΟΣ — ΔΗΜΑΡΧΙΑ ΑΘΗΝΑΙΩΝ",
        "ΠΙΣΤΟΠΟΙΗΤΙΚΟΝ ΓΕΝΝΗΣΕΩΣ (Αριθ. 1428 / 1923)",
        (
            "Πιστοποιοῦμεν ὅτι ἐν Ἀθήναις τῇ 14ῃ Μαΐου 1923 ἐγεννήθη ἄρρεν τέκνον\n"
            "ὑπὸ τῶν νομίμων συζύγων Γεωργίου Θεοδώρου, ἐμπόρου, καὶ\n"
            "Ἑλένης τὸ γένος Βασιλείου Παπαδοπούλου.\n\n"
            "Τὸ βρέφος ὠνομάσθη Χρῆστος κατὰ τὸ ἱερὸν μυστήριον τοῦ βαπτίσματος.\n"
            "Ἀνάδοχος παρέστη ὁ Κωνσταντῖνος Οἰκονόμου, δικηγόρος.\n\n"
            "Ἐν Ἀθήναις, τῇ 20ῇ Μαΐου 1923\n"
            "Ὁ Ληξίαρχος Ἀθηνῶν"
        ),
        seal_color=(40, 60, 140),
    )

    # Document 2: French Consular Passport / Smyrna Evacuation (1922)
    p2 = create_vintage_scan(
        "1922_smyrna_consular_passport.png",
        "RÉPUBLIQUE FRANÇAISE — CONSULAT GÉNÉRAL À SMYRNE",
        "PASSEPORT CONSULAIRE ET LAISSER-PASSER D'URGENCE (N° 8492)",
        (
            "Au nom de la République Française,\n"
            "Prière aux Autorités Militaires et Civiles de laisser passer librement:\n"
            "Monsieur Georges Theodorou, négociant, né à Smyrne (Asie Mineure),\n"
            "accompagné de son épouse Madame Hélène Theodorou.\n\n"
            "Destination: Évacuation maritime vers le port du Pirée (Grèce).\n"
            "Motif: Événements de guerre et rapatriement sanitaire.\n\n"
            "Délivré à Smyrne, le 12 Septembre 1922.\n"
            "Le Consul Général de France"
        ),
        seal_color=(150, 40, 40),
    )

    # Document 3: English Alien Passenger Manifest (1928)
    p3 = create_vintage_scan(
        "1928_ss_byron_manifest.png",
        "UNITED STATES IMMIGRATION SERVICE — PORT OF NEW YORK",
        "LIST OR MANIFEST OF ALIEN PASSENGERS FOR THE S.S. BYRON",
        (
            "Arriving at Port of New York, October 24th, 1928 from Piraeus, Greece.\n\n"
            "Line 17: Theodorou, Christos. Age: 5. Sex: M. Nationality: Greek.\n"
            "Father: Georgios Theodorou, Athens.\n"
            "Final Destination: Chicago, Illinois.\n"
            "Accompanying relative: Mother, Eleni Theodorou, Age: 26.\n"
            "Inspected and permitted entry at Ellis Island."
        ),
        seal_color=(30, 100, 50),
    )

    # Document 4: Polytonic Family Photograph Notes (1935)
    p4 = create_vintage_scan(
        "1935_piraeus_garden_photo.png",
        "ΟΙΚΟΓΕΝΕΙΑΚΟΝ ΑΡΧΕΙΟΝ — ΑΝΑΜΝΗΣΕΙΣ ΠΕΙΡΑΙΩΣ",
        "ΦΩΤΟΓΡΑΦΙΑ ΕΝ ΤΩι ΚΗΠΩι ΤΗΣ ΟΙΚΙΑΣ (περὶ τὸ 1935)",
        (
            "Ἀπὸ ἀριστερῶν πρὸς τὰ δεξιά:\n"
            "Ἑλένη Θεοδώρου, ὁ μικρὸς Χρῆστος καὶ ὁ Γεώργιος Θεοδώρου.\n\n"
            "«Εἰς ἀνάμνησιν τῶν εἰρηνικῶν ἐτῶν πρὸ τῆς μεγάλης δοκιμασίας τοῦ πολέμου.\n"
            "Πειραιεύς, περὶ τὸν Μάϊον τοῦ 1935»."
        ),
        seal_color=(100, 70, 30),
    )

    p_george = db.get_or_create_person("Γεώργιος", "Θεοδώρου")
    p_eleni = db.get_or_create_person("Ἑλένη", "Θεοδώρου")
    p_christos = db.get_or_create_person("Χρῆστος", "Θεοδώρου")

    # Each scan is described once, here. In v1 this same block wrote four
    # documents and then four events that repeated their titles, dates,
    # locations and people back at them -- and the four events had to be kept
    # in step with the four documents by hand.
    doc1 = db.add_document(
        file_path=str(p1),
        thumbnail_path=str(p1),
        kind="certificate",
        language="el-poly",
        title="Γέννησις Χρήστου Θεοδώρου",
        edtf_date="1923-05-14",
        location="Ἀθῆναι",
        transcription=(
            "ΒΑΣΙΛΕΙΟΝ ΤΗΣ ΕΛΛΑΔΟΣ — ΔΗΜΑΡΧΙΑ ΑΘΗΝΑΙΩΝ\n"
            "ΠΙΣΤΟΠΟΙΗΤΙΚΟΝ ΓΕΝΝΗΣΕΩΣ (Αριθ. 1428 / 1923)\n"
            "Πιστοποιοῦμεν ὅτι ἐν Ἀθήναις τῇ 14ῃ Μαΐου 1923 ἐγεννήθη ἄρρεν τέκνον "
            "ὑπὸ τῶν νομίμων συζύγων Γεωργίου Θεοδώρου, ἐμπόρου, καὶ Ἑλένης τὸ γένος Βασιλείου Παπαδοπούλου. "
            "Τὸ βρέφος ὠνομάσθη Χρῆστος."
        ),
        confidence_score=0.99,
    )
    db.update_document(doc1, fields={
        "summary": "Ἐπίσημον πιστοποιητικὸν γεννήσεως ἐκδοθὲν ὑπὸ τοῦ Ληξιαρχείου Ἀθηνῶν.",
        "creator": "Ληξιαρχεῖον Ἀθηνῶν",
        "tags": ["life event", "birth"],
    }, reviewed=True, reviewed_by="seed")

    doc2 = db.add_document(
        file_path=str(p2),
        thumbnail_path=str(p2),
        kind="passport",
        language="fr",
        title="Evacuation de Smyrne vers le Piree",
        edtf_date="1922-09-12",
        location="Smyrne (Asia Minor)",
        transcription=(
            "RÉPUBLIQUE FRANÇAISE — CONSULAT GÉNÉRAL À SMYRNE\n"
            "PASSEPORT CONSULAIRE ET LAISSER-PASSER D'URGENCE (N° 8492)\n"
            "Au nom de la République Française, prière de laisser passer librement: "
            "Monsieur Georges Theodorou et son épouse Madame Hélène Theodorou. "
            "Évacuation maritime vers le port du Pirée. Délivré à Smyrne, le 12 Septembre 1922."
        ),
        confidence_score=0.98,
    )
    db.update_document(doc2, fields={
        "summary": "Passeport consulaire francais emis en urgence lors des evenements de Smyrne.",
        "creator": "Consulat Général de France à Smyrne",
        "tags": ["migration", "refugee"],
    }, reviewed=True, reviewed_by="seed")

    doc3 = db.add_document(
        file_path=str(p3),
        thumbnail_path=str(p3),
        kind="document",
        language="en",
        title="Arrival at New York aboard S.S. Byron",
        edtf_date="1928-10-24",
        location="New York (Ellis Island)",
        transcription=(
            "UNITED STATES IMMIGRATION SERVICE — PORT OF NEW YORK\n"
            "LIST OR MANIFEST OF ALIEN PASSENGERS FOR THE S.S. BYRON\n"
            "Arriving at Port of New York, October 24th, 1928 from Piraeus, Greece.\n"
            "Line 17: Theodorou, Christos. Age: 5. Nationality: Greek. "
            "Destination: Chicago, Illinois. Accompanying relative: Mother, Eleni Theodorou."
        ),
        confidence_score=0.95,
    )
    db.update_document(doc3, fields={
        "summary": "Christos and Eleni Theodorou arrive at Ellis Island en route to Chicago.",
        "creator": "United States Immigration Service",
        "tags": ["migration", "ship manifest"],
    })

    doc4 = db.add_document(
        file_path=str(p4),
        thumbnail_path=str(p4),
        kind="photograph",
        language="el-poly",
        title="Οἰκογενειακὴ Διαμονὴ ἐν Πειραιεῖ",
        edtf_date="1935~",
        location="Πειραιεύς",
        transcription=(
            "ΟΙΚΟΓΕΝΕΙΑΚΟΝ ΑΡΧΕΙΟΝ — ΑΝΑΜΝΗΣΕΙΣ ΠΕΙΡΑΙΩΣ\n"
            "ΦΩΤΟΓΡΑΦΙΑ ΕΝ ΤΩι ΚΗΠΩι ΤΗΣ ΟΙΚΙΑΣ (περὶ τὸ 1935)\n"
            "Ἑλένη Θεοδώρου, ὁ μικρὸς Χρῆστος καὶ ὁ Γεώργιος Θεοδώρου. "
            "Πειραιεύς, περὶ τὸν Μάϊον τοῦ 1935."
        ),
        confidence_score=0.91,
    )
    db.update_document(doc4, fields={
        "summary": "Φωτογραφικὴ ἀνάμνησις τῆς οἰκογενείας ἐν τῷ κήπῳ τοῦ οἴκου των ἐν Πειραιεῖ.",
        "tags": ["life event", "family"],
    })

    db.link_document_person(doc1, p_christos, role="subject")
    db.link_document_person(doc1, p_george, role="father")
    db.link_document_person(doc1, p_eleni, role="mother")

    db.link_document_person(doc2, p_george, role="subject")
    db.link_document_person(doc2, p_eleni, role="spouse")

    db.link_document_person(doc3, p_christos, role="subject")
    db.link_document_person(doc3, p_eleni, role="mother")

    db.link_document_person(doc4, p_george, role="subject")
    db.link_document_person(doc4, p_eleni, role="subject")
    db.link_document_person(doc4, p_christos, role="subject")

    console.print(
        "[bold green][OK] Seeded 4 documents and 3 people. "
        "All four documents project onto the timeline from their own fields.[/bold green]"
    )


def cmd_ingest(args: argparse.Namespace) -> None:
    """Ingest a single historical scan or an entire folder of scans using OpenCV and local OCR."""
    scan_path = Path(args.path)
    if not scan_path.exists():
        console.print(f"[bold red][ERROR] Path not found:[/bold red] {scan_path}")
        sys.exit(1)

    db = get_db()
    db.init_db()

    run_ocr = getattr(args, "ocr", False)
    if getattr(args, "no_ocr", False) or getattr(args, "command", "") == "index":
        run_ocr = False

    # Without OCR and without a --type, each scan is sorted by the text detector
    # (text pages -> document, photographs -> photograph). v1 defaulted every
    # scan to "photograph", which switched off its reading.
    default_type = getattr(args, "type", None) or ("document" if run_ocr else None)

    if scan_path.is_dir():
        files_to_process = [
            p for p in scan_path.rglob("*")
            if p.is_file() and p.suffix.lower() in HistoricalPreprocessor.SUPPORTED_EXTENSIONS
        ]
        if not files_to_process:
            console.print(f"[yellow][WARN] No supported scan files found in directory: {scan_path}[/yellow]")
            return
        mode_label = "[cyan]Full Ingest & OCR[/cyan]" if run_ocr else "[yellow]Quick Index (Instant)[/yellow]"
        console.print(f"[bold cyan]Batch Ingestion ({mode_label}):[/bold cyan] Found [bold yellow]{len(files_to_process)}[/bold yellow] scan(s) in {scan_path}")
    else:
        files_to_process = [scan_path]

    if not run_ocr:
        # Fast path: quick index without loading heavy OCR pipeline
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Indexing scans...", total=len(files_to_process))
            for fpath in files_to_process:
                progress.update(task, description=f"Indexing {fpath.name}")
                try:
                    doc_id = db.index_scan(
                        file_path=str(fpath.resolve()),
                        kind=default_type,
                    )
                    kind = (db.get_document(doc_id) or {}).get("kind")
                    console.print(f"  [green][OK] Indexed document #{doc_id}: {fpath.name} (Type: {kind})[/green]")
                except Exception as e:
                    console.print(f"  [bold red][ERROR] Failed to index {fpath.name}: {e}[/bold red]")
                progress.advance(task)
        console.print(f"\n[bold green][OK] Quick indexing complete. Run 'archivesys open' to curate records.[/bold green]")
        return

    # OCR path
    preprocessor, engine, cfg = build_tuned_pipeline(getattr(args, "engine", "auto"))
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Ingesting scans...", total=len(files_to_process))

        for fpath in files_to_process:
            progress.update(task, description=f"Processing {fpath.name}")
            try:
                processed = preprocessor.process(fpath, deskew=cfg.deskew_enabled)
                deskew_angle = processed["deskew_angle"]
                result = engine.extract(processed.get(cfg.ocr_input, processed["ocr_ready"]),
                                        preferred_lang=args.lang or cfg.default_lang)

                doc_id = db.add_document(
                    file_path=str(fpath.resolve()),
                    thumbnail_path=str(fpath.resolve()),
                    kind=getattr(args, "type", None) or result.document_type,
                    language=result.language,
                    transcription=result.transcription,
                    confidence_score=result.confidence,
                )
                console.print(f"  [green][OK] Ingested document #{doc_id}: {fpath.name} (Lang: {result.language}, Conf: {result.confidence:.2f}, Tilt: {deskew_angle:.2f} deg)[/green]")
            except Exception as e:
                console.print(f"  [bold red][ERROR] Failed to process {fpath.name}: {e}[/bold red]")
            progress.advance(task)

    console.print(f"\n[bold green][OK] Ingestion complete. Run 'archivesys open' to curate records.[/bold green]")


def find_available_port(start_port: int = 8000, max_attempts: int = 25) -> int:
    """Find the first available TCP port on localhost starting from start_port."""
    import socket
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start_port


def _open_browser_when_ready(host: str, port: int, url: str) -> None:
    """Poll the server socket in a background thread and only open the browser once the port is listening."""
    import threading
    import socket
    import time

    def _wait_and_open() -> None:
        for _ in range(60):  # Poll every 50ms for up to 3 seconds
            try:
                with socket.create_connection((host, port), timeout=0.05):
                    webbrowser.open(url)
                    return
            except OSError:
                time.sleep(0.05)
        # Fallback if polling timed out
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_wait_and_open, daemon=True).start()


def _bundle_progress(progress: Progress, task) -> Callable[[str, int, int], None]:
    def report(stage: str, done: int = 0, total: int = 0) -> None:
        progress.update(task, description=stage, completed=done, total=total or None)
    return report


def _bundle_spinner() -> Progress:
    return Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console, transient=True,
    )


def _local_time(iso: Optional[str]) -> str:
    """'2026-09-23T10:08:13+00:00' as the reader's own clock shows it."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y, %H:%M")
    except (TypeError, ValueError):
        return iso or "an unknown date"


def _human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def cmd_save(args: argparse.Namespace) -> None:
    """Save the whole archive into one .archivesys file."""
    db = get_db()
    db.init_db()
    dest = Path(args.path) if getattr(args, "path", None) else Path.cwd()

    with _bundle_spinner() as progress:
        task = progress.add_task("Preparing", total=None)
        try:
            report = save_bundle(db, dest, name=getattr(args, "name", None),
                                 progress=_bundle_progress(progress, task))
        except (BundleError, OSError) as exc:
            console.print(f"[bold red][ERROR] Could not save the archive:[/bold red] {exc}")
            sys.exit(1)

    body = (
        f"[bold]{report.path}[/bold]\n\n"
        f"  {report.documents} documents · {report.people} people · {report.files} scans · "
        f"{_human_size(report.total_bytes)}\n"
        f"  Every file is fingerprinted (SHA-256), so opening it later proves nothing changed.\n\n"
        f"  [dim]Open it here or on another computer:[/dim]  archivesys open \"{report.path.name}\""
    )
    if report.missing:
        body += f"\n\n  [yellow]{len(report.missing)} scan(s) could not be found and are not inside:[/yellow]"
        for m in report.missing[:8]:
            body += f"\n    #{m['doc_id']} {m['path']}"
    if report.changed:
        body += f"\n\n  [yellow]{len(report.changed)} scan(s) changed since they were indexed; saved as they are now.[/yellow]"
    console.print(Panel(body, title="[bold green]Archive saved[/bold green]", border_style="green"))


def cmd_verify(args: argparse.Namespace) -> None:
    """Check every file in a saved archive against its fingerprint."""
    with _bundle_spinner() as progress:
        task = progress.add_task("Verifying", total=None)
        try:
            result = verify_bundle(args.bundle, progress=_bundle_progress(progress, task))
        except BundleError as exc:
            console.print(f"[bold red][ERROR][/bold red] {exc}")
            sys.exit(1)

    if result["ok"]:
        console.print(Panel(
            f"[bold]{result['name']}[/bold] (saved {_local_time(result['saved_at'])})\n\n"
            f"  All {result['files']} files match their fingerprints · {_human_size(result['bytes'])}",
            title="[bold green]Intact[/bold green]", border_style="green",
        ))
        return
    body = "\n".join(f"  • {p}" for p in result["problems"][:25])
    console.print(Panel(body, title="[bold red]Damaged[/bold red]", border_style="red"))
    sys.exit(1)


def _open_saved_archive(db: DatabaseManager, bundle_path: Path) -> None:
    """Replace the current archive with a saved one, reporting what happened."""
    if not bundle_path.is_file():
        console.print(f"[bold red][ERROR] File not found:[/bold red] {bundle_path}")
        sys.exit(1)
    with _bundle_spinner() as progress:
        task = progress.add_task("Reading", total=None)
        try:
            report = restore_bundle(bundle_path, db, progress=_bundle_progress(progress, task))
        except BundleError as exc:
            console.print(f"[bold red][ERROR] Could not open {bundle_path.name}:[/bold red] {exc}")
            console.print("[dim]Nothing was changed.[/dim]")
            sys.exit(1)

    body = f"[bold]{report.name}[/bold] — {report.documents} documents, {report.people} people"
    if report.relinked:
        body += f"\n  {report.relinked} scan(s) were already on this computer and are linked where they are"
    if report.unpacked:
        body += f"\n  {report.unpacked} scan(s) unpacked into {report.library}"
    for m in report.missing[:8]:
        body += f"\n  [yellow]#{m['doc_id']} has no scan ({m['reason']}): {m['path']}[/yellow]"
    for w in report.warnings:
        body += f"\n  [yellow]{w}[/yellow]"
    if report.backup:
        body += f"\n\n  [dim]What was open before is saved in:[/dim]\n  {report.backup}\n  [dim]Open that file to go back.[/dim]"
    console.print(Panel(body, title="[bold green]Archive opened[/bold green]", border_style="green"))


def cmd_open(args: argparse.Namespace) -> None:
    """Launch the Archive Dashboard & Curation Studio directly in the browser."""
    db = get_db()
    db.init_db()

    bundle_arg = getattr(args, "bundle", None)
    if bundle_arg:
        _open_saved_archive(db, Path(bundle_arg).expanduser())

    req_port = getattr(args, "port", None) or 8000
    port = find_available_port(req_port)
    app = create_curation_app(db, DEFAULT_SCANS_DIR)
    url = f"http://127.0.0.1:{port}/curate"

    banner_text = (
        f"[bold white]ArchiveSys Platform Running[/bold white]\n\n"
        f"  • [bold cyan]Local Studio:[/bold cyan]  [link={url}]{url}[/link]\n"
        f"  • [bold cyan]Database:[/bold cyan]      {DEFAULT_DB_PATH}\n"
        f"  • [dim]Press [bold red]Ctrl+C[/bold red] to stop server cleanly[/dim]"
    )
    console.print(Panel(banner_text, title="[bold green]ArchiveSys[/bold green]", border_style="green"))

    if not getattr(args, "no_browser", False):
        _open_browser_when_ready("127.0.0.1", port, url)

    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        console.print("\n[dim]ArchiveSys server shut down cleanly.[/dim]")


def cmd_export(args: argparse.Namespace) -> None:
    """Compile SQLite records into standalone portable static dashboard."""
    db = get_db()
    db.init_db()
    compiler = DashboardCompiler(db, output_dir=args.output, quality=getattr(args, "quality", None))
    out_file = compiler.compile(copy_scans=True)
    console.print(f"[bold green][OK] Static timeline dashboard compiled successfully:[/bold green] {out_file}")
    console.print(f"[cyan]Scans published at '{compiler.quality}' quality in {out_file.parent / 'assets' / 'scans'}[/cyan]")


def cmd_serve(args: argparse.Namespace) -> None:
    """Serve compiled dist/ folder locally."""
    import http.server
    import socketserver

    dist_dir = Path("dist")
    if not (dist_dir / "index.html").exists():
        console.print("[yellow][WARN] dist/index.html not found. Exporting first...[/yellow]")
        cmd_export(argparse.Namespace(output="dist"))

    port = args.port
    os.chdir(dist_dir)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}"
        console.print(f"[bold green]Serving archive dashboard at {url}[/bold green]")
        if not args.no_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("\n[dim]Static server stopped.[/dim]")


def main() -> None:
    """Main CLI entrypoint for archivesys."""
    # If no arguments provided, launch the web platform immediately
    if len(sys.argv) == 1:
        cmd_open(argparse.Namespace(port=8000, no_browser=False, bundle=None))
        return
    # `archivesys Family.archivesys` -- what a file association, or dragging
    # the file onto a terminal, produces.
    if len(sys.argv) == 2 and sys.argv[1].lower().endswith(BUNDLE_SUFFIX) and Path(sys.argv[1]).is_file():
        cmd_open(argparse.Namespace(port=8000, no_browser=False, bundle=sys.argv[1]))
        return

    parser = argparse.ArgumentParser(
        prog="archivesys",
        description="Historical Family Archive & Timeline Projection Engine"
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    # 1. open
    p_open = subparsers.add_parser("open", help="Launch the Archive Platform in your browser")
    p_open.add_argument("bundle", nargs="?", default=None,
                        help="A saved .archivesys file to open first. The current archive is "
                             "backed up, and you land exactly where the file was saved.")
    p_open.add_argument("--port", type=int, default=8000, help="Target local port (default: 8000)")
    p_open.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")

    # 1b. save
    p_save = subparsers.add_parser(
        "save", help="Save the whole archive (scans, records, where you are) into one .archivesys file")
    p_save.add_argument("path", nargs="?", default=None,
                        help="A folder (a dated name is chosen) or a file name (default: current folder)")
    p_save.add_argument("--name", default=None, help='A name for the archive (default: "Family Archive <date>")')

    # 1c. verify
    p_verify = subparsers.add_parser("verify", help="Check a saved .archivesys file against its fingerprints")
    p_verify.add_argument("bundle", help="The .archivesys file to check")

    # 2. export
    p_export = subparsers.add_parser("export", help="Export standalone portable timeline HTML")
    p_export.add_argument("--output", default="dist", help="Output directory (default: dist)")
    p_export.add_argument("--quality", choices=["original", "high", "compact"], default=None,
                          help="How scans are published: original files (no loss), full-resolution JPEG, "
                               "or 2400 px JPEG (default: the studio setting, 'original' unless changed)")

    # 3. status
    subparsers.add_parser("status", help="View summary table of archive database")

    # 4. ingest
    p_ingest = subparsers.add_parser("ingest", help="Ingest a scan image or directory into archive")
    p_ingest.add_argument("path", help="Path to document scan or directory (JPG, PNG, PDF, TIFF, HEIC)")
    p_ingest.add_argument("--engine", choices=["auto", "tesseract", "kraken", "mock"], default="auto")
    p_ingest.add_argument("--lang", choices=["el-poly", "el", "fr", "en"], default=None)
    p_ingest.add_argument("--no-ocr", action="store_true", help="Quickly index scans without waiting for OCR (default behavior)")
    p_ingest.add_argument("--ocr", action="store_true", help="Run full OCR during ingestion (default: instant indexing without OCR)")
    p_ingest.add_argument("--type", default=None, help="Document type (e.g. photograph, document, certificate, letter)")

    # 4b. index (convenient alias for ingest --no-ocr)
    p_index = subparsers.add_parser("index", help="Quickly index scans without OCR (alias for ingest --no-ocr)")
    p_index.add_argument("path", help="Path to document scan or directory")
    p_index.add_argument("--type", default=None, help="Document type (default: sorted automatically into document or photograph)")

    # 5. seed
    subparsers.add_parser("seed", help="Seed archive with sample multilingual historical records")

    # 6. clean
    p_clean = subparsers.add_parser("clean", help="Purge missing document records and stale scan assets")
    p_clean.add_argument("--purge-samples", action="store_true", help="Also purge synthetic sample data and records")

    # 7. serve
    p_serve = subparsers.add_parser("serve", help="Serve compiled static dashboard locally")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--no-browser", action="store_true")

    # 8. help
    p_help = subparsers.add_parser("help", help="Show help and usage information")
    p_help.add_argument("topic", nargs="?", help="Optional command name to view detailed help for")

    args = parser.parse_args()

    def cmd_help(args: argparse.Namespace) -> None:
        topic = getattr(args, "topic", None)
        if topic and topic in subparsers.choices:
            subparsers.choices[topic].print_help()
        else:
            parser.print_help()

    commands = {
        "open": cmd_open,
        "save": cmd_save,
        "verify": cmd_verify,
        "export": cmd_export,
        "status": cmd_status,
        "ingest": cmd_ingest,
        "index": cmd_ingest,
        "seed": cmd_seed,
        "clean": cmd_clean,
        "serve": cmd_serve,
        "help": cmd_help,
    }

    cmd_fn = commands.get(args.command, cmd_open)
    cmd_fn(args)


if __name__ == "__main__":
    main()
