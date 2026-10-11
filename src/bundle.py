"""Save the whole archive to one file, and open it again exactly as it was.

A .archivesys file is the archive as a single portable object: every master
scan byte for byte, every record, and where the curator was when it was saved.
Opening it -- on this computer or another -- restores all three.

Format
------
A ZIP file whose contents are a BagIt 1.0 bag (RFC 8493), the packaging
standard libraries and archives use to move collections between institutions:

    <bag>/
      bagit.txt                  declares the bag
      bag-info.txt               when, by what, and how much (Payload-Oxum)
      manifest-sha256.txt        SHA-256 of every payload file
      tagmanifest-sha256.txt     SHA-256 of the three files above
      data/
        archivesys.json          format version, file table, settings, session
        archive.db               the SQLite database, snapshotted consistently
        files/<folder>/<name>    each master scan, identical to the original

Why these choices:

* ZIP rather than a new container: standard library at both ends, random
  access (the studio reads the summary without unpacking gigabytes), ZIP64 for
  archives over 4 GB, and anyone can open it with tools already on their
  computer. Nothing is locked inside a format only ArchiveSys can read.
* Scans are stored, not recompressed. PNG and JPEG are already compressed:
  measured on this archive's own scans, deflate and LZMA both saved 0.0%.
  The same holds for Parquet or any other lossless codec.
* SQLite rather than an export format: the database is copied whole, so every
  column, link and id survives by construction, and a file saved by an older
  version is brought forward by the same migrations that upgrade a live one.
* BagIt, because fixity is the whole promise. Every file is checked against
  its fingerprint when the archive is opened, and any BagIt tool can verify a
  saved archive independently of ArchiveSys.

Opening never deletes or overwrites a file. A scan still at its original path
with the same fingerprint is linked back where it is; anything else is
unpacked into data/library. The archive being replaced is saved to
data/backups first, and everything is verified before anything is changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

from src import config
from src.database.db_manager import DatabaseManager
from src.session import SessionError, load_session, save_session, session_path

FORMAT = "archivesys-bundle"
FORMAT_VERSION = 1
BUNDLE_SUFFIX = ".archivesys"

_CHUNK = 1 << 20
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Already-compressed formats go in as they are. Recompressing them costs time
# and, measured on this archive's scans, saves nothing.
_STORED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif", ".jp2", ".pdf", ".zip"}

# Headroom kept free when unpacking, so opening an archive never fills a disk.
_DISK_MARGIN = 200 * 1024 * 1024

Progress = Callable[[str, int, int], None]


def _no_progress(stage: str, done: int = 0, total: int = 0) -> None:
    pass


class BundleError(Exception):
    """A saved archive could not be written, read, or trusted."""


def app_version() -> str:
    try:
        from importlib.metadata import version
        return version("archivesys")
    except Exception:
        return "unknown"


# =============================================================================
# Reports
# =============================================================================
@dataclass
class SaveReport:
    path: Path
    name: str
    documents: int
    people: int
    files: int                        # scans packed into the file
    total_bytes: int                  # size of the .archivesys file itself
    scans_included: bool
    missing: List[Dict[str, Any]] = field(default_factory=list)   # scans not found
    changed: List[Dict[str, Any]] = field(default_factory=list)   # bytes differ from the fingerprint on record
    seconds: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["path"] = str(self.path)
        return out


@dataclass
class RestoreReport:
    name: str
    saved_at: Optional[str]
    documents: int
    people: int
    relinked: int                     # scans already on this computer, linked where they are
    unpacked: int                     # scans written into the library
    library: str
    backup: Optional[str]             # a saved archive of what was open before
    missing: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================================
# Saving
# =============================================================================
def default_bundle_name(now: Optional[datetime] = None) -> str:
    return f"Family Archive {(now or datetime.now()):%Y-%m-%d %H%M}"


def save_bundle(
    db: DatabaseManager,
    dest: str | Path,
    *,
    name: Optional[str] = None,
    session: Optional[Dict[str, Any]] = None,
    include_scans: bool = True,
    progress: Optional[Progress] = None,
) -> SaveReport:
    """Save the whole archive into one .archivesys file.

    `dest` is a directory (a dated file name is chosen) or a file path. The
    live archive is only read: the database is snapshotted and each master is
    streamed once, fingerprinted as it goes into the file.

    `include_scans=False` records every scan's location and fingerprint but not
    its bytes. Such a file is tiny and opens by linking the scans back where
    they are -- which is what the automatic backup before an open uses.
    """
    started = time.monotonic()
    progress = progress or _no_progress
    name = _one_line(name) or default_bundle_name()
    dest = _bundle_destination(Path(dest).expanduser(), name)

    if session is None:
        session = load_session(session_path(db.db_path))
    settings = asdict(config.load_settings())

    partial = dest.with_name(f".{dest.name}.partial")
    with tempfile.TemporaryDirectory(prefix="archivesys-save-") as tmp:
        snap = db.snapshot_to(Path(tmp) / "archive.db")
        try:
            report = _write_bundle(
                snap, partial, name=name, bag_name=dest.stem, session=session,
                settings=settings, include_scans=include_scans, progress=progress,
            )
            os.replace(partial, dest)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    report.path = dest
    report.total_bytes = dest.stat().st_size
    report.seconds = round(time.monotonic() - started, 2)
    return report


def _write_bundle(
    snap: Path, out: Path, *, name: str, bag_name: str, session: Dict[str, Any],
    settings: Dict[str, Any], include_scans: bool, progress: Progress,
) -> SaveReport:
    root = f"{_safe_filename(bag_name)}/"
    manifest: Dict[str, str] = {}          # bag-relative payload path -> sha256
    payload_bytes = 0
    files: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    changed: List[Dict[str, Any]] = []
    used_names: set[str] = set()

    conn = sqlite3.connect(str(snap))
    conn.row_factory = sqlite3.Row
    try:
        docs = conn.execute(
            "SELECT id, file_path, sha256, title FROM documents ORDER BY id"
        ).fetchall()
        counts = {
            "documents": len(docs),
            "people": conn.execute("SELECT COUNT(*) FROM people").fetchone()[0],
            "clusters": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }

        with zipfile.ZipFile(out, "w", allowZip64=True) as zf:
            for i, doc in enumerate(docs):
                progress("Packing scans" if include_scans else "Fingerprinting scans", i, len(docs))
                original = _absolute(doc["file_path"])
                if not original.is_file():
                    missing.append({"doc_id": doc["id"], "title": doc["title"], "path": doc["file_path"]})
                    continue

                st = original.stat()
                member = None
                if include_scans:
                    member = "data/" + _unique_name(_files_arcname(original), used_names)
                    sha, size = _write_file(zf, original, root + member, st)
                    manifest[member] = sha
                    payload_bytes += size
                else:
                    sha, size = _sha256_file(original)

                if doc["sha256"] and doc["sha256"] != sha:
                    changed.append({"doc_id": doc["id"], "title": doc["title"], "path": str(original)})
                # The saved copy must agree with itself: record what was
                # actually fingerprinted, even where the live row is stale.
                conn.execute(
                    "UPDATE documents SET sha256 = ?, file_size = ? WHERE id = ?",
                    (sha, size, doc["id"]),
                )
                files.append({
                    "doc_id": doc["id"],
                    "path": member,
                    "original_path": str(original),
                    "sha256": sha,
                    "size": size,
                    "mtime": st.st_mtime,
                })
            progress("Packing records", len(docs), len(docs))

            conn.commit()
            resume = _resume_summary(conn, session)
            conn.close()

            sha, size = _write_file(zf, snap, root + "data/archive.db", snap.stat())
            manifest["data/archive.db"] = sha
            payload_bytes += size

            info = {
                "format": FORMAT,
                "format_version": FORMAT_VERSION,
                "generator": f"archivesys {app_version()}",
                "name": name,
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "scans_included": include_scans,
                "counts": {**counts, "files": len(files), "missing": len(missing)},
                "files": files,
                "missing": missing,
                "settings": settings,
                "session": session,
                "resume": resume,
            }
            info_bytes = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
            manifest["data/archivesys.json"] = _write_bytes(zf, root + "data/archivesys.json", info_bytes)
            payload_bytes += len(info_bytes)

            scans_note = f"{len(files)} scans" if include_scans else "no scans (locations and fingerprints only)"
            tags = {
                "bagit.txt": "BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n",
                "bag-info.txt": (
                    f"Bag-Software-Agent: archivesys {app_version()}\n"
                    f"Bagging-Date: {date.today().isoformat()}\n"
                    f"Payload-Oxum: {payload_bytes}.{len(manifest)}\n"
                    f"External-Description: {name}: {counts['documents']} documents, "
                    f"{counts['people']} people, {scans_note}. "
                    f"Open with ArchiveSys: archivesys open <this file>\n"
                ),
                "manifest-sha256.txt": "".join(
                    f"{digest}  {_bagit_encode(path)}\n" for path, digest in sorted(manifest.items())
                ),
            }
            tag_digests = {n: _write_bytes(zf, root + n, text.encode("utf-8")) for n, text in tags.items()}
            _write_bytes(zf, root + "tagmanifest-sha256.txt", "".join(
                f"{digest}  {n}\n" for n, digest in sorted(tag_digests.items())
            ).encode("utf-8"))
    finally:
        conn.close()

    return SaveReport(
        path=out, name=name, documents=counts["documents"], people=counts["people"],
        files=len(files) if include_scans else 0, total_bytes=0, scans_included=include_scans,
        missing=missing, changed=changed,
    )


def _resume_summary(conn: sqlite3.Connection, session: Dict[str, Any]) -> Dict[str, Any]:
    """What 'where you were' means in words, for the dialog shown before opening."""
    view = session.get("view")
    out: Dict[str, Any] = {"view": view if isinstance(view, str) else None}
    doc_id = session.get("doc_id")
    if type(doc_id) is int:
        row = conn.execute("SELECT id, title, file_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row:
            out["document"] = {"id": row["id"], "title": row["title"] or Path(row["file_path"]).name}
    person_id = session.get("person_id")
    if type(person_id) is int and view == "person":
        row = conn.execute("SELECT id, first_name, last_name FROM people WHERE id = ?", (person_id,)).fetchone()
        if row:
            out["person"] = {"id": row["id"], "name": f"{row['first_name']} {row['last_name']}"}
    return out


def _write_file(zf: zipfile.ZipFile, src: Path, arcname: str, st: os.stat_result) -> Tuple[str, int]:
    """Stream one file into the ZIP, fingerprinting it in the same pass."""
    info = zipfile.ZipInfo(arcname, date_time=_zip_time(st.st_mtime))
    info.compress_type = (zipfile.ZIP_STORED if src.suffix.lower() in _STORED_SUFFIXES
                          else zipfile.ZIP_DEFLATED)
    info.external_attr = 0o644 << 16
    info.file_size = st.st_size        # lets zipfile choose ZIP64 up front for very large scans
    digest, size = hashlib.sha256(), 0
    with src.open("rb") as fh, zf.open(info, "w") as out:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
            out.write(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _write_bytes(zf: zipfile.ZipFile, arcname: str, data: bytes) -> str:
    info = zipfile.ZipInfo(arcname, date_time=_zip_time(time.time()))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)
    return hashlib.sha256(data).hexdigest()


def _zip_time(timestamp: float) -> Tuple[int, int, int, int, int, int]:
    """ZIP stores local time and cannot represent dates before 1980 or after 2107."""
    try:
        local = tuple(time.localtime(timestamp)[:6])
    except (OverflowError, OSError, ValueError):
        local = (1980, 1, 1, 0, 0, 0)
    return min(max(local, (1980, 1, 1, 0, 0, 0)), (2107, 12, 31, 23, 59, 58))


def _files_arcname(path: Path) -> str:
    """files/<the folder it was in>/<its name>.

    Keeps the one piece of provenance that usually matters -- a folder called
    "yellow_folder_dad" -- without writing someone's home directory into it.
    """
    parent = _clean_component(path.parent.name) or "scans"
    return f"files/{parent}/{_clean_component(path.name) or 'scan'}"


def _unique_name(arcname: str, used: set[str]) -> str:
    """Case-insensitively unique, since the file may be opened on macOS or Windows."""
    stem, dot, suffix = arcname.rpartition(".")
    if not dot or "/" in suffix:
        stem, suffix = arcname, ""
    candidate, n = arcname, 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({n}).{suffix}" if suffix else f"{stem} ({n})"
        n += 1
    used.add(candidate.casefold())
    return candidate


def _bundle_destination(dest: Path, name: str) -> Path:
    """A folder gets a dated, never-clobbering name; a file path is taken as given."""
    if dest.is_dir():
        base = _safe_filename(name)
        dest, n = dest / f"{base}{BUNDLE_SUFFIX}", 2
        while dest.exists():
            dest, n = dest.with_name(f"{base} ({n}){BUNDLE_SUFFIX}"), n + 1
    elif dest.suffix.lower() != BUNDLE_SUFFIX:
        dest = dest.with_name(dest.name + BUNDLE_SUFFIX)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


# =============================================================================
# Reading
# =============================================================================
@dataclass
class _Bag:
    zf: zipfile.ZipFile
    root: str                      # "" or "<bag name>/"
    info: Dict[str, Any]
    manifest: Dict[str, str]       # bag-relative path -> sha256


def _open_bag(path: str | Path) -> _Bag:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BundleError(f"This is not a saved ArchiveSys archive ({exc}).") from exc
    try:
        root = _find_root(zf.namelist())
        try:
            raw_info = zf.read(root + "data/archivesys.json")
            info = json.loads(raw_info.decode("utf-8"))
            manifest = _parse_manifest(zf.read(root + "manifest-sha256.txt").decode("utf-8"))
        except KeyError as exc:
            raise BundleError(f"This archive is incomplete: {exc.args[0]} is missing.") from exc
        except (ValueError, zipfile.BadZipFile) as exc:
            raise BundleError(f"This archive is damaged and cannot be read ({exc}).") from exc
        _check_format(info)
        # The summary is trusted for everything that follows, so it is checked
        # like any other payload file: the manifest anchors the whole chain.
        if manifest.get("data/archivesys.json") != hashlib.sha256(raw_info).hexdigest():
            raise BundleError("This archive is damaged: its summary does not match its fingerprint.")
        return _Bag(zf, root, info, manifest)
    except BaseException:
        zf.close()
        raise


def _find_root(names: List[str]) -> str:
    roots = {n[: -len("bagit.txt")] for n in names
             if PurePosixPath(n).name == "bagit.txt" and n.count("/") <= 1}
    if len(roots) != 1:
        raise BundleError("This file does not contain a saved ArchiveSys archive.")
    return roots.pop()


def _check_format(info: Any) -> None:
    if not isinstance(info, dict) or info.get("format") != FORMAT:
        raise BundleError("This file is a BagIt bag, but not one saved by ArchiveSys.")
    version = info.get("format_version")
    if type(version) is not int or version < 1:
        raise BundleError("This archive does not say which format version it uses.")
    if version > FORMAT_VERSION:
        raise BundleError(
            f"This archive was saved by a newer version of ArchiveSys (format {version}); "
            f"update ArchiveSys to open it."
        )


def _parse_manifest(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not _SHA256.match(parts[0].lower()):
            raise ValueError(f"unreadable manifest line: {line[:80]!r}")
        out[_bagit_decode(parts[1])] = parts[0].lower()
    return out


def inspect_bundle(path: str | Path) -> Dict[str, Any]:
    """What a saved archive holds, read from its summary without unpacking it."""
    bag = _open_bag(path)
    with bag.zf:
        info = bag.info
        files = info.get("files") if isinstance(info.get("files"), list) else []
        return {
            "name": info.get("name"),
            "saved_at": info.get("saved_at"),
            "generator": info.get("generator"),
            "format_version": info.get("format_version"),
            "scans_included": info.get("scans_included", True),
            "counts": info.get("counts") if isinstance(info.get("counts"), dict) else {},
            "scan_bytes": sum(int(f.get("size") or 0) for f in files
                              if isinstance(f, dict) and f.get("path")),
            "resume": info.get("resume") if isinstance(info.get("resume"), dict) else {},
        }


def verify_bundle(path: str | Path, progress: Optional[Progress] = None) -> Dict[str, Any]:
    """Check every file in a saved archive against its fingerprint.

    The same check BagIt tools perform. It reads the whole file, so it proves
    the archive is intact -- on a backup drive years later, say -- without
    opening it or touching the current archive.
    """
    progress = progress or _no_progress
    bag = _open_bag(path)
    problems: List[str] = []
    total = 0
    with bag.zf as zf:
        names = set(zf.namelist())
        payload = {n[len(bag.root):] for n in names
                   if n.startswith(bag.root + "data/") and not n.endswith("/")}
        for rel in sorted(payload - set(bag.manifest)):
            problems.append(f"{rel}: not listed in the manifest")

        items = sorted(bag.manifest.items())
        for i, (rel, expected) in enumerate(items):
            progress("Verifying", i, len(items))
            if bag.root + rel not in names:
                problems.append(f"{rel}: listed in the manifest but not in the file")
                continue
            try:
                actual, size = _hash_member(zf, bag.root + rel)
            except (zipfile.BadZipFile, OSError, EOFError) as exc:
                problems.append(f"{rel}: unreadable ({exc})")
                continue
            total += size
            if actual != expected:
                problems.append(f"{rel}: does not match its fingerprint -- changed or damaged")
        progress("Verifying", len(items), len(items))

        tag_manifest = bag.root + "tagmanifest-sha256.txt"
        if tag_manifest in names:
            try:
                for rel, expected in _parse_manifest(zf.read(tag_manifest).decode("utf-8")).items():
                    if bag.root + rel not in names:
                        problems.append(f"{rel}: listed in the tag manifest but not in the file")
                    elif hashlib.sha256(zf.read(bag.root + rel)).hexdigest() != expected:
                        problems.append(f"{rel}: does not match its fingerprint -- changed or damaged")
            except (ValueError, zipfile.BadZipFile) as exc:
                problems.append(f"tagmanifest-sha256.txt: unreadable ({exc})")

        if not problems:
            oxum = _bag_info_field(zf, bag.root, "Payload-Oxum")
            if oxum and oxum != f"{total}.{len(items)}":
                problems.append(f"bag-info.txt: Payload-Oxum says {oxum}, found {total}.{len(items)}")

    return {
        "ok": not problems,
        "name": bag.info.get("name"),
        "saved_at": bag.info.get("saved_at"),
        "files": len(items),
        "bytes": total,
        "problems": problems,
    }


def _bag_info_field(zf: zipfile.ZipFile, root: str, key: str) -> Optional[str]:
    try:
        text = zf.read(root + "bag-info.txt").decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip().lower() == key.lower():
            return value.strip()
    return None


def _hash_member(zf: zipfile.ZipFile, member: str) -> Tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with zf.open(member) as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# =============================================================================
# Opening
# =============================================================================
def restore_bundle(
    path: str | Path,
    db: DatabaseManager,
    *,
    progress: Optional[Progress] = None,
) -> RestoreReport:
    """Open a saved archive in place of the current one.

    Stage, verify, then commit. The workspace is not touched until every file
    has matched its fingerprint and the records have been migrated to this
    version, so a damaged or foreign file changes nothing. The archive being
    replaced is saved to data/backups before the swap, and no file on disk is
    deleted or overwritten at any point.
    """
    started = time.monotonic()
    progress = progress or _no_progress
    workspace = db.db_path.parent.resolve()
    library = workspace / "library"
    _sweep_stale_staging(workspace)

    bag = _open_bag(path)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=workspace))
    try:
        with bag.zf as zf:
            info = bag.info
            names = set(zf.namelist())

            # --- 1. the records --------------------------------------------
            progress("Checking the records", 0, 0)
            staged_db = staging / "archive.db"
            _extract_verified(zf, bag, "data/archive.db", staged_db)
            _prepare_database(staged_db)
            recorded = _read_fingerprints(staged_db)
            staged = DatabaseManager(staged_db)
            staged.init_db()                 # bring an older archive forward
            # init_db fills in missing fingerprints by hashing whatever file is
            # at each recorded path -- on *this* computer. Verifying against
            # those would bless a stranger's file, so the archive's own are put back.
            _write_fingerprints(staged_db, recorded)

            conn = staged.get_connection()
            try:
                rows = {r["id"]: dict(r) for r in conn.execute(
                    "SELECT id, file_path, sha256, file_size, title FROM documents")}
                people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
            finally:
                conn.close()

            # --- 2. decide where every scan will live ----------------------
            entries = _file_entries(info, bag, names)
            relink: Dict[int, Path] = {}
            unpack: List[Tuple[int, Dict[str, Any]]] = []
            unfound: List[Tuple[int, str]] = []          # (doc id, why)

            for i, (doc_id, row) in enumerate(sorted(rows.items())):
                progress("Looking for scans already on this computer", i, len(rows))
                entry = entries.get(doc_id)
                original = Path(entry["original_path"]) if entry else _absolute(row["file_path"])
                expected = entry["sha256"] if entry else row["sha256"]
                size = entry["size"] if entry else row["file_size"]
                if expected and _matches(original, expected, size):
                    relink[doc_id] = original
                elif entry and entry["path"]:
                    unpack.append((doc_id, entry))
                elif not original.exists():
                    unfound.append((doc_id, "not found"))
                else:
                    # Something else now occupies the recorded path. Binding the
                    # record to bytes that do not match its fingerprint would
                    # attach this document's metadata to a different file.
                    unfound.append((doc_id, "a different file is at its old location"))

            # Paths other documents hold (or will hold), so nothing lands on one.
            taken = {_path_key(_absolute(r["file_path"])) for r in rows.values()}
            taken |= {_path_key(p) for p in relink.values()}

            needed = sum(e["size"] for _, e in unpack) + staged_db.stat().st_size
            free = shutil.disk_usage(workspace).free
            if needed + _DISK_MARGIN > free:
                raise BundleError(
                    f"Not enough disk space: opening this archive needs {_human_bytes(needed)} "
                    f"and {_human_bytes(free)} is free."
                )

            # --- 3. unpack into staging, verifying every byte ---------------
            placements: List[Tuple[int, Path, Optional[Path], Optional[float]]] = []
            for i, (doc_id, entry) in enumerate(unpack):
                progress("Unpacking scans", i, len(unpack))
                own = _path_key(_absolute(rows[doc_id]["file_path"]))
                dest, identical = _destination(library, entry["path"], entry["sha256"], taken, own)
                taken.add(_path_key(dest))
                staged_file = None
                if not identical:
                    staged_file = staging / "files" / str(doc_id)
                    _extract_verified(zf, bag, entry["path"], staged_file)
                placements.append((doc_id, dest, staged_file, entry.get("mtime")))
            progress("Unpacking scans", len(unpack), len(unpack))

            # A document detached from a mismatched file is pointed where its
            # scan would be unpacked -- clearly missing, and the natural place
            # to put the scan back if it turns up.
            detached: Dict[int, Path] = {}
            for doc_id, why in unfound:
                if why != "not found":
                    name = _absolute(rows[doc_id]["file_path"]).name or f"document-{doc_id}"
                    dest, _ = _destination(library, f"data/files/missing/{_clean_component(name)}",
                                           None, taken, None)
                    taken.add(_path_key(dest))
                    detached[doc_id] = dest

            # --- 4. point the staged records at the final locations --------
            for doc_id, original in relink.items():
                if _absolute(rows[doc_id]["file_path"]) != original:
                    staged.relocate_document(doc_id, original)
            for doc_id, dest, _, _ in placements:
                staged.relocate_document(doc_id, dest)
            for doc_id, dest in detached.items():
                staged.relocate_document(doc_id, dest)

        # --- 5. commit -----------------------------------------------------
        progress("Saving a backup of the current archive", 0, 0)
        backup = _backup_current(db, workspace, info.get("name"))

        progress("Opening", 0, 0)
        for doc_id, dest, staged_file, mtime in placements:
            if staged_file is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_file, dest)
            if isinstance(mtime, (int, float)):
                try:
                    os.utime(dest, (mtime, mtime))
                except OSError:
                    pass

        db.replace_with(staged_db)
        db.init_db()

        warnings: List[str] = []
        settings = info.get("settings")
        if isinstance(settings, dict):
            try:
                config.reset_settings()
                config.save_settings(settings)
            except (OSError, TypeError, ValueError) as exc:
                warnings.append(f"The transcription settings could not be restored: {exc}")
        try:
            session = info.get("session") if isinstance(info.get("session"), dict) else {}
            save_session(session_path(db.db_path), session)
        except (OSError, SessionError) as exc:
            warnings.append(f"Where you were could not be restored: {exc}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return RestoreReport(
        name=str(info.get("name") or Path(path).stem),
        saved_at=info.get("saved_at"),
        documents=len(rows),
        people=people,
        relinked=len(relink),
        unpacked=len(placements),
        library=str(library),
        backup=str(backup) if backup else None,
        missing=[{"doc_id": d, "title": rows[d]["title"], "path": rows[d]["file_path"], "reason": why}
                 for d, why in unfound],
        warnings=warnings,
        seconds=round(time.monotonic() - started, 2),
    )


def _file_entries(info: Dict[str, Any], bag: _Bag, names: set[str]) -> Dict[int, Dict[str, Any]]:
    """The file table, checked against the manifest before anything trusts it."""
    raw_entries = info.get("files")
    if not isinstance(raw_entries, list):
        raise BundleError("This archive's file table is missing or damaged.")
    out: Dict[int, Dict[str, Any]] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise BundleError("This archive's file table is damaged.")
        doc_id, sha = raw.get("doc_id"), raw.get("sha256")
        member, original, size = raw.get("path"), raw.get("original_path"), raw.get("size")
        if (type(doc_id) is not int or not isinstance(sha, str) or not _SHA256.match(sha)
                or not isinstance(original, str) or type(size) is not int or size < 0):
            raise BundleError("This archive's file table is damaged.")
        if member is not None:
            _check_member_path(member)
            if bag.manifest.get(member) != sha:
                raise BundleError(f"{member}: the file table and the manifest disagree.")
            if bag.root + member not in names:
                raise BundleError(f"{member} is listed but is not in the file.")
        out[doc_id] = {"path": member, "original_path": original, "sha256": sha,
                       "size": size, "mtime": raw.get("mtime")}
    return out


def _check_member_path(member: Any) -> None:
    """Refuse anything that could write outside the library (zip-slip)."""
    ok = isinstance(member, str)
    if ok:
        p = PurePosixPath(member)
        ok = (not p.is_absolute() and len(p.parts) >= 3 and p.parts[:2] == ("data", "files")
              and all(part not in ("", ".", "..") and "\\" not in part and "\x00" not in part
                      for part in p.parts))
    if not ok:
        raise BundleError(f"This archive contains an unsafe file path: {member!r}")


def _extract_verified(zf: zipfile.ZipFile, bag: _Bag, rel: str, dest: Path) -> None:
    expected = bag.manifest.get(rel)
    if expected is None:
        raise BundleError(f"{rel} is not listed in the archive's manifest.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with zf.open(bag.root + rel) as src, dest.open("wb") as out:
            while chunk := src.read(_CHUNK):
                digest.update(chunk)
                out.write(chunk)
    except KeyError as exc:
        raise BundleError(f"This archive is incomplete: {rel} is missing.") from exc
    except (zipfile.BadZipFile, EOFError) as exc:
        raise BundleError(f"{rel} is damaged inside the archive ({exc}).") from exc
    if digest.hexdigest() != expected:
        raise BundleError(f"{rel} does not match its fingerprint -- the archive is damaged.")


def _prepare_database(path: Path) -> None:
    """Check the staged records, and keep their data but not their behaviour.

    Triggers and views are dropped and rebuilt from this version's schema by
    init_db, rather than trusted from a file that may have come from anywhere.
    """
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error as exc:
        raise BundleError(f"The records in this archive cannot be read ({exc}).") from exc
    try:
        conn.execute("PRAGMA trusted_schema = OFF")
        status = conn.execute("PRAGMA quick_check").fetchone()[0]
        if status != "ok":
            raise BundleError(f"The records in this archive are damaged ({status}).")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"documents", "people"} <= tables:
            raise BundleError("This archive holds no document records.")
        for kind, name in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchall():
            quoted = '"' + name.replace('"', '""') + '"'
            conn.execute(f"DROP {kind.upper()} IF EXISTS {quoted}")
        conn.commit()
    except sqlite3.DatabaseError as exc:
        raise BundleError(f"The records in this archive cannot be read ({exc}).") from exc
    finally:
        conn.close()


def _read_fingerprints(path: Path) -> Dict[int, Tuple[Optional[str], Optional[int]]]:
    conn = sqlite3.connect(str(path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        if not {"sha256", "file_size"} <= cols:
            return {}                      # saved before fingerprints existed
        return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, sha256, file_size FROM documents")}
    finally:
        conn.close()


def _write_fingerprints(path: Path, recorded: Dict[int, Tuple[Optional[str], Optional[int]]]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executemany("UPDATE documents SET sha256 = ?, file_size = ? WHERE id = ?",
                         [(sha, size, doc_id) for doc_id, (sha, size) in recorded.items()])
        conn.commit()
    finally:
        conn.close()


def _destination(library: Path, member: str, sha256: Optional[str],
                 taken: set[str], own: Optional[str]) -> Tuple[Path, bool]:
    """Where a scan goes in the library, and whether an identical copy is already there.

    Never overwrites: an existing file with the same fingerprint is reused (the
    same archive opened twice), anything else gets a numbered name.
    """
    rel = PurePosixPath(member).relative_to("data/files")
    candidate = library.joinpath(*(_portable_component(p) for p in rel.parts))
    if not candidate.resolve().is_relative_to(library.resolve()):
        raise BundleError(f"This archive contains an unsafe file path: {member!r}")
    stem, suffix, n = candidate.stem, candidate.suffix, 2
    while True:
        key = _path_key(candidate)
        if key not in taken or key == own:
            if not candidate.exists():
                return candidate, False
            if sha256 and candidate.is_file() and _sha256_file(candidate)[0] == sha256:
                return candidate, True
        candidate = candidate.with_name(f"{stem} ({n}){suffix}")
        n += 1


def _backup_current(db: DatabaseManager, workspace: Path, opening: Any) -> Optional[Path]:
    """Save what is open now as a small .archivesys, so opening it goes back.

    Scans are not copied: opening never deletes one, so a backup that records
    where each scan is and its fingerprint is enough to restore exactly.
    """
    counts = db.archive_counts()
    if not any(counts.values()):
        return None                        # nothing to lose
    stamp = f"{datetime.now():%Y-%m-%d %H%M%S}"
    dest = workspace / "backups" / f"Before opening {stamp}{BUNDLE_SUFFIX}"
    label = f"“{_one_line(opening)}”" if opening else "an archive"
    save_bundle(db, dest, name=f"Archive before opening {label}", include_scans=False)
    return dest


def _sweep_stale_staging(workspace: Path) -> None:
    """Remove staging left behind by an open that was killed part way."""
    cutoff = time.time() - 6 * 3600
    for stale in workspace.glob(".restore-*"):
        try:
            if stale.is_dir() and stale.stat().st_mtime < cutoff:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            pass


# =============================================================================
# Small helpers
# =============================================================================
def _absolute(raw: Any) -> Path:
    """Resolve a stored path the way the rest of the archive opens it."""
    p = Path(str(raw or "")).expanduser()
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def _path_key(p: Path) -> str:
    return str(p).casefold()


def _matches(path: Path, sha256: str, size: Optional[int]) -> bool:
    try:
        if not path.is_file():
            return False
        if isinstance(size, int) and path.stat().st_size != size:
            return False
        return _sha256_file(path)[0] == sha256
    except OSError:
        return False


def _sha256_file(path: Path) -> Tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _clean_component(part: str) -> str:
    """A single path component with separators and control characters removed."""
    part = "".join("_" if c in "/\\" or ord(c) < 32 else c for c in part).strip()
    return "" if part in (".", "..") else part


def _portable_component(part: str) -> str:
    """A component that is also a legal file name where the archive is opened."""
    part = _clean_component(part)
    if os.name == "nt":
        part = "".join("_" if c in '<>:"|?*' else c for c in part).rstrip(" .")
    return part or "_"


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", name).strip(" .-")
    return cleaned[:120] or "Family Archive"


def _one_line(text: Any) -> str:
    return " ".join(str(text).split()) if text else ""


def _bagit_encode(path: str) -> str:
    """BagIt manifests percent-encode the three characters that would break a line."""
    return path.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _bagit_decode(path: str) -> str:
    return re.sub(r"%(25|0[aAdD])",
                  lambda m: {"25": "%", "0a": "\n", "0d": "\r"}[m.group(1).lower()], path)


def _human_bytes(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"
