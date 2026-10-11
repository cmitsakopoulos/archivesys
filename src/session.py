"""Where the curator is: the open view, the open record, the filters they set.

This used to live only in the browser's memory, so every restart landed back on
the Scan Explorer with document #1 loaded. It is kept on the server instead, as
a small JSON file beside the database, for three reasons: it survives restarts,
it belongs to the archive rather than to one browser, and it travels inside a
saved .archivesys file -- opening one puts the curator back exactly where they
were when it was saved.

The contents are the studio's own business. The server stores them without
interpreting them, beyond refusing anything that is not a small JSON object.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

SESSION_FILE = "session.json"

# Far above anything the studio writes (a few hundred bytes), low enough that a
# runaway client cannot fill the disk through this endpoint.
MAX_SESSION_BYTES = 64 * 1024

_lock = threading.Lock()


class SessionError(ValueError):
    """The session payload was refused."""


def session_path(db_path: str | Path) -> Path:
    """The session lives beside the database it describes."""
    return Path(db_path).parent / SESSION_FILE


def load_session(path: str | Path) -> Dict[str, Any]:
    """Read the saved session, or an empty one if there is none or it is unreadable."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_session(path: str | Path, data: Any) -> Dict[str, Any]:
    """Validate and persist a session. Written atomically: a crash mid-write
    leaves the previous session intact rather than a truncated file."""
    if not isinstance(data, dict):
        raise SessionError("a session must be a JSON object")
    try:
        encoded = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SessionError(f"a session must be plain JSON: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_SESSION_BYTES:
        raise SessionError("session is too large")

    p = Path(path)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(encoded, encoding="utf-8")
        os.replace(tmp, p)
    return data
