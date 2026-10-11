/**
 * Historical Family Archive Dashboard & Curation Studio
 *
 * 1. Primary Navigation Router (Scan Explorer, Archive, Record, Timeline)
 * 2. Local Filesystem Scan Browser & one-call ingestion
 * 3. Archive catalog (grid with previews / detailed table)
 * 4. The Record: one scrolling editor over one set of fields, which ARE the
 *    fields the timeline projects. No second copy, no status ladder.
 * 5. Background OCR activity panel & polytonic palette
 */

// Global State
let documents = [];
let currentDocIndex = -1;
let currentDoc = null;
// An item scanned as several files is shown one page at a time.
let currentPage = 0;
let requestedPage = 0;

let allPeople = [];
let allKinds = [];
let allTags = [];
let currentFsPath = "";
let currentFsQuery = "";
let currentFsFilterStatus = "all";
let catalogViewMode = "grid";
let catalogSearchQuery = "";
let catalogReviewFilter = "all";
let catalogKindFilter = "all";
let catalogGroupMode = "none";
let catalogPeopleFilter = [];
let catalogPersonMatch = "any";

// Record editor local state
let currentDocTags = [];
let currentLinkedPeople = [];
let saveTimer = null;
let savingNow = false;
let dirtySinceSave = false;

// Panzoom & Viewer State
let scale = 1.0;
let panX = 0;
let panY = 0;
let cachedFitScale = 1.0;
let isMouseDown = false;
let hasDragged = false;
let mouseDownX = 0;
let mouseDownY = 0;
let startX = 0;
let startY = 0;
let smoothTransformTimer = null;

// ============================================================================
// Initialization
// ============================================================================
document.addEventListener("DOMContentLoaded", async () => {
  setupNavigationRouter();
  setupPanzoom();
  setupFilterControls();
  setupDiacriticPalette();
  setupRecordEditor();
  setupCurationResizer();
  setupModals();
  setupKeyboardShortcuts();
  setupSettingsView();
  setupSelection();
  setupActivityPanel();
  setupPeopleView();
  setupArchiveFile();
  setupImportWizard();
  setupPagesReview();

  // Where the curator was. The server keeps it, so it survives a restart and
  // comes back with an opened .archivesys file.
  const session = await loadSession();
  applySessionBeforeLoad(session);

  await Promise.all([
    loadPeople(),
    loadKinds(),
    loadTags(),
    loadDocumentArchive(session.doc_id)
  ]);

  applySessionAfterLoad();
  loadDirectory(currentFsPath);
  restoreRequestedView(session);
  loadDuplicates();
  startSessionTracking();
  announceOpenedArchive();
});

// ============================================================================
// Primary Navigation Router
// ============================================================================
function setupNavigationRouter() {
  const navItems = document.querySelectorAll(".nav-item");
  navItems.forEach(btn => {
    btn.addEventListener("click", () => {
      const viewId = btn.getAttribute("data-view");
      switchView(viewId);
    });
  });

  // Reflect the active view in the URL hash so a view can be linked, bookmarked
  // and survive a reload, rather than always landing back on the explorer.
  // The initial view is resolved in restoreRequestedView(), after the archive
  // has loaded — a deep link to the catalog must not render before it has data.
  window.addEventListener("hashchange", () => {
    const viewId = viewFromHash();
    if (viewId) switchView(viewId);
  });

  // Sidebar toggle & collapse
  const sidebar = document.getElementById("app-sidebar");
  const toggleBtn = document.getElementById("btn-sidebar-toggle");

  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => sidebar.classList.toggle("collapsed"));
  }

  // Recompile timeline button in header
  const btnRecompile = document.getElementById("btn-recompile-timeline");
  if (btnRecompile) {
    btnRecompile.addEventListener("click", async () => {
      btnRecompile.textContent = "Compiling...";
      btnRecompile.disabled = true;
      try {
        const res = await fetch("/api/timeline/recompile", { method: "POST" });
        const result = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(result.detail || res.statusText);
        showToast(`Timeline exported with ${result.quality} scans to ${result.path}`);
        reloadTimelineFrame();
      } catch (err) {
        console.error("Failed to recompile timeline:", err);
        showToast(`Could not export the timeline: ${err.message}`, "error");
      } finally {
        setTimeout(() => {
          btnRecompile.textContent = "Recompile Timeline";
          btnRecompile.disabled = false;
        }, 800);
      }
    });
  }

  // Refresh timeline iframe button
  const btnRefreshTimeline = document.getElementById("btn-refresh-timeline-frame");
  if (btnRefreshTimeline) {
    btnRefreshTimeline.addEventListener("click", reloadTimelineFrame);
  }

  // The cover comes down only once the fresh page has actually arrived.
  const timelineFrame = document.getElementById("timeline-iframe");
  timelineFrame?.addEventListener("load", () => {
    const cover = document.getElementById("timeline-loading");
    if (cover && timelineFrame.getAttribute("src") !== "about:blank") cover.hidden = true;
  });
}

/**
 * Reload the Timeline Preview, covering it until the fresh page arrives.
 * The server recompiles the whole timeline for every load; until it answers,
 * the old page would stay clickable -- and an inspector opened on it would
 * vanish mid-edit when the new page replaced it.
 */
function reloadTimelineFrame() {
  const iframe = document.getElementById("timeline-iframe");
  if (!iframe) return;
  const cover = document.getElementById("timeline-loading");
  if (cover) cover.hidden = false;
  iframe.src = "/timeline?t=" + Date.now();
}

function viewFromHash() {
  const params = new URLSearchParams(window.location.search);
  const key = ((window.location.hash || "").replace(/^#/, "") || params.get("view") || "").trim();
  if (!key) return null;
  const id = key.startsWith("view-") ? key : `view-${key}`;
  return document.getElementById(id) ? id : null;
}

function restoreRequestedView(session = {}) {
  // A link or bookmark with a #view wins; otherwise go back to the saved one.
  const saved = typeof session.view === "string" ? `view-${session.view}` : null;
  const savedIsView = saved && document.getElementById(saved)?.classList.contains("studio-view");
  const viewId = viewFromHash() || (savedIsView ? saved : null);
  if (!viewId) return;
  if (viewId === "view-person") {
    // The person view shows one person; without one it is an empty page.
    if (Number.isInteger(session.person_id)) openPerson(session.person_id);
    else switchView("view-people");
    return;
  }
  if (viewId !== "view-explorer") switchView(viewId);
}

function switchView(viewId) {
  const hashKey = viewId.replace(/^view-/, "");
  if ((window.location.hash || "").replace(/^#/, "") !== hashKey) {
    history.replaceState(null, "", `#${hashKey}`);
  }

  // Update sidebar active state
  document.querySelectorAll(".nav-item").forEach(item => {
    if (item.getAttribute("data-view") === viewId) {
      item.classList.add("active");
    } else {
      item.classList.remove("active");
    }
  });

  // Switch view containers
  document.querySelectorAll(".studio-view").forEach(view => {
    if (view.id === viewId) {
      view.classList.add("active");
    } else {
      view.classList.remove("active");
    }
  });

  // View specific refreshes
  if (viewId === "view-settings") {
    loadSettingsView();
  } else if (viewId === "view-database") {
    renderDatabaseCatalog();
  } else if (viewId === "view-timeline") {
    reloadTimelineFrame();
  } else if (viewId === "view-curation") {
    // A saved zoom can only be applied once the viewer has a size.
    requestAnimationFrame(applyPendingViewerRestore);
  }
  persistSession();
}

// ============================================================================
// Data Loaders
// ============================================================================
async function loadDocumentArchive(preferredDocId = null) {
  try {
    const res = await fetch("/api/documents");
    if (!res.ok) throw new Error("Failed to load documents");
    documents = await res.json();
    updateHeaderStats();
    renderDatabaseCatalog();
    loadPageProposals();

    // The list may have changed around the open record (pages combined, a page
    // separated): keep Prev and Next counting from where it now stands.
    if (currentDoc) {
      const idx = documents.findIndex(d => d.id === currentDoc.id);
      if (idx !== -1) currentDocIndex = idx;
    }

    if (documents.length > 0 && currentDocIndex === -1) {
      const preferred = documents.findIndex(d => d.id === preferredDocId);
      loadDocumentForCuration(preferred !== -1 ? preferred : 0, false);
    }
  } catch (err) {
    console.error("Archive load error:", err);
  }
}

// The kind vocabulary is served, not duplicated. v1 kept three separate copies
// in JavaScript -- a dropdown, a label map and an OCR-skip list -- and they
// disagreed with each other and with the database.
async function loadKinds() {
  try {
    const res = await fetch("/api/kinds");
    if (res.ok) {
      allKinds = await res.json();
      renderKindChips();
      renderKindPills();
    }
  } catch (err) {
    console.error("Failed to load kinds:", err);
  }
}

async function loadTags() {
  try {
    const res = await fetch("/api/tags");
    if (res.ok) {
      allTags = await res.json();
      const list = document.getElementById("tag-suggestions");
      if (list) {
        list.innerHTML = "";
        allTags.forEach(t => {
          const opt = document.createElement("option");
          opt.value = t.tag;
          list.appendChild(opt);
        });
      }
    }
  } catch (err) {
    console.error("Failed to load tags:", err);
  }
}

function kindMeta(key) {
  return allKinds.find(k => k.key === key) || { key: key, label: key, hint: "", textual: true };
}

function updateHeaderStats() {
  const reviewed = documents.filter(d => d.reviewed).length;
  const onTimeline = documents.filter(d => d.on_timeline).length;
  const statsEl = document.getElementById("header-stats");
  if (statsEl) {
    statsEl.textContent =
      `${documents.length} documents · ${documents.length - reviewed} to review · ${onTimeline} on the timeline`;
  }
  const dbCountEl = document.getElementById("nav-db-count");
  if (dbCountEl) dbCountEl.textContent = String(documents.length);
}

function populatePeoplePicker() {
  const picker = document.getElementById("person-picker");
  if (!picker) return;
  picker.innerHTML = '<option value="">Add a person…</option>';
  allPeople.forEach(p => {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = `${p.first_name} ${p.last_name}` + (p.birth_edtf ? ` (${p.birth_edtf})` : "");
    picker.appendChild(opt);
  });
}

function renderKindPills() {
  const row = document.getElementById("db-kind-pills");
  if (!row) return;
  row.innerHTML = '<button class="pill active" data-kind="all">All</button>';
  allKinds.forEach(k => {
    const pill = document.createElement("button");
    pill.className = "pill";
    pill.setAttribute("data-kind", k.key);
    pill.textContent = k.label;
    row.appendChild(pill);
  });
}

function kindLabel(key) {
  return kindMeta(key).label;
}

// Renders one page of scan cards. Called on directory load and again for each
// "Load more" page, appending rather than rebuilding so existing cards persist.
function renderScanCards(data, fsFiles, fsFilesCount, appending) {
  const filesList = data.files || [];
  const total = data.total_files != null ? data.total_files : filesList.length;

  document.getElementById("fs-load-more-row")?.remove();

  if (!appending && filesList.length === 0) {
    fsFiles.innerHTML = '<span class="counter-label">No image or document scan files found in this directory.</span>';
    if (fsFilesCount) fsFilesCount.textContent = "0 scans";
    return;
  }

  filesList.forEach(file => {
    const card = document.createElement("div");
    card.className = "file-card";

    const sizeKb = (file.size_bytes / 1024).toFixed(0);
    const sizeMb = (file.size_bytes / (1024 * 1024)).toFixed(1);
    const sizeDisplay = file.size_bytes > 1024 * 1024 ? `${sizeMb} MB` : `${sizeKb} KB`;

    const isIndexed = file.is_indexed;
    const statusLabel = isIndexed
      ? `Indexed as #${file.doc_id}${file.reviewed ? " — reviewed" : " — to review"}`
      : "Not yet indexed";

    // Only the indexed state is badged. When almost every file is unindexed, an
    // "UNINDEXED" chip on every card is noise carrying no information; the
    // absence of a badge says the same thing more quietly.
    const badge = isIndexed
      ? `<span class="file-card-status-badge indexed" title="${statusLabel}">#${file.doc_id}</span>`
      : "";

    card.dataset.path = file.path;
    card.innerHTML = `
      <div class="file-card-thumb-wrap">
        <img src="/api/thumb?path=${encodeURIComponent(file.path)}" alt="" loading="lazy"
             onerror="this.closest('.file-card-thumb-wrap').classList.add('thumb-failed');">
        ${badge}
        <label class="select-box" title="Select (shift-click for a range)">
          <input type="checkbox" class="select-box-input" data-path="${file.path}">
          <span class="select-box-mark" aria-hidden="true">
            <svg viewBox="0 0 20 20"><polyline points="4.5,10.5 8.5,14.5 15.5,5.5"/></svg>
          </span>
        </label>
        <div class="file-card-hover">
          ${isIndexed
            ? `<button class="btn btn-primary btn-sm btn-open-curation" data-doc-id="${file.doc_id}">Open</button>`
            : `<button class="btn btn-primary btn-sm btn-ingest-file" data-file-path="${file.path}" title="Index scan into archive">Index</button>`}
        </div>
      </div>
      <div class="file-card-info">
        <span class="file-card-title" title="${file.name}">${file.name}</span>
        <span class="file-card-meta">${sizeDisplay}</span>
      </div>
    `;

    card.querySelector(".btn-open-curation")?.addEventListener("click", (e) => {
      e.stopPropagation();
      openDocInCuration(file.doc_id, file.page_no || 0);
    });
    card.querySelector(".btn-quick-index-file")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      await ingestFileFromDisk(file.path, e.currentTarget);
    });
    card.querySelector(".btn-ingest-file")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      await ingestFileFromDisk(file.path, e.currentTarget);
    });

    const box = card.querySelector(".select-box-input");
    box.checked = selectedPaths.has(file.path);
    const selectBox = card.querySelector(".select-box");
    // Prevent mousedown from initiating browser text selection when clicking checkbox
    selectBox?.addEventListener("mousedown", (e) => {
      e.preventDefault();
    });
    selectBox?.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      window.getSelection()?.removeAllRanges();
      toggleSelection(file.path, e.shiftKey);
    });

    // Also support clicking / shift-clicking the card directly
    card.addEventListener("mousedown", (e) => {
      if (e.shiftKey) {
        e.preventDefault(); // Intercept browser text selection immediately on mousedown
      }
    });
    card.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, .select-box")) return;
      if (e.shiftKey) {
        e.preventDefault();
        window.getSelection()?.removeAllRanges();
        toggleSelection(file.path, true);
      } else if (selectedPaths.size > 0) {
        e.preventDefault();
        toggleSelection(file.path, false);
      }
    });

    fsFiles.appendChild(card);
  });

  const shown = fsFiles.querySelectorAll(".file-card").length;
  if (fsFilesCount) {
    fsFilesCount.textContent = shown < total ? `${shown} of ${total} scans` : `${total} scans`;
  }

  if (data.has_more) {
    const row = document.createElement("div");
    row.id = "fs-load-more-row";
    row.className = "load-more-row";
    row.innerHTML = `<button class="btn btn-secondary" id="btn-fs-load-more">
        Load ${Math.min(data.limit, total - shown)} more of ${total - shown} remaining</button>`;
    fsFiles.after(row);
    row.querySelector("button").addEventListener("click", (e) => {
      e.currentTarget.disabled = true;
      e.currentTarget.textContent = "Loading...";
      loadDirectory(currentFsPath, shown, currentFsQuery, currentFsFilterStatus);
    });
  }
}

// ============================================================================
// VIEW 1: Local Scan Explorer
// ============================================================================
async function loadDirectory(path, offset = 0, query = currentFsQuery, filterStatus = currentFsFilterStatus) {
  const fsDirectories = document.getElementById("fs-directories");
  const fsFiles = document.getElementById("fs-files");
  const fsFilesCount = document.getElementById("fs-files-count");
  const fsPathInput = document.getElementById("fs-path-input");
  const bookmarksRow = document.getElementById("quick-bookmarks");

  // offset > 0 means "load more": append to the existing grid rather than
  // rebuilding it, so the cards already on screen do not flash.
  const appending = offset > 0;
  if (!appending) {
    fsDirectories.innerHTML = '<span class="counter-label">Scanning directory...</span>';
    fsFiles.innerHTML = "";
  }

  try {
    const params = new URLSearchParams();
    if (path) params.set("path", path);
    if (offset) params.set("offset", String(offset));
    if (query) params.set("query", query);
    if (filterStatus && filterStatus !== "all") params.set("filter_status", filterStatus);
    const res = await fetch(`/api/fs/browse?${params.toString()}`);
    const data = await res.json();

    currentFsPath = data.current_path;
    if (fsPathInput) fsPathInput.value = currentFsPath;
    persistSession();

    if (appending) {
      renderScanCards(data, fsFiles, fsFilesCount, true);
      return;
    }

    // Render Quick Bookmarks
    if (bookmarksRow && data.quick_bookmarks) {
      bookmarksRow.innerHTML = '<span class="bookmarks-label">Quick Jump:</span>';
      data.quick_bookmarks.forEach(bm => {
        const btn = document.createElement("button");
        btn.className = "bookmark-pill";
        btn.textContent = bm.name;
        btn.addEventListener("click", () => loadDirectory(bm.path));
        bookmarksRow.appendChild(btn);
      });
    }

    // Render Breadcrumbs
    renderBreadcrumbs(currentFsPath);

    // Render Subdirectories
    fsDirectories.innerHTML = "";
    if (!data.directories || data.directories.length === 0) {
      fsDirectories.innerHTML = '<span class="counter-label">No subfolders in this directory.</span>';
    } else {
      data.directories.forEach(dir => {
        const card = document.createElement("div");
        card.className = "dir-card";
        card.innerHTML = `
          <span class="dir-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></span>
          <span class="dir-name" title="${dir.name}">${dir.name}</span>
        `;
        card.addEventListener("click", () => loadDirectory(dir.path));
        fsDirectories.appendChild(card);
      });
    }

    // Render Scan Files
    renderScanCards(data, fsFiles, fsFilesCount, false);

    // Up parent directory button
    const btnUp = document.getElementById("btn-fs-up");
    if (btnUp) {
      if (data.parent_path) {
        btnUp.disabled = false;
        btnUp.onclick = () => loadDirectory(data.parent_path);
      } else {
        btnUp.disabled = true;
      }
    }

  } catch (err) {
    fsDirectories.innerHTML = `<span class="counter-label" style="color: var(--accent-red);">Error reading directory: ${err}</span>`;
  }
}

function renderBreadcrumbs(fullPath) {
  const container = document.getElementById("fs-breadcrumbs");
  if (!container) return;
  container.innerHTML = "";

  const parts = fullPath.split("/").filter(Boolean);
  let acc = "";

  // Root crumb
  const rootCrumb = document.createElement("span");
  rootCrumb.className = "crumb-link";
  rootCrumb.textContent = "/";
  rootCrumb.addEventListener("click", () => loadDirectory("/"));
  container.appendChild(rootCrumb);

  parts.forEach((part, idx) => {
    acc += "/" + part;
    const currentAcc = acc;

    const sep = document.createElement("span");
    sep.className = "crumb-sep";
    sep.textContent = ">";
    container.appendChild(sep);

    const crumb = document.createElement("span");
    if (idx === parts.length - 1) {
      crumb.className = "crumb-current";
      crumb.textContent = part;
    } else {
      crumb.className = "crumb-link";
      crumb.textContent = part;
      crumb.addEventListener("click", () => loadDirectory(currentAcc));
    }
    container.appendChild(crumb);
  });
}

// Index file from filesystem explorer into archive (Instant index without requiring OCR)
async function ingestFileFromDisk(filePath, buttonElement) {
  if (buttonElement) {
    buttonElement.disabled = true;
    buttonElement.textContent = "Indexing...";
  }
  try {
    const res = await fetch("/api/documents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: [filePath] }),
    });
    if (res.ok) {
      const body = await res.json();
      const data = body.indexed && body.indexed[0];
      if (!data) throw new Error((body.skipped && body.skipped[0]?.reason) || "Index failed");
      await loadDocumentArchive();
      updateHeaderStats();
      const card = document.querySelector(`.file-card[data-path="${CSS.escape(filePath)}"]`);
      if (card) {
        card.querySelector(".file-card-hover").innerHTML = `
          <button class="btn btn-primary btn-sm btn-open-curation" data-doc-id="${data.doc_id}">Open</button>
        `;
        card.querySelector(".btn-open-curation")?.addEventListener("click", (e) => {
          e.stopPropagation();
          openDocInCuration(data.doc_id);
        });
        const existingBadge = card.querySelector(".file-card-status-badge");
        if (existingBadge) {
          existingBadge.textContent = `#${data.doc_id}`;
          existingBadge.className = "file-card-status-badge indexed";
        } else {
          const thumbWrap = card.querySelector(".file-card-thumb-wrap");
          const badge = document.createElement("span");
          badge.className = "file-card-status-badge indexed";
          badge.textContent = `#${data.doc_id}`;
          thumbWrap.appendChild(badge);
        }
      }
    } else {
      const err = await res.json().catch(() => ({ detail: "Index failed" }));
      console.error("Index failed:", err);
      if (buttonElement) {
        buttonElement.disabled = false;
        buttonElement.textContent = "Error";
      }
    }
  } catch (err) {
    console.error("Failed to index file:", err);
    if (buttonElement) {
      buttonElement.disabled = false;
      buttonElement.textContent = "Error";
    }
  }
}

// Setup Explorer navigation and in-folder search/filter controls
function setupExplorerControls() {
  const pathInput = document.getElementById("fs-path-input");
  const btnGo = document.getElementById("btn-fs-go");
  const btnRefresh = document.getElementById("btn-fs-refresh");
  const searchInput = document.getElementById("fs-search-input");
  const btnClear = document.getElementById("btn-fs-clear-search");
  const statusPills = document.getElementById("fs-status-pills");

  if (btnGo && pathInput) {
    btnGo.addEventListener("click", () => {
      if (pathInput.value.trim()) loadDirectory(pathInput.value.trim());
    });
    pathInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && pathInput.value.trim()) {
        loadDirectory(pathInput.value.trim());
      }
    });
  }

  if (btnRefresh) {
    btnRefresh.addEventListener("click", () => loadDirectory(currentFsPath));
  }

  let searchTimer = null;
  if (searchInput) {
    searchInput.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        currentFsQuery = searchInput.value.trim();
        loadDirectory(currentFsPath, 0, currentFsQuery, currentFsFilterStatus);
      }, 250);
    });
  }

  if (btnClear && searchInput) {
    btnClear.addEventListener("click", () => {
      searchInput.value = "";
      currentFsQuery = "";
      loadDirectory(currentFsPath, 0, "", currentFsFilterStatus);
    });
  }

  if (statusPills) {
    statusPills.addEventListener("click", (e) => {
      const pill = e.target.closest(".pill");
      if (!pill) return;
      statusPills.querySelectorAll(".pill").forEach(p => p.classList.remove("active"));
      pill.classList.add("active");
      currentFsFilterStatus = pill.getAttribute("data-filter") || "all";
      loadDirectory(currentFsPath, 0, currentFsQuery, currentFsFilterStatus);
    });
  }
}

