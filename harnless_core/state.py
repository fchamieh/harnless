"""Mutable runtime state: one home for everything that changes after import.

Always reached through this module object (`state.CONTEXT_WINDOW`), never copied with
`from ... import` — a copy would silently stop seeing the changes main()'s flags, the
slash commands and the test suite make."""

import os
import sys

from harnless_core import config


API_URL = "http://127.0.0.1:11434/v1/chat/completions"
API_KEY = None
MODEL = "local-model"
TEMPERATURE = 1.0
MAX_SUBAGENT_DEPTH = 2
# Runaway guard: how many model turns (API calls) a single sub-agent may take
# before its run is stopped; 0 disables the guard. Only sub-agents are capped —
# a top-level agent is steered by the user, who can always double-ESC it.
SUBAGENT_STEP_LIMIT = 500
# Current sub-agent nesting level: depth 0 = top-level agent. Scoped to each
# run_agent call (set on entry, restored on exit — see run_agent).
_AGENT_DEPTH = 0
OUTPUT_INDENT = ""
CWD = os.getcwd()
VISION_ENABLED = True
# Double-ESC streaming interrupt: enabled for interactive REPL sessions
# (set in main()); two ESC presses within this window cancel the stream.
INTERRUPT_ENABLED = False
# Auto-send mode (REPL only): True (default) — Enter submits the line;
# False — Enter inserts a newline and Ctrl+Enter submits (Ctrl+D also
# submits a non-empty line, which is the reliable send key on POSIX where
# Ctrl+Enter is not distinguishable from Enter). Toggled with /auto-send.
AUTO_SEND = True
CONTEXT_PROGRESS_DELAY = 1.0
# Context compaction: the answer to "the context is full". Older turns are
# replaced by one model-written handoff note and the recent turns stay verbatim.
# Two triggers: proactive (usage crosses COMPACT_THRESHOLD_PCT of CONTEXT_WINDOW)
# and reactive (the server rejects the prompt as too long). Nothing is lost for
# good — every message is journaled to the session log (see below).
CONTEXT_WINDOW = 0  # window in tokens (probed or --context-window); 0 = unknown
AUTO_COMPACT = True  # compact automatically; --no-auto-compact / /auto-compact off
COMPACT_THRESHOLD_PCT = 85  # auto-compact once usage reaches this % of the window
COMPACT_KEEP_TOKENS_PCT = 25  # verbatim tail budget, as a % of the window
COMPACT_MIN_MESSAGES = 8  # below this many messages, refuse to compact
COMPACT_PRUNE_TOOL_RESULTS = True  # elide tool results that fall out of the tail
COMPACT_DROP_REASONING = True  # stop replaying reasoning_content of old turns
COMPACT_MAX_ATTEMPTS = 1  # compact-and-retry budget after a reactive overflow
COMPACT_SUMMARY_TRIMS = 3  # tries at the summarising request, each with a smaller head
# Session log: every conversation is journaled as it happens — the exact message
# objects the harness replays to the API, one JSON object per line, plus a header
# record per log and a usage record per LLM call. <id>.jsonl holds the top-level
# conversation; each sub-agent run gets its own file named after where it sits in
# the delegation chain (<id>.1.jsonl, <id>.1.2.jsonl). Nothing a session says is
# ever lost, which is what compaction can lean on — and what --resume / /resume
# rebuilds a conversation from. Enabled by main(); tests keep it off unless they
# point SESSION_DIR at a temp dir and turn it on.
SESSION_DIR = os.environ.get("HARNLESS_SESSIONS_DIR") or os.path.join(
    CWD, ".harnless", "sessions"
)
SESSION_LOG = False  # journal messages as they enter the conversation (main() enables)
SESSION_ID = ""  # <YYYYmmdd-HHMMSS>-<rand>, one per session (set in main())
SESSION_MODE = "interactive"  # recorded in each log's header ("interactive" / "one-shot")
SESSION_WARNED = False  # a journaling failure is reported once, not per record
# Journal state, keyed like _record_usage keys a conversation: MAIN_CONV_ID for
# the top-level conversation, a per-run conv id for a sub-agent, else id(messages).
SESSION_FILES: dict = {}  # conversation key -> the .jsonl it is appending to
SESSION_SEQ: dict = {}  # conversation key -> next record seq (a stable message id:
# compaction can later name the turns it replaced by their seq, and a resumed run
# picks up where the log left off)
SESSION_MSG_SEQ: dict = {}  # conversation key -> journal seq of each message *in the
# conversation*, in order (None for a message a rebuild invented, which has no record
# of its own). It is what lets a compact record name the seqs it replaced.
COMPACT_STATS: dict = {}  # conversation key -> rounds / turns replaced (/status)
AGENT_LOG_ID = ""  # delegation chain id of the agent currently running ("" = top level)
SUBAGENT_CHILDREN: dict = {}  # parent chain id -> how many sub-agent runs it started
# Server-reported token usage per conversation, keyed by conversation id
# (MAIN_CONV_ID for the top-level conversation, unique counters for
# sub-agents — see _record_usage).
USAGE_BY_CONV: dict[int, dict] = {}
# The `message` argument of an agent's `exit` call, per conversation id: the
# text a sub-agent meant as its closing statement, recorded by _agent_loop's
# exit handler and harvested by tool_task as that sub-agent's summary. Dropped
# when the sub-agent finishes.
EXIT_NOTE_BY_CONV: dict[int, str] = {}
MAIN_CONV_ID = 0
_NEXT_CONV_ID = 1  # monotonically increasing usage keys for sub-agent runs
# Preferred shell for run_shell on Windows: "auto" (pwsh → powershell → cmd),
# or force "pwsh" / "powershell" / "cmd". Set from --shell in main().
SHELL_PREFERRED = "auto"
COLORS_ENABLED = True
def _stdout_can_encode_emoji() -> bool:
    """True if sys.stdout's encoding can encode every emoji icon."""
    enc = getattr(sys.stdout, "encoding", None)
    if not enc:
        return False
    try:
        for ch in config.ICONS.values():
            ch.encode(enc)
    except (LookupError, UnicodeEncodeError):
        return False
    return True
EMOJI_ENABLED = _stdout_can_encode_emoji()
_SHELL_CACHE: dict[str, tuple] = {}
TODO_ITEMS = []  # [{"id": int, "text": str, "status": "pending"|"in_progress"|"done"}]
TODO_NEXT_ID = 1
TODO_FILE = os.path.join(CWD, ".harnless", "todo.md")
TODO_LAST_INJECTED = None  # serialized list state last injected as a reminder
MEMORY_PROJECT_FILE = os.path.join(CWD, ".harnless", "memory.md")
MEMORY_GLOBAL_FILE = os.path.join(os.path.expanduser("~"), ".harnless", "memory.md")
MCP_TOOLS = []      # OpenAI tool specs for MCP tools
MCP_DISPATCH = {}   # tool name -> (MCPClient, tool name)
MCP_CLIENTS = []    # all configured MCP clients (for /status)
PENDING_MCP = []    # [(name, config)] servers with "enabled": false; enable via /tools
DISABLED_TOOLS = set()  # tool names toggled off via /tools
HISTORY = []
HISTORY_MAX = 1000
HISTORY_FILE = os.environ.get("HARNLESS_HISTORY") or os.path.join(
    os.path.expanduser("~"), ".harnless_history"
)
