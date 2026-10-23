"""Working-directory containment: turning a model-supplied relative path into an
absolute one guaranteed to stay inside CWD, and back again.

CWD itself may be a symlink/junction, which is why the checks run against both forms of
it (`_cwd_roots`)."""

import os
import re

from harnless_core import state


def _cwd_roots() -> list:
    """Absolute forms of CWD: as given, plus fully resolved when CWD is a link.

    CWD itself may be a symlink/junction (e.g. X:\\links\\proj -> X:\\real\\proj),
    while paths are resolved with realpath(), so both forms are needed to keep the
    containment check and the paths shown to the model consistent.
    """
    roots = [state.CWD]
    real = os.path.realpath(state.CWD)
    if os.path.normcase(real) != os.path.normcase(state.CWD):
        roots.append(real)
    return roots
def _inside_cwd(full: str, roots: list = None) -> bool:
    """True if an already realpath-resolved absolute path stays inside CWD.

    Case-insensitive on Windows (normcase); comparing against the resolved CWD is
    what makes a symlinked working directory work, while a link *inside* CWD that
    points outside is still rejected because `full` has already been resolved.
    """
    p = os.path.normcase(full)
    for root in (roots or _cwd_roots()):
        r = os.path.normcase(root).rstrip("\\/")
        if p == r or p.startswith(r + os.sep):
            return True
    return False
def _rel_to_cwd(full: str, roots: list = None) -> str:
    """Express an absolute path relative to CWD, using forward slashes.

    Picks whichever CWD form (the link or its target) actually contains the path,
    so a symlinked CWD yields `src/main.py` instead of `../../../real/src/main.py`.
    """
    fallback = None
    for root in (roots or _cwd_roots()):
        try:
            rel = os.path.relpath(full, root).replace("\\", "/")
        except ValueError:  # e.g. path on a different drive (Windows)
            continue
        if not rel.startswith(".."):
            return rel
        if fallback is None or len(rel) < len(fallback):
            fallback = rel
    return fallback if fallback is not None else full
def safe_resolve(rel_path: str) -> str:
    """Resolve a relative path and ensure it stays inside CWD. Returns absolute path."""
    rel_path = rel_path.replace("\\", "/")
    if rel_path.startswith("./"):
        rel_path = rel_path[2:]
    if os.path.isabs(rel_path):
        raise ValueError(f"Absolute paths are not allowed: {rel_path}")
    roots = _cwd_roots()
    full = os.path.realpath(os.path.join(state.CWD, rel_path))
    if not _inside_cwd(full, roots):
        raise ValueError(f"Path escapes working directory: {rel_path}")
    return full
def _case_flags(case_sensitive: bool) -> int:
    """re flags for a search knob: exact case by default, IGNORECASE when opted out."""
    return 0 if case_sensitive else re.IGNORECASE
def _glob_to_regex(pattern: str, case_sensitive: bool = True):
    """Compile a glob pattern (or a raw regex) for matching CWD-relative paths.

    `**/` matches zero or more directories, so `**/*.py` also matches files sitting
    directly in the searched directory; plain `*`/`?` match across path separators
    because patterns are matched against the whole relative path. A pattern
    containing regex metacharacters is used as a regex. Matching is case-sensitive
    unless `case_sensitive` is False.
    """
    flags = _case_flags(case_sensitive)
    if any(c in pattern for c in "[](){}|\\^$"):
        return re.compile(pattern, flags)
    translated = pattern.replace("**/", "\x00")  # placeholder, so it isn't re-translated
    translated = translated.replace("?", ".").replace("*", ".*")
    return re.compile("^" + translated.replace("\x00", "(?:.*/)?") + "$", flags)
def _read_lines(path: str) -> list:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines
