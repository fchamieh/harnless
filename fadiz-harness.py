#!/usr/bin/env python3
"""fadiz-harness: a minimal agent harness for a local llama-server (OpenAI-compatible)."""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error

API_URL = "http://127.0.0.1:11434/v1/chat/completions"
CWD = os.getcwd()


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


def tool_run_bash(args: dict) -> str:
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
        return f"exit code: {proc.returncode}\n{result}"
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {args.get('timeout', 120)}s"


def tool_mkdir(args: dict) -> str:
    path = safe_resolve(args["path"])
    os.makedirs(path, exist_ok=True)
    return f"created directory: {path}"


def tool_read_file(args: dict) -> str:
    path = safe_resolve(args["path"])
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    if len(content) > 50_000:
        content = content[:50_000] + "\n... [truncated]"
    return content


def tool_write_file(args: dict) -> str:
    path = safe_resolve(args["path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(args["content"])
    return f"wrote {len(args['content'])} chars to {path}"


def tool_grep(args: dict) -> str:
    root = safe_resolve(args["path"])
    pattern = re.compile(args["pattern"], re.IGNORECASE)
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            rel = os.path.relpath(fp, CWD).replace("\\", "/")
                            matches.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(matches) >= 200:
                                return "\n".join(matches) + "\n... [truncated at 200 matches]"
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(matches) if matches else "no matches"


def tool_patch_file(args: dict) -> str:
    path = safe_resolve(args["path"])
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    old = args["old_string"]
    new = args["new_string"]
    count = content.count(old)
    if count == 0:
        return "error: old_string not found in file"
    if count > 1 and not args.get("replace_all"):
        return f"error: old_string found {count} times; provide more context or set replace_all"
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
    regex = re.compile("^" + pattern.replace("?", ".").replace("*", ".*") + "$") \
        if not any(c in pattern for c in "[](){}|\\^$") else re.compile(pattern)
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), CWD)
            if regex.search(rel.replace("\\", "/")):
                results.append(rel.replace("\\", "/"))
    if not results:
        return "no files matched"
    return "\n".join(sorted(results)[:500]) + ("\n... [truncated at 500 files]" if len(results) > 500 else "")


TOOLS = {
    "get_cwd": (
        {"type": "function", "function": {
            "name": "get_cwd",
            "description": "Get the current working directory of the harness.",
            "parameters": {"type": "object", "properties": {}},
        }},
        tool_get_cwd,
    ),
    "run_bash": (
        {"type": "function", "function": {
            "name": "run_bash",
            "description": "Run a bash command or script in the current working directory.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string", "description": "The command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"},
            }, "required": ["command"]},
        }},
        tool_run_bash,
    ),
    "mkdir": (
        {"type": "function", "function": {
            "name": "mkdir",
            "description": "Create a directory (and parents) given its relative path to CWD, e.g. ./x/y/z.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Relative path to create, e.g. ./x/y/z"},
            }, "required": ["path"]},
        }},
        tool_mkdir,
    ),
    "read_file": (
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a file given its relative path to CWD, e.g. ./x/y/z/file.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Relative path to the file"},
            }, "required": ["path"]},
        }},
        tool_read_file,
    ),
    "write_file": (
        {"type": "function", "function": {
            "name": "write_file",
            "description": "Write (create/overwrite) a file given its relative path to CWD and its full content.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Relative path to the file"},
                "content": {"type": "string", "description": "Full file content to write"},
            }, "required": ["path", "content"]},
        }},
        tool_write_file,
    ),
    "grep": (
        {"type": "function", "function": {
            "name": "grep",
            "description": "Recursively search file contents inside a relative directory path using a regex pattern.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Relative directory path to search, e.g. ./x/y/z"},
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
            }, "required": ["path", "pattern"]},
        }},
        tool_grep,
    ),
    "patch_file": (
        {"type": "function", "function": {
            "name": "patch_file",
            "description": "Patch a file by replacing an exact old_string with new_string (changes certain lines). Provide enough context in old_string so it matches exactly once.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Relative path to the file"},
                "old_string": {"type": "string", "description": "Exact text to replace (must match, including whitespace)"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)"},
            }, "required": ["path", "old_string", "new_string"]},
        }},
        tool_patch_file,
    ),
    "glob": (
        {"type": "function", "function": {
            "name": "glob",
            "description": "Find files under a relative directory path by glob pattern (e.g. **/*.py) or regex.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Relative directory path to search, e.g. ./x/y/z"},
                "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.ts) or regex matched against relative file paths"},
            }, "required": ["path", "pattern"]},
        }},
        tool_glob,
    ),
    "exit": (
        {"type": "function", "function": {
            "name": "exit",
            "description": "Finish the harness and exit with a given exit code. Use 0 for success, non-zero for failure. Call this when the task is complete.",
            "parameters": {"type": "object", "properties": {
                "code": {"type": "integer", "description": "Exit code (default 0)"},
                "message": {"type": "string", "description": "Optional final message"},
            }, "required": []},
        }},
        tool_exit,
    ),
}

