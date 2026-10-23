import urllib.request
"""The MCP client: JSON-RPC over stdio or HTTP, tool discovery, and how a server's
tools join the harness's own tool tables."""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import urllib

from harnless_core import caps, config, render, state


class MCPError(Exception):
    pass

def _mcp_content_to_text(result: dict) -> str:
    """Convert an MCP tools/call result to a string for the tool-result channel."""
    parts = []
    for item in result.get("content") or []:
        t = item.get("type")
        if t == "text":
            parts.append(item.get("text", ""))
        elif t == "image":
            parts.append(f"[image: {item.get('mimeType', 'unknown')}]")
        elif t == "resource":
            parts.append(f"[resource: {(item.get('resource') or {}).get('uri', '')}]")
        elif t == "resource_link":
            parts.append(f"[resource link: {item.get('uri', '')}]")
        else:
            parts.append(json.dumps(item))
    text = "\n".join(p for p in parts if p)
    structured = result.get("structuredContent")
    if structured is not None:
        text = (text + "\n" if text else "") + "structured: " + json.dumps(structured)
    if not text:
        text = "(no content)"
    if result.get("isError"):
        text = "error: " + text
    return caps._truncate(
        text,
        config.MCP_RESULT_LIMIT,
        unit="chars",
        hint="the MCP server returned more than the harness keeps; ask it for a narrower result",
    )

def _expand_env(value):
    """Expand ${VAR} references in a string using os.environ; missing vars left as-is."""
    if not isinstance(value, str):
        return value
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )

