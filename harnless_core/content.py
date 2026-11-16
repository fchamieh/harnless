"""What a response body says about how it is encoded — and what we send back.

Neither half of this is something urllib does for us: it advertises no
Accept-Encoding at all, and it hands back exactly the bytes a server sent. So a
server — or a proxy, or a CDN in front of one — that gzips a response would
leave every reader in the harness decoding binary garbage, and a gzipped *error*
body would quietly make the server's "the context is full" wording unreadable,
which is the one rejection the agent loop knows how to act on (see
`errors._context_overflow_reason`). This module is the only place that knows
content-codings, so each reader is one call deep instead of each holding its own
idea of what a body is.

Stdlib-only draws the line: gzip and deflate are advertised because gzip and
zlib ship with Python; br and zstd are not, because advertising a coding is
asking the server to send back something we cannot read.

Packing the other direction (`--upstream-gzip`) lives here too, with the rule
OpenCode ships with: compression is an optimization, so a body it cannot shrink
— or a zlib that fails — goes out plain rather than failing the request."""

import gzip
import zlib

from harnless_core import config

# What we advertise, and the only codings this module can read. The two lists
# are one fact: a coding is advertised because it can be decoded.
ACCEPT_ENCODING = "gzip, deflate"
DECODABLE = ("gzip", "x-gzip", "deflate")


class ContentEncodingError(ValueError):
    """A response came back in a content-coding this harness cannot read.

    A ValueError, so a reader that already treats an unreadable body as an error
    (fetch_url's tool error, `_http_error_body`'s empty body) keeps doing that
    instead of being handed compressed bytes nothing can decode."""


def _header(resp, name: str) -> str:
    """One header of a response.

    Tolerates what the tests hand in as a response — a BytesIO (no headers at
    all), or an object whose `headers` has no `get` — because a body reader that
    needs headers is the thing that would otherwise break them."""
    get = getattr(getattr(resp, "headers", None), "get", None)
    if not callable(get):
        return ""
    try:
        return get(name) or ""
    except Exception:
        return ""


def codings(resp) -> list:
    """The content-codings a response claims, in the order it lists them — which
    is the order they were applied, so the last one is the outermost. [] when it
    claims none, which is what a local llama-server does."""
    parts = [part.strip().lower() for part in _header(resp, "Content-Encoding").split(",")]
    return [part for part in parts if part and part != "identity"]


def _require(codings: list):
    """Refuse a body in a coding nothing here can read, before anything tries to
    turn those bytes into text and gets mojibake instead of an error."""
    for name in codings:
        if name not in DECODABLE:
            raise ContentEncodingError(
                f"response is '{name}'-encoded; harnless reads {', '.join(DECODABLE)}"
            )


def _decompressor(name: str, sample: bytes = b""):
    """A fresh incremental decompressor for one coding.

    A `deflate` body is either a zlib stream (first byte 0x78: compression
    method 8) or raw deflate with no header at all, and telling them apart needs
    a byte of the wire — so the sniff runs on the coding closest to the
    transport. A response coded twice is already rarer than this plans for."""
    if name in ("gzip", "x-gzip"):
        return zlib.decompressobj(zlib.MAX_WBITS | 16)
    if not sample or sample[:1] == b"\x78":
        return zlib.decompressobj()
    return zlib.decompressobj(-zlib.MAX_WBITS)


def _read(resp, limit: int = None) -> bytes:
    try:
        return resp.read(limit) if limit is not None else resp.read()
    except TypeError:  # a stand-in response whose read() takes no argument
        return resp.read()


def decode(resp, limit: int = None) -> bytes:
    """The whole response body, un-coded if it came back content-coded.

    `limit` caps what is read *off the wire* (fetch_url's ceiling); a coded body
    then decodes to no more than that. A body cut off mid-stream yields what
    arrived instead of raising — the caller already reports a truncated body, and
    the part that did arrive is usually the part worth reading. A coding nothing
    here can read raises, because decoding compressed bytes as text is exactly
    how garbage gets into a context."""
    codes = codings(resp)
    _require(codes)
    data = _read(resp, limit)
    for name in reversed(codes):  # the outermost coding is the first one off the wire
        data = _decode_whole(data, name)
    return data


def _decode_whole(data: bytes, name: str) -> bytes:
    """Inflate a whole body, tolerating one that was truncated or that a proxy
    mislabeled: what came through is returned, what did not is dropped."""
    if name in ("gzip", "x-gzip"):
        try:
            return gzip.decompress(data)  # reads a chain of gzip members, which gzip proxies do
        except (zlib.error, EOFError, OSError, IndexError):
            try:  # the tolerant pass: as far as the stream got is what we keep
                return _decompressor(name).decompress(data)
            except zlib.error:
                return b""
    # A "deflate" body is a zlib stream or raw deflate with no header at all.
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            return zlib.decompressobj(wbits).decompress(data)
        except zlib.error:
            continue
    return b""


def _chunks(resp):
    """The wire, in RESPONSE_CHUNK pieces.

    A real HTTPResponse supports read(size); a stand-in may only be iterable, or
    have a read() that takes no argument, and both are answers here."""
    read = getattr(resp, "read", None)
    if not callable(read):
        yield from resp
        return
    while True:
        try:
            chunk = read(config.RESPONSE_CHUNK)
        except TypeError:
            chunk = read()
        if not chunk:
            return
        yield chunk


def iter_lines(resp):
    """The response body as lines: how `stream_chat` reads SSE.

    An uncoded body is iterated exactly as `for raw in resp` was, so the plain
    path is untouched — including an interrupt closing the socket mid-read and
    surfacing as the send/read error `stream_chat` knows how to swallow. A coded
    one is inflated chunk by chunk and cut on b"\\n": feeding the decompressor as
    bytes arrive is what keeps a compressed stream streaming, where wrapping the
    response in gzip.GzipFile would hold tokens back."""
    codes = codings(resp)
    _require(codes)
    if not codes:
        yield from resp
        return
    chain = None
    pending = b""
    for wire in _chunks(resp):
        if chain is None:
            chain = [
                _decompressor(name, wire if depth == 0 else b"")
                for depth, name in enumerate(reversed(codes))
            ]
        data = wire
        for obj in chain:
            data = obj.decompress(data)
        pending += data
        while True:
            line, sep, rest = pending.partition(b"\n")
            if not sep:
                break
            pending = rest
            yield line + sep
    if pending:
        yield pending


def compress(payload: bytes) -> bytes | None:
    """gzip for a request body the caller asked us to compress, or None when
    plain is the better answer.

    gzip carries a header, a footer and a CRC, so a small body compresses to
    something *larger* than it started as — and a body that grows is not an
    optimization. A zlib that fails is not a request that fails: plain JSON."""
    if not payload:
        return None
    try:
        packed = gzip.compress(payload, config.GZIP_LEVEL)
    except Exception:
        return None
    return packed if len(packed) < len(payload) else None
