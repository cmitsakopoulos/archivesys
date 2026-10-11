"""Background OCR job queue.

OCR of a faded page with the refinement pass costs tens of seconds. Running it
inside the request that triggered it gives the browser nothing to show for a
minute, and firing several at once simply makes them all slower — Tesseract is
already multithreaded, so competing runs fight over the same cores.

So: one worker thread, one job at a time, and real progress reported out of the
engine rather than animated in the browser.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

# Terminal states a job can settle into.
DONE_STATES = {"done", "failed", "cancelled"}


@dataclass
class Job:
    id: int
    kind: str                      # "ingest" | "reocr" | "import"
    label: str                     # what the user sees (a file name)
    path: Optional[str] = None
    paths: List[str] = field(default_factory=list)    # "import": every scan to add
    groups: List[List[str]] = field(default_factory=list)   # "import": scans that are pages of one item
    doc_id: Optional[int] = None
    preferred_lang: Optional[str] = None

    state: str = "queued"          # queued | running | done | failed | cancelled
    stage: str = "Waiting"
    done_units: int = 0
    total_units: int = 0
    queued_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "doc_id": self.doc_id,
            "state": self.state,
            "stage": self.stage,
            "done_units": self.done_units,
            "total_units": self.total_units,
            "elapsed": round(self.elapsed(), 1),
            "result": self.result,
            "error": self.error,
        }


class JobQueue:
    """Serial OCR worker with cancellation and progress reporting."""

    def __init__(self, runner: Callable[[Job, Callable[..., None], Callable[[], bool]], Dict[str, Any]]):
        self._runner = runner
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending: Deque[Job] = deque()
        self._active: Optional[Job] = None
        self._recent: Deque[Job] = deque(maxlen=25)
        self._cancelled_ids: set[int] = set()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ api --
    def submit(self, **kwargs) -> Job:
        job = Job(id=next(self._ids), **kwargs)
        with self._wake:
            self._pending.append(job)
            self._wake.notify()
        self._ensure_worker()
        return job

    def cancel(self, job_id: int) -> bool:
        """Cancel a queued job outright, or ask a running one to stop."""
        with self._lock:
            self._cancelled_ids.add(job_id)
            for job in list(self._pending):
                if job.id == job_id:
                    self._pending.remove(job)
                    job.state = "cancelled"
                    job.stage = "Cancelled before starting"
                    job.finished_at = time.time()
                    self._recent.appendleft(job)
                    return True
            return self._active is not None and self._active.id == job_id

    def cancel_all(self) -> int:
        with self._lock:
            ids = [j.id for j in self._pending]
            if self._active:
                ids.append(self._active.id)
        for jid in ids:
            self.cancel(jid)
        return len(ids)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active": self._active.as_dict() if self._active else None,
                "queued": [j.as_dict() for j in self._pending],
                "recent": [j.as_dict() for j in self._recent],
                "queued_count": len(self._pending),
            }

    # --------------------------------------------------------------- worker --
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="ocr-worker", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._wake:
                # Idle out rather than spinning a thread forever; submit() will
                # start a fresh worker when more work arrives.
                if not self._pending and not self._wake.wait_for(lambda: bool(self._pending), timeout=60):
                    self._thread = None
                    return
                job = self._pending.popleft()
                self._active = job

            job.state = "running"
            job.started_at = time.time()
            job.stage = "Starting"

            def progress(stage: str, done: int = 0, total: int = 0, _j=job):
                _j.stage = stage
                _j.done_units = done
                _j.total_units = total

            def is_cancelled(_j=job) -> bool:
                with self._lock:
                    return _j.id in self._cancelled_ids

            try:
                job.result = self._runner(job, progress, is_cancelled)
                job.state = "cancelled" if is_cancelled() else "done"
                job.stage = "Cancelled" if job.state == "cancelled" else "Complete"
            except Exception as e:
                if is_cancelled():
                    job.state, job.stage = "cancelled", "Cancelled"
                else:
                    job.state, job.stage = "failed", "Failed"
                    job.error = str(e)
            finally:
                job.finished_at = time.time()
                with self._lock:
                    self._recent.appendleft(job)
                    self._active = None
                    self._cancelled_ids.discard(job.id)