def load_mcp_config(path: str) -> dict:
    """Load an MCP config file. Accepts {"mcpServers": {...}} or a bare {...}.

    Returns a dict of server name -> config, with ${VAR} expansion applied to
    command/args/env/headers/url.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
    out = {}
    for name, cfg in servers.items():
        cfg = dict(cfg)
        if "command" in cfg:
            cfg["command"] = _expand_env(cfg["command"])
        if "args" in cfg:
            cfg["args"] = [_expand_env(a) for a in cfg["args"]]
        if "env" in cfg:
            cfg["env"] = {k: _expand_env(v) for k, v in cfg["env"].items()}
        if "headers" in cfg:
            cfg["headers"] = {k: _expand_env(v) for k, v in cfg["headers"].items()}
        if "url" in cfg:
            cfg["url"] = _expand_env(cfg["url"])
        out[name] = cfg
    return out

def _load_mcp_config_into(mcp_servers: dict, path: str) -> None:
    """Load one MCP config file into mcp_servers (later definitions win)."""
    try:
        loaded = load_mcp_config(path)
    except Exception as e:
        print(render.colorize(f"{render.icon('error')} failed to load mcp config {path}: {e}", "error"))
        return
    for name, cfg in loaded.items():
        if name in mcp_servers:
            print(
                render.colorize(
                    f"{render.icon('error')} duplicate mcp server name '{name}'; later definition wins",
                    "error",
                )
            )
        mcp_servers[name] = cfg

def _parse_mcp_stdio(spec: str):
    """Parse 'name:command args...' into (name, config)."""
    if ":" not in spec:
        raise MCPError(f"invalid --mcp-stdio '{spec}'; expected name:command args...")
    name, rest = spec.split(":", 1)
    parts = rest.split()
    if not name or not parts:
        raise MCPError(f"invalid --mcp-stdio '{spec}'; expected name:command args...")
    return name, {"transport": "stdio", "command": parts[0], "args": parts[1:]}

def _parse_mcp_http(spec: str):
    """Parse 'name=url' into (name, config)."""
    if "=" not in spec:
        raise MCPError(f"invalid --mcp-http '{spec}'; expected name=url")
    name, url = spec.split("=", 1)
    if not name or not url:
        raise MCPError(f"invalid --mcp-http '{spec}'; expected name=url")
    return name, {"transport": "http", "url": url}

class MCPClient:
    """A minimal MCP client over stdio or Streamable HTTP (tools only)."""

    def __init__(self, name: str, cfg: dict):
        # `cfg`, not `config`: this file refers to the config module by that name, and a
        # parameter called config would hide it inside this method.
        self.name = name
        self.config = cfg
        self.transport = cfg.get("transport", "stdio")
        self.tool_names = []
        self._id = 0
        self._proc: subprocess.Popen | None = None
        self._out_q: queue.Queue | None = None
        self._stderr_buf = []
        self._session_id = None
        self._url: str | None = None
        self._headers = {}

    # -- lifecycle
    def connect(self) -> dict:
        if self.transport == "stdio":
            self._connect_stdio()
        elif self.transport == "http":
            self._connect_http()
        else:
            raise MCPError(f"unknown transport: {self.transport}")
        info = self._request(
            "initialize",
            {
                "protocolVersion": config.MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "harnless", "version": config.VERSION},
            },
        )
        self._notify("notifications/initialized", {})
        return info

    def close(self):
        if self.transport == "stdio" and self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    # -- stdio transport
    def _connect_stdio(self):
        command = self.config.get("command")
        if not command:
            raise MCPError("stdio server requires 'command'")
        args = [command] + list(self.config.get("args") or [])
        env = os.environ.copy()
        for k, v in (self.config.get("env") or {}).items():
            env[k] = v
        use_shell = False  # `shell` is the shell module's alias in this file
        if os.name == "nt":
            # Windows: PATH entries like npx/npm/uvx are .cmd shims, which
            # CreateProcess (shell=False) cannot launch (WinError 2) — route
            # them through cmd.exe. list2cmdline quotes the args, so this is
            # safe even with spaces in arguments.
            resolved = shutil.which(command)
            if resolved and resolved.lower().endswith((".cmd", ".bat")):
                use_shell = True
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=state.CWD,
            env=env,
            text=True,
            shell=use_shell,
        )
        self._out_q = queue.Queue()
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self):
        if self._proc is None or self._out_q is None:
            return
        stdout = self._proc.stdout
        if stdout is None:
            return
        try:
            for line in stdout:
                self._out_q.put(line)
        except Exception:
            pass
        self._out_q.put(None)  # sentinel on EOF

    def _drain_stderr(self):
        if self._proc is None:
            return
        stderr = self._proc.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                self._stderr_buf.append(line.rstrip("\n"))
                if len(self._stderr_buf) > 500:
                    self._stderr_buf.pop(0)
        except Exception:
            pass

    def _send_stdio(self, msg: dict):
        if self._proc is None or self._proc.poll() is not None:
            raise MCPError(f"stdio server '{self.name}' is not running")
        stdin = self._proc.stdin
        if stdin is None:
            raise MCPError(f"stdio server '{self.name}' has no stdin")
        stdin.write(json.dumps(msg) + "\n")
        stdin.flush()

    def _recv_stdio(self) -> dict:
        if self._out_q is None:
            raise MCPError(f"stdio server '{self.name}' is not connected")
        try:
            line = self._out_q.get(timeout=config.MCP_TIMEOUT)
        except queue.Empty:
            raise MCPError(f"stdio server '{self.name}' timed out after {config.MCP_TIMEOUT}s")
        if line is None:
            raise MCPError(f"stdio server '{self.name}' closed the connection")
        line = line.strip()
        if not line:
            return self._recv_stdio()
        return json.loads(line)

    # -- http transport
    def _connect_http(self):
        url = self.config.get("url")
        if not url:
            raise MCPError("http server requires 'url'")
        self._url = url
        self._headers = dict(self.config.get("headers") or {})

    def _http_post(self, payload: dict):
        if self._url is None:
            raise MCPError(f"http server '{self.name}' is not connected")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=config.MCP_TIMEOUT) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
        return ctype, body

    def _parse_sse_response(self, body: str, want_id):
        last = None
        for raw in body.splitlines():
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if want_id is not None and obj.get("id") != want_id:
                continue
            last = obj
        if last is None:
            raise MCPError("no JSON-RPC response found in SSE stream")
        return last

    def _http_request(self, payload: dict) -> dict:
        ctype, body = self._http_post(payload)
        if not body.strip():
            raise MCPError("empty response from http server")
        if "text/event-stream" in ctype:
            return self._parse_sse_response(body, payload.get("id"))
        return json.loads(body)

    # -- JSON-RPC
    def _next_id(self):
        self._id += 1
        return self._id

    def _request(self, method: str, params: dict) -> dict:
        rid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        if self.transport == "stdio":
            self._send_stdio(msg)
            while True:
                resp = self._recv_stdio()
                if resp.get("id") == rid:
                    break
        else:
            resp = self._http_request(msg)
        if "error" in resp:
            err = resp["error"]
            raise MCPError(f"{method} failed: {err.get('message', err)}")
        return resp.get("result", {})

    def _notify(self, method: str, params: dict):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.transport == "stdio":
            self._send_stdio(msg)
        else:
            self._http_post(msg)

    # -- tools
    def list_tools(self) -> list:
        tools = []
        cursor = None
        while True:
            params = {}
            if cursor:
                params["cursor"] = cursor
            result = self._request("tools/list", params)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, args: dict) -> str:
        result = self._request("tools/call", {"name": name, "arguments": args})
        return _mcp_content_to_text(result)

def _register_mcp_client(client, taken=()) -> bool:
    """Connect one MCP client and append its tools to MCP_TOOLS/MCP_DISPATCH.

    `taken` is the set of tool names the caller has already claimed. This module does not
    look at the harness's own tool tables — the agent layer knows those, and hands the
    names down (see _builtin_tool_names). A name already taken by another MCP server is
    caught here, because that registration is ours.

    Returns True if the server connected (its tools, if any, are registered),
    False if the connection failed.
    """
    state.MCP_CLIENTS.append(client)
    try:
        client.connect()
    except Exception as e:
        print(
            render.colorize(
                f"{render.icon('error')} mcp server '{client.name}' failed to connect: {e}",
                "error",
            )
        )
        return False
    try:
        tools = client.list_tools()
    except Exception as e:
        print(
            render.colorize(
                f"{render.icon('error')} mcp server '{client.name}' tools/list failed: {e}",
                "error",
            )
        )
        client.close()
        return True
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        if name in taken or name in state.MCP_DISPATCH:
            print(
                render.colorize(
                    f"{render.icon('error')} mcp tool '{name}' (server '{client.name}') "
                    f"collides with an existing tool; skipped",
                    "error",
                )
            )
            continue
        spec = {
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description") or "",
                "parameters": t.get("inputSchema")
                or {"type": "object", "properties": {}},
            },
        }
        state.MCP_TOOLS.append(spec)
        state.MCP_DISPATCH[name] = (client, name)
        client.tool_names.append(name)
    return True

def register_mcp_tools(clients: list, taken=()) -> None:
    """Connect to MCP clients and register their tools into MCP_TOOLS/MCP_DISPATCH.

    Tool names are used as-is. A name in `taken` (the caller's built-ins) or already
    registered by an earlier server is skipped — built-ins win, then first server.
    """
    state.MCP_TOOLS = []
    state.MCP_DISPATCH = {}
    state.MCP_CLIENTS = []
    for client in clients:
        _register_mcp_client(client, taken)

def _enable_pending_mcp(name: str, taken=()) -> tuple:
    """Connect a pending (enabled:false) MCP server and register its tools.

    Returns (ok, message). On failure the server stays pending so the user can
    retry from /tools.
    """
    for i, (n, cfg) in enumerate(state.PENDING_MCP):
        if n == name:
            client = MCPClient(n, cfg)
            ok = _register_mcp_client(client, taken)
            if ok:
                state.PENDING_MCP.pop(i)
                return True, f"{render.icon('ok')} mcp server '{n}' enabled ({len(client.tool_names)} tools)"
            return False, f"{render.icon('error')} mcp server '{n}' failed to connect"
    return False, f"{render.icon('error')} mcp server '{name}' is not pending"

def _mcp_server_tools(server: str) -> list:
    """Sorted names of the tools registered from the given MCP server."""
    return sorted(
        name for name, (client, _) in state.MCP_DISPATCH.items() if client.name == server
    )