document.addEventListener("DOMContentLoaded", setupExplorerControls);

// ============================================================================
// VIEW 2: Indexed Database Catalog
// ============================================================================
function setupCatalogControls() {
  const btnGrid = document.getElementById("btn-mode-grid");
  const btnList = document.getElementById("btn-mode-list");
  const searchInput = document.getElementById("db-search");
  const btnClear = document.getElementById("btn-db-clear-search");
  const statusPills = document.getElementById("db-review-pills");
  const catPills = document.getElementById("db-kind-pills");

  if (btnGrid && btnList) {
    btnGrid.addEventListener("click", () => {
      catalogViewMode = "grid";
      btnGrid.classList.add("active");
      btnList.classList.remove("active");
      document.getElementById("db-grid-container").style.display = "grid";
      document.getElementById("db-table-container").style.display = "none";
    });

    btnList.addEventListener("click", () => {
      catalogViewMode = "list";
      btnList.classList.add("active");
      btnGrid.classList.remove("active");
      document.getElementById("db-grid-container").style.display = "none";
      document.getElementById("db-table-container").style.display = "block";
    });
  }

  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      catalogSearchQuery = e.target.value.trim().toLowerCase();
      if (btnClear) btnClear.style.display = catalogSearchQuery ? "block" : "none";
      renderDatabaseCatalog();
    });
  }

  if (btnClear && searchInput) {
    btnClear.addEventListener("click", () => {
      searchInput.value = "";
      catalogSearchQuery = "";
      btnClear.style.display = "none";
      renderDatabaseCatalog();
      searchInput.focus();
    });
  }

  if (statusPills) {
    statusPills.addEventListener("click", (e) => {
      const pill = e.target.closest(".pill");
      if (!pill) return;
      statusPills.querySelectorAll(".pill").forEach(p => p.classList.remove("active"));
      pill.classList.add("active");
      catalogReviewFilter = pill.getAttribute("data-review");
      renderDatabaseCatalog();
    });
  }

  if (catPills) {
    catPills.addEventListener("click", (e) => {
      const pill = e.target.closest(".pill");
      if (!pill) return;
      catPills.querySelectorAll(".pill").forEach(p => p.classList.remove("active"));
      pill.classList.add("active");
      catalogKindFilter = pill.getAttribute("data-kind");
      renderDatabaseCatalog();
    });
  }
}
document.addEventListener("DOMContentLoaded", setupCatalogControls);

/** Sorted on the way in, but the detector could not tell text from photograph. */
function isUndecided(doc) {
  return !doc.reviewed && doc.metadata?.sorting?.verdict === "unsure";
}

function getFilteredDatabaseDocs() {
  const docs = documents.filter(doc => {
    if (catalogReviewFilter === "todo" && doc.reviewed) return false;
    if (catalogReviewFilter === "done" && !doc.reviewed) return false;
    if (catalogKindFilter !== "all" && doc.kind !== catalogKindFilter) return false;

    if (catalogPeopleFilter.length) {
      const ids = (doc.people || []).map(p => p.id);
      const hit = catalogPersonMatch === "all"
        ? catalogPeopleFilter.every(id => ids.includes(id))
        : catalogPeopleFilter.some(id => ids.includes(id));
      if (!hit) return false;
    }

    if (catalogSearchQuery) {
      const haystack = [
        doc.file_path, doc.title, doc.transcription, doc.location, doc.summary,
        (doc.tags || []).join(" "),
        (doc.people || []).map(p => `${p.first_name} ${p.last_name}`).join(" ")
      ].join(" ").toLowerCase();
      if (!haystack.includes(catalogSearchQuery) && String(doc.id) !== catalogSearchQuery) return false;
    }
    return true;
  });
  // Undecided scans wait first in "To review" rather than among hundreds.
  if (catalogReviewFilter === "todo") docs.sort((a, b) => isUndecided(b) - isUndecided(a));
  return docs;
}

async function deleteDocumentItem(docId, docTitle, e) {
  if (e) {
    e.stopPropagation();
    e.preventDefault();
  }
  const pages = (documents.find(d => d.id === docId)?.page_count) || 1;
  const confirmed = confirm(`Are you sure you want to delete document #${docId} ("${docTitle}")?\n`
    + `This will permanently delete the record${pages > 1 ? ` and its ${pages} pages` : ""}, OCR text, and linked timeline events. `
    + "The scan files themselves are not touched.");
  if (!confirmed) return;

  try {
    const res = await fetch(`/api/documents/${docId}`, { method: "DELETE" });
    if (res.ok) {
      if (currentDoc && currentDoc.id === docId) {
        currentDoc = null;
        currentDocIndex = -1;
        const scanImg = document.getElementById("scan-image");
        if (scanImg) scanImg.removeAttribute("src");
        const emptyOverlay = document.getElementById("viewport-empty");
        if (emptyOverlay) emptyOverlay.style.display = "flex";
        const transText = document.getElementById("transcription-text");
        if (transText) transText.value = "";
        const filePath = document.getElementById("file-path-display");
        if (filePath) filePath.textContent = "No file loaded";
        const subLabel = document.getElementById("nav-active-doc-sub");
        if (subLabel) {
          subLabel.textContent = "None selected";
        }
      }
      await Promise.all([loadDocumentArchive(), loadPeople()]);
      // Deleted from a person's page: that page loses the card too.
      if (currentPerson && document.getElementById("view-person")?.classList.contains("active")) {
        await openPerson(currentPerson.id);
      }
    } else {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Failed to delete document.");
    }
  } catch (err) {
    console.error("Delete document error:", err);
    alert("Network error while deleting document.");
  }
}

/** One catalog card. Shared by the archive grid, groups, and a person's page. */
function buildCatalogCard(doc) {
  const docTitle = doc.title || doc.file_path.split("/").pop();
  const docSnippet = (doc.summary || doc.transcription || "").substring(0, 140);
  const docDate = doc.date_display || "";
  const reviewMark = doc.reviewed
    ? '<span class="review-chip is-done">Reviewed</span>'
    : '<span class="review-chip is-todo">To review</span>';
  const timelineMark = (doc.excluded
    ? '<span class="timeline-chip is-off" title="Held back from the timeline">Held back</span>'
    : (doc.edtf_date ? "" : '<span class="timeline-chip is-undated" title="No date, so not on the timeline">Undated</span>'))
    + (isUndecided(doc) ? '<span class="timeline-chip is-undecided" title="Not clearly text or a photograph: choose its kind">Kind?</span>' : "");

  const card = document.createElement("div");
  card.className = "doc-catalog-card";
  card.innerHTML = `
    <div class="doc-catalog-thumb-wrap">
      <img src="/api/thumb?doc_id=${doc.id}" alt="${escapeHtml(docTitle)}" loading="lazy">
      <span class="card-review-mark">${reviewMark}</span>
      ${doc.page_count > 1 ? `<span class="page-count-chip">${doc.page_count} pages</span>` : ""}
    </div>
    <div class="doc-catalog-content">
      <span class="doc-catalog-title">${escapeHtml(docTitle)}</span>
      <div class="doc-catalog-tags">
        <span class="badge-type">${escapeHtml(kindLabel(doc.kind))}</span>
        <span class="badge-lang">${formatLanguageBadge(doc.language)}</span>
        ${docDate ? `<span class="field-hint">${escapeHtml(docDate)}</span>` : ""}
        ${timelineMark}
      </div>
      ${renderCardPeople(doc)}
      <p class="doc-catalog-snippet">${escapeHtml(docSnippet) || "Nothing written down yet."}</p>
      <div class="doc-catalog-actions">
        <span class="field-hint">#${doc.id}</span>
        <button class="btn-card-delete" title="Delete document #${doc.id}" aria-label="Delete document #${doc.id}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
          </svg>
        </button>
      </div>
    </div>`;
  card.querySelector(".btn-card-delete")?.addEventListener("click", (e) =>
    deleteDocumentItem(doc.id, docTitle, e));
  card.addEventListener("click", () => openDocInCuration(doc.id));
  return card;
}

/** Who is in it, on the card. The whole point of the person axis is that this
    is visible without opening the record. */
function renderCardPeople(doc) {
  const people = doc.people || [];
  if (!people.length) return "";
  const shown = people.slice(0, 3).map(p =>
    `<span class="card-person">${escapeHtml(p.first_name)} ${escapeHtml(p.last_name)}</span>`).join("");
  const rest = people.length > 3 ? `<span class="card-person is-more">+${people.length - 3}</span>` : "";
  return `<div class="doc-catalog-people">${shown}${rest}</div>`;
}

function buildCatalogRow(doc) {
  const docTitle = doc.title || doc.file_path.split("/").pop();
  const docDate = doc.date_display || "";
  const reviewMark = doc.reviewed
    ? '<span class="review-chip is-done">Reviewed</span>'
    : '<span class="review-chip is-todo">To review</span>';
  const timelineMark = (doc.excluded
    ? '<span class="timeline-chip is-off">Held back</span>'
    : (doc.edtf_date ? "" : '<span class="timeline-chip is-undated">Undated</span>'))
    + (isUndecided(doc) ? ' <span class="timeline-chip is-undecided">Kind?</span>' : "");

  const tr = document.createElement("tr");
  tr.className = "clickable-row";
  tr.innerHTML = `
    <td><strong>#${doc.id}</strong></td>
    <td><div class="table-thumb"><img src="/api/thumb?doc_id=${doc.id}" alt="" loading="lazy"></div></td>
    <td>
      <strong>${escapeHtml(docTitle)}</strong><br>
      <span class="field-hint">${escapeHtml(doc.file_path.split("/").pop())}</span>
    </td>
    <td>${escapeHtml(docDate)}${docDate && timelineMark ? " " : ""}${timelineMark}</td>
    <td><span class="badge-type">${escapeHtml(kindLabel(doc.kind))}</span></td>
    <td><span class="badge-lang">${formatLanguageBadge(doc.language)}</span></td>
    <td>${reviewMark}</td>
    <td style="text-align: center;">
      <button class="btn-row-delete" title="Delete document #${doc.id}" aria-label="Delete document #${doc.id}">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3 6 5 6 21 6"></polyline>
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
        </svg>
      </button>
    </td>`;
  tr.querySelector(".btn-row-delete")?.addEventListener("click", (e) =>
    deleteDocumentItem(doc.id, docTitle, e));
  tr.addEventListener("click", () => openDocInCuration(doc.id));
  return tr;
}

/**
 * Split the catalog into groups.
 *
 * By person, a document appears once under EVERY person it involves. That is
 * the many-to-many relation showing through, and it is what makes the view
 * answer "what do we have about them" rather than "list each file once".
 * The final group is the one that matters most day to day: the documents
 * nobody has been linked to yet, which is the work queue.
 */
function groupDocuments(docs, mode) {
  if (mode === "none") return [{ key: "all", label: null, docs }];

  const groups = new Map();
  const push = (key, label, doc, sortKey) => {
    if (!groups.has(key)) groups.set(key, { key, label, docs: [], sortKey });
    groups.get(key).docs.push(doc);
  };

  if (mode === "person") {
    const orphans = [];
    docs.forEach(doc => {
      const people = doc.people || [];
      if (!people.length) { orphans.push(doc); return; }
      people.forEach(p => push(`p${p.id}`, `${p.first_name} ${p.last_name}`, doc,
        `${p.last_name} ${p.first_name}`.toLowerCase()));
    });
    const out = [...groups.values()].sort((a, b) => a.sortKey.localeCompare(b.sortKey));
    if (orphans.length) {
      out.push({ key: "nobody", label: "Nobody linked yet", docs: orphans, isQueue: true });
    }
    return out;
  }

  if (mode === "kind") {
    docs.forEach(doc => push(doc.kind || "document", kindLabel(doc.kind), doc, kindLabel(doc.kind)));
    return [...groups.values()].sort((a, b) => a.sortKey.localeCompare(b.sortKey));
  }

  if (mode === "decade") {
    docs.forEach(doc => {
      if (!doc.sort_start) { push("undated", "Undated", doc, "zzzz"); return; }
      const decade = Math.floor(doc.sort_start / 10) * 10;
      push(`d${decade}`, `${decade}s`, doc, String(decade));
    });
    return [...groups.values()].sort((a, b) => a.sortKey.localeCompare(b.sortKey));
  }

  return [{ key: "all", label: null, docs }];
}

function describeActiveFilters() {
  const bits = [];
  if (catalogReviewFilter === "todo") bits.push("still to review");
  if (catalogReviewFilter === "done") bits.push("already reviewed");
  if (catalogKindFilter !== "all") bits.push(kindLabel(catalogKindFilter).toLowerCase());
  if (catalogPeopleFilter.length) {
    const names = catalogPeopleFilter
      .map(id => allPeopleRich.find(p => p.id === id))
      .filter(Boolean)
      .map(p => `${p.first_name} ${p.last_name}`);
    bits.push(`${catalogPersonMatch === "all" ? "all of" : "any of"} ${names.join(", ")}`);
  }
  if (catalogSearchQuery) bits.push(`matching “${catalogSearchQuery}”`);
  return bits;
}

function renderDatabaseCatalog() {
  const filtered = getFilteredDatabaseDocs();
  const gridContainer = document.getElementById("db-grid-container");
  const tableBody = document.getElementById("db-table-body");
  if (!gridContainer || !tableBody) return;

  gridContainer.innerHTML = "";
  tableBody.innerHTML = "";

  if (filtered.length === 0) {
    // Name the filters responsible and offer the way out, rather than the
    // unhelpfully generic "no documents match the active filter criteria".
    const bits = describeActiveFilters();
    const why = bits.length
      ? `No documents are ${bits.join(", and ")}.`
      : "The archive is empty. Index some scans from the Scan Explorer.";
    gridContainer.innerHTML = `
      <div class="empty-note">
        <p>${escapeHtml(why)}</p>
        ${bits.length ? '<button type="button" class="btn btn-secondary btn-sm" id="btn-clear-filters">Clear the filters</button>' : ""}
      </div>`;
    document.getElementById("btn-clear-filters")?.addEventListener("click", clearCatalogFilters);
    tableBody.innerHTML = `<tr><td colspan="8" class="empty-cell">${escapeHtml(why)}</td></tr>`;
    return;
  }

  groupDocuments(filtered, catalogGroupMode).forEach(group => {
    if (group.label !== null) {
      const header = document.createElement("div");
      header.className = "catalog-group-header" + (group.isQueue ? " is-queue" : "");
      const person = group.key.startsWith("p")
        ? allPeopleRich.find(p => `p${p.id}` === group.key)
        : null;
      header.innerHTML = `
        ${person ? `<span class="catalog-group-face">${personAvatar(person)}</span>` : ""}
        <span class="catalog-group-name">${escapeHtml(group.label)}</span>
        <span class="catalog-group-count">${group.docs.length}</span>
        ${group.isQueue ? '<span class="catalog-group-hint">linking people is what makes every other view work</span>' : ""}`;
      if (person) {
        header.classList.add("is-clickable");
        header.addEventListener("click", () => openPerson(person.id));
      }
      gridContainer.appendChild(header);
    }

    const shelf = document.createElement("div");
    shelf.className = "catalog-shelf";
    group.docs.forEach(doc => shelf.appendChild(buildCatalogCard(doc)));
    gridContainer.appendChild(shelf);
  });

  filtered.forEach(doc => tableBody.appendChild(buildCatalogRow(doc)));
}

function formatLanguageBadge(lang) {
  if (lang === "el-poly") return "GRC";
  if (lang === "el") return "ELL";
  if (lang === "fr") return "FRA";
  if (lang === "en") return "ENG";
  if (lang === "mixed") return "MIX";
  return (lang || "DOC").toUpperCase();
}

// ============================================================================
// Interactive Date Selection Box Component
// ============================================================================
// ============================================================================
// Reading dates the way a historian writes them
//
// v1 spent six controls on one date -- a native picker, an EDTF text box, a
// precision dropdown, a "circa" checkbox, Today and Clear -- and then needed a
// second copy of all six for the event. The precision dropdown and the circa
// checkbox were only ever restating what the typed string already said.
//
// One input. It accepts what an archivist would write on the folder, converts
// to EDTF, and reads its interpretation back so nothing is guessed silently.
// ============================================================================
// parseHistoricalDate() and its month tables live in dates.js, loaded before
// this file and shared with the timeline inspector.

/** Renders a date string back to the curator, EDTF included. */
function renderDateReading(raw) {
  const box = document.getElementById("date-reading");
  if (!box) return;
  const r = parseHistoricalDate(raw);

  if (r.empty) {
    box.className = "date-reading";
    box.innerHTML = '<span class="date-reading-empty">Undated — it will sit outside the timeline until you give it a date.</span>';
    return;
  }
  if (!r.ok) {
    box.className = "date-reading is-unread";
    box.innerHTML = `<span class="date-reading-text">Sorry — ${escapeHtml(r.reading)}.</span>`;
    return;
  }
  box.className = "date-reading is-read";
  box.innerHTML =
    `<span class="date-reading-text">${escapeHtml(r.reading)}</span>` +
    `<code class="date-reading-edtf" title="Extended Date/Time Format, as stored">${escapeHtml(r.edtf)}</code>`;
}

// ----------------------------------------------------------------------------
// Dates the page states about itself
//
// Historical records almost always carry their own date. Reading it off the
// transcription turns the slowest field in the form into one click, and it
// works across Katharevousa, French and English alike because all three write
// their years in Arabic numerals.
// ----------------------------------------------------------------------------
async function loadDateSuggestions(docId) {
  const box = document.getElementById("date-found");
  const list = document.getElementById("date-found-list");
  if (!box || !list) return;

  box.hidden = true;
  list.innerHTML = "";

  let data;
  try {
    const res = await fetch(`/api/documents/${docId}/date-suggestions`);
    if (!res.ok) return;
    data = await res.json();
  } catch { return; }

  // A different document may have been opened while this was in flight.
  if (!currentDoc || currentDoc.id !== docId) return;

  const current = (document.getElementById("doc-date")?.value || "").trim();
  const fresh = (data.suggestions || []).filter(s => s.edtf !== current);
  if (!fresh.length) return;

  fresh.slice(0, 3).forEach(s => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "date-found-chip" + (s.confidence >= 0.8 ? " is-strong" : "");
    chip.innerHTML =
      `<span class="dfc-date">${escapeHtml(s.display)}</span>` +
      `<span class="dfc-why">${escapeHtml(s.evidence)}</span>`;
    chip.title = s.source === "filename"
      ? `From the file name · ${s.edtf}`
      : `From the transcription · ${s.edtf}`;
    chip.addEventListener("click", () => {
      const input = document.getElementById("doc-date");
      if (!input) return;
      input.value = s.edtf;
      renderDateReading(input.value);
      updateProjectionReadout();
      scheduleSave();
      loadDateSuggestions(docId);
    });
    list.appendChild(chip);
  });

  box.hidden = false;
}

function currentDateEdtf() {
  const el = document.getElementById("doc-date");
  if (!el) return "";
  const r = parseHistoricalDate(el.value);
  return r.ok ? r.edtf : "";
}

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}


// ---------------------------------------------------------------------------
// Grouping and person-filter controls on the archive
// ---------------------------------------------------------------------------
function populatePersonFilterSelect() {
  const sel = document.getElementById("db-person-select");
  if (!sel) return;
  sel.innerHTML = '<option value="">Anyone</option>';
  allPeopleRich.forEach(p => {
    if (catalogPeopleFilter.includes(p.id)) return;
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = `${p.first_name} ${p.last_name}` + (p.doc_count ? ` (${p.doc_count})` : "");
    sel.appendChild(opt);
  });
}

function renderPersonFilterChips() {
  const box = document.getElementById("db-person-chips");
  const toggle = document.getElementById("db-person-match");
  if (!box) return;
  box.innerHTML = "";

  catalogPeopleFilter.forEach(id => {
    const person = allPeopleRich.find(p => p.id === id);
    if (!person) return;
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    chip.innerHTML = `${escapeHtml(person.first_name)} ${escapeHtml(person.last_name)}<button type="button" title="Remove">&times;</button>`;
    chip.querySelector("button").addEventListener("click", () => {
      catalogPeopleFilter = catalogPeopleFilter.filter(x => x !== id);
      populatePersonFilterSelect();
      renderPersonFilterChips();
      renderDatabaseCatalog();
    });
    box.appendChild(chip);
  });

  // "all of" only means something with two or more people selected.
  if (toggle) toggle.hidden = catalogPeopleFilter.length < 2;
}

function clearCatalogFilters() {
  catalogReviewFilter = "all";
  catalogKindFilter = "all";
  catalogPeopleFilter = [];
  catalogSearchQuery = "";
  const search = document.getElementById("db-search");
  if (search) search.value = "";
  document.querySelectorAll("#db-review-pills .pill").forEach(p =>
    p.classList.toggle("active", p.getAttribute("data-review") === "all"));
  document.querySelectorAll("#db-kind-pills .pill").forEach(p =>
    p.classList.toggle("active", p.getAttribute("data-kind") === "all"));
  populatePersonFilterSelect();
  renderPersonFilterChips();
  renderDatabaseCatalog();
}

function setupGroupingControls() {
  const groupPills = document.getElementById("db-group-pills");
  if (groupPills) {
    groupPills.addEventListener("click", (e) => {
      const pill = e.target.closest(".pill");
      if (!pill) return;
      groupPills.querySelectorAll(".pill").forEach(p => p.classList.remove("active"));
      pill.classList.add("active");
      catalogGroupMode = pill.getAttribute("data-group") || "none";
      // Grouping is a grid idea; the table stays flat and sortable.
      if (catalogGroupMode !== "none" && catalogViewMode !== "grid") {
        document.getElementById("btn-mode-grid")?.click();
      }
      renderDatabaseCatalog();
    });
  }

  const personSelect = document.getElementById("db-person-select");
  if (personSelect) {
    personSelect.addEventListener("change", () => {
      const id = parseInt(personSelect.value, 10);
      if (!Number.isNaN(id) && !catalogPeopleFilter.includes(id)) {
        catalogPeopleFilter.push(id);
        populatePersonFilterSelect();
        renderPersonFilterChips();
        renderDatabaseCatalog();
      }
      personSelect.value = "";
    });
  }

  const match = document.getElementById("db-person-match");
  if (match) {
    match.addEventListener("click", (e) => {
      const opt = e.target.closest(".match-opt");
      if (!opt) return;
      match.querySelectorAll(".match-opt").forEach(o => o.classList.remove("active"));
      opt.classList.add("active");
      catalogPersonMatch = opt.getAttribute("data-match") || "any";
      renderDatabaseCatalog();
    });
  }
}

