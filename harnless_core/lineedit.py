"""Reading the keyboard: the raw-mode key iterators (Windows and POSIX), the
cursor-editing line editor behind `readline_prompt`, and the console-side watchers the
agent loop uses (ESC handling, the context-usage meter, upload progress)."""

import os
import sys
import threading
import time

from harnless_core import config, context, render, state

def _windows_ctrl_held() -> bool:
    """Best-effort: True if Ctrl is held right now (GetAsyncKeyState sign bit).

    msvcrt.getwch() cannot distinguish Enter from Ctrl+Enter (both arrive
    as '\\r'), so when a '\\r' is read we check the live key state. The user
    is normally still holding Ctrl when the event is processed; a very fast
    tap may read back as plain Enter.
    """
    import ctypes

    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
    except Exception:
        return False

def _windows_key_token(getwch):
    """Read one key via getwch and return its token."""
    ch = getwch()
    if ch in ("\x00", "\xe0"):
        return config._WINDOWS_EXT_MAP.get(getwch(), "ignore")
    if ch == "\r":
        return "ctrl_enter" if _windows_ctrl_held() else "enter"
    if ch == "\n":
        return "newline"
    if ch == "\x08":
        return "backspace"
    if ch == "\x03":
        return "ctrl_c"
    if ch == "\x04":
        return "ctrl_d"
    if ch == "\x15":
        return "ctrl_u"
    if ch == "\x1b":
        return "esc"
    if ch == "\t" or ord(ch) < 32:
        return "ignore"
    return ("char", ch)

def _iter_keys_windows():
    """Yield key tokens from the Windows console via msvcrt."""
    import msvcrt

    while True:
        yield _windows_key_token(msvcrt.getwch)

def _iter_keys_windows_poll(stop_event):
    """Like _iter_keys_windows, but polls kbhit() so the reader thread can
    be stopped without a keypress."""
    import msvcrt

    while not stop_event.is_set():
        if not msvcrt.kbhit():
            time.sleep(0.02)
            continue
        yield _windows_key_token(msvcrt.getwch)

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
        # Ctrl+Enter as emitted by terminals that can send it (in raw mode
        # plain Enter and Ctrl+Enter are both just '\\r'):
        "13;5u": "ctrl_enter",  # kitty keyboard protocol
        "27;5;13~": "ctrl_enter",  # xterm modifyOtherKeys level 2
    }.get(params, "ignore")

def _set_raw_input(fd):
    """Put the terminal in raw mode for *input only*: tty.setraw's changes,
    but with oflag left intact.

    tty.setraw runs cfmakeraw, which zeroes the output flags as well, and
    stdin and stdout are the same terminal device: while it is in effect, a
    '\\n' written to stdout reaches the terminal without the CR that ONLCR
    would have added, and every line printed after the first starts wherever
    the previous one ended (staircase output). That window covers the whole
    streaming turn — the interrupt watcher holds the terminal while the
    harness prints reasoning, Markdown replies, and tool lines. Input-only raw
    mode keeps the input side exactly as cfmakeraw sets it (ICRNL off: Enter
    still arrives as '\\r'; ISIG off: Ctrl+C still arrives as '\\x03') while
    OPOST/ONLCR go on translating newlines for stdout."""
    import termios

    mode = termios.tcgetattr(fd)  # type: ignore[reportAttributeAccessIssue]
    # cfmakeraw's pass, index by index — except mode[1] (oflag), left intact:
    # the terminal keeps turning "\n" into CR-NL on output, which is what
    # every print in the harness relies on.
    mode[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                 | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
    mode[2] &= ~(termios.CSIZE | termios.PARENB)
    mode[2] |= termios.CS8
    mode[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG
                 | termios.IEXTEN)
    mode[6][termios.VMIN] = 1
    mode[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSAFLUSH, mode)  # type: ignore[reportAttributeAccessIssue]

def _iter_keys_posix():
    """Yield key tokens from a POSIX terminal in input-only raw mode."""
    import select
    import termios

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)  # type: ignore[reportAttributeAccessIssue]

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
        _set_raw_input(fd)
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
                elif seq == "\r":
                    # Some terminals (e.g. tmux with a custom binding) emit
                    # ESC \r for Ctrl+Enter.
                    yield "ctrl_enter"
                else:
                    yield "ignore"
            elif ch == "\r":
                yield "enter"
            elif ch == "\n":
                yield "newline"
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
        termios.tcsetattr(fd, termios.TCSADRAIN, old)  # type: ignore[reportAttributeAccessIssue]

