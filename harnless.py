#!/usr/bin/env python3
"""harnless: a minimal agent harness for a local llama-server (OpenAI-compatible)."""

import argparse
import atexit
import base64
import html.parser
import http.client
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import urllib.error
from urllib.parse import urlparse
from urllib.request import url2pathname

VERSION = "1.4.4"
API_URL = "http://127.0.0.1:11434/v1/chat/completions"
API_KEY = None
MODEL = "local-model"
TEMPERATURE = 1.0
MAX_SUBAGENT_DEPTH = 2
_AGENT_DEPTH = 0
OUTPUT_INDENT = ""
CWD = os.getcwd()
VISION_ENABLED = True
# Double-ESC streaming interrupt: enabled for interactive REPL sessions
# (set in main()); two ESC presses within this window cancel the stream.
INTERRUPT_ENABLED = False
DOUBLE_ESC_WINDOW = 2.0
# Auto-send mode (REPL only): True (default) — Enter submits the line;
# False — Enter inserts a newline and Ctrl+Enter submits (Ctrl+D also
# submits a non-empty line, which is the reliable send key on POSIX where
# Ctrl+Enter is not distinguishable from Enter). Toggled with /auto-send.
AUTO_SEND = True
# Show a "sending context" progress line while waiting for the LLM's first
# token, for large requests only (estimated token count, chars/4, including
# the tool schemas). The line appears CONTEXT_PROGRESS_DELAY seconds in,
# reports request-body upload progress (percent + tokens sent so far) while
# the payload is being sent, then the elapsed wait. Active only on a TTY.
CONTEXT_PROGRESS_THRESHOLD = 30_000
CONTEXT_PROGRESS_DELAY = 1.0
# Chunk size the counting opener writes the request body in, so upload
# progress is observable (http.client would otherwise sendall() it at once).
PROGRESS_CHUNK = 64 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REMOTE_BYTES = 10 * 1024 * 1024
# Output caps. Two layers: a tool's `limit`-style argument is model-facing (the
# agent can ask for less), the constants below are the harness-side ceiling it
# cannot exceed. TOOL_RESULT_LIMIT is the final backstop in execute_tool, kept
# above every per-tool cap so a tool that already trimmed is never trimmed twice.
TOOL_RESULT_LIMIT = 60_000  # any single tool result (chars)
READ_FILE_LIMIT = 50_000  # read_file and @[cwd://] refs (chars)
SHELL_OUTPUT_LIMIT = 20_000  # run_shell stdout+stderr (chars)
FETCH_TEXT_LIMIT = 50_000  # fetch_url body text (chars)
GREP_MATCH_LIMIT = 200  # grep matching lines (count)
GREP_LINE_LIMIT = 400  # grep: chars kept per matched line
GREP_TEXT_LIMIT = 30_000  # grep: whole result (chars)
LIST_LIMIT = 500  # glob / list_dir entries (count)
MAX_COUNT_LIMIT = 5_000  # ceiling on a model-supplied count-based limit
MCP_RESULT_LIMIT = 30_000  # MCP tools/call result (chars)
SUBAGENT_SUMMARY_LIMIT = 20_000  # task: sub-agent final summary (chars)
TODO_BLOCK_LIMIT = 8_000  # todo render + reminder injection (chars)
MEMORY_BLOCK_LIMIT = 12_000  # memory notes injected into the system prompt (chars)
AGENTS_MD_LIMIT = 20_000  # AGENTS.md appended to the system prompt (chars)
# Server-reported token usage per conversation, keyed by conversation id
# (MAIN_CONV_ID for the top-level conversation, unique counters for
# sub-agents — see _record_usage).
USAGE_BY_CONV: dict[int, dict] = {}
MAIN_CONV_ID = 0
_NEXT_CONV_ID = 1  # monotonically increasing usage keys for sub-agent runs
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_REF_RE = re.compile(r"@\[((?:cwd|file|https?)://[^\]]+)\]")
# Preferred shell for run_shell on Windows: "auto" (pwsh → powershell → cmd),
# or force "pwsh" / "powershell" / "cmd". Set from --shell in main().
SHELL_PREFERRED = "auto"
SHELL_NOTE = (
    " Commands run in PowerShell (pwsh, falling back to powershell.exe); use PowerShell syntax "
    "(e.g. Get-ChildItem, Select-String, Get-Content, Copy-Item) — bash/cmd syntax will not work."
    if os.name == "nt"
    else " Commands run in bash."
)

ANSI = {
    "user": "\033[1;36m",
    "assistant": "\033[1;32m",
    "thinking": "\033[1;35m",
    "tool": "\033[1;33m",
    "result": "\033[2m",
    "error": "\033[1;31m",
    "dim": "\033[2m",
    "reset": "\033[0m",
    # markdown
    "heading": "\033[1;36m",
    "code": "\033[35m",
    "codeblock": "\033[48;5;236m",
    "bullet": "\033[1;32m",
    "quote": "\033[2m",
    "bold": "\033[1m",
    "italic": "\033[3m",
    "underline": "\033[4m",
}

COLORS_ENABLED = True

ICONS = {
    "user": "🧑",
    "assistant": "🤖",
    "thinking": "🤔",
    "tool": "🔧",
    "result": "↳",
    "exit": "🏁",
    "error": "⚠️",
    "wait": "⏳",
    "ok": "✅",
}
ASCII_ICONS = {
    "user": "you",
    "assistant": "bot",
    "thinking": "think",
    "tool": "tool",
    "result": "out",
    "exit": "exit",
    "error": "err",
    "wait": "wait",
    "ok": "ok",
}


def _stdout_can_encode_emoji() -> bool:
    """True if sys.stdout's encoding can encode every emoji icon."""
    enc = getattr(sys.stdout, "encoding", None)
    if not enc:
        return False
    try:
        for ch in ICONS.values():
            ch.encode(enc)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


EMOJI_ENABLED = _stdout_can_encode_emoji()


def set_color_enabled(enabled: bool):
    global COLORS_ENABLED
    COLORS_ENABLED = enabled


def set_emoji_enabled(enabled: bool):
    global EMOJI_ENABLED
    EMOJI_ENABLED = enabled


def icon(key: str) -> str:
    table = ICONS if EMOJI_ENABLED else ASCII_ICONS
    return table.get(key, "")


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(
                kernel32.GetStdHandle(-11), ctypes.byref(mode)
            ):
                return False
            return bool(mode.value & 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            return False
    return True


def colorize(text: str, key=None) -> str:
    if not COLORS_ENABLED or key is None:
        return text
    return f"{ANSI[key]}{text}{ANSI['reset']}"


class ExitSignal(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message


def _cwd_roots() -> list:
    """Absolute forms of CWD: as given, plus fully resolved when CWD is a link.

    CWD itself may be a symlink/junction (e.g. X:\\links\\proj -> X:\\real\\proj),
    while paths are resolved with realpath(), so both forms are needed to keep the
    containment check and the paths shown to the model consistent.
    """
    roots = [CWD]
    real = os.path.realpath(CWD)
    if os.path.normcase(real) != os.path.normcase(CWD):
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
    full = os.path.realpath(os.path.join(CWD, rel_path))
    if not _inside_cwd(full, roots):
        raise ValueError(f"Path escapes working directory: {rel_path}")
    return full


def _glob_to_regex(pattern: str):
    """Compile a glob pattern (or a raw regex) for matching CWD-relative paths.

    `**/` matches zero or more directories, so `**/*.py` also matches files sitting
    directly in the searched directory; plain `*`/`?` match across path separators
    because patterns are matched against the whole relative path. A pattern
    containing regex metacharacters is used as a regex.
    """
    if any(c in pattern for c in "[](){}|\\^$"):
        return re.compile(pattern)
    translated = pattern.replace("**/", "\x00")  # placeholder, so it isn't re-translated
    translated = translated.replace("?", ".").replace("*", ".*")
    return re.compile("^" + translated.replace("\x00", "(?:.*/)?") + "$")


# ---------------------------------------------------------------- output caps


def _limit_arg(args: dict, key: str, default: int, ceiling: int) -> int:
    """Read an integer limit-style argument, clamped to [1, ceiling].

    Model-facing: the agent can ask for a smaller (or bigger) result, but
    never past the harness-side ceiling. A missing or bogus value falls back
    to the default.
    """
    raw = args.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, ceiling))


def _cap_note(stat: str, hint: str) -> str:
    """The marker appended to a capped result: how much was dropped + how to recover."""
    return f"\n... [truncated: {stat}{'; ' + hint if hint else ''}]"


def _truncate(text: str, limit: int, *, unit: str = "chars", hint: str = "") -> str:
    """Cap `text` at `limit` characters, appending what was dropped and a recovery hint."""
    if len(text) <= limit:
        return text
    return text[:limit] + _cap_note(
        f"{len(text)} {unit} total, showing first {limit}", hint
    )


def _cap_count(items: list, limit: int, *, unit: str, hint: str = "") -> str:
    """Join at most `limit` items, appending how many exist and a recovery hint."""
    if len(items) <= limit:
        return "\n".join(str(i) for i in items)
    return "\n".join(str(i) for i in items[:limit]) + _cap_note(
        f"{len(items)} {unit}, showing first {limit}", hint
    )


def _clip_line(line: str, limit: int) -> str:
    """Clip one over-long line (e.g. minified JS) so a single hit can't dump megabytes."""
    if len(line) <= limit:
        return line
    return line[:limit] + f" …[+{len(line) - limit} chars on this line]"


# ---------------------------------------------------------------- tools


def tool_get_cwd(args: dict) -> str:
    return CWD


def tool_exit(args: dict):
    code = int(args.get("code", 0))
    message = args.get("message", "")
    raise ExitSignal(code, message)


def _prompt_line(prompt: str):
    """Read a line from the user, returning None on EOF/cancel."""
    try:
        line = readline_prompt(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return line.strip()


def _print_question(text: str):
    print(OUTPUT_INDENT + colorize(f"{icon('thinking')} {text}", "assistant"), flush=True)


def tool_ask_user(args: dict) -> str:
    question = (args.get("question") or "").strip()
    if not question:
        return "error: empty question"
    raw_options = args.get("options")
    if raw_options is not None and not isinstance(raw_options, list):
        return "error: options must be a list of strings"
    options = [str(o) for o in (raw_options or [])]
    print()
    _print_question(question)
    for i, opt in enumerate(options, 1):
        print(OUTPUT_INDENT + colorize(f"  {i}) {opt}", "dim"))
    if options:
        print(OUTPUT_INDENT + colorize("  (or type your own answer)", "dim"))
    prompt = OUTPUT_INDENT + colorize(f"{icon('user')} answer> ", "user")
    answer = _prompt_line(prompt)
    if answer is None:
        return "user cancelled (no answer)"
    if not answer:
        return "user answered: (empty)"
    if options:
        try:
            idx = int(answer)
        except ValueError:
            idx = 0
        if 1 <= idx <= len(options):
            return f"user selected: {options[idx - 1]}"
    return f"user answered: {answer}"


_SHELL_CACHE: dict[str, tuple] = {}


def _resolve_shell(preferred: str = "auto") -> tuple:
    """Resolve the shell run_shell should use.

    Returns (argv_prefix, command_prefix):
      argv_prefix: list [shell, flags...] to spawn directly, or None to use
                   shell=True (cmd.exe on Windows, sh elsewhere).
      command_prefix: text prepended to the user command (forces UTF-8 console
                      output on all PowerShell — both 5.1 and pwsh 7 may emit
                      the ANSI code page on redirected stdout, e.g. cp1256 on
                      an Arabic-locale Windows with pwsh 7.6).

    On Windows the preference order is pwsh (PowerShell 7+) → powershell.exe
    (5.1) → cmd.exe; `preferred` can force one of them (falling back to the
    order when the forced shell is missing). Non-Windows always uses shell=True.
    """
    if os.name != "nt":
        return (None, "")
    if preferred in _SHELL_CACHE:
        return _SHELL_CACHE[preferred]

    def _pwsh():
        p = shutil.which("pwsh")
        return [p, "-NoProfile", "-NonInteractive", "-Command"] if p else None

    def _ps():
        p = shutil.which("powershell")
        return [p, "-NoProfile", "-NonInteractive", "-Command"] if p else None

    utf8 = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    result = None
    if preferred == "pwsh":
        a = _pwsh()
        result = (a, utf8) if a else None
    elif preferred == "powershell":
        a = _ps()
        result = (a, utf8) if a else None
    elif preferred == "cmd":
        result = (None, "")
    if result is None:  # auto, or a forced shell that is missing
        a = _pwsh()
        result = (a, utf8) if a else None
        if result is None:
            a = _ps()
            result = (a, utf8) if a else (None, "")
    _SHELL_CACHE[preferred] = result
    return result


def tool_run_shell(args: dict) -> str:
    command = args.get("command", "")
    if not command.strip():
        return "error: empty command"
    argv_prefix, command_prefix = _resolve_shell(SHELL_PREFERRED)
    if argv_prefix is not None:
        # PowerShell -Command maps any non-zero native exit code to 1; append
        # `exit $LASTEXITCODE` so the real exit code of the last native command
        # is propagated (pure PowerShell statements leave it at 0).
        cmd = argv_prefix + [command_prefix + command + "; exit $LASTEXITCODE"]
        shell = False
        # PowerShell output is UTF-8 (forced via the command prefix on both
        # pwsh 7 and 5.1); decode as UTF-8 rather than the locale code page.
        encoding = "utf-8"
    else:
        cmd = command
        shell = True
        encoding = None
    try:
        proc = subprocess.run(
            cmd,
            shell=shell,
            cwd=CWD,
            capture_output=True,
            text=True,
            encoding=encoding,
            # A stray non-UTF-8 byte (e.g. a native command writing in the
            # ANSI code page) must not crash the reader thread and wipe the
            # whole output; degrade to U+FFFD instead.
            errors="replace",
            timeout=int(args.get("timeout", 120)),
        )
        out = []
        if proc.stdout:
            out.append(proc.stdout.rstrip())
        if proc.stderr:
            out.append(f"[stderr]\n{proc.stderr.rstrip()}")
        result = "\n".join(out) if out else "(no output)"
        limit = _limit_arg(args, "max_output", SHELL_OUTPUT_LIMIT, TOOL_RESULT_LIMIT)
        result = _truncate(
            result,
            limit,
            hint="re-run with a larger max_output, or redirect to a file and read it with read_file",
        )
        return f"exit code: {proc.returncode}\n{result}"
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {args.get('timeout', 120)}s"


def tool_mkdir(args: dict) -> str:
    path = safe_resolve(args["path"])
    os.makedirs(path, exist_ok=True)
    return f"created directory: {path}"


def _read_lines(path: str) -> list:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def tool_read_file(args: dict) -> str:
    path = safe_resolve(args["path"])
    lines = _read_lines(path)
    offset = int(args.get("offset", 1))
    count = int(args.get("lines", 0))
    if offset < 1:
        return "error: offset must be >= 1"
    if count < 0:
        return "error: lines must be >= 0"
    start = offset - 1
    end = len(lines) if count == 0 else start + count
    selected = lines[start:end]
    if args.get("line_numbers", True):
        selected = [f"{i + start + 1}: {line}" for i, line in enumerate(selected)]
    content = "\n".join(selected)
    limit = _limit_arg(args, "max_chars", READ_FILE_LIMIT, TOOL_RESULT_LIMIT)
    content = _truncate(
        content, limit, hint="read the rest with offset/lines, or raise max_chars"
    )
    if content and (offset != 1 or count != 0):
        content += f"\n[lines {offset}-{min(end, len(lines))} of {len(lines)}]"
    return content if content else "(empty)"


def tool_write_file(args: dict) -> str:
    path = safe_resolve(args["path"])
    content = args["content"]
    content_lines = content.split("\n")
    if content_lines and content_lines[-1] == "":
        content_lines.pop()
    if args.get("offset") is None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"wrote {len(content)} chars to {path}"
    offset = int(args["offset"])
    n = int(args.get("lines", 0))
    if offset < 1 or n < 0:
        return "error: offset must be >= 1 and lines must be >= 0"
    try:
        existing = _read_lines(path)
    except FileNotFoundError:
        return f"error: file does not exist, cannot modify line range: {path}"
    if offset > len(existing) + 1:
        return f"error: offset {offset} is beyond end of file ({len(existing)} lines)"
    start = offset - 1
    result = existing[:start] + content_lines + existing[start + n :]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(result) + ("\n" if result else ""))
    if n > 0:
        return f"replaced lines {offset}-{offset + n - 1} of {path} with {len(content_lines)} line(s)"
    return f"inserted {len(content_lines)} line(s) before line {offset} in {path}"


def _iter_files(root: str):
    """Yield the files to scan for grep/glob.

    `root` itself when it is a file, otherwise every file under it, skipping
    .git/node_modules/__pycache__. A missing path yields nothing (callers
    check existence first so they can report it explicitly).
    """
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in (".git", "node_modules", "__pycache__")
        ]
        for name in filenames:
            yield os.path.join(dirpath, name)


