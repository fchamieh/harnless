import urllib.request
import urllib.error
import http.client
"""Talking to the model server: request building, non-streaming and streaming
completions, SSE parsing, progress reporting and the context-window probe."""

import http
import json
import sys
import threading
import time
import urllib

from harnless_core import config, errors, journal, lineedit, measure, render, state


def _response_socket(resp):
    """Best-effort: the underlying socket of a urllib response (for shutdown).

    Older Pythons wrap the socket in a SocketFile (resp.fp.fp.raw._sock);
    Python 3.14+ removed it, so resp.fp is the BufferedReader directly
    (resp.fp.raw._sock). Try both layouts.
    """
    for attr in ("fp.fp.raw._sock", "fp.raw._sock"):
        try:
            obj = resp
            for part in attr.split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    return None

class ContextProgress:
    """Shows a "sending context" progress line while waiting for the LLM's
    first token, for large requests only (estimated >= CONTEXT_PROGRESS_THRESHOLD
    tokens). The line appears CONTEXT_PROGRESS_DELAY seconds in, reports the
    request-body upload (percent + tokens sent so far) while it is in flight,
    then the elapsed wait, and is cleared when the wait ends.

    Only active when stdout is a TTY. If lines are printed below the progress
    line (e.g. the double-ESC hint), the line freezes so a redraw can't
    clobber them, and stop() clears the right line.
    """

    def __init__(self, messages: list, tools: list = None):
        self.tokens = measure.estimate_context_tokens(messages, tools)
        self.upload = None  # _UploadProgress, set by _open_request before sending
        self._stop = threading.Event()
        self._thread = None
        self._drawn = False
        self._hint_before = False
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self.extra_lines = 0  # lines printed below the progress line

    @property
    def active(self) -> bool:
        return self._thread is not None

    def start(self):
        if self.tokens < config.CONTEXT_PROGRESS_THRESHOLD:
            return
        if not sys.stdout.isatty():
            return
        self._t0 = time.monotonic()
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
            thread.join(timeout=2.0)
        with self._lock:
            if self._drawn:
                self._drawn = False
                if not self._hint_before and self.extra_lines:
                    # lines were printed below us (e.g. the esc hint): move
                    # back up to the progress line before clearing it
                    sys.stdout.write(f"\x1b[{self.extra_lines}A")
                sys.stdout.write("\r\x1b[2K")
                sys.stdout.flush()

    def note_lines_below(self, n: int):
        """Called when `n` lines are printed below the progress line."""
        self.extra_lines += n

    def _run(self):
        if self._stop.wait(state.CONTEXT_PROGRESS_DELAY):
            return
        with self._lock:
            self._hint_before = self.extra_lines > 0
            self._drawn = True
            sys.stdout.write(self._line())
            sys.stdout.flush()
        while not self._stop.is_set():
            if self._stop.wait(0.5):
                return
            if self.extra_lines:
                # something was printed below us (e.g. the esc hint); freeze
                # the line so the redraw can't clobber it
                continue
            with self._lock:
                sys.stdout.write("\r\x1b[2K" + self._line())
                sys.stdout.flush()

    def _line(self) -> str:
        elapsed = render._format_elapsed(time.monotonic() - self._t0)
        upload = self.upload
        if upload is None or upload.sent >= upload.total:
            text = (
                f"{render.icon('wait')} waiting for first token "
                f"({render._format_tokens(self.tokens)} tokens) {elapsed}"
            )
        else:
            pct = upload.sent * 100 // upload.total
            sent_tokens = self.tokens * upload.sent // upload.total
            text = (
                f"{render.icon('wait')} sending context… {pct}% "
                f"({render._format_tokens(sent_tokens)}/{render._format_tokens(self.tokens)} tokens) "
                f"{elapsed}"
            )
        return state.OUTPUT_INDENT + render.colorize(text, "dim")

def normalize_api_url(url: str | None) -> str:
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
    if state.API_KEY:
        headers["Authorization"] = f"Bearer {state.API_KEY}"
    return headers

def probe_context_window() -> int:
    """Best-effort: query the API's /models endpoint for the context window size.

    Returns the model's context window in tokens, or 0 if it cannot be
    determined (server unreachable, no /models endpoint, field missing).
    """
    if not state.API_URL.endswith("/chat/completions"):
        return 0
    models_url = state.API_URL[: -len("/chat/completions")] + "/models"
    try:
        req = urllib.request.Request(models_url, headers=_headers())
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return 0
    models = data.get("data") or []
    if not models:
        return 0
    model = models[0]
    if len(models) > 1:
        for m in models:
            if m.get("id") == state.MODEL:
                model = m
                break

    def _as_int(v):
        return int(v) if isinstance(v, (int, float)) and v > 0 else 0

    # OpenAI/OpenRouter use context_length; llama.cpp exposes meta.n_ctx.
    return _as_int(model.get("context_length")) or _as_int(
        (model.get("meta") or {}).get("n_ctx")
    )