document.addEventListener("DOMContentLoaded", setupGroupingControls);

// ============================================================================
// People
//
// People are many-to-many with documents, and both directions matter. A
// certificate involves a child, a father and a mother; a person recurs across
// certificates, photographs and letters spanning decades. Grouping the catalog
// by person therefore shows a document under each person it involves -- that
// repetition is the view working, not a bug in it.
// ============================================================================
let allPeopleRich = [];
let peopleSort = "name";
let peopleSearch = "";
let currentPerson = null;
let duplicateGroups = [];

async function loadPeople(sort = peopleSort) {
  peopleSort = sort;
  try {
    const res = await fetch(`/api/people?sort=${encodeURIComponent(sort)}`);
    if (!res.ok) return;
    allPeopleRich = await res.json();
    allPeople = allPeopleRich;
    populatePeoplePicker();
    populatePersonFilterSelect();
    renderPeopleGrid();

    const sub = document.getElementById("nav-people-count");
    if (sub) {
      const n = allPeopleRich.length;
      sub.textContent = n === 0 ? "Nobody yet" : n === 1 ? "1 person" : `${n} people`;
    }
  } catch (err) {
    console.error("Failed to load people:", err);
  }
}

// Dismissing the banner used to hide it for good, so a duplicate created a
// month later was never mentioned. Now each group is dismissed on its own: a
// new group, or a new member of an old one, raises the banner again.
const DUPES_DISMISSED_KEY = "studio_dupes_dismissed_groups";
const PAGES_DISMISSED_KEY = "studio_pages_dismissed_groups";

// ============================================================================
// Combining pages: records that were added one per page before pages were
// kept together. The notice shows only while there is something to combine.
// ============================================================================
let pageProposals = [];

function dismissedPageKeys() {
  try {
    const saved = JSON.parse(localStorage.getItem(PAGES_DISMISSED_KEY) || "[]");
    return new Set(Array.isArray(saved) ? saved : []);
  } catch {
    return new Set();
  }
}

async function loadPageProposals() {
  try {
    const res = await fetch("/api/pages/proposals");
    if (!res.ok) return;
    pageProposals = (await res.json()).proposals || [];
  } catch { return; }
  renderPagesBanner();
}

function renderPagesBanner() {
  const banner = document.getElementById("pages-banner");
  const text = document.getElementById("pages-banner-text");
  if (!banner || !text) return;
  // Scanner-numbered series ("Scan 1", "Scan 2") are offered in the review,
  // but never raise the notice on their own: they are usually separate scans.
  const dismissed = dismissedPageKeys();
  const named = pageProposals.filter(p => !p.counter && !dismissed.has(p.key));
  if (!named.length) { banner.hidden = true; return; }
  const records = named.reduce((n, p) => n + p.doc_ids.length, 0);
  const example = named[0].pages.slice(0, 2).map(p => `“${p.name}”`).join(", ");
  text.textContent = `${plural(named.length, "item is", "items are")} split across ${records} records, `
    + `one per page (such as ${example}). Combine each into one item with its pages?`;
  banner.hidden = false;
}

function openPagesReview() {
  const body = document.getElementById("pages-body");
  const named = pageProposals.filter(p => !p.counter);
  const counters = pageProposals.filter(p => p.counter);
  const row = (p, checked) => {
    const shown = p.pages.slice(0, 6);
    const more = p.pages.length - shown.length;
    return `
      <label class="pages-row">
        <input type="checkbox" class="pages-pick" value="${escapeHtml(p.key)}"${checked ? " checked" : ""}>
        <span class="pages-row-text">
          <span class="import-name">${escapeHtml(p.name)}</span>
          <span class="import-note">${escapeHtml(p.folder)} · ${plural(p.pages.length, p.counter ? "scan" : "page")}
            ${p.pages.some(x => x.described) ? " · details kept" : ""}</span>
        </span>
        <span class="pages-row-thumbs">${shown.map(x =>
          `<img src="/api/thumb?doc_id=${x.id}" alt="" title="${escapeHtml(x.name)}" loading="lazy">`).join("")}
          ${more > 0 ? `<span class="counter-label">+${more}</span>` : ""}</span>
      </label>`;
  };
  body.innerHTML = `
    <p class="archive-status">Each item below was added as one record per page. Combined, it becomes one
      record with its pages in order: described once, read page by page, and shown once on the timeline.
      Titles, dates, people and notes already written on any page are kept.</p>
    ${named.length ? `
      <div class="import-section">
        <div class="import-section-head"><strong>Named pages · ${named.length}</strong>
          <span><button type="button" class="btn-link-action" data-pick="all">All</button>
                <button type="button" class="btn-link-action" data-pick="none">None</button></span></div>
        <div class="import-list">${named.map(p => row(p, true)).join("")}</div>
      </div>` : ""}
    ${counters.length ? `
      <div class="import-section">
        <div class="import-section-head"><strong>Numbered by the scanner · ${counters.length}</strong>
          <span class="field-note">Usually separate scans; tick one only if it is a single item</span></div>
        <div class="import-list">${counters.map(p => row(p, false)).join("")}</div>
      </div>` : ""}`;
  body.querySelectorAll("[data-pick]").forEach(btn => btn.addEventListener("click", () => {
    btn.closest(".import-section").querySelectorAll(".pages-pick")
      .forEach(box => { box.checked = btn.dataset.pick === "all"; });
    updatePagesCount();
  }));
  body.onchange = updatePagesCount;
  updatePagesCount();
  document.getElementById("modal-pages").style.display = "flex";
}

function pickedPageProposals() {
  const keys = new Set([...document.querySelectorAll("#pages-body .pages-pick:checked")].map(b => b.value));
  return pageProposals.filter(p => keys.has(p.key));
}

function updatePagesCount() {
  const n = pickedPageProposals().length;
  const btn = document.getElementById("btn-pages-combine");
  btn.textContent = n ? `Combine ${plural(n, "item")}` : "Combine";
  btn.disabled = n === 0;
}

async function combinePickedPages() {
  const picked = pickedPageProposals();
  if (!picked.length) return;
  const btn = document.getElementById("btn-pages-combine");
  btn.disabled = true;
  btn.textContent = "Combining…";
  await flushPendingSave();
  try {
    const res = await fetch("/api/pages/combine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ groups: picked.map(p => p.doc_ids) }),
    });
    if (!res.ok) throw new Error(await responseError(res, "The pages could not be combined."));
    const result = await res.json();
    document.getElementById("modal-pages").style.display = "none";
    const records = picked.reduce((n, p) => n + p.doc_ids.length, 0);
    showToast(`Combined ${records} records into ${plural(result.combined.length, "item")}.`
      + (result.failed.length ? ` ${plural(result.failed.length, "item")} could not be combined.` : ""),
      result.failed.length ? "warn" : "done");
    // The record open in the studio may now be a page of an item: open the
    // item there, at that page, without leaving the view you are in.
    const openId = currentDoc?.id;
    const lead = picked.find(p => p.doc_ids.includes(openId));
    await loadDocumentArchive();
    const idx = lead ? documents.findIndex(d => d.id === lead.lead_id) : -1;
    if (idx !== -1) {
      requestedPage = Math.max(0, lead.doc_ids.indexOf(openId));
      loadDocumentForCuration(idx, false);
    }
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    btn.disabled = false;
    updatePagesCount();
  }
}

function setupPagesReview() {
  document.getElementById("btn-review-pages")?.addEventListener("click", openPagesReview);
  document.getElementById("btn-dismiss-pages")?.addEventListener("click", () => {
    const keys = dismissedPageKeys();
    pageProposals.forEach(p => keys.add(p.key));
    try { localStorage.setItem(PAGES_DISMISSED_KEY, JSON.stringify([...keys])); } catch { /* this visit only */ }
    renderPagesBanner();
  });
  const close = () => { document.getElementById("modal-pages").style.display = "none"; };
  document.getElementById("btn-close-pages")?.addEventListener("click", close);
  document.getElementById("btn-pages-cancel")?.addEventListener("click", close);
  document.getElementById("btn-pages-combine")?.addEventListener("click", combinePickedPages);
}

function duplicateGroupKey(group) {
  return `${group.reason}:${group.people.map(p => p.id).sort((a, b) => a - b).join(",")}`;
}

function dismissedDuplicateKeys() {
  try {
    const saved = JSON.parse(localStorage.getItem(DUPES_DISMISSED_KEY) || "[]");
    return new Set(Array.isArray(saved) ? saved : []);
  } catch {
    return new Set();
  }
}

function dismissDuplicateGroups(groups) {
  const keys = dismissedDuplicateKeys();
  groups.forEach(g => keys.add(duplicateGroupKey(g)));
  try { localStorage.setItem(DUPES_DISMISSED_KEY, JSON.stringify([...keys])); } catch { /* this visit only */ }
  duplicateGroups = duplicateGroups.filter(g => !keys.has(duplicateGroupKey(g)));
  renderDuplicateBanner();
}

async function loadDuplicates() {
  try { localStorage.removeItem("studio_dupes_dismissed"); } catch { /* the old all-or-nothing flag */ }
  try {
    const res = await fetch("/api/people/duplicates");
    if (!res.ok) return;
    const dismissed = dismissedDuplicateKeys();
    duplicateGroups = (await res.json()).filter(g => !dismissed.has(duplicateGroupKey(g)));
  } catch { return; }
  renderDuplicateBanner();
}

function renderDuplicateBanner() {
  const banner = document.getElementById("duplicate-banner");
  const text = document.getElementById("duplicate-banner-text");
  if (!banner || !text) return;
  if (!duplicateGroups.length) { banner.hidden = true; return; }

  const n = duplicateGroups.reduce((sum, g) => sum + g.people.length, 0);
  text.textContent = `${n} people may be the same as someone else — spelling variants split a person in two.`;
  banner.hidden = false;
}

/** Initials on a ground tinted from the name, so each person is consistent. */
function personAvatar(person, size) {
  const initials = `${(person.first_name || "?")[0]}${(person.last_name || "")[0] || ""}`.toUpperCase();
  const seed = (person.name_key || `${person.first_name}${person.last_name}`)
    .split("").reduce((a, c) => a + c.charCodeAt(0), 0);
  const hue = seed % 360;
  if (person.portrait) {
    return `<img class="person-portrait" src="/api/thumb?doc_id=${person.portrait}" alt="" loading="lazy">`;
  }
  return `<span class="person-initials" style="--avatar-hue:${hue}" aria-hidden="true">${escapeHtml(initials)}</span>`;
}

function lifespanLabel(person) {
  const b = person.birth_edtf, d = person.death_edtf;
  if (b && d) return `${b} – ${d}`;
  if (b) return `b. ${b}`;
  if (d) return `d. ${d}`;
  return "";
}

function visiblePeople() {
  if (!peopleSearch) return allPeopleRich;
  const q = peopleSearch.toLowerCase();
  return allPeopleRich.filter(p =>
    `${p.first_name} ${p.last_name} ${p.maiden_name || ""}`.toLowerCase().includes(q));
}

function renderPeopleGrid() {
  const grid = document.getElementById("people-grid");
  if (!grid) return;
  const people = visiblePeople();
  grid.innerHTML = "";

  if (!people.length) {
    grid.innerHTML = allPeopleRich.length
      ? `<p class="empty-note">Nobody matches “${escapeHtml(peopleSearch)}”.</p>`
      : `<p class="empty-note">No people yet. Link someone to a document in the Curation Studio and they appear here.</p>`;
    return;
  }

  people.forEach(p => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "person-card" + (p.doc_count ? "" : " is-unattested");
    const span = lifespanLabel(p);
    card.innerHTML = `
      <div class="person-card-face">${personAvatar(p)}</div>
      <div class="person-card-body">
        <span class="person-card-name">${escapeHtml(p.first_name)} ${escapeHtml(p.last_name)}</span>
        ${p.maiden_name ? `<span class="person-card-maiden">née ${escapeHtml(p.maiden_name)}</span>` : ""}
        ${span ? `<span class="person-card-span">${escapeHtml(span)}</span>` : ""}
        <span class="person-card-counts">
          ${p.doc_count === 1 ? "1 document" : `${p.doc_count} documents`}${
            p.timeline_count ? ` · ${p.timeline_count} on the timeline` : ""}
        </span>
      </div>`;
    card.addEventListener("click", () => openPerson(p.id));
    grid.appendChild(card);
  });
}

async function openPerson(personId) {
  try {
    const [pRes, dRes] = await Promise.all([
      fetch(`/api/people/${personId}`),
      fetch(`/api/people/${personId}/documents`),
    ]);
    if (!pRes.ok) {
      // Deleted or merged away (a saved position can still name them).
      if (pRes.status === 404) switchView("view-people");
      return;
    }
    currentPerson = await pRes.json();
    currentPerson.documents = dRes.ok ? await dRes.json() : [];
  } catch { return; }

  const p = currentPerson;
  document.getElementById("person-name").textContent = `${p.first_name} ${p.last_name}`;
  document.getElementById("person-lifespan").textContent = [
    p.maiden_name ? `née ${p.maiden_name}` : "",
    lifespanLabel(p),
    plural(p.documents.length, "document"),
  ].filter(Boolean).join(" · ");
  const notes = document.getElementById("person-notes");
  if (notes) {
    notes.textContent = p.notes || "";
    notes.hidden = !p.notes;
  }
  document.getElementById("person-portrait").innerHTML = personAvatar(p);

  const roles = document.getElementById("person-roles");
  roles.innerHTML = (p.roles || [])
    .map(r => `<span class="person-role-tag">${escapeHtml(r.role)} · ${r.n}</span>`).join("");

  renderPersonDocuments(p.documents);
  switchView("view-person");
}

function renderPersonDocuments(docs) {
  const box = document.getElementById("person-documents");
  if (!box) return;
  box.innerHTML = "";
  if (!docs.length) {
    box.innerHTML = '<p class="empty-note">Nothing linked to them yet.</p>';
    return;
  }
  docs.forEach(doc => box.appendChild(buildCatalogCard(doc)));
}

// ---------------------------------------------------------------------------
// Portrait chooser
// ---------------------------------------------------------------------------
async function openPortraitChooser(personId) {
  let data;
  try {
    const res = await fetch(`/api/people/${personId}/portrait-options`);
    if (!res.ok) return;
    data = await res.json();
  } catch { return; }

  const modal = document.getElementById("modal-portrait");
  const body = document.getElementById("portrait-options");
  if (!modal || !body) return;

  const person = currentPerson || {};
  body.innerHTML = "";

  const choose = (mode, documentId, label, note, selected) => {
    const opt = document.createElement("button");
    opt.type = "button";
    opt.className = "portrait-option" + (selected ? " is-selected" : "");
    opt.innerHTML = `
      <div class="portrait-option-face">${
        documentId ? `<img src="/api/thumb?doc_id=${documentId}" alt="" loading="lazy">`
                   : `<span class="portrait-option-blank">${mode === "none" ? "—" : "?"}</span>`}</div>
      <div class="portrait-option-text">
        <strong>${escapeHtml(label)}</strong>
        ${note ? `<span>${escapeHtml(note)}</span>` : ""}
      </div>`;
    opt.addEventListener("click", async () => {
      await fetch(`/api/people/${personId}/portrait`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, document_id: documentId }),
      });
      modal.style.display = "none";
      await loadPeople();
      await openPerson(personId);
    });
    body.appendChild(opt);
  };

  choose("auto", data.suggested,
    "Let the archive choose",
    data.suggested ? "Currently the least crowded, earliest photograph of them" : "No photograph of them yet",
    person.portrait_mode === "auto");

  (data.options || []).forEach(o => {
    choose("pinned", o.id, o.title || `Document #${o.id}`,
      [o.date_display, o.role].filter(Boolean).join(" · "),
      person.portrait_mode === "pinned" && person.portrait_document_id === o.id);
  });

  choose("none", null, "No picture",
    "Respected — the archive will not pick one for them",
    person.portrait_mode === "none");

  modal.style.display = "flex";
}

function setupPeopleView() {
  document.getElementById("people-sort-select")?.addEventListener("change", (e) => loadPeople(e.target.value));

  const search = document.getElementById("people-search");
  if (search) {
    let t = null;
    search.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => { peopleSearch = search.value.trim(); renderPeopleGrid(); }, 180);
    });
  }
  document.getElementById("btn-people-clear-search")?.addEventListener("click", () => {
    if (search) search.value = "";
    peopleSearch = "";
    renderPeopleGrid();
  });

  document.getElementById("btn-back-to-people")?.addEventListener("click", () => switchView("view-people"));
  document.getElementById("btn-person-edit")?.addEventListener("click", () => {
    if (currentPerson) openPersonEditor(currentPerson);
  });
  document.getElementById("btn-person-portrait")?.addEventListener("click", () => {
    if (currentPerson) openPortraitChooser(currentPerson.id);
  });

  // The rare and the destructive sit behind "…", as they do on the record.
  const btnMore = document.getElementById("btn-person-overflow");
  const sheet = document.getElementById("person-overflow-sheet");
  if (btnMore && sheet) {
    btnMore.addEventListener("click", (e) => {
      e.stopPropagation();
      sheet.hidden = !sheet.hidden;
      btnMore.setAttribute("aria-expanded", sheet.hidden ? "false" : "true");
    });
    document.addEventListener("click", () => {
      sheet.hidden = true;
      btnMore.setAttribute("aria-expanded", "false");
    });
  }
  document.getElementById("btn-person-merge")?.addEventListener("click", () => {
    if (currentPerson) openMergeWith(currentPerson);
  });
  document.getElementById("btn-person-delete")?.addEventListener("click", () => {
    if (currentPerson) deletePerson(currentPerson);
  });

  document.getElementById("btn-dismiss-duplicates")?.addEventListener("click", () => {
    dismissDuplicateGroups(duplicateGroups);
  });
  document.getElementById("btn-review-duplicates")?.addEventListener("click", openMergeModal);

  document.getElementById("btn-close-portrait-modal")?.addEventListener("click", () => {
    document.getElementById("modal-portrait").style.display = "none";
  });
  document.getElementById("btn-close-merge-modal")?.addEventListener("click", () => {
    document.getElementById("modal-merge").style.display = "none";
  });

  setupPersonEditor();
  document.getElementById("merge-with-select")?.addEventListener("change", renderMergePair);
  document.getElementById("btn-close-merge-with")?.addEventListener("click", () => {
    document.getElementById("modal-merge-with").style.display = "none";
  });
}

// ---------------------------------------------------------------------------
// Correcting people: a misspelt name, a wrong date, a duplicate, or someone
// who should not be in the archive at all. Documents could always be
// corrected and deleted; people could only be created.
// ---------------------------------------------------------------------------

/** What a typed lifespan date will be stored as, shown under its field. */
function renderLifespanReading(inputId) {
  const input = document.getElementById(inputId);
  const out = document.getElementById(`${inputId}-reading`);
  if (!input || !out) return;
  const r = parseHistoricalDate(input.value);
  out.className = "lifespan-reading" + (r.empty ? "" : r.ok ? " is-read" : " is-unread");
  out.textContent = r.empty ? "" : r.ok ? r.reading : `Sorry — ${r.reading}.`;
}

/** A lifespan field as EDTF: "" when left empty, null when it cannot be read. */
function lifespanEdtf(inputId) {
  const r = parseHistoricalDate(document.getElementById(inputId)?.value || "");
  return r.ok ? r.edtf : null;
}

function unreadableLifespan(birth, death) {
  return `The ${birth === null ? "birth" : "death"} date cannot be read. Try 1895, c. 1895 or May 1895.`;
}

function showFormError(id, message) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = message || "";
  el.hidden = !message;
}

/** The server's own reason for refusing, when it gave one in words. */
async function responseError(res, fallback) {
  const body = await res.json().catch(() => ({}));
  return typeof body.detail === "string" ? body.detail : fallback;
}

function openPersonEditor(person) {
  setVal("edit-person-first", person.first_name || "");
  setVal("edit-person-last", person.last_name || "");
  setVal("edit-person-maiden", person.maiden_name || "");
  setVal("edit-person-birth", person.birth_edtf || "");
  setVal("edit-person-death", person.death_edtf || "");
  setVal("edit-person-notes", person.notes || "");
  renderLifespanReading("edit-person-birth");
  renderLifespanReading("edit-person-death");
  showFormError("edit-person-error", "");
  document.getElementById("modal-edit-person").style.display = "flex";
  document.getElementById("edit-person-first")?.focus();
}

function closePersonEditor() {
  document.getElementById("modal-edit-person").style.display = "none";
}

