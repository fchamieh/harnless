"""Tests for harnless.py. Run from the repo root: python tests.py"""

import base64
import contextlib
import email.message
import io
import json
import os
import shutil
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from urllib.request import pathname2url

import harnless as h


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = "./_test_tmp"
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, rel: str) -> str:
        return os.path.realpath(os.path.join(self.tmp, rel))

    def w(self, rel: str, content: str):
        return h.tool_write_file({"path": os.path.join(self.tmp, rel).replace("\\", "/"), "content": content})

    def r(self, rel: str, **kw) -> str:
        args = {"path": os.path.join(self.tmp, rel).replace("\\", "/"), "line_numbers": False}
        args.update(kw)
        return h.tool_read_file(args)


class TestSafeResolve(Base):
    def test_normal_resolution(self):
        root_p = os.path.realpath(os.path.join(os.getcwd(), "a", "b.txt"))
        self.assertEqual(h.safe_resolve("./a/b.txt"), root_p)
        self.assertEqual(h.safe_resolve("a/b.txt"), root_p)

    def test_absolute_rejected(self):
        with self.assertRaises(ValueError):
            h.safe_resolve(os.sep + "abs")

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(ValueError):
            h.safe_resolve("../evil.txt")
        with self.assertRaises(ValueError):
            h.safe_resolve("..\\evil.txt")


class TestReadFile(Base):
    def setUp(self):
        super().setUp()
        self.w("t.txt", "a\nb\nc\nd\n")

    def test_read_all_numbered(self):
        args = {"path": f"{self.tmp}/t.txt"}
        self.assertEqual(h.tool_read_file(args), "1: a\n2: b\n3: c\n4: d")

    def test_read_no_numbers(self):
        self.assertEqual(self.r("t.txt"), "a\nb\nc\nd")

    def test_read_range(self):
        self.assertEqual(self.r("t.txt", offset=2, lines=2, line_numbers=True), "2: b\n3: c\n[lines 2-3 of 4]")

    def test_read_beyond_eof(self):
        self.assertEqual(self.r("t.txt", offset=9), "(empty)")

    def test_read_bad_offset(self):
        self.assertEqual(self.r("t.txt", offset=0), "error: offset must be >= 1")

    def test_read_bad_lines(self):
        self.assertEqual(self.r("t.txt", lines=-1), "error: lines must be >= 0")

    def test_read_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            self.r("nope.txt")

    def test_read_empty_file(self):
        self.w("e.txt", "")
        self.assertEqual(self.r("e.txt"), "(empty)")

    def test_read_truncation(self):
        self.w("big.txt", "a" * 60_000 + "\n")
        out = self.r("big.txt")
        self.assertTrue(out.startswith("a" * 50_000))
        self.assertTrue(out.endswith("\n... [truncated]"))

    def test_read_no_trailing_newline(self):
        self.w("nt.txt", "a\nb")
        self.assertEqual(self.r("nt.txt"), "a\nb")


class TestWriteFile(Base):
    def test_full_overwrite(self):
        msg = h.tool_write_file({"path": f"{self.tmp}/t.txt", "content": "a\nb\nc\nd\n"})
        self.assertEqual(msg, f"wrote 8 chars to {self.p('t.txt')}")

    def test_creates_parent_dirs(self):
        self.w("deep/nested/f.txt", "hi")
        self.assertEqual(self.r("deep/nested/f.txt"), "hi")

    def test_empty_content(self):
        msg = self.w("e.txt", "")
        self.assertTrue(msg.startswith("wrote 0 chars to "))
        self.assertTrue(os.path.exists(self.p("e.txt")))

    def test_insert_before_line_2(self):
        self.w("t.txt", "a\nb\nc\nd\n")
        msg = h.tool_write_file({"path": f"{self.tmp}/t.txt", "content": "x", "offset": 2, "lines": 0})
        self.assertEqual(msg, f"inserted 1 line(s) before line 2 in {self.p('t.txt')}")
        self.assertEqual(self.r("t.txt"), "a\nx\nb\nc\nd")

    def test_insert_at_line_1(self):
        self.w("t.txt", "b\n")
        self.w("t.txt", "a")
        self.assertEqual(self.r("t.txt"), "a")
        h.tool_write_file({"path": f"{self.tmp}/t.txt", "content": "z", "offset": 1, "lines": 0})
        self.assertEqual(self.r("t.txt"), "z\na")

    def test_replace_range(self):
        self.w("t.txt", "a\nx\nb\nc\nd\n")
        msg = h.tool_write_file({"path": f"{self.tmp}/t.txt", "content": "q\nr", "offset": 3, "lines": 2})
        self.assertEqual(msg, f"replaced lines 3-4 of {self.p('t.txt')} with 2 line(s)")
        self.assertEqual(self.r("t.txt"), "a\nx\nq\nr\nd")

    def test_append_at_eof(self):
        self.w("t.txt", "a\nb\nc\n")
        h.tool_write_file({"path": f"{self.tmp}/t.txt", "content": "end", "offset": 4})
        self.assertEqual(self.r("t.txt"), "a\nb\nc\nend")

    def test_offset_beyond_end(self):
        self.w("t.txt", "a\nb\n")
        self.assertEqual(
            h.tool_write_file({"path": f"{self.tmp}/t.txt", "content": "z", "offset": 99}),
            "error: offset 99 is beyond end of file (2 lines)")

    def test_offset_zero(self):
        self.w("t.txt", "a\n")
        self.assertEqual(
            h.tool_write_file({"path": f"{self.tmp}/t.txt", "content": "z", "offset": 0}),
            "error: offset must be >= 1 and lines must be >= 0")

    def test_missing_file_with_offset(self):
        self.assertEqual(
            h.tool_write_file({"path": f"{self.tmp}/nope.txt", "content": "z", "offset": 1}),
            f"error: file does not exist, cannot modify line range: {self.p('nope.txt')}")


class TestPatchFile(Base):
    def patch(self, rel, old, new, **kw):
        args = {"path": os.path.join(self.tmp, rel).replace("\\", "/"), "old_string": old, "new_string": new}
        args.update(kw)
        return h.tool_patch_file(args)

    def test_simple(self):
        self.w("t.txt", "foo\nbar\n")
        self.assertEqual(self.patch("t.txt", "foo", "FOO"), f"patched {self.p('t.txt')}: replaced 1 occurrence(s)")
        self.assertEqual(self.r("t.txt"), "FOO\nbar")

    def test_scoped_patch(self):
        self.w("t.txt", "foo\nbar\nfoo\nbaz\nfoo\n")
        self.assertEqual(self.patch("t.txt", "foo", "FOO", offset=3, lines=1),
                         f"patched {self.p('t.txt')}: replaced 1 occurrence(s)")
        self.assertEqual(self.r("t.txt"), "foo\nbar\nFOO\nbaz\nfoo")

    def test_scoped_not_found(self):
        self.w("t.txt", "foo\nbar\nfoo\nbaz\nfoo\n")
        self.assertEqual(self.patch("t.txt", "bar", "BAR", offset=4, lines=1),
                         "error: old_string not found in lines 4-4")

    def test_scoped_multiple(self):
        self.w("t.txt", "foo\nfoo\nfoo\n")
        self.assertEqual(self.patch("t.txt", "foo", "X", offset=1, lines=2),
                         "error: old_string found 2 times in lines 1-2; narrow the range or add context")

    def test_scoped_to_eof(self):
        self.w("t.txt", "foo\nfoo\nfoo\n")
        self.assertEqual(self.patch("t.txt", "foo", "LAST", offset=3),
                         f"patched {self.p('t.txt')}: replaced 1 occurrence(s)")
        self.assertEqual(self.r("t.txt"), "foo\nfoo\nLAST")

    def test_unscoped_multiple_errors(self):
        self.w("t.txt", "foo\nfoo\nLAST\n")
        self.assertEqual(self.patch("t.txt", "foo", "X"),
                         "error: old_string found 2 times; provide more context, use offset/lines, or set replace_all")

    def test_replace_all(self):
        self.w("t.txt", "foo\nbar\nfoo\n")
        self.assertEqual(self.patch("t.txt", "foo", "X", replace_all=True),
                         f"patched {self.p('t.txt')}: replaced 2 occurrence(s)")
        self.assertEqual(self.r("t.txt"), "X\nbar\nX")

    def test_multiline_scoped(self):
        self.w("t.txt", "l1\nmid\nl3\nmid\nl5\n")
        self.assertEqual(self.patch("t.txt", "mid\nl3", "MID\nL3", offset=2, lines=2),
                         f"patched {self.p('t.txt')}: replaced 1 occurrence(s)")
        self.assertEqual(self.r("t.txt"), "l1\nMID\nL3\nmid\nl5")

    def test_whitespace_sensitive(self):
        self.w("t.txt", "foo\nbar\n")
        self.assertEqual(self.patch("t.txt", "foo ", "X"), "error: old_string not found in file")

    def test_whole_file(self):
        self.w("t.txt", "only")
        self.assertEqual(self.patch("t.txt", "only", "changed"),
                         f"patched {self.p('t.txt')}: replaced 1 occurrence(s)")
        self.assertEqual(self.r("t.txt"), "changed")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            self.patch("nope.txt", "a", "b")


class TestGrep(Base):
    def grep(self, pattern, **kw):
        args = {"path": self.tmp, "pattern": pattern}
        args.update(kw)
        return h.tool_grep(args)

    def test_basic(self):
        self.w("st/g1.txt", "l1\nAAA mid\nl3\n")
        self.assertEqual(self.grep("AAA"), "_test_tmp/st/g1.txt:2: AAA mid")

    def test_no_matches(self):
        self.w("st/g1.txt", "nothing here\n")
        self.assertEqual(self.grep("AAA"), "no matches")

    def test_case_insensitive(self):
        self.w("st/g1.txt", "AAA\n")
        self.assertEqual(self.grep("aaa"), "_test_tmp/st/g1.txt:1: AAA")

    def test_context(self):
        self.w("st/g1.txt", "l1\nAAA mid\nl3\nl9\nl10\nAAA end\n")
        self.assertEqual(self.grep("AAA", context=1),
                         "  _test_tmp/st/g1.txt:1: l1\n> _test_tmp/st/g1.txt:2: AAA mid\n  _test_tmp/st/g1.txt:3: l3\n"
                         "--\n  _test_tmp/st/g1.txt:5: l10\n> _test_tmp/st/g1.txt:6: AAA end")

    def test_file_pattern_regex(self):
        self.w("st/g1.txt", "AAA\n")
        self.w("st/g2.txt", "AAA\n")
        self.assertEqual(self.grep("AAA", file_pattern=".*g1.*"), "_test_tmp/st/g1.txt:1: AAA")

    def test_file_pattern_glob(self):
        self.w("st/g1.txt", "AAA\n")
        self.w("st/other.md", "AAA\n")
        self.assertEqual(self.grep("AAA", file_pattern="*.txt"), "_test_tmp/st/g1.txt:1: AAA")

    def test_file_pattern_no_match(self):
        self.w("st/g1.txt", "AAA\n")
        self.assertEqual(self.grep("AAA", file_pattern="nope.*"), "no matches")

    def test_node_modules_skipped(self):
        os.makedirs(os.path.join(self.tmp, "node_modules", "pkg"), exist_ok=True)
        with open(os.path.join(self.tmp, "node_modules", "pkg", "p.js"), "w", encoding="utf-8") as f:
            f.write("AAA\n")
        self.assertEqual(self.grep("AAA"), "no matches")

    def test_match_truncation(self):
        self.w("st/many.txt", "\n".join("AAA" for _ in range(250)))
        out = self.grep("AAA")
        self.assertTrue(out.endswith("... [truncated at 200 matches]"))


class TestGlob(Base):
    def glob(self, pattern):
        return h.tool_glob({"path": self.tmp, "pattern": pattern})

    def test_star(self):
        self.w("a.txt", "x")
        self.w("b.md", "x")
        self.assertEqual(self.glob("*.txt"), "_test_tmp/a.txt")

    def test_nested(self):
        self.w("src/deep/c.py", "x")
        self.assertEqual(self.glob("**/*.py"), "_test_tmp/src/deep/c.py")

    def test_no_match(self):
        self.w("a.txt", "x")
        self.assertEqual(self.glob("*.py"), "no files matched")

    def test_pycache_skipped(self):
        os.makedirs(os.path.join(self.tmp, "__pycache__"), exist_ok=True)
        with open(os.path.join(self.tmp, "__pycache__", "m.cpython-311.pyc"), "w") as f:
            f.write("x")
        self.assertEqual(self.glob("**/*.pyc"), "no files matched")


class TestDirOps(Base):
    def test_mkdir_nested(self):
        path = os.path.join(self.tmp, "x", "y", "z").replace("\\", "/")
        self.assertEqual(h.tool_mkdir({"path": path}), f"created directory: {self.p('x/y/z')}")

    def test_mkdir_existing_ok(self):
        os.makedirs(os.path.join(self.tmp, "x"))
        h.tool_mkdir({"path": f"{self.tmp}/x"})

    def test_list_dir(self):
        os.makedirs(os.path.join(self.tmp, "st", "sub"))
        os.makedirs(os.path.join(self.tmp, "st", "empty"))
        self.w("st/a.txt", "one")
        self.w("st/sub/b.txt", "one")
        self.assertEqual(h.tool_list_dir({"path": f"{self.tmp}/st"}), "a.txt\nempty/\nsub/")

    def test_list_dir_empty(self):
        os.makedirs(os.path.join(self.tmp, "empty"))
        self.assertEqual(h.tool_list_dir({"path": f"{self.tmp}/empty"}), "(empty)")

    def test_list_dir_missing(self):
        self.assertEqual(h.tool_list_dir({"path": f"{self.tmp}/missing"}), "error: not a directory: " + f"{self.tmp}/missing")

    def test_list_dir_truncation(self):
        for i in range(502):
            with open(os.path.join(self.tmp, f"f{i:03d}.txt"), "w") as f:
                f.write("x")
        out = h.tool_list_dir({"path": self.tmp})
        self.assertEqual(len(out.split("\n")), 501)
        self.assertTrue(out.endswith("... [truncated at 500 entries]"))

    def test_copy(self):
        self.w("st/a.txt", "one\ntwo\n")
        self.assertEqual(h.tool_copy_file({"src": f"{self.tmp}/st/a.txt", "dst": f"{self.tmp}/st/c.txt"}),
                         f"copied {self.p('st/a.txt')} -> {self.p('st/c.txt')}")
        self.assertEqual(self.r("st/c.txt"), "one\ntwo")

    def test_move_creates_parents(self):
        self.w("st/a.txt", "one")
        self.assertEqual(h.tool_move_file({"src": f"{self.tmp}/st/a.txt", "dst": f"{self.tmp}/st/new/d.txt"}),
                         f"moved {self.p('st/a.txt')} -> {self.p('st/new/d.txt')}")
        self.assertEqual(self.r("st/new/d.txt"), "one")

    def test_delete(self):
        self.w("st/a.txt", "one")
        self.assertEqual(h.tool_delete_file({"path": f"{self.tmp}/st/a.txt"}), f"deleted {self.p('st/a.txt')}")
        self.assertFalse(os.path.exists(self.p("st/a.txt")))

    def test_delete_missing(self):
        self.assertEqual(h.tool_delete_file({"path": f"{self.tmp}/nope.txt"}), f"error: file does not exist: {self.tmp}/nope.txt")

    def test_delete_dir_rejected(self):
        os.makedirs(os.path.join(self.tmp, "d"))
        self.assertEqual(h.tool_delete_file({"path": f"{self.tmp}/d"}), f"error: cannot delete a directory: {self.tmp}/d")

    def test_copy_missing(self):
        self.assertEqual(h.tool_copy_file({"src": f"{self.tmp}/nope.txt", "dst": f"{self.tmp}/x.txt"}),
                         f"error: source does not exist: {self.tmp}/nope.txt")