def _iter_keys_posix_poll(stop_event):
    """Like _iter_keys_posix, but polls with select() so the reader thread
    can be stopped without a keypress."""
    import select
    import termios

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)  # type: ignore[reportAttributeAccessIssue]

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
        _set_raw_input(fd)
        while not stop_event.is_set():
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                continue
            ch = read_char()
            if ch is None:
                yield "ctrl_d"
                return
            if ch == "\x1b":
                # A bare ESC has no following bytes; an escape sequence does.
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    yield "esc"
                    continue
                seq = read_char()
                if seq == "[":
                    yield _parse_csi_seq(read_char)
                elif seq == "\r":
                    # Some terminals (e.g. tmux with a custom binding) emit
                    # ESC \r for Ctrl+Enter.
                    yield "ctrl_enter"
                else:
                    yield "ignore"
            elif ch == "\r":
                yield "enter"
            elif ch == "\n":
                yield "newline"
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
        termios.tcsetattr(fd, termios.TCSADRAIN, old)  # type: ignore[reportAttributeAccessIssue]

def _iter_keys_interrupt(stop_event):
    """Yield key tokens, polling so the reader thread can be stopped."""
    if os.name == "nt":
        yield from _iter_keys_windows_poll(stop_event)
    else:
        yield from _iter_keys_posix_poll(stop_event)

def check_double_esc(presses: dict, token, now: float) -> bool:
    """Track ESC presses in `presses` ({"last_esc": float}).

    Returns True when a second ESC arrives within DOUBLE_ESC_WINDOW seconds
    of the first; any other key resets the wait.
    """
    if token == "esc":
        if now - presses["last_esc"] < config.DOUBLE_ESC_WINDOW:
            return True
        presses["last_esc"] = now
    else:
        presses["last_esc"] = 0.0
    return False

class InterruptWatcher:
    """Watches stdin on a background thread while a stream is in flight.

    Two ESC presses within DOUBLE_ESC_WINDOW seconds cancel the stream:
    the attached socket is closed so the main thread's blocked send or
    read unblocks, and `triggered` is set for the stream loop to check.
    The upload socket is attached while the request body is being sent
    (see _open_request's on_socket), so an interrupt can also abort the
    context send itself.
    """

    def __init__(self):
        self._stop = threading.Event()
        self._sock = None
        self._sock_lock = threading.Lock()
        self._thread = None
        self.triggered = False
        # Called after the "press esc again" hint is printed (the hint adds
        # 2 lines below any progress line; the spinner uses this to clear
        # the right line on stop).
        self.on_hint = None

    def attach_socket(self, sock):
        with self._sock_lock:
            self._sock = sock

    def start(self):
        if not sys.stdin.isatty():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=0.5)

    def _cancel(self):
        self.triggered = True
        with self._sock_lock:
            sock = self._sock
        if sock is not None:
            # close() (not shutdown()) unblocks a pending recv on Windows;
            # the stream loop swallows the resulting read error.
            try:
                sock.close()
            except OSError:
                pass

    def _run(self):
        presses = {"last_esc": 0.0}
        hinted = False
        for token in _iter_keys_interrupt(self._stop):
            if check_double_esc(presses, token, time.monotonic()):
                self._cancel()
                return
            if token == "esc" and not hinted:
                hinted = True
                sys.stdout.write(
                    "\n" + render.colorize("press esc again to interrupt", "dim") + "\n"
                )
                sys.stdout.flush()
                if self.on_hint is not None:
                    self.on_hint()

def _edit_cur(row: int, total: int, width: int) -> tuple:
    """Cursor cell (row, col) after `total` display columns have been written
    into a run that starts at physical `row`, column 0.

    Soft wrap is *pending*: measured on Windows (CONOUT$ via
    GetConsoleScreenBufferInfo) a run that exactly fills a row leaves the
    cursor on that row's **last** column, and the next row is only entered
    when the next character is written — `ESC[nA/B/C/D` and `CR` act on that
    cell and clear the pending flag without moving the cursor, and `ESC[2K`
    erases that row. So the position of a character that lands exactly on a
    wrap boundary is the last cell of the row before it, not column 0 of the
    row after it (which is where the cursor would have to move to *first*).
    """
    if total <= 0:
        return row, 0
    # Rows the run occupies: a line that soft-wraps without ending on a column
    # boundary still takes the next row, while a run that ends exactly on a
    # boundary is only *pending* there, so it counts once.
    rows = (total + width - 1) // width
    col = width - 1 if total % width == 0 else total % width
    return row + rows - 1, col