async function savePersonEdits() {
  const person = currentPerson;
  if (!person) return;
  const first = document.getElementById("edit-person-first").value.trim();
  const last = document.getElementById("edit-person-last").value.trim();
  if (!first || !last) {
    showFormError("edit-person-error", "A person needs both a first and a last name.");
    return;
  }
  const birth = lifespanEdtf("edit-person-birth");
  const death = lifespanEdtf("edit-person-death");
  if (birth === null || death === null) {
    showFormError("edit-person-error", unreadableLifespan(birth, death));
    return;
  }

  const btn = document.getElementById("btn-save-edit-person");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/api/people/${person.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields: {
        first_name: first,
        last_name: last,
        maiden_name: document.getElementById("edit-person-maiden").value,
        birth_edtf: birth,
        death_edtf: death,
        notes: document.getElementById("edit-person-notes").value,
      } }),
    });
    if (!res.ok) {
      showFormError("edit-person-error", await responseError(res, "That could not be saved."));
      return;
    }
    const saved = (await res.json()).person;
    closePersonEditor();

    // Their name is shown on every document they are in, the open record too.
    currentLinkedPeople.forEach(lp => {
      if (lp.id === saved.id) Object.assign(lp, { first_name: saved.first_name, last_name: saved.last_name });
    });
    renderLinkedPeople();
    await Promise.all([loadPeople(), loadDocumentArchive(), loadDuplicates()]);
    await openPerson(saved.id);
  } catch {
    showFormError("edit-person-error", "Could not reach the archive. Nothing was changed.");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function setupPersonEditor() {
  document.getElementById("form-edit-person")?.addEventListener("submit", (e) => {
    e.preventDefault();
    savePersonEdits();
  });
  document.getElementById("btn-close-edit-person")?.addEventListener("click", closePersonEditor);
  document.getElementById("btn-cancel-edit-person")?.addEventListener("click", closePersonEditor);
  // A complaint about a field goes as soon as that field is being put right.
  document.getElementById("form-edit-person")?.addEventListener("input", () => showFormError("edit-person-error", ""));
  document.getElementById("modal-new-person")?.addEventListener("input", () => showFormError("new-person-error", ""));
  ["edit-person-birth", "edit-person-death", "new-person-birth", "new-person-death"].forEach(id => {
    document.getElementById(id)?.addEventListener("input", () => renderLifespanReading(id));
  });
  // Esc cancels these dialogs from inside a field too, as it does on a Mac.
  ["modal-edit-person", "modal-merge-with"].forEach(id => {
    const modal = document.getElementById(id);
    modal?.addEventListener("keydown", (e) => {
      if (e.key === "Escape") modal.style.display = "none";
    });
  });
}

async function deletePerson(person) {
  const name = `${person.first_name} ${person.last_name}`;
  const n = Array.isArray(person.documents) ? person.documents.length : (person.doc_count || 0);
  const detail = n
    ? `They are named in ${plural(n, "document")}. ${n === 1 ? "It stays" : "Those stay"} in the archive; only ${person.first_name} is taken off ${n === 1 ? "it" : "them"}.`
    : "Nothing in the archive is linked to them.";
  if (!confirm(`Delete ${name}?\n\n${detail}\n\nThis cannot be undone.`)) return;

  // A pending save of the open record lands first, with the links it had.
  await flushPendingSave();
  let removed = null;
  try {
    const res = await fetch(`/api/people/${person.id}`, { method: "DELETE" });
    if (res.ok) {
      removed = (await res.json()).deleted;
    } else if (res.status !== 404) {      // 404: already gone, which is the goal
      alert(await responseError(res, `${name} could not be deleted.`));
      return;
    }
  } catch {
    alert(`Could not reach the archive. ${name} was not deleted.`);
    return;
  }

  forgetPerson(person.id);
  if (document.getElementById("view-person")?.classList.contains("active")) switchView("view-people");
  const docs = removed ? removed.documents : 0;
  showToast(`Deleted ${name}.` + (docs === 1 ? " The document they were in stays in the archive."
    : docs > 1 ? ` The ${docs} documents they were in stay in the archive.` : ""), "done", 6000);
  await Promise.all([loadPeople(), loadDocumentArchive(), loadDuplicates()]);
}

/** Drop someone who no longer exists from everything this page holds on them,
    so no filter, open record or later autosave refers to them again. */
function forgetPerson(personId) {
  if (currentPerson?.id === personId) currentPerson = null;
  if (catalogPeopleFilter.includes(personId)) {
    catalogPeopleFilter = catalogPeopleFilter.filter(id => id !== personId);
    renderPersonFilterChips();
  }
  if (currentLinkedPeople.some(lp => lp.id === personId)) {
    currentLinkedPeople = currentLinkedPeople.filter(lp => lp.id !== personId);
    renderLinkedPeople();
  }
  documents.forEach(d => { if (d.people) d.people = d.people.filter(p => p.id !== personId); });
}

/** Fold one person into another, then bring this page in line with it. */
async function mergePeople(keepId, mergeId) {
  // A pending save of the open record lands first, so the merge moves its links.
  await flushPendingSave();
  const res = await fetch("/api/people/merge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep_id: keepId, merge_id: mergeId }),
  });
  if (!res.ok) throw new Error(await responseError(res, "Those two could not be merged."));
  const survivor = (await res.json()).person;

  // The open record's links now belong to the survivor. Say so at once, before
  // an autosave can send the old id, then take the server's exact version.
  if (currentLinkedPeople.some(lp => lp.id === mergeId)) {
    const hasKeep = currentLinkedPeople.some(lp => lp.id === keepId);
    currentLinkedPeople = currentLinkedPeople
      .filter(lp => !(lp.id === mergeId && hasKeep))
      .map(lp => lp.id === mergeId
        ? { ...lp, id: keepId, first_name: survivor.first_name, last_name: survivor.last_name } : lp);
    renderLinkedPeople();
    refreshOpenRecordPeople();
  }
  forgetPerson(mergeId);
  return survivor;
}

/** Take the open record's people from the server after they changed there. */
async function refreshOpenRecordPeople() {
  const docId = currentDoc?.id;
  if (!docId) return;
  try {
    const res = await fetch(`/api/documents/${docId}/full`);
    if (!res.ok) return;
    const full = await res.json();
    // Typing since then wins: its save carries the list already put right.
    if (currentDoc?.id !== docId || dirtySinceSave || savingNow) return;
    currentLinkedPeople = (full.people || []).map(p => ({
      id: p.id, first_name: p.first_name, last_name: p.last_name, role: p.role || "subject",
    }));
    renderLinkedPeople();
  } catch { /* the list put right above stands */ }
}

/** One person in a merge choice, with the button that keeps them. */
function mergePersonRow(person, onKeep) {
  const row = document.createElement("div");
  row.className = "merge-person";
  const span = lifespanLabel(person);
  row.innerHTML = `
    <span class="merge-person-name">${escapeHtml(person.first_name)} ${escapeHtml(person.last_name)}</span>
    <span class="field-note">#${person.id} · ${plural(person.doc_count || 0, "document")}${
      span ? `<span class="merge-person-span"> · ${escapeHtml(span)}</span>` : ""}</span>`;
  const keep = document.createElement("button");
  keep.type = "button";
  keep.className = "btn btn-outline btn-sm";
  keep.textContent = "Keep this one";
  keep.addEventListener("click", () => onKeep(keep));
  row.appendChild(keep);
  return row;
}

function openMergeModal() {
  const modal = document.getElementById("modal-merge");
  const body = document.getElementById("merge-groups");
  if (!modal || !body) return;
  body.innerHTML = "";

  if (!duplicateGroups.length) {
    body.innerHTML = '<p class="empty-note">Nothing looks duplicated.</p>';
  }

  duplicateGroups.forEach(group => {
    const box = document.createElement("div");
    box.className = "merge-group";
    box.innerHTML = `<p class="merge-reason">Matched on ${escapeHtml(group.reason)}</p>`;
    const list = document.createElement("div");
    list.className = "merge-people";

    group.people.forEach(person => {
      list.appendChild(mergePersonRow(person, async (keep) => {
        keep.disabled = true;
        keep.textContent = "Merging…";
        try {
          for (const other of group.people.filter(o => o.id !== person.id)) {
            await mergePeople(person.id, other.id);
          }
        } catch (err) {
          keep.disabled = false;
          keep.textContent = "Keep this one";
          alert(err.message);
          return;
        }
        modal.style.display = "none";
        await Promise.all([loadPeople(), loadDuplicates(), loadDocumentArchive()]);
        if (document.getElementById("view-person")?.classList.contains("active")) {
          openPerson(currentPerson ? currentPerson.id : person.id);
        }
      }));
    });

    // A shared name is not always one person: two cousins can carry it.
    const different = document.createElement("button");
    different.type = "button";
    different.className = "btn-link-action merge-different";
    different.textContent = "These are different people";
    different.addEventListener("click", () => {
      dismissDuplicateGroups([group]);
      box.remove();
      if (!duplicateGroups.length) modal.style.display = "none";
    });

    box.append(list, different);
    body.appendChild(box);
  });

  modal.style.display = "flex";
}

// Two records of one person that the duplicate check cannot see: a
// transliteration (Georgios / Γεώργιος), a misspelling, a married name.
let mergeWithSource = null;

function openMergeWith(person) {
  mergeWithSource = person;
  const select = document.getElementById("merge-with-select");
  select.innerHTML = '<option value="">Choose the other record…</option>';
  allPeopleRich.filter(p => p.id !== person.id).forEach(p => {
    const opt = document.createElement("option");
    opt.value = p.id;
    const span = lifespanLabel(p);
    opt.textContent = `${p.first_name} ${p.last_name}${span ? ` (${span})` : ""} — ${plural(p.doc_count || 0, "document")}`;
    select.appendChild(opt);
  });
  document.getElementById("merge-with-title").textContent =
    `${person.first_name} ${person.last_name} is the same person as…`;
  document.getElementById("merge-with-pair").innerHTML = "";
  showFormError("merge-with-error", allPeopleRich.length > 1 ? "" : "There is nobody else in the archive yet.");
  document.getElementById("modal-merge-with").style.display = "flex";
  select.focus();
}

function renderMergePair() {
  const box = document.getElementById("merge-with-pair");
  const otherId = Number(document.getElementById("merge-with-select")?.value);
  box.innerHTML = "";
  showFormError("merge-with-error", "");
  const other = allPeopleRich.find(p => p.id === otherId);
  if (!mergeWithSource || !other) return;
  const source = allPeopleRich.find(p => p.id === mergeWithSource.id) || mergeWithSource;

  [[source, other], [other, source]].forEach(([keep, gone]) => {
    box.appendChild(mergePersonRow(keep, async (btn) => {
      btn.disabled = true;
      btn.textContent = "Merging…";
      try {
        await mergePeople(keep.id, gone.id);
      } catch (err) {
        btn.disabled = false;
        btn.textContent = "Keep this one";
        showFormError("merge-with-error", err.message);
        return;
      }
      document.getElementById("modal-merge-with").style.display = "none";
      showToast(`Merged ${gone.first_name} ${gone.last_name} into ${keep.first_name} ${keep.last_name}.`, "done", 6000);
      await Promise.all([loadPeople(), loadDocumentArchive(), loadDuplicates()]);
      await openPerson(keep.id);
    }));
  });
}

// ============================================================================
// The Record
// ============================================================================
function renderKindChips() {
  const box = document.getElementById("kind-chips");
  if (!box) return;
  const current = box.getAttribute("data-value") || "";
  box.innerHTML = "";
  allKinds.forEach(k => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "kind-chip" + (k.key === current ? " is-on" : "");
    chip.setAttribute("data-kind", k.key);
    chip.setAttribute("role", "radio");
    chip.setAttribute("aria-checked", k.key === current ? "true" : "false");
    chip.title = k.hint;
    chip.textContent = k.label;
    box.appendChild(chip);
  });
}

function setKind(key) {
  const box = document.getElementById("kind-chips");
  if (!box) return;
  box.setAttribute("data-value", key);
  box.querySelectorAll(".kind-chip").forEach(c => {
    const on = c.getAttribute("data-kind") === key;
    c.classList.toggle("is-on", on);
    c.setAttribute("aria-checked", on ? "true" : "false");
  });
  applyKindConsequences(key);
}

function currentKind() {
  return document.getElementById("kind-chips")?.getAttribute("data-value") || "document";
}

/** Show what the chosen kind actually changes, where it changes it. */
function applyKindConsequences(key) {
  const meta = kindMeta(key);
  const note = document.getElementById("kind-note");
  if (note) note.textContent = meta.hint || "";

  const section = document.getElementById("sec-text");
  const heading = document.getElementById("text-heading");
  const textNote = document.getElementById("text-note");
  const runBtn = document.getElementById("btn-run-ocr");
  const area = document.getElementById("transcription-text");

  updateSectionTitles();
  if (section) section.classList.toggle("is-quiet", !meta.textual);
  if (heading) heading.textContent = meta.textual ? "What it says" : "Anything written on it";
  if (runBtn) runBtn.textContent = meta.textual ? "Read the page" : "Try reading it anyway";
  if (textNote) {
    textNote.hidden = meta.textual;
    textNote.textContent = meta.textual ? "" :
      "Photographs are not read automatically. Type a caption, a verso inscription or a photographer's stamp here.";
  }
  if (area) {
    area.placeholder = meta.textual
      ? "Type what the page says, exactly as it says it."
      : "Caption, inscription on the back, studio stamp…";
  }
}

/** The projection readout: a consequence of the fields, never an input. */
function updateProjectionReadout() {
  const line = document.getElementById("projection-line");
  const text = document.getElementById("projection-text");
  if (!line || !text) return;

  const excluded = document.getElementById("doc-excluded")?.checked;
  const raw = document.getElementById("doc-date")?.value || "";
  const parsed = parseHistoricalDate(raw);

  let state, message;
  if (excluded) {
    state = "is-off";
    message = "Held back — this will not appear on the timeline";
  } else if (parsed.empty) {
    state = "is-undated";
    message = "Undated — give it a date above and it joins the timeline";
  } else if (!parsed.ok) {
    state = "is-undated";
    message = "The date above cannot be read, so this is not on the timeline yet";
  } else {
    state = "is-on";
    const clean = parsed.reading.split(" · ")[0];
    message = `On the timeline at ${clean}`;
  }
  line.className = `projection-line ${state}`;
  text.textContent = message;
  updateSectionTitles();
}

function setupRecordEditor() {
  // --- section jump nav (scroll position, not hidden state) ---
  const scroller = document.getElementById("record-scroll");
  document.querySelectorAll(".jump-link").forEach(btn => {
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.getAttribute("data-target"));
      if (target && scroller) scroller.scrollTo({ top: target.offsetTop - 12, behavior: "smooth" });
    });
  });
  if (scroller) {
    let ticking = false;
    scroller.addEventListener("scroll", () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        // Measured against the scrollport itself. offsetTop is relative to the
        // nearest positioned ancestor, which is not the scroller here, so
        // comparing it with scrollTop was off by a constant and marked the
        // wrong section pinned.
        const portTop = scroller.getBoundingClientRect().top;

        let current = null;
        scroller.querySelectorAll(".section-title:not(.is-plain)").forEach(t => {
          const top = t.getBoundingClientRect().top;
          // A sticky title sits exactly at the scrollport top while its own
          // section owns the viewport. Once that section ends, the title is
          // pushed up out of view with it -- above the port, not pinned to it --
          // so this is a band around the port top, not everything above it.
          const pinned = Math.abs(top - portTop) <= 2;
          t.classList.toggle("is-pinned", pinned);
          if (pinned) current = t.parentElement.id;
        });

        document.querySelectorAll(".jump-link").forEach(b =>
          b.classList.toggle("is-current", b.getAttribute("data-target") === (current || "sec-identity")));
        ticking = false;
      });
    });
  }

  // --- prev / next ---
  document.getElementById("btn-prev-doc")?.addEventListener("click", () => {
    if (currentDocIndex > 0) loadDocumentForCuration(currentDocIndex - 1);
  });
  document.getElementById("btn-next-doc")?.addEventListener("click", () => {
    if (currentDocIndex < documents.length - 1) loadDocumentForCuration(currentDocIndex + 1);
  });

  // --- kind chips ---
  document.getElementById("kind-chips")?.addEventListener("click", (e) => {
    const chip = e.target.closest(".kind-chip");
    if (!chip) return;
    setKind(chip.getAttribute("data-kind"));
    // The curator has decided; how the scan was sorted no longer matters.
    const sorted = document.getElementById("kind-sorted");
    if (sorted) sorted.hidden = true;
    scheduleSave();
  });

  // --- the one date input ---
  const dateInput = document.getElementById("doc-date");
  if (dateInput) {
    dateInput.addEventListener("input", () => {
      renderDateReading(dateInput.value);
      updateProjectionReadout();
    });
    // Normalise to the canonical form on the way out, so the box and the
    // database agree about what was stored.
    dateInput.addEventListener("blur", () => {
      const r = parseHistoricalDate(dateInput.value);
      if (r.ok && !r.empty && r.edtf !== dateInput.value.trim()) {
        dateInput.value = r.edtf;
        renderDateReading(dateInput.value);
      }
    });
  }

  document.getElementById("doc-excluded")?.addEventListener("change", updateProjectionReadout);

  // --- transcription stats ---
  const transText = document.getElementById("transcription-text");
  if (transText) {
    transText.addEventListener("input", () => {
      const len = transText.value.length;
      const words = transText.value.trim().split(/\s+/).filter(Boolean).length;
      const stats = document.getElementById("transcription-stats");
      if (stats) stats.textContent = `${len} characters · ${words} words`;
      updateSectionTitles();
    });
  }

  // --- people ---
  document.getElementById("btn-link-person")?.addEventListener("click", () => {
    const picker = document.getElementById("person-picker");
    const roleSel = document.getElementById("person-role");
    const personId = picker?.value;
    if (!personId) return;
    const person = allPeople.find(p => String(p.id) === String(personId));
    if (!person) return;
    if (!currentLinkedPeople.some(lp => String(lp.id) === String(personId))) {
      currentLinkedPeople.push({
        id: person.id, first_name: person.first_name,
        last_name: person.last_name, role: roleSel?.value || "subject"
      });
      renderLinkedPeople();
      scheduleSave();
    }
    picker.value = "";
  });

  setupTagsInput();

  // --- everything else saves itself ---
  const pane = document.querySelector(".curation-editor-pane");
  if (pane) {
    const onChange = (e) => {
      if (e.target.closest(".modal-backdrop")) return;
      if (e.target.id === "person-picker" || e.target.id === "person-role") return;
      scheduleSave();
    };
    pane.addEventListener("input", onChange);
    pane.addEventListener("change", onChange);
  }

  // --- the one button that ends the job ---
  document.getElementById("btn-done")?.addEventListener("click", () => finishRecord());

  // --- overflow: the rare and the destructive, away from the main path ---
  const btnOverflow = document.getElementById("btn-overflow");
  const sheet = document.getElementById("overflow-sheet");
  if (btnOverflow && sheet) {
    btnOverflow.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = sheet.hidden;
      sheet.hidden = !open;
      btnOverflow.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", () => {
      sheet.hidden = true;
      btnOverflow.setAttribute("aria-expanded", "false");
    });
  }
  document.getElementById("btn-reopen")?.addEventListener("click", async () => {
    if (!currentDoc) return;
    await saveRecord({ reviewed: false });
    await loadDocumentForCuration(currentDocIndex, false);
  });
  document.getElementById("btn-delete-doc")?.addEventListener("click", () => {
    if (!currentDoc) return;
    deleteDocumentItem(currentDoc.id, currentDoc.title || currentDoc.file_path);
  });
  document.getElementById("btn-split-page")?.addEventListener("click", splitCurrentPage);
}

function openDocInCuration(docId, page = 0) {
  const idx = documents.findIndex(d => d.id === docId);
  requestedPage = page;
  if (idx !== -1) loadDocumentForCuration(idx, true);
}

async function loadDocumentForCuration(index, switchViewFlag = true) {
  if (index < 0 || index >= documents.length) return;

  // Never let a pending save land on the document we are leaving.
  await flushPendingSave();

  currentDocIndex = index;
  currentDoc = documents[index];
  dirtySinceSave = false;
  setSaveStatus("idle", "Ready");
  // A saved zoom belongs to its own scan; moving on to another one drops it.
  if (pendingViewerRestore && pendingViewerRestore.doc_id !== currentDoc.id) pendingViewerRestore = null;

  const subLabel = document.getElementById("nav-active-doc-sub");
  if (subLabel) subLabel.textContent = `#${currentDoc.id} ${currentDoc.file_path.split("/").pop()}`;

  let full;
  try {
    const res = await fetch(`/api/documents/${currentDoc.id}/full`);
    full = res.ok ? await res.json() : { ...currentDoc, people: [], tags: [] };
  } catch (e) {
    full = { ...currentDoc, people: [], tags: [] };
  }
  currentDoc = { ...currentDoc, ...full };
  documents[index] = { ...documents[index], ...full };

  populateRecord(full);
  currentPage = Math.min(requestedPage, pageCount() - 1);
  requestedPage = 0;
  renderPageStrip();
  if (currentPage > 0) showPageText();

  const scanImg = document.getElementById("scan-image");
  const emptyOverlay = document.getElementById("viewport-empty");
  const filePathDisplay = document.getElementById("file-path-display");
  const counterLabel = document.getElementById("doc-counter-label");

  if (filePathDisplay) filePathDisplay.textContent = currentDoc.file_path;
  if (counterLabel) counterLabel.textContent = `${currentDocIndex + 1} of ${documents.length}`;

  if (scanImg && emptyOverlay) {
    const loadingDocId = currentDoc.id;
    scanImg.onerror = () => {
      emptyOverlay.textContent = `Unable to load the scan for #${currentDoc.id}. The file was not found.`;
      emptyOverlay.style.display = "block";
      scanImg.style.display = "none";
    };
    scanImg.onload = () => {
      emptyOverlay.style.display = "none";
      scanImg.style.display = "block";
      const dimLabel = document.getElementById("dimensions-display");
      if (dimLabel) dimLabel.textContent = `${scanImg.naturalWidth} × ${scanImg.naturalHeight} px`;
      fitToViewport("contain", false);
      scanImg.dataset.fittedDoc = String(loadingDocId);
      applyPendingViewerRestore();
    };
    delete scanImg.dataset.fittedDoc;
    scanImg.src = `/api/image?doc_id=${currentPageObj().id}`;
    if (scanImg.complete && scanImg.naturalWidth > 0) fitToViewport("contain", false);
  }

  if (switchViewFlag) switchView("view-curation");
  persistSession();
}

function populateRecord(doc) {
  const filename = doc.file_path.split("/").pop();

  const head = document.getElementById("record-title");
  if (head) head.textContent = doc.title || filename;

  const mark = document.getElementById("review-mark");
  if (mark) mark.hidden = !doc.reviewed;

  setVal("doc-title", doc.title || "");
  setVal("doc-location", doc.location || "");
  setVal("doc-summary", doc.summary || "");
  setVal("doc-creator", doc.creator || "");
  setVal("doc-notes", doc.notes || "");

  const dateInput = document.getElementById("doc-date");
  if (dateInput) {
    dateInput.value = doc.edtf_date || "";
    renderDateReading(dateInput.value);
  }

  const excluded = document.getElementById("doc-excluded");
  if (excluded) excluded.checked = Boolean(doc.excluded);

  setKind(doc.kind || "document");
  renderSortingNote(doc);

  currentDocTags = Array.isArray(doc.tags) ? [...doc.tags] : [];
  renderTags();

  currentLinkedPeople = (doc.people || []).map(p => ({
    id: p.id, first_name: p.first_name, last_name: p.last_name, role: p.role || "subject"
  }));
  renderLinkedPeople();

  const langSelect = document.getElementById("language-select");
  if (langSelect && doc.language) langSelect.value = doc.language;

  const transArea = document.getElementById("transcription-text");
  if (transArea) {
    transArea.value = doc.transcription || "";
    const stats = document.getElementById("transcription-stats");
    const words = (doc.transcription || "").trim().split(/\s+/).filter(Boolean).length;
    if (stats) stats.textContent = `${(doc.transcription || "").length} characters · ${words} words`;
  }

  const runBtn = document.getElementById("btn-run-ocr");
  if (runBtn && kindMeta(doc.kind).textual) {
    runBtn.textContent = doc.transcription ? "Read the page again" : "Read the page";
  }

  const doneBtn = document.getElementById("btn-done");
  if (doneBtn) doneBtn.innerHTML = doc.reviewed ? 'Next <kbd>⌘↵</kbd>' : 'Done <kbd>⌘↵</kbd>';

  updateProjectionReadout();
  loadDateSuggestions(doc.id);

  document.getElementById("record-scroll")?.scrollTo({ top: 0 });
}

