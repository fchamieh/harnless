#!/usr/bin/env python3
"""harnless: a minimal agent harness for a local llama-server (OpenAI-compatible)."""

import argparse
import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
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


# ---------------------------------------------------------------- mcp

MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_TIMEOUT = 60  # seconds per request

MCP_TOOLS = []      # OpenAI tool specs for MCP tools
MCP_DISPATCH = {}   # tool name -> (MCPClient, tool name)
MCP_CLIENTS = []    # all configured MCP clients (for /status)

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
    return text


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
        self._proc = None
        self._out_q = None
        self._stderr_buf = []
        self._session_id = None
        self._url = None
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
                "clientInfo": {"name": "harnless", "version": "0.1"},
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
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=CWD,
            env=env,
            text=True,
        )
        self._out_q = queue.Queue()
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self):
        try:
            for line in self._proc.stdout:
                self._out_q.put(line)
        except Exception:
            pass
        self._out_q.put(None)  # sentinel on EOF

    def _drain_stderr(self):
        try:
            for line in self._proc.stderr:
                self._stderr_buf.append(line.rstrip("\n"))
                if len(self._stderr_buf) > 500:
                    self._stderr_buf.pop(0)
        except Exception:
            pass

    def _send_stdio(self, msg: dict):
        if self._proc is None or self._proc.poll() is not None:
            raise MCPError(f"stdio server '{self.name}' is not running")
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _recv_stdio(self) -> dict:
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


def register_mcp_tools(clients: list) -> None:
    """Connect to MCP clients and register their tools into MCP_TOOLS/MCP_DISPATCH.

    Tool names are used as-is. A name that collides with a built-in tool or an
    earlier-registered MCP tool is skipped (built-ins win, then first server).
    """
    global MCP_TOOLS, MCP_DISPATCH, MCP_CLIENTS
    MCP_TOOLS = []
    MCP_DISPATCH = {}
    MCP_CLIENTS = list(clients)
    for client in clients:
        try:
            client.connect()
        except Exception as e:
            print(
                colorize(
                    f"{icon('error')} mcp server '{client.name}' failed to connect: {e}",
                    "error",
                )
            )
            continue
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
            continue
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
        elif ch == "\x1b":
            yield "esc"
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
    import select
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
                # A bare ESC has no following bytes; an escape sequence does.
                # Wait briefly to tell the two apart.
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    yield "esc"
                    continue
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


def normalize_api_url(url: str) -> str:
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


def chat(
    messages: list, model: str, interactive: bool = False, temperature: float = 0.2
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
            "tools": _active_tools(interactive),
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
    messages: list, model: str, interactive: bool = False, temperature: float = 0.2
):
    """Stream one chat turn, printing reasoning and content live.

    Returns (message, streamed) where streamed is False if no deltas
    arrived (e.g. server ignored stream mode) — caller should fall back.
    """
    message = {"role": "assistant"}
    started_reasoning = False
    renderer = None
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
            if renderer is None:
                if started_reasoning:
                    print()
                label = f"{icon('assistant')} assistant> "
                print(colorize(label, "assistant"), end="", flush=True)
                renderer = MarkdownRenderer(indent=display_width(label))
            renderer.write(content)
        accumulate_delta(message, delta)
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
    print(colorize(label, "assistant"), end="", flush=True)
    renderer = MarkdownRenderer(indent=display_width(label))
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
                return client.call_tool(tool_name, args)
            except MCPError as e:
                return f"error: {e}"
            except Exception as e:
                return f"error: {type(e).__name__}: {e}"
        return f"error: unknown tool: {name}"
    try:
        return str(fn(args))
    except ExitSignal:
        raise
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


def _active_tools(interactive: bool) -> list:
    """Tool specs to send to the API, excluding tools toggled off via /tools."""
    base = OPENAI_TOOLS_INTERACTIVE if interactive else OPENAI_TOOLS
    return [s for s in base + MCP_TOOLS if s["function"]["name"] not in DISABLED_TOOLS]


def _all_tool_names() -> set:
    return {s["function"]["name"] for s in OPENAI_TOOLS_INTERACTIVE + MCP_TOOLS}


def format_tools() -> str:
    """Build the /tools checklist: '[X] name — description' per tool."""
    lines = []
    for s in OPENAI_TOOLS_INTERACTIVE + MCP_TOOLS:
        name = s["function"]["name"]
        desc = s["function"].get("description", "")
        mark = " " if name in DISABLED_TOOLS else "X"
        lines.append(f"[{mark}] {name} — {desc}")
    return "\n".join(lines)


