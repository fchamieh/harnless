"""The file search tools: CWD-contained walking, .gitignore honouring, and the
grep/glob/list_dir/patch_file/read-side helpers they share.

A model-supplied pattern never escapes the working directory, and ignored files are
skipped unless the caller asks for them."""

import os
import re
import shutil

from harnless_core import caps, config, paths


def _ignore_case_flag() -> int:
    """git matches ignore patterns case-insensitively where the filesystem is
    (its `core.ignorecase` defaults to true on Windows, false on POSIX)."""
    return re.IGNORECASE if os.name == "nt" else 0

def _gitignore_glob_re(pattern: str) -> str:
    """Translate one gitignore pattern into regex source, without anchors.

    `**` crosses directories only when it is a whole path component (`**/`, a
    trailing `/**`, or a bare `**`) — git treats `a**/b` like `a*/b`. `*` and `?`
    never cross a separator, `[abc]`/`[!abc]` classes and backslash escapes work.
    Returns None when the pattern can never match, so the rule is dropped: git
    ignores a pattern whose `[` class is never closed (verified with
    `git check-ignore`).
    """
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            component = (i == 0 or pattern[i - 1] == "/") and (
                j == n or pattern[j] == "/"
            )
            if component and j < n:
                out.append("(?:.*/)?")  # '**/' over zero or more directories
                i = j + 1
            elif component:
                out.append(".*")  # a trailing '/**', or a bare '**'
                i = j
            else:
                out.append("[^/]*")  # '**' inside a name segment: behaves like '*'
                i = j
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
        elif c == "[":
            k, neg = i + 1, False
            if k < n and pattern[k] in "!^":
                neg, k = True, k + 1
            cls = ""
            if k < n and pattern[k] == "]":
                cls, k = "]", k + 1  # a ']' in the first position is a literal
            while k < n and pattern[k] != "]":
                cls += pattern[k]
                k += 1
            if k >= n:
                return None  # never closed: git drops the whole pattern, it matches nothing
            else:
                out.append("[" + ("^" if neg else "") + cls + "]")
                i = k + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)

def _gitignore_rules(text: str, base: str) -> list:
    """Compile the lines of one .gitignore into `(regex, negate, dir_only, base)`.

    git decides with the **last** matching line of a file, so the rules come back
    reversed: the first match found against them is the one that decided. `base`
    is the directory the file sits in — patterns match paths relative to it.
    """
    rules = []
    for raw in text.splitlines():
        line = re.sub(r"(?<!\\)\s+$", "", raw)  # git drops unescaped trailing blanks
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        if not line:
            continue
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
            if not line:
                continue
        anchored = "/" in line  # a trailing slash alone does not anchor a pattern
        if line.startswith("/"):
            line = line[1:]  # the anchor is the file's own directory, not a literal '/'
        if not line:
            continue
        body = _gitignore_glob_re(line)
        if body is None:
            continue  # an unclosed '[' class: git drops the pattern entirely
        try:
            regex = re.compile(
                "^" + (body if anchored else "(?:.*/)?" + body) + "$", _ignore_case_flag()
            )
        except re.error:
            continue  # a pattern re cannot parse (e.g. [z-a]) is dropped, not fatal
        rules.append((regex, negate, dir_only, base))
    rules.reverse()
    return rules

def _gitignore_file(dir_abs: str):
    """The rules declared by `dir_abs/.gitignore`, or None if it has no such file."""
    try:
        with open(
            os.path.join(dir_abs, config.GITIGNORE_NAME), "r", encoding="utf-8", errors="replace"
        ) as f:
            return _gitignore_rules(f.read(), dir_abs)
    except OSError:
        return None

def _gitignore_ancestors(root: str) -> list:
    """The rules of the .gitignore files **above** a searched directory.

    git applies those too — a `dist/` in the repository root hides `src/dist/` —
    so walk up from `root`, stopping at the directory holding `.git` (a work-tree
    root; in a linked work tree that is a file) or after GITIGNORE_ANCESTOR_SCAN
    levels, whichever is nearer. A nested .gitignore outranks a shallower one, so
    the rules come back deepest-first.
    """
    d = os.path.dirname(os.path.abspath(root))
    if os.path.exists(os.path.join(os.path.abspath(root), ".git")):
        return []  # the searched directory is itself a work-tree root: nothing above applies
    chain = []
    for _ in range(config.GITIGNORE_ANCESTOR_SCAN):
        if not d:
            break
        chain.append(d)
        if os.path.exists(os.path.join(d, ".git")):
            break
        parent = os.path.dirname(d)
        if parent == d:
            break  # the filesystem root
        d = parent
    rules = []
    for base in reversed(chain):  # shallowest first: the nearer files end up in front
        found = _gitignore_file(base)
        if found:
            rules = found + rules
    return rules

