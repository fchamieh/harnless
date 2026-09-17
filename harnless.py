#!/usr/bin/env python3
"""harnless: a minimal agent harness for a local llama-server (OpenAI-compatible)."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error

API_URL = "http://127.0.0.1:11434/v1/chat/completions"
API_KEY = None
CWD = os.getcwd()
SHELL_NOTE = (
    " Commands run in cmd.exe; prefer cross-platform commands (e.g. dir, type, copy, del) over bash-specific syntax."
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
}
ASCII_ICONS = {
    "user": "you",
    "assistant": "bot",
    "thinking": "think",
    "tool": "tool",
    "result": "out",
    "exit": "exit",
    "error": "err",
}
EMOJI_ENABLED = True


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


def safe_resolve(rel_path: str) -> str:
    """Resolve a relative path and ensure it stays inside CWD. Returns absolute path."""
    rel_path = rel_path.replace("\\", "/")
    if rel_path.startswith("./"):
        rel_path = rel_path[2:]
    if os.path.isabs(rel_path):
        raise ValueError(f"Absolute paths are not allowed: {rel_path}")
    full = os.path.realpath(os.path.join(CWD, rel_path))
    cwd_real = os.path.realpath(CWD)
    if full != cwd_real and not full.startswith(cwd_real + os.sep):
        raise ValueError(f"Path escapes working directory: {rel_path}")
    return full


# ---------------------------------------------------------------- tools


def tool_get_cwd(args: dict) -> str:
    return CWD


def tool_exit(args: dict):
    code = int(args.get("code", 0))
    message = args.get("message", "")
    raise ExitSignal(code, message)


def tool_run_shell(args: dict) -> str:
    command = args.get("command", "")
    if not command.strip():
        return "error: empty command"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=CWD,
            capture_output=True,
            text=True,
            timeout=int(args.get("timeout", 120)),
        )
        out = []
        if proc.stdout:
            out.append(proc.stdout.rstrip())
        if proc.stderr:
            out.append(f"[stderr]\n{proc.stderr.rstrip()}")
        result = "\n".join(out) if out else "(no output)"
        if len(result) > 20_000:
            result = result[:20_000] + f"\n... [truncated, {len(result)} chars total]"
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
    if len(content) > 50_000:
        content = content[:50_000] + "\n... [truncated]"
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


def tool_grep(args: dict) -> str:
    root = safe_resolve(args["path"])
    pattern = re.compile(args["pattern"], re.IGNORECASE)
    context = max(0, int(args.get("context", 0)))
    file_pattern = args.get("file_pattern")
    file_re = None
    if file_pattern:
        file_re = (
            re.compile("^" + file_pattern.replace("?", ".").replace("*", ".*") + "$")
            if not any(c in file_pattern for c in "[](){}|\\^$")
            else re.compile(file_pattern)
        )
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in (".git", "node_modules", "__pycache__")
        ]
        for name in filenames:
            fp = os.path.join(dirpath, name)
            rel = os.path.relpath(fp, CWD).replace("\\", "/")
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
                        matches.append("--")
                    prefix = ">" if j in hits else " "
                    matches.append(f"{prefix} {rel}:{j + 1}: {lines[j]}")
            else:
                for i in hit_idx:
                    matches.append(f"{rel}:{i + 1}: {lines[i]}")
            if len(matches) >= 200:
                return "\n".join(matches) + "\n... [truncated at 200 matches]"
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
    pattern = args["pattern"]
    regex = (
        re.compile("^" + pattern.replace("?", ".").replace("*", ".*") + "$")
        if not any(c in pattern for c in "[](){}|\\^$")
        else re.compile(pattern)
    )
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in (".git", "node_modules", "__pycache__")
        ]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), CWD)
            if regex.search(rel.replace("\\", "/")):
                results.append(rel.replace("\\", "/"))
    if not results:
        return "no files matched"
    return "\n".join(sorted(results)[:500]) + (
        "\n... [truncated at 500 files]" if len(results) > 500 else ""
    )


def tool_list_dir(args: dict) -> str:
    path = safe_resolve(args["path"])
    if not os.path.isdir(path):
        return f"error: not a directory: {args['path']}"
    entries = sorted(os.listdir(path))
    out = []
    for name in entries:
        full = os.path.join(path, name)
        out.append(name + "/" if os.path.isdir(full) else name)
        if len(out) >= 500:
            break
    if not out:
        return "(empty)"
    return "\n".join(out) + (
        "\n... [truncated at 500 entries]" if len(entries) > 500 else ""
    )


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
                "description": "Recursively search file contents inside a relative directory path using a regex pattern.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative directory path to search, e.g. ./x/y/z",
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
                "description": "Find files under a relative directory path by glob pattern (e.g. **/*.py) or regex.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative directory path to search, e.g. ./x/y/z",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern (e.g. **/*.ts) or regex matched against relative file paths",
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


def get_system_prompt(cwd, additional) -> str:
    return (
        "You are a coding assistant running inside a harness. Your working directory is {cwd}. "
        "All file paths you use must be relative to it (e.g. ./src/main.py). "
        "Use the provided tools to inspect and modify files, run commands, and search the codebase. "
        "Explore with list_dir, glob, and grep; read files (the trailer shows total line count) before editing them, "
        "and use patch_file with exact matches for edits. "
        "If patch_file reports multiple matches, re-read the area with line numbers and retry using offset/lines. "
        "Before taking any action that modifies the file system (writing, patching, moving, copying, or deleting files), "
        "plan the change when required and present the plan to the user for approval before acting; "
        "only proceed once the user has agreed. Read-only exploration does not require a plan. "
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
    if len(content) > 20_000:
        content = content[:20_000] + "\n... [truncated]"
    return content


# ---------------------------------------------------------------- line editor

HISTORY = []
HISTORY_MAX = 100
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


def _iter_keys_windows():
    """Yield key tokens from the Windows console via msvcrt."""
    import msvcrt

    ext_map = {
        "H": "up",
        "J": "down",
        "K": "left",
        "L": "right",
        "G": "home",
        "M": "end",
        "P": "delete",
    }
    while True:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            yield ext_map.get(code, "ignore")
        elif ch == "\r":
            yield "enter"
        elif ch == "\x08":
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
    }.get(params, "ignore")


def _iter_keys_posix():
    """Yield key tokens from a POSIX terminal in raw mode via termios/tty."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

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
        tty.setraw(fd)
        while True:
            ch = read_char()
            if ch is None:
                yield "ctrl_d"
                return
            if ch == "\x1b":
                seq = read_char()
                if seq == "[":
                    yield _parse_csi_seq(read_char)
                else:
                    yield "ignore"
            elif ch in ("\r", "\n"):
                yield "enter"
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
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _edit_line(prompt: str, keys) -> str:
    """Run a minimal line editor over a key-token iterator. Returns the line."""
    buf = []
    pos = 0
    hist_idx = len(HISTORY)

    prev_len = 0

    def render():
        nonlocal prev_len
        line = "".join(buf)
        pad = max(0, prev_len - max(len(line), pos))
        sys.stdout.write(
            "\r" + prompt + line + " " * pad
            + "\r" + prompt + line[:pos]
        )
        sys.stdout.flush()
        prev_len = len(line)

    def newline():
        sys.stdout.write("\r\n")
        sys.stdout.flush()

    render()
    while True:
        token = next(keys)
        if token == "enter":
            break
        elif token == "ctrl_c":
            newline()
            raise KeyboardInterrupt
        elif token == "ctrl_d":
            if not buf:
                newline()
                raise EOFError
            break
        elif token == "backspace":
            if pos > 0:
                del buf[pos - 1]
                pos -= 1
        elif token == "delete":
            if pos < len(buf):
                del buf[pos]
        elif token == "ctrl_u":
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
            if hist_idx > 0:
                hist_idx -= 1
                buf = list(HISTORY[hist_idx])
                pos = len(buf)
        elif token == "down":
            if hist_idx < len(HISTORY):
                hist_idx += 1
                buf = list(HISTORY[hist_idx]) if hist_idx < len(HISTORY) else []
                pos = len(buf)
        elif isinstance(token, tuple) and token[0] == "char":
            buf.insert(pos, token[1])
            pos += 1
        render()
    newline()
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


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def chat(
    messages: list, model: str, interactive: bool = False, temperature: float = 0.2
) -> dict:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "tools": OPENAI_TOOLS_INTERACTIVE if interactive else OPENAI_TOOLS,
            "tool_choice": "auto",
            "temperature": temperature,
        }
    ).encode("utf-8")
    req = urllib.request.Request(API_URL, data=payload, headers=_headers())
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_request(
    messages: list,
    model: str,
    stream: bool,
    interactive: bool = False,
    temperature: float = 0.2,
) -> urllib.request.Request:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "tools": OPENAI_TOOLS_INTERACTIVE if interactive else OPENAI_TOOLS,
            "tool_choice": "auto",
            "temperature": temperature,
            "stream": stream,
        }
    ).encode("utf-8")
    return urllib.request.Request(API_URL, data=payload, headers=_headers())