def tool_grep(args: dict) -> str:
    root = safe_resolve(args["path"])
    if not os.path.exists(root):
        return f"error: path not found: {args['path']}"
    pattern = re.compile(args["pattern"], re.IGNORECASE)
    context = max(0, int(args.get("context", 0)))
    limit = _limit_arg(args, "limit", GREP_MATCH_LIMIT, MAX_COUNT_LIMIT)
    file_pattern = args.get("file_pattern")
    file_re = _glob_to_regex(file_pattern) if file_pattern else None
    matches = []
    used = 0
    roots = _cwd_roots()

    def capped() -> str:
        """Build the truncation note: stopped by match count, or by output size."""
        if len(matches) >= limit:
            stat = f"{limit} matching lines shown, more exist"
        else:
            stat = f"output capped at {GREP_TEXT_LIMIT} chars ({used} shown)"
        return "\n".join(matches) + _cap_note(
            stat, "narrow the pattern or file_pattern, or raise limit"
        )

    for fp in _iter_files(root):
        rel = _rel_to_cwd(fp, roots)
        if file_re is not None and not (
            file_re.search(rel) or file_re.search(os.path.basename(rel))
        ):
            continue
        try:
            lines = _read_lines(fp)
        except (OSError, UnicodeDecodeError):
            continue
        hit_idx = [i for i, line in enumerate(lines) if pattern.search(line)]
        if not hit_idx:
            continue
        entries = []
        if context > 0:
            hits = set(hit_idx)
            shown = []
            for i in hit_idx:
                for j in range(
                    max(0, i - context), min(len(lines), i + context + 1)
                ):
                    if j not in shown:
                        shown.append(j)
            for k, j in enumerate(shown):
                if k and j > shown[k - 1] + 1:
                    entries.append("--")
                prefix = ">" if j in hits else " "
                entries.append(
                    f"{prefix} {rel}:{j + 1}: {_clip_line(lines[j], GREP_LINE_LIMIT)}"
                )
        else:
            for i in hit_idx:
                entries.append(f"{rel}:{i + 1}: {_clip_line(lines[i], GREP_LINE_LIMIT)}")
        for entry in entries:
            if len(matches) >= limit or used + len(entry) > GREP_TEXT_LIMIT:
                return capped()
            matches.append(entry)
            used += len(entry)
    return "\n".join(matches) if matches else "no matches"


def tool_patch_file(args: dict) -> str:
    path = safe_resolve(args["path"])
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
    root = safe_resolve(args["path"])
    if not os.path.exists(root):
        return f"error: path not found: {args['path']}"
    pattern = args["pattern"]
    regex = _glob_to_regex(pattern)
    results = []
    roots = _cwd_roots()
    for fp in _iter_files(root):
        rel = _rel_to_cwd(fp, roots)
        if regex.search(rel):
            results.append(rel)
    if not results:
        return "no files matched"
    limit = _limit_arg(args, "limit", LIST_LIMIT, MAX_COUNT_LIMIT)
    return _cap_count(
        sorted(results), limit, unit="files", hint="narrow the pattern, or raise limit"
    )


def tool_list_dir(args: dict) -> str:
    path = safe_resolve(args["path"])
    if not os.path.isdir(path):
        return f"error: not a directory: {args['path']}"
    entries = sorted(os.listdir(path))
    limit = _limit_arg(args, "limit", LIST_LIMIT, MAX_COUNT_LIMIT)
    out = []
    for name in entries[:limit]:
        full = os.path.join(path, name)
        out.append(name + "/" if os.path.isdir(full) else name)
    if not out:
        return "(empty)"
    result = "\n".join(out)
    if len(entries) > limit:
        result += _cap_note(
            f"{len(entries)} entries, showing first {limit}",
            "raise limit, or glob the directory for a narrower match",
        )
    return result


def tool_delete_file(args: dict) -> str:
    path = safe_resolve(args["path"])
    if not os.path.exists(path):
        return f"error: file does not exist: {args['path']}"
    if os.path.isdir(path):
        return f"error: cannot delete a directory: {args['path']}"
    os.remove(path)
    return f"deleted {path}"


def tool_move_file(args: dict) -> str:
    src = safe_resolve(args["src"])
    dst = safe_resolve(args["dst"])
    if not os.path.exists(src):
        return f"error: source does not exist: {args['src']}"
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.move(src, dst)
    return f"moved {src} -> {dst}"


def tool_copy_file(args: dict) -> str:
    src = safe_resolve(args["src"])
    dst = safe_resolve(args["dst"])
    if not os.path.exists(src):
        return f"error: source does not exist: {args['src']}"
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copy2(src, dst)
    return f"copied {src} -> {dst}"


_HTML_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
_HTML_SKIP_TAGS = {"script", "style", "head", "noscript", "template", "svg"}


class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HTML_SKIP_TAGS:
            self._skip += 1
        elif tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _HTML_SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(markup: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    lines = [
        re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        for line in "".join(parser.parts).split("\n")
    ]
    out = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def tool_fetch_url(args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "error: url must be an http:// or https:// URL"
    try:
        timeout = float(args.get("timeout", 30))
    except (TypeError, ValueError):
        return "error: timeout must be a number"
    if timeout <= 0:
        return "error: timeout must be > 0"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "harnless/1.0",
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
        },
    )
    max_bytes = 2_000_000
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        return f"error: HTTP {e.code} {e.reason} for {url}"
    except urllib.error.URLError as e:
        return f"error: could not fetch {url}: {e.reason}"
    except (OSError, ValueError) as e:
        return f"error: could not fetch {url}: {e}"
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    try:
        text = data.decode(charset, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    if args.get("strip_html"):
        text = _html_to_text(text)
    limit = _limit_arg(args, "max_chars", FETCH_TEXT_LIMIT, TOOL_RESULT_LIMIT)
    clipped = len(text) > limit
    text = _truncate(text, limit, hint="raise max_chars, or fetch a narrower URL")
    if truncated and not clipped:
        text += _cap_note(
            f"body read stopped at {max_bytes} bytes",
            "the response is larger than the harness read ceiling",
        )
    return text if text else "(empty)"


SUBAGENT_NOTE = (
    "You are a sub-agent delegated a specific task. Work autonomously using the tools. "
    "The parent only sees your final summary: make it self-contained and concise — key "
    "findings, file paths with line numbers, decisions, and caveats — omitting intermediate "
    "detail (aim for under 50 lines unless the task asks for more). "
    "When the task is complete, write that final summary, then call the exit tool with code 0. "
    "If the task cannot be completed, explain why in your final message and call the exit tool "
    "with a non-zero code."
)


def tool_task(args: dict) -> str:
    """Delegate a task to a sub-agent: a nested run_agent with a fresh context.

    Returns the sub-agent's exit code and final summary as the tool result.
    """
    task = (args.get("task") or "").strip()
    if not task:
        return "error: empty task"
    if _AGENT_DEPTH >= MAX_SUBAGENT_DEPTH:
        return (
            f"error: sub-agent depth limit reached ({MAX_SUBAGENT_DEPTH}); "
            "do the work yourself"
        )
    system = get_system_prompt(CWD, SUBAGENT_NOTE)
    agents_md = load_agents_md()
    if agents_md:
        system += (
            "\n\nProject instructions (AGENTS.md in the working directory):\n"
            + agents_md
        )
    additions = _context_additions()
    if additions:
        system += "\n\n" + additions
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    global OUTPUT_INDENT, TODO_LAST_INJECTED, _NEXT_CONV_ID
    old_indent = OUTPUT_INDENT
    old_todo_state = TODO_LAST_INJECTED
    conv_id = _NEXT_CONV_ID
    _NEXT_CONV_ID += 1
    OUTPUT_INDENT = old_indent + "  "
    try:
        code = run_agent(
            messages,
            MODEL,
            interactive=False,
            temperature=TEMPERATURE,
            depth=_AGENT_DEPTH + 1,
            conv_id=conv_id,
        )
    finally:
        OUTPUT_INDENT = old_indent
        TODO_LAST_INJECTED = old_todo_state
        # The sub-agent's messages list is freed on return; drop its usage
        # entry so the address can't be recycled and resurrect stale usage.
        USAGE_BY_CONV.pop(conv_id, None)
    final = ""
    for m in reversed(messages):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            final = m["content"].strip()
            break
    if final:
        final = _truncate(
            final,
            SUBAGENT_SUMMARY_LIMIT,
            hint="delegate a narrower task, or ask the sub-agent for a shorter summary",
        )
        return f"exit code: {code}\n{final}"
    return f"exit code: {code}"


# ---------------------------------------------------------------- state tools

TODO_ITEMS = []  # [{"id": int, "text": str, "status": "pending"|"in_progress"|"done"}]
TODO_NEXT_ID = 1
TODO_FILE = os.path.join(CWD, ".harnless", "todo.md")
TODO_LAST_INJECTED = None  # serialized list state last injected as a reminder

MEMORY_PROJECT_FILE = os.path.join(CWD, ".harnless", "memory.md")
MEMORY_GLOBAL_FILE = os.path.join(os.path.expanduser("~"), ".harnless", "memory.md")


def _todo_render() -> str:
    if not TODO_ITEMS:
        return "(empty)"
    marks = {"pending": " ", "in_progress": "~", "done": "x"}
    return "\n".join(
        f"- [{marks.get(i['status'], ' ')}] {i['id']}. {i['text']}" for i in TODO_ITEMS
    )


def _todo_block() -> str:
    """The todo list as shown to the model: capped, so a long list can't bloat context.

    _todo_render() stays uncapped for the file on disk; only what enters the
    conversation is trimmed.
    """
    return _truncate(
        _todo_render(),
        TODO_BLOCK_LIMIT,
        unit="chars",
        hint="the list is long; mark finished items done and use action 'clear'",
    )


def _todo_save():
    try:
        os.makedirs(os.path.dirname(TODO_FILE), exist_ok=True)
        with open(TODO_FILE, "w", encoding="utf-8") as f:
            f.write(_todo_render() + "\n" if TODO_ITEMS else "")
    except OSError:
        pass


def _todo_load():
    """Load the todo list from TODO_FILE (best effort)."""
    global TODO_ITEMS, TODO_NEXT_ID, TODO_LAST_INJECTED
    try:
        with open(TODO_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    items = []
    for line in lines:
        m = re.match(r"^\s*-\s*\[( |~|x)\]\s*(\d+)\.\s+(.*)$", line)
        if not m:
            continue
        mark, num, text = m.groups()
        items.append(
            {
                "id": int(num),
                "text": text,
                "status": {" ": "pending", "~": "in_progress", "x": "done"}[mark],
            }
        )
    if items:
        TODO_ITEMS = items
        TODO_NEXT_ID = max(i["id"] for i in items) + 1
        TODO_LAST_INJECTED = None


def _todo_update_one(args: dict):
    """Apply a single update (id, plus status and/or text). Returns an error string or None."""
    if args.get("id") is None:
        return "error: 'update' requires id"
    try:
        tid = int(args["id"])
    except (TypeError, ValueError):
        return "error: id must be an integer"
    item = next((i for i in TODO_ITEMS if i["id"] == tid), None)
    if item is None:
        return f"error: no todo with id {tid}"
    if args.get("text"):
        item["text"] = args["text"].strip()
    if args.get("status"):
        status = args["status"].strip().lower()
        if status not in ("pending", "in_progress", "done"):
            return "error: status must be pending, in_progress, or done"
        item["status"] = status
    return None


def tool_todo(args: dict) -> str:
    global TODO_NEXT_ID
    action = (args.get("action") or "list").strip().lower()
    if action == "add":
        items = args.get("items")
        if isinstance(items, list):
            texts = [str(t).strip() for t in items]
        else:
            texts = [(args.get("text") or "").strip()]
        texts = [t for t in texts if t]
        if not texts:
            return "error: 'add' requires text or a non-empty items array"
        for t in texts:
            TODO_ITEMS.append({"id": TODO_NEXT_ID, "text": t, "status": "pending"})
            TODO_NEXT_ID += 1
    elif action == "update":
        updates = args.get("updates")
        if isinstance(updates, list):
            for u in updates:
                if not isinstance(u, dict):
                    return "error: each entry in 'updates' must be an object with an 'id'"
                err = _todo_update_one(u)
                if err:
                    return err
        else:
            err = _todo_update_one(args)
            if err:
                return err
    elif action == "clear":
        TODO_ITEMS.clear()
    elif action != "list":
        return f"error: unknown action '{action}' (use add, update, list, or clear)"
    _todo_save()
    return "todo list:\n" + _todo_block()


def _todo_reminder(messages: list):
    """Append a reminder with the current todo list if it changed since the last injection.

    Sent as a *user* message, not system: some chat templates (e.g. Qwen) forbid
    system messages anywhere but the first position, and a mid-conversation system
    reminder would make the server reject the whole request.
    """
    global TODO_LAST_INJECTED
    if not TODO_ITEMS:
        TODO_LAST_INJECTED = None
        return
    state = json.dumps(TODO_ITEMS, sort_keys=True)
    if state == TODO_LAST_INJECTED:
        return
    messages.append({"role": "user", "content": "Current todo list:\n" + _todo_block()})
    TODO_LAST_INJECTED = state


def _memory_read(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln[2:].strip() for ln in f.read().splitlines() if ln.startswith("- ")]
    except OSError:
        return []


def _memory_write(path: str, notes: list):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(f"- {n}" for n in notes) + ("\n" if notes else ""))
    except OSError:
        pass


def _memory_path(scope: str) -> str:
    return MEMORY_PROJECT_FILE if scope == "project" else MEMORY_GLOBAL_FILE


def tool_memory(args: dict) -> str:
    action = (args.get("action") or "list").strip().lower()
    scope = (args.get("scope") or "").strip().lower()
    if scope not in ("", "project", "global", "all"):
        return "error: scope must be 'project' or 'global'"
    if action == "add":
        text = (args.get("text") or "").strip()
        if not text:
            return "error: 'add' requires text"
        target = scope or "project"
        notes = _memory_read(_memory_path(target))
        if text in notes:
            return f"already in {target} memory"
        notes.append(text)
        _memory_write(_memory_path(target), notes)
        return f"added to {target} memory ({len(notes)} notes)"
    if action == "remove":
        text = (args.get("text") or "").strip()
        if not text:
            return "error: 'remove' requires text"
        target = scope or "project"
        notes = _memory_read(_memory_path(target))
        kept = [n for n in notes if text not in n]
        if len(kept) == len(notes):
            return f"no {target} memory note contains '{text}'"
        _memory_write(_memory_path(target), kept)
        return f"removed {len(notes) - len(kept)} note(s) from {target} memory"
    if action == "list":
        scopes = ("project", "global") if scope in ("", "all") else (scope,)
        parts = []
        for s in scopes:
            notes = _memory_read(_memory_path(s))
            body = "\n".join(f"- {n}" for n in notes) if notes else "(empty)"
            body = _truncate(
                body,
                MEMORY_BLOCK_LIMIT,
                unit="chars",
                hint=f"the {s} memory is long; remove stale notes with action 'remove'",
            )
            parts.append(f"{s} memory:\n{body}")
        return "\n\n".join(parts)
    return f"error: unknown action '{action}' (use add, list, or remove)"


def load_memory() -> str:
    """Read project and global memory notes. Returns a formatted block, '' if none.

    Capped: this block is appended to every system prompt (parent and
    sub-agents), so an unbounded memory file would silently eat context.
    """
    parts = []
    for label, path in (("project", MEMORY_PROJECT_FILE), ("global", MEMORY_GLOBAL_FILE)):
        notes = _memory_read(path)
        if notes:
            parts.append(f"{label}:\n" + "\n".join(f"- {n}" for n in notes))
    return _truncate(
        "\n\n".join(parts),
        MEMORY_BLOCK_LIMIT,
        unit="chars",
        hint="memory is long; remove stale or superseded notes",
    )


def _context_additions() -> str:
    """Memory block to append to a system prompt ('' if none)."""
    memory = load_memory()
    if not memory:
        return ""
    return "Memory (persistent notes from previous sessions):\n" + memory


TOOLS = {
    "get_cwd": (
        {
            "type": "function",
            "function": {
                "name": "get_cwd",
                "description": "Get the current working directory of the harness.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        tool_get_cwd,
    ),
    "run_shell": (
        {
            "type": "function",
            "function": {
                "name": "run_shell",
                "description": "Run a command in the current working directory."
                + SHELL_NOTE,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The command to run",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (default 120)",
                        },
                        "max_output": {
                            "type": "integer",
                            "description": (
                                f"Max characters of stdout+stderr to return (default {SHELL_OUTPUT_LIMIT}, "
                                f"max {TOOL_RESULT_LIMIT}); the rest is dropped with a truncation note"
                            ),
                        },
                    },
                    "required": ["command"],
                },
            },
        },
        tool_run_shell,
    ),
    "mkdir": (
        {
            "type": "function",
            "function": {
                "name": "mkdir",
                "description": "Create a directory (and parents) given its relative path to CWD, e.g. ./x/y/z.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to create, e.g. ./x/y/z",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        tool_mkdir,
    ),
    "read_file": (
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file given its relative path to CWD, e.g. ./x/y/z/file. Can read a line range; line numbers help target subsequent patch_file/write_file operations.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to the file",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "First line to read, 1-based (default 1)",
                        },
                        "lines": {
                            "type": "integer",
                            "description": "Number of lines to read (default: rest of file)",
                        },
                        "line_numbers": {
                            "type": "boolean",
                            "description": "Prefix each line with its line number (default true)",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": (
                                f"Max characters to return (default {READ_FILE_LIMIT}, max {TOOL_RESULT_LIMIT}); "
                                "prefer offset/lines to read a file in chunks"
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        tool_read_file,
    ),
    "write_file": (
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a file given its relative path to CWD. Without offset: create/overwrite the whole file. With offset: replace lines [offset, offset+lines) with the content (lines=0 inserts before line offset).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to the file",
                        },
                        "content": {
                            "type": "string",
                            "description": "File (or line-range) content to write",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "1-based line to start at. Omit to overwrite the whole file",
                        },
                        "lines": {
                            "type": "integer",
                            "description": "With offset: number of existing lines to replace (default 0 = insert)",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        },
        tool_write_file,
    ),
    "grep": (
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "Search file contents with a regex pattern. path may be a directory (searched recursively) or a single file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to search: a directory (searched recursively) or a single file, e.g. ./x/y/z",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Regex pattern to search",
                        },
                        "context": {
                            "type": "integer",
                            "description": "Lines of context around each match (default 0); match lines are prefixed with '>'",
                        },
                        "file_pattern": {
                            "type": "string",
                            "description": "Optional glob (e.g. *.py) or regex matched against relative file paths to restrict files scanned",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"Max matching lines to return (default {GREP_MATCH_LIMIT}, max {MAX_COUNT_LIMIT}); "
                                f"lines longer than {GREP_LINE_LIMIT} chars are clipped"
                            ),
                        },
                    },
                    "required": ["path", "pattern"],
                },
            },
        },
        tool_grep,
    ),
    "patch_file": (
        {
            "type": "function",
            "function": {
                "name": "patch_file",
                "description": "Patch a file by replacing an exact old_string with new_string (changes certain lines). Provide enough context in old_string so it matches exactly once; if it matches multiple times, narrow the search with offset/lines.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to the file",
                        },
                        "old_string": {
                            "type": "string",
                            "description": "Exact text to replace (must match, including whitespace)",
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement text",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Optional: first line (1-based) of the region where old_string must be found",
                        },
                        "lines": {
                            "type": "integer",
                            "description": "Optional: number of lines in the search region, starting at offset (default: to end of file)",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace all occurrences (default false)",
                        },
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        },
        tool_patch_file,
    ),
    "glob": (
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": "Find files by glob pattern (e.g. **/*.py) or regex. path may be a directory (searched recursively) or a single file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to search: a directory (searched recursively) or a single file, e.g. ./x/y/z",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern (e.g. **/*.ts) or regex matched against relative file paths",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"Max files to return (default {LIST_LIMIT}, max {MAX_COUNT_LIMIT})"
                            ),
                        },
                    },
                    "required": ["path", "pattern"],
                },
            },
        },
        tool_glob,
    ),
    "list_dir": (
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List the contents of a directory given its relative path to CWD. Directories end with '/'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative directory path, e.g. ./src",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"Max entries to return (default {LIST_LIMIT}, max {MAX_COUNT_LIMIT})"
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        tool_list_dir,
    ),
    "delete_file": (
        {
            "type": "function",
            "function": {
                "name": "delete_file",
                "description": "Delete a file given its relative path to CWD. Cannot delete directories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path of the file to delete",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        tool_delete_file,
    ),
    "move_file": (
        {
            "type": "function",
            "function": {
                "name": "move_file",
                "description": "Move (rename) a file between relative paths. Creates destination parent directories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "src": {
                            "type": "string",
                            "description": "Relative source path",
                        },
                        "dst": {
                            "type": "string",
                            "description": "Relative destination path",
                        },
                    },
                    "required": ["src", "dst"],
                },
            },
        },
        tool_move_file,
    ),
    "copy_file": (
        {
            "type": "function",
            "function": {
                "name": "copy_file",
                "description": "Copy a file to a new relative path. Creates destination parent directories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "src": {
                            "type": "string",
                            "description": "Relative source path",
                        },
                        "dst": {
                            "type": "string",
                            "description": "Relative destination path",
                        },
                    },
                    "required": ["src", "dst"],
                },
            },
        },
        tool_copy_file,
    ),
    "fetch_url": (
        {
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": "Fetch the content of an http:// or https:// URL with a GET request. Optionally strip HTML tags to plain text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The http:// or https:// URL to fetch",
                        },
                        "strip_html": {
                            "type": "boolean",
                            "description": "Convert an HTML response to plain text (default false)",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (default 30)",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": (
                                f"Max characters of response body to return (default {FETCH_TEXT_LIMIT}, "
                                f"max {TOOL_RESULT_LIMIT})"
                            ),
                        },
                    },
                    "required": ["url"],
                },
            },
        },
        tool_fetch_url,
    ),
    "todo": (
        {
            "type": "function",
            "function": {
                "name": "todo",
                "description": (
                    "Manage a todo list for tracking multi-step work. Actions: 'add' (items array, or "
                    "text for a single item) to add items, 'update' (id, plus status and/or text, or an "
                    "updates array of such objects) to change item(s), 'list' to show all items, 'clear' "
                    "to remove all items. The current "
                    "list is re-shown to you automatically after changes. Use it for multi-step tasks: "
                    "add all the steps up front in a single 'add' call, mark each in_progress then done "
                    "as you go."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "One of: add, update, list, clear (default: list)",
                        },
                        "text": {
                            "type": "string",
                            "description": "Item text (for add), or replacement text (for update)",
                        },
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Item texts to add in one call (for add; preferred over repeated "
                                "single-item adds)"
                            ),
                        },
                        "id": {
                            "type": "integer",
                            "description": "Item id (for update)",
                        },
                        "status": {
                            "type": "string",
                            "description": "New status (for update): pending, in_progress, or done",
                        },
                        "updates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer", "description": "Item id"},
                                    "status": {
                                        "type": "string",
                                        "description": "New status: pending, in_progress, or done",
                                    },
                                    "text": {
                                        "type": "string",
                                        "description": "Replacement text",
                                    },
                                },
                                "required": ["id"],
                            },
                            "description": (
                                "Item updates to apply in one call (for update; preferred over repeated "
                                "single-item updates)"
                            ),
                        },
                    },
                    "required": ["action"],
                },
            },
        },
        tool_todo,
    ),
    "memory": (
        {
            "type": "function",
            "function": {
                "name": "memory",
                "description": (
                    "Persistent notes that survive across sessions and are loaded into your context at "
                    "startup. Actions: 'add' (text, scope) to store a note, 'list' (scope) to show notes, "
                    "'remove' (text, scope) to delete notes containing the text. Scope: 'project' (this "
                    "working directory, default) or 'global' (all projects); omit scope for list to see "
                    "both. Use it for durable facts about the user, their preferences, or the project, and "
                    "for one-line pitfall notes learned while working — prefix those with 'gotcha: ' "
                    "(e.g. 'gotcha: tests must run from the repo root')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "One of: add, list, remove (default: list)",
                        },
                        "text": {
                            "type": "string",
                            "description": "Note text (for add), or substring to match (for remove)",
                        },
                        "scope": {
                            "type": "string",
                            "description": "'project' (default) or 'global'; omit for list to see both",
                        },
                    },
                    "required": ["action"],
                },
            },
        },
        tool_memory,
    ),
    "task": (
        {
            "type": "function",
            "function": {
                "name": "task",
                "description": (
                    "Delegate a self-contained task to a sub-agent with a fresh, isolated "
                    "context. It runs with the same tools (it can delegate further, up to the "
                    "depth limit) and returns only its final summary — all intermediate output "
                    "(file reads, command output) stays in the sub-agent and is discarded, so "
                    "your context stays clean. Use it proactively for: exploring an unfamiliar "
                    "codebase, answering 'how does X work' questions, researching to create a "
                    "plan, and any work that would produce lots of intermediate output. The "
                    "sub-agent cannot see this conversation: the task string must be complete "
                    "and self-contained — include relevant paths, constraints, and the exact "
                    "output you want (e.g. 'return key findings with file:line references, "
                    "under 50 lines')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The complete, self-contained task for the sub-agent",
                        },
                    },
                    "required": ["task"],
                },
            },
        },
        tool_task,
    ),
    "ask_user": (
        {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": (
                    "Ask the user a question and block until they answer. Use it whenever you need "
                    "feedback, a decision, clarification, or missing information. Optionally provide "
                    "a list of suggested options the user can pick from; they may still type a custom "
                    "answer. Returns the user's answer (or 'user cancelled')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The question to ask the user",
                        },
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional suggested choices; the user can pick one by number, or type their own answer (the UI already offers that, so don't add a 'custom answer' option)",
                        },
                    },
                    "required": ["question"],
                },
            },
        },
        tool_ask_user,
    ),
    "exit": (
        {
            "type": "function",
            "function": {
                "name": "exit",
                "description": "Finish the harness and exit with a given exit code. Use 0 for success, non-zero for failure. Call this when the task is complete.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "integer",
                            "description": "Exit code (default 0)",
                        },
                        "message": {
                            "type": "string",
                            "description": "Optional final message",
                        },
                    },
                    "required": [],
                },
            },
        },
        tool_exit,
    ),
}

