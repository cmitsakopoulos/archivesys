-- SQLite Schema for Historical Family Archive Engine
-- Enforce foreign key constraints
PRAGMA foreign_keys = ON;

-- 1. People Entities
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    maiden_name TEXT,

    -- Lifespan. Validated as strictly as a document's date: an unreadable one
    -- is refused, never stored as the parser's invented 1900.
    birth_edtf TEXT,
    death_edtf TEXT,
    birth_sort REAL,
    death_sort REAL,

    -- Diacritic- and sigma-folded name, so that Γεώργιος and Γεωργιος are seen
    -- to be the same man rather than becoming two people.
    name_key TEXT,

    -- Representative picture. Derived by default; a curator may pin one, or
    -- say there should be none. "None" is a decision, not an empty field.
    portrait_mode TEXT NOT NULL DEFAULT 'auto',   -- 'auto' | 'pinned' | 'none'
    portrait_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    portrait_crop TEXT,                           -- JSON {x,y,w,h}; reserved

    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Documents.
--    A document IS a timeline entry. The descriptive fields below are the same
--    fields the timeline projects; there is no parallel "event" copy of them.
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,
    thumbnail_path TEXT,
    kind TEXT NOT NULL DEFAULT 'document', -- certificate|letter|photograph|passport|press|book|document
    language TEXT NOT NULL DEFAULT 'el-poly', -- 'el-poly', 'el', 'fr', 'en', 'mixed'

    -- Description (projected verbatim onto the timeline)
    title TEXT,
    edtf_date TEXT,                           -- e.g. "1923~", "1912-05/1912-08"
    sort_start REAL,                          -- Mathematical float year (e.g. 1923.37), derived
    sort_end REAL,                            -- Optional end float year, derived
    date_display TEXT,                        -- Human readable badge (e.g. "ca. May 1923"), derived
    location TEXT,
    summary TEXT,                             -- Narrative shown on the timeline card
    creator TEXT,                             -- Issuing authority / photographer
    notes TEXT,                               -- Provenance, not published
    tags TEXT,                                -- JSON array of subject keywords

    -- Content
    transcription TEXT,                       -- Exact Unicode NFC (polytonic / accented)
    search_content TEXT,                      -- Normalized search key (NFD stripped + Greek sigma folded)

    -- Review state: unreviewed is reviewed_at IS NULL. There is no third state.
    reviewed_at TIMESTAMP,
    reviewed_by TEXT,
    excluded INTEGER NOT NULL DEFAULT 0,      -- 1 = keep in archive, keep off the timeline

    -- Provenance of the bytes
    metadata_json TEXT,                       -- Dimensions, EXIF, OCR metrics. Machine-written only.
    confidence_score REAL DEFAULT 1.0,
    sha256 TEXT,                              -- Cryptographic byte fingerprint (survives renames & moves)
    file_size INTEGER,                        -- Exact file size in bytes

    -- Pages. An item scanned as several files is one record (the first page)
    -- with its other pages as records pointing at it. A page keeps its own
    -- file, fingerprint, thumbnail and transcription; everything that
    -- describes the item -- title, date, people, kind, review -- is the
    -- first page's. NULL for a single-page item and for every first page.
    parent_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    page_no INTEGER,                          -- 1, 2, ... for pages; NULL for the first
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes are created in DatabaseManager.init_db AFTER migration, because a
-- v1 database reaching this script has no sort_start column yet.

CREATE TABLE IF NOT EXISTS document_people (
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
    role TEXT DEFAULT 'subject',              -- 'subject', 'author', 'recipient', 'witness', 'photographer'
    PRIMARY KEY (document_id, person_id)
);

-- 3. Clusters.
--    The ONLY thing a document cannot express: an occurrence evidenced by
--    several documents at once, or one evidenced by none at all (a birth, a
--    death, an emigration known from family memory). Rare by design. A cluster
--    with exactly one source document is just that document and is ignored.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    category TEXT DEFAULT 'life_event',
    edtf_date TEXT NOT NULL,
    sort_start REAL NOT NULL,
    sort_end REAL,
    date_display TEXT NOT NULL,
    location TEXT,
    verification_status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS event_participants (
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
    role TEXT DEFAULT 'participant',
    PRIMARY KEY (event_id, person_id)
);

CREATE TABLE IF NOT EXISTS event_sources (
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, document_id)
);

-- 4. Full-Text Search Virtual Table (FTS5)
CREATE VIRTUAL TABLE IF NOT EXISTS fts_documents USING fts5(
    document_id UNINDEXED,
    search_content,
    tokenize = 'unicode61'
);

-- 5. Automated Synchronization Triggers
CREATE TRIGGER IF NOT EXISTS trg_docs_insert AFTER INSERT ON documents
BEGIN
    INSERT INTO fts_documents(document_id, search_content)
    VALUES (new.id, COALESCE(new.search_content, ''));
END;

CREATE TRIGGER IF NOT EXISTS trg_docs_update AFTER UPDATE OF search_content ON documents
BEGIN
    UPDATE fts_documents SET search_content = COALESCE(new.search_content, '')
    WHERE document_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_docs_delete AFTER DELETE ON documents
BEGIN
    DELETE FROM fts_documents WHERE document_id = old.id;
END;