/** How the scan was sorted on the way in, until a person has reviewed it. */
function renderSortingNote(doc) {
  const note = document.getElementById("kind-sorted");
  if (!note) return;
  const s = doc.metadata?.sorting;
  note.hidden = !s || doc.reviewed;
  if (!s) return;
  const share = s.text_area >= 0.01 ? `${Math.round(s.text_area * 100)}%` : "under 1%";
  note.textContent = s.verdict === "unsure"
    ? "Sorted automatically, but undecided: no clear text and no photograph. Choose the kind above."
    : s.verdict === "photo"
      ? "Sorted automatically as a photograph: no text found on the scan."
      : `Sorted automatically as a document: text covers ${share} of the scan.`;
  note.classList.toggle("is-undecided", s.verdict === "unsure");
}

// ============================================================================
// Pages: "14600", "14600 1", "14600 2" are one item. The record describes the
// item; the scan and the transcription shown are the current page's.
// ============================================================================
function pageCount() {
  return Math.max(1, (currentDoc?.pages || []).length);
}

function currentPageObj() {
  const pages = currentDoc?.pages || [];
  if (pages.length > 1 && pages[currentPage]) return pages[currentPage];
  return { id: currentDoc?.id, page_no: 0, transcription: currentDoc?.transcription || "" };
}

function renderPageStrip() {
  const strip = document.getElementById("page-strip");
  const pages = currentDoc?.pages || [];
  const multi = pages.length > 1;
  if (strip) {
    strip.hidden = !multi;
    strip.innerHTML = multi ? pages.map((p, i) => `
      <button type="button" class="page-thumb${i === currentPage ? " is-current" : ""}" data-page="${i}"
              title="${escapeHtml(p.name || "")}" aria-label="Page ${i + 1}"${i === currentPage ? ' aria-current="true"' : ""}>
        <img src="/api/thumb?doc_id=${p.id}" alt="" loading="lazy">
        <span>${i + 1}</span>
      </button>`).join("") : "";
    strip.querySelectorAll("[data-page]").forEach(btn =>
      btn.addEventListener("click", () => goToPage(Number(btn.dataset.page))));
    strip.querySelector(".is-current")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  const split = document.getElementById("btn-split-page");
  if (split) split.hidden = !(multi && currentPage > 0);
  const all = document.getElementById("btn-run-ocr-all");
  if (all) {
    all.hidden = !multi || !kindMeta(currentDoc?.kind).textual;
    all.textContent = `Read all ${pages.length} pages`;
  }
  const runBtn = document.getElementById("btn-run-ocr");
  if (runBtn && !runBtn.disabled && kindMeta(currentDoc?.kind).textual) {
    const read = Boolean(currentPageObj().transcription);
    runBtn.textContent = multi ? (read ? "Read this page again" : "Read this page")
                               : (read ? "Read the page again" : "Read the page");
  }
  const path = document.getElementById("file-path-display");
  if (path && currentDoc) path.textContent = currentPageObj().file_path || currentDoc.file_path;
  updateSectionTitles();
}

/** Put the current page's own text in the transcription box. */
function showPageText() {
  const text = currentPageObj().transcription || "";
  const transArea = document.getElementById("transcription-text");
  if (transArea) transArea.value = text;
  const stats = document.getElementById("transcription-stats");
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  if (stats) stats.textContent = `${text.length} characters · ${words} words`;
}

async function goToPage(index) {
  if (!currentDoc) return;
  const next = Math.max(0, Math.min(pageCount() - 1, index));
  if (next === currentPage) return;
  await flushPendingSave();            // what was typed belongs to the page it was typed on
  currentPage = next;
  showPageText();
  renderPageStrip();
  const scanImg = document.getElementById("scan-image");
  if (scanImg) {
    delete scanImg.dataset.fittedDoc;
    scanImg.src = `/api/image?doc_id=${currentPageObj().id}`;
  }
  persistSession();
}

async function splitCurrentPage() {
  if (!currentDoc || currentPage === 0) return;
  await flushPendingSave();
  const page = currentPageObj();
  const itemId = currentDoc.id;
  const res = await fetch(`/api/documents/${page.id}/split`, { method: "POST" });
  if (!res.ok) {
    showToast(await responseError(res, "That page could not be separated."), "error");
    return;
  }
  await loadDocumentArchive(itemId);
  openDocInCuration(itemId, Math.max(0, currentPage - 1));
  showToast(`${page.name || "The page"} is now a separate record, waiting under To review.`);
}

function updateSectionTitles() {
  const kind = kindMeta(currentKind()).label;
  const raw = (document.getElementById("doc-date")?.value || "").trim();
  const parsed = parseHistoricalDate(raw);
  const when = parsed.empty || !parsed.ok ? "undated" : parsed.reading.split(" · ")[0];
  setText("identity-meta", `${kind} · ${when}`);

  const n = currentLinkedPeople.length;
  setText("people-meta", n === 0 ? "nobody yet" : n === 1 ? "1 person" : `${n} people`);

  const chars = (document.getElementById("transcription-text")?.value || "").length;
  const pageNote = pageCount() > 1 ? `Page ${currentPage + 1} of ${pageCount()} · ` : "";
  setText("text-meta", pageNote + (chars ? `${chars.toLocaleString()} characters` : "nothing yet"));
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function setVal(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value;
}

function renderLinkedPeople() {
  const container = document.getElementById("people-list");
  if (!container) return;
  container.innerHTML = "";

  updateSectionTitles();

  if (currentLinkedPeople.length === 0) {
    container.innerHTML = '<p class="field-note">Nobody linked yet. Linking a person puts this document on their thread of the timeline.</p>';
    return;
  }

  currentLinkedPeople.forEach((p, i) => {
    const row = document.createElement("div");
    row.className = "person-row";
    const name = `${escapeHtml(p.first_name)} ${escapeHtml(p.last_name)}`;
    row.innerHTML = `
      <span class="person-name">${name}</span>
      <select class="person-role-select" aria-label="Role of ${name}">${roleOptionsHtml(p.role)}</select>
      <button type="button" class="person-unlink" title="Unlink">&times;</button>`;
    // A wrong role is put right here, not by unlinking and linking again.
    row.querySelector(".person-role-select").addEventListener("change", (e) => {
      p.role = e.target.value;
      scheduleSave();
    });
    row.querySelector(".person-unlink").addEventListener("click", () => {
      currentLinkedPeople.splice(i, 1);
      renderLinkedPeople();
      scheduleSave();
    });
    container.appendChild(row);
  });
}

/** The roles the Link picker offers, plus an old one (father, spouse...) this
    person already has, which is shown and kept rather than silently replaced. */
function roleOptionsHtml(current) {
  const roles = [...document.querySelectorAll("#person-role option")].map(o => [o.value, o.textContent]);
  if (current && !roles.some(([value]) => value === current)) roles.push([current, current]);
  return roles.map(([value, label]) =>
    `<option value="${escapeHtml(value)}"${value === current ? " selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function setupTagsInput() {
  const input = document.getElementById("tags-entry");
  if (!input) return;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      const val = input.value.trim().replace(/,$/, "");
      if (val && !currentDocTags.includes(val)) {
        currentDocTags.push(val);
        renderTags();
        scheduleSave();
      }
      input.value = "";
    } else if (e.key === "Backspace" && !input.value && currentDocTags.length) {
      currentDocTags.pop();
      renderTags();
      scheduleSave();
    }
  });
  input.addEventListener("blur", () => {
    const val = input.value.trim();
    if (val && !currentDocTags.includes(val)) {
      currentDocTags.push(val);
      renderTags();
      scheduleSave();
    }
    input.value = "";
  });
}

function renderTags() {
  const field = document.getElementById("tag-field");
  const input = document.getElementById("tags-entry");
  if (!field || !input) return;
  field.querySelectorAll(".tag-chip").forEach(c => c.remove());
  currentDocTags.forEach((tag, i) => {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    chip.innerHTML = `${escapeHtml(tag)}<button type="button" title="Remove">&times;</button>`;
    chip.querySelector("button").addEventListener("click", () => {
      currentDocTags.splice(i, 1);
      renderTags();
      scheduleSave();
    });
    field.insertBefore(chip, input);
  });
}

function setupCurationResizer() {
  const resizer = document.getElementById("curation-resizer");
  const layout = document.querySelector(".curation-layout");
  if (!resizer || !layout) return;

  const STORAGE_KEY = "curation_split_ratio";
  const DEFAULT_RATIO = 50;

  // Restore persisted ratio if saved
  const savedRatio = parseFloat(localStorage.getItem(STORAGE_KEY));
  if (!isNaN(savedRatio) && savedRatio >= 20 && savedRatio <= 80) {
    layout.style.setProperty("--curation-viewer-width", `${savedRatio}%`);
  }

  let isDragging = false;

  const onPointerDown = (e) => {
    isDragging = true;
    resizer.classList.add("is-dragging");
    document.body.classList.add("is-resizing-curation");
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    e.preventDefault();
  };

  const onPointerMove = (e) => {
    if (!isDragging) return;
    const rect = layout.getBoundingClientRect();
    if (rect.width <= 0) return;

    const offsetX = e.clientX - rect.left;
    const minViewerWidth = 320;
    const minEditorWidth = 380;
    const clampedX = Math.max(minViewerWidth, Math.min(rect.width - minEditorWidth, offsetX));
    const percent = Math.max(20, Math.min(80, (clampedX / rect.width) * 100));

    layout.style.setProperty("--curation-viewer-width", `${percent.toFixed(2)}%`);
  };

  const onPointerUp = () => {
    if (!isDragging) return;
    isDragging = false;
    resizer.classList.remove("is-dragging");
    document.body.classList.remove("is-resizing-curation");
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerup", onPointerUp);

    const currentVal = layout.style.getPropertyValue("--curation-viewer-width");
    if (currentVal) {
      localStorage.setItem(STORAGE_KEY, parseFloat(currentVal));
    }
  };

  resizer.addEventListener("pointerdown", onPointerDown);

  // Double click resets to balanced 50% default
  resizer.addEventListener("dblclick", () => {
    layout.style.setProperty("--curation-viewer-width", `${DEFAULT_RATIO}%`);
    localStorage.removeItem(STORAGE_KEY);
  });
}

// ============================================================================
// Saving
//
// One path. v1 had Save Draft, Verify & Save, Reject and an Auto-Save toggle,
// where the first two posted an identical payload differing by one string, the
// third left the document on the published timeline anyway, and the fourth made
// the first one invisible. Work is kept as you type; Done means "I am finished
// looking at this", and nothing else.
// ============================================================================
function setSaveStatus(state, message) {
  const badge = document.getElementById("save-state");
  const text = document.getElementById("save-text");
  if (!badge || !text) return;
  badge.className = `save-state save-${state}`;
  text.textContent = message || ({
    saved: "Saved", saving: "Saving…", unsaved: "Unsaved", idle: "Ready", error: "Could not save"
  })[state] || "";
}

function scheduleSave() {
  if (!currentDoc) return;
  dirtySinceSave = true;
  setSaveStatus("unsaved", "Unsaved");
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveRecord(), 700);
}

/** Wait for any in-flight or pending save, so navigation cannot outrun it. */
async function flushPendingSave() {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
    if (dirtySinceSave) await saveRecord();
  }
  while (savingNow) {
    await new Promise(r => setTimeout(r, 40));
  }
}

function collectRecord() {
  return {
    title: document.getElementById("doc-title")?.value || "",
    edtf_date: currentDateEdtf(),
    location: document.getElementById("doc-location")?.value || "",
    summary: document.getElementById("doc-summary")?.value || "",
    creator: document.getElementById("doc-creator")?.value || "",
    notes: document.getElementById("doc-notes")?.value || "",
    kind: currentKind(),
    language: document.getElementById("language-select")?.value || "el-poly",
    transcription: document.getElementById("transcription-text")?.value || "",
    excluded: Boolean(document.getElementById("doc-excluded")?.checked),
    tags: currentDocTags,
  };
}

async function saveRecord({ reviewed = null } = {}) {
  if (!currentDoc) return null;
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }

  const docId = currentDoc.id;
  savingNow = true;
  setSaveStatus("saving");

  const payload = {
    fields: collectRecord(),
    people_links: currentLinkedPeople.map(p => ({ id: p.id, role: p.role })),
  };
  if (reviewed !== null) payload.reviewed = reviewed;

  // On a later page the transcription box holds that page's text: it is saved
  // to the page, and the item keeps its own first page's text.
  const page = currentPage > 0 ? currentPageObj() : null;
  const pageText = page ? payload.fields.transcription : null;
  if (page) delete payload.fields.transcription;

  try {
    if (page && pageText !== (page.transcription || "")) {
      const pageRes = await fetch(`/api/documents/${page.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields: { transcription: pageText } }),
      });
      if (!pageRes.ok) throw new Error(`Server returned ${pageRes.status}`);
    }
    const res = await fetch(`/api/documents/${docId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`Server returned ${res.status}`);
    const data = await res.json();
    const saved = data.document;

    const idx = documents.findIndex(d => d.id === docId);
    if (idx !== -1) documents[idx] = { ...documents[idx], ...saved };
    if (currentDoc && currentDoc.id === docId) currentDoc = { ...currentDoc, ...saved };

    dirtySinceSave = false;
    setSaveStatus("saved", `Saved ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);

    const head = document.getElementById("record-title");
    if (head && currentDoc?.id === docId) head.textContent = saved.title || saved.file_path.split("/").pop();
    const mark = document.getElementById("review-mark");
    if (mark && currentDoc?.id === docId) mark.hidden = !saved.reviewed;

    updateHeaderStats();
    renderDatabaseCatalog();
    loadTags();
    return saved;
  } catch (err) {
    console.error("Save failed:", err);
    setSaveStatus("error", "Could not save — your typing is still here");
    return null;
  } finally {
    savingNow = false;
  }
}

/** Done: mark reviewed, then move to the next document that still needs one. */
async function finishRecord() {
  if (!currentDoc) return;
  const btn = document.getElementById("btn-done");
  if (btn) btn.disabled = true;

  const saved = await saveRecord({ reviewed: true });
  if (btn) btn.disabled = false;
  if (!saved) return;

  const next = nextUnreviewedIndex(currentDocIndex);
  if (next !== -1) {
    loadDocumentForCuration(next);
  } else {
    setSaveStatus("saved", "Saved — nothing left to review");
  }
}

function nextUnreviewedIndex(from) {
  for (let i = from + 1; i < documents.length; i++) {
    if (!documents[i].reviewed) return i;
  }
  for (let i = 0; i <= from; i++) {
    if (!documents[i].reviewed) return i;
  }
  return from + 1 < documents.length ? from + 1 : -1;
}

// Nothing in flight should be lost to a closed tab.
window.addEventListener("beforeunload", (e) => {
  if (dirtySinceSave || savingNow) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// The timeline inspector (inside the Timeline Preview frame) saves through the
// same endpoint as this editor. Take its version of the record, so the editor
// here never autosaves the old values back over a change made there.
window.addEventListener("message", (e) => {
  if (e.origin !== window.location.origin || e.data?.type !== "archivesys:document-saved") return;
  const saved = e.data.document;
  if (!saved || !Number.isInteger(saved.id)) return;
  const idx = documents.findIndex(d => d.id === saved.id);
  if (idx !== -1) documents[idx] = { ...documents[idx], ...saved };
  if (currentDoc && currentDoc.id === saved.id && !dirtySinceSave && !savingNow) {
    currentDoc = { ...currentDoc, ...saved };
    populateRecord(currentDoc);
  }
  updateHeaderStats();
  renderDatabaseCatalog();
  loadTags();
});

function setupModals() {
  // New Person Modal
  const modalPerson = document.getElementById("modal-new-person");
  const btnOpenPerson = document.getElementById("btn-new-person");
  const btnClosePerson = document.getElementById("btn-close-person-modal");
  const btnCancelPerson = document.getElementById("btn-cancel-person-modal");
  const btnSavePerson = document.getElementById("btn-save-new-person");

  if (btnOpenPerson) {
    btnOpenPerson.addEventListener("click", () => {
      modalPerson.style.display = "flex";
      document.getElementById("new-person-first").focus();
    });
  }

  function closePersonModal() {
    modalPerson.style.display = "none";
    document.getElementById("new-person-first").value = "";
    document.getElementById("new-person-last").value = "";
    document.getElementById("new-person-birth").value = "";
    document.getElementById("new-person-death").value = "";
    document.getElementById("new-person-notes").value = "";
    renderLifespanReading("new-person-birth");
    renderLifespanReading("new-person-death");
    showFormError("new-person-error", "");
  }

  if (btnClosePerson) btnClosePerson.addEventListener("click", closePersonModal);
  if (btnCancelPerson) btnCancelPerson.addEventListener("click", closePersonModal);

  if (btnSavePerson) {
    btnSavePerson.addEventListener("click", async () => {
      const first = document.getElementById("new-person-first")?.value.trim();
      const last = document.getElementById("new-person-last")?.value.trim();
      if (!first || !last) {
        showFormError("new-person-error", "A person needs both a first and a last name.");
        return;
      }
      // Read the way the record reads dates, so "c. 1895" is stored as 1895~
      // instead of being refused by the server.
      const birth = lifespanEdtf("new-person-birth");
      const death = lifespanEdtf("new-person-death");
      if (birth === null || death === null) {
        showFormError("new-person-error", unreadableLifespan(birth, death));
        return;
      }

      try {
        const res = await fetch("/api/people", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            first_name: first,
            last_name: last,
            birth_edtf: birth || null,
            death_edtf: death || null,
            notes: document.getElementById("new-person-notes")?.value.trim() || null
          })
        });

        if (!res.ok) throw new Error(await responseError(res, "The person could not be created."));
        const newPerson = await res.json();
        allPeople.push(newPerson);
        populatePeoplePicker();
        loadPeople();

        // Immediately link to current document
        currentLinkedPeople.push({
          id: newPerson.id,
          first_name: newPerson.first_name,
          last_name: newPerson.last_name,
          role: "subject"
        });
        renderLinkedPeople();
        scheduleSave();
        closePersonModal();

      } catch (err) {
        showFormError("new-person-error", err.message || "Could not reach the archive.");
      }
    });
  }

}

// ============================================================================
// Re-OCR (queued, reported in the Activity panel)
// ============================================================================
document.addEventListener("DOMContentLoaded", () => {
  const btnRerun = document.getElementById("btn-run-ocr");
  if (!btnRerun) return;

  btnRerun.addEventListener("click", async () => {
    if (!currentDoc) return;
    const targetLang = document.getElementById("language-select")?.value || "el-poly";

    btnRerun.disabled = true;
    btnRerun.textContent = "Queued...";

    const submitted = await enqueueJobs({
      doc_id: currentPageObj().id,
      preferred_lang: targetLang === "mixed" ? null : targetLang,
    });

    if (!submitted || !submitted.jobs || !submitted.jobs[0]) {
      btnRerun.disabled = false;
      btnRerun.textContent = "Read the page";
      return;
    }

    // Watch for this specific job to run and finish, then bring its text into the editor.
    const jobId = submitted.jobs[0].id;
    const docId = currentDoc.id;
    const pageId = currentPageObj().id;
    const watch = setInterval(async () => {
      let data;
      try {
        data = await (await fetch("/api/jobs")).json();
      } catch { return; }

      // 1. Check if the job is actively running to provide live feedback on the button
      if (data.active && data.active.id === jobId) {
        const stage = data.active.stage || "Extracting";
        const elapsed = data.active.elapsed ? ` (${Math.round(data.active.elapsed)}s)` : "...";
        btnRerun.textContent = `${stage}${elapsed}`;
      }

      // 2. Check if the job completed in recent jobs
      const job = (data.recent || []).find(j => j.id === jobId);
      if (!job) return;

      clearInterval(watch);
      btnRerun.disabled = false;
      btnRerun.textContent = "Read the page again";

      if (job.state === "failed") {
        alert(`OCR failed: ${job.error || "Unknown OCR engine error"}`);
        return;
      }

      if (job.state !== "done") return;

      // Update in-memory document in the catalog regardless of active document
      const docInCatalog = documents.find(d => d.id === docId);
      rememberPageText(docId, pageId, job.result);
      if (docInCatalog && job.result && pageId === docId) {
        docInCatalog.transcription = job.result.transcription;
        docInCatalog.language = job.result.language;
        if (job.result.confidence !== undefined) docInCatalog.confidence_score = job.result.confidence;
      }
      renderDatabaseCatalog();

      // The user may have moved on while it ran; only populate the editor pane if still on this page.
      if (!currentDoc || currentDoc.id !== docId || currentPageObj().id !== pageId) return;
      applyOcrResult(job.result);
    }, 1000);
  });

  // Every page of the item, one job each, in page order. Progress is in Activity.
  document.getElementById("btn-run-ocr-all")?.addEventListener("click", async (e) => {
    if (!currentDoc) return;
    const btnAll = e.currentTarget;
    const targetLang = document.getElementById("language-select")?.value || "el-poly";
    const docId = currentDoc.id;
    const pages = [...(currentDoc.pages || [])];
    btnAll.disabled = true;
    const queued = [];
    for (const page of pages) {
      const submitted = await enqueueJobs({ doc_id: page.id, preferred_lang: targetLang === "mixed" ? null : targetLang });
      const job = submitted?.jobs?.[0];
      if (job) queued.push({ jobId: job.id, pageId: page.id });
    }
    btnAll.textContent = `Reading ${plural(queued.length, "page")}…`;
    const pending = new Map(queued.map(q => [q.jobId, q.pageId]));
    const watch = setInterval(async () => {
      let data;
      try { data = await (await fetch("/api/jobs")).json(); } catch { return; }
      for (const job of data.recent || []) {
        if (!pending.has(job.id)) continue;
        const pageId = pending.get(job.id);
        pending.delete(job.id);
        if (job.state !== "done") continue;
        rememberPageText(docId, pageId, job.result);
        if (currentDoc?.id === docId && currentPageObj().id === pageId) applyOcrResult(job.result);
      }
      if (currentDoc?.id === docId) btnAll.textContent = pending.size
        ? `Reading ${pending.size} more…` : `Read all ${pages.length} pages`;
      if (!pending.size) {
        clearInterval(watch);
        btnAll.disabled = false;
        if (currentDoc?.id === docId) { renderPageStrip(); loadDateSuggestions(docId); }
      }
    }, 1000);
  });
});

/** A page's new text, kept in memory wherever that item is held. */
function rememberPageText(docId, pageId, result) {
  if (!result || typeof result.transcription !== "string") return;
  for (const holder of [currentDoc, documents.find(d => d.id === docId)]) {
    if (!holder || holder.id !== docId) continue;
    const page = (holder.pages || []).find(p => p.id === pageId);
    if (page) page.transcription = result.transcription;
    if (pageId === docId) holder.transcription = result.transcription;
  }
}

