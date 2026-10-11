"""SQLite database manager and repository layer for historical family archive.

One document is one timeline entry. The fields the curator edits ARE the fields
the timeline projects -- there is no parallel "event" record shadowing them.
Clusters (the `events` table) exist only for the two things a document cannot
express on its own: an occurrence evidenced by several documents at once, and
one evidenced by none at all.
"""

from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from src.ingestion.polytonic import PolytonicNormalizer
from src.database.edtf_parser import EDTFParser, NotADate

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# -----------------------------------------------------------------------------
# The single taxonomy axis.
#
# A kind earns its place only if it changes how the archive BEHAVES: whether the
# engine tries to read text off the page, and how the item reads on the timeline.
# Everything else a curator might want to say about a document -- military,
# migration, marriage, wartime -- is a subject, and subjects are tags.
# -----------------------------------------------------------------------------
DOCUMENT_KINDS: List[Dict[str, Any]] = [
    {"key": "certificate", "label": "Certificate", "hint": "Birth, marriage, death, baptism", "textual": True},
    {"key": "letter", "label": "Letter", "hint": "Correspondence, postcards, telegrams", "textual": True},
    {"key": "photograph", "label": "Photograph", "hint": "Portraits and snapshots", "textual": False},
    {"key": "passport", "label": "Passport & ID", "hint": "Travel and identity papers", "textual": True},
    {"key": "press", "label": "Press", "hint": "Newspaper and magazine clippings", "textual": True},
    {"key": "book", "label": "Book", "hint": "Published volumes and pamphlets", "textual": True},
    {"key": "document", "label": "Document", "hint": "Deeds, manifests, records, anything else", "textual": True},
]

KIND_KEYS = [k["key"] for k in DOCUMENT_KINDS]

# Vocabularies this replaces. The left side is everything the old UI, the old
# schema comments and the old event.category select could produce.
LEGACY_KIND_MAP = {
    "magazine": "press",
    "newspaper": "press",
    "periodical": "press",
    "manifest": "document",
    "portrait": "photograph",
    "photo": "photograph",
    "map": "document",
    "artifact": "document",
    "life_event": "document",
    "military": "document",
    "migration": "document",
    "official": "document",
    "correspondence": "letter",
}

# The old event.category values carried real meaning that should not be thrown
# away on migration -- they become tags, which is what they always were.
LEGACY_CATEGORY_TAGS = {
    "life_event": "life event",
    "military": "military",
    "migration": "migration",
    "official": "official",
    "correspondence": "correspondence",
}

_NON_TEXTUAL = {k["key"] for k in DOCUMENT_KINDS if not k["textual"]}


def normalize_kind(value: Optional[str]) -> str:
    """Fold any legacy or free-text type onto the seven-value vocabulary."""
    if not value:
        return "document"
    key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if key in KIND_KEYS:
        return key
    if key in LEGACY_KIND_MAP:
        return LEGACY_KIND_MAP[key]
    return "document"


def is_textual_kind(kind: Optional[str]) -> bool:
    """Whether the OCR engine should attempt a transcription for this kind.

    Single source of truth, shared by the server and (via /api/kinds) the studio.
    The old build had this list hardcoded in JavaScript with values that did not
    exist in the category dropdown, so the heuristic never matched reality.
    """
    return normalize_kind(kind) not in _NON_TEXTUAL


# The roles a person can play ON a document. Deliberately closed, and
# deliberately NOT the place for relationships between people -- "father" says
# this person is a father, but not whose, and is only recoverable by guessing
# from the other links on the same document. Relationships get their own table
# when they are needed; until then legacy values are kept and shown, not lost.
DOCUMENT_ROLES = ["subject", "author", "recipient", "witness", "photographer"]


def person_name_key(first_name: str, last_name: str) -> str:
    """Fold a name the way the search index folds text.

    Strips diacritics and folds final sigma, so Γεώργιος Θεοδώρου and
    Γεωργιος Θεοδωρου collapse to one key. Latin transliterations do not fold
    this way and are caught, if at all, by matching lifespans instead.
    """
    combined = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
    exact = PolytonicNormalizer.normalize_exact(combined)
    return PolytonicNormalizer.normalize_search(exact)