OPENAI_TOOLS = [spec for spec, _ in TOOLS.values()]
OPENAI_TOOLS_INTERACTIVE = [
    spec for name, (spec, _) in TOOLS.items() if name != "exit"
]
DISPATCH = {name: fn for name, (_, fn) in TOOLS.items()}


# ---------------------------------------------------------------- mcp

MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_TIMEOUT = 60  # seconds per request
MCP_HOME_CONFIG = os.path.join(os.path.expanduser("~"), ".harnless", "mcp.json")

MCP_TOOLS = []      # OpenAI tool specs for MCP tools
MCP_DISPATCH = {}   # tool name -> (MCPClient, tool name)
MCP_CLIENTS = []    # all configured MCP clients (for /status)
PENDING_MCP = []    # [(name, config)] servers with "enabled": false; enable via /tools

DISABLED_TOOLS = set()  # tool names toggled off via /tools


class MCPError(Exception):
    pass


def _mcp_content_to_text(result: dict) -> str:
    """Convert an MCP tools/call result to a string for the tool-result channel."""
    parts = []
    for item in result.get("content") or []:
        t = item.get("type")
        if t == "text":
            parts.append(item.get("text", ""))
        elif t == "image":
            parts.append(f"[image: {item.get('mimeType', 'unknown')}]")
        elif t == "resource":
            parts.append(f"[resource: {(item.get('resource') or {}).get('uri', '')}]")
        elif t == "resource_link":
            parts.append(f"[resource link: {item.get('uri', '')}]")
        else:
            parts.append(json.dumps(item))
    text = "\n".join(p for p in parts if p)
    structured = result.get("structuredContent")
    if structured is not None:
        text = (text + "\n" if text else "") + "structured: " + json.dumps(structured)
    if not text:
        text = "(no content)"
    if result.get("isError"):
        text = "error: " + text
    return _truncate(
        text,
        MCP_RESULT_LIMIT,
        unit="chars",
        hint="the MCP server returned more than the harness keeps; ask it for a narrower result",
    )


def _expand_env(value):
    """Expand ${VAR} references in a string using os.environ; missing vars left as-is."""
    if not isinstance(value, str):
        return value
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )


def load_mcp_config(path: str) -> dict:
    """Load an MCP config file. Accepts {"mcpServers": {...}} or a bare {...}.

    Returns a dict of server name -> config, with ${VAR} expansion applied to
    command/args/env/headers/url.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
    out = {}
    for name, cfg in servers.items():
        cfg = dict(cfg)
        if "command" in cfg:
            cfg["command"] = _expand_env(cfg["command"])
        if "args" in cfg:
            cfg["args"] = [_expand_env(a) for a in cfg["args"]]
        if "env" in cfg:
            cfg["env"] = {k: _expand_env(v) for k, v in cfg["env"].items()}
        if "headers" in cfg:
            cfg["headers"] = {k: _expand_env(v) for k, v in cfg["headers"].items()}
        if "url" in cfg:
            cfg["url"] = _expand_env(cfg["url"])
        out[name] = cfg
    return out


def _load_mcp_config_into(mcp_servers: dict, path: str) -> None:
    """Load one MCP config file into mcp_servers (later definitions win)."""
    try:
        loaded = load_mcp_config(path)
    except Exception as e:
        print(colorize(f"{icon('error')} failed to load mcp config {path}: {e}", "error"))
        return
    for name, cfg in loaded.items():
        if name in mcp_servers:
            print(
                colorize(
                    f"{icon('error')} duplicate mcp server name '{name}'; later definition wins",
                    "error",
                )
            )
        mcp_servers[name] = cfg


def _parse_mcp_stdio(spec: str):
    """Parse 'name:command args...' into (name, config)."""
    if ":" not in spec:
        raise MCPError(f"invalid --mcp-stdio '{spec}'; expected name:command args...")
    name, rest = spec.split(":", 1)
    parts = rest.split()
    if not name or not parts:
        raise MCPError(f"invalid --mcp-stdio '{spec}'; expected name:command args...")
    return name, {"transport": "stdio", "command": parts[0], "args": parts[1:]}


def _parse_mcp_http(spec: str):
    """Parse 'name=url' into (name, config)."""
    if "=" not in spec:
        raise MCPError(f"invalid --mcp-http '{spec}'; expected name=url")
    name, url = spec.split("=", 1)
    if not name or not url:
        raise MCPError(f"invalid --mcp-http '{spec}'; expected name=url")
    return name, {"transport": "http", "url": url}


class MCPClient:
    """A minimal MCP client over stdio or Streamable HTTP (tools only)."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.transport = config.get("transport", "stdio")
        self.tool_names = []
        self._id = 0
        self._proc: subprocess.Popen | None = None
        self._out_q: queue.Queue | None = None
        self._stderr_buf = []
        self._session_id = None
        self._url: str | None = None
        self._headers = {}

    # -- lifecycle
    def connect(self) -> dict:
        if self.transport == "stdio":
            self._connect_stdio()
        elif self.transport == "http":
            self._connect_http()
        else:
            raise MCPError(f"unknown transport: {self.transport}")
        info = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "harnless", "version": VERSION},
            },
        )
        self._notify("notifications/initialized", {})
        return info

    def close(self):
        if self.transport == "stdio" and self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    # -- stdio transport
    def _connect_stdio(self):
        command = self.config.get("command")
        if not command:
            raise MCPError("stdio server requires 'command'")
        args = [command] + list(self.config.get("args") or [])
        env = os.environ.copy()
        for k, v in (self.config.get("env") or {}).items():
            env[k] = v
        shell = False
        if os.name == "nt":
            # Windows: PATH entries like npx/npm/uvx are .cmd shims, which
            # CreateProcess (shell=False) cannot launch (WinError 2) — route
            # them through cmd.exe. list2cmdline quotes the args, so this is
            # safe even with spaces in arguments.
            resolved = shutil.which(command)
            if resolved and resolved.lower().endswith((".cmd", ".bat")):
                shell = True
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=CWD,
            env=env,
            text=True,
            shell=shell,
        )
        self._out_q = queue.Queue()
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self):
        if self._proc is None or self._out_q is None:
            return
        stdout = self._proc.stdout
        if stdout is None:
            return
        try:
            for line in stdout:
                self._out_q.put(line)
        except Exception:
            pass
        self._out_q.put(None)  # sentinel on EOF

    def _drain_stderr(self):
        if self._proc is None:
            return
        stderr = self._proc.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                self._stderr_buf.append(line.rstrip("\n"))
                if len(self._stderr_buf) > 500:
                    self._stderr_buf.pop(0)
        except Exception:
            pass

    def _send_stdio(self, msg: dict):
        if self._proc is None or self._proc.poll() is not None:
            raise MCPError(f"stdio server '{self.name}' is not running")
        stdin = self._proc.stdin
        if stdin is None:
            raise MCPError(f"stdio server '{self.name}' has no stdin")
        stdin.write(json.dumps(msg) + "\n")
        stdin.flush()

    def _recv_stdio(self) -> dict:
        if self._out_q is None:
            raise MCPError(f"stdio server '{self.name}' is not connected")
        try:
            line = self._out_q.get(timeout=MCP_TIMEOUT)
        except queue.Empty:
            raise MCPError(f"stdio server '{self.name}' timed out after {MCP_TIMEOUT}s")
        if line is None:
            raise MCPError(f"stdio server '{self.name}' closed the connection")
        line = line.strip()
        if not line:
            return self._recv_stdio()
        return json.loads(line)

    # -- http transport
    def _connect_http(self):
        url = self.config.get("url")
        if not url:
            raise MCPError("http server requires 'url'")
        self._url = url
        self._headers = dict(self.config.get("headers") or {})

    def _http_post(self, payload: dict):
        if self._url is None:
            raise MCPError(f"http server '{self.name}' is not connected")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=MCP_TIMEOUT) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
        return ctype, body

    def _parse_sse_response(self, body: str, want_id):
        last = None
        for raw in body.splitlines():
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if want_id is not None and obj.get("id") != want_id:
                continue
            last = obj
        if last is None:
            raise MCPError("no JSON-RPC response found in SSE stream")
        return last

    def _http_request(self, payload: dict) -> dict:
        ctype, body = self._http_post(payload)
        if not body.strip():
            raise MCPError("empty response from http server")
        if "text/event-stream" in ctype:
            return self._parse_sse_response(body, payload.get("id"))
        return json.loads(body)

    # -- JSON-RPC
    def _next_id(self):
        self._id += 1
        return self._id

    def _request(self, method: str, params: dict) -> dict:
        rid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        if self.transport == "stdio":
            self._send_stdio(msg)
            while True:
                resp = self._recv_stdio()
                if resp.get("id") == rid:
                    break
        else:
            resp = self._http_request(msg)
        if "error" in resp:
            err = resp["error"]
            raise MCPError(f"{method} failed: {err.get('message', err)}")
        return resp.get("result", {})

    def _notify(self, method: str, params: dict):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.transport == "stdio":
            self._send_stdio(msg)
        else:
            self._http_post(msg)

    # -- tools
    def list_tools(self) -> list:
        tools = []
        cursor = None
        while True:
            params = {}
            if cursor:
                params["cursor"] = cursor
            result = self._request("tools/list", params)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, args: dict) -> str:
        result = self._request("tools/call", {"name": name, "arguments": args})
        return _mcp_content_to_text(result)


