"""Fixed tunables: every knob the harness ships with, plus the fixed text it is
built from (tool descriptions, notes and prompts are model-facing copy).

Nothing here is ever rebound at runtime. A knob a flag, a slash command or a test can
change lives in state.py instead — see harnless_core/state.py."""

import os
import re

VERSION = "1.8.3"
SUBAGENT_DEPTH_CEILING = 10  # sanity clamp for --max-subagents
SUBAGENT_STEPS_CEILING = 10_000  # sanity clamp for --max-subagent-steps
SUBAGENT_STEP_LIMIT_CODE = 4  # exit code reported to the parent when the guard fires
# A capped sub-agent gets one closing turn instead of stopping cold: a run that
# ends on a tool-call turn leaves tool_task with no text to hand the parent.
STEP_LIMIT_CLOSE_PROMPT = (
    "You have reached the harness step limit for this sub-agent. Do not call any "
    "more tools. Reply now with only the final summary the parent will receive: "
    "self-contained findings with file paths and line numbers, decisions, and "
    "caveats."
)
# Shown to the delegating agent when a sub-agent produced no hand-off text at
# all. The sub-agent's printed output never enters the parent's context, so an
# empty result has to say so — otherwise "nothing found" and "summary lost"
# look identical and the parent redoes the work.
NO_SUMMARY_NOTE = (
    "(sub-agent returned no final summary — the output it printed is not part of "
    "your context, so this task is still undone: delegate a narrower task or do it yourself)"
)
# Prefix on a hand-off recovered from less explicit places (a reasoning-only
# turn, or narration from before the final turn) so the parent knows the text
# was salvaged, not written as a summary.
SALVAGED_SUMMARY_NOTE = "(no closing summary from the sub-agent; its last output follows)"
DOUBLE_ESC_WINDOW = 2.0
# Show a "sending context" progress line while waiting for the LLM's first
# token, for large requests only (estimated token count, chars/4, including
# the tool schemas). The line appears CONTEXT_PROGRESS_DELAY seconds in,
# reports request-body upload progress (percent + tokens sent so far) while
# the payload is being sent, then the elapsed wait. Active only on a TTY.
CONTEXT_PROGRESS_THRESHOLD = 30_000
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
SESSION_READ_LIMIT = 30_000  # sessions: transcript text returned from the log (chars)
SESSION_RECORD_CHARS = 2_000  # sessions: chars kept of one recorded message
# Search filtering: grep/glob skip what git would not have shown in the same
# directory. GITIGNORE_ANCESTOR_SCAN bounds how far up from the searched
# directory those files are looked for (it stops earlier at the .git directory).
GITIGNORE_NAME = ".gitignore"
GITIGNORE_SKIP_NAMES = (".git", "node_modules", "__pycache__")  # always skipped
GITIGNORE_ANCESTOR_SCAN = 16
COMPACT_THRESHOLD_CEILING = 99  # sanity clamp for --compact-threshold
COMPACT_KEEP_MIN_MESSAGES = 6  # floor on the verbatim tail (message count)
COMPACT_TOOL_RESULT_KEEP = 1_500  # chars kept of an elided tool result (head+tail)
COMPACT_SUMMARY_LIMIT = 4_000  # chars kept from the generated handoff note
# Said out loud when the summarizing request itself failed: the turns were still
# dropped (that is what made room), so the session must know the note is missing.
COMPACT_FALLBACK_NOTE = (
    "(no model summary: the summarizing request failed, so the earlier turns "
    "were dropped without being rewritten — the full transcript is in the "
    "session log)"
)
# The same said by a run that keeps no journal: there the turns really are gone, and a
# note must not point at a log that does not exist (see journal.journaling()).
COMPACT_FALLBACK_NOTE_UNJOURNALED = (
    "(no model summary: the summarizing request failed, so the earlier turns "
    "were dropped without being rewritten — and this run keeps no session log, "
    "so they are gone)"
)
# Said about a handoff note the harness had to cut short: where the rest of it still is.
COMPACT_NOTE_HINT = "the full transcript is in the session log (sessions tool)"
COMPACT_NOTE_HINT_UNJOURNALED = "this run keeps no session log, so the rest of it is gone"
CONTEXT_OVERFLOW_CODE = 3  # exit code: context full, and compaction could not help
# Given to the model to write the note that replaces the turns about to go. The
# todo list and memory notes are re-injected by the harness anyway, so the note
# must not spend its budget restating them.
COMPACT_SUMMARY_PROMPT = (
    "You are compacting an agent session that ran out of context. The "
    "conversation below is about to be replaced by the note you write; the last "
    "few turns will be kept as-is. Write the handoff note the agent continues "
    "from. Cover, in dense bullets under 400 words: the goal; decisions taken "
    "and why; files touched (exact paths, with line numbers where it matters); "
    "facts and results worth keeping; the current state; open problems and the "
    "next step. Do not call tools. Do not restate the todo list or the memory "
    "notes — the harness supplies those separately. Reply with the note only."
)
# Marks the summary as harness-generated inside the conversation, so the model
# never mistakes it for something the user said.
COMPACT_SUMMARY_HEADER = (
    "[harnless compacted this session: the earlier turns above were replaced by "
    "the note below]"
)
SESSION_URL_KEEP = 300  # chars of a content part's URL kept (base64 image data is elided)
SESSION_LIST_LIMIT = 30  # sessions listed by /resume / /sessions (count)
SESSION_RECORD_LIMIT = 50_000  # records read from one journal file (sanity ceiling)
SESSION_KEEP_CEILING = 10_000  # sanity clamp for --resume-last
# When the REPL resumes a session it echoes back the last stretch of the transcript
# it restored, printed exactly the way the original session printed it (see
# replay_session_tail): the `you>` line, the thinking line, the assistant reply
# through the same Markdown renderer, the tool-call and tool-result lines with the
# same caps. RESUME_TAIL_ITEMS is how many *speaking turns* (user / assistant) the
# echo reaches back over, RESUME_TAIL_RECORDS is the hard bound on records replayed
# (so a tool-heavy final turn cannot flood the screen), and RESUME_TAIL_CHARS caps a
# replayed thinking block (a reply is shown whole — it is what the user came back for).
# Items = 0 turns off the echo; only the interactive mode asks for it.
RESUME_TAIL_ITEMS = 4  # speaking turns a resumed run echoes back
RESUME_TAIL_RECORDS = 12  # journal records replayed at most
RESUME_TAIL_CHARS = 1_200  # chars of a replayed thinking block
# User messages the harness injects for the model and never prints as the user's own:
# the todo reminder, and the handoff note a compacted head is replaced by.
TODO_REMINDER_HEADER = "Current todo list:"
# Said instead of a result for a tool call the log recorded but never answered
# (the session ended while it was running): a server rejects a dangling call.
SESSION_UNANSWERED_NOTE = (
    "[harnless: this tool call was still running when the session ended, so the "
    "session log has no result for it]"
)
_SESSION_META_KEYS = ("type", "seq", "ts", "chars")  # journal envelope, not message keys
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_REF_RE = re.compile(r"@\[((?:cwd|file|https?)://[^\]]+)\]")
# run_shell timeouts. The direct child is only the shell *wrapper*: the real
# work (a test runner, a compiler, a dev server) runs as its grandchild and
# inherits the output pipes. Killing the wrapper alone leaves that orphan
# holding the pipes open, so the harness's output drain waits for it instead of
# returning — a "timed out after 120s" that never came back, and a runaway the
# user had to kill by hand. Hence: kill the whole tree, then drain with a
# deadline of its own.
SHELL_TIMEOUT_DEFAULT = 120  # seconds a command may run before it is killed
SHELL_TIMEOUT_MIN = 1
SHELL_TIMEOUT_CEILING = 3_600  # a tool call that never returns stalls the turn
KILL_DRAIN_TIMEOUT = 5  # seconds spent collecting output after the kill
TASKKILL_TIMEOUT = 15  # seconds to let taskkill itself tear the tree down
SHELL_NOTE = (
    " Commands run in PowerShell (pwsh, falling back to powershell.exe); use PowerShell syntax "
    "(e.g. Get-ChildItem, Select-String, Get-Content, Copy-Item) — bash/cmd syntax will not work."
    if os.name == "nt"
    else " Commands run in bash."
) + (
    " The call waits for everything still holding the output pipes, so do not leave "
    "background processes running: at the timeout the command and its whole process tree are killed."
)
# What the terminal shows of a tool call about to run and of the answer it returned.
# The agent loop prints these and a resumed session replays them with the very same
# caps (see _print_tool_call / _print_tool_result / replay_session_tail), so the
# replay cannot drift from what the original session showed.
TOOL_ARG_ECHO = 200  # chars of a tool call's arguments
TOOL_RESULT_ECHO = 500  # chars of a tool result
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
# What a rejection has to say to count as "out of context" rather than some other bad
# request: llama.cpp (n_ctx, context exceeded), OpenAI-compatible servers ('prompt is
# too long', maximum context length), and the proxies in between word it differently.
_CONTEXT_OVERFLOW_RE = re.compile(
    r"n_ctx|context[_ ]length|context[_ ]window|maximum context|context exceeded"
    r"|too many tokens|prompt is too long|input length|token limit|outside the context",
    re.IGNORECASE,
)
_CONTEXT_OVERFLOW_STATUS = (400, 413, 422)  # rejections worth reading the body of
_HTML_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
_HTML_SKIP_TAGS = {"script", "style", "head", "noscript", "template", "svg"}
SUBAGENT_NOTE = (
    "You are a sub-agent delegated a specific task. Work autonomously using the tools. "
    "The parent receives only your closing message: none of your tool output and nothing "
    "you merely print reaches it. Make that message carry the result — self-contained and "
    "concise, key findings with file paths and line numbers, decisions, and caveats, "
    "omitting intermediate detail (aim for under 50 lines unless the task asks for more). "
    "When the task is complete, write that summary as your reply and then call the exit "
    "tool with code 0 — or pass the same summary as exit's `message` argument; the harness "
    "reads either and keeps the more substantive one. If the task cannot be completed, say "
    "why in your closing message and call exit with a non-zero code."
)
# Result a delegating agent gets when its sub-agent was interrupted. The note
# is carried further up the delegation chain as-is instead of being re-wrapped
# at every level.
SUBAGENT_INTERRUPT_PREFIX = (
    "(sub-agent interrupted by the user; the output below is partial)"
)
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_TIMEOUT = 60  # seconds per request
MCP_HOME_CONFIG = os.path.join(os.path.expanduser("~"), ".harnless", "mcp.json")
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


REPL_COMMANDS = [
    ("/new", "clear session history and start over (in a new session log)"),
    ("/clear-screen", "clear the terminal screen"),
    ("/status", "show context usage, api url, and tools"),
    ("/sessions", "list the recorded session logs"),
    ("/resume", "continue a recorded session: /resume <session-id> (no argument lists them)"),
    ("/compact", "replace this session's older turns with a handoff note now"),
    ("/auto-compact", "on|off, or 'threshold N': compact automatically as the context fills up"),
    ("/tools", "interactive tool menu: up/down move, space toggle, enter apply, esc cancel"),
    ("/auto-send", "on: Enter submits (default); off: Enter inserts a newline, Ctrl+Enter sends"),
    ("/help", "show this help"),
    ("/exit", "quit (alias: /quit)"),
]
