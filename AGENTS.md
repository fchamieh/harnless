# AGENTS.md

Single-file Python project: `harnless.py` — a minimal CLI agent harness that talks to a local llama-server (OpenAI-compatible API) and exposes file/shell tools (bash on POSIX, PowerShell on Windows). Stdlib only; no dependencies, no venv, build is in the `build.py` script.

## Requirements

- A running LLM server for the agent to talk to. Default endpoint is `http://127.0.0.1:11434/v1/chat/completions` (`API_URL` in `harnless.py`), overridable via `--api-url`. Tests (`tests.py`) don't need a server.

## Notes

- `tests.py` imports the module directly (`import harnless`).