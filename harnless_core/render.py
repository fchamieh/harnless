"""Terminal rendering: the colour/icon tables and their switches, the Markdown
renderer, the width helpers, and the lines the agent loop prints.

The printers are shared with the journal replay on purpose (see harnless_core/journal.py
replay_session_tail) so a resumed session replays exactly what it showed live."""

import os
import re
import shutil
import sys
import unicodedata

from harnless_core import config, state

def set_color_enabled(enabled: bool):
    state.COLORS_ENABLED = enabled
def set_emoji_enabled(enabled: bool):
    state.EMOJI_ENABLED = enabled
def icon(key: str) -> str:
    table = config.ICONS if state.EMOJI_ENABLED else config.ASCII_ICONS
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
    if not state.COLORS_ENABLED or key is None:
        return text
    return f"{config.ANSI[key]}{text}{config.ANSI['reset']}"
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
def display_width(text: str) -> int:
    """Approximate terminal display width (East Asian wide/fullwidth chars count as 2)."""
    width = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width
def terminal_width() -> int:
    """Current terminal column count (falls back to 80)."""
    return shutil.get_terminal_size((80, 24)).columns
def _is_table_sep(line: str) -> bool:
    """True if line is a table delimiter row (must contain a pipe)."""
    return "|" in line and bool(config._MD_TABLE_SEP.match(line))
def _split_table_row(line: str) -> list:
    """Split a pipe-table row into stripped cell strings.

    Leading/trailing pipes are removed; \\| stays inside its cell.
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in config._MD_PIPE_SPLIT.split(s)]
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
        if not state.COLORS_ENABLED:
            self._out.write(chunk)
            self._out.flush()
            return
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)

    def flush(self):
        if not state.COLORS_ENABLED:
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
            fence = config._MD_FENCE.match(line)
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
        fence = config._MD_FENCE.match(line)
        if fence and fence.group(1)[0] == "`" and "`" in fence.group(2):
            fence = None
        if self._table is not None:
            if (line.strip() and "|" in line and not fence
                    and not config._MD_HEADING.match(line) and not config._MD_QUOTE.match(line)
                    and not config._MD_LIST.match(line)):
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
        m = config._MD_HEADING.match(line)
        if m:
            self._line(prefix + colorize(self._inline(m.group(2).strip()), "heading"))
            return
        if config._MD_HR.match(line):
            self._line(prefix + colorize("─" * 20, "dim"))
            return
        m = config._MD_LIST.match(line)
        if m:
            pad, marker, rest = m.groups()
            bullet = "• " if marker in ("-", "*", "+") else f"{marker} "
            self._line(prefix + pad + colorize(bullet, "bullet") + self._inline(rest))
            return
        m = config._MD_QUOTE.match(line)
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
                display_width(config._ANSI_RE.sub("", self._inline(row[i])))
                for row in plain
            )
            widths.append(max(w, config._TABLE_MIN_COL))

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
        if budget < ncols * config._TABLE_MIN_COL:
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

            labels = [config._ANSI_RE.sub("", self._inline(c)) for c in header]
            for index, record in enumerate(body or [None]):
                if index:
                    table_line("")
                for i, label in enumerate(labels):
                    value = (config._ANSI_RE.sub("", self._inline(record[i]))
                             if record is not None else None)
                    wrapped(label if value is None else f"{label}: {value}")
            return
        while sum(widths) > budget and max(widths) > config._TABLE_MIN_COL:
            i = widths.index(max(widths))
            widths[i] -= 1

        def rule(left, mid, right):
            return left + mid.join("─" * (w + 2) for w in widths) + right

        def cell(text, i, align):
            width = widths[i]
            w = display_width(config._ANSI_RE.sub("", text))
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
                if display_width(config._ANSI_RE.sub("", visible)) > widths[i]:
                    visible = _truncate_visible(config._ANSI_RE.sub("", visible), widths[i])
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
                    and text[i + 1] in config._MD_ESCAPABLE):
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
                link = config._MD_LINK.match(text, i)
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
        text = config._MD_BOLD.sub(lambda m: colorize(m.group(1), "bold"), text)
        return config._MD_ITALIC.sub(lambda m: colorize(m.group(1), "italic"), text)
def print_assistant(content: str):
    """Print a complete (non-streamed) assistant message with Markdown styling."""
    label = f"{icon('assistant')} assistant> "
    print()
    print(state.OUTPUT_INDENT + colorize(label, "assistant"), end="", flush=True)
    renderer = MarkdownRenderer(indent=display_width(state.OUTPUT_INDENT + label))
    renderer.write(content or "")
    renderer.flush()
    print()
def print_user(text: str):
    """Print a user turn the way the prompt echoed it: the label, then their text
    untouched (what a resumed session replays — see replay_session_tail)."""
    print(colorize(f"{icon('user')} you> ", "user") + (text or ""))
def print_reasoning(reasoning: str, limit: int = 0):
    """The `thinking:` line a non-streamed turn gets (the streaming path paints it
    live instead). `limit` caps a replayed one: thinking is the part of a session
    worth summarising, not reading again."""
    text = (reasoning or "").strip()
    if not text:
        return
    if limit and len(text) > limit:
        text = text[:limit] + f"... [+{len(text) - limit} chars in the session log]"
    print(state.OUTPUT_INDENT + colorize(f"{icon('thinking')} thinking: {text}", "thinking"))
def print_tool_call(name: str, raw_args: str):
    """How the loop shows a call before running it: the name and its arguments, previewed."""
    args = raw_args or ""
    print(
        state.OUTPUT_INDENT
        + colorize(f"{icon('tool')} {name}({args[:config.TOOL_ARG_ECHO]})", "tool")
    )
def print_tool_result(result: str):
    """How the loop shows what a call returned: its first TOOL_RESULT_ECHO characters."""
    text = result or ""
    print(
        state.OUTPUT_INDENT
        + colorize(
            f"{icon('result')} {text[:config.TOOL_RESULT_ECHO]}"
            f"{'...' if len(text) > config.TOOL_RESULT_ECHO else ''}",
            "result",
        )
    )