OPENAI_TOOLS = [spec for spec, _ in TOOLS.values()]
DISPATCH = {name: fn for name, (_, fn) in TOOLS.items()}

SYSTEM_PROMPT = (
    "You are a coding assistant running inside a harness. Your working directory is {cwd}. "
    "All file paths you use must be relative to it (e.g. ./src/main.py). "
    "Use the provided tools to inspect and modify files, run commands, and search the codebase. "
    "Prefer reading files before editing them, and use patch_file with exact matches for edits. "
    "When a task is done, summarize what you did concisely and call the exit tool with code 0. "
    "If the task cannot be completed, call the exit tool with a non-zero code and explain why."
).format(cwd=CWD)


# ---------------------------------------------------------------- client

def chat(messages: list, model: str) -> dict:
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "tools": OPENAI_TOOLS,
        "tool_choice": "auto",
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


# ---------------------------------------------------------------- loop

def run_agent(messages: list, model: str) -> int:
    while True:
        try:
            data = chat(messages, model)
        except urllib.error.URLError as e:
            print(f"[connection error] {e}")
            return 1
        choice = data["choices"][0]["message"]
        messages.append(choice)

        tool_calls = choice.get("tool_calls") or []
        if not tool_calls:
            print(f"\nassistant> {choice.get('content', '')}\n")
            return 0

        if choice.get("content"):
            print(choice["content"].strip())
        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments", "")
            arg_preview = raw_args[:200]
            print(f"[tool] {name}({arg_preview})")
            try:
                result = execute_tool(name, raw_args)
            except ExitSignal as e:
                if e.message:
                    print(f"[exit] {e.message}")
                sys.exit(e.code)
            print(f"[result] {result[:500]}{'...' if len(result) > 500 else ''}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })


def main():
    parser = argparse.ArgumentParser(description="fadiz-harness: minimal LLM agent harness")
    parser.add_argument("--model", default="local-model",
                        help="model name to send in the request (llama-server usually ignores it)")
    parser.add_argument("--system-prompt", default=None,
                        help="system prompt to use (replaces the built-in one)")
    parser.add_argument("--prompt", default=None,
                        help="one-shot mode: send this as the only user message, then exit")
    args = parser.parse_args()

    system_prompt = args.system_prompt if args.system_prompt is not None else SYSTEM_PROMPT
    messages = [{"role": "system", "content": system_prompt}]

    if args.prompt is not None:
        print(f"fadiz-harness one-shot in {CWD} (api: {API_URL})")
        messages.append({"role": "user", "content": args.prompt})
        sys.exit(run_agent(messages, args.model))

    print(f"fadiz-harness ready in {CWD} (api: {API_URL})")
    print("type /exit to quit\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            break
        messages.append({"role": "user", "content": user_input})
        run_agent(messages, args.model)


if __name__ == "__main__":
    main()