class _UploadProgress:
    """Bytes-sent counter for a request body (updated by the counting opener)."""

    def __init__(self, total: int):
        self.total = total
        self.sent = 0
        self.on_socket = None  # called with the connection socket once open

    def add(self, n: int):
        self.sent = min(self.total, self.sent + n)

def _progress_opener(upload: _UploadProgress) -> urllib.request.OpenerDirector:
    """A urllib opener whose HTTP(S) connections report body upload progress.

    Large writes are sent in PROGRESS_CHUNK pieces (http.client would
    otherwise sendall() the whole body at once, so no progress would be
    observable). All other urllib behavior (proxies, redirects, https,
    timeouts) is unchanged.
    """

    def make_handler(base):
        class Handler(base):
            # Before the stock HTTP/HTTPS handlers (order 500) so ours wins.
            handler_order = 499

            def do_open(self, http_class, req, **kwargs):  # `req`: urllib's own name
                class Counting(http_class):
                    def connect(self):
                        super().connect()
                        # Report the socket as soon as it exists so a
                        # double ESC can close it to abort the send.
                        if upload.on_socket is not None:
                            upload.on_socket(self.sock)

                    def send(self, data):
                        total = len(data)
                        if total <= config.PROGRESS_CHUNK:
                            return super().send(data)
                        mv = memoryview(data)
                        for i in range(0, total, config.PROGRESS_CHUNK):
                            piece = bytes(mv[i : i + config.PROGRESS_CHUNK])
                            super().send(piece)
                            upload.add(len(piece))

                return super(Handler, self).do_open(Counting, req, **kwargs)

        return Handler()

    opener = urllib.request.build_opener()
    opener.add_handler(make_handler(urllib.request.HTTPHandler))
    opener.add_handler(make_handler(urllib.request.HTTPSHandler))
    return opener

def _open_request(req: urllib.request.Request, progress=None, on_socket=None):
    """urlopen `req`; if `progress` (a ContextProgress) is given, track the
    request-body upload in it (progress.upload is set before sending); if
    `on_socket` is given it is called with the connection socket once open,
    so an interrupt can close it to abort the send."""
    if progress is None and on_socket is None:
        return urllib.request.urlopen(req, timeout=600)
    upload = _UploadProgress(len(req.data or b""))
    if progress is not None:
        progress.upload = upload
    upload.on_socket = on_socket
    return _progress_opener(upload).open(req, timeout=600)

def chat(
    messages: list,
    model: str,
    tools: list = None,
    temperature: float = 1.0,
    conv_id: int = None,
) -> dict:
    payload = json.dumps(
        _request_body(messages, model, tools=tools, temperature=temperature)
    ).encode("utf-8")
    req = urllib.request.Request(state.API_URL, data=payload, headers=_headers())
    spinner = ContextProgress(messages, tools)
    spinner.start()
    try:
        with _open_request(req, spinner if spinner.active else None) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # A prompt the server refuses because it is too long is something the agent can
        # act on — compact and retry. Anything else stays the error it was.
        reason = errors._context_overflow_reason(e)
        if reason:
            raise errors.ContextOverflow(reason) from e
        raise
    finally:
        spinner.stop()
    journal._record_usage(messages, data.get("usage"), conv_id)
    return data

def _request_body(
    messages: list,
    model: str,
    stream=None,
    tools: list = None,
    temperature: float = 1.0,
) -> dict:
    """What a chat request sends.

    `stream=None` (the non-streaming call) omits the key entirely. `tools` is the list of
    tool schemas this request may answer with: None or empty sends none — a request whose
    job is to write something, like a compaction handoff note, must not be one the server
    answers with a tool call. Which schemas the agent's own turns advertise is the agent
    layer's call (`_active_tools`); this layer is told, it does not decide.
    """
    body: dict = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if stream is not None:
        body["stream"] = stream
        if stream:
            # Ask for a final usage chunk so /status can show exact token counts.
            body["stream_options"] = {"include_usage": True}
    return body

def _build_request(
    messages: list,
    model: str,
    stream: bool,
    tools: list = None,
    temperature: float = 1.0,
) -> urllib.request.Request:
    payload = json.dumps(
        _request_body(messages, model, stream, tools=tools, temperature=temperature)
    ).encode("utf-8")
    return urllib.request.Request(state.API_URL, data=payload, headers=_headers())