function applyOcrResult(result) {
  const transText = document.getElementById("transcription-text");
  if (!transText || !result || typeof result.transcription !== "string") return;

  transText.value = result.transcription;
  const stats = document.getElementById("transcription-stats");
  if (stats) {
    const len = result.transcription.length;
    const words = result.transcription.trim().split(/\s+/).filter(Boolean).length;
    stats.textContent = `${len} characters, ${words} words`;
  }

  const page = currentPageObj();
  page.transcription = result.transcription;
  if (currentPage === 0) currentDoc.transcription = result.transcription;
  currentDoc.language = result.language;
  if (result.confidence !== undefined) {
    currentDoc.confidence_score = result.confidence;
  }

  // Update language select to match the language the engine actually used
  const langSelect = document.getElementById("language-select");
  if (langSelect && result.language) langSelect.value = result.language;

  markTranscriptionSaved(result);

  // Fresh text means the page may now state its own date.
  if (currentDoc) loadDateSuggestions(currentDoc.id);

  // Bring the text section into view: the reader's next move is to check it.
  document.getElementById("sec-text")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function markTranscriptionSaved(result) {
  const bits = [`${result.characters || (result.transcription || "").length} characters`];
  if (result.refined_lines) bits.push(`${result.refined_lines} lines re-read`);
  if (result.dropped_noise_lines) bits.push(`${result.dropped_noise_lines} noise lines dropped`);
  setSaveStatus("saved", `Read the page — ${bits.join(", ")}`);
}

function markTranscriptionUnsaved(result) {
  const bits = [`${result.characters || 0} characters`];
  if (result.refined_lines) bits.push(`${result.refined_lines} lines re-read`);
  setSaveStatus("unsaved", `New text not saved yet — ${bits.join(", ")}`);
}

// ============================================================================
// Explorer selection (checkbox, shift-range) & batch ingest
// ============================================================================

// Paths currently ticked, and the anchor a shift-click measures its range from.
const selectedPaths = new Set();
let selectionAnchor = null;

function visiblePaths() {
  return Array.from(document.querySelectorAll("#fs-files .file-card"))
    .map(c => c.dataset.path)
    .filter(Boolean);
}

function toggleSelection(path, isRange) {
  const paths = visiblePaths();

  if (isRange && selectionAnchor && selectionAnchor !== path) {
    // Shift-click selects everything between the anchor and this card, matching
    // the behaviour of a file manager: the range takes the state being applied
    // to the clicked item rather than toggling each member individually.
    const a = paths.indexOf(selectionAnchor);
    const b = paths.indexOf(path);
    if (a !== -1 && b !== -1) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      const turningOn = selectedPaths.has(selectionAnchor) || !selectedPaths.has(path);
      for (let i = lo; i <= hi; i++) {
        if (turningOn) selectedPaths.add(paths[i]);
        else selectedPaths.delete(paths[i]);
      }
      syncSelectionUI();
      return;
    }
  }

  if (selectedPaths.has(path)) selectedPaths.delete(path);
  else selectedPaths.add(path);
  selectionAnchor = path;
  syncSelectionUI();
}

function syncSelectionUI() {
  document.querySelectorAll("#fs-files .file-card").forEach(card => {
    const on = selectedPaths.has(card.dataset.path);
    card.classList.toggle("is-selected", on);
    const box = card.querySelector(".select-box-input");
    if (box) box.checked = on;
  });

  const bar = document.getElementById("selection-bar");
  const count = document.getElementById("selection-count");
  const n = selectedPaths.size;
  if (bar) bar.hidden = n === 0;
  if (count) count.textContent = `${n} scan${n === 1 ? "" : "s"} selected`;
}

function clearSelection() {
  selectedPaths.clear();
  selectionAnchor = null;
  syncSelectionUI();
}

function setupSelection() {
  document.getElementById("btn-clear-selection")?.addEventListener("click", clearSelection);

  // Prevent shift-click anywhere in the file explorer grid from highlighting text
  const fsFilesEl = document.getElementById("fs-files");
  if (fsFilesEl) {
    fsFilesEl.addEventListener("mousedown", (e) => {
      if (e.shiftKey) {
        e.preventDefault();
      }
    });
  }

  document.getElementById("btn-select-all")?.addEventListener("click", () => {
    const paths = visiblePaths();
    const everySelected = paths.length > 0 && paths.every(p => selectedPaths.has(p));
    if (everySelected) clearSelection();
    else {
      paths.forEach(p => selectedPaths.add(p));
      syncSelectionUI();
    }
  });

  document.getElementById("btn-ingest-selected")?.addEventListener("click", async (e) => {
    if (!selectedPaths.size) return;
    const btn = e.currentTarget;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Indexing...";
    }
    const paths = Array.from(selectedPaths);
    try {
      const res = await fetch("/api/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths }),
      });
      if (res.ok) {
        await loadDocumentArchive();
        updateHeaderStats();
        loadDirectory(currentFsPath, 0, currentFsQuery, currentFsFilterStatus);
      }
    } catch (err) {
      console.error("Batch index failed:", err);
    } finally {
      clearSelection();
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Index selected";
      }
    }
  });
}

// ============================================================================
// Adding a whole folder — a three-step wizard
//
// 1. Folders: only this folder, or its subfolders too (asked when there are any).
// 2. Check:   every file by name. Copies of scans already in the archive, or of
//             another file in the list, are shown with what they copy and left
//             out unless ticked.
// 3. Add:     a background job adds them and sorts each into document or
//             photograph; undecided ones wait first under "To review".
// "Select all" + "Index selected" only ever saw the 60 cards loaded so far.
// ============================================================================
const importWizard = { plan: null, folderPlan: null, step: "scope", jobId: null, timer: null, combine: {} };

function setupImportWizard() {
  document.getElementById("btn-import-folder")?.addEventListener("click", openImportWizard);
  document.getElementById("btn-close-import")?.addEventListener("click", closeImportWizard);
  document.getElementById("btn-import-cancel")?.addEventListener("click", cancelImportStep);
  document.getElementById("btn-import-back")?.addEventListener("click", () => renderImportScope());
  document.getElementById("btn-import-next")?.addEventListener("click", advanceImport);
}

async function fetchImportPlan(includeSubfolders) {
  const res = await fetch("/api/import/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: currentFsPath, include_subfolders: includeSubfolders }),
  });
  if (!res.ok) throw new Error(await responseError(res, "That folder could not be read."));
  return res.json();
}

function setImportStep(step, { next = null, back = false, cancel = "Cancel" } = {}) {
  importWizard.step = step;
  document.querySelectorAll("#import-steps li").forEach(li => {
    const order = ["scope", "review", "run"];
    li.classList.toggle("is-current", li.dataset.step === step);
    li.classList.toggle("is-done", order.indexOf(li.dataset.step) < order.indexOf(step));
  });
  const btnNext = document.getElementById("btn-import-next");
  btnNext.hidden = next === null;
  if (next !== null) btnNext.textContent = next;
  btnNext.disabled = false;
  document.getElementById("btn-import-back").hidden = !back;
  document.getElementById("btn-import-cancel").textContent = cancel;
}

async function openImportWizard() {
  if (!currentFsPath) return;
  const modal = document.getElementById("modal-import");
  const body = document.getElementById("import-body");
  if (importWizard.jobId) {            // one import at a time: show the running one
    modal.style.display = "flex";
    return;
  }
  importWizard.plan = importWizard.folderPlan = null;
  const name = currentFsPath.split("/").filter(Boolean).pop() || currentFsPath;
  document.getElementById("import-title").textContent = `Add “${name}” to the archive`;
  setImportStep("scope");
  document.getElementById("btn-import-next").hidden = true;
  body.innerHTML = '<p class="archive-status">Looking through the folder…</p>';
  modal.style.display = "flex";
  try {
    importWizard.folderPlan = await fetchImportPlan(false);
  } catch (err) {
    body.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
    return;
  }
  // Nothing to ask about when there are no subfolders with scans in them.
  if (importWizard.folderPlan.subfolders.length) renderImportScope();
  else renderImportReview(importWizard.folderPlan);
}

function planCount(plan) {
  return plan.new.length + plan.duplicates.length + plan.already.length;
}

function renderImportScope() {
  const plan = importWizard.folderPlan;
  const here = planCount(plan);
  const below = plan.subfolders.reduce((n, f) => n + f.count, 0);
  const body = document.getElementById("import-body");
  body.innerHTML = `
    <p class="archive-status">This folder has ${plural(here, "scan")}. It also has
      ${plural(plan.subfolders.length, "folder")} with ${plural(below, "more scan", "more scans")}:</p>
    <ul class="import-folders">${plan.subfolders.map(f =>
      `<li><span>${escapeHtml(f.name)}</span><span class="counter-label">${f.count}</span></li>`).join("")}</ul>
    <div class="import-choice" role="radiogroup">
      <label><input type="radio" name="import-scope" value="here" checked>
        Only this folder <span class="counter-label">${here}</span></label>
      <label><input type="radio" name="import-scope" value="all">
        This folder and its subfolders <span class="counter-label">${here + below}</span></label>
    </div>`;
  setImportStep("scope", { next: "Next" });
}

function renderImportReview(plan) {
  importWizard.plan = plan;
  // Named pages are combined unless the curator says otherwise; a scanner's
  // own numbering ("Scan 1", "Scan 2") is kept apart unless they say so.
  importWizard.combine = Object.fromEntries((plan.groups || []).map(g => [g.key, !g.counter]));
  const body = document.getElementById("import-body");
  const row = (item, checked, note = "") => `
    <label class="import-row">
      <input type="checkbox" class="import-pick" value="${escapeHtml(item.path)}"${checked ? " checked" : ""}>
      <span class="import-name" title="${escapeHtml(item.path)}">${escapeHtml(item.rel)}</span>
      ${note ? `<span class="import-note">${note}</span>` : ""}
    </label>`;
  const groupsByKey = Object.fromEntries((plan.groups || []).map(g => [g.key, g]));
  const groupRow = (g) => {
    const files = g.paths.map(p => plan.new.find(i => i.path === p)).filter(Boolean);   // in page order
    const together = importWizard.combine[g.key];
    const joins = g.in_archive ? ` · joins ${plural(g.in_archive, "page")} already in the archive` : "";
    const label = together
      ? `${plural(files.length, "page")}${joins}`
      : `${plural(files.length, "separate scan")}${g.counter ? ", numbered by the scanner" : ""}`;
    return `
      <div class="import-group${together ? " is-combined" : ""}" data-group="${escapeHtml(g.key)}">
        <label class="import-row">
          <input type="checkbox" class="import-pick import-group-pick" data-paths="${escapeHtml(JSON.stringify(files.map(f => f.path)))}" checked>
          <span class="import-name">${escapeHtml(g.folder ? `${g.folder}/${g.name}` : g.name)}</span>
          <span class="import-note">${label}</span>
          <button type="button" class="btn-link-action" data-toggle-group="${escapeHtml(g.key)}">
            ${together ? "Keep as separate scans" : "Combine as pages"}</button>
        </label>
        <details class="import-group-files"><summary>${files.map(f => escapeHtml(f.rel.split("/").pop())).slice(0, 3).join(", ")}${files.length > 3 ? ` … ${escapeHtml(files[files.length - 1].rel.split("/").pop())}` : ""}</summary>
          <div class="import-list">${files.map(f => `<div class="import-row is-plain"><span class="import-name">${escapeHtml(f.rel)}</span></div>`).join("")}</div>
        </details>
      </div>`;
  };
  const shownGroups = new Set();
  const newRows = plan.new.map(item => {
    const g = item.group && groupsByKey[item.group];
    if (!g) return row(item, true);
    if (shownGroups.has(g.key)) return "";
    shownGroups.add(g.key);
    return groupRow(g);
  }).join("");

  body.innerHTML = `
    <p class="archive-status" id="import-headline"></p>
    ${plan.new.length ? `
      <div class="import-section">
        <div class="import-section-head">
          <strong>New · ${plan.new.length}</strong>
          <span><button type="button" class="btn-link-action" data-pick="all">All</button>
                <button type="button" class="btn-link-action" data-pick="none">None</button></span>
        </div>
        <div class="import-list">${newRows}</div>
      </div>` : ""}
    ${plan.duplicates.length ? `
      <div class="import-section">
        <div class="import-section-head"><strong>Copies · ${plan.duplicates.length}</strong>
          <span class="field-note">Left out unless you tick them</span></div>
        <div class="import-list">${plan.duplicates.map(item => row(item, false,
          `same file as ${escapeHtml(item.same_as.name)} ${item.same_as.in_archive ? "(in the archive)" : "(in this list)"}`)).join("")}</div>
      </div>` : ""}
    ${plan.already.length ? `
      <details class="import-section">
        <summary><strong>Already in the archive · ${plan.already.length}</strong></summary>
        <div class="import-list">${plan.already.map(item =>
          `<div class="import-row is-plain"><span class="import-name">${escapeHtml(item.rel)}</span>
           <span class="import-note">#${item.doc_id}</span></div>`).join("")}</div>
      </details>` : ""}`;

  body.querySelectorAll("[data-pick]").forEach(btn => btn.addEventListener("click", () => {
    btn.closest(".import-section").querySelectorAll(".import-pick")
      .forEach(box => { box.checked = btn.dataset.pick === "all"; });
    updateImportCount();
  }));
  body.querySelectorAll("[data-toggle-group]").forEach(btn => btn.addEventListener("click", (e) => {
    e.preventDefault();
    const key = btn.dataset.toggleGroup;
    importWizard.combine[key] = !importWizard.combine[key];
    const picks = [...body.querySelectorAll(".import-pick")].map(b => [b.value || b.dataset.paths, b.checked]);
    renderImportReview(plan);
    // Re-rendering must not undo what was ticked or unticked.
    body.querySelectorAll(".import-pick").forEach(b => {
      const was = picks.find(([v]) => v === (b.value || b.dataset.paths));
      if (was) b.checked = was[1];
    });
    updateImportCount();
  }));
  body.onchange = updateImportCount;
  setImportStep("review", { next: "Add", back: importWizard.folderPlan?.subfolders.length > 0 });
  updateImportCount();
}

function pickedImportPaths() {
  return [...document.querySelectorAll("#import-body .import-pick:checked")].flatMap(box =>
    box.dataset.paths ? JSON.parse(box.dataset.paths) : [box.value]);
}

/** The page groups to keep together, as the server wants them: full paths, in page order. */
function pickedImportGroups() {
  const picked = new Set(pickedImportPaths());
  return (importWizard.plan?.groups || [])
    .filter(g => importWizard.combine[g.key] && g.paths.some(p => picked.has(p)))
    .map(g => g.paths);
}

/** How many records the picked scans will make, pages counted into their item. */
function importItemCount() {
  const picked = new Set(pickedImportPaths());
  let items = picked.size;
  for (const g of importWizard.plan?.groups || []) {
    if (!importWizard.combine[g.key]) continue;
    const n = g.paths.filter(p => picked.has(p)).length;
    if (n) items -= g.in_archive ? n : n - 1;
  }
  return items;
}

function updateImportCount() {
  const plan = importWizard.plan;
  if (!plan || importWizard.step !== "review") return;
  const n = pickedImportPaths().length;
  const btn = document.getElementById("btn-import-next");
  btn.textContent = n ? `Add ${plural(n, "scan")}` : "Add";
  btn.disabled = n === 0;
  const headline = document.getElementById("import-headline");
  if (headline) {
    headline.textContent = !plan.new.length && !plan.duplicates.length
      ? "Every scan here is already in the archive."
      : !plan.new.length && !n
        ? "Nothing new here: the rest are copies of scans you have. Tick one to add it anyway."
        : n ? (importItemCount() !== n
                ? `${plural(n, "scan")} will be added as ${plural(importItemCount(), "item")}, pages kept together, and sorted into documents and photographs.`
                : `${plural(n, "scan")} will be added and sorted into documents and photographs.`)
            : "Nothing is ticked.";
  }
}

async function advanceImport() {
  const btn = document.getElementById("btn-import-next");
  if (importWizard.step === "done") {
    // "Review them": what was just added, undecided ones first.
    closeImportWizard();
    switchView("view-database");
    document.querySelector('#db-review-pills [data-review="todo"]')?.click();
    return;
  }
  if (importWizard.step === "scope") {
    const all = document.querySelector('input[name="import-scope"]:checked')?.value === "all";
    if (!all) return renderImportReview(importWizard.folderPlan);
    btn.disabled = true;
    document.getElementById("import-body").innerHTML = '<p class="archive-status">Looking through the subfolders…</p>';
    try {
      renderImportReview(await fetchImportPlan(true));
    } catch (err) {
      document.getElementById("import-body").innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
    }
    return;
  }
  if (importWizard.step === "review") {
    const paths = pickedImportPaths();
    if (!paths.length) return;
    btn.disabled = true;
    try {
      const res = await fetch("/api/import/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: importWizard.plan.folder, paths, groups: pickedImportGroups() }),
      });
      if (!res.ok) throw new Error(await responseError(res, "The scans could not be added."));
      importWizard.jobId = (await res.json()).job.id;
    } catch (err) {
      btn.disabled = false;
      document.getElementById("import-body").insertAdjacentHTML("beforeend",
        `<p class="form-error">${escapeHtml(err.message)}</p>`);
      return;
    }
    renderImportProgress({ done_units: 0, total_units: paths.length, stage: "Starting", state: "queued" });
    startActivityPolling();
    watchImport();
  }
}

function renderImportProgress(job) {
  const total = job.total_units || 0, done = job.done_units || 0;
  document.getElementById("import-body").innerHTML = `
    <p class="archive-status">${job.state === "queued" ? "Waiting for the page being read to finish…"
      : `Adding scans… ${done} of ${total}`}</p>
    <div class="activity-progress"><div class="activity-progress-fill" style="width:${total ? Math.round(done / total * 100) : 0}%"></div></div>
    <p class="field-note">${escapeHtml(job.stage || "")}</p>
    <p class="field-note">You can close this; it carries on, and Activity shows how far it has got.</p>`;
  setImportStep("run", { next: null, cancel: "Stop" });
}

function watchImport() {
  clearInterval(importWizard.timer);
  importWizard.timer = setInterval(async () => {
    let data;
    try { data = await (await fetch("/api/jobs")).json(); } catch { return; }
    const id = importWizard.jobId;
    const job = [data.active, ...(data.queued || []), ...(data.recent || [])].find(j => j && j.id === id);
    if (!job) return;
    if (job.state === "queued" || job.state === "running") {
      if (importWizard.step === "run") renderImportProgress(job);
      return;
    }
    clearInterval(importWizard.timer);
    importWizard.jobId = null;
    await loadDocumentArchive();
    loadDirectory(currentFsPath, 0, currentFsQuery, currentFsFilterStatus);
    renderImportSummary(job);
  }, 700);
}

function importSummaryText(r) {
  const bits = [];
  if (r.documents) bits.push(plural(r.documents, "document"));
  if (r.photographs) bits.push(plural(r.photographs, "photograph"));
  if (r.undecided) bits.push(`${r.undecided} undecided`);
  if (r.unsorted) bits.push(`${r.unsorted} not sorted`);
  const items = r.pages ? ` as ${plural(r.items || 0, "item")}` : "";
  return `Added ${plural(r.added || 0, "scan")}${items}${bits.length ? `: ${bits.join(" · ")}` : ""}`;
}

function renderImportSummary(job) {
  const modal = document.getElementById("modal-import");
  if (modal.style.display === "none") {
    showToast(`${importSummaryText(job.result || {})}.`, job.state === "failed" ? "error" : "done");
    return;
  }
  const r = job.result || {};
  const stopped = job.state === "cancelled";
  document.getElementById("import-body").innerHTML = job.state === "failed"
    ? `<p class="form-error">Adding stopped: ${escapeHtml(job.error || "unknown error")}</p>`
    : `
      <p class="archive-status">${stopped ? "Stopped. " : ""}${escapeHtml(importSummaryText(r))}.</p>
      ${r.undecided ? `<p class="field-note">${r.undecided === 1
        ? "One scan could not be told apart as text or photograph. It is filed as a document and comes"
        : `${r.undecided} scans could not be told apart as text or photograph. They are filed as documents and come`}
        first under To review, marked “Kind?”.</p>` : ""}
      ${r.already ? `<p class="field-note">${plural(r.already, "scan was", "scans were")} added meanwhile and skipped.</p>` : ""}
      ${(r.failed || []).length ? `<p class="form-error">${plural(r.failed.length, "scan")} could not be added:</p>
        <div class="import-list">${r.failed.map(f =>
          `<div class="import-row is-plain"><span class="import-name">${escapeHtml(f.name)}</span>
           <span class="import-note">${escapeHtml(f.reason)}</span></div>`).join("")}</div>` : ""}`;
  setImportStep("run", { next: r.added ? "Review them" : null, cancel: "Close" });
  importWizard.step = "done";
}

async function cancelImportStep() {
  if (importWizard.step === "run" && importWizard.jobId) {
    await fetch(`/api/jobs/${importWizard.jobId}/cancel`, { method: "POST" }).catch(() => {});
    return;           // the watcher shows the summary of what was added
  }
  closeImportWizard();
}

function closeImportWizard() {
  document.getElementById("modal-import").style.display = "none";
  // A running import carries on; its summary arrives as a toast.
}

// ============================================================================
// Activity panel — a live view of the OCR queue, in place of a blocking modal
// ============================================================================

let activityTimer = null;

async function enqueueJobs(payload) {
  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    pollActivity();
    startActivityPolling();
    return await res.json();
  } catch (err) {
    console.error("Could not queue OCR job:", err);
    return null;
  }
}

function startActivityPolling() {
  if (activityTimer) return;
  activityTimer = setInterval(pollActivity, 1000);
}

function stopActivityPolling() {
  clearInterval(activityTimer);
  activityTimer = null;
}

async function pollActivity() {
  let data;
  try {
    data = await (await fetch("/api/jobs")).json();
  } catch {
    return;
  }

  const panel = document.getElementById("activity-panel");
  const current = document.getElementById("activity-current");
  const idle = !data.active && !data.queued_count;
  if (panel) panel.hidden = idle && !(data.recent || []).length;

  if (data.active) {
    current.hidden = false;
    document.getElementById("activity-file").textContent = data.active.label;
    document.getElementById("activity-elapsed").textContent = `${Math.round(data.active.elapsed)}s`;

    const { done_units: done, total_units: total, stage } = data.active;
    document.getElementById("activity-stage").textContent =
      total ? `${stage} ${done}/${total}` : stage;

    // Indeterminate until the refinement pass reports a denominator, rather
    // than inventing a percentage the way the old modal did.
    const fill = document.getElementById("activity-progress-fill");
    if (total > 0) {
      fill.classList.remove("is-indeterminate");
      fill.style.width = `${Math.round((done / total) * 100)}%`;
    } else {
      fill.classList.add("is-indeterminate");
      fill.style.width = "100%";
    }
  } else if (current) {
    current.hidden = true;
  }

  const queued = document.getElementById("activity-queued");
  if (queued) {
    queued.hidden = !data.queued_count;
    queued.textContent = data.queued_count
      ? `${data.queued_count} more waiting` : "";
  }

  renderRecentJobs(data.recent || []);
  if (idle) stopActivityPolling();
}