def toggle_tools(names: list) -> list:
    """Toggle the given tool names on/off. Returns [(name, 'on'|'off'|'unknown')]."""
    known = _all_tool_names()
    results = []
    for n in names:
        if n not in known:
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

    Up/down move the cursor, space toggles the highlighted tool, enter
    applies the changes and quits, esc (or ctrl+c/ctrl+d) quits without
    applying them. Returns True if changes were applied, False if cancelled.
    """
    specs = OPENAI_TOOLS_INTERACTIVE + MCP_TOOLS
    names = [s["function"]["name"] for s in specs]
    descs = [s["function"].get("description", "") for s in specs]
    if not names:
        return False
    disabled = set(DISABLED_TOOLS)
    cursor = 0
    width = shutil.get_terminal_size((80, 24)).columns

    def build_lines():
        lines = [colorize("tools — space: toggle, enter: apply, esc: cancel", "dim")]
        for i, name in enumerate(names):
            mark = " " if name in disabled else "x"
            if i == cursor:
                lines.append(colorize(f"> [{mark}] {name}", "tool"))
            else:
                lines.append(f"  [{mark}] {name}")
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
    draw()
    try:
        while True:
            token = next(keys)
            if token == "up":
                cursor = max(0, cursor - 1)
            elif token == "down":
                cursor = min(len(names) - 1, cursor + 1)
            elif token == "enter":
                DISABLED_TOOLS.clear()
                DISABLED_TOOLS.update(disabled)
                return True
            elif token in ("esc", "ctrl_c", "ctrl_d"):
                return False
            elif token == ("char", " "):
                name = names[cursor]
                if name in disabled:
                    disabled.discard(name)
                else:
                    disabled.add(name)
            draw()
    except StopIteration:
        return False
    finally:
        close = getattr(keys, "close", None)
        if close is not None:
            close()
        if drawn:
            sys.stdout.write(f"\x1b[{drawn}B")
            sys.stdout.flush()


def format_status(messages: list) -> str:
    """Build the /status report: context usage, API URL, tool names, MCP servers."""
    total_chars = 0
    for m in messages:
        total_chars += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            total_chars += len((tc.get("function") or {}).get("arguments") or "")
    approx_tokens = total_chars // 4
    tool_names = ", ".join(s["function"]["name"] for s in _active_tools(True))
    lines = [
        f"context: {approx_tokens} tokens (~{total_chars} chars) in {len(messages)} messages",
        f"api url: {API_URL}",
        f"tools: {tool_names}",
    ]
    if MCP_CLIENTS:
        mcp_lines = []
        for c in MCP_CLIENTS:
            names = ", ".join(c.tool_names) if c.tool_names else "no tools"
            mcp_lines.append(f"  {c.name} ({c.transport}): {names}")
        lines.append("mcp servers:\n" + "\n".join(mcp_lines))
    return "\n".join(lines)


def format_help() -> str:
    """Build the /help report: list of REPL commands."""
    return (
        "/new            clear session history and start over\n"
        "/clear-screen   clear the terminal screen\n"
        "/status         show context usage, api url, and tools\n"
        "/tools          interactive tool menu: up/down move, space toggle, enter apply, esc cancel\n"
        "/tools <name>   toggle a tool on/off\n"
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
                print_assistant(message.get("content", ""))
            return 0

        if message.get("content") and not streamed:
            print_assistant(message["content"])
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

    set_color_enabled(not args.no_color and color_enabled())
    set_emoji_enabled(not args.no_emoji)

    mcp_servers = {}
    for path in args.mcp_config or []:
        try:
            loaded = load_mcp_config(path)
        except Exception as e:
            print(colorize(f"{icon('error')} failed to load mcp config {path}: {e}", "error"))
            continue
        for name, cfg in loaded.items():
            if name in mcp_servers:
                print(
                    colorize(
                        f"{icon('error')} duplicate mcp server name '{name}'; later definition wins",
                        "error",
                    )
                )
            mcp_servers[name] = cfg
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

    if mcp_servers:
        mcp_clients = [MCPClient(name, cfg) for name, cfg in mcp_servers.items()]
        register_mcp_tools(mcp_clients)

        def _close_mcp():
            for c in mcp_clients:
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
    messages = [{"role": "system", "content": system_prompt}]

    if args.prompt is not None:
        print(colorize(f"harnless one-shot in {CWD} (api: {API_URL})", "dim"))
        messages.append({"role": "user", "content": args.prompt})
        sys.exit(run_agent(messages, args.model, temperature=args.temperature))

    _history_load()

    print(colorize(f"harnless ready in {CWD} (api: {API_URL})", "dim"))
    print(
        colorize(
            "type /new to start over, /clear-screen to clear the screen, /status for session info, /tools to toggle tools, /help for commands, /exit to quit\n"
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
        if user_input == "/help":
            print(colorize(format_help(), "dim"))
            continue
        messages.append({"role": "user", "content": user_input})
        run_agent(
            messages, args.model, interactive=True, temperature=args.temperature
        )


if __name__ == "__main__":
    main()
