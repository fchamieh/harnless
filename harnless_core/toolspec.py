"""toolspec: the contract the model is shown.

One entry per built-in tool: the OpenAI function schema (name, description, parameters) and
the order they are advertised in. Descriptions here are model-facing copy — they are what the
model reads when it decides which tool to call and with what arguments.

The other half of the pairing — which function answers each call — is DISPATCH in harnless.py:
schemas are data, implementations are behaviour. Keys must match.
"""

from harnless_core import config

SPECS = {
    "get_cwd": {
        "type": "function",
        "function": {
            "name": "get_cwd",
            "description": "Get the current working directory of the harness.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "run_shell": {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a command in the current working directory."
            + config.SHELL_NOTE,
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to run",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            f"Timeout in seconds (default {config.SHELL_TIMEOUT_DEFAULT}, max "
                            f"{config.SHELL_TIMEOUT_CEILING}); on expiry the command and everything "
                            "it started are killed and whatever it printed so far is returned"
                        ),
                    },
                    "max_output": {
                        "type": "integer",
                        "description": (
                            f"Max characters of stdout+stderr to return (default {config.SHELL_OUTPUT_LIMIT}, "
                            f"max {config.TOOL_RESULT_LIMIT}); the rest is dropped with a truncation note"
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
    "mkdir": {
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
    "read_file": {
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
                            f"Max characters to return (default {config.READ_FILE_LIMIT}, max {config.TOOL_RESULT_LIMIT}); "
                            "prefer offset/lines to read a file in chunks"
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    "write_file": {
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
    "grep": {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regex pattern (case-sensitive by default; case_sensitive=false ignores case). path may be a directory (searched recursively) or a single file. Files git would ignore (.gitignore) are skipped unless respect_gitignore is false.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to search: a directory (searched recursively) or a single file, e.g. ./x/y/z",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search (exact case unless case_sensitive is false; an inline (?i) also works)",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": (
                            "Match pattern (and file_pattern) with exact case (default true); "
                            "set false to ignore case"
                        ),
                    },
                    "context": {
                        "type": "integer",
                        "description": "Lines of context around each match (default 0); match lines are prefixed with '>'",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Optional glob (e.g. *.py) or regex matched against relative file paths to restrict files scanned (obeys case_sensitive)",
                    },
                    "respect_gitignore": {
                        "type": "boolean",
                        "description": (
                            "Skip paths that .gitignore rules hide (default true); "
                            "set false to search them too"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Max matching lines to return (default {config.GREP_MATCH_LIMIT}, max {config.MAX_COUNT_LIMIT}); "
                            f"lines longer than {config.GREP_LINE_LIMIT} chars are clipped"
                        ),
                    },
                },
                "required": ["path", "pattern"],
            },
        },
    },
    "patch_file": {
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
    "glob": {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by glob pattern (e.g. **/*.py) or regex, matched against relative file paths with exact case by default (case_sensitive=false ignores case). path may be a directory (searched recursively) or a single file. Files git would ignore (.gitignore) are skipped unless respect_gitignore is false.",
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
                    "case_sensitive": {
                        "type": "boolean",
                        "description": (
                            "Match pattern with exact case (default true); "
                            "set false to match paths ignoring case"
                        ),
                    },
                    "respect_gitignore": {
                        "type": "boolean",
                        "description": (
                            "Skip paths that .gitignore rules hide (default true); "
                            "set false to list them too"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Max files to return (default {config.LIST_LIMIT}, max {config.MAX_COUNT_LIMIT})"
                        ),
                    },
                },
                "required": ["path", "pattern"],
            },
        },
    },
    "list_dir": {
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
                            f"Max entries to return (default {config.LIST_LIMIT}, max {config.MAX_COUNT_LIMIT})"
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    "delete_file": {
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
    "move_file": {
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
    "copy_file": {
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
    "fetch_url": {
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
                            f"Max characters of response body to return (default {config.FETCH_TEXT_LIMIT}, "
                            f"max {config.TOOL_RESULT_LIMIT})"
                        ),
                    },
                },
                "required": ["url"],
            },
        },
    },
    "todo": {
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
    "memory": {
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
    "task": {
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
    "ask_user": {
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
    "sessions": {
        "type": "function",
        "function": {
            "name": "sessions",
            "description": (
                "Read the session journal. Every turn of every session is journaled to a .jsonl log, so the "
                "turns context compaction replaced are still readable: 'list' the recorded sessions, 'spans' "
                "for a session's compactions (the journal seq ranges they replaced), or 'read' the recorded "
                "turns of a seq range. Use it to recover something a compacted-away search already found "
                "instead of searching again."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "'list' (default), 'spans', or 'read'",
                    },
                    "session": {
                        "type": "string",
                        "description": "Which log: a session id, an '<id>.<chain>' sub-agent log, 'latest' (default), or a path to a .jsonl file",
                    },
                    "from": {
                        "type": "integer",
                        "description": "First journal seq to read (inclusive; default 0)",
                    },
                    "to": {
                        "type": "integer",
                        "description": "Last journal seq to read (inclusive; default the end of that log)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max records to return (default 60; the last N of the range are shown)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters of transcript text (default 30000, max 60000)",
                    },
                },
                "required": [],
            },
        },
    },
    "exit": {
        "type": "function",
        "function": {
            "name": "exit",
            "description": "Finish this agent's run and exit with a given exit code. Use 0 for success, non-zero for failure. Call this when the task is complete. For a sub-agent, `message` is also the hand-off: whatever the parent agent receives as this task's result (write your summary as your reply text or pass it here — the harness keeps the more substantive of the two).",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "integer",
                        "description": "Exit code (default 0)",
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional closing message; shown to the user and used as a sub-agent's summary for the parent",
                    },
                },
                "required": [],
            },
        },
    }
}

# `exit` ends a run: it is a tool a one-shot run may call, never one the REPL offers.
NOT_FOR_REPL = frozenset({"exit"})

OPENAI_TOOLS = [spec for spec in SPECS.values()]
OPENAI_TOOLS_INTERACTIVE = [
    spec for name, spec in SPECS.items() if name not in NOT_FOR_REPL
]