function renderRecentJobs(recent) {
  const list = document.getElementById("activity-recent");
  if (!list) return;

  list.innerHTML = recent.slice(0, 5).map(j => {
    const r = j.result || {};
    let detail;
    if (r.kind === "import") {
      detail = importSummaryText(r) + (j.state === "cancelled" ? " (stopped)" : "");
    } else if (j.state === "done") {
      const bits = [`${r.characters || 0} chars`];
      if (r.refined_lines) bits.push(`${r.refined_lines} re-read`);
      if (r.dropped_noise_lines) bits.push(`${r.dropped_noise_lines} noise dropped`);
      detail = bits.join(" · ");
    } else {
      detail = j.error || j.stage;
    }
    return `
      <li class="activity-item is-${j.state}">
        <span class="activity-item-dot"></span>
        <span class="activity-item-body">
          <span class="activity-item-name" title="${escapeHtml(j.label)}">${escapeHtml(j.label)}</span>
          <span class="activity-item-detail">${escapeHtml(detail)}</span>
        </span>
      </li>`;
  }).join("");

  // A finished ingest means the explorer's index badges are now stale.
  const finishedIds = recent.filter(j => j.state === "done").map(j => j.id).join(",");
  if (finishedIds && finishedIds !== renderRecentJobs._last) {
    renderRecentJobs._last = finishedIds;
    loadDocumentArchive();
  }
}

function setupActivityPanel() {
  document.getElementById("btn-cancel-all-jobs")?.addEventListener("click", async () => {
    await fetch("/api/jobs/cancel-all", { method: "POST" });
    pollActivity();
  });

  document.getElementById("btn-cancel-active")?.addEventListener("click", async () => {
    const data = await (await fetch("/api/jobs")).json();
    if (data.active) {
      await fetch(`/api/jobs/${data.active.id}/cancel`, { method: "POST" });
      pollActivity();
    }
  });

  // Pick up work already running from an earlier page load.
  pollActivity().then(startActivityPolling);
}

// ============================================================================
// Scan viewer: pan, zoom, click-to-zoom and image filters
// ============================================================================
function enableSmoothTransition() {
  const container = document.getElementById("panzoom-container");
  if (container) {
    container.classList.add("smooth-transform");
    clearTimeout(smoothTransformTimer);
    smoothTransformTimer = setTimeout(() => {
      container.classList.remove("smooth-transform");
    }, 240);
  }
}

function disableSmoothTransition() {
  const container = document.getElementById("panzoom-container");
  if (container) {
    clearTimeout(smoothTransformTimer);
    container.classList.remove("smooth-transform");
  }
}

function clampPan() {
  const viewport = document.getElementById("viewport");
  const img = document.getElementById("scan-image");
  if (!viewport || !img || !img.naturalWidth) return;

  const vpW = viewport.clientWidth;
  const vpH = viewport.clientHeight;
  const imgW = img.naturalWidth * scale;
  const imgH = img.naturalHeight * scale;

  // Horizontal clamping: lock centered if smaller than viewport
  if (imgW <= vpW) {
    panX = Math.round((vpW - imgW) / 2);
  } else {
    const padX = 24;
    const minX = vpW - imgW - padX;
    const maxX = padX;
    panX = Math.min(maxX, Math.max(minX, panX));
  }

  // Vertical clamping: lock centered if smaller than viewport
  if (imgH <= vpH) {
    panY = Math.round((vpH - imgH) / 2);
  } else {
    const padY = 24;
    const minY = vpH - imgH - padY;
    const maxY = padY;
    panY = Math.min(maxY, Math.max(minY, panY));
  }
}

function updateZoomUI() {
  const badge = document.getElementById("zoom-level-badge");
  if (badge) {
    badge.textContent = `${Math.round(scale * 100)}%`;
  }
  const viewport = document.getElementById("viewport");
  if (viewport) {
    const isZoomed = scale > (cachedFitScale || 0.5) * 1.35;
    viewport.classList.toggle("is-zoomed", isZoomed);
  }

  const btnFitPage = document.getElementById("zoom-fit-page");
  const btnFitWidth = document.getElementById("zoom-fit-width");
  const btnReset = document.getElementById("zoom-reset");

  if (btnFitPage && cachedFitScale) {
    btnFitPage.classList.toggle("active", Math.abs(scale - cachedFitScale) < 0.02);
  }
  if (btnReset) {
    btnReset.classList.toggle("active", Math.abs(scale - 1.0) < 0.02);
  }
}

function applyPanzoomTransform() {
  const container = document.getElementById("panzoom-container");
  if (container) {
    container.style.transform = `translate(${Math.round(panX)}px, ${Math.round(panY)}px) scale(${scale})`;
  }
  updateZoomUI();
}

function fitToViewport(mode = "contain", smooth = true) {
  const viewport = document.getElementById("viewport");
  const img = document.getElementById("scan-image");
  if (!viewport || !img || !img.naturalWidth || !img.naturalHeight) return;

  const vpW = viewport.clientWidth;
  const vpH = viewport.clientHeight;
  if (vpW <= 0 || vpH <= 0) return;

  const padding = 20;
  const availW = Math.max(50, vpW - padding * 2);
  const availH = Math.max(50, vpH - padding * 2);

  if (mode === "width") {
    const targetScale = Math.min(availW / img.naturalWidth, 4.0);
    scale = targetScale;
    panX = Math.round((vpW - img.naturalWidth * scale) / 2);
    panY = padding; // Top-aligned so user can scroll down naturally to read
  } else {
    // "contain" / Fit Page
    const scaleW = availW / img.naturalWidth;
    const scaleH = availH / img.naturalHeight;
    const targetScale = Math.min(scaleW, scaleH);
    scale = targetScale;
    panX = Math.round((vpW - img.naturalWidth * scale) / 2);
    panY = Math.round((vpH - img.naturalHeight * scale) / 2);
    cachedFitScale = targetScale;
  }

  if (smooth) {
    enableSmoothTransition();
  } else {
    disableSmoothTransition();
  }
  applyPanzoomTransform();
}

function setPanzoomScale(next, originX, originY, smooth = false) {
  const clamped = Math.min(8, Math.max(0.05, next));
  const viewport = document.getElementById("viewport");
  if (viewport && originX !== undefined && originY !== undefined) {
    const rect = viewport.getBoundingClientRect();
    const cx = originX - rect.left;
    const cy = originY - rect.top;
    panX = cx - ((cx - panX) * clamped) / scale;
    panY = cy - ((cy - panY) * clamped) / scale;
  } else if (viewport) {
    const cx = viewport.clientWidth / 2;
    const cy = viewport.clientHeight / 2;
    panX = cx - ((cx - panX) * clamped) / scale;
    panY = cy - ((cy - panY) * clamped) / scale;
  }
  scale = clamped;
  clampPan();
  if (smooth) {
    enableSmoothTransition();
  } else {
    disableSmoothTransition();
  }
  applyPanzoomTransform();
}

function resetPanzoom() {
  fitToViewport("contain", true);
}

function handleViewportClick(e) {
  if (e.target && e.target.closest && (e.target.closest(".viewport-overlay") || e.target.closest("button"))) return;
  const img = document.getElementById("scan-image");
  if (!img || !img.naturalWidth || img.style.display === "none") return;
  const viewport = document.getElementById("viewport");
  if (!viewport) return;

  // Dismiss hint badge
  const hint = document.getElementById("viewport-hint");
  if (hint) hint.classList.add("fade-out");

  const isZoomedIn = scale > (cachedFitScale || 0.5) * 1.35;

  if (e.altKey || e.shiftKey || isZoomedIn) {
    // Zoom out back to full document overview
    fitToViewport("contain", true);
  } else {
    // Zoom in centered directly on clicked point!
    const rect = viewport.getBoundingClientRect();
    const clickVpX = e.clientX - rect.left;
    const clickVpY = e.clientY - rect.top;

    // Original image coordinates of the clicked point
    const imgX = (clickVpX - panX) / scale;
    const imgY = (clickVpY - panY) / scale;

    // High clarity reading zoom level: 2.5x fit or at least 1.5, up to 4.0
    const targetScale = Math.min(4.0, Math.max(1.5, (cachedFitScale || 1.0) * 2.5));

    // Center this clicked point into the viewport
    panX = Math.round((viewport.clientWidth / 2) - (imgX * targetScale));
    panY = Math.round((viewport.clientHeight / 2) - (imgY * targetScale));
    scale = targetScale;

    clampPan();
    enableSmoothTransition();
    applyPanzoomTransform();
  }
}

function setupPanzoom() {
  const viewport = document.getElementById("viewport");
  if (!viewport) return;

  // Toolbar zoom buttons
  document.getElementById("zoom-in")?.addEventListener("click", () => {
    setPanzoomScale(scale * 1.25, undefined, undefined, true);
  });
  document.getElementById("zoom-out")?.addEventListener("click", () => {
    setPanzoomScale(scale / 1.25, undefined, undefined, true);
  });
  document.getElementById("zoom-fit-page")?.addEventListener("click", () => {
    fitToViewport("contain", true);
  });
  document.getElementById("zoom-fit-width")?.addEventListener("click", () => {
    fitToViewport("width", true);
  });
  document.getElementById("zoom-reset")?.addEventListener("click", () => {
    setPanzoomScale(1.0, undefined, undefined, true);
  });

  // Natural scroll to move, or Ctrl/Cmd + Wheel to zoom
  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    disableSmoothTransition();

    const hint = document.getElementById("viewport-hint");
    if (hint) hint.classList.add("fade-out");

    // Trackpad pinch-to-zoom sends ctrlKey = true, or user holding Ctrl / Cmd
    if (e.ctrlKey || e.metaKey) {
      const factor = e.deltaY < 0 ? 1.1 : (1 / 1.1);
      setPanzoomScale(scale * factor, e.clientX, e.clientY, false);
      return;
    }

    // Natural document scrolling!
    let dx = e.deltaX;
    let dy = e.deltaY;
    if (e.shiftKey && !dx) {
      dx = dy;
      dy = 0;
    }

    panX -= dx;
    panY -= dy;
    clampPan();
    applyPanzoomTransform();
  }, { passive: false });

  // Disallow native drag-and-drop on viewport & image completely
  viewport.addEventListener("dragstart", (e) => {
    e.preventDefault();
    return false;
  });

  // Mouse interaction: Click to Zoom (or scroll/drag when zoomed in)
  viewport.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); // Stop native image drag & text selection
    isMouseDown = true;
    hasDragged = false;
    mouseDownX = e.clientX;
    mouseDownY = e.clientY;
    startX = e.clientX - panX;
    startY = e.clientY - panY;
    disableSmoothTransition();
  });

  window.addEventListener("mousemove", (e) => {
    if (!isMouseDown) return;
    const img = document.getElementById("scan-image");
    if (!img || !img.naturalWidth) return;

    const dx = e.clientX - mouseDownX;
    const dy = e.clientY - mouseDownY;

    // Only allow drag-panning if the document is zoomed larger than the viewport
    const canPan = (img.naturalWidth * scale > viewport.clientWidth) ||
                   (img.naturalHeight * scale > viewport.clientHeight);

    if (canPan && !hasDragged && Math.hypot(dx, dy) > 5) {
      hasDragged = true;
      viewport.classList.add("is-dragging");
    }

    if (hasDragged && canPan) {
      panX = e.clientX - startX;
      panY = e.clientY - startY;
      clampPan();
      applyPanzoomTransform();
    }
  });

  window.addEventListener("mouseup", (e) => {
    if (!isMouseDown) return;
    isMouseDown = false;
    viewport.classList.remove("is-dragging");

    // If pointer stayed within 5px threshold (or cannot pan), treat as a clean click to zoom!
    if (!hasDragged && e.target && (viewport === e.target || viewport.contains(e.target))) {
      handleViewportClick(e);
    }
  });

  // Double click toggles fit page or fit width
  viewport.addEventListener("dblclick", (e) => {
    e.preventDefault();
    const isFit = Math.abs(scale - (cachedFitScale || 1.0)) < 0.02;
    fitToViewport(isFit ? "width" : "contain", true);
  });

  // Window resize refits if currently at fit scale
  window.addEventListener("resize", () => {
    const isFit = cachedFitScale && Math.abs(scale - cachedFitScale) < 0.05;
    if (isFit) {
      fitToViewport("contain", false);
    }
  });

  // Auto-dismiss hint after 4 seconds
  setTimeout(() => {
    const hint = document.getElementById("viewport-hint");
    if (hint) hint.classList.add("fade-out");
  }, 4000);
}

function setupFilterControls() {
  const img = document.getElementById("scan-image");
  const buttons = {
    "filter-raw": "",
    "filter-contrast": "filter-contrast",
    "filter-invert": "filter-invert",
  };

  Object.entries(buttons).forEach(([id, cls]) => {
    document.getElementById(id)?.addEventListener("click", () => {
      if (!img) return;
      img.classList.remove("filter-contrast", "filter-invert");
      if (cls) img.classList.add(cls);
      Object.keys(buttons).forEach(other =>
        document.getElementById(other)?.classList.toggle("active", other === id));
    });
  });
}

// ============================================================================
// Polytonic character palette
// ============================================================================
function setupDiacriticPalette() {
  const palette = document.getElementById("diacritic-palette");
  const textarea = document.getElementById("transcription-text");
  if (!palette || !textarea) return;

  palette.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-diacritic");
    if (!btn) return;
    const ch = btn.getAttribute("data-char");
    if (!ch) return;

    // Insert at the caret and leave the cursor after it, so a reviewer can type
    // a run of accented characters without reaching for the mouse each time.
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? start;
    textarea.value = textarea.value.slice(0, start) + ch + textarea.value.slice(end);
    textarea.focus();
    textarea.selectionStart = textarea.selectionEnd = start + ch.length;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

// ============================================================================
// Keyboard shortcuts
// ============================================================================
function setupKeyboardShortcuts() {
  document.addEventListener("keydown", (e) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);

    // Cmd/Ctrl+Enter verifies and saves, and must work from inside the
    // transcription box — that is where the reviewer is when they finish.
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      document.getElementById("btn-done")?.click();
      return;
    }

    if (e.altKey && (e.key === "s" || e.key === "S")) {
      e.preventDefault();
      document.getElementById("app-sidebar")?.classList.toggle("collapsed");
      return;
    }

    if (typing) return;

    if ((e.key === "[" || e.key === "]") && document.getElementById("view-curation")?.classList.contains("active")) {
      e.preventDefault();
      goToPage(currentPage + (e.key === "]" ? 1 : -1));
      return;
    }

    if (e.key === "+" || e.key === "=") {
      e.preventDefault();
      setPanzoomScale(scale * 1.25, undefined, undefined, true);
    } else if (e.key === "-" || e.key === "_") {
      e.preventDefault();
      setPanzoomScale(scale / 1.25, undefined, undefined, true);
    } else if (e.key === "0") {
      e.preventDefault();
      fitToViewport("contain", true);
    } else if (e.altKey && e.key === "ArrowLeft") {
      e.preventDefault();
      document.getElementById("btn-prev-doc")?.click();
    } else if (e.altKey && e.key === "ArrowRight") {
      e.preventDefault();
      document.getElementById("btn-next-doc")?.click();
    } else if (e.key === "Escape") {
      // A locked dialog is mid-operation (opening an archive) and must stay up.
      document.querySelectorAll(".modal-backdrop:not([data-locked])").forEach(m => { m.style.display = "none"; });
    }
  });
}

// ============================================================================
// VIEW 5: Pipeline Settings & OCR Tuning Bench
// ============================================================================

const OCR_INPUT_LABELS = {
  ocr_ready: "Grayscale, contrast-adjusted and denoised — recommended",
  enhanced:  "Grayscale with contrast adjustment only",
  gray:      "Plain grayscale, no enhancement",
  binarized: "Pure black and white — measured far worse on faded typescript",
};

let settingsState = null;

const SETTING_CONTROLS = [
  ["set-model-set",        "model_set",          "value"],
  ["set-default-lang",     "default_lang",       "value"],
  ["set-ocr-input",        "ocr_input",          "value"],
  ["set-clahe",            "clahe_clip_limit",   "number"],
  ["set-deskew",           "deskew_enabled",     "checked"],
  ["set-deadband",         "deskew_deadband",    "number"],
  ["set-refine",           "refine_enabled",     "checked"],
  ["set-refine-threshold", "refine_threshold",   "number"],
  ["set-refine-margin",    "refine_margin",      "number"],
  ["set-page-size",        "explorer_page_size", "number"],
  ["set-export-quality",   "export_quality",     "value"],
];

// What each export quality writes beside the page. The page itself always
// carries a preview of every scan; this is what the inspector opens.
const EXPORT_QUALITY_LABELS = {
  original: "Every scan is copied exactly as scanned; nothing is lost. The export is as large as the scans themselves.",
  high:     "Every scan at full resolution as a high-quality JPEG: indistinguishable on screen, several times smaller. Originals already in JPEG are copied as they are.",
  compact:  "Scans at 2400 px on the long side, and smaller previews in the page: still readable, at about a twentieth of the size of the originals.",
};

const RANGE_OUTPUTS = {
  "set-clahe": "out-clahe",
  "set-deadband": "out-deadband",
  "set-refine-threshold": "out-refine-threshold",
  "set-refine-margin": "out-refine-margin",
  "set-page-size": "out-page-size",
};

async function loadSettingsView() {
  const data = await (await fetch("/api/settings")).json();
  settingsState = data.settings;

  const modelSel = document.getElementById("set-model-set");
  if (modelSel && !modelSel.options.length) {
    (data.model_sets || []).forEach(m => {
      const o = document.createElement("option");
      o.value = m.name;
      o.textContent = `${m.name} — ${m.label} (${m.languages.join(", ")})`;
      modelSel.appendChild(o);
    });
    if (!modelSel.options.length) {
      modelSel.innerHTML = '<option value="tessdata">tessdata (none found)</option>';
    }
  }

  const inputSel = document.getElementById("set-ocr-input");
  if (inputSel && !inputSel.options.length) {
    (data.ocr_inputs || []).forEach(k => {
      const o = document.createElement("option");
      o.value = k;
      o.textContent = k;
      inputSel.appendChild(o);
    });
  }

  applySettingsToForm(settingsState);
}

function applySettingsToForm(s) {
  SETTING_CONTROLS.forEach(([id, key, kind]) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (kind === "checked") el.checked = !!s[key];
    else el.value = s[key];
  });
  Object.entries(RANGE_OUTPUTS).forEach(([i, o]) => syncRangeOutput(i, o));
  updateSettingsHints();
}

function syncRangeOutput(inputId, outId) {
  const input = document.getElementById(inputId);
  const out = document.getElementById(outId);
  if (input && out) out.textContent = input.value;
}

function updateSettingsHints() {
  const inputSel = document.getElementById("set-ocr-input");
  const hint = document.getElementById("hint-ocr-input");
  if (inputSel && hint) hint.textContent = OCR_INPUT_LABELS[inputSel.value] || "";

  const exportSel = document.getElementById("set-export-quality");
  const exportHint = document.getElementById("hint-export-quality");
  if (exportSel && exportHint) exportHint.textContent = EXPORT_QUALITY_LABELS[exportSel.value] || "";

  const refine = document.getElementById("set-refine");
  const fields = document.getElementById("refine-fields");
  if (refine && fields) fields.classList.toggle("is-disabled", !refine.checked);
}

function collectSettingsFromForm() {
  const out = {};
  SETTING_CONTROLS.forEach(([id, key, kind]) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (kind === "checked") out[key] = el.checked;
    else if (kind === "number") out[key] = parseFloat(el.value);
    else out[key] = el.value;
  });
  return out;
}

// The same setting, one click from Recompile Timeline. Saved on change, like a
// preference, since what it governs happens only on the next export.
async function syncQuickExportQuality() {
  const quick = document.getElementById("timeline-export-quality");
  if (!quick) return;
  if (!settingsState) {
    try { settingsState = (await (await fetch("/api/settings")).json()).settings; } catch (_) { return; }
  }
  quick.value = settingsState.export_quality || "original";
}

function setupQuickExportQuality() {
  const quick = document.getElementById("timeline-export-quality");
  if (!quick) return;
  syncQuickExportQuality();
  quick.addEventListener("change", async () => {
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ export_quality: quick.value }),
      });
      if (!res.ok) throw new Error(res.statusText);
      settingsState = (await res.json()).settings;
      const full = document.getElementById("set-export-quality");
      if (full) { full.value = settingsState.export_quality; updateSettingsHints(); }
      showToast(`Exports will use ${quick.options[quick.selectedIndex].text.toLowerCase()} scans. Recompile to apply.`);
    } catch (err) {
      showToast("Could not save the export quality.", "error");
      syncQuickExportQuality();
    }
  });
}

const BENCH_VARIANTS = {
  current: () => [{}],
  models:  () => [{ model_set: "tessdata" }, { model_set: "tessdata_best" }],
  passes:  () => [{ refine_enabled: false }, { refine_enabled: true }],
  inputs:  () => [{ ocr_input: "ocr_ready" }, { ocr_input: "binarized" }],
};

function setupSettingsView() {
  SETTING_CONTROLS.forEach(([id]) => {
    document.getElementById(id)?.addEventListener("input", () => {
      if (RANGE_OUTPUTS[id]) syncRangeOutput(id, RANGE_OUTPUTS[id]);
      updateSettingsHints();
      const note = document.getElementById("settings-save-note");
      if (note) note.textContent = "Unsaved changes";
    });
  });

  document.getElementById("btn-save-settings")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectSettingsFromForm()),
      });
      settingsState = (await res.json()).settings;
      applySettingsToForm(settingsState);
      syncQuickExportQuality();
      const note = document.getElementById("settings-save-note");
      if (note) note.textContent = "Saved";
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("btn-reset-settings")?.addEventListener("click", async () => {
    const res = await fetch("/api/settings/reset", { method: "POST" });
    settingsState = (await res.json()).settings;
    applySettingsToForm(settingsState);
    syncQuickExportQuality();
    const note = document.getElementById("settings-save-note");
    if (note) note.textContent = "Defaults restored";
  });

  setupQuickExportQuality();

  document.getElementById("btn-bench-use-current")?.addEventListener("click", () => {
    const input = document.getElementById("bench-path");
    if (input && currentDoc) input.value = currentDoc.file_path || "";
  });

  document.getElementById("btn-run-bench")?.addEventListener("click", runBenchmark);
}

