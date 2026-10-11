"""Which scans are pages of one item, read from their file names.

A flatbed scanning to PNG cannot make a multi-page file, so the pages of one
item are saved as separate files and named to say so: the first page plain,
the rest with a space and their number -- "12345", "12345 1", "12345 2", or
"Passeport", "Passeport 1". A print and its written back follow the same rule.

Scanners number their own output the same way ("Scan", "Scan 1", "Scan 2"),
but that counter runs across unrelated scans. Such series are still grouped,
and marked `counter`, so they are offered unticked for a person to decide.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# "<name> <n>": a space, then up to three digits. Four would read a year
# ("wedding 1952") as page 1952.
PAGE_SUFFIX = re.compile(r"^(?P<base>.+?) (?P<n>\d{1,3})$")

# Names scanners and cameras give their own numbered output.
SCANNER_COUNTERS = {
    "scan", "scanned document", "scanned image", "image", "img",
    "untitled", "document", "photo", "picture",
}


def page_of(path: str | Path) -> Tuple[str, str, int]:
    """(folder, item name, page number) for one scan; page 0 has no suffix."""
    p = Path(path)
    m = PAGE_SUFFIX.match(p.stem)
    if m:
        return str(p.parent), m.group("base"), int(m.group("n"))
    return str(p.parent), p.stem, 0


def is_counter(base: str) -> bool:
    return base.strip().casefold() in SCANNER_COUNTERS


def group_key(path: str | Path) -> Tuple[str, str]:
    folder, base, _ = page_of(path)
    return folder, base.casefold()


def group_pages(paths: Iterable[str | Path]) -> List[Dict[str, object]]:
    """Scans that are pages of one item, in page order.

    Only groups of two or more are returned. Pages are grouped within one
    folder, whatever their file type.
    """
    groups: Dict[Tuple[str, str], List[Tuple[int, str, str]]] = {}
    names: Dict[Tuple[str, str], str] = {}
    for raw in paths:
        folder, base, n = page_of(raw)
        key = (folder, base.casefold())
        groups.setdefault(key, []).append((n, Path(raw).name.casefold(), str(raw)))
        if n == 0 or key not in names:
            names[key] = base

    out = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        members.sort()
        out.append({
            "folder": key[0],
            "name": names[key],
            "counter": is_counter(names[key]),
            "paths": [m[2] for m in members],
            "pages": [m[0] for m in members],
        })
    out.sort(key=lambda g: (g["folder"], str(g["name"]).casefold()))
    return out


def lead_of(paths: List[str]) -> Optional[str]:
    """The item's first page: the lowest-numbered of its scans."""
    return min(paths, key=lambda p: (page_of(p)[2], Path(p).name.casefold())) if paths else None