def _register_mcp_client(client) -> bool:
    """Connect one MCP client and append its tools to MCP_TOOLS/MCP_DISPATCH.

    Returns True if the server connected (its tools, if any, are registered),
    False if the connection failed.
    """
    global MCP_CLIENTS
    MCP_CLIENTS.append(client)
    try:
        client.connect()
    except Exception as e:
        print(
            colorize(
                f"{icon('error')} mcp server '{client.name}' failed to connect: {e}",
                "error",
            )
        )
        return False
    try:
        tools = client.list_tools()
    except Exception as e:
        print(
            colorize(
                f"{icon('error')} mcp server '{client.name}' tools/list failed: {e}",
                "error",
            )
        )
        client.close()
        return True
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        if name in DISPATCH or name in MCP_DISPATCH:
            print(
                colorize(
                    f"{icon('error')} mcp tool '{name}' (server '{client.name}') "
                    f"collides with an existing tool; skipped",
                    "error",
                )
            )
            continue
        spec = {
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description") or "",
                "parameters": t.get("inputSchema")
                or {"type": "object", "properties": {}},
            },
        }
        MCP_TOOLS.append(spec)
        MCP_DISPATCH[name] = (client, name)
        client.tool_names.append(name)
    return True


def register_mcp_tools(clients: list) -> None:
    """Connect to MCP clients and register their tools into MCP_TOOLS/MCP_DISPATCH.

    Tool names are used as-is. A name that collides with a built-in tool or an
    earlier-registered MCP tool is skipped (built-ins win, then first server).
    """
    global MCP_TOOLS, MCP_DISPATCH, MCP_CLIENTS
    MCP_TOOLS = []
    MCP_DISPATCH = {}
    MCP_CLIENTS = []
    for client in clients:
        _register_mcp_client(client)


def _enable_pending_mcp(name: str) -> tuple:
    """Connect a pending (enabled:false) MCP server and register its tools.

    Returns (ok, message). On failure the server stays pending so the user can
    retry from /tools.
    """
    for i, (n, cfg) in enumerate(PENDING_MCP):
        if n == name:
            client = MCPClient(n, cfg)
            ok = _register_mcp_client(client)
            if ok:
                PENDING_MCP.pop(i)
                return True, f"{icon('ok')} mcp server '{n}' enabled ({len(client.tool_names)} tools)"
            return False, f"{icon('error')} mcp server '{n}' failed to connect"
    return False, f"{icon('error')} mcp server '{name}' is not pending"


def _mcp_server_tools(server: str) -> list:
    """Sorted names of the tools registered from the given MCP server."""
    return sorted(
        name for name, (client, _) in MCP_DISPATCH.items() if client.name == server
    )


def get_system_prompt(cwd, additional) -> str:
    return (
        "You are a coding assistant running inside a harness. Your working directory is {cwd}. "
        "All file paths you use must be relative to it (e.g. ./src/main.py). "
        "Use the provided tools to inspect and modify files, run commands, and search the codebase. "
        "Tool results are capped: if a result ends with a '[truncated: ...]' note, narrow the request "
        "(a smaller limit/max_chars, a tighter pattern, or a narrower path) instead of re-running it unchanged. "
        "Explore with list_dir, glob, and grep; read files (the trailer shows total line count) before editing them, "
        "and use patch_file with exact matches for edits. "
        "If patch_file reports multiple matches, re-read the area with line numbers and retry using offset/lines. "
        "Before taking any action that modifies the file system (writing, patching, moving, copying, or deleting files), "
        "plan the change when required and use the ask_user tool to get the user's explicit approval before acting; "
        "only proceed once the user has approved. Read-only exploration does not require approval. "
        "Use the ask_user tool whenever you need feedback, a decision, clarification, missing information, "
        "or yes/no approval; it blocks until the user responds. "
        "For multi-step work, track progress with the todo tool: add all the steps up front in a single 'add' call, mark each in_progress then done as you go. "
        "When you hit an error and determine the root cause, record a one-line note with the memory tool, prefixed 'gotcha: ' (e.g. 'gotcha: tests must run from the repo root'), so you don't repeat it. "
        "Use the memory tool for durable facts (user preferences, project conventions) that should survive across sessions. "
        "Delegate generously with the task tool to protect your context: the sub-agent runs in a fresh context and returns only its final summary. "
        "Use it for codebase exploration, research to create plans, 'how does this work' questions, and any work producing lots of intermediate output — "
        "do the exploration in a sub-agent, then plan from its summary. "
        "Write self-contained tasks: the sub-agent cannot see this conversation, so include the relevant paths, constraints, and the exact output you want "
        "(e.g. 'return key findings with file:line references, under 50 lines'). "
        "Format responses in Markdown (headings, lists, tables, fenced code blocks); the terminal renders it. "
        "{additional}"
    ).format(cwd=cwd, additional=additional)


def load_agents_md() -> str:
    """Read AGENTS.md (case-insensitive) from CWD, truncated. Returns "" if absent."""
    path = os.path.join(CWD, "AGENTS.md")
    if not os.path.exists(path):
        for name in os.listdir(CWD):
            if name.upper() == "AGENTS.MD":
                path = os.path.join(CWD, name)
                break
        else:
            return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return _truncate(
        content,
        AGENTS_MD_LIMIT,
        unit="chars",
        hint="AGENTS.md is long; trim the project instructions",
    )


# ---------------------------------------------------------------- file references


def _content_chars(content) -> int:
    """Approximate character length of a message content value.

    Handles both plain strings and OpenAI multimodal content (a list of
    parts) so /status can estimate context usage for either.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                total += len(part.get("text", ""))
            elif ptype == "image_url":
                total += len((part.get("image_url") or {}).get("url", ""))
        return total
    return 0


def _read_local_file(path: str, label: str) -> dict:
    """Read a local file into a content part (text or image_url)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_MIME:
        if not VISION_ENABLED:
            return {"type": "text", "text": f"[image: {label} (not sent; vision disabled)]"}
        size = os.path.getsize(path)
        if size > MAX_IMAGE_BYTES:
            return {"type": "text", "text": f"[error: image too large ({size} bytes): {label}]"}
        with open(path, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{IMAGE_MIME[ext]};base64,{b64}"},
        }
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    content = _truncate(
        content,
        READ_FILE_LIMIT,
        unit="chars",
        hint="the referenced file is large; reference a narrower file",
    )
    return {"type": "text", "text": f"[file: {label}]\n{content}"}


def _read_cwd_relative(rel: str) -> dict:
    """Resolve a cwd:// reference (CWD-relative, sandboxed) to a content part."""
    try:
        path = safe_resolve(rel)
    except ValueError as e:
        return {"type": "text", "text": f"[error: {e}]"}
    if not os.path.isfile(path):
        return {"type": "text", "text": f"[error: file not found: {rel}]"}
    return _read_local_file(path, rel)


def _read_local_uri(uri: str) -> dict:
    """Resolve a file:// reference (absolute local path) to a content part."""
    path = url2pathname(urlparse(uri).path)
    if not os.path.isfile(path):
        return {"type": "text", "text": f"[error: file not found: {uri}]"}
    return _read_local_file(path, uri)


def _fetch_remote(uri: str) -> dict:
    """Fetch an http(s):// reference and return a content part (text or image)."""
    url = uri.replace(" ", "%20")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "harnless/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(MAX_REMOTE_BYTES + 1)
            ctype = resp.headers.get_content_type() or ""
    except Exception as e:
        return {"type": "text", "text": f"[error: could not fetch {uri}: {e}]"}
    truncated = len(data) > MAX_REMOTE_BYTES
    if truncated:
        data = data[:MAX_REMOTE_BYTES]
    ext = os.path.splitext(urlparse(uri).path)[1].lower()
    mime = ctype if ctype.startswith("image/") else IMAGE_MIME.get(ext)
    if mime:
        if not VISION_ENABLED:
            return {"type": "text", "text": f"[image: {uri} (not sent; vision disabled)]"}
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }
    text = data.decode("utf-8", errors="replace")
    clipped = len(text) > FETCH_TEXT_LIMIT
    text = _truncate(
        text,
        FETCH_TEXT_LIMIT,
        hint="the referenced URL is large; reference a narrower page",
    )
    if truncated and not clipped:
        text += _cap_note(
            f"body read stopped at {MAX_REMOTE_BYTES} bytes",
            "the response is larger than the harness read ceiling",
        )
    return {"type": "text", "text": f"[file: {uri}]\n{text}"}


def _resolve_uri(uri: str):
    """Resolve a reference URI to a content part, or None for an unknown scheme."""
    if uri.startswith("cwd://"):
        return _read_cwd_relative(uri[len("cwd://"):])
    if uri.startswith("file://"):
        return _read_local_uri(uri)
    if re.match(r"^https?://", uri):
        return _fetch_remote(uri)
    return None


def expand_file_refs(text: str):
    """Expand @[uri] file references in text into OpenAI message content.

    Returns the original string when no reference resolves, otherwise a list
    of content parts (text and image_url) in order. A @[...] token whose
    scheme is not cwd/file/http(s) is left as literal text.
    """
    refs_found = False
    parts = []
    last = 0
    for m in _REF_RE.finditer(text):
        if m.start() > last:
            parts.append(("text", text[last:m.start()]))
        part = _resolve_uri(m.group(1))
        if part is None:
            parts.append(("text", m.group(0)))
        else:
            refs_found = True
            parts.append(("part", part))
        last = m.end()
    if last < len(text):
        parts.append(("text", text[last:]))
    if not refs_found:
        return text
    content = []
    for kind, value in parts:
        if kind == "text":
            if value:
                content.append({"type": "text", "text": value})
        else:
            content.append(value)
    return content


def build_user_message(text: str) -> dict:
    """Build a user message, expanding any @[uri] file references in text."""
    return {"role": "user", "content": expand_file_refs(text)}


# ---------------------------------------------------------------- line editor

HISTORY = []
HISTORY_MAX = 1000
HISTORY_FILE = os.environ.get("HARNLESS_HISTORY") or os.path.join(
    os.path.expanduser("~"), ".harnless_history"
)


def _history_load():
    """Load persisted history (last HISTORY_MAX entries) into HISTORY."""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError:
        return
    HISTORY.extend(ln for ln in lines if ln.strip())
    if len(HISTORY) > HISTORY_MAX:
        del HISTORY[: len(HISTORY) - HISTORY_MAX]


def _history_save():
    """Write the current HISTORY to HISTORY_FILE (best effort)."""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            for entry in HISTORY:
                f.write(entry + "\n")
    except OSError:
        pass


def _history_add(entry: str):
    entry = entry.strip()
    if not entry:
        return
    if HISTORY and HISTORY[-1] == entry:
        return
    HISTORY.append(entry)
    if len(HISTORY) > HISTORY_MAX:
        HISTORY.pop(0)
    _history_save()


_WINDOWS_EXT_MAP = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
    "G": "home",
    "O": "end",
    "R": "ignore",
    "S": "delete",
    "I": "ignore",
    "Q": "ignore",
}


def _windows_ctrl_held() -> bool:
    """Best-effort: True if Ctrl is held right now (GetAsyncKeyState sign bit).

    msvcrt.getwch() cannot distinguish Enter from Ctrl+Enter (both arrive
    as '\\r'), so when a '\\r' is read we check the live key state. The user
    is normally still holding Ctrl when the event is processed; a very fast
    tap may read back as plain Enter.
    """
    import ctypes

    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
    except Exception:
        return False


def _windows_key_token(getwch):
    """Read one key via getwch and return its token."""
    ch = getwch()
    if ch in ("\x00", "\xe0"):
        return _WINDOWS_EXT_MAP.get(getwch(), "ignore")
    if ch == "\r":
        return "ctrl_enter" if _windows_ctrl_held() else "enter"
    if ch == "\n":
        return "newline"
    if ch == "\x08":
        return "backspace"
    if ch == "\x03":
        return "ctrl_c"
    if ch == "\x04":
        return "ctrl_d"
    if ch == "\x15":
        return "ctrl_u"
    if ch == "\x1b":
        return "esc"
    if ch == "\t" or ord(ch) < 32:
        return "ignore"
    return ("char", ch)


def _iter_keys_windows():
    """Yield key tokens from the Windows console via msvcrt."""
    import msvcrt

    while True:
        yield _windows_key_token(msvcrt.getwch)


def _iter_keys_windows_poll(stop_event):
    """Like _iter_keys_windows, but polls kbhit() so the reader thread can
    be stopped without a keypress."""
    import msvcrt

    while not stop_event.is_set():
        if not msvcrt.kbhit():
            time.sleep(0.02)
            continue
        yield _windows_key_token(msvcrt.getwch)


def _parse_csi_seq(read_char):
    """Consume a CSI sequence (after ESC [) and return a key token.

    Reads until the final byte (0x40-0x7E) so the whole sequence is
    consumed; e.g. Delete sends ESC [ 3 ~ and must not leak the '~'.
    """
    params = ""
    while True:
        c = read_char()
        if c is None:
            return "ignore"
        params += c
        if 0x40 <= ord(c) <= 0x7E:
            break
    return {
        "A": "up",
        "B": "down",
        "C": "right",
        "D": "left",
        "H": "home",
        "F": "end",
        "3~": "delete",
        # Ctrl+Enter as emitted by terminals that can send it (in raw mode
        # plain Enter and Ctrl+Enter are both just '\\r'):
        "13;5u": "ctrl_enter",  # kitty keyboard protocol
        "27;5;13~": "ctrl_enter",  # xterm modifyOtherKeys level 2
    }.get(params, "ignore")


def _iter_keys_posix():
    """Yield key tokens from a POSIX terminal in raw mode via termios/tty."""
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)  # type: ignore[reportAttributeAccessIssue]

    def read_char():
        b = os.read(fd, 1)
        if not b:
            return None
        first = b[0]
        if first < 0x80:
            return chr(first)
        if first >= 0xF0:
            n = 4
        elif first >= 0xE0:
            n = 3
        elif first >= 0xC0:
            n = 2
        else:
            return chr(first)
        rest = b""
        for _ in range(n - 1):
            rest += os.read(fd, 1)
        return (b + rest).decode("utf-8", errors="replace")

    try:
        tty.setraw(fd)  # type: ignore[reportAttributeAccessIssue]
        while True:
            ch = read_char()
            if ch is None:
                yield "ctrl_d"
                return
            if ch == "\x1b":
                # A bare ESC has no following bytes; an escape sequence does.
                # Wait briefly to tell the two apart.
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    yield "esc"
                    continue
                seq = read_char()
                if seq == "[":
                    yield _parse_csi_seq(read_char)
                elif seq == "\r":
                    # Some terminals (e.g. tmux with a custom binding) emit
                    # ESC \r for Ctrl+Enter.
                    yield "ctrl_enter"
                else:
                    yield "ignore"
            elif ch == "\r":
                yield "enter"
            elif ch == "\n":
                yield "newline"
            elif ch in ("\x7f", "\x08"):
                yield "backspace"
            elif ch == "\x03":
                yield "ctrl_c"
            elif ch == "\x04":
                yield "ctrl_d"
            elif ch == "\x15":
                yield "ctrl_u"
            elif ch == "\t" or ord(ch) < 32:
                yield "ignore"
            else:
                yield ("char", ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)  # type: ignore[reportAttributeAccessIssue]


