"""CLI Reviewer with native OS image projection (macOS QuickLook / Preview).

Enables rapid human-in-the-loop review of historical documents directly
from the terminal while projecting the high-resolution scan on the user's screen.
"""

from __future__ import annotations
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt
from rich.columns import Columns

from src.database.db_manager import DatabaseManager
from src.ingestion.polytonic import PolytonicNormalizer
from src.ingestion.ocr_engine import get_ocr_engine

console = Console()


class NativeReviewer:
    """Orchestrates interactive terminal curation with native OS image projection."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def project_image(self, image_path: str | Path) -> Optional[subprocess.Popen]:
        """Project the scan image using native OS viewer (QuickLook or Preview on macOS)."""
        path = Path(image_path).resolve()
        if not path.exists():
            console.print(f"[bold red][ERROR] Scan file not found:[/bold red] {path}")
            return None

        system = platform.system()
        try:
            if system == "Darwin":
                # Prefer QuickLook (-p) non-blocking
                return subprocess.Popen(
                    ["/usr/bin/qlmanage", "-p", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif system == "Linux":
                return subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif system == "Windows":
                os.startfile(str(path))  # type: ignore
                return None
        except Exception as e:
            try:
                return subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                console.print(f"[yellow][WARN] Could not launch native image viewer: {e}[/yellow]")
        return None

    def edit_text_in_editor(self, initial_text: str) -> str:
        """Open system $EDITOR to edit polytonic transcription and metadata."""
        editor = os.environ.get("EDITOR", "nano")
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".md", delete=False, encoding="utf-8") as tf:
            tf.write(initial_text)
            temp_path = tf.name

        try:
            subprocess.call([editor, temp_path])
            with open(temp_path, "r", encoding="utf-8") as f:
                edited = f.read()
            return edited
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def review_session(self, status_filter: str = "pending") -> None:
        """Run the interactive review loop over documents.

        `status_filter` is kept for the CLI's sake: "verified" revisits reviewed
        documents, anything else queues the ones still to review.
        """
        want_reviewed = status_filter == "verified"
        docs = self.db.list_documents(reviewed=want_reviewed)
        label = "reviewed" if want_reviewed else "still to review"
        if not docs:
            console.print(f"[bold green][OK] No documents {label}![/bold green]")
            return

        header_text = (
            f"[bold cyan]Historical Archive Curation Studio (Terminal Mode)[/bold cyan]\n"
            f"Found [bold yellow]{len(docs)}[/bold yellow] document(s) {label}.\n"
            f"Native image projection enabled via macOS QuickLook."
        )
        console.print(Panel(header_text, title="Archival Curation", border_style="cyan"))

        for i, doc in enumerate(docs, 1):
            scan_path = doc["file_path"]
            doc_id = doc["id"]

            console.rule(f"[bold yellow]Document {i}/{len(docs)} (ID: {doc_id})[/bold yellow]")

            # Project image
            proc = self.project_image(scan_path)

            try:
                self._review_single_document(doc)
            finally:
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

        console.print("[bold green][OK] Review queue completed.[/bold green]")

    def _review_single_document(self, doc: Dict[str, Any]) -> None:
        doc_id = doc["id"]
        transcription = doc.get("transcription") or ""
        language = doc.get("language") or "el-poly"
        kind = doc.get("kind") or "document"
        status = "reviewed" if doc.get("reviewed") else "to review"

        while True:
            # Metadata Table Panel (Left)
            meta_table = Table(show_header=False, box=None)
            meta_table.add_row("[bold]Document ID:[/bold]", str(doc_id))
            meta_table.add_row("[bold]Scan File:[/bold]", Path(doc["file_path"]).name)
            meta_table.add_row("[bold]Full Path:[/bold]", str(doc["file_path"]))
            meta_table.add_row("[bold]Language:[/bold]", f"[cyan]{language}[/cyan]")
            meta_table.add_row("[bold]Kind:[/bold]", f"[magenta]{kind}[/magenta]")
            meta_table.add_row("[bold]Status:[/bold]", f"[yellow]{status}[/yellow]")
            meta_table.add_row("[bold]Confidence:[/bold]", f"{doc.get('confidence_score', 1.0):.2f}")
            meta_panel = Panel(meta_table, title="Metadata", border_style="blue")

            # Transcription Preview Panel (Right)
            preview_lines = transcription.strip().split("\n")
            preview_snippet = "\n".join(preview_lines[:12])
            if len(preview_lines) > 12:
                preview_snippet += f"\n... [{len(preview_lines) - 12} more lines in full text]"
            trans_panel = Panel(
                preview_snippet if preview_snippet else "[dim]No transcription recorded[/dim]",
                title=f"Transcription ({language})",
                border_style="green",
            )

            # Side-by-side terminal columns
            console.print(Columns([meta_panel, trans_panel], equal=True))

            console.print(
                "[bold][A][/bold]pprove and Verify  "
                "[bold][T][/bold] Edit Transcription ($EDITOR)  "
                "[bold][E][/bold] Edit Metadata  "
                "[bold][R][/bold]e-run OCR  "
                "[bold][O][/bold]pen Scan  "
                "[bold][S][/bold]kip  "
                "[bold][Q][/bold]uit"
            )
            choice = Prompt.ask("Action", choices=["a", "t", "e", "r", "o", "s", "q"], default="a").lower()

            if choice == "a":
                self.db.update_document(
                    doc_id=doc_id,
                    fields={"transcription": transcription, "language": language, "kind": kind},
                    reviewed=True,
                    reviewed_by="user-cli",
                )
                console.print(f"[bold green][OK] Document #{doc_id} reviewed and saved.[/bold green]\n")
                break

            elif choice == "t":
                console.print("[dim]Opening $EDITOR for transcription...[/dim]")
                edited = self.edit_text_in_editor(transcription)
                transcription = PolytonicNormalizer.normalize_exact(edited)
                console.print("[green][OK] Transcription updated in memory buffer.[/green]")

            elif choice == "e":
                new_lang = Prompt.ask(
                    "Language [el-poly/el/fr/en/mixed]",
                    default=language,
                )
                new_type = Prompt.ask(
                    "Kind [certificate/letter/photograph/passport/press/book/document]",
                    default=kind,
                )
                language = new_lang
                kind = new_type
                console.print("[green][OK] Metadata updated in memory buffer.[/green]")

            elif choice == "r":
                target_lang = Prompt.ask("Preferred OCR language", default=language)
                engine = get_ocr_engine("auto")
                result = engine.extract(doc["file_path"], preferred_lang=target_lang)
                transcription = result.transcription
                language = result.language
                kind = result.document_type
                console.print(f"[green][OK] OCR re-run completed ({result.engine_name}).[/green]")

            elif choice == "o":
                self.project_image(doc["file_path"])

            elif choice == "s":
                console.print(f"[yellow][INFO] Skipped Document #{doc_id}[/yellow]\n")
                break

            elif choice == "q":
                console.print("[bold red]Exiting review session.[/bold red]")
                raise SystemExit(0)
