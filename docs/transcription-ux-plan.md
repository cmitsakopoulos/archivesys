# Transcription apparatus — working plan

Record of what is wrong, what is being built, and why. Written before the work so
the reasoning survives the diff.

## Diagnosis

### A. The "no feedback" problem (reported)

Ingesting one scan takes **55 s** on this hardware with the second pass enabled.
During that time:

- `/api/fs/ingest` is a single blocking POST. The card's button says "Ingest" and
  nothing else changes for a minute.
- Re-OCR opens a modal whose progress bar is **fabricated**: three `setTimeout`
  calls walk it to 84 % within 950 ms, then it sits frozen for the remaining ~54 s.
  The work is fine; the reporting lies. A user reasonably concludes it has hung.
- The modal is blocking, so the archive cannot be browsed while OCR runs.

### B. "Transcription doesn't show / is inaccurate" (reported)

Two separate causes, both real:

1. **It does return** — verified: 1179 characters, 36 lines, 12 refined. The user
   is almost certainly abandoning the frozen-looking modal before it finishes.
   Compounding this: the result is **never persisted**. It populates the textarea
   only, so navigating away silently discards it.
2. **Real accuracy loss from non-text regions.** Of 36 lines, 8 are junk decoded
   from the printed logo, punch holes, fold creases and the signature flourish:
   `{`, `“Ὡ᾿ὡὩὩ`, `ταν]`, `κος`, `-`, `: ᾿Ξ Ὅν`, `[-Ξ- -`, `Μετά πλείσξης »»"`.
   The body text is good; this debris is what makes the output look broken.

### C. Things not asked about but wrong

- **No concurrency guard.** Clicking Ingest on ten cards fires ten parallel 55 s
  OCR runs and saturates the CPU. Nothing serialises them.
- **Batch ingest is unusable at this cost.** 218 scans × 55 s ≈ 3.3 hours with no
  way to watch, pause or stop it.
- **No cancel.** A run started by accident must be waited out.
- **The language dropdown lies.** It still offers "Multilingual / Mixed —
  grc+ell+fra+eng", but Latin packs were deliberately removed from the Greek
  routes because they inject Latin homoglyphs into Greek words.
- **Unsaved OCR output is silent.** No indication that a re-run must be saved.

## Plan

### 1. A job queue with honest progress  (fixes A, C)

Serial background worker on the server. One OCR job at a time — the work is
CPU-bound, so running several concurrently makes all of them slower.

- `POST /api/jobs` — enqueue one or many paths (or a doc id for re-OCR)
- `GET  /api/jobs` — poll queue + active job + recent results
- `POST /api/jobs/{id}/cancel` — drop a queued job, or ask the active one to stop

Progress is **measured, not animated**: the engine gains a progress callback and
reports real stages — loading, preprocessing, first pass, then `line n of m`
through the refinement pass, which is where the minute actually goes.

### 2. Inline Activity panel, not a modal  (requested)

The sidebar has a large empty void below its four nav items. That becomes the
activity surface: current job, real progress bar, queue depth, recent results,
cancel. Non-blocking, always visible, no pop-ups. The blocking OCR modal is
retired and its fake timers deleted.

### 3. Selection + batch ingest  (requested)

Checkbox selection on the explorer grid:

- Empty / ticked box states, drawn as SVG so they animate (stroke-dashoffset
  draw-on for the tick, the standard technique).
- Click toggles; **Shift-click selects the range** from the last anchor.
- Ctrl/Cmd-click extends without resetting the anchor.
- A selection action bar appears only when something is selected.
- "Select all on page" / "Clear".

### 4. Noise-line filter  (fixes B2)

Drop lines that are simultaneously low-confidence and structurally implausible —
no run of letters, or mostly non-letter characters. Conservative by design:
short **real** lines (`Κύριον`, `ΣΤΡΥΜΩΝ`, `Διατελῶ`) must survive. Exposed as a
setting and reported ("8 noise lines dropped") so it is never silently lossy.

### 5. Small correctness fixes  (fixes C)

- Correct the language dropdown labels to what the router actually does.
- Mark the transcription dirty after a re-run so unsaved output is visible.

## Deliberately not doing

- **Parallel OCR workers.** Tesseract is already multithreaded; competing
  processes would slow the queue down, not speed it up.
- **Auto-saving re-OCR output.** It would overwrite human corrections. The fix
  is to make "unsaved" visible, not to remove the human's control.

## Outcome

All five items built and verified against the real scan.

| | before | after |
|---|---|---|
| Ingest request blocks for | 55 s | 0.025 s (queued) |
| Progress reported | fabricated (84 % at 950 ms, then frozen) | measured (`Re-reading unclear lines 14/25`) |
| Concurrent OCR runs | unbounded | 1, serialised |
| Cancel | impossible | per-job and stop-all |
| Junk lines in output | 8 of 36 | 0 (6 dropped, 2 harmless kept) |

The noise filter needed two rules beyond the first design. Tesseract turned out
to be *confidently* wrong on page furniture — a stray brace scored **75** and a
fold crease **65** — so a confidence gate alone let them through. Added: a line
with no letter or digit anywhere is dropped regardless of confidence, and a
short line under 25 confidence is dropped as unreadable. Validated against the
engine's real reported confidences: all debris dropped, no real text lost.

## Postmortem: the init regression

Removing the OCR modal's JavaScript was done by deleting everything between two
section-comment markers. That range was wider than intended and took five
unrelated functions with it — `setupPanzoom`, `resetPanzoom`,
`setupFilterControls`, `setupDiacriticPalette`, `setupKeyboardShortcuts` — plus
the whole Settings-view module, which had been appended after the modal block.

`setupPanzoom()` is called on the second line of the `DOMContentLoaded` handler.
Its absence threw immediately and aborted the entire init, so the explorer never
listed a directory, selection was never wired, and the activity panel never
started. The page looked frozen. Nothing caught it: the file still parsed
(`node --check` passed), and no Python test loads the page.

Guarded by `tests/test_studio_assets.py`, which asserts that every function the
init block calls is defined, that a named set of features still exists, and that
no JS handler binds to an element id absent from the markup. That last check
immediately found three further dangling references, one of them also mine
(a handler left behind after removing the duplicate sidebar collapse button) and
two pre-existing (deleting the open document tried to reveal an empty state that
was never in the markup, so the studio silently kept showing the deleted record).