def _iter_keys_posix_poll(stop_event):
    """Like _iter_keys_posix, but polls with select() so the reader thread
    can be stopped without a keypress."""
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)  # type: ignore[reportAttributeAccessIssue]

    def read_char():
        b = os.read(fd, 1)
        if not b:
            return None
        first = b[0]
        if first < 0x80:
            return chr(first)
        if first >= 0xF0:
            n = 4
        elif first >= 0xE0:
            n = 3
        elif first >= 0xC0:
            n = 2
        else:
            return chr(first)
        rest = b""
        for _ in range(n - 1):
            rest += os.read(fd, 1)
        return (b + rest).decode("utf-8", errors="replace")

    try:
        tty.setraw(fd)  # type: ignore[reportAttributeAccessIssue]
        while not stop_event.is_set():
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                continue
            ch = read_char()
            if ch is None:
                yield "ctrl_d"
                return
            if ch == "\x1b":
                # A bare ESC has no following bytes; an escape sequence does.
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    yield "esc"
                    continue
                seq = read_char()
                if seq == "[":
                    yield _parse_csi_seq(read_char)
                elif seq == "\r":
                    # Some terminals (e.g. tmux with a custom binding) emit
                    # ESC \r for Ctrl+Enter.
                    yield "ctrl_enter"
                else:
                    yield "ignore"
            elif ch == "\r":
                yield "enter"
            elif ch == "\n":
                yield "newline"
            elif ch in ("\x7f", "\x08"):
                yield "backspace"
            elif ch == "\x03":
                yield "ctrl_c"
            elif ch == "\x04":
                yield "ctrl_d"
            elif ch == "\x15":
                yield "ctrl_u"
            elif ch == "\t" or ord(ch) < 32:
                yield "ignore"
            else:
                yield ("char", ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)  # type: ignore[reportAttributeAccessIssue]


def _iter_keys_interrupt(stop_event):
    """Yield key tokens, polling so the reader thread can be stopped."""
    if os.name == "nt":
        yield from _iter_keys_windows_poll(stop_event)
    else:
        yield from _iter_keys_posix_poll(stop_event)


def check_double_esc(state: dict, token, now: float) -> bool:
    """Track ESC presses in `state` ({"last_esc": float}).

    Returns True when a second ESC arrives within DOUBLE_ESC_WINDOW seconds
    of the first; any other key resets the wait.
    """
    if token == "esc":
        if now - state["last_esc"] < DOUBLE_ESC_WINDOW:
            return True
        state["last_esc"] = now
    else:
        state["last_esc"] = 0.0
    return False


class StreamInterrupted(Exception):
    """The user interrupted a streaming response (double ESC)."""

    def __init__(self, message: dict):
        super().__init__("stream interrupted")
        self.message = message  # the partial assistant message


def _response_socket(resp):
    """Best-effort: the underlying socket of a urllib response (for shutdown).

    Older Pythons wrap the socket in a SocketFile (resp.fp.fp.raw._sock);
    Python 3.14+ removed it, so resp.fp is the BufferedReader directly
    (resp.fp.raw._sock). Try both layouts.
    """
    for attr in ("fp.fp.raw._sock", "fp.raw._sock"):
        try:
            obj = resp
            for part in attr.split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    return None


class InterruptWatcher:
    """Watches stdin on a background thread while a stream is in flight.

    Two ESC presses within DOUBLE_ESC_WINDOW seconds cancel the stream:
    the attached socket is closed so the main thread's blocked send or
    read unblocks, and `triggered` is set for the stream loop to check.
    The upload socket is attached while the request body is being sent
    (see _open_request's on_socket), so an interrupt can also abort the
    context send itself.
    """

    def __init__(self):
        self._stop = threading.Event()
        self._sock = None
        self._sock_lock = threading.Lock()
        self._thread = None
        self.triggered = False
        # Called after the "press esc again" hint is printed (the hint adds
        # 2 lines below any progress line; the spinner uses this to clear
        # the right line on stop).
        self.on_hint = None

    def attach_socket(self, sock):
        with self._sock_lock:
            self._sock = sock

    def start(self):
        if not sys.stdin.isatty():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=0.5)

    def _cancel(self):
        self.triggered = True
        with self._sock_lock:
            sock = self._sock
        if sock is not None:
            # close() (not shutdown()) unblocks a pending recv on Windows;
            # the stream loop swallows the resulting read error.
            try:
                sock.close()
            except OSError:
                pass

    def _run(self):
        state = {"last_esc": 0.0}
        hinted = False
        for token in _iter_keys_interrupt(self._stop):
            if check_double_esc(state, token, time.monotonic()):
                self._cancel()
                return
            if token == "esc" and not hinted:
                hinted = True
                sys.stdout.write(
                    "\n" + colorize("press esc again to interrupt", "dim") + "\n"
                )
                sys.stdout.flush()
                if self.on_hint is not None:
                    self.on_hint()


def _format_tokens(n: int) -> str:
    """Compact token count: 999, 1.5k, 2.3M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _format_elapsed(seconds: float) -> str:
    """Elapsed time: 5s, 1m05s."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


class ContextProgress:
    """Shows a "sending context" progress line while waiting for the LLM's
    first token, for large requests only (estimated >= CONTEXT_PROGRESS_THRESHOLD
    tokens). The line appears CONTEXT_PROGRESS_DELAY seconds in, reports the
    request-body upload (percent + tokens sent so far) while it is in flight,
    then the elapsed wait, and is cleared when the wait ends.

    Only active when stdout is a TTY. If lines are printed below the progress
    line (e.g. the double-ESC hint), the line freezes so a redraw can't
    clobber them, and stop() clears the right line.
    """

    def __init__(self, messages: list, interactive: bool = False):
        self.tokens = estimate_context_tokens(messages, interactive)
        self.upload = None  # _UploadProgress, set by _open_request before sending
        self._stop = threading.Event()
        self._thread = None
        self._drawn = False
        self._hint_before = False
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self.extra_lines = 0  # lines printed below the progress line

    @property
    def active(self) -> bool:
        return self._thread is not None

    def start(self):
        if self.tokens < CONTEXT_PROGRESS_THRESHOLD:
            return
        if not sys.stdout.isatty():
            return
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        with self._lock:
            if self._drawn:
                self._drawn = False
                if not self._hint_before and self.extra_lines:
                    # lines were printed below us (e.g. the esc hint): move
                    # back up to the progress line before clearing it
                    sys.stdout.write(f"\x1b[{self.extra_lines}A")
                sys.stdout.write("\r\x1b[2K")
                sys.stdout.flush()

    def note_lines_below(self, n: int):
        """Called when `n` lines are printed below the progress line."""
        self.extra_lines += n

    def _run(self):
        if self._stop.wait(CONTEXT_PROGRESS_DELAY):
            return
        with self._lock:
            self._hint_before = self.extra_lines > 0
            self._drawn = True
            sys.stdout.write(self._line())
            sys.stdout.flush()
        while not self._stop.is_set():
            if self._stop.wait(0.5):
                return
            if self.extra_lines:
                # something was printed below us (e.g. the esc hint); freeze
                # the line so the redraw can't clobber it
                continue
            with self._lock:
                sys.stdout.write("\r\x1b[2K" + self._line())
                sys.stdout.flush()

    def _line(self) -> str:
        elapsed = _format_elapsed(time.monotonic() - self._t0)
        upload = self.upload
        if upload is None or upload.sent >= upload.total:
            text = (
                f"{icon('wait')} waiting for first token "
                f"({_format_tokens(self.tokens)} tokens) {elapsed}"
            )
        else:
            pct = upload.sent * 100 // upload.total
            sent_tokens = self.tokens * upload.sent // upload.total
            text = (
                f"{icon('wait')} sending context… {pct}% "
                f"({_format_tokens(sent_tokens)}/{_format_tokens(self.tokens)} tokens) "
                f"{elapsed}"
            )
        return OUTPUT_INDENT + colorize(text, "dim")


def _edit_cur(row: int, total: int, width: int) -> tuple:
    """Cursor cell (row, col) after `total` display columns have been written
    into a run that starts at physical `row`, column 0.

    Soft wrap is *pending*: measured on Windows (CONOUT$ via
    GetConsoleScreenBufferInfo) a run that exactly fills a row leaves the
    cursor on that row's **last** column, and the next row is only entered
    when the next character is written — `ESC[nA/B/C/D` and `CR` act on that
    cell and clear the pending flag without moving the cursor, and `ESC[2K`
    erases that row. So the position of a character that lands exactly on a
    wrap boundary is the last cell of the row before it, not column 0 of the
    row after it (which is where the cursor would have to move to *first*).
    """
    if total <= 0:
        return row, 0
    # Rows the run occupies: a line that soft-wraps without ending on a column
    # boundary still takes the next row, while a run that ends exactly on a
    # boundary is only *pending* there, so it counts once.
    rows = (total + width - 1) // width
    col = width - 1 if total % width == 0 else total % width
    return row + rows - 1, col


def _edit_rows(text: str, prompt_w: int, width: int) -> int:
    """How many physical rows `prompt + text` paints once soft-wrapped."""
    rows = 0
    for i, ln in enumerate(text.split("\n")):
        total = (prompt_w if i == 0 else 0) + display_width(ln)
        rows += max(1, (total + width - 1) // width)  # an empty line is a row too
    return max(1, rows)


def _edit_phys_pos(text: str, prompt_w: int, pos: int, width: int) -> tuple:
    """Terminal cursor cell (row, col) for character position `pos` in `text`,
    printed after a prompt of display width `prompt_w` in a terminal `width`
    columns wide (see `_edit_cur` for the wrap model). `pos` may be len(text)
    (end of text)."""
    lines = text.split("\n")
    row = 0
    for i, ln in enumerate(lines):
        start = prompt_w if i == 0 else 0
        if pos <= len(ln):
            return _edit_cur(row, start + display_width(ln[:pos]), width)
        # Next logical line: the printed newline drops the cursor to column 0
        # of the row after the rows this line occupied.
        total = start + display_width(ln)
        row += max(1, (total + width - 1) // width)
        pos -= len(ln) + 1
    return row, 0


def _edit_line(prompt: str, keys) -> str:
    """Run a minimal line editor over a key-token iterator. Returns the line
    (may contain newlines inserted via the "newline" token, e.g. Ctrl+J, or
    via Enter when auto-send is off). Enter submits when AUTO_SEND is on;
    when it is off, Enter inserts a newline and Ctrl+Enter (or Ctrl+D) sends.

    Up/down first move the caret line-by-line within a multi-line buffer
    (column preserved, clamped to the target line). From the first line, up
    recalls history while saving the current line as a draft; the first down
    restores it (buffer and caret). Editing a recalled entry discards the
    draft, so subsequent downs walk history forward as usual."""
    buf = []
    pos = 0
    hist_idx = len(HISTORY)
    draft = None  # (buf, pos) of the line being edited when a history recall
    # started, so the first down press returns to it (with its caret)

    prev_rows = 0        # physical rows the previous render painted
    prev_cur_row = 0     # which of those rows the cursor ended on
    prev_rendered = False

    def render(commit=False):
        nonlocal prev_rows, prev_cur_row, prev_rendered
        line = "".join(buf)
        width = terminal_width()
        prompt_w = display_width(_ANSI_RE.sub("", prompt))
        # An explicit CR: in raw mode a bare LF only moves down, which would
        # leave every row after a newline shifted right by the column it fell on.
        text = line.replace("\n", "\r\n")
        out = ""
        if prev_rendered:
            # Walk back to the first physical row of the previous render and
            # clear every row it painted (cursor moves only, no newlines, so
            # the screen never scrolls while editing). The walk-back starts
            # from where the cursor actually *is* — the edit row whenever the
            # previous render repositioned it, the last row otherwise. Walking
            # back by the previous text's *end* row instead overshoots as soon
            # as the cursor sits above that row: each render then starts one
            # row too high, the block creeps up over the output above it, and
            # its old tail rows are left behind as duplicates.
            if prev_cur_row:
                out += f"\x1b[{prev_cur_row}A"
            out += "\r"
            for i in range(prev_rows):
                out += "\x1b[2K"
                if i < prev_rows - 1:
                    out += "\x1b[B"
            if prev_rows > 1:
                out += f"\x1b[{prev_rows - 1}A"
        rows = _edit_rows(line, prompt_w, width)
        out += prompt + text
        cur_row = rows - 1
        if commit:
            # Redrawn from the block's first row, so the caller's newline lands
            # on the fresh row right below it.
            out += "\r\n"
        elif pos < len(line):
            # Position the cursor at the edit position with cursor moves (not
            # by re-printing, which would re-draw every line after the first
            # newline). Cells are physical (row, col), so soft-wrapped lines
            # are handled too.
            trow, tcol = _edit_phys_pos(line, prompt_w, pos, width)
            erow, ecol = _edit_phys_pos(line, prompt_w, len(line), width)
            # After printing, the cursor is at the end of the text (erow,
            # ecol). The target is always at or above it (trow <= erow), so
            # move UP by the row difference (the column is preserved across
            # the up-move), then adjust the column toward the target. Moving
            # DOWN here would push the cursor into the blank rows below the
            # text, and each re-render would start from that drifted position
            # and re-print the previous line on every keystroke (it only went
            # unnoticed at the bottom of the screen, where a down-move is a
            # no-op).
            if erow > trow:
                out += f"\x1b[{erow - trow}A"
            if tcol != ecol:
                out += f"\x1b[{tcol - ecol}C" if tcol > ecol else f"\x1b[{ecol - tcol}D"
            cur_row = trow
        sys.stdout.write(out)
        sys.stdout.flush()
        prev_rows = rows
        prev_cur_row = cur_row
        prev_rendered = True

    def commit():
        """Leave the line on screen the way the user sees it and drop the cursor
        onto a fresh row below it. Needed because the cursor can be parked in
        the middle of a wrapped/multi-line block: without the redraw the tail
        rows stay painted and the next output prints over them."""
        render(commit=True)

    render()
    while True:
        token = next(keys)
        if token == "enter":
            if AUTO_SEND:
                break
            # Auto-send off: Enter inserts a newline; Ctrl+Enter sends.
            draft = None
            buf.insert(pos, "\n")
            pos += 1
        elif token == "ctrl_enter":
            break
        elif token == "ctrl_c":
            commit()
            raise KeyboardInterrupt
        elif token == "ctrl_d":
            if not buf:
                commit()
                raise EOFError
            break
        elif token == "backspace":
            if pos > 0:
                draft = None
                del buf[pos - 1]
                pos -= 1
        elif token == "delete":
            if pos < len(buf):
                draft = None
                del buf[pos]
        elif token == "ctrl_u":
            draft = None
            buf = buf[pos:]
            pos = 0
        elif token == "left":
            pos = max(0, pos - 1)
        elif token == "right":
            pos = min(len(buf), pos + 1)
        elif token == "home":
            pos = 0
        elif token == "end":
            pos = len(buf)
        elif token == "up":
            line = "".join(buf)
            if line.rfind("\n", 0, pos) >= 0:
                # Multi-line input: move the caret up within the buffer first
                # (column preserved, clamped to the line above) instead of
                # jumping to history and losing the draft.
                line_start = line.rfind("\n", 0, pos) + 1
                prev_start = line.rfind("\n", 0, line_start - 1) + 1
                pos = prev_start + min(pos - line_start, line_start - prev_start - 1)
            elif hist_idx > 0:
                # Caret on the first line: recall the previous history entry.
                # Leaving the *current* line for history (the first up) saves
                # it as the draft, so the first down press restores it; from
                # an empty line nothing is saved, so downs walk history
                # forward as usual (the last one clears the line).
                if hist_idx == len(HISTORY) and buf:
                    draft = (list(buf), pos)
                hist_idx -= 1
                buf = list(HISTORY[hist_idx])
                pos = len(buf)
        elif token == "down":
            if draft is not None:
                # First down after a history recall: restore the saved line
                # (buffer and caret).
                buf, pos = draft
                draft = None
                hist_idx = len(HISTORY)
            else:
                line = "".join(buf)
                if line.rfind("\n", pos) >= 0:
                    # Multi-line input with the caret above the last line:
                    # move down within the buffer (column preserved, clamped).
                    line_start = line.rfind("\n", 0, pos) + 1
                    next_start = line.find("\n", pos) + 1
                    next_end = line.find("\n", next_start)
                    next_len = (len(line) if next_end < 0 else next_end) - next_start
                    pos = next_start + min(pos - line_start, next_len)
                elif hist_idx < len(HISTORY):
                    hist_idx += 1
                    buf = list(HISTORY[hist_idx]) if hist_idx < len(HISTORY) else []
                    pos = len(buf)
        elif token == "newline":
            draft = None
            buf.insert(pos, "\n")
            pos += 1
        elif isinstance(token, tuple) and token[0] == "char":
            draft = None
            ch = token[1]
            if ch == "/" and not buf:
                # Slash on an empty line opens the command menu; a selection
                # replaces the line, a cancel keeps the typed slash.
                cmd = commands_menu()
                if cmd is not None:
                    buf = list(cmd)
                    pos = len(buf)
                else:
                    buf.insert(pos, ch)
                    pos += 1
            else:
                buf.insert(pos, ch)
                pos += 1
        render()
    commit()
    return "".join(buf)


def readline_prompt(prompt: str) -> str:
    """Read a line with arrow-key history. Falls back to input() when stdin
    is not a TTY (piped input, tests)."""
    if not sys.stdin.isatty():
        return input(prompt)
    try:
        keys = _iter_keys_windows() if os.name == "nt" else _iter_keys_posix()
    except Exception:
        return input(prompt)
    line = _edit_line(prompt, keys)
    _history_add(line)
    return line


# ---------------------------------------------------------------- client


def normalize_api_url(url: str | None) -> str:
    """Return the chat completions endpoint for a given API URL.

    Accepts either a full endpoint (``https://openrouter.ai/api/v1/chat/completions``)
    or just a base URL (``https://openrouter.ai/api/v1``), which gets
    ``/chat/completions`` appended. A trailing slash is tolerated.
    """
    endpoint = (url or "").strip().rstrip("/")
    if not endpoint or endpoint.endswith("/chat/completions"):
        return endpoint
    return endpoint + "/chat/completions"


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def probe_context_window() -> int:
    """Best-effort: query the API's /models endpoint for the context window size.

    Returns the model's context window in tokens, or 0 if it cannot be
    determined (server unreachable, no /models endpoint, field missing).
    """
    if not API_URL.endswith("/chat/completions"):
        return 0
    models_url = API_URL[: -len("/chat/completions")] + "/models"
    try:
        req = urllib.request.Request(models_url, headers=_headers())
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return 0
    models = data.get("data") or []
    if not models:
        return 0
    model = models[0]
    if len(models) > 1:
        for m in models:
            if m.get("id") == MODEL:
                model = m
                break

    def _as_int(v):
        return int(v) if isinstance(v, (int, float)) and v > 0 else 0

    # OpenAI/OpenRouter use context_length; llama.cpp exposes meta.n_ctx.
    return _as_int(model.get("context_length")) or _as_int(
        (model.get("meta") or {}).get("n_ctx")
    )


def _record_usage(messages: list, usage, conv_id: int = None) -> None:
    """Remember the server-reported token usage for a conversation.

    Keyed by an explicit conversation id when given (MAIN_CONV_ID for the
    top-level conversation, a unique counter per sub-agent run), else by
    id(messages). Explicit ids matter: id() is a memory address, and a
    freed sub-agent messages list can be reallocated at the same address,
    which would make /status resurrect stale sub-agent usage. Sub-agent
    entries are deleted when the sub-agent finishes (tool_task).
    """
    if isinstance(usage, dict) and usage.get("prompt_tokens"):
        USAGE_BY_CONV[conv_id if conv_id is not None else id(messages)] = usage


class _UploadProgress:
    """Bytes-sent counter for a request body (updated by the counting opener)."""

    def __init__(self, total: int):
        self.total = total
        self.sent = 0
        self.on_socket = None  # called with the connection socket once open

    def add(self, n: int):
        self.sent = min(self.total, self.sent + n)


def _progress_opener(upload: _UploadProgress) -> urllib.request.OpenerDirector:
    """A urllib opener whose HTTP(S) connections report body upload progress.

    Large writes are sent in PROGRESS_CHUNK pieces (http.client would
    otherwise sendall() the whole body at once, so no progress would be
    observable). All other urllib behavior (proxies, redirects, https,
    timeouts) is unchanged.
    """

    def make_handler(base):
        class Handler(base):
            # Before the stock HTTP/HTTPS handlers (order 500) so ours wins.
            handler_order = 499

            def do_open(self, http_class, request, **kwargs):
                class Counting(http_class):
                    def connect(self):
                        super().connect()
                        # Report the socket as soon as it exists so a
                        # double ESC can close it to abort the send.
                        if upload.on_socket is not None:
                            upload.on_socket(self.sock)

                    def send(self, data):
                        total = len(data)
                        if total <= PROGRESS_CHUNK:
                            return super().send(data)
                        mv = memoryview(data)
                        for i in range(0, total, PROGRESS_CHUNK):
                            piece = bytes(mv[i : i + PROGRESS_CHUNK])
                            super().send(piece)
                            upload.add(len(piece))

                return super(Handler, self).do_open(Counting, request, **kwargs)

        return Handler()

    opener = urllib.request.build_opener()
    opener.add_handler(make_handler(urllib.request.HTTPHandler))
    opener.add_handler(make_handler(urllib.request.HTTPSHandler))
    return opener


def _open_request(req: urllib.request.Request, progress=None, on_socket=None):
    """urlopen `req`; if `progress` (a ContextProgress) is given, track the
    request-body upload in it (progress.upload is set before sending); if
    `on_socket` is given it is called with the connection socket once open,
    so an interrupt can close it to abort the send."""
    if progress is None and on_socket is None:
        return urllib.request.urlopen(req, timeout=600)
    upload = _UploadProgress(len(req.data or b""))
    if progress is not None:
        progress.upload = upload
    upload.on_socket = on_socket
    return _progress_opener(upload).open(req, timeout=600)


def chat(
    messages: list,
    model: str,
    interactive: bool = False,
    temperature: float = 1.0,
    conv_id: int = None,
) -> dict:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "tools": _active_tools(interactive),
            "tool_choice": "auto",
            "temperature": temperature,
        }
    ).encode("utf-8")
    req = urllib.request.Request(API_URL, data=payload, headers=_headers())
    spinner = ContextProgress(messages, interactive)
    spinner.start()
    try:
        with _open_request(req, spinner if spinner.active else None) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    finally:
        spinner.stop()
    _record_usage(messages, data.get("usage"), conv_id)
    return data


