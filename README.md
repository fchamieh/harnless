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
- **Sub-agents** — the `task` tool delegates self-contained work to a nested agent (configurable depth)
- **State tools** — `todo` (task list) and `memory` (persistent notes) survive across turns
- **Streaming** — token-by-token output with Markdown rendering; press `ESC` twice to interrupt a running generation
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
| `/new` | Clear the session history |
| `/clear-screen` | Clear the terminal |
| `/tools` | Toggle tools on/off (interactive menu; MCP tools are grouped under their server — space on the server row toggles all of its tools) |
| `/tools <name>` | Toggle a specific tool on/off (`mcp:<server>` toggles all of a server's tools) |
| `/status` | Show context usage, model, enabled tools, and MCP servers |
| `/exit` | Quit (alias: `/quit`) |

Other REPL behaviors:

- **Multi-line input** — `Ctrl+J` inserts a newline; `Enter` submits.
- **Interrupt** — press `ESC` twice within 2 s to cancel a running generation (works mid-stream and while the context is being uploaded).
- **History** — input history is saved to `~/.harnless_history` (override with the `HARNLESS_HISTORY` env var).

### One-shot mode

`python harnless.py --prompt "task"` runs the agent to completion and exits with its exit code.

### CLI options

| Flag | Description |
|---|---|
| `--api-url URL` | OpenAI-compatible endpoint (a base URL like `https://host/v1` or a full `.../chat/completions` URL) |
| `--api-key KEY` | Sent as `Authorization: Bearer <key>` (omit for unauthenticated local servers) |
| `--model NAME` | Model name sent in the request |
| `--system-prompt TEXT` | Replace the built-in system prompt |
| `--temperature N` | Sampling temperature (default `0.2`) |
| `--max-subagents N` | Max sub-agent nesting depth for the `task` tool (default `3`) |
| `--shell MODE` | Windows shell for `run_shell`: `auto` / `pwsh` / `powershell` / `cmd` (default `auto`, ignored off-Windows) |
| `--context-window N` | Context window size in tokens (shown in `/status`); probed from the API's `/models` endpoint if omitted |
| `--version` | Print the version and exit |

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
`memory`, `task`, `ask_user`, `exit`.

All file paths are relative to the working directory and sandboxed to it. An `AGENTS.md`
in the working directory (case-insensitive) is auto-loaded and appended to the system prompt.

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