async function runBenchmark() {
  const btn = document.getElementById("btn-run-bench");
  const status = document.getElementById("bench-status");
  const out = document.getElementById("bench-results");
  const filePath = document.getElementById("bench-path")?.value.trim();

  if (!filePath) {
    out.innerHTML = '<p class="bench-empty">Choose a scan to test first.</p>';
    return;
  }

  const kind = document.getElementById("bench-variants")?.value || "current";
  btn.disabled = true;
  status.textContent = "Running...";
  out.innerHTML = '<p class="bench-empty">Running OCR — the two-pass comparison can take a minute.</p>';

  try {
    const res = await fetch("/api/settings/benchmark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_path: filePath,
        reference_text: document.getElementById("bench-reference")?.value || null,
        variants: BENCH_VARIANTS[kind](),
      }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    renderBenchResults(await res.json());
    status.textContent = "Done";
  } catch (err) {
    out.innerHTML = `<p class="bench-empty bench-error">${err.message}</p>`;
    status.textContent = "Failed";
  } finally {
    btn.disabled = false;
  }
}

function renderBenchResults(data) {
  const out = document.getElementById("bench-results");
  const scored = data.results.filter(r => typeof r.cer === "number");
  const best = scored.length ? Math.min(...scored.map(r => r.cer)) : null;

  out.innerHTML = data.results.map(r => {
    if (r.error) {
      return `<div class="bench-result"><div class="bench-result-head">
                <span class="bench-label">${r.label}</span>
                <span class="bench-error">${r.error}</span></div></div>`;
    }
    const isBest = best !== null && r.cer === best && data.results.length > 1;
    const metric = typeof r.cer === "number"
      ? `<span class="bench-metric ${isBest ? "is-best" : ""}">${r.cer}% error</span>`
      : `<span class="bench-metric">conf ${(r.confidence * 100).toFixed(0)}%</span>`;
    return `
      <div class="bench-result ${isBest ? "is-best" : ""}">
        <div class="bench-result-head">
          <span class="bench-label">${r.label}</span>
          ${metric}
        </div>
        <div class="bench-stats">
          <span>${r.seconds}s</span>
          <span>${r.lines} lines</span>
          <span>${r.refined_lines} re-read</span>
          <span>tilt ${r.deskew_angle}&deg;</span>
          <span>${r.characters} chars</span>
        </div>
        <pre class="bench-preview">${(r.transcription || "").slice(0, 600)}</pre>
      </div>`;
  }).join("");
}

// ============================================================================
// Session: where the curator is
//
// Kept by the server rather than in this tab, so it survives a restart and
// travels inside a saved .archivesys file. Opening one lands the curator back
// on the same view, the same record, the same filters -- and the same spot on
// the page they were transcribing.
// ============================================================================
let sessionTracking = false;
let sessionListening = false;
let sessionTimer = null;
let lastSessionJson = "";
let lastViewerState = null;       // zoom and position on the open scan
let pendingViewerRestore = null;  // the saved one, until the scan can take it

const SESSION_CHOICES = {
  review: ["all", "todo", "done"],
  group: ["none", "person", "kind", "decade"],
  match: ["any", "all"],
  mode: ["grid", "list"],
  fsFilter: ["all", "unindexed", "indexed"],
  peopleSort: ["name", "documents", "earliest", "lifespan"],
  viewerFilter: ["filter-raw", "filter-contrast", "filter-invert"],
};

async function loadSession() {
  try {
    const res = await fetch("/api/session");
    if (!res.ok) return {};
    const data = await res.json();
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch {
    return {};
  }
}

function captureViewerState() {
  // Until a saved position has been put back it is still the position:
  // measuring the viewer in between would record a plain "fit page" over it.
  if (pendingViewerRestore && pendingViewerRestore.doc_id === currentDoc?.id) return lastViewerState;
  const img = document.getElementById("scan-image");
  const viewport = document.getElementById("viewport");
  // Zoom is kept relative to "fit page", and position as the point of the page
  // at the centre of the viewer: both survive a different window or screen.
  if (currentDoc && viewport?.clientWidth > 0 && img?.naturalWidth && cachedFitScale) {
    lastViewerState = {
      doc_id: currentDoc.id,
      zoom: +(scale / cachedFitScale).toFixed(4),
      cx: +(((viewport.clientWidth / 2) - panX) / scale / img.naturalWidth).toFixed(4),
      cy: +(((viewport.clientHeight / 2) - panY) / scale / img.naturalHeight).toFixed(4),
      scroll: Math.round(document.getElementById("record-scroll")?.scrollTop || 0),
    };
  }
  if (lastViewerState && lastViewerState.doc_id !== currentDoc?.id) lastViewerState = null;
  return lastViewerState;
}

function captureSession() {
  const active = document.querySelector(".studio-view.active");
  const split = parseFloat(document.querySelector(".curation-layout")
    ?.style.getPropertyValue("--curation-viewer-width"));
  return {
    view: active ? active.id.replace(/^view-/, "") : "explorer",
    doc_id: currentDoc ? currentDoc.id : null,
    person_id: currentPerson ? currentPerson.id : null,
    catalog: {
      search: document.getElementById("db-search")?.value || "",
      review: catalogReviewFilter,
      kind: catalogKindFilter,
      group: catalogGroupMode,
      people: catalogPeopleFilter,
      match: catalogPersonMatch,
      mode: catalogViewMode,
    },
    explorer: { path: currentFsPath, query: currentFsQuery, filter: currentFsFilterStatus },
    people: { sort: peopleSort, search: peopleSearch },
    layout: {
      sidebar_collapsed: Boolean(document.getElementById("app-sidebar")?.classList.contains("collapsed")),
      split: Number.isFinite(split) ? split : null,
    },
    viewer: {
      filter: document.querySelector(".filter-controls .btn-tool.active")?.id || "filter-raw",
      ...(captureViewerState() || {}),
    },
  };
}

function setActivePill(containerId, attr, value) {
  document.querySelectorAll(`#${containerId} .pill`).forEach(p =>
    p.classList.toggle("active", p.getAttribute(attr) === value));
}

/** Everything that does not depend on data from the server. Values are checked,
 *  not trusted: an opened archive may have been saved by someone else. */
function applySessionBeforeLoad(session) {
  const pick = (value, allowed, fallback) => (allowed.includes(value) ? value : fallback);
  const text = (value) => (typeof value === "string" ? value : "");

  const cat = session.catalog || {};
  catalogReviewFilter = pick(cat.review, SESSION_CHOICES.review, "all");
  catalogGroupMode = pick(cat.group, SESSION_CHOICES.group, "none");
  catalogPersonMatch = pick(cat.match, SESSION_CHOICES.match, "any");
  catalogKindFilter = text(cat.kind) || "all";           // checked against /api/kinds once loaded
  catalogPeopleFilter = Array.isArray(cat.people) ? cat.people.filter(Number.isInteger) : [];
  setVal("db-search", text(cat.search));
  catalogSearchQuery = text(cat.search).trim().toLowerCase();
  const clearBtn = document.getElementById("btn-db-clear-search");
  if (clearBtn) clearBtn.style.display = catalogSearchQuery ? "block" : "none";
  setActivePill("db-review-pills", "data-review", catalogReviewFilter);
  setActivePill("db-group-pills", "data-group", catalogGroupMode);
  document.querySelectorAll("#db-person-match .match-opt").forEach(o =>
    o.classList.toggle("active", o.getAttribute("data-match") === catalogPersonMatch));
  if (pick(cat.mode, SESSION_CHOICES.mode, "grid") === "list") document.getElementById("btn-mode-list")?.click();

  const ex = session.explorer || {};
  currentFsPath = text(ex.path);
  currentFsQuery = text(ex.query);
  currentFsFilterStatus = pick(ex.filter, SESSION_CHOICES.fsFilter, "all");
  setVal("fs-search-input", currentFsQuery);
  setActivePill("fs-status-pills", "data-filter", currentFsFilterStatus);

  const ppl = session.people || {};
  peopleSort = pick(ppl.sort, SESSION_CHOICES.peopleSort, "name");
  peopleSearch = text(ppl.search);
  setVal("people-sort-select", peopleSort);
  setVal("people-search", peopleSearch);

  const lay = session.layout || {};
  document.getElementById("app-sidebar")?.classList.toggle("collapsed", lay.sidebar_collapsed === true);
  if (Number.isFinite(lay.split) && lay.split >= 20 && lay.split <= 80) {
    document.querySelector(".curation-layout")?.style.setProperty("--curation-viewer-width", `${lay.split}%`);
  }

  const viewer = session.viewer || {};
  if (SESSION_CHOICES.viewerFilter.includes(viewer.filter)) document.getElementById(viewer.filter)?.click();
  if (Number.isInteger(viewer.doc_id) && viewer.doc_id === session.doc_id) {
    pendingViewerRestore = {
      doc_id: viewer.doc_id, zoom: viewer.zoom, cx: viewer.cx, cy: viewer.cy, scroll: viewer.scroll,
    };
    lastViewerState = { ...pendingViewerRestore };
  }
}

/** Filters that name kinds or people can only be checked once those have loaded. */
function applySessionAfterLoad() {
  if (catalogKindFilter !== "all" && !allKinds.some(k => k.key === catalogKindFilter)) catalogKindFilter = "all";
  setActivePill("db-kind-pills", "data-kind", catalogKindFilter);
  catalogPeopleFilter = catalogPeopleFilter.filter(id => allPeopleRich.some(p => p.id === id));
  populatePersonFilterSelect();
  renderPersonFilterChips();
  renderPeopleGrid();
  renderDatabaseCatalog();
}

/** Put the scan back at the saved zoom and position, once it is loaded and visible. */
function applyPendingViewerRestore() {
  const saved = pendingViewerRestore;
  if (!saved || !currentDoc || saved.doc_id !== currentDoc.id) return;
  const img = document.getElementById("scan-image");
  const viewport = document.getElementById("viewport");
  if (!img?.naturalWidth || !viewport || viewport.clientWidth <= 0) return;  // try again when it is
  // The scan's own load handler re-fits the page; a position put back before
  // it runs would be undone, so wait until it has (it calls back in here).
  if (img.dataset.fittedDoc !== String(saved.doc_id)) return;
  pendingViewerRestore = null;

  fitToViewport("contain", false);            // establishes "fit page" for this window
  if (Number.isFinite(saved.zoom) && saved.zoom > 0) {
    const clamp01 = (v) => (Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0.5);
    scale = Math.min(8, Math.max(0.05, cachedFitScale * saved.zoom));
    panX = viewport.clientWidth / 2 - clamp01(saved.cx) * img.naturalWidth * scale;
    panY = viewport.clientHeight / 2 - clamp01(saved.cy) * img.naturalHeight * scale;
    clampPan();
    applyPanzoomTransform();
  }
  if (Number.isFinite(saved.scroll)) document.getElementById("record-scroll")?.scrollTo({ top: saved.scroll });
}

function persistSession() {
  if (!sessionTracking) return;
  clearTimeout(sessionTimer);
  sessionTimer = setTimeout(sendSession, 400);
}

function sendSession() {
  sessionTimer = null;
  if (!sessionTracking) return;
  const json = JSON.stringify(captureSession());
  if (json === lastSessionJson) return;
  lastSessionJson = json;
  fetch("/api/session", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: json, keepalive: true,
  }).catch(() => { lastSessionJson = ""; });
}

function startSessionTracking() {
  sessionTracking = true;
  if (!sessionListening) {
    sessionListening = true;
    // Any interaction may move the curator; a changed position is sent once
    // things settle, and an unchanged one not at all.
    ["click", "input", "change", "pointerup", "keyup"].forEach(type =>
      document.addEventListener(type, persistSession, true));
    document.addEventListener("wheel", persistSession, { capture: true, passive: true });
    window.addEventListener("pagehide", () => {
      if (!sessionTracking) return;
      const json = JSON.stringify(captureSession());
      // Sent as plain text: browsers may refuse a beacon with a JSON content
      // type, and the server reads the body as JSON either way.
      if (json !== lastSessionJson) navigator.sendBeacon("/api/session", json);
    });
  }
  persistSession();
}

function stopSessionTracking() {
  sessionTracking = false;
  clearTimeout(sessionTimer);
  sessionTimer = null;
}

// ============================================================================
// Saving and opening the whole archive (.archivesys)
//
// One file holds everything: every scan byte for byte, every record, and where
// you are right now. Opening it -- here or on another computer -- restores all
// three. Nothing on disk is ever deleted or overwritten by opening one, and
// what was open before is kept as a small .archivesys in data/backups.
// ============================================================================
let archiveUpload = null;      // the server's answer to an upload: token + summary
let archiveUploadXhr = null;
let archiveModalState = "closed";
let archiveDragDepth = 0;
let toastTimer = null;

function setupArchiveFile() {
  const input = document.getElementById("archive-file-input");
  document.getElementById("btn-save-archive")?.addEventListener("click", saveArchive);
  document.getElementById("btn-open-archive")?.addEventListener("click", () => input?.click());
  input?.addEventListener("change", () => {
    const file = input.files && input.files[0];
    input.value = "";                 // choosing the same file again must still fire
    if (file) openArchiveFile(file);
  });
  document.getElementById("btn-archive-confirm")?.addEventListener("click", confirmArchiveOpen);
  document.getElementById("btn-archive-cancel")?.addEventListener("click", cancelArchiveOpen);
  document.getElementById("btn-close-archive-modal")?.addEventListener("click", cancelArchiveOpen);
  document.getElementById("btn-archive-toast-close")?.addEventListener("click", hideToast);

  // Dropping a file on the page used to navigate the tab to it, taking the
  // session with it. Now an .archivesys opens, and anything else is declined.
  const overlay = document.getElementById("archive-drop-overlay");
  const carriesFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
  window.addEventListener("dragenter", (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    archiveDragDepth += 1;
    if (overlay) overlay.hidden = false;
  });
  window.addEventListener("dragover", (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("dragleave", (e) => {
    if (!carriesFiles(e)) return;
    archiveDragDepth = Math.max(0, archiveDragDepth - 1);
    if (!archiveDragDepth && overlay) overlay.hidden = true;
  });
  window.addEventListener("drop", (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    archiveDragDepth = 0;
    if (overlay) overlay.hidden = true;
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) openArchiveFile(file);
  });
}

function formatBytes(n) {
  if (!Number.isFinite(n) || n < 0) return "";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i += 1; }
  return i === 0 ? `${n} bytes` : `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

function showToast(message, tone = "done", ms = 9000) {
  const toast = document.getElementById("archive-toast");
  const text = document.getElementById("archive-toast-text");
  if (!toast || !text) return;
  text.textContent = message;
  toast.className = `toast is-${tone}`;
  toast.hidden = false;
  clearTimeout(toastTimer);
  if (tone !== "busy") toastTimer = setTimeout(hideToast, ms);
}

function hideToast() {
  const toast = document.getElementById("archive-toast");
  if (toast) toast.hidden = true;
}

async function saveArchive() {
  const btn = document.getElementById("btn-save-archive");
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "Saving…";
  showToast("Packing every scan and record into one file…", "busy");
  try {
    await flushPendingSave();         // the last few keystrokes belong in the file too
    const res = await fetch("/api/bundle/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: captureSession() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);

    const link = document.createElement("a");
    link.href = data.download_url;
    link.download = data.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();

    let message = `Saved “${data.filename}” — ${plural(data.documents, "document")}, `
      + `${plural(data.files, "scan")}, ${formatBytes(data.total_bytes)}. `
      + "Open it any time, here or on another computer, to come back to exactly this point.";
    if (data.missing?.length) {
      message += ` ${plural(data.missing.length, "scan")} could not be found and ${data.missing.length === 1 ? "is" : "are"} not inside.`;
    }
    if (data.jobs_pending) {
      message += ` ${plural(data.jobs_pending, "page")} still being read will not have ${data.jobs_pending === 1 ? "its" : "their"} text in this file.`;
    }
    showToast(message, data.missing?.length || data.jobs_pending ? "warn" : "done", 15000);
  } catch (err) {
    showToast(`Could not save the archive: ${err.message}`, "error", 15000);
  } finally {
    btn.disabled = false;
    btn.textContent = "Save archive";
  }
}

/** One dialog, four moments: reading the file, what it holds, opening, or why not. */
function renderArchiveModal(state, detail = {}) {
  archiveModalState = state;
  const modal = document.getElementById("modal-archive");
  const title = document.getElementById("archive-modal-title");
  const status = document.getElementById("archive-status");
  const progress = document.getElementById("archive-progress");
  const fill = document.getElementById("archive-progress-fill");
  const summary = document.getElementById("archive-summary");
  const note = document.getElementById("archive-note");
  const confirm = document.getElementById("btn-archive-confirm");
  const cancel = document.getElementById("btn-archive-cancel");
  const close = document.getElementById("btn-close-archive-modal");
  if (!modal) return;

  modal.style.display = "flex";
  if (state === "opening") modal.setAttribute("data-locked", "");
  else modal.removeAttribute("data-locked");

  progress.hidden = !(state === "reading" || state === "opening");
  fill.classList.toggle("is-indeterminate", state === "opening");
  if (state === "reading") fill.style.width = "0%";
  if (state === "opening") fill.style.width = "100%";
  summary.hidden = state !== "confirm";
  note.hidden = !(state === "confirm" && note.textContent);
  confirm.hidden = state !== "confirm";
  cancel.hidden = state === "opening";
  close.hidden = state === "opening";
  cancel.textContent = state === "error" ? "Close" : "Cancel";

  if (state === "reading") {
    title.textContent = "Opening archive";
    status.textContent = `Reading “${detail.filename}”…`;
  } else if (state === "confirm") {
    title.textContent = "Open this archive?";
    status.textContent = detail.message || "";
  } else if (state === "opening") {
    title.textContent = "Opening archive";
    status.textContent = "Checking every scan against its fingerprint, then opening…";
  } else if (state === "error") {
    title.textContent = "Could not open the archive";
    status.textContent = detail.message || "Something went wrong.";
  }
}

function viewLabel(view) {
  if (view === "person") return "a person’s page";
  return document.querySelector(`.nav-item[data-view="view-${view}"] .nav-title`)?.textContent || "the Scan Explorer";
}

function fillArchiveSummary(data) {
  const info = data.bundle || {};
  const counts = info.counts || {};
  const summary = document.getElementById("archive-summary");
  summary.innerHTML = "";
  const row = (label, value) => {
    if (!value) return;
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value;
    summary.append(dt, dd);
  };

  const saved = info.saved_at ? new Date(info.saved_at) : null;
  row("Name", info.name || data.filename);
  row("Saved", saved && !Number.isNaN(saved.getTime())
    ? saved.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");
  const holds = [plural(counts.documents || 0, "document"), plural(counts.people || 0, "person", "people")];
  if (info.scans_included === false) holds.push("no scans (a backup that links to them where they are)");
  else holds.push(`${plural(counts.files || 0, "scan")} (${formatBytes(info.scan_bytes || 0)})`);
  row("Holds", holds.join(" · "));
  const resume = info.resume || {};
  if (resume.view) {
    let where = viewLabel(resume.view);
    if (resume.view === "person" && resume.person) where = `${resume.person.name}’s page`;
    else if (resume.view === "curation" && resume.document) where += ` — “${resume.document.title}”`;
    row("Opens at", where);
  }

  const current = data.current || {};
  const note = document.getElementById("archive-note");
  note.textContent = current.documents || current.people
    ? `What is open now (${plural(current.documents || 0, "document")}) is saved to data/backups first, `
      + "so you can open it again to go back. Nothing on your disk is deleted or overwritten."
    : "Nothing on your disk is deleted or overwritten.";
}

function openArchiveFile(file) {
  if (!/\.archivesys$/i.test(file.name)) {
    showToast(`“${file.name}” is not a saved archive. Choose a file ending in .archivesys.`, "error");
    return;
  }
  if (archiveModalState === "reading" || archiveModalState === "opening") return;

  renderArchiveModal("reading", { filename: file.name });
  const xhr = new XMLHttpRequest();
  archiveUploadXhr = xhr;
  xhr.open("POST", `/api/bundle/upload?filename=${encodeURIComponent(file.name)}`);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => {
    const fill = document.getElementById("archive-progress-fill");
    if (fill && e.lengthComputable) fill.style.width = `${Math.round((e.loaded / e.total) * 100)}%`;
  };
  xhr.onload = () => {
    archiveUploadXhr = null;
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* reported below */ }
    if (xhr.status < 200 || xhr.status >= 300) {
      renderArchiveModal("error", { message: data.detail || `The file could not be read (${xhr.status}).` });
      return;
    }
    archiveUpload = data;
    fillArchiveSummary(data);
    renderArchiveModal("confirm", { message: "" });
  };
  xhr.onerror = () => {
    archiveUploadXhr = null;
    renderArchiveModal("error", { message: "The file could not be sent to ArchiveSys. Is it still running?" });
  };
  xhr.send(file);
}

function cancelArchiveOpen() {
  if (archiveModalState === "opening") return;     // cannot abandon an open half way
  if (archiveUploadXhr) {
    archiveUploadXhr.abort();
    archiveUploadXhr = null;
  }
  if (archiveUpload) {
    fetch(`/api/bundle/upload/${encodeURIComponent(archiveUpload.token)}`, { method: "DELETE" }).catch(() => {});
    archiveUpload = null;
  }
  archiveModalState = "closed";
  const modal = document.getElementById("modal-archive");
  if (modal) modal.style.display = "none";
}

async function confirmArchiveOpen() {
  const upload = archiveUpload;
  if (!upload || archiveModalState !== "confirm") return;
  renderArchiveModal("opening");
  // From here the old position must not be saved: it would overwrite the one
  // being restored.
  stopSessionTracking();
  await flushPendingSave();
  try {
    const res = await fetch(`/api/bundle/restore/${encodeURIComponent(upload.token)}`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (res.status === 409) {
      // Pages are still being read. The file is kept, so they can try again.
      startSessionTracking();
      renderArchiveModal("confirm", { message: data.detail });
      return;
    }
    if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
    archiveUpload = null;
    try { sessionStorage.setItem("archive_opened", describeOpenedArchive(data)); } catch { /* optional */ }
    history.replaceState(null, "", "/curate");      // the saved view, not the current #hash
    window.location.reload();
  } catch (err) {
    archiveUpload = null;
    startSessionTracking();
    renderArchiveModal("error", { message: err.message });
  }
}

function describeOpenedArchive(r) {
  const parts = [`Opened “${r.name}”.`];
  if (r.relinked) parts.push(`${plural(r.relinked, "scan")} ${r.relinked === 1 ? "was" : "were"} already on this computer.`);
  if (r.unpacked) parts.push(`${plural(r.unpacked, "scan")} unpacked into ${r.library}.`);
  if (r.missing?.length) parts.push(`${plural(r.missing.length, "scan")} could not be found.`);
  if (r.backup) parts.push(`What was open before is saved as ${r.backup.split("/").pop()} — open it to go back.`);
  return parts.join(" ");
}

function announceOpenedArchive() {
  let note = null;
  try {
    note = sessionStorage.getItem("archive_opened");
    sessionStorage.removeItem("archive_opened");
  } catch { /* storage unavailable: nothing to announce */ }
  if (note) showToast(note, "done", 15000);
}