def _build_request(
    messages: list,
    model: str,
    stream: bool,
    interactive: bool = False,
    temperature: float = 1.0,
) -> urllib.request.Request:
    body = {
        "model": model,
        "messages": messages,
        "tools": _active_tools(interactive),
        "tool_choice": "auto",
        "temperature": temperature,
        "stream": stream,
    }
    if stream:
        # Ask for a final usage chunk so /status can show exact token counts.
        body["stream_options"] = {"include_usage": True}
    payload = json.dumps(body).encode("utf-8")
    return urllib.request.Request(API_URL, data=payload, headers=_headers())


def parse_sse_line(line: str):
    """Parse one SSE line from a streaming chat response.

    Returns the delta dict for data lines, a {"usage": ...} dict for the
    final usage chunk (when stream_options.include_usage is set), the
    string "[DONE]" for the terminator, or None for comments/blank/
    malformed lines.
    """
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return "[DONE]"
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return None
    usage = chunk.get("usage")
    if usage:
        return {"usage": usage}
    choices = chunk.get("choices") or []
    if not choices:
        return None
    return choices[0].get("delta") or {}


def stream_chat(
    messages: list,
    model: str,
    interactive: bool = False,
    temperature: float = 1.0,
    watcher=None,
    progress=None,
    conv_id: int = None,
):
    """Yield deltas from a streaming chat response until [DONE].

    If `watcher` (an InterruptWatcher) is given, the connection and response
    sockets are attached to it and the stream stops once it is triggered: a
    double ESC during the request-body upload closes the upload socket and
    aborts the send. If `progress` (a ContextProgress) is given, the
    request-body upload is tracked in it.
    """
    req = _build_request(
        messages, model, stream=True, interactive=interactive, temperature=temperature
    )
    try:
        resp_cm = _open_request(req, progress, watcher.attach_socket if watcher else None)
    except (OSError, http.client.HTTPException):
        # An interrupt during the context send closes the socket, which
        # surfaces as a send error; swallow it only when the interrupt
        # caused it.
        if watcher is None or not watcher.triggered:
            raise
        return
    with resp_cm as resp:
        if watcher is not None:
            watcher.attach_socket(_response_socket(resp))
        try:
            for raw in resp:
                if watcher is not None and watcher.triggered:
                    break
                parsed = parse_sse_line(raw.decode("utf-8"))
                if parsed is None:
                    continue
                if parsed == "[DONE]":
                    break
                if "usage" in parsed:
                    _record_usage(messages, parsed["usage"], conv_id)
                    continue
                yield parsed
        except (OSError, http.client.HTTPException):
            # An interrupt closes the socket, which surfaces as a read
            # error here; swallow it only when the interrupt caused it.
            if watcher is None or not watcher.triggered:
                raise