def _ignore_view(rules: list, dir_abs: str) -> list:
    """The rules that can reach entries of `dir_abs`, as
    `(regex, negate, dir_only, prefix)`, where `prefix` is `dir_abs` expressed
    relative to the directory each rule's file sits in."""
    view = []
    for regex, negate, dir_only, base in rules:
        rel = os.path.relpath(dir_abs, base).replace("\\", "/")
        if rel.startswith(".."):
            continue  # this rule's file does not cover this directory
        view.append((regex, negate, dir_only, "" if rel == "." else rel))
    return view

def _is_ignored(view: list, name: str, is_dir: bool) -> bool:
    """git's verdict for one entry: the first matching rule decides (deepest file
    first, then the last line in it), and a dir-only rule cannot match a file."""
    for regex, negate, dir_only, prefix in view:
        if dir_only and not is_dir:
            continue
        if regex.match(name if not prefix else f"{prefix}/{name}"):
            return not negate
    return False

def _gitignored_note(ignored: int) -> str:
    """Say why a search that found nothing may have missed the file: ignored
    paths were skipped, and here is how to look at them anyway."""
    if not ignored:
        return ""
    return (
        f"\n... [{ignored} gitignored path{'s' if ignored != 1 else ''} skipped per "
        f"{config.GITIGNORE_NAME}; pass respect_gitignore:false to include them]"
    )