def parse_sse_line(line: str):
    """Parse one SSE line from a streaming chat response.

    Returns the delta dict for data lines, a {"usage": ...} dict for the
    final usage chunk (when stream_options.include_usage is set), the
    string "[DONE]" for the terminator, or None for comments/blank/
    malformed lines.
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
    usage = chunk.get("usage")
    if usage:
        return {"usage": usage}
    choices = chunk.get("choices") or []
    if not choices:
        return None
    return choices[0].get("delta") or {}

def stream_chat(
    messages: list,
    model: str,
    tools: list = None,
    temperature: float = 1.0,
    watcher=None,
    progress=None,
    conv_id: int = None,
):
    """Yield deltas from a streaming chat response until [DONE].

    `tools` is what this request advertises (see _request_body). If `watcher` (an
    InterruptWatcher) is given, the connection and response sockets are attached to it and
    the stream stops once it is triggered: a double ESC during the request-body upload
    closes the upload socket and aborts the send. If `progress` (a ContextProgress) is
    given, the request-body upload is tracked in it.
    """
    req = _build_request(messages, model, stream=True, tools=tools, temperature=temperature)
    try:
        resp_cm = _open_request(req, progress, watcher.attach_socket if watcher else None)
    except urllib.error.HTTPError as e:
        # Refused before the stream even started: if it is a size rejection, raise the
        # thing the agent loop knows how to recover from.
        reason = errors._context_overflow_reason(e)
        if reason:
            raise errors.ContextOverflow(reason) from e
        if watcher is None or not watcher.triggered:
            raise
        return
    except (OSError, http.client.HTTPException):
        # An interrupt during the context send closes the socket, which
        # surfaces as a send error; swallow it only when the interrupt
        # caused it.
        if watcher is None or not watcher.triggered:
            raise
        return
    with resp_cm as resp:
        if watcher is not None:
            watcher.attach_socket(_response_socket(resp))
        try:
            for raw in resp:
                if watcher is not None and watcher.triggered:
                    break
                parsed = parse_sse_line(raw.decode("utf-8"))
                if parsed is None:
                    continue
                if parsed == "[DONE]":
                    break
                if "usage" in parsed:
                    journal._record_usage(messages, parsed["usage"], conv_id)
                    continue
                yield parsed
        except (OSError, http.client.HTTPException):
            # An interrupt closes the socket, which surfaces as a read
            # error here; swallow it only when the interrupt caused it.
            if watcher is None or not watcher.triggered:
                raise

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

def stream_once(
    messages: list,
    model: str,
    tools: list = None,
    temperature: float = 1.0,
    conv_id: int = None,
) -> tuple[dict, bool]:
    """Stream one chat turn, printing reasoning and content live.

    Returns (message, streamed) where streamed is False if no deltas
    arrived (e.g. server ignored stream mode) — caller should fall back.
    Raises StreamInterrupted (carrying the partial message) if the user
    interrupts with a double ESC.
    """
    message = {"role": "assistant"}
    started_reasoning = False
    renderer = None
    streamed = False
    watcher = lineedit.InterruptWatcher() if state.INTERRUPT_ENABLED else None
    if watcher is not None:
        watcher.start()
    spinner = ContextProgress(messages, tools)
    spinner.start()
    if watcher is not None:
        # the esc hint prints 2 lines below the progress line; tell the
        # spinner so it can clear the right line on stop
        watcher.on_hint = lambda: spinner.note_lines_below(2)
    try:
        for delta in stream_chat(
            messages,
            model,
            tools=tools,
            temperature=temperature,
            watcher=watcher,
            progress=spinner if spinner.active else None,
            conv_id=conv_id,
        ):
            spinner.stop()
            streamed = True
            reasoning = delta.get("reasoning_content")
            if reasoning:
                if not started_reasoning:
                    print(
                        state.OUTPUT_INDENT
                        + render.colorize(f"{render.icon('thinking')} thinking: ", "thinking"),
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
                    label = f"{render.icon('assistant')} assistant> "
                    print(
                        state.OUTPUT_INDENT + render.colorize(label, "assistant"),
                        end="",
                        flush=True,
                    )
                    renderer = render.MarkdownRenderer(
                        indent=render.display_width(state.OUTPUT_INDENT + label)
                    )
                renderer.write(content)
            accumulate_delta(message, delta)
    finally:
        if watcher is not None:
            watcher.stop()
        spinner.stop()
    if watcher is not None and watcher.triggered:
        if started_reasoning:
            print()
        if renderer is not None:
            renderer.flush()
            print()
        print(state.OUTPUT_INDENT + render.colorize("(interrupted)", "dim"))
        raise errors.StreamInterrupted(message)
    if started_reasoning:
        print()
    if renderer is not None:
        renderer.flush()
        print()
    return message, streamed
