# harnless

A minimal, single-file agent harness for local LLM servers. `harnless` talks to any
OpenAI-compatible chat API (built for a local [llama-server](https://github.com/ggml-org/llama.cpp))
and gives the model a set of file and shell tools so it can read, write, search, and run
commands in your working directory.

Pure Python standard library — **no dependencies, no venv, no build step**.

## Features

- **Single file** — the entire harness is `harnless.py` (stdlib only)
- **OpenAI-compatible** — works with llama-server, llama.cpp, OpenRouter, or any `/v1/chat/completions` endpoint
- **File & shell tools** — read/write/patch/search files and run shell commands (PowerShell on Windows, bash on POSIX)
- **Sub-agents** — the `task` tool delegates self-contained work to a nested agent (configurable depth) and hands the result back as the sub-agent's closing summary
- **State tools** — `todo` (task list) and `memory` (persistent notes) survive across turns
- **Session log** — every message of a conversation is journaled as JSON Lines under `.harnless/sessions/` (each sub-agent run in its own file), and `--resume` / `/resume` continues a recorded session
- **Context compaction** — when the context fills up, the oldest turns are replaced by one handoff note the model wrote; the turns it replaced stay readable in the session log (the `sessions` tool reads them back by `seq`)
- **Streaming** — token-by-token output with Markdown rendering; press `ESC` twice to interrupt a running generation (it stops sub-agents and their parents too)
- **MCP support** — plug in external tools via [Model Context Protocol](https://modelcontextprotocol.io) servers (stdio or HTTP)
- **Standalone binaries** — build a self-contained executable for Windows, Linux, or macOS with Nuitka

## Requirements

- Python 3.10+ (stdlib only)
- A running LLM server exposing an OpenAI-compatible API. The default endpoint is
  `http://127.0.0.1:11434/v1/chat/completions` (llama-server's default).

## Quick start

```bash
# point it at your server and chat
python harnless.py

# or run a single task and exit
python harnless.py --prompt "summarize the files in this directory"
```

## Usage

### Interactive mode

`python harnless.py` starts a chat REPL. Typing `/` on an empty line opens a command menu
(up/down to move, enter to select, esc to cancel):

| Command | Effect |
|---|---|
| `/new` | Clear the session history and start over (in a new session log) |
| `/clear-screen` | Clear the terminal |
| `/tools` | Toggle tools on/off (interactive menu; MCP tools are grouped under their server, collapsed by default — `+`/`=` expands, `-` collapses — and space on the server row toggles all of its tools) |
| `/tools <name>` | Toggle a specific tool on/off (`mcp:<server>` toggles all of a server's tools) |
| `/status` | Show context usage, model, enabled tools, and MCP servers |
| `/sessions` | List the recorded session logs (newest first) |
| `/resume <id>` | Continue a recorded session (`/resume` with no argument lists them) |
| `/compact` | Replace this session's older turns with a handoff note now |
| `/auto-compact on\|off\|threshold N` | Switch automatic compaction, or set the percentage of the window it starts at |
| `/exit` | Quit (alias: `/quit`) |

Other REPL behaviors:

- **Multi-line input** — `Ctrl+J` inserts a newline; `Enter` submits.
- **Interrupt** — press `ESC` twice within 2 s to cancel a running generation (works mid-stream and while the context is being uploaded).
- **History** — input history is saved to `~/.harnless_history` (override with the `HARNLESS_HISTORY` env var).

### One-shot mode

`python harnless.py --prompt "task"` runs the agent to completion and exits with its exit code:
`0` finished, `1` a connection/API failure (or a `--resume` that matched nothing), `3` out of
context and compaction could not help — plus whatever code the agent's own `exit` call asked for
(`4` is what a step-limited sub-agent reports to whoever delegated it).

### CLI options

| Flag | Description |
|---|---|
| `--api-url URL` | OpenAI-compatible endpoint (a base URL like `https://host/v1` or a full `.../chat/completions` URL) |
| `--api-key KEY` | Sent as `Authorization: Bearer <key>` (omit for unauthenticated local servers) |
| `--model NAME` | Model name sent in the request |
| `--system-prompt TEXT` | Replace the built-in system prompt |
| `--temperature N` | Sampling temperature (default `1.0`) |
| `--max-subagents N` | Max sub-agent nesting depth for the `task` tool (default `2`; `0` disables sub-agents, max `10`) |
| `--max-subagent-steps N` | Model turns a single sub-agent may take before its run is stopped (default `40`; `0` = unlimited; top-level agents are never capped) |
| `--shell MODE` | Windows shell for `run_shell`: `auto` / `pwsh` / `powershell` / `cmd` (default `auto`, ignored off-Windows) |
| `--context-window N` | Context window size in tokens (shown in `/status`); probed from the API's `/models` endpoint if omitted |
| `--no-auto-compact` | Do not compact the conversation automatically (a rejected-for-size prompt then ends the run with exit code 3; `/compact` still works) |
| `--compact-threshold PCT` | Auto-compact once the conversation reaches this percentage of the context window (default `85`, max `99`) |
| `--sessions-dir DIR` | Where session logs are written (default `.harnless/sessions`, or `HARNLESS_SESSIONS_DIR`) |
| `--no-session-log` | Do not journal the session |
| `--resume SPEC` | Continue a recorded session: a session id, a `"<id>.<chain>"` sub-agent log, `latest`, or a path to a `.jsonl` |
| `--resume-last [N]` | Continue the **newest** recorded session, keeping only its last N messages (no value or `0` = all). With `--resume` it only trims that session. Nothing recorded yet → warns and starts a fresh session |
| `--version` | Print the version and exit |

### Session log

Every conversation is journaled as it happens — the exact message objects the harness
replays to the model, one JSON object per line:

```
.harnless/sessions/20261009-110041-a4c68b.jsonl      the top-level conversation
.harnless/sessions/20261009-110041-a4c68b.1.jsonl    its first sub-agent run
.harnless/sessions/20261009-110041-a4c68b.1.2.jsonl  a sub-agent of that sub-agent
```

The dotted chain id in a sub-agent file says who delegated it. Each line is one record:

| `type` | What it holds |
|---|---|
| `session` | First line: version, session id, mode, cwd, API URL, model — and for a sub-agent log, its `depth`, `subagent_id`, `parent_log` and the `task` text it was given |
| `message` | One message: `role`, `content`, `tool_calls`, `tool_call_id`, `reasoning_content`, plus the harness envelope (`seq`, `ts`, `chars`) |
| `usage` | One per model call: message count and the server-reported token usage (how full the context was at that point) |
| `compact` | Written when context compaction replaces older turns: the `replaced` seq range, `dropped`/`kept` counts, `summary_seq` (the handoff note's own record), whether the note came from the model or is a fallback placeholder, `pruned_chars`, and the token numbers the cut was made with (`window`, `used_tokens`, `turn_tokens`, `token_scale`, `keep_tokens`) |
| `end` | How a sub-agent's run ended: its exit code, `interrupted`, `step_limited` |
| `resume` / `cleared` | Written when a log is resumed or when `/new` starts a new session |

`seq` numbers a conversation's records from 0 and keeps counting across a resume and across a
compaction — a stable id for each message, which is what a `compact` record names the replaced
turns with. Inline images (`@[cwd://img.png]`) are recorded as a note naming them rather than as
base64 megabytes (`chars` still reports their real size). A log that cannot be written is dropped
with one warning and the session continues without it. Nothing is written when `--no-session-log`
is given.

**Resuming** (`--resume <id|latest|path>`, or `/resume <id>` in the REPL) reads such a
file back and continues that conversation: the recorded turns are replayed, the *system
prompt is not* (the new run supplies its own, with the current AGENTS.md and memory
notes), and any tool call the log left unanswered gets a note as its answer, so the
rebuilt conversation is one a server will accept. The resumed run keeps appending to the
same file — one continuous transcript, restored messages not recorded a second time.
`--resume-last [N]` cuts a long session down to its last N records before rebuilding it (a cut
landing inside a tool-call batch drops the orphaned answer) — and on its own it is how you say
"continue where I left off" without looking up an id: `python harnless.py --resume-last` picks
up the newest log, `--resume-last 30` picks it up keeping only its last 30 messages. With
nothing recorded yet it says so and opens a fresh session, while a `--resume <id>` that matches
nothing stays an error (exit 1). `/sessions` lists what is recorded.

A resumed run in the REPL also shows you **where you left off**: after the note it replays the
tail of the journal the way the session printed it — the `you>` line, the `thinking:` line, the
reply through the same Markdown renderer, each tool call followed by the answer it got, in the
order they happened. `RESUME_TAIL_ITEMS` is how many speaking turns the echo reaches back over
and `RESUME_TAIL_RECORDS` is the hard bound on records replayed (a tool-heavy turn cannot flood
the screen), with `RESUME_TAIL_CHARS` capping a replayed thinking block — a reply is shown
whole, thinking is not. The turns the harness injects for the model (todo reminders, a
compaction handoff note) are not replayed, because they were never something you saw. A
one-shot run has nobody standing at the terminal, so `--prompt` prints the note only.

### Context compaction

When the context fills up, the oldest turns are replaced by one note the model wrote from them,
and the recent turns stay exactly as they were:

```
system prompt                kept
[harnless handoff note]      the model's summary of everything below the cut
… recent turns …             kept verbatim
```

Two things trigger it:

- **proactively**, before the next request, once the conversation crosses `--compact-threshold`
  percent of the context window — measured as what the server last reported for that conversation,
  or an estimate of its *turns* when it has not reported anything; the tool schemas and system
  prompt ride along with every request either way, so they are not what this trigger watches
  (compaction cannot shrink them);
- **reactively**, when the server rejects a prompt as too long: the rejection is printed with the
  server's own wording, the conversation is compacted, and the request is retried (`/compact` does
  the same on demand, `--no-auto-compact` or `/auto-compact off` turns the automatic ones off).

Before the summarising request is made, the turns about to go are shrunk: an oversized tool result
keeps its first and last characters with a note about what was elided, and thinking that already
produced its turn stops being replayed. If the summarising request itself fails, the turns still
go — that is what made the room — and a placeholder stands in for the note, said out loud and
recorded.

Nothing is lost for good. Every turn was journaled when it entered the conversation, and the
`compact` record names the seqs it replaced, so the **`sessions`** tool can read them back: `list`
the recorded logs, `spans` for a session's compactions and the ranges they replaced, `read` the
transcript of a seq range (a `compact` record shows up in a read as the gap it made). A handoff
note in the conversation names the session and the range it stands for, so the agent can look a
fact up instead of re-running the tool that already found it.

A sub-agent compacts its own run in its own log. If a prompt is still too big after the one
compaction attempt, the run stops with exit code `3` and says where the transcript is.

### MCP servers

External tools can be added via [Model Context Protocol](https://modelcontextprotocol.io) servers:

- `~/.harnless/mcp.json` is auto-loaded at startup if present
- `--mcp-config FILE` (repeatable; JSON `{"mcpServers": {...}}` or a bare `{...}`)
- `--mcp-stdio 'NAME:COMMAND ARGS...'`
- `--mcp-http 'NAME=URL'`

A server entry may set `"enabled": false` to skip connecting it at startup (it can be enabled later from the `/tools` menu). Once connected, the server's tools stay grouped under its entry in the `/tools` menu: space on the server row enables/disables all of its tools at once, and each tool can still be toggled individually.

## Tools

Built-in tools: `get_cwd`, `run_shell`, `mkdir`, `read_file`, `write_file`, `patch_file`,
`grep`, `glob`, `list_dir`, `delete_file`, `move_file`, `copy_file`, `fetch_url`, `todo`,
`memory`, `task`, `ask_user`, `sessions`, `exit`.

All file paths are relative to the working directory and sandboxed to it. An `AGENTS.md`
in the working directory (case-insensitive) is auto-loaded and appended to the system prompt.

`grep` and `glob` match with **exact case** by default — pass `case_sensitive: false` to ignore
case (`grep` applies it to both the search pattern and `file_pattern`).

They also skip what a **`.gitignore`** would hide: the same rules git applies in that
directory, including nested `.gitignore` files (a nearer one outranks a shallower), `!`
re-includes, `**`, and case-insensitive matching where the filesystem is. No `git` needed —
the engine is in `harnless.py`, so it behaves the same outside a repository and on every
platform. `list_dir` still reports what is really on disk.

```
grep  {"path": "./", "pattern": "TODO"}                        # .gitignored files skipped
grep  {"path": "./", "pattern": "TODO", "respect_gitignore": false}   # search them anyway
```

A file you name explicitly as `path` is searched whatever git thinks of it, and when the
skipping is what made an answer empty, the result says so:
`no matches` + `... [2 gitignored paths skipped per .gitignore; pass respect_gitignore:false to include them]`.

`run_shell` waits for the command **and everything it started**. At `timeout` (default 120s,
max 3600s) the whole process tree is killed and whatever it printed so far is returned as
`[partial output]` — no orphaned test runners, no drain that never finishes.

## Building standalone executables

`build.py` compiles `harnless.py` into a self-contained binary with [Nuitka](https://nuitka.net).

### Native build (current OS)

```bash
pip install "nuitka[onefile]"   # zstandard enables onefile compression
python build.py                 # single-file binary -> dist/harnless (dist/harnless.exe on Windows)
python build.py --onedir        # folder build (faster startup, fewer AV false positives)
python build.py --lto           # link-time optimization (faster binary, slower build)
```

### Cross-build a Linux binary (from any OS)

A pre-baked builder image makes cross-building trivial:

```bash
podman build -t harnless-builder .   # or: docker build -t harnless-builder .
python build.py --target linux        # -> dist/harnless (Linux ELF)
```

The image is based on `manylinux2014`, so the resulting binary runs on any modern Linux
(glibc ≥ 2.14: CentOS/RHEL 7+, Ubuntu 14.04+, Debian 8+, Fedora, Arch, ...).

## Development

```bash
python tests.py    # stdlib unittest, no pytest; run from the repo root
```

## License

[MIT](LICENSE) — Copyright (c) 2026 Fadi Chamieh