def parse_sse_line(line: str):
    """Parse one SSE line from a streaming chat response.

    Returns the delta dict for data lines, the string "[DONE]" for the
    terminator, or None for comments/blank/malformed lines.
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
    choice = chunk["choices"][0]
    return choice.get("delta") or {}


def stream_chat(
    messages: list, model: str, interactive: bool = False, temperature: float = 0.2
):
    """Yield deltas from a streaming chat response until [DONE]."""
    req = _build_request(
        messages, model, stream=True, interactive=interactive, temperature=temperature
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            parsed = parse_sse_line(raw.decode("utf-8"))
            if parsed is None:
                continue
            if parsed == "[DONE]":
                break
            yield parsed


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


def stream_once(
    messages: list, model: str, interactive: bool = False, temperature: float = 0.2
):
    """Stream one chat turn, printing reasoning and content live.

    Returns (message, streamed) where streamed is False if no deltas
    arrived (e.g. server ignored stream mode) — caller should fall back.
    """
    message = {"role": "assistant"}
    started_reasoning = False
    started_content = False
    streamed = False
    for delta in stream_chat(
        messages, model, interactive=interactive, temperature=temperature
    ):
        streamed = True
        reasoning = delta.get("reasoning_content")
        if reasoning:
            if not started_reasoning:
                print(
                    colorize(f"{icon('thinking')} thinking: ", "thinking"),
                    end="",
                    flush=True,
                )
                started_reasoning = True
            print(reasoning, end="", flush=True)
        content = delta.get("content")
        if content:
            if not started_content:
                if started_reasoning:
                    print()
                print(
                    colorize(f"{icon('assistant')} assistant> ", "assistant"),
                    end="",
                    flush=True,
                )
                started_content = True
            print(content, end="", flush=True)
        accumulate_delta(message, delta)
    if started_reasoning:
        print()
    if started_content:
        print()
    return message, streamed


def execute_tool(name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        return f"error: invalid JSON arguments: {raw_args}"
    fn = DISPATCH.get(name)
    if fn is None:
        return f"error: unknown tool: {name}"
    try:
        return str(fn(args))
    except ExitSignal:
        raise
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


def format_status(messages: list) -> str:
    """Build the /status report: context usage, API URL, tool names."""
    total_chars = 0
    for m in messages:
        total_chars += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            total_chars += len((tc.get("function") or {}).get("arguments") or "")
    approx_tokens = total_chars // 4
    tool_names = ", ".join(s["function"]["name"] for s in OPENAI_TOOLS_INTERACTIVE)
    return (
        f"context: {approx_tokens} tokens (~{total_chars} chars) in {len(messages)} messages\n"
        f"api url: {API_URL}\n"
        f"tools: {tool_names}"
    )


def format_help() -> str:
    """Build the /help report: list of REPL commands."""
    return (
        "/new            clear session history and start over\n"
        "/clear-screen   clear the terminal screen\n"
        "/status         show context usage, api url, and tools\n"
        "/help           show this help\n"
        "/exit           quit (alias: /quit)"
    )


# ---------------------------------------------------------------- loop


def run_agent(
    messages: list, model: str, interactive: bool = False, temperature: float = 0.2
) -> int:
    while True:
        message = None
        streamed = False
        try:
            message, streamed = stream_once(
                messages, model, interactive=interactive, temperature=temperature
            )
            if not streamed:
                message = None
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            print(
                colorize(
                    f"{icon('error')} streaming failed ({e}); retrying non-streaming",
                    "error",
                )
            )
        if message is None:
            try:
                data = chat(
                    messages, model, interactive=interactive, temperature=temperature
                )
            except urllib.error.URLError as e:
                print(colorize(f"{icon('error')} connection error: {e}", "error"))
                return 1
            message = data["choices"][0]["message"]
        messages.append(message)

        reasoning = message.get("reasoning_content")
        if reasoning and not streamed:
            print(
                colorize(
                    f"{icon('thinking')} thinking: {reasoning.strip()}", "thinking"
                )
            )

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            if not streamed:
                label = colorize(f"{icon('assistant')} assistant> ", "assistant")
                print(f"\n{label}{message.get('content', '')}\n")
            return 0

        if message.get("content") and not streamed:
            print(
                colorize(
                    f"{icon('assistant')} {message['content'].strip()}", "assistant"
                )
            )
        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments", "")
            arg_preview = raw_args[:200]
            print(colorize(f"{icon('tool')} {name}({arg_preview})", "tool"))
            try:
                result = execute_tool(name, raw_args)
            except ExitSignal as e:
                if e.message:
                    print(colorize(f"{icon('exit')} {e.message}", "tool"))
                if interactive:
                    return e.code
                sys.exit(e.code)
            print(
                colorize(
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


def main():
    global API_URL, API_KEY
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="harnless: minimal LLM agent harness")
    parser.add_argument(
        "--api-url",
        default=API_URL,
        help=f"OpenAI-compatible chat completions endpoint (default: {API_URL})",
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
        default=0.2,
        help="sampling temperature (default: 0.2; lower = more deterministic tool calls)",
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
    args = parser.parse_args()

    API_URL = args.api_url
    API_KEY = args.api_key

    set_color_enabled(not args.no_color and color_enabled())
    set_emoji_enabled(not args.no_emoji)

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
    messages = [{"role": "system", "content": system_prompt}]

    if args.prompt is not None:
        print(colorize(f"harnless one-shot in {CWD} (api: {API_URL})", "dim"))
        messages.append({"role": "user", "content": args.prompt})
        sys.exit(run_agent(messages, args.model, temperature=args.temperature))

    _history_load()

    print(colorize(f"harnless ready in {CWD} (api: {API_URL})", "dim"))
    print(
        colorize(
            "type /new to start over, /clear-screen to clear the screen, /status for session info, /help for commands, /exit to quit\n"
            "use up/down arrows to recall previous input\n",
            "dim",
        )
    )

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
            print(colorize(format_status(messages), "dim"))
            continue
        if user_input == "/help":
            print(colorize(format_help(), "dim"))
            continue
        messages.append({"role": "user", "content": user_input})
        run_agent(
            messages, args.model, interactive=True, temperature=args.temperature
        )


if __name__ == "__main__":
    main()