def _edit_rows(text: str, prompt_w: int, width: int) -> int:
    """How many physical rows `prompt + text` paints once soft-wrapped."""
    rows = 0
    for i, ln in enumerate(text.split("\n")):
        total = (prompt_w if i == 0 else 0) + render.display_width(ln)
        rows += max(1, (total + width - 1) // width)  # an empty line is a row too
    return max(1, rows)

def _edit_phys_pos(text: str, prompt_w: int, pos: int, width: int) -> tuple:
    """Terminal cursor cell (row, col) for character position `pos` in `text`,
    printed after a prompt of display width `prompt_w` in a terminal `width`
    columns wide (see `_edit_cur` for the wrap model). `pos` may be len(text)
    (end of text)."""
    lines = text.split("\n")
    row = 0
    for i, ln in enumerate(lines):
        start = prompt_w if i == 0 else 0
        if pos <= len(ln):
            return _edit_cur(row, start + render.display_width(ln[:pos]), width)
        # Next logical line: the printed newline drops the cursor to column 0
        # of the row after the rows this line occupied.
        total = start + render.display_width(ln)
        row += max(1, (total + width - 1) // width)
        pos -= len(ln) + 1
    return row, 0

def _edit_line(prompt: str, keys, menu=None) -> str:
    """Run a minimal line editor over a key-token iterator. Returns the line
    (may contain newlines inserted via the "newline" token, e.g. Ctrl+J, or
    via Enter when auto-send is off). Enter submits when AUTO_SEND is on;
    when it is off, Enter inserts a newline and Ctrl+Enter (or Ctrl+D) sends.

    Up/down first move the caret line-by-line within a multi-line buffer
    (column preserved, clamped to the target line). From the first line, up
    recalls history while saving the current line as a draft; the first down
    restores it (buffer and caret). Editing a recalled entry discards the
    draft, so subsequent downs walk history forward as usual."""
    buf = []
    pos = 0
    hist_idx = len(state.HISTORY)
    draft = None  # (buf, pos) of the line being edited when a history recall
    # started, so the first down press returns to it (with its caret)

    prev_rows = 0        # physical rows the previous render painted
    prev_cur_row = 0     # which of those rows the cursor ended on
    prev_rendered = False

    def redraw(commit=False):
        nonlocal prev_rows, prev_cur_row, prev_rendered
        line = "".join(buf)
        width = render.terminal_width()
        prompt_w = render.display_width(config._ANSI_RE.sub("", prompt))
        # An explicit CR: the render must land rows correctly whatever the
        # terminal's output translation is doing (input-only raw mode keeps
        # ONLCR on, but the editor does not depend on it — an extra CR at
        # column 0 is inert).
        text = line.replace("\n", "\r\n")
        out = ""
        if prev_rendered:
            # Walk back to the first physical row of the previous render and
            # clear every row it painted (cursor moves only, no newlines, so
            # the screen never scrolls while editing). The walk-back starts
            # from where the cursor actually *is* — the edit row whenever the
            # previous render repositioned it, the last row otherwise. Walking
            # back by the previous text's *end* row instead overshoots as soon
            # as the cursor sits above that row: each render then starts one
            # row too high, the block creeps up over the output above it, and
            # its old tail rows are left behind as duplicates.
            if prev_cur_row:
                out += f"\x1b[{prev_cur_row}A"
            out += "\r"
            for i in range(prev_rows):
                out += "\x1b[2K"
                if i < prev_rows - 1:
                    out += "\x1b[B"
            if prev_rows > 1:
                out += f"\x1b[{prev_rows - 1}A"
        rows = _edit_rows(line, prompt_w, width)
        out += prompt + text
        cur_row = rows - 1
        if commit:
            # Redrawn from the block's first row, so the caller's newline lands
            # on the fresh row right below it.
            out += "\r\n"
        elif pos < len(line):
            # Position the cursor at the edit position with cursor moves (not
            # by re-printing, which would re-draw every line after the first
            # newline). Cells are physical (row, col), so soft-wrapped lines
            # are handled too.
            trow, tcol = _edit_phys_pos(line, prompt_w, pos, width)
            erow, ecol = _edit_phys_pos(line, prompt_w, len(line), width)
            # After printing, the cursor is at the end of the text (erow,
            # ecol). The target is always at or above it (trow <= erow), so
            # move UP by the row difference (the column is preserved across
            # the up-move), then adjust the column toward the target. Moving
            # DOWN here would push the cursor into the blank rows below the
            # text, and each re-render would start from that drifted position
            # and re-print the previous line on every keystroke (it only went
            # unnoticed at the bottom of the screen, where a down-move is a
            # no-op).
            if erow > trow:
                out += f"\x1b[{erow - trow}A"
            if tcol != ecol:
                out += f"\x1b[{tcol - ecol}C" if tcol > ecol else f"\x1b[{ecol - tcol}D"
            cur_row = trow
        sys.stdout.write(out)
        sys.stdout.flush()
        prev_rows = rows
        prev_cur_row = cur_row
        prev_rendered = True

    def commit():
        """Leave the line on screen the way the user sees it and drop the cursor
        onto a fresh row below it. Needed because the cursor can be parked in
        the middle of a wrapped/multi-line block: without the redraw the tail
        rows stay painted and the next output prints over them."""
        redraw(commit=True)

    redraw()
    while True:
        token = next(keys)
        if token == "enter":
            if state.AUTO_SEND:
                break
            # Auto-send off: Enter inserts a newline; Ctrl+Enter sends.
            draft = None
            buf.insert(pos, "\n")
            pos += 1
        elif token == "ctrl_enter":
            break
        elif token == "ctrl_c":
            commit()
            raise KeyboardInterrupt
        elif token == "ctrl_d":
            if not buf:
                commit()
                raise EOFError
            break
        elif token == "backspace":
            if pos > 0:
                draft = None
                del buf[pos - 1]
                pos -= 1
        elif token == "delete":
            if pos < len(buf):
                draft = None
                del buf[pos]
        elif token == "ctrl_u":
            draft = None
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
            line = "".join(buf)
            if line.rfind("\n", 0, pos) >= 0:
                # Multi-line input: move the caret up within the buffer first
                # (column preserved, clamped to the line above) instead of
                # jumping to history and losing the draft.
                line_start = line.rfind("\n", 0, pos) + 1
                prev_start = line.rfind("\n", 0, line_start - 1) + 1
                pos = prev_start + min(pos - line_start, line_start - prev_start - 1)
            elif hist_idx > 0:
                # Caret on the first line: recall the previous history entry.
                # Leaving the *current* line for history (the first up) saves
                # it as the draft, so the first down press restores it; from
                # an empty line nothing is saved, so downs walk history
                # forward as usual (the last one clears the line).
                if hist_idx == len(state.HISTORY) and buf:
                    draft = (list(buf), pos)
                hist_idx -= 1
                buf = list(state.HISTORY[hist_idx])
                pos = len(buf)
        elif token == "down":
            if draft is not None:
                # First down after a history recall: restore the saved line
                # (buffer and caret).
                buf, pos = draft
                draft = None
                hist_idx = len(state.HISTORY)
            else:
                line = "".join(buf)
                if line.rfind("\n", pos) >= 0:
                    # Multi-line input with the caret above the last line:
                    # move down within the buffer (column preserved, clamped).
                    line_start = line.rfind("\n", 0, pos) + 1
                    next_start = line.find("\n", pos) + 1
                    next_end = line.find("\n", next_start)
                    next_len = (len(line) if next_end < 0 else next_end) - next_start
                    pos = next_start + min(pos - line_start, next_len)
                elif hist_idx < len(state.HISTORY):
                    hist_idx += 1
                    buf = list(state.HISTORY[hist_idx]) if hist_idx < len(state.HISTORY) else []
                    pos = len(buf)
        elif token == "newline":
            draft = None
            buf.insert(pos, "\n")
            pos += 1
        elif isinstance(token, tuple) and token[0] == "char":
            draft = None
            ch = token[1]
            if ch == "/" and not buf and menu is not None:
                # Slash on an empty line opens the caller's command menu; a selection
                # replaces the line, a cancel keeps the typed slash. The editor does not
                # know what a command is — the REPL hands it `menu` (commands_menu).
                cmd = menu()
                if cmd is not None:
                    buf = list(cmd)
                    pos = len(buf)
                else:
                    buf.insert(pos, ch)
                    pos += 1
            else:
                buf.insert(pos, ch)
                pos += 1
        redraw()
    commit()
    return "".join(buf)

def readline_prompt(prompt: str, menu=None) -> str:
    """Read a line with arrow-key history. Falls back to input() when stdin
    is not a TTY (piped input, tests).

    `menu`, when given, is called for a lone `/` on an empty line and returns the
    command to put on the line (or None): the editor reads keys, the caller decides
    what a command menu is.
    """
    if not sys.stdin.isatty():
        return input(prompt)
    try:
        keys = _iter_keys_windows() if os.name == "nt" else _iter_keys_posix()
    except Exception:
        return input(prompt)
    line = _edit_line(prompt, keys, menu)
    context._history_add(line)
    return line
