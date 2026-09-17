"""Tests for harnless.py. Run from the repo root: python tests.py"""

import contextlib
import io
import json
import os
import shutil
import sys
import unittest

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
        h.set_emoji_enabled(True)
        self.addCleanup(h.set_emoji_enabled, True)
        self.assertEqual(h.icon("thinking"), h.ICONS["thinking"])
        self.assertEqual(h.icon("tool"), h.ICONS["tool"])

    def test_ascii_fallback(self):
        h.set_emoji_enabled(False)
        self.addCleanup(h.set_emoji_enabled, True)
        self.assertEqual(h.icon("thinking"), h.ASCII_ICONS["thinking"])
        self.assertEqual(h.icon("assistant"), h.ASCII_ICONS["assistant"])

    def test_unknown_key(self):
        h.set_emoji_enabled(True)
        self.addCleanup(h.set_emoji_enabled, True)
        self.assertEqual(h.icon("nope"), "")


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
        # 100 + 50 + 10 + 40 + 20 = 220 chars -> 55 approx tokens
        self.assertTrue(out.startswith("context: 55 tokens (~220 chars) in 4 messages"))

    def test_empty_history(self):
        out = h.format_status([])
        self.assertTrue(out.startswith("context: 0 tokens (~0 chars) in 0 messages"))

    def test_api_url_line(self):
        old = h.API_URL
        h.API_URL = "http://example.com/v1/chat/completions"
        self.addCleanup(setattr, h, "API_URL", old)
        out = h.format_status([{"role": "system", "content": "x"}])
        self.assertIn("api url: http://example.com/v1/chat/completions", out)

    def test_tool_names(self):
        out = h.format_status([{"role": "system", "content": "x"}])
        names = [s["function"]["name"] for s in h.OPENAI_TOOLS_INTERACTIVE]
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

        master, slave = pty.openpty()
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
    def test_bare_esc_via_pty(self):
        import pty
        import tty

        master, slave = pty.openpty()
        # Put the slave in raw mode before writing so the lone ESC byte is
        # not held back by the canonical-mode line discipline.
        tty.setraw(slave)
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
        c.connect = lambda: None
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

            def log_message(self, *a):
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

    def test_enter_applies_no_changes(self):
        applied, _ = self.menu(["enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_space_toggles_and_enter_applies(self):
        first = h.OPENAI_TOOLS_INTERACTIVE[0]["function"]["name"]
        applied, _ = self.menu([("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {first})

    def test_down_moves_cursor(self):
        second = h.OPENAI_TOOLS_INTERACTIVE[1]["function"]["name"]
        applied, _ = self.menu(["down", ("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {second})

    def test_up_at_top_stays(self):
        first = h.OPENAI_TOOLS_INTERACTIVE[0]["function"]["name"]
        applied, _ = self.menu(["up", "up", ("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {first})

    def test_down_clamps_at_end(self):
        n = len(h.OPENAI_TOOLS_INTERACTIVE)
        last = h.OPENAI_TOOLS_INTERACTIVE[-1]["function"]["name"]
        applied, _ = self.menu(["down"] * (n + 5) + [("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {last})

    def test_toggle_twice_restores(self):
        first = h.OPENAI_TOOLS_INTERACTIVE[0]["function"]["name"]
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
        first = h.OPENAI_TOOLS_INTERACTIVE[0]["function"]["name"]
        applied, _ = self.menu([("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {"read_file", first})

    def test_rendering(self):
        applied, out = self.menu(["enter"])
        self.assertTrue(applied)
        first = h.OPENAI_TOOLS_INTERACTIVE[0]["function"]["name"]
        second = h.OPENAI_TOOLS_INTERACTIVE[1]["function"]["name"]
        self.assertIn("> [x] " + first, out)
        self.assertIn("[x] " + second, out)
        self.assertIn("space: toggle", out)

    def test_rendering_shows_disabled_mark(self):
        h.DISABLED_TOOLS.add("read_file")
        applied, out = self.menu(["enter"])
        self.assertTrue(applied)
        self.assertIn("[ ] read_file", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