def _iter_files(root: str, respect_gitignore: bool = True, ignored: list = None):
    """Yield the files to scan for grep/glob.

    `root` itself when it is a file: a path the agent named explicitly is
    searched whatever git thinks of it. (Naming a *directory* only spares that
    directory from being pruned; the rules still judge the files beneath it.)
    Otherwise every file under it, skipping
    `.git`/`node_modules`/`__pycache__` always and — unless `respect_gitignore`
    is false — everything the .gitignore files covering the tree ignore. An
    ignored directory is pruned rather than entered, as git prunes it, so a
    `build/` or `.venv/` costs nothing to skip; `ignored`, a one-slot list when
    given, counts the paths refused. A missing path yields nothing (callers check
    existence first so they can report it explicitly).
    """
    if os.path.isfile(root):
        yield root
        return
    rules = []
    if respect_gitignore:
        rules = _gitignore_ancestors(root)
        local = _gitignore_file(root)
        if local:
            rules = local + rules
    pending = [(root, rules)]
    while pending:
        dir_abs, inherited = pending.pop()
        view = _ignore_view(inherited, dir_abs) if inherited else None
        try:
            entries = sorted(os.scandir(dir_abs), key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            if entry.name in config.GITIGNORE_SKIP_NAMES:
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if view is not None and _is_ignored(view, entry.name, is_dir):
                if ignored is not None:
                    ignored[0] += 1
                continue
            if is_dir:
                if entry.is_symlink():
                    continue  # os.walk(followlinks=False): a linked dir is not descended
                local = _gitignore_file(entry.path)
                subdirs.append((entry.path, local + inherited if local else inherited))
            else:
                yield entry
        pending.extend(reversed(subdirs))  # keep the subtrees in directory order

def tool_grep(args: dict) -> str:
    root = paths.safe_resolve(args["path"])
    if not os.path.exists(root):
        return f"error: path not found: {args['path']}"
    case_sensitive = caps._bool_arg(args, "case_sensitive", True)
    pattern = re.compile(args["pattern"], paths._case_flags(case_sensitive))
    around = max(0, int(args.get("context", 0)))
    limit = caps._limit_arg(args, "limit", config.GREP_MATCH_LIMIT, config.MAX_COUNT_LIMIT)
    ignored = [0]
    walk = _iter_files(root, caps._bool_arg(args, "respect_gitignore", True), ignored)
    file_pattern = args.get("file_pattern")
    file_re = (
        paths._glob_to_regex(file_pattern, case_sensitive) if file_pattern else None
    )
    matches = []
    used = 0
    roots = paths._cwd_roots()

    def capped() -> str:
        """Build the truncation note: stopped by match count, or by output size."""
        if len(matches) >= limit:
            stat = f"{limit} matching lines shown, more exist"
        else:
            stat = f"output capped at {config.GREP_TEXT_LIMIT} chars ({used} shown)"
        return "\n".join(matches) + caps._cap_note(
            stat, "narrow the pattern or file_pattern, or raise limit"
        )

    for fp in walk:
        rel = paths._rel_to_cwd(fp, roots)
        if file_re is not None and not (
            file_re.search(rel) or file_re.search(os.path.basename(rel))
        ):
            continue
        try:
            lines = paths._read_lines(fp)
        except (OSError, UnicodeDecodeError):
            continue
        hit_idx = [i for i, line in enumerate(lines) if pattern.search(line)]
        if not hit_idx:
            continue
        entries = []
        if around > 0:
            hits = set(hit_idx)
            shown = []
            for i in hit_idx:
                for j in range(
                    max(0, i - around), min(len(lines), i + around + 1)
                ):
                    if j not in shown:
                        shown.append(j)
            for k, j in enumerate(shown):
                if k and j > shown[k - 1] + 1:
                    entries.append("--")
                prefix = ">" if j in hits else " "
                entries.append(
                    f"{prefix} {rel}:{j + 1}: {caps._clip_line(lines[j], config.GREP_LINE_LIMIT)}"
                )
        else:
            for i in hit_idx:
                entries.append(f"{rel}:{i + 1}: {caps._clip_line(lines[i], config.GREP_LINE_LIMIT)}")
        for entry in entries:
            if len(matches) >= limit or used + len(entry) > config.GREP_TEXT_LIMIT:
                return capped()
            matches.append(entry)
            used += len(entry)
    # A capped answer already carries its own recover-the-rest hint; the empty
    # one is the case where the agent might conclude a file is not there at all.
    if not matches:
        return "no matches" + _gitignored_note(ignored[0])
    return "\n".join(matches)

def tool_patch_file(args: dict) -> str:
    path = paths.safe_resolve(args["path"])
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    old = args["old_string"]
    new = args["new_string"]
    offset = args.get("offset")
    if offset is not None:
        offset = int(offset)
        n = int(args.get("lines", 0))
        if offset < 1 or n < 0:
            return "error: offset must be >= 1 and lines must be >= 0"
        lines = content.split("\n")
        if offset > len(lines):
            return f"error: offset {offset} is beyond end of file ({len(lines)} lines)"
        start = sum(len(line) + 1 for line in lines[: offset - 1])
        if n > 0:
            end = (
                start
                + sum(len(line) + 1 for line in lines[offset - 1 : offset - 1 + n])
                - 1
            )
        else:
            end = len(content)
        region = content[start:end]
        region_count = region.count(old)
        end_label = f"{offset + n - 1}" if n > 0 else "end of file"
        if region_count == 0:
            return f"error: old_string not found in lines {offset}-{end_label}"
        if region_count > 1:
            return f"error: old_string found {region_count} times in lines {offset}-{end_label}; narrow the range or add context"
        content = content[:start] + region.replace(old, new) + content[end:]
        n = 1
    else:
        count = content.count(old)
        if count == 0:
            return "error: old_string not found in file"
        if count > 1 and not args.get("replace_all"):
            return f"error: old_string found {count} times; provide more context, use offset/lines, or set replace_all"
        if args.get("replace_all"):
            content = content.replace(old, new)
            n = count
        else:
            content = content.replace(old, new, 1)
            n = 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"patched {path}: replaced {n} occurrence(s)"

def tool_glob(args: dict) -> str:
    root = paths.safe_resolve(args["path"])
    if not os.path.exists(root):
        return f"error: path not found: {args['path']}"
    pattern = args["pattern"]
    case_sensitive = caps._bool_arg(args, "case_sensitive", True)
    regex = paths._glob_to_regex(pattern, case_sensitive)
    results = []
    roots = paths._cwd_roots()
    ignored = [0]
    walk = _iter_files(root, caps._bool_arg(args, "respect_gitignore", True), ignored)
    for fp in walk:
        rel = paths._rel_to_cwd(fp, roots)
        if regex.search(rel):
            results.append(rel)
    if not results:
        return "no files matched" + _gitignored_note(ignored[0])
    limit = caps._limit_arg(args, "limit", config.LIST_LIMIT, config.MAX_COUNT_LIMIT)
    return caps._cap_count(
        sorted(results), limit, unit="files", hint="narrow the pattern, or raise limit"
    )

def tool_list_dir(args: dict) -> str:
    path = paths.safe_resolve(args["path"])
    if not os.path.isdir(path):
        return f"error: not a directory: {args['path']}"
    entries = sorted(os.listdir(path))
    limit = caps._limit_arg(args, "limit", config.LIST_LIMIT, config.MAX_COUNT_LIMIT)
    out = []
    for name in entries[:limit]:
        full = os.path.join(path, name)
        out.append(name + "/" if os.path.isdir(full) else name)
    if not out:
        return "(empty)"
    result = "\n".join(out)
    if len(entries) > limit:
        result += caps._cap_note(
            f"{len(entries)} entries, showing first {limit}",
            "raise limit, or glob the directory for a narrower match",
        )
    return result

def tool_delete_file(args: dict) -> str:
    path = paths.safe_resolve(args["path"])
    if not os.path.exists(path):
        return f"error: file does not exist: {args['path']}"
    if os.path.isdir(path):
        return f"error: cannot delete a directory: {args['path']}"
    os.remove(path)
    return f"deleted {path}"

def tool_move_file(args: dict) -> str:
    src = paths.safe_resolve(args["src"])
    dst = paths.safe_resolve(args["dst"])
    if not os.path.exists(src):
        return f"error: source does not exist: {args['src']}"
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.move(src, dst)
    return f"moved {src} -> {dst}"

def tool_copy_file(args: dict) -> str:
    src = paths.safe_resolve(args["src"])
    dst = paths.safe_resolve(args["dst"])
    if not os.path.exists(src):
        return f"error: source does not exist: {args['src']}"
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copy2(src, dst)
    return f"copied {src} -> {dst}"