class TestRunShell(Base):
    def sh(self, command, **kw):
        args = {"command": command}
        args.update(kw)
        return h.tool_run_shell(args)

    def test_success(self):
        out = self.sh("python -c \"print('hello')\"")
        self.assertEqual(out, "exit code: 0\nhello")

    def test_nonzero_exit_with_stderr(self):
        out = self.sh("python -c \"import sys; sys.stderr.write('boom'); sys.exit(3)\"")
        self.assertEqual(out, "exit code: 3\n[stderr]\nboom")

    def test_empty_command(self):
        self.assertEqual(self.sh(""), "error: empty command")
        self.assertEqual(self.sh("   "), "error: empty command")

    def test_output_truncation(self):
        r = self.sh("python -c \"import sys; sys.stdout.write('x'*30000)\"")
        self.assertTrue(r.startswith("exit code: 0\n" + "x" * 20_000 + "\n... [truncated, 30000 chars total]"))

    def test_timeout(self):
        out = self.sh("python -c \"import time; time.sleep(2)\"", timeout=1)
        self.assertEqual(out, "error: command timed out after 1s")


class TestFetchUrl(unittest.TestCase):
    class _Resp:
        def __init__(self, body, charset="utf-8"):
            self._data = body if isinstance(body, bytes) else body.encode("utf-8")
            self._charset = charset

            class _Headers:
                def get_content_charset(_self):
                    return charset

            self.headers = _Headers()

        def read(self, n=-1):
            return self._data if n is None or n < 0 else self._data[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fetch(self, body, charset="utf-8", **args):
        a = {"url": "https://example.com/page"}
        a.update(args)
        with mock.patch("urllib.request.urlopen", return_value=self._Resp(body, charset)):
            return h.tool_fetch_url(a)

    def test_basic_fetch(self):
        self.assertEqual(self._fetch("hello world"), "hello world")

    def test_raw_html_kept_by_default(self):
        self.assertEqual(self._fetch("<p>hi</p>"), "<p>hi</p>")

    def test_strip_html(self):
        out = self._fetch("<h1>Title</h1><p>Some <b>bold</b> text</p>", strip_html=True)
        self.assertEqual(out, "Title\n\nSome bold text")

    def test_strip_html_skips_script_and_style(self):
        out = self._fetch(
            "<style>x{}</style><script>evil()</script><p>ok</p>", strip_html=True
        )
        self.assertEqual(out, "ok")

    def test_entities_decoded_when_stripping(self):
        self.assertEqual(self._fetch("<p>a &amp; b</p>", strip_html=True), "a & b")

    def test_invalid_scheme(self):
        self.assertEqual(
            h.tool_fetch_url({"url": "ftp://x"}), "error: url must be an http:// or https:// URL"
        )
        self.assertEqual(
            h.tool_fetch_url({"url": "example.com"}), "error: url must be an http:// or https:// URL"
        )

    def test_http_error(self):
        err = urllib.error.HTTPError("https://x", 404, "Not Found", email.message.Message(), None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            out = h.tool_fetch_url({"url": "https://x"})
        self.assertEqual(out, "error: HTTP 404 Not Found for https://x")

    def test_url_error(self):
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("down")
        ):
            out = h.tool_fetch_url({"url": "https://x"})
        self.assertTrue(out.startswith("error: could not fetch https://x:"))

    def test_bad_timeout(self):
        self.assertEqual(h.tool_fetch_url({"url": "https://x", "timeout": 0}), "error: timeout must be > 0")
        self.assertTrue(h.tool_fetch_url({"url": "https://x", "timeout": "abc"}).startswith("error:"))

    def test_unknown_charset_falls_back_to_utf8(self):
        self.assertEqual(self._fetch("hi", charset="no-such-codec"), "hi")

    def test_truncation(self):
        out = self._fetch("x" * 60_000)
        self.assertTrue(out.endswith("\n... [truncated]"))
        self.assertEqual(len(out), 50_000 + len("\n... [truncated]"))

    def test_empty(self):
        self.assertEqual(self._fetch(""), "(empty)")

    def test_registered(self):
        all_names = [s["function"]["name"] for s in h.OPENAI_TOOLS]
        interactive_names = [s["function"]["name"] for s in h.OPENAI_TOOLS_INTERACTIVE]
        self.assertIn("fetch_url", all_names)
        self.assertIn("fetch_url", interactive_names)
        self.assertIn("fetch_url", h.DISPATCH)


class TestColorize(unittest.TestCase):
    def test_disabled_returns_plain(self):
        h.set_color_enabled(False)
        self.addCleanup(h.set_color_enabled, True)
        self.assertEqual(h.colorize("hello", "tool"), "hello")
        self.assertEqual(h.colorize("hello", None), "hello")

    def test_enabled_wraps_ansi(self):
        h.set_color_enabled(True)
        self.addCleanup(h.set_color_enabled, True)
        self.assertEqual(h.colorize("hello", "tool"), f"{h.ANSI['tool']}hello{h.ANSI['reset']}")
        self.assertEqual(h.colorize("hello"), "hello")

    def test_no_color_env_disables(self):
        old = os.environ.get("NO_COLOR")
        os.environ["NO_COLOR"] = "1"
        try:
            self.assertFalse(h.color_enabled())
        finally:
            if old is None:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = old


class TestIcon(unittest.TestCase):
    def test_emoji_default(self):
        old = h.EMOJI_ENABLED
        h.set_emoji_enabled(True)
        self.addCleanup(h.set_emoji_enabled, old)
        self.assertEqual(h.icon("thinking"), h.ICONS["thinking"])
        self.assertEqual(h.icon("tool"), h.ICONS["tool"])

    def test_ascii_fallback(self):
        old = h.EMOJI_ENABLED
        h.set_emoji_enabled(False)
        self.addCleanup(h.set_emoji_enabled, old)
        self.assertEqual(h.icon("thinking"), h.ASCII_ICONS["thinking"])
        self.assertEqual(h.icon("assistant"), h.ASCII_ICONS["assistant"])

    def test_unknown_key(self):
        old = h.EMOJI_ENABLED
        h.set_emoji_enabled(True)
        self.addCleanup(h.set_emoji_enabled, old)
        self.assertEqual(h.icon("nope"), "")


class TestEmojiAutoDisable(unittest.TestCase):
    class _FakeStdout:
        def __init__(self, enc):
            self.encoding = enc

    def _check(self, enc, expected):
        old = sys.stdout
        sys.stdout = self._FakeStdout(enc)
        self.addCleanup(setattr, sys, "stdout", old)
        self.assertEqual(h._stdout_can_encode_emoji(), expected)

    def test_utf8_allows_emoji(self):
        self._check("utf-8", True)

    def test_cp1256_disables_emoji(self):
        self._check("cp1256", False)

    def test_ascii_disables_emoji(self):
        self._check("ascii", False)

    def test_missing_encoding_disables_emoji(self):
        self._check(None, False)

    def test_unknown_encoding_disables_emoji(self):
        self._check("not-a-real-codec", False)


class TestMarkdownRenderer(unittest.TestCase):
    def _render(self, text, chunks=None, colors=True, indent=0):
        old = h.COLORS_ENABLED
        h.set_color_enabled(colors)
        self.addCleanup(h.set_color_enabled, old)
        out = io.StringIO()
        r = h.MarkdownRenderer(out=out, indent=indent)
        if chunks is None:
            r.write(text)
        else:
            for c in chunks:
                r.write(c)
        r.flush()
        return out.getvalue()

    def test_plain_text(self):
        self.assertEqual(self._render("hello world\n"), "hello world\n")

    def test_heading(self):
        out = self._render("# Title\n")
        self.assertNotIn("#", out)
        self.assertIn(h.ANSI["heading"] + "Title" + h.ANSI["reset"], out)

    def test_bold(self):
        out = self._render("a **bold** b\n")
        self.assertIn(h.ANSI["bold"] + "bold" + h.ANSI["reset"], out)
        self.assertNotIn("**", out)

    def test_italic(self):
        out = self._render("a *it* b\n")
        self.assertIn(h.ANSI["italic"] + "it" + h.ANSI["reset"], out)

    def test_inline_code(self):
        out = self._render("use `patch_file` now\n")
        self.assertIn(h.ANSI["code"] + "patch_file" + h.ANSI["reset"], out)

    def test_code_span_protects_markers(self):
        out = self._render("`**not bold**`\n")
        self.assertNotIn(h.ANSI["bold"], out)
        self.assertIn(h.ANSI["code"] + "**not bold**" + h.ANSI["reset"], out)

    def test_code_block(self):
        out = self._render("```python\nx = 1\n```\n")
        self.assertIn("```python", out)
        self.assertIn(h.ANSI["codeblock"] + "x = 1" + h.ANSI["reset"], out)

    def test_code_block_no_inline_styling(self):
        out = self._render("```\n**not bold**\n```\n")
        self.assertNotIn(h.ANSI["bold"], out)

    def test_unordered_list(self):
        out = self._render("- item\n")
        self.assertIn(h.ANSI["bullet"] + "• " + h.ANSI["reset"], out)
        self.assertIn("item", out)

    def test_ordered_list(self):
        out = self._render("1. first\n")
        self.assertIn(h.ANSI["bullet"] + "1. " + h.ANSI["reset"], out)

    def test_blockquote(self):
        out = self._render("> quoted\n")
        self.assertIn(h.ANSI["quote"], out)
        self.assertIn("quoted", out)

    def test_horizontal_rule(self):
        out = self._render("---\n")
        self.assertIn(h.ANSI["dim"], out)
        self.assertNotIn("---", out)

    def test_link(self):
        out = self._render("see [docs](http://x.y) now\n")
        self.assertIn(h.ANSI["underline"] + "docs" + h.ANSI["reset"], out)
        self.assertIn(h.ANSI["dim"] + " (http://x.y)" + h.ANSI["reset"], out)

    def test_link_destination_is_literal(self):
        url = "https://example.com/a*b*c?q=**x**&file=a_b"
        self.assertEqual(self._plain(f"[link]({url})"), f"link ({url})\n")
        out = self._render(f"[**bold** `code`]({url})")
        self.assertIn(h.ANSI["bold"] + "bold", out)
        self.assertIn(h.ANSI["code"] + "code", out)
        self.assertIn(url, out)

    def test_escaped_inline_markers(self):
        self.assertEqual(self._plain(r"\*literal\* \`code\` \[link](url)"),
                         "*literal* `code` [link](url)\n")
        self.assertEqual(self._plain(r"**bold \* literal**"), "bold * literal\n")
        self.assertEqual(self._plain(r"a\qb \\"), "a\\qb \\\n")

    def test_variable_length_code_span(self):
        out = self._render("`` `x` **literal** ``")
        self.assertIn(h.ANSI["code"] + "`x` **literal**" + h.ANSI["reset"], out)
        self.assertNotIn(h.ANSI["bold"], out)

    def test_fence_length_type_and_closing_suffix(self):
        for opening, closing in (("````python", "````"), ("~~~~python", "~~~~~")):
            with self.subTest(opening=opening):
                doc = opening + "\n```\n~~~\n````suffix\n**literal**\n" + closing + "\n**bold**\n"
                out = self._render(doc)
                self.assertIn(h.ANSI["codeblock"] + "**literal**", out)
                self.assertIn(h.ANSI["bold"] + "bold", out)
                self.assertEqual(self._render(doc, chunks=list(doc)), out)

    def test_code_fence_continuation_indent(self):
        self.assertEqual(self._plain("Intro\n```py\nx\n```\n", indent=4),
                         "Intro\n    ```py\n      x\n    ```\n")

    def test_table_separator_count_mismatch(self):
        for sep in ("|---|", "|---|---|---|"):
            out = self._plain("| A | B |\n" + sep + "\n")
            self.assertNotIn("┌", out)
            self.assertIn("| A | B |", out)

    def test_table_followed_by_fence_with_pipe(self):
        out = self._render("| A |\n|---|\n```a|b\n**literal**\n```\n")
        self.assertIn(h.ANSI["codeblock"] + "**literal**", out)
        self.assertIn("└", out)

    def test_combining_character_width(self):
        self.assertEqual(h.display_width("e\u0301"), 1)
        self.assertEqual(h.display_width("\u200d\ufe0f"), 0)
        out = self._plain("| H |\n|---|\n| e\u0301 |\n| abc |\n")
        self.assertEqual(len({h.display_width(line) for line in out.splitlines()}), 1)
        self.assertEqual(h._truncate_visible("e\u0301abcd", 3), "e\u0301a…")

    def test_colors_disabled_passthrough(self):
        doc = "# T\n**b** `c` *i*\n- x\n> q\n---\n```py\ncode\n```\n[l](http://u)\n"
        self.assertEqual(self._render(doc, colors=False), doc)

    def test_flush_emits_partial_line(self):
        self.assertEqual(self._render("no newline", chunks=["no newline"]), "no newline\n")

    def test_open_fence_at_eof(self):
        out = self._render("```python\ncode", chunks=["```python\ncode"])
        self.assertIn(h.ANSI["codeblock"] + "code" + h.ANSI["reset"], out)

    def test_continuation_indent(self):
        self.assertEqual(self._render("line one\nline two\n", indent=4), "line one\n    line two\n")

    # ------------------------------------------------------------ tables

    def _plain(self, text, **kw):
        return h._ANSI_RE.sub("", self._render(text, **kw))

    def test_table_basic(self):
        out = self._plain("| A | B |\n|---|---|\n| 1 | 2 |\n")
        self.assertNotIn("| A | B |", out)  # raw row is gone
        self.assertIn("┌", out)
        self.assertIn("┴", out)
        self.assertIn("A", out)
        self.assertIn("2", out)
        # header separator row present
        self.assertIn("├", out)

    def test_table_columns_aligned(self):
        out = self._plain("| A | Longer |\n|---|---|\n| 1 | 2 |\n")
        lines = [l for l in out.splitlines() if l.strip()]
        # every line of the table must be the same display width
        widths = {h.display_width(l) for l in lines}
        self.assertEqual(len(widths), 1, out)

    def test_table_alignment_markers(self):
        out = self._plain("| L | C | R |\n|:--|:-:|--:|\n| 1 | 1 | 1 |\n")
        body = [l for l in out.splitlines() if l.strip()][3]
        # drop the one-space row padding around each cell
        cells = [c[1:-1] for c in body.split("│") if c]
        self.assertEqual(cells[0], "1  ")  # left: trailing pad only
        self.assertEqual(cells[1], " 1 ")  # center: padded both sides
        self.assertEqual(cells[2], "  1")  # right: leading pad only

    def test_table_indent_applied(self):
        out = self._render("Intro\n\n| A |\n|---|\n| 1 |\n", indent=4)
        table = [l for l in out.splitlines() if l.strip()][1:]
        for line in table:
            self.assertTrue(line.startswith("    "), line)

    def test_table_inline_styling(self):
        out = self._render("| A |\n|---|\n| `code` **b** |\n")
        self.assertIn(h.ANSI["code"] + "code" + h.ANSI["reset"], out)
        self.assertIn(h.ANSI["bold"] + "b" + h.ANSI["reset"], out)
        self.assertNotIn("**", out)
        self.assertNotIn("`", out)

    def test_table_wide_cells(self):
        out = self._plain("| H |\n|---|\n| 🤖 |\n")
        lines = [l for l in out.splitlines() if l.strip()]
        widths = {h.display_width(l) for l in lines}
        self.assertEqual(len(widths), 1, out)

    def test_table_ragged_rows(self):
        out = self._plain("| A | B | C |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |\n")
        self.assertIn("A", out)
        self.assertIn("3", out)
        self.assertNotIn("4", out)  # extra cell dropped

    def test_table_escaped_pipe(self):
        out = self._plain("| A | B |\n|---|---|\n| x \\| y | z |\n")
        self.assertIn("x | y", out)
        self.assertIn("z", out)

    def test_table_pipe_prose_not_a_table(self):
        out = self._plain("a | b\nnot a separator\n")
        self.assertIn("a | b", out)
        self.assertIn("not a separator", out)
        self.assertNotIn("┌", out)

    def test_table_in_code_block(self):
        out = self._plain("```\n| a | b |\n|---|---|\n```\n")
        self.assertIn("| a | b |", out)
        self.assertNotIn("┌", out)

    def test_table_at_eof_without_newline(self):
        out = self._plain("| A | B |\n|---|---|\n| 1 | 2 |")
        self.assertIn("┌", out)
        self.assertIn("┴", out)

    def test_table_then_text(self):
        out = self._plain("| A | B |\n|---|---|\n| 1 | 2 |\nAfter\n")
        self.assertIn("┴", out)
        self.assertTrue(out.rstrip().endswith("After"))

    def test_table_colors_disabled_passthrough(self):
        doc = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        self.assertEqual(self._render(doc, colors=False), doc)

    def test_table_chunking_invariance(self):
        doc = (
            "Intro\n\n| A | B |\n|---|:--:|\n| 1 | 2 |\n| x \\| y | z |\n\n"
            "| C |\n|---|\n| 3 |\n\nAfter\n"
        )
        full = self._render(doc)
        for i in range(len(doc) + 1):
            out = self._render(doc, chunks=[doc[:i], doc[i:]])
            self.assertEqual(out, full, f"split at {i}")

    def test_table_width_clamp(self):
        real = h.terminal_width
        h.terminal_width = lambda: 30
        self.addCleanup(setattr, h, "terminal_width", real)
        out = self._plain(
            "| Column one | Column two | Column three |\n|---|---|---|\n"
            "| aaaaaaaaaa | bbbbbbbbbb | cccccccccc |\n"
        )
        for line in out.splitlines():
            if line.strip():
                self.assertLessEqual(h.display_width(line), 30, line)
        self.assertIn("…", out)  # truncated

    def test_table_narrow_terminal_preserves_all_cells(self):
        real = h.terminal_width
        h.terminal_width = lambda: 20
        self.addCleanup(setattr, h, "terminal_width", real)
        labels = list("ABCDEFG")
        values = [f"value{i}" for i in range(7)]
        doc = ("|" + "|".join(labels) + "|\n"
               + "|---" * 7 + "|\n"
               + "|" + "|".join(values) + "|\n")
        out = self._plain(doc)
        for label, value in zip(labels, values):
            self.assertIn(f"{label}: {value}", out)
        for line in out.splitlines():
            self.assertLessEqual(h.display_width(line), 20)
        self.assertEqual(self._plain(doc, chunks=list(doc)), out)

    def test_table_first_line_reserves_label_width(self):
        real = h.terminal_width
        h.terminal_width = lambda: 24
        self.addCleanup(setattr, h, "terminal_width", real)
        out = self._plain("| Long heading |\n|---|\n| long value |\n", indent=8)
        lines = out.splitlines()
        self.assertLessEqual(h.display_width(lines[0]) + 8, 24)
        for line in lines[1:]:
            self.assertTrue(line.startswith(" " * 8))
            self.assertLessEqual(h.display_width(line), 24)

    def test_table_extremely_narrow_terminal(self):
        real = h.terminal_width
        self.addCleanup(setattr, h, "terminal_width", real)
        for width in (1, 2, 5):
            h.terminal_width = lambda: width
            out = self._plain("| H |\n|---|\n| 界e\u0301long |\n", indent=10)
            for line in out.splitlines():
                self.assertLessEqual(h.display_width(line), width)

    def test_chunking_invariance(self):
        doc = (
            "# Title\n\n**bold** and `code` and *it*\n\n- a\n- b\n\n"
            "```python\nx = 1\n```\n\n> quote\n\n---\n\n[link](http://x)\n"
        )
        full = self._render(doc)
        for i in range(len(doc) + 1):
            out = self._render(doc, chunks=[doc[:i], doc[i:]])
            self.assertEqual(out, full, f"split at {i}")

    def test_display_width(self):
        self.assertEqual(h.display_width("abc"), 3)
        self.assertEqual(h.display_width("🤖"), 2)
        self.assertEqual(h.display_width("🤖 ab"), 5)

    def test_print_assistant(self):
        h.set_color_enabled(False)
        self.addCleanup(h.set_color_enabled, True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            h.print_assistant("# Hi\n")
        out = buf.getvalue()
        self.assertIn("assistant> ", out)
        self.assertIn("# Hi", out)


class TestLoadAgentsMd(Base):
    def setUp(self):
        super().setUp()
        self._old_cwd = h.CWD
        setattr(h, "CWD", self.p(""))

    def tearDown(self):
        setattr(h, "CWD", self._old_cwd)
        super().tearDown()

    def test_absent(self):
        self.assertEqual(h.load_agents_md(), "")

    def test_case_insensitive(self):
        with open(os.path.join(self.p(""), "agents.md"), "w", encoding="utf-8") as f:
            f.write("rules here")
        self.assertEqual(h.load_agents_md(), "rules here")

    def test_exact_name(self):
        with open(os.path.join(self.p(""), "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("rules")
        self.assertEqual(h.load_agents_md(), "rules")

    def test_truncation(self):
        with open(os.path.join(self.p(""), "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("a" * 25_000)
        out = h.load_agents_md()
        self.assertTrue(out.startswith("a" * 20_000))
        self.assertTrue(out.endswith("\n... [truncated]"))


class TestDispatch(Base):
    def test_get_cwd(self):
        self.assertEqual(h.execute_tool("get_cwd", "{}"), h.CWD)

    def test_invalid_json(self):
        self.assertEqual(h.execute_tool("read_file", "{bad"), "error: invalid JSON arguments: {bad")

    def test_unknown_tool(self):
        self.assertEqual(h.execute_tool("nope", "{}"), "error: unknown tool: nope")

    def test_exit_signal_propagates(self):
        with self.assertRaises(h.ExitSignal) as ctx:
            h.execute_tool("exit", '{"code": 5, "message": "done"}')
        self.assertEqual(ctx.exception.code, 5)
        self.assertEqual(ctx.exception.message, "done")

    def test_interactive_tools_exclude_exit(self):
        interactive_names = [s["function"]["name"] for s in h.OPENAI_TOOLS_INTERACTIVE]
        all_names = [s["function"]["name"] for s in h.OPENAI_TOOLS]
        self.assertNotIn("exit", interactive_names)
        self.assertEqual(
            interactive_names, [n for n in all_names if n != "exit"]
        )

    def test_tool_exception_caught(self):
        self.assertTrue(h.execute_tool("read_file", '{"path": "nope.txt"}').startswith("error:"))


class TestSubagents(unittest.TestCase):
    def setUp(self):
        self._old_depth = h._AGENT_DEPTH
        self._old_max = h.MAX_SUBAGENT_DEPTH
        self._old_indent = h.OUTPUT_INDENT
        self._old_model = h.MODEL
        self._old_temp = h.TEMPERATURE
        h._AGENT_DEPTH = 0
        h.MAX_SUBAGENT_DEPTH = 3
        h.OUTPUT_INDENT = ""
        h.MODEL = "test-model"
        h.TEMPERATURE = 0.2
        self.addCleanup(setattr, h, "_AGENT_DEPTH", self._old_depth)
        self.addCleanup(setattr, h, "MAX_SUBAGENT_DEPTH", self._old_max)
        self.addCleanup(setattr, h, "OUTPUT_INDENT", self._old_indent)
        self.addCleanup(setattr, h, "MODEL", self._old_model)
        self.addCleanup(setattr, h, "TEMPERATURE", self._old_temp)

    def test_task_registered_in_both_tool_lists(self):
        all_names = [s["function"]["name"] for s in h.OPENAI_TOOLS]
        interactive_names = [s["function"]["name"] for s in h.OPENAI_TOOLS_INTERACTIVE]
        self.assertIn("task", all_names)
        self.assertIn("task", interactive_names)
        self.assertIn("task", h.DISPATCH)

    def test_task_empty(self):
        self.assertEqual(h.tool_task({"task": "   "}), "error: empty task")
        self.assertEqual(h.tool_task({}), "error: empty task")

    def test_task_depth_limit(self):
        h._AGENT_DEPTH = h.MAX_SUBAGENT_DEPTH
        out = h.tool_task({"task": "do something"})
        self.assertTrue(out.startswith("error: sub-agent depth limit reached"))

    def test_task_runs_nested_agent(self):
        calls = []

        def fake_run_agent(messages, model, interactive=False, temperature=0.2, depth=0):
            calls.append({
                "messages": list(messages), "model": model,
                "interactive": interactive, "temperature": temperature, "depth": depth,
            })
            messages.append({"role": "assistant", "content": "sub summary"})
            return 0

        old = h.run_agent
        h.run_agent = fake_run_agent
        self.addCleanup(setattr, h, "run_agent", old)
        out = h.tool_task({"task": "do X"})
        self.assertEqual(out, "exit code: 0\nsub summary")
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["temperature"], 0.2)
        self.assertFalse(call["interactive"])
        self.assertEqual(call["depth"], 1)
        self.assertEqual(call["messages"][1], {"role": "user", "content": "do X"})
        self.assertIn("sub-agent", call["messages"][0]["content"])
        self.assertEqual(h.OUTPUT_INDENT, "")

    def test_task_indent_restored_on_error(self):
        def boom(messages, model, interactive=False, temperature=0.2, depth=0):
            raise RuntimeError("nope")

        old = h.run_agent
        h.run_agent = boom
        self.addCleanup(setattr, h, "run_agent", old)
        with self.assertRaises(RuntimeError):
            h.tool_task({"task": "do X"})
        self.assertEqual(h.OUTPUT_INDENT, "")

    def test_task_no_final_content(self):
        def fake_run_agent(messages, model, interactive=False, temperature=0.2, depth=0):
            messages.append({"role": "assistant", "content": ""})
            return 2

        old = h.run_agent
        h.run_agent = fake_run_agent
        self.addCleanup(setattr, h, "run_agent", old)
        self.assertEqual(h.tool_task({"task": "do X"}), "exit code: 2")

    def test_run_agent_exit_returns_code(self):
        def fake_stream_once(messages, model, interactive=False, temperature=0.2):
            return ({"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "exit", "arguments": '{"code": 7, "message": "done"}'}}]}, True)

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = h.run_agent([{"role": "user", "content": "go"}], "m")
        self.assertEqual(code, 7)
        self.assertIn("done", buf.getvalue())


class TestStatus(unittest.TestCase):
    def test_context_usage_counts_content_and_tool_args(self):
        messages = [
            {"role": "system", "content": "a" * 100},
            {"role": "user", "content": "b" * 50},
            {
                "role": "assistant",
                "content": "c" * 10,
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "d" * 40},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "1", "content": "e" * 20},
        ]
        out = h.format_status(messages)
        # 100 + 50 + 10 + 40 + 20 = 220 chars of conversation, plus the tool
        # schemas that are sent with every request
        total = 220 + len(json.dumps(h._active_tools(True)))
        self.assertTrue(
            out.startswith(f"context: ~{total // 4} tokens (estimated, ~{total} chars) in 4 messages")
        )

    def test_empty_history(self):
        out = h.format_status([])
        total = len(json.dumps(h._active_tools(True)))
        self.assertTrue(
            out.startswith(f"context: ~{total // 4} tokens (estimated, ~{total} chars) in 0 messages")
        )

    def test_counts_reasoning_content(self):
        messages = [
            {"role": "assistant", "content": "c" * 10, "reasoning_content": "r" * 30},
        ]
        out = h.format_status(messages)
        total = 40 + len(json.dumps(h._active_tools(True)))
        self.assertTrue(
            out.startswith(f"context: ~{total // 4} tokens (estimated, ~{total} chars) in 1 messages")
        )

    def test_context_window_percentage(self):
        messages = [{"role": "system", "content": "x" * 400}]
        out = h.format_status(messages, context_window=10000)
        total = 400 + len(json.dumps(h._active_tools(True)))
        pct = (total // 4) * 100 // 10000
        self.assertIn(f"~{pct}% of 10000 window", out)

    def test_no_context_window_omits_percentage(self):
        out = h.format_status([{"role": "system", "content": "x"}])
        self.assertNotIn("% of", out)

    def test_tiny_percentage_shows_lt_1(self):
        out = h.format_status([{"role": "system", "content": "x" * 400}], context_window=2222080)
        self.assertIn("<1% of 2222080 window", out)


class TestProbeContextWindow(unittest.TestCase):
    def setUp(self):
        self.old_api_url = h.API_URL
        self.old_model = h.MODEL
        h.API_URL = "http://127.0.0.1:11434/v1/chat/completions"
        h.MODEL = "local-model"
        self.addCleanup(setattr, h, "API_URL", self.old_api_url)
        self.addCleanup(setattr, h, "MODEL", self.old_model)

    def _fake_response(self, payload):
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def test_context_length_field_and_models_url(self):
        payload = {"data": [{"id": "m1", "context_length": 8192}]}
        with mock.patch(
            "urllib.request.urlopen", return_value=self._fake_response(payload)
        ) as m:
            self.assertEqual(h.probe_context_window(), 8192)
        req = m.call_args[0][0]
        self.assertEqual(req.full_url, "http://127.0.0.1:11434/v1/models")

    def test_llamacpp_meta_n_ctx(self):
        payload = {"data": [{"id": "m1", "meta": {"n_ctx": 222208}}]}
        with mock.patch(
            "urllib.request.urlopen", return_value=self._fake_response(payload)
        ):
            self.assertEqual(h.probe_context_window(), 222208)

    def test_context_length_wins_over_n_ctx(self):
        payload = {"data": [{"id": "m1", "context_length": 4096, "meta": {"n_ctx": 8192}}]}
        with mock.patch(
            "urllib.request.urlopen", return_value=self._fake_response(payload)
        ):
            self.assertEqual(h.probe_context_window(), 4096)

    def test_multi_model_matches_by_id(self):
        payload = {
            "data": [
                {"id": "other", "context_length": 1000},
                {"id": "local-model", "context_length": 4096},
            ]
        }
        with mock.patch(
            "urllib.request.urlopen", return_value=self._fake_response(payload)
        ):
            self.assertEqual(h.probe_context_window(), 4096)

    def test_multi_model_no_match_uses_first(self):
        payload = {
            "data": [
                {"id": "a", "context_length": 1000},
                {"id": "b", "context_length": 2000},
            ]
        }
        with mock.patch(
            "urllib.request.urlopen", return_value=self._fake_response(payload)
        ):
            self.assertEqual(h.probe_context_window(), 1000)

    def test_network_error_returns_zero(self):
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("down")
        ):
            self.assertEqual(h.probe_context_window(), 0)

    def test_malformed_json_returns_zero(self):
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"not json")):
            self.assertEqual(h.probe_context_window(), 0)

    def test_missing_field_returns_zero(self):
        payload = {"data": [{"id": "m1"}]}
        with mock.patch(
            "urllib.request.urlopen", return_value=self._fake_response(payload)
        ):
            self.assertEqual(h.probe_context_window(), 0)

    def test_empty_data_returns_zero(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=self._fake_response({"data": []})
        ):
            self.assertEqual(h.probe_context_window(), 0)

    def test_api_url_line(self):
        old = h.API_URL
        h.API_URL = "http://example.com/v1/chat/completions"
        self.addCleanup(setattr, h, "API_URL", old)
        out = h.format_status([{"role": "system", "content": "x"}])
        self.assertIn("api url: http://example.com/v1/chat/completions", out)

    def test_tool_names(self):
        out = h.format_status([{"role": "system", "content": "x"}])
        names = sorted(s["function"]["name"] for s in h.OPENAI_TOOLS_INTERACTIVE)
        self.assertIn("tools: " + ", ".join(names), out)
        self.assertNotIn("exit", out.split("tools: ")[1])


class TestNormalizeApiUrl(unittest.TestCase):
    def test_full_endpoint_unchanged(self):
        url = "https://openrouter.ai/api/v1/chat/completions"
        self.assertEqual(h.normalize_api_url(url), url)

    def test_full_endpoint_trailing_slash(self):
        self.assertEqual(
            h.normalize_api_url("https://openrouter.ai/api/v1/chat/completions/"),
            "https://openrouter.ai/api/v1/chat/completions",
        )

    def test_base_url_gets_endpoint(self):
        self.assertEqual(
            h.normalize_api_url("https://openrouter.ai/api/v1"),
            "https://openrouter.ai/api/v1/chat/completions",
        )

    def test_local_default_base_url(self):
        self.assertEqual(
            h.normalize_api_url("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434/v1/chat/completions",
        )

    def test_strips_whitespace_and_slashes(self):
        self.assertEqual(
            h.normalize_api_url("  http://127.0.0.1:11434/v1/  "),
            "http://127.0.0.1:11434/v1/chat/completions",
        )

    def test_empty_stays_empty(self):
        self.assertEqual(h.normalize_api_url(""), "")
        self.assertEqual(h.normalize_api_url(None), "")


class TestHelp(unittest.TestCase):
    def test_lists_all_commands(self):
        out = h.format_help()
        for cmd in ("/new", "/clear-screen", "/status", "/help", "/exit"):
            self.assertIn(cmd, out)

    def test_mentions_quit_alias(self):
        self.assertIn("/quit", h.format_help())


class TestParseSseLine(unittest.TestCase):
    def _line(self, delta):
        return "data: " + json.dumps({"choices": [{"delta": delta}]})

    def test_valid_delta(self):
        self.assertEqual(h.parse_sse_line(self._line({"content": "hi"})), {"content": "hi"})

    def test_done_terminator(self):
        self.assertEqual(h.parse_sse_line("data: [DONE]"), "[DONE]")

    def test_comment_ignored(self):
        self.assertIsNone(h.parse_sse_line(": keepalive"))

    def test_blank_ignored(self):
        self.assertIsNone(h.parse_sse_line(""))

    def test_non_data_line_ignored(self):
        self.assertIsNone(h.parse_sse_line("event: message"))

    def test_malformed_json_ignored(self):
        self.assertIsNone(h.parse_sse_line("data: {not json"))

    def test_missing_delta(self):
        self.assertEqual(h.parse_sse_line("data: " + json.dumps({"choices": [{"index": 0}]})), {})

    def test_whitespace_tolerance(self):
        self.assertEqual(h.parse_sse_line("  data:   [DONE]  "), "[DONE]")


class TestStreamAccumulation(unittest.TestCase):
    def accumulate(self, chunks):
        msg = {}
        for c in chunks:
            h.accumulate_delta(msg, c)
        return msg

    def test_content_only(self):
        self.assertEqual(self.accumulate([{"content": "He"}, {"content": "llo"}]), {"content": "Hello"})

    def test_empty_delta_ignored(self):
        self.assertEqual(self.accumulate([{}, {"content": "a"}, {}]), {"content": "a"})

    def test_reasoning_and_content(self):
        msg = self.accumulate([
            {"reasoning_content": "let me "},
            {"reasoning_content": "think"},
            {"content": "answer"},
        ])
        self.assertEqual(msg["reasoning_content"], "let me think")
        self.assertEqual(msg["content"], "answer")

    def test_tool_call_split_across_chunks(self):
        msg = self.accumulate([
            {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file"}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": '}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]},
        ])
        self.assertEqual(msg["tool_calls"], [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}
        ])

    def test_parallel_tool_calls(self):
        msg = self.accumulate([
            {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file"}},
                            {"index": 1, "id": "c2", "function": {"name": "write_file"}}]},
            {"tool_calls": [{"index": 1, "function": {"arguments": '{"path": "x"}'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "y"}'}}]},
        ])
        self.assertEqual(len(msg["tool_calls"]), 2)
        self.assertEqual(msg["tool_calls"][0]["id"], "c1")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], '{"path": "y"}')
        self.assertEqual(msg["tool_calls"][1]["id"], "c2")
        self.assertEqual(msg["tool_calls"][1]["function"]["arguments"], '{"path": "x"}')

    def test_tool_call_with_content(self):
        msg = self.accumulate([
            {"content": "let me check "},
            {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "get_cwd", "arguments": "{}"}}]},
        ])
        self.assertEqual(msg["content"], "let me check ")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "get_cwd")

    def test_mutation_is_in_place(self):
        msg = {"role": "assistant"}
        result = h.accumulate_delta(msg, {"content": "x"})
        self.assertIs(result, msg)


class TestLineEditor(Base):
    def setUp(self):
        super().setUp()
        self._old_history = h.HISTORY
        h.HISTORY = []
        self.addCleanup(setattr, h, "HISTORY", self._old_history)
        self._old_history_file = h.HISTORY_FILE
        h.HISTORY_FILE = os.path.join(self.tmp, "history.txt")
        self.addCleanup(setattr, h, "HISTORY_FILE", self._old_history_file)

    def edit(self, keys):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            line = h._edit_line("you> ", iter(keys))
        return line, buf.getvalue()

    def test_typing_and_enter(self):
        keys = [("char", c) for c in "hello"] + ["enter"]
        line, out = self.edit(keys)
        self.assertEqual(line, "hello")
        self.assertTrue(out.endswith("you> hello\r\n"))

    def test_newline_token_inserts_newline(self):
        keys = [("char", "a"), "newline", ("char", "b"), "enter"]
        line, out = self.edit(keys)
        self.assertEqual(line, "a\nb")
        self.assertIn("you> a\nb", out)

    def test_multiline_render_does_not_reprint_lines(self):
        keys = [("char", "a"), "newline", ("char", "b"), "enter"]
        line, out = self.edit(keys)
        self.assertEqual(line, "a\nb")
        # The final render must draw the text once and leave the cursor at the
        # end; the cursor is positioned with ANSI moves, not by re-printing the
        # text (which would re-draw the lines after the newline on every key).
        self.assertTrue(out.endswith("you> a\nb\r\n"))
        self.assertNotIn("you> a\nb\ryou> a", out)

    def _narrow_edit(self, keys, width=10):
        real = h.terminal_width
        h.terminal_width = lambda: width
        self.addCleanup(setattr, h, "terminal_width", real)
        return self.edit(keys)

    def test_soft_wrap_clears_all_physical_lines(self):
        # Width 10, prompt "you> " (5 cols): 12 chars wrap to 2 physical lines.
        keys = [("char", c) for c in "0123456789ab"] + ["enter"]
        line, out = self._narrow_edit(keys)
        self.assertEqual(line, "0123456789ab")
        # The last render must move up one physical line and clear both of
        # them before re-printing (clearing only the logical lines would leave
        # the wrapped first line re-printed on every key).
        self.assertIn("\x1b[1A\r\x1b[2K\x1b[B\x1b[2K\x1b[1Ayou> 0123456789ab", out)
        self.assertTrue(out.endswith("you> 0123456789ab\r\n"))

    def test_soft_wrap_cursor_move_across_wrap(self):
        keys = [("char", c) for c in "0123456789ab"] + ["left"] * 8 + ["enter"]
        line, out = self._narrow_edit(keys)
        self.assertEqual(line, "0123456789ab")
        # Moving from char 5 to char 4 crosses the wrap: the cursor goes from
        # row 1 col 7 to row 0 col 9 (one line down-to-up, two cols right).
        self.assertIn("\x1b[1B\x1b[2C", out)

    def test_soft_wrap_boundary_cursor(self):
        # Prompt (5) + 6 chars: the end sits at row 1 col 1; one left puts the
        # cursor exactly on the wrap boundary (total col 10), i.e. row 1 col 0.
        keys = [("char", c) for c in "012345"] + ["left"] + ["enter"]
        line, out = self._narrow_edit(keys)
        self.assertEqual(line, "012345")
        self.assertTrue(out.endswith("\x1b[1D\r\n"))

    def test_soft_wrap_exact_boundary_no_premature_wrap(self):
        # Prompt (5) + 5 chars fills the row exactly: the terminal has not
        # wrapped yet, so the cursor stays at row 0 col 9 and a left-arrow
        # needs no row move.
        keys = [("char", c) for c in "01234"] + ["left"] + ["enter"]
        line, out = self._narrow_edit(keys)
        self.assertEqual(line, "01234")
        self.assertTrue(out.endswith("\x1b[2Kyou> 01234\r\n"))

    def test_soft_wrap_then_newline_clears_all_rows(self):
        # Width 10, prompt "you> " (5 cols): 8 chars wrap to rows 0-1, and a
        # Ctrl+J newline drops the cursor to row 2. The next keystroke must
        # clear all three physical rows (move up 2), not just two, or the
        # stale first line is re-printed on every key.
        keys = [("char", c) for c in "01234567"] + ["newline", ("char", "x"), "enter"]
        line, out = self._narrow_edit(keys)
        self.assertEqual(line, "01234567\nx")
        self.assertIn(
            "\x1b[2A\r\x1b[2K\x1b[B\x1b[2K\x1b[B\x1b[2K\x1b[2Ayou> 01234567\nx", out
        )
        self.assertTrue(out.endswith("you> 01234567\nx\r\n"))

    def test_newline_in_middle_of_line(self):
        keys = [("char", "a"), ("char", "b"), "left", "newline", "enter"]
        self.assertEqual(self.edit(keys)[0], "a\nb")

    def test_backspace_across_newline(self):
        keys = [("char", "a"), "newline", ("char", "b"), "backspace", "backspace", "enter"]
        self.assertEqual(self.edit(keys)[0], "a")

    def test_backspace_and_delete(self):
        keys = [("char", c) for c in "abc"] + ["left", "left", "delete", "enter"]
        self.assertEqual(self.edit(keys)[0], "ac")
        keys = [("char", c) for c in "abc"] + ["backspace", "left", "delete", "enter"]
        self.assertEqual(self.edit(keys)[0], "a")

    def test_cursor_movement(self):
        keys = [("char", c) for c in "abcd"] + ["left", "left", ("char", "X"), "enter"]
        self.assertEqual(self.edit(keys)[0], "abXcd")

    def test_home_end(self):
        keys = [("char", c) for c in "ab"] + ["home", ("char", "X"), "end", ("char", "Y"), "enter"]
        self.assertEqual(self.edit(keys)[0], "XabY")

    def test_ctrl_u_clears_to_end(self):
        keys = [("char", c) for c in "abc"] + ["left", "ctrl_u", "enter"]
        self.assertEqual(self.edit(keys)[0], "c")

    def test_history_up_down(self):
        h.HISTORY = ["first", "second"]
        keys = ["up", "enter"]
        self.assertEqual(self.edit(keys)[0], "second")
        keys = ["up", "up", "enter"]
        self.assertEqual(self.edit(keys)[0], "first")
        keys = ["up", "down", "enter"]
        self.assertEqual(self.edit(keys)[0], "")
        keys = ["up", "up", "down", "enter"]
        self.assertEqual(self.edit(keys)[0], "second")
        keys = ["up", "up", "down", "down", "enter"]
        self.assertEqual(self.edit(keys)[0], "")

    def test_history_up_at_top_stays(self):
        h.HISTORY = ["only"]
        self.assertEqual(self.edit(["up", "up", "enter"])[0], "only")

    def test_ctrl_c_raises(self):
        with self.assertRaises(KeyboardInterrupt):
            self.edit([("char", "a"), "ctrl_c"])

    def test_ctrl_d_empty_raises_eof(self):
        with self.assertRaises(EOFError):
            self.edit(["ctrl_d"])

    def test_ctrl_d_nonempty_submits(self):
        self.assertEqual(self.edit([("char", "a"), "ctrl_d"])[0], "a")

    def test_ignore_tokens(self):
        keys = [("char", "a"), "ignore", ("char", "b"), "enter"]
        self.assertEqual(self.edit(keys)[0], "ab")

    def test_history_add_dedup_and_cap(self):
        h._history_add("a")
        h._history_add("a")
        h._history_add("b")
        self.assertEqual(h.HISTORY, ["a", "b"])
        h._history_add("")
        h._history_add("   ")
        self.assertEqual(h.HISTORY, ["a", "b"])
        old_max = h.HISTORY_MAX
        h.HISTORY_MAX = 3
        self.addCleanup(setattr, h, "HISTORY_MAX", old_max)
        for i in range(10):
            h._history_add(f"e{i}")
        self.assertEqual(len(h.HISTORY), 3)
        self.assertEqual(h.HISTORY, ["e7", "e8", "e9"])

    def test_history_persisted_to_file(self):
        h._history_add("one")
        h._history_add("two")
        with open(h.HISTORY_FILE, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "one\ntwo\n")

    def test_history_load_from_file(self):
        with open(h.HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write("old1\nold2\n\n")
        h.HISTORY = []
        h._history_load()
        self.assertEqual(h.HISTORY, ["old1", "old2"])

    def test_history_load_missing_file(self):
        h.HISTORY = []
        h._history_load()
        self.assertEqual(h.HISTORY, [])

    def test_history_load_caps_at_max(self):
        old_max = h.HISTORY_MAX
        h.HISTORY_MAX = 2
        self.addCleanup(setattr, h, "HISTORY_MAX", old_max)
        with open(h.HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write("a\nb\nc\n")
        h.HISTORY = []
        h._history_load()
        self.assertEqual(h.HISTORY, ["b", "c"])

    def test_readline_prompt_falls_back_without_tty(self):
        import io
        old = sys.stdin
        sys.stdin = io.StringIO("piped line\n")
        try:
            self.assertEqual(h.readline_prompt("you> "), "piped line")
        finally:
            sys.stdin = old

    def _patch_menu(self, result):
        real = h.commands_menu
        calls = []
        h.commands_menu = lambda: (calls.append(1), result)[1]
        self.addCleanup(setattr, h, "commands_menu", real)
        return calls

    def test_slash_on_empty_line_opens_menu(self):
        calls = self._patch_menu("/new")
        line, out = self.edit([("char", "/"), "enter"])
        self.assertEqual(calls, [1])
        self.assertEqual(line, "/new")
        self.assertTrue(out.endswith("you> /new\r\n"))

    def test_slash_menu_selection_is_extensible(self):
        self._patch_menu("/tools")
        line, _ = self.edit([("char", "/"), ("char", " "), ("char", "r"), "enter"])
        self.assertEqual(line, "/tools r")

    def test_slash_menu_cancel_keeps_slash(self):
        self._patch_menu(None)
        line, _ = self.edit([("char", "/"), ("char", "n"), ("char", "e"), ("char", "w"), "enter"])
        self.assertEqual(line, "/new")

    def test_slash_nonempty_line_no_menu(self):
        calls = self._patch_menu("/new")
        line, _ = self.edit([("char", "a"), ("char", "/"), "enter"])
        self.assertEqual(line, "a/")
        self.assertEqual(calls, [])


class TestWindowsKeyMap(unittest.TestCase):
    def first(self, chars):
        class FakeMsvcrt:
            def __init__(self, seq):
                self._it = iter(seq)

            def getwch(self):
                return next(self._it)

        with mock.patch.dict(sys.modules, {"msvcrt": FakeMsvcrt(list(chars))}):
            return next(h._iter_keys_windows())

    def test_extended_keys(self):
        for code, token in (
            ("H", "up"),
            ("P", "down"),
            ("K", "left"),
            ("M", "right"),
            ("G", "home"),
            ("O", "end"),
            ("S", "delete"),
        ):
            self.assertEqual(self.first(["\xe0", code]), token, code)

    def test_plain_keys(self):
        self.assertEqual(self.first(["\r"]), "enter")
        self.assertEqual(self.first(["\n"]), "newline")
        self.assertEqual(self.first(["a"]), ("char", "a"))
        self.assertEqual(self.first(["\x1b"]), "esc")


class TestCsiSequences(unittest.TestCase):
    def feed(self, seq):
        it = iter(list(seq))
        return h._parse_csi_seq(lambda: next(it, None))

    def test_delete(self):
        self.assertEqual(self.feed("3~"), "delete")

    def test_arrows(self):
        self.assertEqual(self.feed("A"), "up")
        self.assertEqual(self.feed("B"), "down")
        self.assertEqual(self.feed("C"), "right")
        self.assertEqual(self.feed("D"), "left")

    def test_home_end(self):
        self.assertEqual(self.feed("H"), "home")
        self.assertEqual(self.feed("F"), "end")

    def test_unknown_ignored(self):
        self.assertEqual(self.feed("2~"), "ignore")
        self.assertEqual(self.feed("1;5C"), "ignore")

    def test_truncated_ignored(self):
        self.assertEqual(self.feed("3"), "ignore")

    @unittest.skipIf(os.name == "nt", "pty not available on Windows")
    def test_delete_key_via_pty(self):
        import pty
        import tty

        master, slave = pty.openpty()  # type: ignore[reportAttributeAccessIssue]
        # Raw mode before writing so the line discipline does not convert
        # \r to \n (ICRNL) and hold back input (canonical mode).
        tty.setraw(slave)  # type: ignore[reportAttributeAccessIssue]
        old = sys.stdin
        stdin_file = os.fdopen(slave, "r")
        try:
            sys.stdin = stdin_file
            os.write(master, b"abc\x1b[3~\r")
            tokens = []
            for tok in h._iter_keys_posix():
                tokens.append(tok)
                if tok == "enter":
                    break
            self.assertEqual(
                tokens,
                [("char", "a"), ("char", "b"), ("char", "c"), "delete", "enter"],
            )
        finally:
            sys.stdin = old
            stdin_file.close()
            os.close(master)

    @unittest.skipIf(os.name == "nt", "pty not available on Windows")
    def test_ctrl_j_via_pty(self):
        import pty
        import tty

        master, slave = pty.openpty()  # type: ignore[reportAttributeAccessIssue]
        # Raw mode before writing so \n (Ctrl+J) and \r (Enter) pass through
        # unconverted.
        tty.setraw(slave)  # type: ignore[reportAttributeAccessIssue]
        old = sys.stdin
        stdin_file = os.fdopen(slave, "r")
        try:
            sys.stdin = stdin_file
            os.write(master, b"a\nb\r")
            tokens = []
            for tok in h._iter_keys_posix():
                tokens.append(tok)
                if tok == "enter":
                    break
            self.assertEqual(
                tokens, [("char", "a"), "newline", ("char", "b"), "enter"]
            )
        finally:
            sys.stdin = old
            stdin_file.close()
            os.close(master)

    @unittest.skipIf(os.name == "nt", "pty not available on Windows")
    def test_bare_esc_via_pty(self):
        import pty
        import tty

        master, slave = pty.openpty()  # type: ignore[reportAttributeAccessIssue]
        # Put the slave in raw mode before writing so the lone ESC byte is
        # not held back by the canonical-mode line discipline.
        tty.setraw(slave)  # type: ignore[reportAttributeAccessIssue]
        old = sys.stdin
        stdin_file = os.fdopen(slave, "r")
        try:
            sys.stdin = stdin_file
            os.write(master, b"\x1b")
            gen = h._iter_keys_posix()
            tokens = []
            try:
                for tok in gen:
                    tokens.append(tok)
                    if tok == "esc":
                        break
            finally:
                gen.close()
            self.assertEqual(tokens, ["esc"])
        finally:
            sys.stdin = old
            stdin_file.close()
            os.close(master)


class TestEndToEndScenario(Base):
    def test_agent_workflow(self):
        proj = os.path.join(self.tmp, "proj").replace("\\", "/")
        h.tool_write_file({"path": f"{proj}/src/main.py", "content": "def calc(x):\n    return x + 1\n\nprint(calc(5))\n"})
        h.tool_write_file({"path": f"{proj}/src/util.py", "content": "def other():\n    pass\n"})
        h.tool_write_file({"path": f"{proj}/README.md", "content": "# demo\n"})

        found = h.tool_grep({"path": proj, "pattern": "def calc"})
        self.assertEqual(found, "_test_tmp/proj/src/main.py:1: def calc(x):")

        self.assertEqual(
            h.tool_patch_file({"path": f"{proj}/src/main.py", "old_string": "return x + 1", "new_string": "return x * 2"}),
            f"patched {self.p('proj/src/main.py')}: replaced 1 occurrence(s)")

        self.assertEqual(self.r("proj/src/main.py"), "def calc(x):\n    return x * 2\n\nprint(calc(5))")

        out = h.tool_run_shell({"command": f"python {proj}/src/main.py".replace("\\", "/")})
        self.assertEqual(out, "exit code: 0\n10")


FAKE_MCP_SERVER = """
import sys, json

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0.1"}}
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "echoes", "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]}}]}
        elif method == "tools/call":
            args = msg.get("params", {}).get("arguments", {})
            result = {"content": [{"type": "text", "text": "echo: " + str(args.get("msg", ""))}]}
        else:
            out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}}
            sys.stdout.write(json.dumps(out) + "\\n")
            sys.stdout.flush()
            continue
        out = {"jsonrpc": "2.0", "id": rid, "result": result}
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.stdout.flush()

main()
"""


class TestMcpContentToText(unittest.TestCase):
    def test_text_items(self):
        self.assertEqual(
            h._mcp_content_to_text({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}),
            "a\nb",
        )

    def test_image(self):
        self.assertEqual(
            h._mcp_content_to_text({"content": [{"type": "image", "mimeType": "image/png"}]}),
            "[image: image/png]",
        )

    def test_structured(self):
        out = h._mcp_content_to_text(
            {"content": [{"type": "text", "text": "x"}], "structuredContent": {"k": 1}}
        )
        self.assertEqual(out, "x\nstructured: " + json.dumps({"k": 1}))

    def test_is_error(self):
        self.assertEqual(
            h._mcp_content_to_text({"content": [{"type": "text", "text": "boom"}], "isError": True}),
            "error: boom",
        )

    def test_empty(self):
        self.assertEqual(h._mcp_content_to_text({}), "(no content)")


class TestMcpConfig(Base):
    def test_expand_env(self):
        old = os.environ.get("HARNLESS_TEST_VAR")
        os.environ["HARNLESS_TEST_VAR"] = "secret"
        try:
            self.assertEqual(h._expand_env("a${HARNLESS_TEST_VAR}b"), "asecretb")
            self.assertEqual(h._expand_env("${MISSING_VAR_XYZ}"), "${MISSING_VAR_XYZ}")
            self.assertEqual(h._expand_env(42), 42)
        finally:
            if old is None:
                os.environ.pop("HARNLESS_TEST_VAR", None)
            else:
                os.environ["HARNLESS_TEST_VAR"] = old

    def test_load_wrapped(self):
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"s": {"transport": "http", "url": "http://x"}}}, f)
        self.assertEqual(h.load_mcp_config(path), {"s": {"transport": "http", "url": "http://x"}})

    def test_load_bare(self):
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"s": {"transport": "stdio", "command": "node", "args": ["a"]}}, f)
        self.assertEqual(
            h.load_mcp_config(path),
            {"s": {"transport": "stdio", "command": "node", "args": ["a"]}},
        )

    def test_load_env_expansion(self):
        old = os.environ.get("HARNLESS_TEST_URL")
        os.environ["HARNLESS_TEST_URL"] = "http://real"
        try:
            path = os.path.join(self.tmp, "cfg.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"s": {"transport": "http", "url": "${HARNLESS_TEST_URL}/mcp"}}, f)
            self.assertEqual(h.load_mcp_config(path), {"s": {"transport": "http", "url": "http://real/mcp"}})
        finally:
            if old is None:
                os.environ.pop("HARNLESS_TEST_URL", None)
            else:
                os.environ["HARNLESS_TEST_URL"] = old

    def test_parse_stdio(self):
        name, cfg = h._parse_mcp_stdio("fs:npx -y server ./data")
        self.assertEqual(name, "fs")
        self.assertEqual(cfg, {"transport": "stdio", "command": "npx", "args": ["-y", "server", "./data"]})

    def test_parse_stdio_invalid(self):
        with self.assertRaises(h.MCPError):
            h._parse_mcp_stdio("no-colon-here")

    def test_parse_http(self):
        name, cfg = h._parse_mcp_http("api=http://127.0.0.1:8000/mcp")
        self.assertEqual(name, "api")
        self.assertEqual(cfg, {"transport": "http", "url": "http://127.0.0.1:8000/mcp"})

    def test_parse_http_invalid(self):
        with self.assertRaises(h.MCPError):
            h._parse_mcp_http("no-equals")


class TestMcpRegister(Base):
    def _fake_client(self, name, tools):
        c = h.MCPClient(name, {"transport": "stdio", "command": "x"})
        c.connect = lambda: {}
        c.list_tools = lambda: tools
        c.close = lambda: None
        return c

    def _reset(self):
        h.MCP_TOOLS = []
        h.MCP_DISPATCH = {}
        h.MCP_CLIENTS = []

    def test_registers_and_collides(self):
        c1 = self._fake_client("a", [{"name": "read_file", "description": "d", "inputSchema": {"type": "object"}}])
        c2 = self._fake_client(
            "b",
            [
                {"name": "echo", "description": "d", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "dup", "description": "d", "inputSchema": {}},
            ],
        )
        c3 = self._fake_client("c", [{"name": "dup", "description": "d", "inputSchema": {}}])
        self.addCleanup(self._reset)
        h.register_mcp_tools([c1, c2, c3])
        names = [s["function"]["name"] for s in h.MCP_TOOLS]
        # read_file collides with a built-in -> skipped; echo registered;
        # dup from c2 registered, dup from c3 skipped (first server wins)
        self.assertEqual(names, ["echo", "dup"])
        self.assertIn("echo", h.MCP_DISPATCH)
        self.assertIs(h.MCP_DISPATCH["echo"][0], c2)
        self.assertEqual(c2.tool_names, ["echo", "dup"])

    def test_spec_shape(self):
        c = self._fake_client("a", [{"name": "t", "description": "desc", "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}}}])
        self.addCleanup(self._reset)
        h.register_mcp_tools([c])
        self.assertEqual(
            h.MCP_TOOLS[0],
            {
                "type": "function",
                "function": {
                    "name": "t",
                    "description": "desc",
                    "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
                },
            },
        )


class TestMcpStdioIntegration(Base):
    def _write_server(self):
        server = os.path.join(self.tmp, "fake_mcp_server.py")
        with open(server, "w", encoding="utf-8") as f:
            f.write(FAKE_MCP_SERVER)
        return server

    def test_connect_list_call(self):
        server = self._write_server()
        client = h.MCPClient("fake", {"transport": "stdio", "command": sys.executable, "args": [server]})
        try:
            client.connect()
            tools = client.list_tools()
            self.assertEqual(len(tools), 1)
            self.assertEqual(tools[0]["name"], "echo")
            self.assertEqual(client.call_tool("echo", {"msg": "hi"}), "echo: hi")
        finally:
            client.close()

    def test_register_end_to_end(self):
        server = self._write_server()
        client = h.MCPClient("fake", {"transport": "stdio", "command": sys.executable, "args": [server]})
        self.addCleanup(client.close)
        self.addCleanup(setattr, h, "MCP_TOOLS", [])
        self.addCleanup(setattr, h, "MCP_DISPATCH", {})
        self.addCleanup(setattr, h, "MCP_CLIENTS", [])
        h.register_mcp_tools([client])
        names = [s["function"]["name"] for s in h.MCP_TOOLS]
        self.assertIn("echo", names)
        self.assertEqual(h.execute_tool("echo", '{"msg": "yo"}'), "echo: yo")


class TestMcpHttpIntegration(Base):
    def test_http_roundtrip(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                msg = json.loads(self.rfile.read(length))
                method = msg.get("method")
                rid = msg.get("id")
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "add", "description": "adds", "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}}]}
                elif method == "tools/call":
                    args = msg.get("params", {}).get("arguments", {})
                    result = {"content": [{"type": "text", "text": str(args.get("a", 0) + args.get("b", 0))}]}
                elif rid is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                else:
                    result = {}
                payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            client = h.MCPClient("http", {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"})
            client.connect()
            tools = client.list_tools()
            self.assertEqual(tools[0]["name"], "add")
            self.assertEqual(client.call_tool("add", {"a": 2, "b": 3}), "5")
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_mcp_http_flag_registers_tools(self):
        """Regression: --mcp-http specs must be added to mcp_servers in main()."""
        import subprocess
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                msg = json.loads(self.rfile.read(length))
                method = msg.get("method")
                rid = msg.get("id")
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "add", "description": "adds", "inputSchema": {"type": "object", "properties": {}}}]}
                elif rid is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                else:
                    result = {}
                payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "harnless.py",
                    "--no-color",
                    "--context-window", "1000",
                    "--mcp-http", f"fake=http://127.0.0.1:{port}/mcp",
                ],
                input="/tools\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("[X] add", proc.stdout)
        finally:
            server.shutdown()
            server.server_close()


class TestToolsToggle(unittest.TestCase):
    def setUp(self):
        self._old_disabled = h.DISABLED_TOOLS
        h.DISABLED_TOOLS = set()
        self.addCleanup(setattr, h, "DISABLED_TOOLS", self._old_disabled)

    def test_format_tools_all_on(self):
        out = h.format_tools()
        lines = out.split("\n")
        self.assertTrue(lines)
        self.assertTrue(all(l.startswith("[X] ") for l in lines))
        self.assertIn("[X] read_file — ", out)

    def test_format_tools_reflects_disabled(self):
        h.DISABLED_TOOLS.add("read_file")
        out = h.format_tools()
        self.assertIn("[ ] read_file — ", out)
        self.assertIn("[X] grep — ", out)

    def test_toggle_off_and_on(self):
        self.assertEqual(h.toggle_tools(["read_file"]), [("read_file", "off")])
        self.assertIn("read_file", h.DISABLED_TOOLS)
        self.assertEqual(h.toggle_tools(["read_file"]), [("read_file", "on")])
        self.assertNotIn("read_file", h.DISABLED_TOOLS)

    def test_toggle_unknown(self):
        self.assertEqual(h.toggle_tools(["nope"]), [("nope", "unknown")])
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_toggle_multiple(self):
        res = h.toggle_tools(["read_file", "grep", "nope"])
        self.assertEqual(res, [("read_file", "off"), ("grep", "off"), ("nope", "unknown")])
        self.assertEqual(h.DISABLED_TOOLS, {"read_file", "grep"})

    def test_active_tools_excludes_disabled(self):
        h.DISABLED_TOOLS.add("read_file")
        names = [s["function"]["name"] for s in h._active_tools(True)]
        self.assertNotIn("read_file", names)
        self.assertIn("grep", names)

    def test_execute_tool_rejects_disabled(self):
        h.DISABLED_TOOLS.add("get_cwd")
        out = h.execute_tool("get_cwd", "{}")
        self.assertTrue(out.startswith("error: tool 'get_cwd' is disabled"))

    def test_status_reflects_disabled(self):
        h.DISABLED_TOOLS.add("read_file")
        out = h.format_status([{"role": "system", "content": "x"}])
        tools_line = out.split("tools: ")[1].split("\n")[0]
        self.assertNotIn("read_file", tools_line)
        self.assertIn("grep", tools_line)


class TestToolsMenu(unittest.TestCase):
    def setUp(self):
        self._old_disabled = h.DISABLED_TOOLS
        h.DISABLED_TOOLS = set()
        self.addCleanup(setattr, h, "DISABLED_TOOLS", self._old_disabled)

    def menu(self, keys):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            applied = h.tools_menu(iter(keys))
        return applied, buf.getvalue()

    def sorted_names(self):
        specs = sorted(
            h.OPENAI_TOOLS_INTERACTIVE + h.MCP_TOOLS,
            key=lambda s: s["function"]["name"],
        )
        return [s["function"]["name"] for s in specs]

    def test_enter_applies_no_changes(self):
        applied, _ = self.menu(["enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_space_toggles_and_enter_applies(self):
        first = self.sorted_names()[0]
        applied, _ = self.menu([("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {first})

    def test_down_moves_cursor(self):
        second = self.sorted_names()[1]
        applied, _ = self.menu(["down", ("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {second})

    def test_up_at_top_stays(self):
        first = self.sorted_names()[0]
        applied, _ = self.menu(["up", "up", ("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {first})

    def test_down_clamps_at_end(self):
        names = self.sorted_names()
        n = len(names)
        last = names[-1]
        applied, _ = self.menu(["down"] * (n + 5) + [("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {last})

    def test_toggle_twice_restores(self):
        first = self.sorted_names()[0]
        applied, _ = self.menu([("char", " "), ("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_esc_cancels_changes(self):
        applied, _ = self.menu([("char", " "), "esc"])
        self.assertFalse(applied)
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_ctrl_c_cancels_changes(self):
        applied, _ = self.menu([("char", " "), "ctrl_c"])
        self.assertFalse(applied)
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_ctrl_d_cancels_changes(self):
        applied, _ = self.menu([("char", " "), "ctrl_d"])
        self.assertFalse(applied)
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_eof_cancels(self):
        applied, _ = self.menu([("char", " ")])
        self.assertFalse(applied)
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_menu_preserves_preexisting_disabled(self):
        h.DISABLED_TOOLS.add("read_file")
        first = self.sorted_names()[0]
        applied, _ = self.menu([("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {"read_file", first})

    def test_rendering(self):
        applied, out = self.menu(["enter"])
        self.assertTrue(applied)
        names = self.sorted_names()
        first, second = names[0], names[1]
        self.assertIn("> [x] " + first, out)
        self.assertIn("[x] " + second, out)
        self.assertIn("space: toggle", out)

    def test_rendering_shows_disabled_mark(self):
        h.DISABLED_TOOLS.add("read_file")
        applied, out = self.menu(["enter"])
        self.assertTrue(applied)
        self.assertIn("[ ] read_file", out)


class TestCommandsMenu(unittest.TestCase):
    def menu(self, keys):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            selected = h.commands_menu(iter(keys))
        return selected, buf.getvalue()

    def test_enter_selects_first(self):
        selected, _ = self.menu(["enter"])
        self.assertEqual(selected, h.REPL_COMMANDS[0][0])

    def test_down_moves_cursor(self):
        selected, _ = self.menu(["down", "enter"])
        self.assertEqual(selected, h.REPL_COMMANDS[1][0])

    def test_up_at_top_stays(self):
        selected, _ = self.menu(["up", "up", "enter"])
        self.assertEqual(selected, h.REPL_COMMANDS[0][0])

    def test_down_clamps_at_end(self):
        selected, _ = self.menu(["down"] * (len(h.REPL_COMMANDS) + 5) + ["enter"])
        self.assertEqual(selected, h.REPL_COMMANDS[-1][0])

    def test_esc_cancels(self):
        selected, _ = self.menu(["esc"])
        self.assertIsNone(selected)

    def test_ctrl_c_cancels(self):
        selected, _ = self.menu(["ctrl_c"])
        self.assertIsNone(selected)

    def test_ctrl_d_cancels(self):
        selected, _ = self.menu(["ctrl_d"])
        self.assertIsNone(selected)

    def test_eof_cancels(self):
        selected, _ = self.menu([])
        self.assertIsNone(selected)

    def test_rendering(self):
        selected, out = self.menu(["enter"])
        self.assertEqual(selected, "/new")
        self.assertIn("> /new", out)
        self.assertIn("/clear-screen", out)
        self.assertIn("enter: select", out)
        # The menu starts on the line below the prompt.
        self.assertTrue(out.startswith("\r\n"))

    def test_exit_clears_menu_lines_and_returns_to_prompt(self):
        _, out = self.menu(["enter"])
        n = 1 + len(h.REPL_COMMANDS) + 1  # header + commands + description
        # On exit each menu line is cleared, then the cursor moves back up
        # to the prompt line.
        tail = "\x1b[2K\x1b[B" * (n - 1) + "\x1b[2K" + f"\x1b[{n}A"
        self.assertTrue(out.endswith(tail))


class TestTodo(Base):
    def setUp(self):
        super().setUp()
        self._old_items = h.TODO_ITEMS
        self._old_next = h.TODO_NEXT_ID
        self._old_injected = h.TODO_LAST_INJECTED
        self._old_file = h.TODO_FILE
        h.TODO_ITEMS = []
        h.TODO_NEXT_ID = 1
        h.TODO_LAST_INJECTED = None
        h.TODO_FILE = os.path.join(self.tmp, ".harnless", "todo.md")
        self.addCleanup(setattr, h, "TODO_ITEMS", self._old_items)
        self.addCleanup(setattr, h, "TODO_NEXT_ID", self._old_next)
        self.addCleanup(setattr, h, "TODO_LAST_INJECTED", self._old_injected)
        self.addCleanup(setattr, h, "TODO_FILE", self._old_file)

    def todo(self, **kw):
        return h.tool_todo(kw)

    def test_add_and_list(self):
        self.assertEqual(self.todo(action="add", text="step one"), "todo list:\n- [ ] 1. step one")
        self.assertEqual(self.todo(action="list"), "todo list:\n- [ ] 1. step one")

    def test_add_requires_text(self):
        self.assertEqual(self.todo(action="add"), "error: 'add' requires text or a non-empty items array")

    def test_add_multiple_items(self):
        self.assertEqual(
            self.todo(action="add", items=["a", "b", "c"]),
            "todo list:\n- [ ] 1. a\n- [ ] 2. b\n- [ ] 3. c")
        self.assertEqual(h.TODO_NEXT_ID, 4)

    def test_add_items_skips_blank(self):
        self.assertEqual(
            self.todo(action="add", items=["a", "  ", "b"]),
            "todo list:\n- [ ] 1. a\n- [ ] 2. b")

    def test_add_empty_items_errors(self):
        self.assertEqual(self.todo(action="add", items=[]),
                         "error: 'add' requires text or a non-empty items array")
        self.assertEqual(self.todo(action="add", items=["  "]),
                         "error: 'add' requires text or a non-empty items array")

    def test_update_status(self):
        self.todo(action="add", text="a")
        self.todo(action="add", text="b")
        self.assertEqual(self.todo(action="update", id=2, status="in_progress"),
                         "todo list:\n- [ ] 1. a\n- [~] 2. b")
        self.assertEqual(self.todo(action="update", id=2, status="done"),
                         "todo list:\n- [ ] 1. a\n- [x] 2. b")

    def test_update_text(self):
        self.todo(action="add", text="a")
        self.assertEqual(self.todo(action="update", id=1, text="renamed"), "todo list:\n- [ ] 1. renamed")

    def test_update_bad_id(self):
        self.assertEqual(self.todo(action="update", id=9, status="done"), "error: no todo with id 9")

    def test_update_bad_status(self):
        self.todo(action="add", text="a")
        self.assertEqual(self.todo(action="update", id=1, status="nope"),
                         "error: status must be pending, in_progress, or done")

    def test_update_requires_id(self):
        self.todo(action="add", text="a")
        self.assertEqual(self.todo(action="update", status="done"), "error: 'update' requires id")

    def test_update_multiple(self):
        self.todo(action="add", items=["a", "b", "c"])
        self.assertEqual(
            self.todo(action="update", updates=[
                {"id": 1, "status": "done"},
                {"id": 2, "status": "in_progress", "text": "renamed"},
            ]),
            "todo list:\n- [x] 1. a\n- [~] 2. renamed\n- [ ] 3. c")

    def test_update_multiple_bad_id(self):
        self.todo(action="add", text="a")
        self.assertEqual(self.todo(action="update", updates=[{"id": 9, "status": "done"}]),
                         "error: no todo with id 9")

    def test_update_multiple_bad_status(self):
        self.todo(action="add", text="a")
        self.assertEqual(self.todo(action="update", updates=[{"id": 1, "status": "nope"}]),
                         "error: status must be pending, in_progress, or done")

    def test_update_multiple_bad_entry(self):
        self.todo(action="add", text="a")
        self.assertEqual(self.todo(action="update", updates=["nope"]),
                         "error: each entry in 'updates' must be an object with an 'id'")

    def test_clear(self):
        self.todo(action="add", text="a")
        self.assertEqual(self.todo(action="clear"), "todo list:\n(empty)")
        self.assertEqual(h.TODO_ITEMS, [])

    def test_unknown_action(self):
        self.assertEqual(self.todo(action="fly"), "error: unknown action 'fly' (use add, update, list, or clear)")

    def test_default_action_is_list(self):
        self.todo(action="add", text="a")
        self.assertEqual(self.todo(), "todo list:\n- [ ] 1. a")

    def test_persisted_to_file(self):
        self.todo(action="add", text="a")
        self.todo(action="update", id=1, status="done")
        with open(h.TODO_FILE, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "- [x] 1. a\n")

    def test_load_from_file(self):
        os.makedirs(os.path.dirname(h.TODO_FILE), exist_ok=True)
        with open(h.TODO_FILE, "w", encoding="utf-8") as f:
            f.write("- [ ] 1. a\n- [~] 2. b\n- [x] 3. c\n")
        h._todo_load()
        self.assertEqual(h.TODO_ITEMS, [
            {"id": 1, "text": "a", "status": "pending"},
            {"id": 2, "text": "b", "status": "in_progress"},
            {"id": 3, "text": "c", "status": "done"},
        ])
        self.assertEqual(h.TODO_NEXT_ID, 4)

    def test_load_missing_file(self):
        h._todo_load()
        self.assertEqual(h.TODO_ITEMS, [])

    def test_reminder_injected_on_change(self):
        messages = []
        h._todo_reminder(messages)
        self.assertEqual(messages, [])  # empty list: nothing injected
        self.todo(action="add", text="a")
        h._todo_reminder(messages)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("1. a", messages[0]["content"])
        h._todo_reminder(messages)  # unchanged list: no duplicate reminder
        self.assertEqual(len(messages), 1)
        self.todo(action="update", id=1, status="done")
        h._todo_reminder(messages)
        self.assertEqual(len(messages), 2)
        self.assertIn("[x]", messages[1]["content"])

    def test_reminder_reset_after_clear(self):
        self.todo(action="add", text="a")
        messages = []
        h._todo_reminder(messages)
        self.todo(action="clear")
        h._todo_reminder(messages)
        self.assertEqual(len(messages), 1)  # clearing does not inject
        self.todo(action="add", text="b")
        h._todo_reminder(messages)
        self.assertEqual(len(messages), 2)  # re-added list injects again

    def test_run_agent_injects_reminder(self):
        def fake_stream_once(messages, model, interactive=False, temperature=0.2):
            if len(messages) == 1:
                return ({"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "todo", "arguments": '{"action": "add", "text": "step"}'}}]}, True)
            return ({"role": "assistant", "content": "done"}, True)

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        messages = [{"role": "user", "content": "go"}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        reminders = [m for m in messages if m.get("role") == "system" and "todo list" in (m.get("content") or "")]
        self.assertEqual(len(reminders), 1)
        self.assertIn("1. step", reminders[0]["content"])


class TestMemory(Base):
    def setUp(self):
        super().setUp()
        self._old_proj = h.MEMORY_PROJECT_FILE
        self._old_glob = h.MEMORY_GLOBAL_FILE
        h.MEMORY_PROJECT_FILE = os.path.join(self.tmp, ".harnless", "memory.md")
        h.MEMORY_GLOBAL_FILE = os.path.join(self.tmp, "global_memory.md")
        self.addCleanup(setattr, h, "MEMORY_PROJECT_FILE", self._old_proj)
        self.addCleanup(setattr, h, "MEMORY_GLOBAL_FILE", self._old_glob)

    def mem(self, **kw):
        return h.tool_memory(kw)

    def test_add_and_list(self):
        self.assertEqual(self.mem(action="add", text="user prefers tabs"), "added to project memory (1 notes)")
        out = self.mem(action="list")
        self.assertIn("project memory:", out)
        self.assertIn("- user prefers tabs", out)
        self.assertIn("global memory:", out)
        self.assertIn("(empty)", out)

    def test_add_requires_text(self):
        self.assertEqual(self.mem(action="add"), "error: 'add' requires text")

    def test_add_duplicate(self):
        self.mem(action="add", text="note")
        self.assertEqual(self.mem(action="add", text="note"), "already in project memory")

    def test_add_global_scope(self):
        self.mem(action="add", text="global note", scope="global")
        self.assertTrue(os.path.exists(h.MEMORY_GLOBAL_FILE))
        self.assertFalse(os.path.exists(h.MEMORY_PROJECT_FILE))

    def test_bad_scope(self):
        self.assertEqual(self.mem(action="add", text="x", scope="nope"),
                         "error: scope must be 'project' or 'global'")

    def test_remove(self):
        self.mem(action="add", text="alpha one")
        self.mem(action="add", text="beta two")
        self.assertEqual(self.mem(action="remove", text="alpha"), "removed 1 note(s) from project memory")
        out = self.mem(action="list", scope="project")
        self.assertIn("- beta two", out)
        self.assertNotIn("alpha", out)

    def test_remove_no_match(self):
        self.mem(action="add", text="alpha")
        self.assertEqual(self.mem(action="remove", text="zzz"), "no project memory note contains 'zzz'")

    def test_remove_requires_text(self):
        self.assertEqual(self.mem(action="remove"), "error: 'remove' requires text")

    def test_list_single_scope(self):
        self.mem(action="add", text="x")
        self.assertEqual(self.mem(action="list", scope="global"), "global memory:\n(empty)")

    def test_unknown_action(self):
        self.assertEqual(self.mem(action="fly"), "error: unknown action 'fly' (use add, list, or remove)")

    def test_load_memory(self):
        self.mem(action="add", text="p1")
        self.mem(action="add", text="g1", scope="global")
        self.assertEqual(h.load_memory(), "project:\n- p1\n\nglobal:\n- g1")

    def test_load_memory_empty(self):
        self.assertEqual(h.load_memory(), "")


class TestGotcha(Base):
    def setUp(self):
        super().setUp()
        self._old_file = h.GOTCHAS_FILE
        h.GOTCHAS_FILE = os.path.join(self.tmp, "GOTCHAS.md")
        self.addCleanup(setattr, h, "GOTCHAS_FILE", self._old_file)

    def gotcha(self, **kw):
        return h.tool_gotcha(kw)

    def test_add_and_list(self):
        self.assertEqual(self.gotcha(action="add", text="tests must run from repo root"),
                         "recorded in GOTCHAS.md (1 total)")
        self.assertEqual(self.gotcha(action="list"), "gotchas:\n- tests must run from repo root")

    def test_add_requires_text(self):
        self.assertEqual(self.gotcha(action="add"), "error: 'add' requires text")

    def test_add_duplicate(self):
        self.gotcha(action="add", text="note one")
        self.assertEqual(self.gotcha(action="add", text="note one"), "already recorded in GOTCHAS.md")
        # a more specific note is not a duplicate
        self.assertEqual(self.gotcha(action="add", text="note one variant"), "recorded in GOTCHAS.md (2 total)")

    def test_list_empty(self):
        self.assertEqual(self.gotcha(action="list"), "gotchas:\n(none recorded)")

    def test_unknown_action(self):
        self.assertEqual(self.gotcha(action="fly"), "error: unknown action 'fly' (use add or list)")

    def test_load_gotchas(self):
        self.gotcha(action="add", text="a")
        self.gotcha(action="add", text="b")
        self.assertEqual(h.load_gotchas(), "- a\n- b")

    def test_load_gotchas_absent(self):
        self.assertEqual(h.load_gotchas(), "")


class TestStateToolsRegistered(unittest.TestCase):
    def test_new_tools_in_both_lists(self):
        all_names = [s["function"]["name"] for s in h.OPENAI_TOOLS]
        interactive_names = [s["function"]["name"] for s in h.OPENAI_TOOLS_INTERACTIVE]
        for name in ("todo", "memory", "gotcha"):
            self.assertIn(name, all_names)
            self.assertIn(name, interactive_names)
            self.assertIn(name, h.DISPATCH)

    def test_system_prompt_mentions_state_tools(self):
        prompt = h.get_system_prompt("/x", "")
        for name in ("todo", "memory", "gotcha"):
            self.assertIn(name, prompt)

    def test_context_additions(self):
        old_proj, old_glob, old_got = h.MEMORY_PROJECT_FILE, h.MEMORY_GLOBAL_FILE, h.GOTCHAS_FILE
        h.MEMORY_PROJECT_FILE = "/nonexistent/proj.md"
        h.MEMORY_GLOBAL_FILE = "/nonexistent/glob.md"
        h.GOTCHAS_FILE = "/nonexistent/gotchas.md"
        self.addCleanup(setattr, h, "MEMORY_PROJECT_FILE", old_proj)
        self.addCleanup(setattr, h, "MEMORY_GLOBAL_FILE", old_glob)
        self.addCleanup(setattr, h, "GOTCHAS_FILE", old_got)
        self.assertEqual(h._context_additions(), "")


class TestAskUser(Base):
    def _respond(self, *answers):
        it = iter(answers)
        old = h.readline_prompt

        def fake(prompt):
            try:
                return next(it)
            except StopIteration:
                raise EOFError

        h.readline_prompt = fake
        self.addCleanup(setattr, h, "readline_prompt", old)

    def _raise(self, exc):
        old = h.readline_prompt

        def fake(prompt):
            raise exc

        h.readline_prompt = fake
        self.addCleanup(setattr, h, "readline_prompt", old)

    def _call(self, fn, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(args)

    def test_registered_in_both_lists(self):
        all_names = [s["function"]["name"] for s in h.OPENAI_TOOLS]
        interactive_names = [s["function"]["name"] for s in h.OPENAI_TOOLS_INTERACTIVE]
        for name in ("ask_user", "confirm"):
            self.assertIn(name, all_names)
            self.assertIn(name, interactive_names)
            self.assertIn(name, h.DISPATCH)

    def test_system_prompt_mentions_interaction_tools(self):
        prompt = h.get_system_prompt("/x", "")
        self.assertIn("ask_user", prompt)
        self.assertIn("confirm", prompt)

    def test_empty_question(self):
        self.assertEqual(h.tool_ask_user({}), "error: empty question")
        self.assertEqual(h.tool_ask_user({"question": "  "}), "error: empty question")

    def test_options_must_be_list(self):
        out = self._call(h.tool_ask_user, {"question": "q", "options": "a"})
        self.assertEqual(out, "error: options must be a list of strings")

    def test_freeform_answer(self):
        self._respond("blue")
        out = self._call(h.tool_ask_user, {"question": "favorite color?"})
        self.assertEqual(out, "user answered: blue")

    def test_select_option_by_number(self):
        self._respond("2")
        out = self._call(
            h.tool_ask_user, {"question": "pick", "options": ["a", "b", "c"]}
        )
        self.assertEqual(out, "user selected: b")

    def test_options_allow_custom_answer(self):
        self._respond("something else")
        out = self._call(h.tool_ask_user, {"question": "pick", "options": ["a", "b"]})
        self.assertEqual(out, "user answered: something else")

    def test_out_of_range_option_is_answer(self):
        self._respond("9")
        out = self._call(h.tool_ask_user, {"question": "pick", "options": ["a", "b"]})
        self.assertEqual(out, "user answered: 9")

    def test_empty_answer(self):
        self._respond("")
        self.assertEqual(h.tool_ask_user({"question": "q"}), "user answered: (empty)")

    def test_cancel(self):
        self._raise(EOFError)
        self.assertEqual(
            h.tool_ask_user({"question": "q"}), "user cancelled (no answer)"
        )

    def test_confirm_yes(self):
        self._respond("y")
        self.assertEqual(h.tool_confirm({"question": "ok?"}), "user confirmed: yes")

    def test_confirm_no(self):
        self._respond("no")
        self.assertEqual(h.tool_confirm({"question": "ok?"}), "user confirmed: no")

    def test_confirm_default_on_empty(self):
        self._respond("")
        self.assertEqual(
            h.tool_confirm({"question": "ok?", "default": True}),
            "user confirmed: yes",
        )

    def test_confirm_empty_without_default(self):
        self._respond("")
        self.assertEqual(
            h.tool_confirm({"question": "ok?"}), "user did not confirm"
        )

    def test_confirm_reprompts_on_invalid(self):
        self._respond("maybe", "yes")
        self.assertEqual(h.tool_confirm({"question": "ok?"}), "user confirmed: yes")

    def test_confirm_cancel(self):
        self._raise(KeyboardInterrupt)
        self.assertEqual(
            h.tool_confirm({"question": "ok?"}), "user cancelled (no confirmation)"
        )


class TestFileRefs(Base):
    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    def setUp(self):
        super().setUp()
        self._old_vision = h.VISION_ENABLED
        self._old_cwd = h.CWD
        h.VISION_ENABLED = True
        h.CWD = self.p("")

    def tearDown(self):
        h.VISION_ENABLED = self._old_vision
        h.CWD = self._old_cwd
        super().tearDown()

    def _write(self, rel, content):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_no_refs_returns_string(self):
        self.assertEqual(h.expand_file_refs("hello world"), "hello world")

    def test_non_uri_ref_stays_literal(self):
        self.assertEqual(h.expand_file_refs("see @[not-a-uri] here"), "see @[not-a-uri] here")

    def test_cwd_text_ref(self):
        self._write("notes.txt", "line one\nline two\n")
        out = h.expand_file_refs("read @[cwd://notes.txt] now")
        self.assertIsInstance(out, list)
        texts = [p["text"] for p in out if p.get("type") == "text"]
        joined = "\n".join(texts)
        self.assertIn("[file: notes.txt]", joined)
        self.assertIn("line one", joined)
        self.assertIn("line two", joined)
        self.assertTrue(any(t.startswith("read ") for t in texts))
        self.assertTrue(any(t.endswith(" now") for t in texts))

    def test_cwd_image_ref(self):
        with open(os.path.join(self.tmp, "dot.png"), "wb") as f:
            f.write(self.PNG)
        out = h.expand_file_refs("@[cwd://dot.png]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "image_url")
        url = out[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), self.PNG)

    def test_cwd_missing_file(self):
        out = h.expand_file_refs("@[cwd://nope.txt]")
        self.assertEqual(out, [{"type": "text", "text": "[error: file not found: nope.txt]"}])

    def test_cwd_escape_rejected(self):
        out = h.expand_file_refs("@[cwd://../evil.txt]")
        self.assertEqual(
            out,
            [{"type": "text", "text": "[error: Path escapes working directory: ../evil.txt]"}],
        )

    def test_file_uri_text_ref(self):
        abs_path = self.p("abs.txt")
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write("absolute content")
        uri = pathname2url(abs_path)
        if not uri.startswith("file://"):
            uri = "file://" + uri
        out = h.expand_file_refs(f"@[{uri}]")
        self.assertIsInstance(out, list)
        joined = "\n".join(p["text"] for p in out if p.get("type") == "text")
        self.assertIn("absolute content", joined)

    def test_file_uri_missing(self):
        out = h.expand_file_refs("@[file:///nonexistent/xyz.txt]")
        self.assertEqual(
            out,
            [{"type": "text", "text": "[error: file not found: file:///nonexistent/xyz.txt]"}],
        )

    def test_no_vision_image_becomes_note(self):
        h.VISION_ENABLED = False
        with open(os.path.join(self.tmp, "dot.png"), "wb") as f:
            f.write(self.PNG)
        out = h.expand_file_refs("@[cwd://dot.png]")
        self.assertEqual(
            out,
            [{"type": "text", "text": "[image: dot.png (not sent; vision disabled)]"}],
        )

    def test_truncation(self):
        self._write("big.txt", "a" * 60_000)
        out = h.expand_file_refs("@[cwd://big.txt]")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["text"].endswith("\n... [truncated]"))

    def test_multiple_refs_ordering(self):
        self._write("a.txt", "AAA")
        self._write("b.txt", "BBB")
        out = h.expand_file_refs("@[cwd://a.txt] mid @[cwd://b.txt]")
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["text"], "[file: a.txt]\nAAA")
        self.assertEqual(out[1]["text"], " mid ")
        self.assertEqual(out[2]["text"], "[file: b.txt]\nBBB")

    def test_build_user_message(self):
        self._write("n.txt", "X")
        msg = h.build_user_message("@[cwd://n.txt]")
        self.assertEqual(msg["role"], "user")
        self.assertIsInstance(msg["content"], list)

    def test_format_status_list_content(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,XYZ"}},
                ],
            }
        ]
        out = h.format_status(messages)
        self.assertTrue(out.startswith("context:"))


class TestContentChars(unittest.TestCase):
    def test_string(self):
        self.assertEqual(h._content_chars("hello"), 5)

    def test_none(self):
        self.assertEqual(h._content_chars(None), 0)

    def test_list_text(self):
        self.assertEqual(h._content_chars([{"type": "text", "text": "abc"}]), 3)

    def test_list_image(self):
        url = "data:image/png;base64,AAAA"
        self.assertEqual(
            h._content_chars([{"type": "image_url", "image_url": {"url": url}}]), len(url)
        )

    def test_list_mixed(self):
        self.assertEqual(
            h._content_chars(
                [
                    {"type": "text", "text": "ab"},
                    {"type": "image_url", "image_url": {"url": "cccc"}},
                ]
            ),
            6,
        )


class TestUsageTracking(unittest.TestCase):
    def setUp(self):
        self.saved = dict(h.USAGE_BY_CONV)
        h.USAGE_BY_CONV.clear()

    def tearDown(self):
        h.USAGE_BY_CONV.clear()
        h.USAGE_BY_CONV.update(self.saved)

    def test_record_usage_stores_by_messages_id(self):
        msgs = [{"role": "user", "content": "hi"}]
        h._record_usage(msgs, {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105})
        self.assertEqual(h.USAGE_BY_CONV[id(msgs)]["prompt_tokens"], 100)

    def test_record_usage_ignores_bad(self):
        msgs = [{"role": "user", "content": "hi"}]
        h._record_usage(msgs, None)
        h._record_usage(msgs, {})
        h._record_usage(msgs, {"prompt_tokens": 0})
        self.assertNotIn(id(msgs), h.USAGE_BY_CONV)

    def test_parse_sse_line_usage_chunk_empty_choices(self):
        line = 'data: {"id":"x","choices":[],"usage":{"prompt_tokens":170000,"completion_tokens":10,"total_tokens":170010}}'
        self.assertEqual(
            h.parse_sse_line(line),
            {"usage": {"prompt_tokens": 170000, "completion_tokens": 10, "total_tokens": 170010}},
        )

    def test_parse_sse_line_usage_chunk_no_choices(self):
        self.assertEqual(h.parse_sse_line('data: {"usage":{"prompt_tokens":42}}'), {"usage": {"prompt_tokens": 42}})

    def test_parse_sse_line_empty_choices_no_usage(self):
        self.assertIsNone(h.parse_sse_line('data: {"id":"x","choices":[]}'))

    def test_parse_sse_line_delta_still_works(self):
        self.assertEqual(
            h.parse_sse_line('data: {"choices":[{"delta":{"content":"hi"}}]}'), {"content": "hi"}
        )

    def test_build_request_stream_options(self):
        req = h._build_request([{"role": "user", "content": "hi"}], "m", stream=True)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["stream_options"], {"include_usage": True})
        req2 = h._build_request([{"role": "user", "content": "hi"}], "m", stream=False)
        body2 = json.loads(req2.data.decode("utf-8"))
        self.assertNotIn("stream_options", body2)

    def test_chat_records_usage(self):
        data = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 55, "completion_tokens": 2, "total_tokens": 57},
        }

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(data).encode("utf-8")

        with mock.patch.object(h.urllib.request, "urlopen", return_value=FakeResp()):
            msgs = [{"role": "user", "content": "hi"}]
            out = h.chat(msgs, "m")
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")
        self.assertEqual(h.USAGE_BY_CONV[id(msgs)]["prompt_tokens"], 55)

    def test_stream_chat_records_usage(self):
        lines = [
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":170000,"completion_tokens":1,"total_tokens":170001}}\n',
            b"data: [DONE]\n",
        ]

        class FakeResp:
            def __enter__(self):
                return iter(lines)

            def __exit__(self, *a):
                return False

        with mock.patch.object(h.urllib.request, "urlopen", return_value=FakeResp()):
            msgs = [{"role": "user", "content": "hi"}]
            deltas = list(h.stream_chat(msgs, "m"))
        self.assertEqual(deltas, [{"content": "hi"}])
        self.assertEqual(h.USAGE_BY_CONV[id(msgs)]["prompt_tokens"], 170000)

    def test_format_status_server_reported(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        h._record_usage(msgs, {"prompt_tokens": 170000, "completion_tokens": 10, "total_tokens": 170010})
        out = h.format_status(msgs, context_window=200000)
        self.assertIn("context: 170000 tokens (server-reported) in 2 messages", out)
        self.assertIn("~85% of 200000 window", out)

    def test_format_status_estimated_fallback(self):
        msgs = [{"role": "user", "content": "hi"}]
        out = h.format_status(msgs, context_window=1000)
        self.assertIn("tokens (estimated, ~", out)


class TestNoVisionCli(Base):
    def test_no_vision_flag_accepted(self):
        import subprocess

        proc = subprocess.run(
            [
                sys.executable,
                "harnless.py",
                "--no-color",
                "--no-vision",
                "--context-window",
                "1000",
            ],
            input="/exit\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0)


class TestInterrupt(Base):
    class _RespAfterError:
        """Yields the given lines, then raises ConnectionResetError (like a
        socket shutdown mid-stream)."""

        def __init__(self, lines):
            self._it = iter(lines)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise ConnectionResetError("socket shut down")

    def test_check_double_esc_within_window(self):
        state = {"last_esc": 0.0}
        self.assertFalse(h.check_double_esc(state, "esc", 10.0))
        self.assertTrue(h.check_double_esc(state, "esc", 11.5))

    def test_check_double_esc_outside_window(self):
        state = {"last_esc": 0.0}
        self.assertFalse(h.check_double_esc(state, "esc", 10.0))
        self.assertFalse(h.check_double_esc(state, "esc", 12.1))
        self.assertTrue(h.check_double_esc(state, "esc", 13.0))

    def test_check_double_esc_other_key_resets(self):
        state = {"last_esc": 0.0}
        self.assertFalse(h.check_double_esc(state, "esc", 10.0))
        self.assertFalse(h.check_double_esc(state, ("char", "a"), 10.5))
        self.assertFalse(h.check_double_esc(state, "esc", 11.0))
        self.assertTrue(h.check_double_esc(state, "esc", 11.5))

    def test_stream_chat_breaks_when_triggered(self):
        lines = [
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"there"}}]}\n',
        ]

        class FakeResp:
            """Yields the lines; the interrupt lands while the 2nd is read."""

            def __init__(self, lines, watcher):
                self._it = iter(lines)
                self._watcher = watcher
                self._first = True

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                line = next(self._it)
                if self._first:
                    self._first = False
                else:
                    self._watcher.triggered = True
                return line

        watcher = h.InterruptWatcher()
        with mock.patch.object(
            h, "_open_request", return_value=FakeResp(lines, watcher)
        ):
            msgs = [{"role": "user", "content": "hi"}]
            deltas = list(h.stream_chat(msgs, "m", watcher=watcher))
        self.assertEqual(deltas, [{"content": "hi"}])

    def test_stream_chat_swallows_read_error_when_triggered(self):
        lines = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n']

        class RespInterruptedMidRead(self._RespAfterError):
            """The socket shutdown surfaces as a read error on the 2nd line."""

            def __init__(self, lines, watcher):
                super().__init__(lines)
                self._watcher = watcher
                self._first = True

            def __next__(self):
                if self._first:
                    self._first = False
                    return super().__next__()
                self._watcher.triggered = True
                raise ConnectionResetError("socket shut down")

        watcher = h.InterruptWatcher()
        with mock.patch.object(
            h, "_open_request", return_value=RespInterruptedMidRead(lines, watcher)
        ):
            msgs = [{"role": "user", "content": "hi"}]
            deltas = list(h.stream_chat(msgs, "m", watcher=watcher))
        self.assertEqual(deltas, [{"content": "hi"}])

    def test_stream_chat_raises_read_error_without_watcher(self):
        lines = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n']
        with mock.patch.object(
            h.urllib.request, "urlopen", return_value=self._RespAfterError(lines)
        ):
            msgs = [{"role": "user", "content": "hi"}]
            with self.assertRaises(ConnectionResetError):
                list(h.stream_chat(msgs, "m"))

    def test_stream_chat_swallows_send_error_when_triggered(self):
        """A double ESC during the context send aborts the upload; the
        resulting send error is swallowed and the stream ends with no deltas."""
        import socket

        a, b = socket.socketpair()
        watcher = h.InterruptWatcher()

        def fake_open(req, progress=None, on_socket=None):
            # the opener reports the connection socket, then the interrupt
            # lands: the watcher closes the socket and the send fails
            on_socket(b)
            watcher.triggered = True
            b.close()
            raise ConnectionResetError("socket closed during send")

        try:
            with mock.patch.object(h, "_open_request", side_effect=fake_open):
                msgs = [{"role": "user", "content": "hi"}]
                deltas = list(h.stream_chat(msgs, "m", watcher=watcher))
            self.assertEqual(deltas, [])
            self.assertTrue(watcher.triggered)
        finally:
            a.close()

    def test_stream_chat_raises_send_error_without_trigger(self):
        with mock.patch.object(
            h, "_open_request", side_effect=ConnectionResetError("socket closed")
        ):
            msgs = [{"role": "user", "content": "hi"}]
            with self.assertRaises(ConnectionResetError):
                list(h.stream_chat(msgs, "m", watcher=h.InterruptWatcher()))

    def test_stream_once_interrupt_raises_with_partial(self):
        class FakeWatcher:
            def __init__(self):
                self.triggered = False

            def start(self):
                self.triggered = True

            def stop(self):
                pass

            def attach_socket(self, sock):
                pass

        def fake_stream_chat(messages, model, interactive=False, temperature=0.2, watcher=None, progress=None):
            yield {"content": "partial"}

        old_enabled = h.INTERRUPT_ENABLED
        old_watcher = h.InterruptWatcher
        old_chat = h.stream_chat
        h.INTERRUPT_ENABLED = True
        h.InterruptWatcher = FakeWatcher
        h.stream_chat = fake_stream_chat
        self.addCleanup(setattr, h, "INTERRUPT_ENABLED", old_enabled)
        self.addCleanup(setattr, h, "InterruptWatcher", old_watcher)
        self.addCleanup(setattr, h, "stream_chat", old_chat)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(h.StreamInterrupted) as cm:
                h.stream_once([{"role": "user", "content": "go"}], "m")
        self.assertEqual(cm.exception.message.get("content"), "partial")
        self.assertIn("(interrupted)", buf.getvalue())

    def test_stream_once_interrupt_before_any_delta(self):
        """A double ESC during the context send (before the first delta)
        interrupts the turn with an empty partial message."""

        class FakeWatcher:
            def __init__(self):
                self.triggered = True

            def start(self):
                pass

            def stop(self):
                pass

            def attach_socket(self, sock):
                pass

        def fake_stream_chat(messages, model, interactive=False, temperature=0.2, watcher=None, progress=None):
            yield from ()

        old_enabled = h.INTERRUPT_ENABLED
        old_watcher = h.InterruptWatcher
        old_chat = h.stream_chat
        h.INTERRUPT_ENABLED = True
        h.InterruptWatcher = FakeWatcher
        h.stream_chat = fake_stream_chat
        self.addCleanup(setattr, h, "INTERRUPT_ENABLED", old_enabled)
        self.addCleanup(setattr, h, "InterruptWatcher", old_watcher)
        self.addCleanup(setattr, h, "stream_chat", old_chat)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(h.StreamInterrupted) as cm:
                h.stream_once([{"role": "user", "content": "go"}], "m")
        self.assertNotIn("content", cm.exception.message)
        self.assertIn("(interrupted)", buf.getvalue())

    def test_run_agent_interrupt_keeps_partial_strips_tool_calls(self):
        def fake_stream_once(messages, model, interactive=False, temperature=0.2):
            raise h.StreamInterrupted(
                {
                    "role": "assistant",
                    "content": "partial answer",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path": '},
                        }
                    ],
                }
            )

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        messages = [{"role": "user", "content": "go"}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(messages[-1], {"role": "assistant", "content": "partial answer"})

    def test_run_agent_interrupt_empty_appends_nothing(self):
        def fake_stream_once(messages, model, interactive=False, temperature=0.2):
            raise h.StreamInterrupted({"role": "assistant"})

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        messages = [{"role": "user", "content": "go"}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(len(messages), 1)

    def test_run_agent_interrupt_reasoning_only_dropped(self):
        """A partial with reasoning but no content is dropped: servers
        reject assistant messages without content/tool_calls, which would
        break every later request in the conversation."""

        def fake_stream_once(messages, model, interactive=False, temperature=0.2):
            raise h.StreamInterrupted(
                {"role": "assistant", "reasoning_content": "still thinking"}
            )

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        messages = [{"role": "user", "content": "go"}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(len(messages), 1)

    def test_run_agent_interrupt_reasoning_and_tool_calls_dropped(self):
        """Stripping the half-formed tool calls leaves a content-less
        partial (reasoning only), which is dropped too."""

        def fake_stream_once(messages, model, interactive=False, temperature=0.2):
            raise h.StreamInterrupted(
                {
                    "role": "assistant",
                    "reasoning_content": "still thinking",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "todo", "arguments": '{"action": '},
                        }
                    ],
                }
            )

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        messages = [{"role": "user", "content": "go"}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(len(messages), 1)

    def test_run_agent_interrupt_content_and_reasoning_kept(self):
        def fake_stream_once(messages, model, interactive=False, temperature=0.2):
            raise h.StreamInterrupted(
                {
                    "role": "assistant",
                    "content": "partial answer",
                    "reasoning_content": "some thinking",
                }
            )

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        messages = [{"role": "user", "content": "go"}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(
            messages[-1],
            {"role": "assistant", "content": "partial answer",
             "reasoning_content": "some thinking"},
        )

    def test_watcher_start_noop_without_tty(self):
        if sys.stdin.isatty():
            self.skipTest("stdin is a tty")
        watcher = h.InterruptWatcher()
        watcher.start()
        self.assertIsNone(watcher._thread)
        watcher.stop()


class TestContextProgress(unittest.TestCase):
    def _big_messages(self):
        # Well over CONTEXT_PROGRESS_THRESHOLD tokens (chars/4).
        return [{"role": "user", "content": "x" * (h.CONTEXT_PROGRESS_THRESHOLD * 4 + 1000)}]

    def test_estimate_context_tokens_counts_content_and_tools(self):
        messages = [
            {"role": "system", "content": "a" * 100},
            {"role": "user", "content": "b" * 50},
            {
                "role": "assistant",
                "content": "c" * 10,
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "d" * 40},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "1", "content": "e" * 20},
        ]
        total = 220 + len(json.dumps(h._active_tools(True)))
        self.assertEqual(h.estimate_context_chars(messages, interactive=True), total)
        self.assertEqual(h.estimate_context_tokens(messages, interactive=True), total // 4)

    def test_format_tokens(self):
        self.assertEqual(h._format_tokens(999), "999")
        self.assertEqual(h._format_tokens(1500), "1.5k")
        self.assertEqual(h._format_tokens(32400), "32.4k")
        self.assertEqual(h._format_tokens(1_500_000), "1.5M")

    def test_format_elapsed(self):
        self.assertEqual(h._format_elapsed(5.4), "5s")
        self.assertEqual(h._format_elapsed(59.4), "59s")
        self.assertEqual(h._format_elapsed(75), "1m15s")

    def test_noop_below_threshold(self):
        spinner = h.ContextProgress([{"role": "user", "content": "hi"}], interactive=True)
        spinner.start()
        self.assertFalse(spinner.active)
        spinner.stop()

    def test_noop_without_tty(self):
        if sys.stdout.isatty():
            self.skipTest("stdout is a tty")
        spinner = h.ContextProgress(self._big_messages(), interactive=True)
        spinner.start()
        self.assertFalse(spinner.active)
        spinner.stop()

    def test_line_shows_upload_progress(self):
        spinner = h.ContextProgress([{"role": "user", "content": "hi"}], interactive=True)
        spinner.tokens = 32_000
        spinner.upload = h._UploadProgress(1000)
        spinner.upload.sent = 450
        line = spinner._line()
        self.assertIn("sending context", line)
        self.assertIn("45%", line)
        # tokens sent so far: 32000 * 450 // 1000 = 14400 -> "14.4k/32.0k"
        self.assertIn("14.4k/32.0k", line)

    def test_line_waiting_state(self):
        spinner = h.ContextProgress([{"role": "user", "content": "hi"}], interactive=True)
        spinner.tokens = 32_000
        spinner.upload = h._UploadProgress(1000)
        spinner.upload.sent = 1000  # upload complete
        line = spinner._line()
        self.assertIn("waiting for first token", line)
        self.assertIn("32.0k tokens", line)

    def test_draws_line_and_clears(self):
        old_delay = h.CONTEXT_PROGRESS_DELAY
        h.CONTEXT_PROGRESS_DELAY = 0.05
        self.addCleanup(setattr, h, "CONTEXT_PROGRESS_DELAY", old_delay)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(buf, "isatty", return_value=True):
            spinner = h.ContextProgress(self._big_messages(), interactive=True)
            spinner.start()
            self.assertTrue(spinner.active)
            deadline = time.time() + 5
            while "waiting for first token" not in buf.getvalue() and time.time() < deadline:
                time.sleep(0.01)
            spinner.stop()
        out = buf.getvalue()
        self.assertIn("waiting for first token", out)
        self.assertIn("tokens", out)
        self.assertIn("\x1b[2K", out)  # the line was cleared

    def test_stop_clears_above_hint_lines(self):
        old_delay = h.CONTEXT_PROGRESS_DELAY
        h.CONTEXT_PROGRESS_DELAY = 0.05
        self.addCleanup(setattr, h, "CONTEXT_PROGRESS_DELAY", old_delay)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(buf, "isatty", return_value=True):
            spinner = h.ContextProgress(self._big_messages(), interactive=True)
            spinner.start()
            deadline = time.time() + 5
            while "waiting for first token" not in buf.getvalue() and time.time() < deadline:
                time.sleep(0.01)
            spinner.note_lines_below(2)  # e.g. the esc hint printed below
            spinner.stop()
        out = buf.getvalue()
        self.assertIn("\x1b[2A", out)  # moved up 2 lines to the progress line
        self.assertIn("\x1b[2K", out)

    def test_open_request_plain_without_progress(self):
        req = urllib.request.Request("http://127.0.0.1:1/x", data=b"hi")
        with mock.patch.object(h.urllib.request, "urlopen", return_value="RESP") as m:
            self.assertEqual(h._open_request(req, None), "RESP")
            m.assert_called_once_with(req, timeout=600)

    def test_upload_progress_counts_bytes(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            body = b"x" * (3 * h.PROGRESS_CHUNK + 123)
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            spinner = h.ContextProgress([{"role": "user", "content": "hi"}])
            with h._open_request(req, spinner) as resp:
                data = resp.read()
            self.assertEqual(json.loads(data), {"ok": True})
            self.assertIsNotNone(spinner.upload)
            self.assertEqual(spinner.upload.total, len(body))
            self.assertEqual(spinner.upload.sent, spinner.upload.total)
        finally:
            server.shutdown()
            server.server_close()

    def test_upload_socket_reported(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                payload = b'{"ok":1}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/x",
                data=b"x" * (2 * h.PROGRESS_CHUNK + 1),
                headers={"Content-Type": "application/json"},
            )
            seen = []
            with h._open_request(req, None, seen.append) as resp:
                resp.read()
            self.assertTrue(seen)  # the connection socket was reported
        finally:
            server.shutdown()
            server.server_close()

    def test_response_socket_finds_socket(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                payload = b'{"ok":1}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/x", data=b"hi")
            resp = urllib.request.urlopen(req, timeout=5)
            try:
                self.assertIsNotNone(h._response_socket(resp))
            finally:
                resp.read()
                resp.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