def compute_sha256_and_size(file_path: str | Path) -> Tuple[Optional[str], Optional[int]]:
    """Compute cryptographic SHA-256 digest and exact byte size of a file."""
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return None, None
    h = hashlib.sha256()
    size = 0
    with p.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _json_list(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return [t.strip() for t in raw.split(",") if t.strip()]
        if isinstance(parsed, list):
            return [str(t).strip() for t in parsed if str(t).strip()]
    return []


class DatabaseManager:
    """Manages SQLite connection, schema migrations, and queries."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        """Create a connection with foreign keys and row factory enabled."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    # -------------------------------------------------------------------------
    # Schema & migration
    # -------------------------------------------------------------------------
    def init_db(self) -> None:
        """Initialize schema, triggers, and apply automatic forward migrations."""
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.get_connection() as conn:
            conn.executescript(schema_sql)
            cur = conn.cursor()

            cur.execute("PRAGMA table_info(documents);")
            columns = {row["name"] for row in cur.fetchall()}

            # --- v1 -> v2: one taxonomy axis -------------------------------
            if "kind" not in columns and "file_type" in columns:
                conn.execute("ALTER TABLE documents RENAME COLUMN file_type TO kind;")
                columns.discard("file_type")
                columns.add("kind")

            # --- v1 -> v2: description fields become real columns ----------
            promoted = {
                "title": "TEXT",
                "edtf_date": "TEXT",
                "sort_start": "REAL",
                "sort_end": "REAL",
                "date_display": "TEXT",
                "location": "TEXT",
                "summary": "TEXT",
                "creator": "TEXT",
                "notes": "TEXT",
                "tags": "TEXT",
                "reviewed_at": "TIMESTAMP",
                "reviewed_by": "TEXT",
                "sha256": "TEXT",
                "file_size": "INTEGER",
            }
            for name, decl in promoted.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {decl};")
            if "excluded" not in columns:
                conn.execute("ALTER TABLE documents ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0;")

            # --- v3 -> v4: an item can have several pages ------------------
            if "parent_id" not in columns:
                conn.execute("ALTER TABLE documents ADD COLUMN parent_id INTEGER "
                             "REFERENCES documents(id) ON DELETE CASCADE;")
            if "page_no" not in columns:
                conn.execute("ALTER TABLE documents ADD COLUMN page_no INTEGER;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(parent_id);")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON documents(sha256);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_sort_start ON documents(sort_start);")

            # --- v2 -> v3: people become a first-class axis ----------------
            cur.execute("PRAGMA table_info(people);")
            pcols = {row["name"] for row in cur.fetchall()}
            for name, decl in {
                "birth_sort": "REAL",
                "death_sort": "REAL",
                "name_key": "TEXT",
                "portrait_document_id": "INTEGER REFERENCES documents(id) ON DELETE SET NULL",
                "portrait_crop": "TEXT",
            }.items():
                if name not in pcols:
                    conn.execute(f"ALTER TABLE people ADD COLUMN {name} {decl};")
            if "portrait_mode" not in pcols:
                conn.execute("ALTER TABLE people ADD COLUMN portrait_mode TEXT NOT NULL DEFAULT 'auto';")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_people_name_key ON people(name_key);")

            if any(c not in pcols for c in ("name_key", "birth_sort")):
                self._migrate_people(conn)

            had_status = "verification_status" in columns
            needs_lift = any(c not in columns for c in ("title", "edtf_date", "reviewed_at"))
            if needs_lift or had_status:
                self._migrate_v1_rows(conn, had_status=had_status)

            # Backfill fingerprints for anything indexed before hashing existed.
            cur.execute("SELECT id, file_path FROM documents WHERE sha256 IS NULL OR sha256 = ''")
            for r in cur.fetchall():
                sha, sz = compute_sha256_and_size(r["file_path"])
                if sha:
                    conn.execute(
                        "UPDATE documents SET sha256 = ?, file_size = ? WHERE id = ?",
                        (sha, sz, r["id"]),
                    )

    def _migrate_v1_rows(self, conn: sqlite3.Connection, had_status: bool) -> None:
        """Lift v1 rows into the collapsed model.

        Three moves, all idempotent:
          1. metadata_json's curator-authored fields become columns.
          2. verification_status collapses to reviewed_at + excluded.
          3. Single-source events are absorbed into their one source document,
             because that is what they always were: a copy of it.
        """
        cur = conn.cursor()
        cur.execute("SELECT * FROM documents")
        rows = [dict(r) for r in cur.fetchall()]

        for row in rows:
            try:
                meta = json.loads(row.get("metadata_json") or "{}")
            except Exception:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}

            updates: Dict[str, Any] = {}
            if row.get("title") is None:
                updates["title"] = meta.get("title") or Path(row["file_path"]).stem
            if row.get("edtf_date") is None and meta.get("date_edtf"):
                updates["edtf_date"] = meta["date_edtf"]
            if row.get("location") is None and meta.get("location"):
                updates["location"] = meta["location"]
            if row.get("creator") is None and meta.get("creator"):
                updates["creator"] = meta["creator"]
            if row.get("notes") is None and meta.get("notes"):
                updates["notes"] = meta["notes"]
            if row.get("tags") is None:
                tags = _json_list(meta.get("tags"))
                if tags:
                    updates["tags"] = json.dumps(tags, ensure_ascii=False)

            updates["kind"] = normalize_kind(row.get("kind"))

            if had_status:
                status = (row.get("verification_status") or "pending").lower()
                if status == "verified" and row.get("reviewed_at") is None:
                    updates["reviewed_at"] = row.get("curated_at") or row.get("created_at")
                    updates["reviewed_by"] = row.get("curated_by") or "migration"
                if status == "rejected":
                    # A rejected scan was always meant to stay in the archive and
                    # stay off the timeline. v1 only managed the first half.
                    updates["excluded"] = 1
                    updates["reviewed_at"] = row.get("curated_at") or row.get("created_at")
                    updates["reviewed_by"] = row.get("curated_by") or "migration"

            edtf = updates.get("edtf_date", row.get("edtf_date"))
            if edtf and row.get("sort_start") is None:
                try:
                    parsed = EDTFParser.parse(edtf, strict=True)
                    updates["edtf_date"] = parsed.raw
                    updates["sort_start"] = parsed.sort_start
                    updates["sort_end"] = parsed.sort_end
                    updates["date_display"] = parsed.date_display
                except NotADate:
                    # A v1 row whose date was never readable stays undated
                    # rather than inheriting the parser's invented 1900.
                    updates["edtf_date"] = None

            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE documents SET {sets} WHERE id = ?",
                    (*updates.values(), row["id"]),
                )

        self._absorb_single_source_events(conn)

    def _absorb_single_source_events(self, conn: sqlite3.Connection) -> int:
        """Fold every one-document event back into that document, then drop it."""
        cur = conn.cursor()
        cur.execute(
            """
            SELECT event_id, COUNT(document_id) AS n, MIN(document_id) AS doc_id
            FROM event_sources GROUP BY event_id HAVING n = 1
            """
        )
        singles = [(r["event_id"], r["doc_id"]) for r in cur.fetchall()]

        absorbed = 0
        for ev_id, doc_id in singles:
            cur.execute("SELECT * FROM events WHERE id = ?", (ev_id,))
            ev = cur.fetchone()
            cur.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
            doc = cur.fetchone()
            if not ev or not doc:
                continue
            ev, doc = dict(ev), dict(doc)

            updates: Dict[str, Any] = {}
            if not doc.get("title") and ev.get("title"):
                updates["title"] = ev["title"]
            if not doc.get("edtf_date") and ev.get("edtf_date"):
                updates["edtf_date"] = ev["edtf_date"]
                updates["sort_start"] = ev.get("sort_start")
                updates["sort_end"] = ev.get("sort_end")
                updates["date_display"] = ev.get("date_display")
            if not doc.get("location") and ev.get("location"):
                updates["location"] = ev["location"]
            if not doc.get("summary") and ev.get("description"):
                updates["summary"] = ev["description"]

            # event.category was a second, disjoint taxonomy. Keep the meaning,
            # drop the axis: it becomes a tag.
            cat_tag = LEGACY_CATEGORY_TAGS.get((ev.get("category") or "").lower())
            if cat_tag:
                tags = _json_list(doc.get("tags"))
                if cat_tag not in tags:
                    tags.append(cat_tag)
                    updates["tags"] = json.dumps(tags, ensure_ascii=False)

            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*updates.values(), doc_id))

            # Participants recorded only on the event would otherwise vanish:
            # v1 never wrote them from the studio, but the CLI seed did.
            conn.execute(
                """
                INSERT OR IGNORE INTO document_people (document_id, person_id, role)
                SELECT ?, person_id, 'subject' FROM event_participants WHERE event_id = ?
                """,
                (doc_id, ev_id),
            )
            conn.execute("DELETE FROM events WHERE id = ?", (ev_id,))
            absorbed += 1

        return absorbed

    def _migrate_people(self, conn: sqlite3.Connection) -> None:
        """Backfill folded names and lifespans for people recorded before v3.

        A lifespan the strict parser cannot read is cleared rather than kept:
        v1 stored whatever was typed and let the parser answer 1900 for it,
        which is the same fabrication already removed from documents.
        """
        cur = conn.cursor()
        cur.execute("SELECT id, first_name, last_name, birth_edtf, death_edtf FROM people")
        for row in [dict(r) for r in cur.fetchall()]:
            updates: Dict[str, Any] = {
                "name_key": person_name_key(row["first_name"], row["last_name"]),
            }
            for field, sort_field in (("birth_edtf", "birth_sort"), ("death_edtf", "death_sort")):
                raw = row.get(field)
                if not raw:
                    continue
                try:
                    updates[sort_field] = EDTFParser.parse(raw, strict=True).sort_start
                except NotADate:
                    updates[field] = None
                    updates[sort_field] = None
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE people SET {sets} WHERE id = ?", (*updates.values(), row["id"]))

    # -------------------------------------------------------------------------
    # People
    # -------------------------------------------------------------------------
    @staticmethod
    def _person_dates(birth_edtf: Optional[str], death_edtf: Optional[str]) -> Dict[str, Any]:
        """Validate a lifespan as strictly as a document's date. Raises NotADate."""
        out: Dict[str, Any] = {
            "birth_edtf": None, "death_edtf": None, "birth_sort": None, "death_sort": None,
        }
        for raw, edtf_key, sort_key in (
            (birth_edtf, "birth_edtf", "birth_sort"),
            (death_edtf, "death_edtf", "death_sort"),
        ):
            if raw is None or not str(raw).strip():
                continue
            parsed = EDTFParser.parse(str(raw).strip(), strict=True)
            out[edtf_key] = parsed.raw
            out[sort_key] = parsed.sort_start

        if out["birth_sort"] is not None and out["death_sort"] is not None:
            if out["death_sort"] < out["birth_sort"]:
                raise NotADate("a death cannot precede a birth")
        return out

    @staticmethod
    def _person_name(first_name: Optional[str], last_name: Optional[str]) -> Tuple[str, str]:
        """Both names, trimmed. A blank one is refused: a nameless person cannot
        be told apart from the next one, or found again. Raises ValueError."""
        first, last = (first_name or "").strip(), (last_name or "").strip()
        if not first or not last:
            raise ValueError("A person needs both a first and a last name.")
        return first, last

    def add_person(
        self,
        first_name: str,
        last_name: str,
        maiden_name: Optional[str] = None,
        birth_edtf: Optional[str] = None,
        death_edtf: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        first, last = self._person_name(first_name, last_name)   # raises ValueError
        dates = self._person_dates(birth_edtf, death_edtf)   # raises NotADate
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO people (
                    first_name, last_name, maiden_name,
                    birth_edtf, death_edtf, birth_sort, death_sort, name_key, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (first, last, maiden_name,
                 dates["birth_edtf"], dates["death_edtf"],
                 dates["birth_sort"], dates["death_sort"],
                 person_name_key(first, last), notes),
            )
            return cur.lastrowid  # type: ignore

    def get_or_create_person(self, first_name: str, last_name: str) -> int:
        """Match on the folded name, so spelling variants do not split a person.

        Exact first+last matching made Γεώργιος Θεοδώρου and Γεωργιος Θεοδωρου
        two different men, which in a polytonic archive happens constantly.
        """
        first, last = first_name.strip(), last_name.strip()
        key = person_name_key(first, last)
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM people WHERE name_key = ? ORDER BY id LIMIT 1", (key,))
            row = cur.fetchone()
            if row:
                return row["id"]
        return self.add_person(first, last)

    # ---- reading people -----------------------------------------------
    PERSON_SORTS = {
        "name": "p.last_name COLLATE NOCASE, p.first_name COLLATE NOCASE",
        "documents": "doc_count DESC, p.last_name COLLATE NOCASE",
        "earliest": "earliest_sort IS NULL, earliest_sort ASC, p.last_name COLLATE NOCASE",
        "lifespan": "p.birth_sort IS NULL, p.birth_sort ASC, p.last_name COLLATE NOCASE",
    }

    def list_people(self, sort: str = "name") -> List[Dict[str, Any]]:
        """Every person, with the counts and portrait the People view needs."""
        order = self.PERSON_SORTS.get(sort, self.PERSON_SORTS["name"])
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT p.*,
                       COUNT(DISTINCT dp.document_id) AS doc_count,
                       COUNT(DISTINCT CASE
                             WHEN d.edtf_date IS NOT NULL AND d.excluded = 0
                             THEN d.id END) AS timeline_count,
                       MIN(CASE WHEN d.excluded = 0 THEN d.sort_start END) AS earliest_sort,
                       MAX(CASE WHEN d.excluded = 0 THEN d.sort_start END) AS latest_sort
                FROM people p
                LEFT JOIN document_people dp ON dp.person_id = p.id
                LEFT JOIN documents d ON d.id = dp.document_id
                GROUP BY p.id
                ORDER BY {order}
                """
            )
            people = [dict(r) for r in cur.fetchall()]

        for person in people:
            self._attach_portrait(person)
        return people

    def get_person(self, person_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM people WHERE id = ?", (person_id,))
            row = cur.fetchone()
            if not row:
                return None
            person = dict(row)
            cur.execute(
                """
                SELECT dp.role, COUNT(*) AS n
                FROM document_people dp WHERE dp.person_id = ?
                GROUP BY dp.role ORDER BY n DESC
                """,
                (person_id,),
            )
            person["roles"] = [dict(r) for r in cur.fetchall()]
        self._attach_portrait(person)
        return person

    def _attach_portrait(self, person: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve the three portrait states into one answer the UI can render."""
        mode = person.get("portrait_mode") or "auto"
        person["portrait_mode"] = mode
        person["portrait"] = None
        if mode == "none":
            return person
        if mode == "pinned" and person.get("portrait_document_id"):
            person["portrait"] = person["portrait_document_id"]
            return person
        person["portrait"] = self.suggest_portrait(person["id"])
        return person

    def suggest_portrait(self, person_id: int) -> Optional[int]:
        """The archive's own best guess at a picture of someone.

        A portrait beats a group shot, and an earlier picture beats a later
        one. Ties break on document id so the choice is stable as the archive
        grows rather than drifting under the curator.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT d.id,
                       (SELECT COUNT(*) FROM document_people o WHERE o.document_id = d.id) AS crowd
                FROM document_people dp
                JOIN documents d ON d.id = dp.document_id
                WHERE dp.person_id = ?
                  AND d.kind = 'photograph'
                  AND d.excluded = 0
                  AND dp.role IN ('subject', 'photographer')
                ORDER BY crowd ASC,
                         d.sort_start IS NULL, d.sort_start ASC,
                         d.id ASC
                LIMIT 1
                """,
                (person_id,),
            )
            row = cur.fetchone()
            return row["id"] if row else None

    def list_person_documents(self, person_id: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT d.*, dp.role
                FROM document_people dp JOIN documents d ON d.id = dp.document_id
                WHERE dp.person_id = ?
                ORDER BY d.sort_start IS NULL, d.sort_start ASC, d.id ASC
                """,
                (person_id,),
            )
            return [self._hydrate(dict(r)) for r in cur.fetchall()]

    # ---- writing people -------------------------------------------------
    def update_person(self, person_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        writable = {"first_name", "last_name", "maiden_name", "notes",
                    "portrait_mode", "portrait_document_id", "portrait_crop"}
        updates = {k: v for k, v in fields.items() if k in writable}

        for name in ("first_name", "last_name"):
            if name in updates:
                updates[name] = (updates[name] or "").strip()
                if not updates[name]:
                    raise ValueError("A person needs both a first and a last name.")
        for optional in ("maiden_name", "notes"):
            if optional in updates:
                updates[optional] = (updates[optional] or "").strip() or None

        if "birth_edtf" in fields or "death_edtf" in fields:
            current = self.get_person(person_id) or {}
            updates.update(self._person_dates(     # raises NotADate
                fields.get("birth_edtf", current.get("birth_edtf")),
                fields.get("death_edtf", current.get("death_edtf")),
            ))
        if "first_name" in updates or "last_name" in updates:
            current = self.get_person(person_id) or {}
            updates["name_key"] = person_name_key(
                updates.get("first_name", current.get("first_name", "")),
                updates.get("last_name", current.get("last_name", "")),
            )
        if updates.get("portrait_mode") == "pinned" and not updates.get("portrait_document_id"):
            raise ValueError("pinning a portrait needs a document")
        if updates.get("portrait_mode") in ("auto", "none"):
            updates["portrait_document_id"] = None

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM people WHERE id = ?", (person_id,))
            if not cur.fetchone():
                raise ValueError(f"Person ID {person_id} not found")
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(f"UPDATE people SET {sets} WHERE id = ?", (*updates.values(), person_id))
            if "first_name" in updates or "last_name" in updates:
                cur.execute("SELECT document_id FROM document_people WHERE person_id = ?", (person_id,))
                for r in cur.fetchall():
                    self._reindex(conn, r["document_id"])
        return self.get_person(person_id)  # type: ignore

    def set_portrait(self, person_id: int, document_id: Optional[int], mode: str = "pinned") -> Dict[str, Any]:
        """Pin a picture, return to the archive's own choice, or say there is none."""
        if mode not in ("auto", "pinned", "none"):
            raise ValueError(f"unknown portrait mode {mode!r}")
        return self.update_person(person_id, {
            "portrait_mode": mode,
            "portrait_document_id": document_id if mode == "pinned" else None,
        })

    # ---- identity hygiene ------------------------------------------------
    def find_duplicate_people(self) -> List[Dict[str, Any]]:
        """Groups of people who are probably one person.

        Folded names catch the spelling variants that matter most here. Latin
        transliterations will not fold together; those are caught, when they
        are caught at all, by an identical recorded lifespan.
        """
        people = self.list_people()
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for person in people:
            key = person.get("name_key") or person_name_key(person["first_name"], person["last_name"])
            groups.setdefault(f"name:{key}", []).append(person)

        by_life: Dict[str, List[Dict[str, Any]]] = {}
        for person in people:
            if person.get("birth_sort") is None:
                continue
            by_life.setdefault(f"life:{person['birth_sort']}:{person.get('death_sort')}", []).append(person)

        out = []
        for key, members in list(groups.items()) + list(by_life.items()):
            if len(members) < 2:
                continue
            out.append({
                "reason": "same name" if key.startswith("name:") else "same lifespan",
                "people": [
                    {"id": m["id"], "first_name": m["first_name"], "last_name": m["last_name"],
                     "doc_count": m.get("doc_count", 0)}
                    for m in sorted(members, key=lambda m: m["id"])
                ],
            })
        return out

    def merge_people(self, keep_id: int, merge_id: int) -> Dict[str, Any]:
        """Fold one person into another. Idempotent, and says so in the notes."""
        if keep_id == merge_id:
            raise ValueError("cannot merge a person into themselves")
        keep, gone = self.get_person(keep_id), self.get_person(merge_id)
        if not keep or not gone:
            raise ValueError("both people must exist")

        with self.get_connection() as conn:
            cur = conn.cursor()
            # Move the links, keeping the more specific role where both exist.
            cur.execute("SELECT document_id, role FROM document_people WHERE person_id = ?", (merge_id,))
            for row in cur.fetchall():
                doc_id, role = row["document_id"], row["role"]
                cur.execute(
                    "SELECT role FROM document_people WHERE document_id = ? AND person_id = ?",
                    (doc_id, keep_id),
                )
                existing = cur.fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO document_people (document_id, person_id, role) VALUES (?, ?, ?)",
                        (doc_id, keep_id, role),
                    )
                elif existing["role"] == "subject" and role != "subject":
                    conn.execute(
                        "UPDATE document_people SET role = ? WHERE document_id = ? AND person_id = ?",
                        (role, doc_id, keep_id),
                    )
            # Clusters too. Where both took part in one, the survivor's row
            # stands; re-pointing it (an UPDATE) would collide with that row and
            # fail the whole merge.
            conn.execute(
                """
                INSERT OR IGNORE INTO event_participants (event_id, person_id, role)
                SELECT event_id, ?, role FROM event_participants WHERE person_id = ?
                """,
                (keep_id, merge_id),
            )

            # Take whatever the survivor is missing.
            filled = {}
            for field in ("maiden_name", "birth_edtf", "death_edtf", "birth_sort", "death_sort"):
                if not keep.get(field) and gone.get(field):
                    filled[field] = gone[field]
            if gone.get("portrait_mode") == "pinned" and keep.get("portrait_mode") == "auto":
                filled["portrait_mode"] = "pinned"
                filled["portrait_document_id"] = gone.get("portrait_document_id")

            note = f"Merged in #{merge_id} {gone['first_name']} {gone['last_name']}."
            filled["notes"] = f"{keep.get('notes') or ''}\n{note}".strip()

            sets = ", ".join(f"{k} = ?" for k in filled)
            conn.execute(f"UPDATE people SET {sets} WHERE id = ?", (*filled.values(), keep_id))
            conn.execute("DELETE FROM people WHERE id = ?", (merge_id,))

            cur.execute("SELECT document_id FROM document_people WHERE person_id = ?", (keep_id,))
            for r in cur.fetchall():
                self._reindex(conn, r["document_id"])

        return self.get_person(keep_id)  # type: ignore

    def delete_person(self, person_id: int) -> Optional[Dict[str, Any]]:
        """Take a person out of the archive. Their documents stay.

        Their links go with them (both link tables cascade). The documents
        they were in are reindexed, or searching the name would still find
        them. A cluster left with neither a document nor anyone else in it goes
        too -- a remembered birth of nobody is nothing. Returns what was
        removed, or None when there is no such person.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT first_name, last_name FROM people WHERE id = ?", (person_id,))
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("SELECT document_id FROM document_people WHERE person_id = ?", (person_id,))
            doc_ids = [r["document_id"] for r in cur.fetchall()]

            conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
            for doc_id in doc_ids:
                self._reindex(conn, doc_id)
            clusters = self._prune_empty_clusters(conn)

        return {
            "id": person_id,
            "name": f"{row['first_name']} {row['last_name']}".strip(),
            "documents": len(doc_ids),
            "clusters_removed": clusters,
        }

    # -------------------------------------------------------------------------
    # Documents
    # -------------------------------------------------------------------------
    @staticmethod
    def build_search_content(doc: Dict[str, Any], people_names: Optional[List[str]] = None,
                             page_texts: Optional[List[str]] = None) -> str:
        """Fold every human-readable field of a document into one search key.

        An item's other pages add their transcriptions: the item is found by
        anything on any of its pages.
        """
        parts: List[str] = []
        p = Path(doc.get("file_path") or "")
        if p.stem:
            parts.append(p.stem.replace("_", " ").replace("-", " "))
            parts.append(p.name)

        for field in ("transcription", "title", "location", "creator", "summary", "notes", "kind"):
            val = doc.get(field)
            if val and isinstance(val, str) and val.strip():
                parts.append(val.strip())

        parts.extend(_json_list(doc.get("tags")))
        parts.extend(t.strip() for t in (page_texts or []) if t and t.strip())
        for name in people_names or []:
            if name and name.strip():
                parts.append(name.strip())

        exact = PolytonicNormalizer.normalize_exact(" ".join(parts))
        return PolytonicNormalizer.normalize_search(exact)

    def _people_names(self, conn: sqlite3.Connection, doc_id: int) -> List[str]:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.first_name || ' ' || p.last_name AS name
            FROM document_people dp JOIN people p ON dp.person_id = p.id
            WHERE dp.document_id = ?
            """,
            (doc_id,),
        )
        return [r["name"] for r in cur.fetchall()]

    def _reindex(self, conn: sqlite3.Connection, doc_id: int) -> None:
        """Recompute the FTS key from whatever the row now holds.

        A page is not found on its own -- its text is indexed under its item,
        so a search lands on the item rather than on page 4 of it.
        """
        cur = conn.cursor()
        cur.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
        row = cur.fetchone()
        if not row:
            return
        if row["parent_id"] is not None:
            conn.execute("UPDATE documents SET search_content = '' WHERE id = ?", (doc_id,))
            self._reindex(conn, row["parent_id"])
            return
        pages = [r["transcription"] for r in conn.execute(
            "SELECT transcription FROM documents WHERE parent_id = ? ORDER BY page_no", (doc_id,))]
        key = self.build_search_content(dict(row), self._people_names(conn, doc_id), pages)
        conn.execute("UPDATE documents SET search_content = ? WHERE id = ?", (key, doc_id))

    @staticmethod
    def _derive_date(edtf: Optional[str]) -> Dict[str, Any]:
        """One place where an EDTF string becomes sortable, displayable columns.

        An unreadable date is refused, never stored as a guess. A document with
        no date is honestly undated and sits off the timeline; a document dated
        1900 because the parser could not read "May 1923" is a fabrication the
        timeline would then repeat as fact.
        """
        if not edtf or not str(edtf).strip():
            return {"edtf_date": None, "sort_start": None, "sort_end": None, "date_display": None}
        parsed = EDTFParser.parse(str(edtf).strip(), strict=True)  # raises NotADate
        return {
            "edtf_date": parsed.raw,
            "sort_start": parsed.sort_start,
            "sort_end": parsed.sort_end,
            "date_display": parsed.date_display,
        }

    def add_document(
        self,
        file_path: str,
        thumbnail_path: Optional[str] = None,
        kind: str = "document",
        language: str = "el-poly",
        transcription: Optional[str] = None,
        title: Optional[str] = None,
        edtf_date: Optional[str] = None,
        location: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        confidence_score: float = 1.0,
        sha256: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> int:
        exact = PolytonicNormalizer.normalize_exact(transcription) if transcription else None
        kind = normalize_kind(kind)
        date = self._derive_date(edtf_date)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        if (not sha256 or not file_size) and Path(file_path).exists() and Path(file_path).is_file():
            computed_sha, computed_sz = compute_sha256_and_size(file_path)
            sha256 = sha256 or computed_sha
            file_size = file_size or computed_sz

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO documents (
                    file_path, thumbnail_path, kind, language, transcription,
                    title, edtf_date, sort_start, sort_end, date_display, location,
                    metadata_json, confidence_score, sha256, file_size
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    thumbnail_path = COALESCE(excluded.thumbnail_path, documents.thumbnail_path),
                    kind = excluded.kind,
                    language = excluded.language,
                    transcription = COALESCE(excluded.transcription, documents.transcription),
                    title = COALESCE(excluded.title, documents.title),
                    edtf_date = COALESCE(excluded.edtf_date, documents.edtf_date),
                    sort_start = COALESCE(excluded.sort_start, documents.sort_start),
                    sort_end = COALESCE(excluded.sort_end, documents.sort_end),
                    date_display = COALESCE(excluded.date_display, documents.date_display),
                    location = COALESCE(excluded.location, documents.location),
                    metadata_json = excluded.metadata_json,
                    confidence_score = excluded.confidence_score,
                    sha256 = COALESCE(excluded.sha256, documents.sha256),
                    file_size = COALESCE(excluded.file_size, documents.file_size)
                """,
                (
                    str(file_path),
                    str(thumbnail_path) if thumbnail_path else None,
                    kind,
                    language,
                    exact,
                    title or Path(file_path).stem,
                    date["edtf_date"],
                    date["sort_start"],
                    date["sort_end"],
                    date["date_display"],
                    location,
                    meta_json,
                    confidence_score,
                    sha256,
                    file_size,
                ),
            )
            cur.execute("SELECT id FROM documents WHERE file_path = ?", (str(file_path),))
            doc_id = cur.fetchone()["id"]
            self._reindex(conn, doc_id)
            return doc_id

    def index_scan(
        self,
        file_path: str | Path,
        kind: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Bring a scan into the archive: hash it, thumbnail it, sort it, record it.

        The single ingest path. A kind given by the curator is used as given.
        Otherwise the page is sorted by a text detector -- text pages become
        documents, photographs photographs -- and the evidence is kept in the
        record's metadata ("sorting"), so the decision can be seen and undone.
        It still guesses nothing else: no date, no title beyond the filename.
        An undecided page is filed as a document, so it is still read.
        """
        p = Path(file_path).expanduser().resolve()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"Scan file does not exist: {p}")

        existing = self.get_document_by_path(str(p))
        if existing:
            return existing["id"]

        sha256, file_size = compute_sha256_and_size(p)

        doc_meta = dict(metadata or {})
        thumb_path: Optional[Path] = None
        sorting: Optional[Dict[str, Any]] = None
        try:
            from PIL import Image
            with Image.open(p) as img:
                doc_meta.setdefault("dimensions", {"width": img.width, "height": img.height})
                page = img.convert("RGB")
                page.thumbnail((1280, 1280))
                if not kind:
                    from src.ingestion.text_presence import sort_pil
                    sorting = sort_pil(page)
                thumb_dir = Path("data/thumbnails")
                thumb_dir.mkdir(parents=True, exist_ok=True)
                # The fingerprint keeps two different "Scan 1.png" from sharing one.
                candidate = thumb_dir / f"{p.stem}_{(sha256 or '')[:8]}_thumb.jpg"
                if not candidate.exists():
                    page.thumbnail((400, 400))
                    page.save(candidate, "JPEG", quality=85)
                thumb_path = candidate if candidate.exists() else None
        except Exception:
            pass

        if sorting:
            doc_meta["sorting"] = sorting
        return self.add_document(
            file_path=str(p),
            thumbnail_path=str(thumb_path) if thumb_path else None,
            kind=normalize_kind(kind or (sorting or {}).get("kind")),
            language="el-poly",
            metadata=doc_meta,
            sha256=sha256,
            file_size=file_size,
        )

    def get_document_by_sha256(self, sha256: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,))
            row = cur.fetchone()
            return self._hydrate(dict(row)) if row else None

    def get_document(self, doc_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
            row = cur.fetchone()
            return self._hydrate(dict(row)) if row else None

    def get_document_by_path(self, file_path: str | Path) -> Optional[Dict[str, Any]]:
        """The record for exactly this file, or None.

        Matched on the path only -- as written or as it resolves -- never on the
        file name. Name matching made a second folder's "Scan 1.png" count as
        already indexed, so a different scan was silently never added.
        """
        raw_str = str(file_path)
        try:
            resolved_str = str(Path(file_path).expanduser().resolve())
        except Exception:
            resolved_str = raw_str

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM documents WHERE file_path = ? OR file_path = ?", (raw_str, resolved_str))
            row = cur.fetchone()
            if row:
                return self._hydrate(dict(row))
        return self.indexed_paths().get(resolved_str)

    def indexed_paths(self) -> Dict[str, Dict[str, Any]]:
        """Every record, keyed by the resolved path of its file -- one query, so
        a folder of hundreds can be checked without a lookup per file."""
        out: Dict[str, Dict[str, Any]] = {}
        with self.get_connection() as conn:
            for r in conn.execute("SELECT * FROM documents").fetchall():
                try:
                    key = str(Path(r["file_path"]).expanduser().resolve())
                except Exception:
                    key = r["file_path"]
                out.setdefault(key, self._hydrate(dict(r)))
        return out

    @staticmethod
    def _hydrate(row: Dict[str, Any]) -> Dict[str, Any]:
        """Decode the two JSON-bearing columns and derive what the UI reads."""
        try:
            row["metadata"] = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            row["metadata"] = {}
        row["tags"] = _json_list(row.get("tags"))
        row["reviewed"] = bool(row.get("reviewed_at"))
        row["excluded"] = bool(row.get("excluded"))
        row["textual"] = is_textual_kind(row.get("kind"))
        row["on_timeline"] = bool(row.get("edtf_date")) and not row["excluded"]
        row["is_page"] = row.get("parent_id") is not None
        return row

    @staticmethod
    def _page_summary(row: Dict[str, Any]) -> Dict[str, Any]:
        """One page as the studio and the timeline show it."""
        try:
            meta = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        return {
            "id": row["id"],
            "page_no": row.get("page_no") or 0,
            "name": Path(row["file_path"]).name,
            "file_path": row["file_path"],
            "thumbnail_path": row.get("thumbnail_path"),
            "transcription": row.get("transcription") or "",
            "confidence_score": row.get("confidence_score"),
            "language": row.get("language"),
            "sha256": row.get("sha256") or "",
            "file_size": row.get("file_size") or 0,
            "sorting": (meta.get("sorting") if isinstance(meta, dict) else None),
        }

    def _pages(self, conn: sqlite3.Connection, lead: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Every page of an item in order, the item's own first page included."""
        rows = conn.execute(
            "SELECT * FROM documents WHERE parent_id = ? ORDER BY page_no, id", (lead["id"],)
        ).fetchall()
        return [self._page_summary(lead)] + [self._page_summary(dict(r)) for r in rows]

    def get_document_full(self, doc_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
            row = cur.fetchone()
            if not row:
                return None
            doc = self._hydrate(dict(row))
            doc["pages"] = self._pages(conn, dict(row)) if not doc["is_page"] else []
            doc["page_count"] = len(doc["pages"]) or 1

            cur.execute(
                """
                SELECT p.id, p.first_name, p.last_name, dp.role
                FROM document_people dp JOIN people p ON dp.person_id = p.id
                WHERE dp.document_id = ?
                ORDER BY p.last_name, p.first_name
                """,
                (doc_id,),
            )
            doc["people"] = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT e.id, e.title, e.edtf_date, e.date_display, e.location,
                       (SELECT COUNT(*) FROM event_sources s WHERE s.event_id = e.id) AS source_count
                FROM event_sources es JOIN events e ON es.event_id = e.id
                WHERE es.document_id = ?
                ORDER BY e.sort_start ASC
                """,
                (doc_id,),
            )
            doc["clusters"] = [dict(r) for r in cur.fetchall()]
            return doc

    def list_documents(
        self,
        reviewed: Optional[bool] = None,
        kind: Optional[str] = None,
        people: Optional[List[int]] = None,
        people_match: str = "any",
    ) -> List[Dict[str, Any]]:
        """List documents, narrowed by review state, kind and the people in them.

        `people_match="all"` is the genealogically interesting one: it finds
        where two people appear together -- a marriage, a joint crossing -- as
        opposed to everything touching either of them.
        """
        # Pages are part of their item, never listed on their own.
        clauses, params = ["parent_id IS NULL"], []
        if reviewed is True:
            clauses.append("reviewed_at IS NOT NULL")
        elif reviewed is False:
            clauses.append("reviewed_at IS NULL")
        if kind and kind != "all":
            clauses.append("kind = ?")
            params.append(normalize_kind(kind))

        if people:
            ids = [int(p) for p in people]
            marks = ",".join("?" for _ in ids)
            if people_match == "all":
                clauses.append(
                    f"""id IN (SELECT document_id FROM document_people
                               WHERE person_id IN ({marks})
                               GROUP BY document_id
                               HAVING COUNT(DISTINCT person_id) = ?)"""
                )
                params.extend([*ids, len(set(ids))])
            else:
                clauses.append(
                    f"id IN (SELECT document_id FROM document_people WHERE person_id IN ({marks}))"
                )
                params.extend(ids)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM documents {where} ORDER BY id", params)
            docs = [self._hydrate(dict(r)) for r in cur.fetchall()]

            # The catalog groups by person, so every row carries its people.
            cur.execute(
                """
                SELECT dp.document_id, p.id AS pid, p.first_name, p.last_name, dp.role
                FROM document_people dp JOIN people p ON p.id = dp.person_id
                ORDER BY p.last_name COLLATE NOCASE, p.first_name COLLATE NOCASE
                """
            )
            by_doc: Dict[int, List[Dict[str, Any]]] = {}
            for r in cur.fetchall():
                by_doc.setdefault(r["document_id"], []).append(
                    {"id": r["pid"], "first_name": r["first_name"],
                     "last_name": r["last_name"], "role": r["role"]}
                )
            cur.execute("SELECT parent_id, COUNT(*) AS n FROM documents "
                        "WHERE parent_id IS NOT NULL GROUP BY parent_id")
            extra_pages = {r["parent_id"]: r["n"] for r in cur.fetchall()}
            for doc in docs:
                doc["people"] = by_doc.get(doc["id"], [])
                doc["page_count"] = 1 + extra_pages.get(doc["id"], 0)
            return docs

    def update_document(
        self,
        doc_id: int,
        fields: Optional[Dict[str, Any]] = None,
        people_links: Optional[List[Dict[str, Any]]] = None,
        reviewed: Optional[bool] = None,
        reviewed_by: str = "studio",
    ) -> Dict[str, Any]:
        """The single write path for curator-authored data.

        v1 had three overlapping writers (update_document_curation,
        update_document_full, and the event branch inside it). They disagreed
        about which fields were authoritative, so the studio kept two copies in
        sync by copying. There is one copy now, so there is one writer.
        """
        fields = dict(fields or {})
        writable = {
            "title", "edtf_date", "location", "summary", "creator", "notes",
            "kind", "language", "transcription", "excluded",
        }
        updates: Dict[str, Any] = {}

        for key in list(fields):
            if key in writable:
                updates[key] = fields[key]

        if "kind" in updates:
            updates["kind"] = normalize_kind(updates["kind"])
        if "transcription" in updates:
            val = updates["transcription"]
            updates["transcription"] = PolytonicNormalizer.normalize_exact(val) if val else None
        if "excluded" in updates:
            updates["excluded"] = 1 if updates["excluded"] else 0
        if "edtf_date" in updates:
            updates.update(self._derive_date(updates["edtf_date"]))
        if "tags" in fields:
            updates["tags"] = json.dumps(_json_list(fields["tags"]), ensure_ascii=False)

        if reviewed is True:
            updates["reviewed_at"] = None  # replaced below with CURRENT_TIMESTAMP
            updates["reviewed_by"] = reviewed_by
        elif reviewed is False:
            updates["reviewed_at"] = None
            updates["reviewed_by"] = None

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, parent_id FROM documents WHERE id = ?", (doc_id,))
            found = cur.fetchone()
            if not found:
                raise ValueError(f"Document ID {doc_id} not found")
            if found["parent_id"] is not None:
                # A page holds only what is on it. The item is described once,
                # on its first page, so nothing else can drift out of step.
                updates = {k: v for k, v in updates.items() if k == "transcription"}
                people_links = None

            if updates:
                assignments, params = [], []
                for key, val in updates.items():
                    if key == "reviewed_at" and reviewed is True:
                        assignments.append("reviewed_at = CURRENT_TIMESTAMP")
                        continue
                    assignments.append(f"{key} = ?")
                    params.append(val)
                params.append(doc_id)
                conn.execute(f"UPDATE documents SET {', '.join(assignments)} WHERE id = ?", params)

            if people_links is not None:
                conn.execute("DELETE FROM document_people WHERE document_id = ?", (doc_id,))
                for item in people_links:
                    p_id = item.get("id") or item.get("person_id")
                    if p_id:
                        # Someone deleted since the editor loaded is skipped rather
                        # than failing the save: OR IGNORE does not cover a foreign
                        # key, so a link to them would lose the whole record.
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO document_people (document_id, person_id, role)
                            SELECT ?, id, ? FROM people WHERE id = ?
                            """,
                            (doc_id, item.get("role", "subject"), int(p_id)),
                        )

            self._reindex(conn, doc_id)

        return self.get_document_full(doc_id)  # type: ignore

    def update_document_ocr(
        self,
        doc_id: int,
        transcription: str,
        language: str,
        confidence_score: float = 1.0,
        ocr_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist machine output. Never touches review state -- OCR is not review."""
        exact = PolytonicNormalizer.normalize_exact(transcription) if transcription else None
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT metadata_json FROM documents WHERE id = ?", (doc_id,))
            row = cur.fetchone()
            try:
                curr_meta = json.loads(row["metadata_json"] or "{}") if row else {}
            except Exception:
                curr_meta = {}
            if ocr_metadata:
                curr_meta.update(ocr_metadata)

            conn.execute(
                """
                UPDATE documents
                SET transcription = ?, language = ?, confidence_score = ?, metadata_json = ?
                WHERE id = ?
                """,
                (exact, language, confidence_score, json.dumps(curr_meta, ensure_ascii=False), doc_id),
            )
            self._reindex(conn, doc_id)

    @staticmethod
    def list_kinds() -> List[Dict[str, Any]]:
        """The taxonomy is fixed, closed and small. That is the point."""
        return [dict(k) for k in DOCUMENT_KINDS]

    def list_tags(self) -> List[Dict[str, Any]]:
        """Tags in use, most common first -- the open half of the taxonomy."""
        counts: Dict[str, int] = {}
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT tags FROM documents WHERE tags IS NOT NULL AND tags != '' AND parent_id IS NULL")
            for r in cur.fetchall():
                for tag in _json_list(r["tags"]):
                    counts[tag] = counts.get(tag, 0) + 1
        return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def delete_document(self, doc_id: int) -> bool:
        """Remove a record. An item goes with all its pages; a page goes alone.
        The files themselves are never touched."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, parent_id FROM documents WHERE id = ?", (doc_id,))
            row = cur.fetchone()
            if not row:
                return False
            ids = [doc_id] + [r["id"] for r in cur.execute(
                "SELECT id FROM documents WHERE parent_id = ?", (doc_id,))]
            self._unpin_portraits(conn, ids)
            marks = ",".join("?" for _ in ids)
            cur.execute(f"DELETE FROM documents WHERE id IN ({marks})", ids)
            if row["parent_id"] is not None:
                self._renumber_pages(conn, row["parent_id"])
                self._reindex(conn, row["parent_id"])
            self._prune_empty_clusters(conn)
            return True

    # -------------------------------------------------------------------------
    # Pages: several scans, one item
    # -------------------------------------------------------------------------
    ITEM_FIELDS = ("title", "edtf_date", "sort_start", "sort_end", "date_display", "location",
                   "summary", "creator", "notes", "tags", "kind", "language",
                   "reviewed_at", "reviewed_by", "excluded")

    @staticmethod
    def _renumber_pages(conn: sqlite3.Connection, lead_id: int) -> None:
        rows = conn.execute("SELECT id FROM documents WHERE parent_id = ? ORDER BY page_no, id",
                            (lead_id,)).fetchall()
        for n, r in enumerate(rows, start=1):
            conn.execute("UPDATE documents SET page_no = ? WHERE id = ?", (n, r["id"]))

    @staticmethod
    def _is_file_name_title(row: Dict[str, Any]) -> bool:
        return not row.get("title") or row["title"] == Path(row["file_path"]).stem

    def combine_pages(self, lead_id: int, page_ids: List[int]) -> Dict[str, Any]:
        """Make `page_ids`, in order, the pages after `lead_id`'s own.

        Nothing described on any of them is lost: a blank on the item is filled
        from the first page that has it, subjects and people are joined, notes
        kept side by side, and clusters that cited a page cite the item. The
        item stays reviewed only if every part of it was. A record that already
        has pages brings them along, in their order.
        """
        with self.get_connection() as conn:
            lead = conn.execute("SELECT * FROM documents WHERE id = ?", (lead_id,)).fetchone()
            if not lead:
                raise ValueError(f"Document ID {lead_id} not found")
            if lead["parent_id"] is not None:
                raise ValueError("That record is already a page of another item.")
            lead = dict(lead)

            order: List[int] = []
            for pid in page_ids:
                if pid == lead_id or pid in order:
                    continue
                row = conn.execute("SELECT id, parent_id FROM documents WHERE id = ?", (pid,)).fetchone()
                if not row:
                    raise ValueError(f"Document ID {pid} not found")
                if row["parent_id"] not in (None, lead_id):
                    raise ValueError(f"Record #{pid} is already a page of another item.")
                if row["parent_id"] == lead_id:
                    continue
                order.append(pid)
                order.extend(r["id"] for r in conn.execute(
                    "SELECT id FROM documents WHERE parent_id = ? ORDER BY page_no, id", (pid,)))

            if not order:
                return self.get_document_full(lead_id)  # type: ignore

            updates: Dict[str, Any] = {}
            tags = _json_list(lead.get("tags"))
            notes = [lead["notes"]] if lead.get("notes") else []
            all_reviewed = bool(lead.get("reviewed_at"))
            all_excluded = bool(lead.get("excluded"))
            start = conn.execute("SELECT COALESCE(MAX(page_no), 0) FROM documents WHERE parent_id = ?",
                                 (lead_id,)).fetchone()[0]

            for n, pid in enumerate(order, start=start + 1):
                page = dict(conn.execute("SELECT * FROM documents WHERE id = ?", (pid,)).fetchone())
                if page["parent_id"] is None:
                    # A record joining as a page: carry over what it described.
                    if self._is_file_name_title({**lead, **updates}) and not self._is_file_name_title(page):
                        updates["title"] = page["title"]
                    if not (updates.get("edtf_date") or lead.get("edtf_date")) and page.get("edtf_date"):
                        for f in ("edtf_date", "sort_start", "sort_end", "date_display"):
                            updates[f] = page.get(f)
                    for f in ("location", "summary", "creator"):
                        if not (updates.get(f) or lead.get(f)) and page.get(f):
                            updates[f] = page[f]
                    if page.get("notes") and page["notes"] not in notes:
                        notes.append(page["notes"])
                    for t in _json_list(page.get("tags")):
                        if t not in tags:
                            tags.append(t)
                    all_reviewed = all_reviewed and bool(page.get("reviewed_at"))
                    all_excluded = all_excluded and bool(page.get("excluded"))

                    conn.execute(
                        """INSERT OR IGNORE INTO document_people (document_id, person_id, role)
                           SELECT ?, person_id, role FROM document_people WHERE document_id = ?""",
                        (lead_id, pid))
                    conn.execute("DELETE FROM document_people WHERE document_id = ?", (pid,))
                    conn.execute(
                        """INSERT OR IGNORE INTO event_sources (event_id, document_id)
                           SELECT event_id, ? FROM event_sources WHERE document_id = ?""",
                        (lead_id, pid))
                    conn.execute("DELETE FROM event_sources WHERE document_id = ?", (pid,))
                conn.execute("UPDATE documents SET parent_id = ?, page_no = ? WHERE id = ?",
                             (lead_id, n, pid))

            updates["tags"] = json.dumps(tags, ensure_ascii=False) if tags else lead.get("tags")
            updates["notes"] = "\n\n".join(notes) if notes else None
            updates["excluded"] = 1 if all_excluded else 0
            if not all_reviewed:
                updates["reviewed_at"] = None
                updates["reviewed_by"] = None
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*updates.values(), lead_id))

            for pid in order:
                self._reindex(conn, pid)
            self._reindex(conn, lead_id)
        return self.get_document_full(lead_id)  # type: ignore

    def split_page(self, page_id: int) -> Dict[str, Any]:
        """Take a page out of its item: it becomes a record of its own, to review."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT id, parent_id FROM documents WHERE id = ?", (page_id,)).fetchone()
            if not row:
                raise ValueError(f"Document ID {page_id} not found")
            lead_id = row["parent_id"]
            if lead_id is None:
                raise ValueError("That is the item's first page, not one of its other pages.")
            conn.execute("""UPDATE documents SET parent_id = NULL, page_no = NULL,
                            reviewed_at = NULL, reviewed_by = NULL WHERE id = ?""", (page_id,))
            self._renumber_pages(conn, lead_id)
            self._reindex(conn, page_id)
            self._reindex(conn, lead_id)
        return {"document": self.get_document_full(page_id), "item": self.get_document_full(lead_id)}

    def page_proposals(self) -> List[Dict[str, Any]]:
        """Records whose file names say they are pages of one item, not yet combined.

        Grouped per folder by the naming rule in ingestion/pages.py. A group
        whose first page already has pages proposes only the records still
        outside it. Scanner counters ("Scan 1", "Scan 2") are proposed but
        marked, since their numbers do not mean pages.
        """
        from src.ingestion.pages import group_pages

        with self.get_connection() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, file_path, thumbnail_path, title, kind, parent_id, page_no FROM documents")]
        by_path = {r["file_path"]: r for r in rows}
        page_counts: Dict[int, int] = {}
        for r in rows:
            if r["parent_id"] is not None:
                page_counts[r["parent_id"]] = page_counts.get(r["parent_id"], 0) + 1

        proposals = []
        for group in group_pages([r["file_path"] for r in rows]):
            members = [by_path[p] for p in group["paths"]]
            loose = [m for m in members if m["parent_id"] is None]
            lead = loose[0] if loose else None
            if len(loose) < 2 or lead is None:
                continue
            proposals.append({
                "key": f"{group['folder']}|{str(group['name']).casefold()}",
                "name": group["name"],
                "folder": Path(str(group["folder"])).name,
                "counter": group["counter"],
                "lead_id": lead["id"],
                "doc_ids": [m["id"] for m in loose],
                "pages": [{
                    "id": m["id"], "name": Path(m["file_path"]).name, "kind": m["kind"],
                    "described": not self._is_file_name_title(m),
                    "page_count": 1 + page_counts.get(m["id"], 0),
                } for m in loose],
            })
        return proposals

    @staticmethod
    def _unpin_portraits(conn: sqlite3.Connection, doc_ids: List[int]) -> None:
        """A portrait pinned to a scan that is going returns to the archive's own
        choice. ON DELETE SET NULL alone leaves it "pinned" to nothing."""
        marks = ",".join("?" for _ in doc_ids)
        conn.execute(
            f"""
            UPDATE people SET portrait_mode = 'auto', portrait_document_id = NULL
            WHERE portrait_mode = 'pinned' AND portrait_document_id IN ({marks})
            """,
            doc_ids,
        )

    @staticmethod
    def _prune_empty_clusters(conn: sqlite3.Connection) -> int:
        """Drop clusters that have lost every source but were never source-less.

        A cluster created with no sources at all (a remembered birth) is
        legitimate and must survive; one whose documents were all deleted is
        debris. The two are told apart by event_participants.
        """
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM events
            WHERE id NOT IN (SELECT DISTINCT event_id FROM event_sources)
              AND id NOT IN (SELECT DISTINCT event_id FROM event_participants)
            """
        )
        return cur.rowcount or 0

    def purge_missing_documents(self, purge_sample_data: bool = False) -> Dict[str, int]:
        """Remove records whose files are gone, or synthetic sample records.

        The seed's people exist only for its sample documents, so they go with
        them: anyone who was in a sample and is left in nothing else. A person
        the curator added, or who appears in any real document, stays -- as does
        everyone linked to a record purged merely because its file went missing.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, file_path FROM documents")
            doc_ids_to_purge, sample_ids = [], []
            for r in cur.fetchall():
                fpath_str = r["file_path"]
                fpath = Path(fpath_str)
                is_missing = not fpath.exists() or not fpath.is_file()
                is_sample = purge_sample_data and (
                    "sample_data" in fpath_str.lower() or fpath_str.startswith("data/sample_data")
                )
                if is_missing or is_sample:
                    doc_ids_to_purge.append(r["id"])
                if is_sample:
                    sample_ids.append(r["id"])

            sample_people: List[int] = []
            if sample_ids:
                marks = ",".join("?" for _ in sample_ids)
                cur.execute(
                    f"SELECT DISTINCT person_id FROM document_people WHERE document_id IN ({marks})",
                    sample_ids,
                )
                sample_people = [r["person_id"] for r in cur.fetchall()]

            # An item whose first page is gone lives on in its next page; a
            # missing page just leaves its item.
            gone = set(doc_ids_to_purge)
            for lead_id in list(gone):
                survivors = [r["id"] for r in cur.execute(
                    "SELECT id FROM documents WHERE parent_id = ? ORDER BY page_no, id", (lead_id,))
                    if r["id"] not in gone]
                if survivors and lead_id not in sample_ids:
                    self._promote(conn, lead_id, survivors)
            parents = {r["parent_id"] for r in cur.execute(
                f"SELECT parent_id FROM documents WHERE parent_id IS NOT NULL AND id IN "
                f"({','.join('?' for _ in doc_ids_to_purge) or 'NULL'})", doc_ids_to_purge)}

            purged_docs = purged_events = purged_people = 0
            if doc_ids_to_purge:
                placeholders = ",".join("?" for _ in doc_ids_to_purge)
                self._unpin_portraits(conn, doc_ids_to_purge)
                cur.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", doc_ids_to_purge)
                purged_docs = len(doc_ids_to_purge)

                for pid in sample_people:
                    cur.execute(
                        """
                        SELECT 1 FROM document_people WHERE person_id = ?
                        UNION ALL SELECT 1 FROM event_participants WHERE person_id = ?
                        LIMIT 1
                        """,
                        (pid, pid),
                    )
                    if cur.fetchone() is None:
                        conn.execute("DELETE FROM people WHERE id = ?", (pid,))
                        purged_people += 1
                purged_events = self._prune_empty_clusters(conn)
            for lead_id in parents - gone:
                self._renumber_pages(conn, lead_id)
                self._reindex(conn, lead_id)

        return {
            "purged_documents": purged_docs,
            "purged_events": max(0, purged_events),
            "purged_people": purged_people,
        }

    def _promote(self, conn: sqlite3.Connection, lead_id: int, survivors: List[int]) -> None:
        """Make the first surviving page the item, carrying the description with it."""
        new_lead, rest = survivors[0], survivors[1:]
        lead = dict(conn.execute("SELECT * FROM documents WHERE id = ?", (lead_id,)).fetchone())
        sets = ", ".join(f"{f} = ?" for f in self.ITEM_FIELDS)
        conn.execute(f"UPDATE documents SET {sets}, parent_id = NULL, page_no = NULL WHERE id = ?",
                     (*(lead.get(f) for f in self.ITEM_FIELDS), new_lead))
        for n, pid in enumerate(rest, start=1):
            conn.execute("UPDATE documents SET parent_id = ?, page_no = ? WHERE id = ?", (new_lead, n, pid))
        for table in ("document_people", "event_sources"):
            conn.execute(f"UPDATE OR IGNORE {table} SET document_id = ? WHERE document_id = ?", (new_lead, lead_id))
        conn.execute("""UPDATE people SET portrait_document_id = ?
                        WHERE portrait_document_id = ?""", (new_lead, lead_id))
        self._reindex(conn, new_lead)

    def link_document_person(self, doc_id: int, person_id: int, role: str = "subject") -> None:
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO document_people (document_id, person_id, role) VALUES (?, ?, ?)",
                (doc_id, person_id, role),
            )
            self._reindex(conn, doc_id)

    # -------------------------------------------------------------------------
    # Clusters
    # -------------------------------------------------------------------------
    def add_event(
        self,
        title: str,
        edtf_date: str,
        description: Optional[str] = None,
        category: str = "life_event",
        location: Optional[str] = None,
        verification_status: str = "pending",
        source_doc_ids: Optional[List[int]] = None,
        participant_ids: Optional[List[int]] = None,
    ) -> int:
        """Create a cluster: an occurrence evidenced by many documents, or none.

        Given exactly one source document this writes nothing to `events` -- it
        describes that document instead, because a one-document event and the
        document are the same thing said twice.
        """
        parsed_date = EDTFParser.parse(edtf_date)
        source_doc_ids = list(source_doc_ids or [])
        participant_ids = list(participant_ids or [])

        if len(source_doc_ids) == 1:
            doc_id = source_doc_ids[0]
            tag = LEGACY_CATEGORY_TAGS.get((category or "").lower())
            existing = self.get_document(doc_id)
            tags = list(existing.get("tags") or []) if existing else []
            if tag and tag not in tags:
                tags.append(tag)
            self.update_document(
                doc_id,
                fields={
                    "title": title.strip(),
                    "edtf_date": edtf_date,
                    "summary": description,
                    "location": location,
                    "tags": tags,
                },
                people_links=[{"id": p, "role": "subject"} for p in participant_ids] or None,
                reviewed=True if verification_status == "verified" else None,
            )
            return doc_id

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO events (
                    title, description, category, edtf_date,
                    sort_start, sort_end, date_display, location, verification_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title.strip(), description, category, parsed_date.raw,
                    parsed_date.sort_start, parsed_date.sort_end,
                    parsed_date.date_display, location, verification_status,
                ),
            )
            event_id: int = cur.lastrowid  # type: ignore

            for doc_id in source_doc_ids:
                cur.execute(
                    "INSERT OR IGNORE INTO event_sources (event_id, document_id) VALUES (?, ?)",
                    (event_id, doc_id),
                )
            for p_id in participant_ids:
                cur.execute(
                    "INSERT OR IGNORE INTO event_participants (event_id, person_id) VALUES (?, ?)",
                    (event_id, p_id),
                )
            return event_id

    # -------------------------------------------------------------------------
    # Full-Text Search (Diacritic & Sigma Folded)
    # -------------------------------------------------------------------------
    def search_documents(self, query: str) -> List[Dict[str, Any]]:
        """Search documents using FTS5 with diacritic and sigma-folded query."""
        if not query or not query.strip():
            return []

        search_key = PolytonicNormalizer.normalize_search(query)
        fts_query = " ".join(f'"{token}"*' for token in search_key.split())

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT d.*, snippet(fts_documents, 0, '<b>', '</b>', '...', 16) AS match_snippet
                FROM fts_documents f JOIN documents d ON f.document_id = d.id
                WHERE fts_documents MATCH ?
                ORDER BY rank
                """,
                (fts_query,),
            )
            return [self._hydrate(dict(r)) for r in cur.fetchall()]

    # -------------------------------------------------------------------------
    # Timeline projection
    # -------------------------------------------------------------------------
    @staticmethod
    def _doc_summary(doc: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": doc["id"],
            "file_path": doc["file_path"],
            "thumbnail_path": doc.get("thumbnail_path") or doc["file_path"],
            "language": doc.get("language", "el-poly"),
            "kind": doc.get("kind", "document"),
            "sha256": doc.get("sha256") or "",
            "file_size": doc.get("file_size") or 0,
            "page_count": len(doc.get("pages") or []) or 1,
        }

    def get_timeline_export_data(self) -> Dict[str, Any]:
        """Project the archive onto the timeline.

        A document with a date IS a timeline entry -- projected from the very
        columns the curator edited, with no second record to keep in step. A
        cluster supersedes its member documents and appears in their place.
        Excluded documents appear nowhere, which in v1 they silently did.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()

            cur.execute("SELECT * FROM documents ORDER BY id")
            all_rows = [dict(r) for r in cur.fetchall()]
            pages_of: Dict[int, List[Dict[str, Any]]] = {}
            for r in sorted((r for r in all_rows if r["parent_id"] is not None),
                            key=lambda r: (r["page_no"] or 0, r["id"])):
                pages_of.setdefault(r["parent_id"], []).append(self._page_summary(r))
            docs_by_id = {}
            for r in all_rows:
                if r["parent_id"] is None:
                    doc = self._hydrate(r)
                    doc["pages"] = [self._page_summary(r)] + pages_of.get(r["id"], [])
                    docs_by_id[r["id"]] = doc

            cur.execute(
                """
                SELECT dp.document_id, p.id AS pid, p.first_name || ' ' || p.last_name AS name, dp.role
                FROM document_people dp JOIN people p ON dp.person_id = p.id
                ORDER BY p.last_name, p.first_name
                """
            )
            doc_people: Dict[int, List[Dict[str, Any]]] = {}
            for row in cur.fetchall():
                doc_people.setdefault(row["document_id"], []).append(
                    {"id": row["pid"], "name": row["name"], "role": row["role"]}
                )

            # --- Clusters first; they claim their member documents -----------
            cur.execute("SELECT * FROM events ORDER BY sort_start ASC")
            cluster_rows = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT event_id, document_id FROM event_sources")
            cluster_sources: Dict[int, List[int]] = {}
            for row in cur.fetchall():
                cluster_sources.setdefault(row["event_id"], []).append(row["document_id"])

            cur.execute(
                """
                SELECT ep.event_id, p.id AS pid, p.first_name || ' ' || p.last_name AS name
                FROM event_participants ep JOIN people p ON ep.person_id = p.id
                """
            )
            cluster_people: Dict[int, List[Dict[str, Any]]] = {}
            for row in cur.fetchall():
                cluster_people.setdefault(row["event_id"], []).append({"id": row["pid"], "name": row["name"]})

            entries: List[Dict[str, Any]] = []
            claimed: set = set()

            for ev in cluster_rows:
                member_ids = [d for d in cluster_sources.get(ev["id"], []) if d in docs_by_id]
                members = [docs_by_id[d] for d in member_ids]
                visible = [m for m in members if not m["excluded"]]

                # A cluster reduced to one surviving document is that document.
                if len(member_ids) == 1 and visible:
                    continue
                if member_ids and not visible:
                    continue

                people = {p["id"]: p for p in cluster_people.get(ev["id"], [])}
                for m in visible:
                    for p in doc_people.get(m["id"], []):
                        people.setdefault(p["id"], {"id": p["id"], "name": p["name"]})

                parsed = EDTFParser.parse(ev["edtf_date"])
                entries.append({
                    "id": f"c{ev['id']}",
                    "source": "cluster",
                    "title": ev["title"],
                    "description": ev.get("description") or "",
                    "category": normalize_kind(ev.get("category")),
                    "edtf_date": ev["edtf_date"],
                    "date_display": ev.get("date_display") or parsed.date_display,
                    "sort_start": ev.get("sort_start") if ev.get("sort_start") is not None else parsed.sort_start,
                    "sort_end": ev.get("sort_end"),
                    "start": parsed.iso_start,
                    "end": parsed.iso_end,
                    "location": ev.get("location") or "",
                    "tags": [t for t in [LEGACY_CATEGORY_TAGS.get((ev.get("category") or "").lower())] if t],
                    "people": list(people.values()),
                    "docs": [self._doc_summary(m) for m in visible],
                })
                claimed.update(member_ids)

            # --- Then every document that stands on its own ------------------
            for doc in docs_by_id.values():
                if doc["id"] in claimed or doc["excluded"] or not doc.get("edtf_date"):
                    continue
                parsed = EDTFParser.parse(doc["edtf_date"])
                entries.append({
                    "id": doc["id"],
                    "source": "document",
                    "title": doc.get("title") or Path(doc["file_path"]).stem,
                    "description": doc.get("summary") or (doc.get("transcription") or "")[:240],
                    "category": doc.get("kind") or "document",
                    "edtf_date": doc["edtf_date"],
                    "date_display": doc.get("date_display") or parsed.date_display,
                    "sort_start": doc.get("sort_start") if doc.get("sort_start") is not None else parsed.sort_start,
                    "sort_end": doc.get("sort_end"),
                    "start": parsed.iso_start,
                    "end": parsed.iso_end,
                    "location": doc.get("location") or "",
                    "tags": doc.get("tags") or [],
                    "reviewed": doc["reviewed"],
                    "people": doc_people.get(doc["id"], []),
                    "docs": [self._doc_summary(doc)],
                })

            entries.sort(key=lambda x: (x["sort_start"] is None, x["sort_start"] or 0))

            cur.execute("SELECT * FROM people ORDER BY last_name, first_name")
            people_list = [dict(r) for r in cur.fetchall()]

            return {
                "events": entries,
                "documents": [d for d in docs_by_id.values() if not d["excluded"]],
                "people": people_list,
            }

    # -------------------------------------------------------------------------
    # Whole-archive copies (saving and opening .archivesys files)
    # -------------------------------------------------------------------------
    def archive_counts(self) -> Dict[str, int]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            return {
                table_label: cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table_label, table in (
                    ("documents", "documents"), ("people", "people"), ("clusters", "events"),
                )
            }

    def snapshot_to(self, dest: str | Path) -> Path:
        """Write a consistent copy of the whole database to `dest`.

        Uses SQLite's online backup rather than copying the file, so the copy
        can never be torn by a save landing halfway through -- the studio keeps
        autosaving while an archive is being packed.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.unlink(missing_ok=True)
        src = self.get_connection()
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return dest

    def replace_with(self, source: str | Path) -> None:
        """Replace this database's entire contents with another database file.

        The inverse of snapshot_to, and as safe under a running server: the
        backup API takes the locks that copying the file over would ignore.
        """
        src = sqlite3.connect(str(source))
        dst = self.get_connection()
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def relocate_document(self, doc_id: int, new_path: str | Path) -> None:
        """Point a document at the new location of its master scan.

        The thumbnail follows only when it was the master itself. A separate
        thumbnail that still exists is kept; one that does not is cleared, so
        the timeline falls back to the master instead of a dead link.
        """
        new_path = str(new_path)
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT file_path, thumbnail_path FROM documents WHERE id = ?", (doc_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Document ID {doc_id} not found")
            thumb = row["thumbnail_path"]
            if thumb == row["file_path"]:
                thumb = new_path
            elif thumb and not Path(thumb).is_file():
                thumb = None
            conn.execute(
                "UPDATE documents SET file_path = ?, thumbnail_path = ? WHERE id = ?",
                (new_path, thumb, doc_id),
            )
            self._reindex(conn, doc_id)