def accumulate_delta(message: dict, delta: dict) -> dict:
    """Merge a streaming delta into an assistant message in place."""
    if not delta:
        return message
    content = delta.get("content")
    if content:
        message["content"] = message.get("content", "") + content
    reasoning = delta.get("reasoning_content")
    if reasoning:
        message["reasoning_content"] = message.get("reasoning_content", "") + reasoning
    for tc in delta.get("tool_calls") or []:
        idx = int(tc.get("index", 0))
        calls = message.setdefault("tool_calls", [])
        while len(calls) <= idx:
            calls.append(
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            )
        target = calls[idx]
        if tc.get("id"):
            target["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            target["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            target["function"]["arguments"] += fn["arguments"]
    return message


def display_width(text: str) -> int:
    """Approximate terminal display width (East Asian wide/fullwidth chars count as 2)."""
    width = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


_MD_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")
_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_MD_HR = re.compile(r"^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$")
_MD_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_MD_ESCAPABLE = frozenset(r"!\"#$%&'()*+,-./:;<=>?@[\]^_`{|}~" + "\\")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\s](?:[^*]*[^*\s])?)\*(?!\*)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# Delimiter row of a pipe table. The trailing group is optional so a
# single-column table (| A | / |---|) is recognized; callers must also
# require a '|' so a bare "---" horizontal rule is never mistaken for one.
_MD_TABLE_SEP = re.compile(r"^\s{0,3}\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_MD_PIPE_SPLIT = re.compile(r"(?<!\\)\|")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_TABLE_MIN_COL = 3


def terminal_width() -> int:
    """Current terminal column count (falls back to 80)."""
    return shutil.get_terminal_size((80, 24)).columns


def _is_table_sep(line: str) -> bool:
    """True if line is a table delimiter row (must contain a pipe)."""
    return "|" in line and bool(_MD_TABLE_SEP.match(line))


def _split_table_row(line: str) -> list:
    """Split a pipe-table row into stripped cell strings.

    Leading/trailing pipes are removed; \\| stays inside its cell.
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in _MD_PIPE_SPLIT.split(s)]


def _table_alignments(sep: str, ncols: int) -> list:
    """Per-column alignment ('left'/'center'/'right') from a delimiter row."""
    aligns = []
    for cell in _split_table_row(sep)[:ncols]:
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        else:
            aligns.append("left")
    return aligns + ["left"] * (ncols - len(aligns))


def _truncate_visible(text: str, width: int) -> str:
    """Truncate plain text to a display width, appending an ellipsis if cut."""
    if display_width(text) <= width:
        return text
    if width <= 0:
        return ""
    if width == 1:
        return "…"
    out = ""
    used = 0
    for ch in text:
        w = display_width(ch)
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


class MarkdownRenderer:
    """Incremental Markdown-to-ANSI renderer for streaming assistant output.

    Feed arbitrary chunks via write(); complete lines are styled as they
    arrive, so markers split across chunks are handled. flush() emits any
    trailing partial line and clears open code-fence state. With colors
    disabled, output is the raw input unchanged (safe for pipes).

    Pipe tables are buffered whole (a line containing '|' is held for one
    line to see whether a delimiter row follows) so columns can be aligned
    to the widest cell before anything is printed. When a box cannot fit,
    cells are rendered as wrapped, plain-text header/value records.
    This is a lightweight Markdown subset, not a full CommonMark parser.
    """

    def __init__(self, out=None, indent: int = 0):
        self._out = out if out is not None else sys.stdout
        self._indent = " " * max(indent, 0)
        self._buf = ""
        self._in_code = False
        self._fence = ""
        self._first = True
        self._pending = None  # possible table row awaiting a delimiter row
        self._table = None  # rows of the table currently being buffered

    def write(self, chunk: str):
        if not COLORS_ENABLED:
            self._out.write(chunk)
            self._out.flush()
            return
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)

    def flush(self):
        if not COLORS_ENABLED:
            return
        if self._buf:
            self._emit(self._buf)
            self._buf = ""
        # Drain any deferred table state (force-emit so nothing re-defers).
        while self._pending is not None or self._table is not None:
            if self._pending is not None:
                pending, self._pending = self._pending, None
                self._emit(pending, defer=False)
            if self._table is not None:
                table, self._table = self._table, None
                self._render_table(table)
        self._in_code = False
        self._fence = ""

    def _emit(self, line: str, defer: bool = True):
        # write() splits on LF; normalize CRLF only in styled output.
        line = line.removesuffix("\r")
        if self._in_code:
            prefix = "" if self._first else self._indent
            self._first = False
            fence = _MD_FENCE.match(line)
            if (fence and fence.group(1)[0] == self._fence[0]
                    and len(fence.group(1)) >= len(self._fence)
                    and not fence.group(2).strip()):
                self._in_code = False
                self._line(prefix + colorize(fence.group(1), "codeblock"))
            else:
                self._line(prefix + "  " + colorize(line, "codeblock"))
            return
        if self._pending is not None:
            pending, self._pending = self._pending, None
            if (_is_table_sep(line)
                    and len(_split_table_row(pending)) == len(_split_table_row(line))):
                self._table = [pending, line]
                return
            self._emit(pending, defer=False)
        fence = _MD_FENCE.match(line)
        if fence and fence.group(1)[0] == "`" and "`" in fence.group(2):
            fence = None
        if self._table is not None:
            if (line.strip() and "|" in line and not fence
                    and not _MD_HEADING.match(line) and not _MD_QUOTE.match(line)
                    and not _MD_LIST.match(line)):
                self._table.append(line)
                return
            table, self._table = self._table, None
            self._render_table(table)
        if fence:
            prefix = "" if self._first else self._indent
            self._first = False
            self._in_code = True
            self._fence = fence.group(1)
            lang = fence.group(2).strip()
            self._line(prefix + colorize(self._fence + lang, "codeblock"))
            return
        if defer and line.strip() and "|" in line and not _is_table_sep(line):
            # Might be a table header; wait for the next line to decide.
            self._pending = line
            return
        prefix = "" if self._first else self._indent
        self._first = False
        m = _MD_HEADING.match(line)
        if m:
            self._line(prefix + colorize(self._inline(m.group(2).strip()), "heading"))
            return
        if _MD_HR.match(line):
            self._line(prefix + colorize("─" * 20, "dim"))
            return
        m = _MD_LIST.match(line)
        if m:
            pad, marker, rest = m.groups()
            bullet = "• " if marker in ("-", "*", "+") else f"{marker} "
            self._line(prefix + pad + colorize(bullet, "bullet") + self._inline(rest))
            return
        m = _MD_QUOTE.match(line)
        if m:
            self._line(prefix + colorize("> " + self._inline(m.group(1)), "quote"))
            return
        self._line(prefix + self._inline(line))

    def _render_table(self, rows: list):
        """Render buffered table rows as an aligned box-drawing table."""
        header = _split_table_row(rows[0])
        aligns = _table_alignments(rows[1], len(header))
        body = [_split_table_row(r) for r in rows[2:]]
        ncols = len(header)
        if not ncols:
            return
        body = [(r + [""] * ncols)[:ncols] for r in body]
        plain = [header] + body

        widths = []
        for i in range(ncols):
            w = max(
                display_width(_ANSI_RE.sub("", self._inline(row[i])))
                for row in plain
            )
            widths.append(max(w, _TABLE_MIN_COL))

        columns = max(1, terminal_width())
        # Reserve the label width even when the first line has no prefix.
        # On exceptionally narrow terminals, start below the label and reduce
        # indentation to leave room for at least one wide character.
        indent = self._indent[:max(0, columns - 2)]
        if self._first and indent != self._indent:
            self._line("")
            self._first = False
        available = columns - len(indent)

        def table_line(text):
            prefix = "" if self._first else indent
            self._line(prefix + text)
            self._first = False

        budget = available - (3 * ncols + 1)
        if budget < ncols * _TABLE_MIN_COL:
            # A box cannot fit: preserve cells as wrapped header/value records.
            # Plain text avoids splitting ANSI escapes when wrapping.
            def wrapped(text):
                chunk, used = "", 0
                for ch in text:
                    w = display_width(ch)
                    if w > available:
                        ch, w = "�", 1
                    if used + w > available:
                        table_line(chunk)
                        chunk, used = "", 0
                    chunk += ch
                    used += w
                table_line(chunk)

            labels = [_ANSI_RE.sub("", self._inline(c)) for c in header]
            for index, record in enumerate(body or [None]):
                if index:
                    table_line("")
                for i, label in enumerate(labels):
                    value = (_ANSI_RE.sub("", self._inline(record[i]))
                             if record is not None else None)
                    wrapped(label if value is None else f"{label}: {value}")
            return
        while sum(widths) > budget and max(widths) > _TABLE_MIN_COL:
            i = widths.index(max(widths))
            widths[i] -= 1

        def rule(left, mid, right):
            return left + mid.join("─" * (w + 2) for w in widths) + right

        def cell(text, i, align):
            width = widths[i]
            w = display_width(_ANSI_RE.sub("", text))
            pad = max(width - w, 0)
            if align == "right":
                left, right = pad, 0
            elif align == "center":
                left, right = pad // 2, pad - pad // 2
            else:
                left, right = 0, pad
            return " " * left + text + " " * right

        def row(cells):
            parts = [colorize(c, k) for c, k in cells]
            inner = colorize("│", "dim").join(
                " " + cell(parts[i], i, aligns[i]) + " " for i in range(ncols)
            )
            return colorize("│", "dim") + inner + colorize("│", "dim")

        def styled(r):
            # Style first so code markers do not affect widths. Truncated
            # cells use plain visible text to avoid cutting an ANSI escape.
            out = []
            for i in range(ncols):
                visible = self._inline(r[i])
                if display_width(_ANSI_RE.sub("", visible)) > widths[i]:
                    visible = _truncate_visible(_ANSI_RE.sub("", visible), widths[i])
                out.append(visible)
            return out

        table_line(colorize(rule("┌", "┬", "┐"), "dim"))
        table_line(row([(t, "heading") for t in styled(header)]))
        table_line(colorize(rule("├", "┼", "┤"), "dim"))
        for r in body:
            table_line(row([(t, None) for t in styled(r)]))
        table_line(colorize(rule("└", "┴", "┘"), "dim"))

    def _line(self, text: str):
        self._out.write(text + "\n")
        self._out.flush()

    def _inline(self, text: str) -> str:
        # Tokenize literal spans before emphasis, so neither escape sequences,
        # code contents nor link destinations can become formatting markers.
        marker = "\x00"
        while marker in text:
            marker += "\x00"
        protected = []
        parts = []

        def protect(value):
            parts.append(f"{marker}{len(protected)}{marker}")
            protected.append(value)

        i = 0
        while i < len(text):
            if (text[i] == "\\" and i + 1 < len(text)
                    and text[i + 1] in _MD_ESCAPABLE):
                protect(text[i + 1])
                i += 2
                continue
            if text[i] == "`":
                end = i + 1
                while end < len(text) and text[end] == "`":
                    end += 1
                ticks = text[i:end]
                closing = re.search(r"(?<!`)" + ticks + r"(?!`)", text[end:])
                if closing:
                    stop = end + closing.start()
                    code = text[end:stop]
                    if code.startswith(" ") and code.endswith(" ") and code.strip():
                        code = code[1:-1]
                    protect(colorize(code, "code"))
                    i = stop + len(ticks)
                    continue
                parts.append(ticks)
                i = end
                continue
            if text[i] == "[":
                link = _MD_LINK.match(text, i)
                if link:
                    protect(colorize(self._inline(link.group(1)), "underline")
                            + colorize(f" ({link.group(2)})", "dim"))
                    i = link.end()
                    continue
            parts.append(text[i])
            i += 1
        rendered = self._emphasis("".join(parts))
        # A single substitution avoids interpreting placeholders in restored text.
        return re.sub(re.escape(marker) + r"(\d+)" + re.escape(marker),
                      lambda m: protected[int(m.group(1))], rendered)

    @staticmethod
    def _emphasis(text: str) -> str:
        text = _MD_BOLD.sub(lambda m: colorize(m.group(1), "bold"), text)
        return _MD_ITALIC.sub(lambda m: colorize(m.group(1), "italic"), text)


def stream_once(
    messages: list,
    model: str,
    interactive: bool = False,
    temperature: float = 1.0,
    conv_id: int = None,
) -> tuple[dict, bool]:
    """Stream one chat turn, printing reasoning and content live.

    Returns (message, streamed) where streamed is False if no deltas
    arrived (e.g. server ignored stream mode) — caller should fall back.
    Raises StreamInterrupted (carrying the partial message) if the user
    interrupts with a double ESC.
    """
    message = {"role": "assistant"}
    started_reasoning = False
    renderer = None
    streamed = False
    watcher = InterruptWatcher() if INTERRUPT_ENABLED else None
    if watcher is not None:
        watcher.start()
    spinner = ContextProgress(messages, interactive)
    spinner.start()
    if watcher is not None:
        # the esc hint prints 2 lines below the progress line; tell the
        # spinner so it can clear the right line on stop
        watcher.on_hint = lambda: spinner.note_lines_below(2)
    try:
        for delta in stream_chat(
            messages,
            model,
            interactive=interactive,
            temperature=temperature,
            watcher=watcher,
            progress=spinner if spinner.active else None,
            conv_id=conv_id,
        ):
            spinner.stop()
            streamed = True
            reasoning = delta.get("reasoning_content")
            if reasoning:
                if not started_reasoning:
                    print(
                        OUTPUT_INDENT
                        + colorize(f"{icon('thinking')} thinking: ", "thinking"),
                        end="",
                        flush=True,
                    )
                    started_reasoning = True
                print(reasoning, end="", flush=True)
            content = delta.get("content")
            if content:
                if renderer is None:
                    if started_reasoning:
                        print()
                    label = f"{icon('assistant')} assistant> "
                    print(
                        OUTPUT_INDENT + colorize(label, "assistant"),
                        end="",
                        flush=True,
                    )
                    renderer = MarkdownRenderer(
                        indent=display_width(OUTPUT_INDENT + label)
                    )
                renderer.write(content)
            accumulate_delta(message, delta)
    finally:
        if watcher is not None:
            watcher.stop()
        spinner.stop()
    if watcher is not None and watcher.triggered:
        if started_reasoning:
            print()
        if renderer is not None:
            renderer.flush()
            print()
        print(OUTPUT_INDENT + colorize("(interrupted)", "dim"))
        raise StreamInterrupted(message)
    if started_reasoning:
        print()
    if renderer is not None:
        renderer.flush()
        print()
    return message, streamed


def print_assistant(content: str):
    """Print a complete (non-streamed) assistant message with Markdown styling."""
    label = f"{icon('assistant')} assistant> "
    print()
    print(OUTPUT_INDENT + colorize(label, "assistant"), end="", flush=True)
    renderer = MarkdownRenderer(indent=display_width(OUTPUT_INDENT + label))
    renderer.write(content or "")
    renderer.flush()
    print()


def execute_tool(name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        return f"error: invalid JSON arguments: {raw_args}"
    if name in DISABLED_TOOLS:
        return f"error: tool '{name}' is disabled (use /tools to re-enable)"
    fn = DISPATCH.get(name)
    if fn is None:
        mcp = MCP_DISPATCH.get(name)
        if mcp is not None:
            client, tool_name = mcp
            try:
                result = client.call_tool(tool_name, args)
            except MCPError as e:
                return f"error: {e}"
            except Exception as e:
                return f"error: {type(e).__name__}: {e}"
        else:
            return f"error: unknown tool: {name}"
    else:
        try:
            result = str(fn(args))
        except ExitSignal:
            raise
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"
    # Final backstop: no single tool result may exceed this, whatever the
    # built-in tool (or an external MCP server) decided to return.
    return _truncate(
        result,
        TOOL_RESULT_LIMIT,
        unit="chars",
        hint="output exceeded the harness limit; split the work or narrow the request",
    )


def _active_tools(interactive: bool) -> list:
    """Tool specs to send to the API, excluding tools toggled off via /tools."""
    base = OPENAI_TOOLS_INTERACTIVE if interactive else OPENAI_TOOLS
    return [s for s in base + MCP_TOOLS if s["function"]["name"] not in DISABLED_TOOLS]


def _all_tool_names() -> set:
    return {s["function"]["name"] for s in OPENAI_TOOLS_INTERACTIVE + MCP_TOOLS}


def format_tools() -> str:
    """Build the /tools checklist: '[X] name — description' per tool.

    MCP tools are grouped under their server ('[X] mcp: server (N tools)'),
    mirroring the interactive menu.
    """
    lines = []
    for s in sorted(OPENAI_TOOLS_INTERACTIVE, key=lambda s: s["function"]["name"]):
        name = s["function"]["name"]
        desc = s["function"].get("description", "")
        mark = " " if name in DISABLED_TOOLS else "X"
        lines.append(f"[{mark}] {name} — {desc}")
    mcp_descs = {s["function"]["name"]: s["function"].get("description", "") for s in MCP_TOOLS}
    mcp_by_server = {}
    for name, (client, _) in MCP_DISPATCH.items():
        mcp_by_server.setdefault(client.name, []).append(name)
    server_order = [c.name for c in MCP_CLIENTS if c.name in mcp_by_server]
    if server_order:
        lines.append("mcp servers:")
        for server in server_order:
            tools = sorted(mcp_by_server[server])
            off = sum(1 for t in tools if t in DISABLED_TOOLS)
            mark = "X" if off == 0 else (" " if off == len(tools) else "-")
            lines.append(f"[{mark}] mcp: {server} ({len(tools)} tools)")
            for t in tools:
                tmark = " " if t in DISABLED_TOOLS else "X"
                lines.append(f"  [{tmark}] {t} — {mcp_descs.get(t, '')}")
    if PENDING_MCP:
        lines.append("mcp servers (disabled in config):")
        for n, _ in PENDING_MCP:
            lines.append(f"[ ] {n}")
    return "\n".join(lines)


def toggle_tools(names: list) -> list:
    """Toggle the given tool names on/off. Returns [(name, 'on'|'off'|'unknown')].

    A name of the form 'mcp:<server>' toggles every tool of that server at
    once (any off -> all on, otherwise all off).
    """
    known = _all_tool_names()
    results = []
    for n in names:
        if n.startswith("mcp:"):
            server = n[len("mcp:"):]
            tools = _mcp_server_tools(server)
            if not tools:
                results.append((n, "unknown"))
            else:
                any_off = any(t in DISABLED_TOOLS for t in tools)
                for t in tools:
                    if any_off:
                        DISABLED_TOOLS.discard(t)
                    else:
                        DISABLED_TOOLS.add(t)
                results.append((n, "on" if any_off else "off"))
        elif n not in known:
            results.append((n, "unknown"))
        elif n in DISABLED_TOOLS:
            DISABLED_TOOLS.discard(n)
            results.append((n, "on"))
        else:
            DISABLED_TOOLS.add(n)
            results.append((n, "off"))
    return results


def tools_menu(keys=None) -> bool:
    """Interactive tool toggle menu.

    Built-in tools are listed flat; mcp tools are grouped under their server
    entry, collapsed by default — '+'/'=' expands the group under the cursor,
    '-' collapses it. Space on a server row toggles all of its tools at once
    (individual tools can be toggled on their own rows when expanded). A
    disabled-in-config mcp server is enabled by toggling its row. Up/down
    move the cursor (skipping collapsed rows), enter applies the changes and
    quits, esc (or ctrl+c/ctrl+d) quits without applying them. Returns True
    if changes were applied, False if cancelled.
    """
    builtin_specs = sorted(
        OPENAI_TOOLS_INTERACTIVE, key=lambda s: s["function"]["name"]
    )
    mcp_by_server = {}
    for name, (client, _) in MCP_DISPATCH.items():
        mcp_by_server.setdefault(client.name, []).append(name)
    server_order = [c.name for c in MCP_CLIENTS if c.name in mcp_by_server]
    pending_names = [n for n, _ in PENDING_MCP]
    rows = [("tool", s["function"]["name"]) for s in builtin_specs]
    for server in server_order:
        rows.append(("server", server))
        for t in sorted(mcp_by_server[server]):
            rows.append(("mcp_tool", t))
    rows += [("pending", n) for n in pending_names]
    if not rows:
        return False
    descs = {s["function"]["name"]: s["function"].get("description", "") for s in builtin_specs}
    for s in MCP_TOOLS:
        descs[s["function"]["name"]] = s["function"].get("description", "")
    disabled = set(DISABLED_TOOLS)
    pending_on = set()
    collapsed = set(server_order)  # mcp groups start collapsed
    tool_server = {}
    for server in server_order:
        for t in mcp_by_server[server]:
            tool_server[t] = server
    cursor = 0
    width = shutil.get_terminal_size((80, 24)).columns
    applied = False
    enable_msgs = []
    n_builtin = len(builtin_specs)
    n_server_rows = sum(1 + len(mcp_by_server[s]) for s in server_order)

    def row_desc(kind, name):
        if kind == "pending":
            return f"mcp server '{name}' (disabled in config)"
        if kind == "server":
            return (
                f"mcp server '{name}' — {len(mcp_by_server[name])} tools "
                "(space toggles all, +/- expand/collapse)"
            )
        return descs.get(name, "")

    def server_mark(server):
        tools = mcp_by_server[server]
        off = sum(1 for t in tools if t in disabled)
        if off == 0:
            return "x"
        if off == len(tools):
            return " "
        return "-"

    def visible_indices():
        return [
            i
            for i, (kind, name) in enumerate(rows)
            if not (kind == "mcp_tool" and tool_server[name] in collapsed)
        ]

    def build_lines():
        lines = [
            colorize(
                "tools — space: toggle, +/-: expand/collapse, enter: apply, esc: cancel",
                "dim",
            )
        ]
        for i, (kind, name) in enumerate(rows):
            if kind == "mcp_tool" and tool_server[name] in collapsed:
                continue
            if i == n_builtin and server_order:
                lines.append(colorize("  mcp servers:", "dim"))
            if i == n_builtin + n_server_rows and pending_names:
                lines.append(colorize("  mcp servers (disabled in config):", "dim"))
            if kind == "server":
                mark = server_mark(name)
            elif kind == "pending":
                mark = "x" if name in pending_on else " "
            else:
                mark = " " if name in disabled else "x"
            indent = "    " if kind == "mcp_tool" else "  "
            if i == cursor:
                lines.append(colorize(f">{indent[1:]}[{mark}] {name}", "tool"))
            else:
                lines.append(f"{indent}[{mark}] {name}")
        desc = row_desc(*rows[cursor])
        if len(desc) > width - 1:
            desc = desc[: width - 4] + "..."
        lines.append(colorize(desc, "dim"))
        return lines

    drawn = 0

    def draw():
        nonlocal drawn
        lines = build_lines()
        for line in lines:
            sys.stdout.write("\r\x1b[2K" + line + "\n")
        if lines:
            sys.stdout.write(f"\x1b[{len(lines)}A")
        sys.stdout.flush()
        drawn = len(lines)

    def move(step):
        nonlocal cursor
        vis = visible_indices()
        pos = vis.index(cursor)
        cursor = vis[max(0, min(len(vis) - 1, pos + step))]

    if keys is None:
        keys = _iter_keys_windows() if os.name == "nt" else _iter_keys_posix()
    draw()
    try:
        while True:
            token = next(keys)
            if token == "up":
                move(-1)
            elif token == "down":
                move(1)
            elif token in (("char", "+"), ("char", "=")):
                kind, name = rows[cursor]
                if kind == "server":
                    collapsed.discard(name)
            elif token == ("char", "-"):
                kind, name = rows[cursor]
                if kind == "server":
                    collapsed.add(name)
            elif token in ("enter", "ctrl_enter"):
                DISABLED_TOOLS.clear()
                DISABLED_TOOLS.update(disabled)
                for name in pending_on:
                    _, msg = _enable_pending_mcp(name)
                    enable_msgs.append(msg)
                applied = True
                break
            elif token in ("esc", "ctrl_c", "ctrl_d"):
                break
            elif token == ("char", " "):
                kind, name = rows[cursor]
                if kind == "pending":
                    if name in pending_on:
                        pending_on.discard(name)
                    else:
                        pending_on.add(name)
                elif kind == "server":
                    tools = mcp_by_server[name]
                    if any(t in disabled for t in tools):
                        disabled.difference_update(tools)
                    else:
                        disabled.update(tools)
                else:
                    if name in disabled:
                        disabled.discard(name)
                    else:
                        disabled.add(name)
            draw()
    except StopIteration:
        pass
    finally:
        close = getattr(keys, "close", None)
        if close is not None:
            close()
        if drawn:
            sys.stdout.write(f"\x1b[{drawn}B")
            sys.stdout.flush()
    for msg in enable_msgs:
        print(msg)
    return applied


def estimate_context_chars(messages: list, interactive: bool = False) -> int:
    """Approximate character count of the next request payload.

    Counts the tool schemas (sent with every request) plus all message
    content (text, reasoning, tool-call arguments). Dividing by 4 gives a
    rough token estimate — the same heuristic /status uses.
    """
    total = len(json.dumps(_active_tools(interactive)))
    for m in messages:
        total += _content_chars(m.get("content"))
        total += len(m.get("reasoning_content") or "")
        for tc in m.get("tool_calls") or []:
            total += len((tc.get("function") or {}).get("arguments") or "")
    return total


def estimate_context_tokens(messages: list, interactive: bool = False) -> int:
    """Rough token count of the next request (chars/4)."""
    return estimate_context_chars(messages, interactive) // 4


def format_status(messages: list, context_window: int = 0, conv_id: int = None) -> str:
    """Build the /status report: context usage, API URL, tool names, MCP servers."""
    tools = _active_tools(True)
    total_chars = estimate_context_chars(messages, interactive=True)
    approx_tokens = total_chars // 4
    usage = USAGE_BY_CONV.get(conv_id if conv_id is not None else id(messages))
    prompt_tokens = (
        int(usage["prompt_tokens"])
        if isinstance(usage, dict) and usage.get("prompt_tokens")
        else 0
    )
    tool_names = ", ".join(sorted(s["function"]["name"] for s in tools))
    if prompt_tokens:
        ctx = f"context: {prompt_tokens} tokens (server-reported) in {len(messages)} messages"
    else:
        ctx = f"context: ~{approx_tokens} tokens (estimated, ~{total_chars} chars) in {len(messages)} messages"
    if context_window > 0:
        used = prompt_tokens or approx_tokens
        pct = used * 100 // context_window
        pct_str = "<1%" if pct == 0 and used > 0 else f"~{pct}%"
        ctx += f" ({pct_str} of {context_window} window)"
    lines = [
        ctx,
        f"api url: {API_URL}",
        f"tools: {tool_names}",
    ]
    if MCP_CLIENTS:
        mcp_lines = []
        for c in MCP_CLIENTS:
            names = ", ".join(c.tool_names) if c.tool_names else "no tools"
            mcp_lines.append(f"  {c.name} ({c.transport}): {names}")
        lines.append("mcp servers:\n" + "\n".join(mcp_lines))
    if PENDING_MCP:
        pend_lines = [f"  {n} (disabled in config; enable via /tools)" for n, _ in PENDING_MCP]
        lines.append("mcp servers (disabled):\n" + "\n".join(pend_lines))
    return "\n".join(lines)


REPL_COMMANDS = [
    ("/new", "clear session history and start over"),
    ("/clear-screen", "clear the terminal screen"),
    ("/status", "show context usage, api url, and tools"),
    ("/tools", "interactive tool menu: up/down move, space toggle, enter apply, esc cancel"),
    ("/auto-send", "on: Enter submits (default); off: Enter inserts a newline, Ctrl+Enter sends"),
    ("/help", "show this help"),
    ("/exit", "quit (alias: /quit)"),
]


def set_auto_send(arg: str) -> str:
    """Apply an /auto-send argument; return the status line to print.

    Returns '' for an unknown argument (the caller prints a usage error).
    """
    global AUTO_SEND
    v = arg.strip().lower()
    if v in ("on", "true", "yes", "1"):
        AUTO_SEND = True
        return "auto-send on — Enter submits, Ctrl+J inserts a newline"
    if v in ("off", "false", "no", "0"):
        AUTO_SEND = False
        return "auto-send off — Enter inserts a newline, Ctrl+Enter (or Ctrl+D) submits"
    if not v:
        return f"auto-send: {'on' if AUTO_SEND else 'off'}"
    return ""


def commands_menu(keys=None) -> str | None:
    """Interactive REPL command menu, opened by typing / on an empty line.

    Up/down move the cursor, enter selects the highlighted command (returned),
    esc (or ctrl+c/ctrl+d) cancels (returns None). Draws below the prompt
    line and restores the cursor to it on exit.
    """
    names = [name for name, _ in REPL_COMMANDS]
    descs = [desc for _, desc in REPL_COMMANDS]
    cursor = 0
    width = shutil.get_terminal_size((80, 24)).columns

    def build_lines():
        lines = [colorize("commands — up/down: move, enter: select, esc: cancel", "dim")]
        for i, name in enumerate(names):
            if i == cursor:
                lines.append(colorize(f"> {name}", "tool"))
            else:
                lines.append(f"  {name}")
        desc = descs[cursor]
        if len(desc) > width - 1:
            desc = desc[: width - 4] + "..."
        lines.append(colorize(desc, "dim"))
        return lines

    drawn = 0

    def draw():
        nonlocal drawn
        lines = build_lines()
        for line in lines:
            sys.stdout.write("\r\x1b[2K" + line + "\n")
        if lines:
            sys.stdout.write(f"\x1b[{len(lines)}A")
        sys.stdout.flush()
        drawn = len(lines)

    if keys is None:
        keys = _iter_keys_windows() if os.name == "nt" else _iter_keys_posix()
    # Start on the line below the prompt (the cursor sits at its end).
    sys.stdout.write("\r\n")
    sys.stdout.flush()
    draw()
    try:
        while True:
            token = next(keys)
            if token == "up":
                cursor = max(0, cursor - 1)
            elif token == "down":
                cursor = min(len(names) - 1, cursor + 1)
            elif token in ("enter", "ctrl_enter"):
                return names[cursor]
            elif token in ("esc", "ctrl_c", "ctrl_d"):
                return None
            draw()
    except StopIteration:
        return None
    finally:
        close = getattr(keys, "close", None)
        if close is not None:
            close()
        if drawn:
            # Cursor is on the first menu line: clear each menu line, then
            # return to the prompt line.
            out = ""
            for i in range(drawn):
                out += "\x1b[2K"
                if i < drawn - 1:
                    out += "\x1b[B"
            out += f"\x1b[{drawn}A"
            sys.stdout.write(out)
            sys.stdout.flush()


def format_help() -> str:
    """Build the /help report: list of REPL commands and file references."""
    lines = [f"{name:<16} {desc}" for name, desc in REPL_COMMANDS]
    lines.append(f"{'/tools <name>':<16} toggle a tool on/off ('mcp:<server>' toggles all of a server's tools)")
    lines.append("Ctrl+J          insert a newline (multi-line input)")
    lines.append("Enter           submit (auto-send on) — insert a newline (auto-send off)")
    lines.append("Ctrl+Enter      submit (auto-send off; Ctrl+D also sends)")
    lines.append(f"{'esc esc':<16} interrupt generation (press twice within 2 s)")
    return (
        "\n".join(lines)
        + "\n"
        "\n"
        "file references (attach a file to your message):\n"
        "  @[cwd://relative/path]   file relative to the working directory\n"
        "  @[file:///abs/path]      absolute local file\n"
        "  @[http(s)://host/file]   remote file\n"
        "text is inlined; images are sent when the model supports vision"
    )


# ---------------------------------------------------------------- loop


def run_agent(
    messages: list,
    model: str,
    interactive: bool = False,
    temperature: float = 1.0,
    depth: int = 0,
    conv_id: int = None,
) -> int:
    global _AGENT_DEPTH
    _AGENT_DEPTH = depth
    while True:
        message = None
        streamed = False
        try:
            message, streamed = stream_once(
                messages,
                model,
                interactive=interactive,
                temperature=temperature,
                conv_id=conv_id,
            )
            if not streamed:
                message = None
        except StreamInterrupted as e:
            # Keep the partial output (minus any half-formed tool calls,
            # which would leave the conversation in an invalid state) so
            # the model can see what it had said, then back out. Partials
            # without content (e.g. interrupted mid-thinking) are dropped:
            # servers reject assistant messages that have neither content
            # nor tool_calls, which would break every later request.
            message = e.message
            message.pop("tool_calls", None)
            if message.get("content"):
                messages.append(message)
            return 0
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            print(
                OUTPUT_INDENT
                + colorize(
                    f"{icon('error')} streaming failed ({e}); retrying non-streaming",
                    "error",
                )
            )
        if message is None:
            try:
                data = chat(
                    messages,
                    model,
                    interactive=interactive,
                    temperature=temperature,
                    conv_id=conv_id,
                )
            except urllib.error.URLError as e:
                print(
                    OUTPUT_INDENT
                    + colorize(f"{icon('error')} connection error: {e}", "error")
                )
                return 1
            message = data["choices"][0]["message"]
        messages.append(message)

        reasoning = message.get("reasoning_content")
        if reasoning and not streamed:
            print(
                OUTPUT_INDENT
                + colorize(
                    f"{icon('thinking')} thinking: {reasoning.strip()}", "thinking"
                )
            )

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            if not streamed:
                print_assistant(message.get("content", ""))
            return 0

        if message.get("content") and not streamed:
            print_assistant(message["content"])
        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments", "")
            arg_preview = raw_args[:200]
            print(
                OUTPUT_INDENT
                + colorize(f"{icon('tool')} {name}({arg_preview})", "tool")
            )
            try:
                result = execute_tool(name, raw_args)
            except ExitSignal as e:
                if e.message:
                    print(
                        OUTPUT_INDENT + colorize(f"{icon('exit')} {e.message}", "tool")
                    )
                return e.code
            print(
                OUTPUT_INDENT
                + colorize(
                    f"{icon('result')} {result[:500]}{'...' if len(result) > 500 else ''}",
                    "result",
                )
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )
        _todo_reminder(messages)


def main():
    global API_URL, API_KEY, MODEL, TEMPERATURE, MAX_SUBAGENT_DEPTH, VISION_ENABLED
    global INTERRUPT_ENABLED, SHELL_PREFERRED
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[reportAttributeAccessIssue]
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description=f"harnless {VERSION}: minimal LLM agent harness"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"harnless {VERSION}",
        help="show the harnless version and exit",
    )
    parser.add_argument(
        "--api-url",
        default=API_URL,
        help=(
            "OpenAI-compatible API URL: either a base URL "
            "(e.g. https://openrouter.ai/api/v1) or a full chat completions "
            f"endpoint (default: {API_URL})"
        ),
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key sent as 'Authorization: Bearer <key>' (omit for local servers that need no auth)",
    )
    parser.add_argument(
        "--model",
        default="local-model",
        help="model name to send in the request (llama-server usually ignores it)",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="system prompt to use (replaces the built-in one)",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="one-shot mode: send this as the only user message, then exit",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="sampling temperature (default: 1.0; lower = more deterministic tool calls)",
    )
    parser.add_argument(
        "--max-subagents",
        type=int,
        default=2,
        help="maximum sub-agent nesting depth for the task tool (default: 2)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=0,
        help="model context window size in tokens, shown as a percentage in /status (0 = not shown)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color output (also auto-disabled for piped output and NO_COLOR)",
    )
    parser.add_argument(
        "--no-emoji",
        action="store_true",
        help="use plain ASCII labels instead of emoji icons",
    )
    parser.add_argument(
        "--no-vision",
        action="store_true",
        help="do not send image file references to the model (replace them with a text note); use with text-only models",
    )
    parser.add_argument(
        "--shell",
        choices=["auto", "pwsh", "powershell", "cmd"],
        default="auto",
        help=(
            "shell for run_shell on Windows: auto (pwsh → powershell → cmd), "
            "or force pwsh / powershell / cmd (default: auto; ignored off-Windows)"
        ),
    )
    parser.add_argument(
        "--mcp-config",
        action="append",
        default=None,
        metavar="FILE",
        help="MCP config file (JSON); repeatable. Accepts {'mcpServers': {...}} or a bare {...}",
    )
    parser.add_argument(
        "--mcp-stdio",
        action="append",
        default=None,
        metavar="NAME:COMMAND ARGS...",
        help="add a stdio MCP server, e.g. --mcp-stdio 'fs:npx -y @modelcontextprotocol/server-filesystem ./data'",
    )
    parser.add_argument(
        "--mcp-http",
        action="append",
        default=None,
        metavar="NAME=URL",
        help="add an http MCP server, e.g. --mcp-http 'api=http://127.0.0.1:8000/mcp'",
    )
    args = parser.parse_args()

    API_URL = normalize_api_url(args.api_url)
    API_KEY = args.api_key
    MODEL = args.model
    TEMPERATURE = args.temperature
    MAX_SUBAGENT_DEPTH = args.max_subagents
    VISION_ENABLED = not args.no_vision
    SHELL_PREFERRED = args.shell

    context_window = args.context_window
    if context_window == 0 and args.prompt is None:
        context_window = probe_context_window()

    set_color_enabled(not args.no_color and color_enabled())
    set_emoji_enabled(not args.no_emoji and _stdout_can_encode_emoji())

    mcp_servers = {}
    if os.path.exists(MCP_HOME_CONFIG):
        _load_mcp_config_into(mcp_servers, MCP_HOME_CONFIG)
    for path in args.mcp_config or []:
        _load_mcp_config_into(mcp_servers, path)
    for spec in args.mcp_stdio or []:
        try:
            name, cfg = _parse_mcp_stdio(spec)
        except MCPError as e:
            print(colorize(f"{icon('error')} {e}", "error"))
            continue
        mcp_servers[name] = cfg
    for spec in args.mcp_http or []:
        try:
            name, cfg = _parse_mcp_http(spec)
        except MCPError as e:
            print(colorize(f"{icon('error')} {e}", "error"))
            continue
        mcp_servers[name] = cfg

    # Servers with "enabled": false are kept pending; the user can enable them
    # from the /tools menu (which connects them on demand).
    for name in [n for n, cfg in mcp_servers.items() if not cfg.get("enabled", True)]:
        cfg = mcp_servers.pop(name)
        cfg.pop("enabled", None)
        PENDING_MCP.append((name, cfg))

    if mcp_servers:
        mcp_clients = [MCPClient(name, cfg) for name, cfg in mcp_servers.items()]
        register_mcp_tools(mcp_clients)

    if mcp_servers or PENDING_MCP:
        def _close_mcp():
            for c in MCP_CLIENTS:
                try:
                    c.close()
                except Exception:
                    pass

        atexit.register(_close_mcp)

    system_prompt_additions = (
        ""
        if args.prompt is None
        else (
            "When a task is done, summarize what you did concisely and call the exit tool with code 0. "
            "If the task cannot be completed, call the exit tool with a non-zero code and explain why."
        )
    )

    system_prompt = (
        args.system_prompt
        if args.system_prompt is not None
        else get_system_prompt(CWD, system_prompt_additions)
    )

    agents_md = load_agents_md()
    if agents_md:
        system_prompt += (
            "\n\nProject instructions (AGENTS.md in the working directory):\n"
            + agents_md
        )
    additions = _context_additions()
    if additions:
        system_prompt += "\n\n" + additions
    _todo_load()
    messages = [{"role": "system", "content": system_prompt}]

    if args.prompt is not None:
        print(colorize(f"harnless {VERSION} one-shot in {CWD} (api: {API_URL})", "dim"))
        messages.append(build_user_message(args.prompt))
        sys.exit(
            run_agent(messages, args.model, temperature=args.temperature,
                      conv_id=MAIN_CONV_ID)
        )

    _history_load()

    INTERRUPT_ENABLED = sys.stdin.isatty()

    print(colorize(f"harnless {VERSION} ready in {CWD} (api: {API_URL})", "dim"))
    banner = (
        "type / for a command menu, /help for all commands, /exit to quit\n"
        "attach files with @[cwd://rel/path], @[file:///abs/path] or @[http(s)://host/file]\n"
        "use up/down arrows to recall previous input, Ctrl+J inserts a newline (multi-line prompt)\n"
        "Enter submits — /auto-send off switches to Ctrl+Enter to send\n"
    )
    if INTERRUPT_ENABLED:
        banner += "press esc twice (within 2 s) to interrupt generation\n"
    print(colorize(banner, "dim"))

    while True:
        try:
            user_input = readline_prompt(colorize(f"{icon('user')} you> ", "user")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            break
        if user_input == "/new":
            messages = [{"role": "system", "content": system_prompt}]
            print(colorize("session cleared — starting over\n", "dim"))
            continue
        if user_input == "/clear-screen":
            os.system("cls" if os.name == "nt" else "clear")
            continue
        if user_input == "/status":
            print(
                colorize(
                    format_status(messages, context_window, conv_id=MAIN_CONV_ID),
                    "dim",
                )
            )
            continue
        if user_input == "/tools" or user_input.startswith("/tools "):
            rest = user_input[len("/tools"):].strip()
            if not rest:
                if sys.stdin.isatty():
                    if tools_menu():
                        print(colorize("tools updated", "dim"))
                    else:
                        print(colorize("tools unchanged", "dim"))
                else:
                    print(colorize(format_tools(), "dim"))
            else:
                for name, state in toggle_tools(rest.split()):
                    if state == "unknown":
                        print(colorize(f"{icon('error')} unknown tool: {name}", "error"))
                    else:
                        print(colorize(f"{name}: {state}", "dim"))
            continue
        if user_input == "/auto-send" or user_input.startswith("/auto-send "):
            msg = set_auto_send(user_input[len("/auto-send"):])
            if msg:
                print(colorize(msg, "dim"))
            else:
                print(colorize("usage: /auto-send on|off", "error"))
            continue
        if user_input == "/help":
            print(colorize(format_help(), "dim"))
            continue
        messages.append(build_user_message(user_input))
        run_agent(
            messages,
            args.model,
            interactive=True,
            temperature=args.temperature,
            conv_id=MAIN_CONV_ID,
        )


if __name__ == "__main__":
    main()
