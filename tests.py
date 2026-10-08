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


def _make_dir_link(target: str, link: str) -> bool:
    """Create a directory link (symlink, else a Windows junction) pointing at target."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import subprocess

        return (
            subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True).returncode
            == 0
        )
    return False


def _remove_dir_link(link: str):
    """Remove a directory link without touching its target."""
    for fn in (os.unlink, os.rmdir):
        try:
            fn(link)
            return
        except OSError:
            continue


class TestSymlinkedCwd(Base):
    """Tools must work when the working directory itself is a symlink/junction."""

    def setUp(self):
        super().setUp()
        self._old_cwd = h.CWD
        self.real = self.p("realdir")  # the link target, holding the files
        self.link = os.path.abspath(os.path.join(self.tmp, "linkdir"))
        os.makedirs(os.path.join(self.real, "src"), exist_ok=True)
        with open(os.path.join(self.real, "src", "main.py"), "w", encoding="utf-8") as f:
            f.write("def calc(x):\n    return x + 1\n")
        with open(os.path.join(self.real, "top.txt"), "w", encoding="utf-8") as f:
            f.write("AAA\n")
        if not _make_dir_link(self.real, self.link):
            self.skipTest("cannot create a directory symlink or junction")
        h.CWD = self.link  # work through the link, as os.getcwd() would report it

    def tearDown(self):
        h.CWD = self._old_cwd
        _remove_dir_link(self.link)
        super().tearDown()

    def test_get_cwd_reports_the_link(self):
        self.assertEqual(h.tool_get_cwd({}), self.link)

    def test_safe_resolve_stays_inside_the_linked_cwd(self):
        self.assertEqual(h.safe_resolve("./src/main.py"), self.p("realdir/src/main.py"))
        self.assertEqual(h.safe_resolve("./"), self.p("realdir"))

    def test_safe_resolve_rejects_escape(self):
        with self.assertRaises(ValueError):
            h.safe_resolve("../evil.txt")

    def test_link_inside_cwd_pointing_outside_is_rejected(self):
        escape = os.path.join(self.real, "escape")
        self.assertTrue(_make_dir_link(self.p(""), escape))
        try:
            with self.assertRaises(ValueError):
                h.safe_resolve("./escape/st.txt")
        finally:
            _remove_dir_link(escape)

    def test_grep_reports_cwd_relative_paths(self):
        self.assertEqual(h.tool_grep({"path": "./", "pattern": "AAA"}), "top.txt:1: AAA")
        self.assertEqual(
            h.tool_grep({"path": "./", "pattern": "def calc"}), "src/main.py:1: def calc(x):"
        )

    def test_glob_reports_cwd_relative_paths(self):
        self.assertEqual(h.tool_glob({"path": "./", "pattern": "**/*.py"}), "src/main.py")
        self.assertEqual(h.tool_glob({"path": "./", "pattern": "*.txt"}), "top.txt")

    def test_list_dir(self):
        self.assertEqual(h.tool_list_dir({"path": "./"}), "src/\ntop.txt")

    def test_write_then_read_through_the_link(self):
        msg = h.tool_write_file({"path": "./new.txt", "content": "hello\n"})
        self.assertEqual(msg, f"wrote 6 chars to {self.p('realdir/new.txt')}")
        self.assertTrue(os.path.exists(os.path.join(self.link, "new.txt")))
        self.assertEqual(h.tool_read_file({"path": "./new.txt", "line_numbers": False}), "hello")


class TestCwdPathHelpers(unittest.TestCase):
    def test_rel_to_cwd_uses_resolved_cwd(self):
        real = os.path.realpath(h.CWD)
        self.assertEqual(h._rel_to_cwd(os.path.join(real, "src", "main.py")), "src/main.py")
        self.assertEqual(h._rel_to_cwd(real), ".")

    def test_rel_to_cwd_outside_cwd(self):
        outside = os.path.realpath(os.path.join(os.path.realpath(h.CWD), "..", "outside.txt"))
        self.assertTrue(h._rel_to_cwd(outside).startswith(".."))

    def test_inside_cwd(self):
        roots = h._cwd_roots()
        self.assertTrue(h._inside_cwd(os.path.realpath(h.CWD), roots))
        self.assertTrue(h._inside_cwd(os.path.realpath(os.path.join(h.CWD, "a", "b.txt")), roots))
        self.assertFalse(h._inside_cwd(os.path.realpath(os.path.join(h.CWD, "..", "evil.txt")), roots))

    def test_glob_to_regex_double_star_matches_root_files(self):
        regex = h._glob_to_regex("**/*.py")
        self.assertTrue(regex.match("main.py"))
        self.assertTrue(regex.match("src/deep/main.py"))
        self.assertFalse(regex.match("main.md"))

    def test_glob_to_regex_single_star(self):
        regex = h._glob_to_regex("*.txt")
        self.assertTrue(regex.match("dir/a.txt"))
        self.assertFalse(regex.match("dir/a.md"))

    def test_glob_to_regex_regex_passthrough(self):
        regex = h._glob_to_regex(r"src/(main|util)\.py")
        self.assertTrue(regex.match("src/main.py"))
        self.assertFalse(regex.match("src/other.py"))


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
        self.assertIn("[truncated: 60000 chars total, showing first 50000;", out)
        self.assertTrue(out.endswith("read the rest with offset/lines, or raise max_chars]"))

    def test_read_max_chars_arg(self):
        self.w("mid.txt", "b" * 5_000 + "\n")
        out = self.r("mid.txt", max_chars=1_000)
        self.assertEqual(len(out.split("\n")[0]), 1_000)
        self.assertIn("[truncated: 5000 chars total, showing first 1000;", out)

    def test_read_max_chars_clamped_to_ceiling(self):
        self.w("big2.txt", "c" * 70_000 + "\n")
        out = self.r("big2.txt", max_chars=10_000_000)
        self.assertIn(f"showing first {h.TOOL_RESULT_LIMIT};", out)

    def test_read_max_chars_bogus_falls_back(self):
        self.w("mid2.txt", "d" * 60_000 + "\n")
        out = self.r("mid2.txt", max_chars="not-a-number")
        self.assertIn(f"showing first {h.READ_FILE_LIMIT};", out)

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
        self.assertEqual(len(out.split("\n")), 201)
        self.assertTrue(
            out.endswith(
                "... [truncated: 200 matching lines shown, more exist; "
                "narrow the pattern or file_pattern, or raise limit]"
            )
        )

    def test_limit_arg(self):
        self.w("st/many2.txt", "\n".join("BBB" for _ in range(50)))
        out = self.grep("BBB", limit=10)
        self.assertEqual(len(out.split("\n")), 11)
        self.assertIn("[truncated: 10 matching lines shown, more exist;", out)

    def test_long_line_clipped(self):
        self.w("st/min.txt", "AAA" + "z" * 1200)
        out = self.grep("AAA")
        self.assertIn("…[+803 chars on this line]", out)
        self.assertLess(len(out), h.GREP_LINE_LIMIT + 100)

    def test_output_char_budget(self):
        self.w("st/wide.txt", "\n".join("AAA" + "y" * 300 for _ in range(200)))
        out = self.grep("AAA")
        self.assertIn(f"output capped at {h.GREP_TEXT_LIMIT} chars", out)
        self.assertLess(len(out), h.GREP_TEXT_LIMIT + 200)

    def test_path_is_file(self):
        self.w("st/g1.txt", "l1\nAAA mid\nl3\n")
        self.w("st/g2.txt", "AAA too\n")
        out = self.grep("AAA", path=os.path.join(self.tmp, "st", "g1.txt").replace("\\", "/"))
        self.assertEqual(out, "_test_tmp/st/g1.txt:2: AAA mid")

    def test_path_is_file_filtered_by_file_pattern(self):
        self.w("st/g1.txt", "AAA\n")
        out = self.grep(
            "AAA",
            path=os.path.join(self.tmp, "st", "g1.txt").replace("\\", "/"),
            file_pattern="*.md",
        )
        self.assertEqual(out, "no matches")

    def test_missing_path(self):
        self.w("st/g1.txt", "AAA\n")
        out = self.grep("AAA", path=os.path.join(self.tmp, "st", "nope.txt").replace("\\", "/"))
        self.assertEqual(out, "error: path not found: ./_test_tmp/st/nope.txt")


class TestGlob(Base):
    def glob(self, pattern, **kw):
        args = {"path": self.tmp, "pattern": pattern}
        args.update(kw)
        return h.tool_glob(args)

    def test_star(self):
        self.w("a.txt", "x")
        self.w("b.md", "x")
        self.assertEqual(self.glob("*.txt"), "_test_tmp/a.txt")

    def test_nested(self):
        self.w("src/deep/c.py", "x")
        self.assertEqual(self.glob("**/*.py"), "_test_tmp/src/deep/c.py")

    def test_double_star_also_matches_files_in_searched_dir(self):
        self.w("a.py", "x")
        self.w("src/deep/c.py", "x")
        self.assertEqual(self.glob("**/*.py"), "_test_tmp/a.py\n_test_tmp/src/deep/c.py")

    def test_no_match(self):
        self.w("a.txt", "x")
        self.assertEqual(self.glob("*.py"), "no files matched")

    def test_pycache_skipped(self):
        os.makedirs(os.path.join(self.tmp, "__pycache__"), exist_ok=True)
        with open(os.path.join(self.tmp, "__pycache__", "m.cpython-311.pyc"), "w") as f:
            f.write("x")
        self.assertEqual(self.glob("**/*.pyc"), "no files matched")

    def test_truncation(self):
        for i in range(600):
            self.w(f"many/f{i:03d}.py", "x")
        out = self.glob("**/*.py")
        self.assertEqual(len(out.split("\n")), 501)
        self.assertTrue(
            out.endswith(
                "... [truncated: 600 files, showing first 500; "
                "narrow the pattern, or raise limit]"
            )
        )

    def test_limit_arg(self):
        for i in range(10):
            self.w(f"few/f{i}.py", "x")
        out = self.glob("**/*.py", limit=4)
        self.assertEqual(len(out.split("\n")), 5)
        self.assertIn("[truncated: 10 files, showing first 4;", out)

    def test_path_is_file(self):
        self.w("a.txt", "x")
        self.w("b.md", "x")
        out = self.glob("*.txt", path=os.path.join(self.tmp, "a.txt").replace("\\", "/"))
        self.assertEqual(out, "_test_tmp/a.txt")

    def test_path_is_file_no_match(self):
        self.w("a.txt", "x")
        out = self.glob("*.md", path=os.path.join(self.tmp, "a.txt").replace("\\", "/"))
        self.assertEqual(out, "no files matched")

    def test_missing_path(self):
        self.w("a.txt", "x")
        out = self.glob("*.txt", path=os.path.join(self.tmp, "nope").replace("\\", "/"))
        self.assertEqual(out, "error: path not found: ./_test_tmp/nope")


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
        self.assertTrue(
            out.endswith(
                "... [truncated: 502 entries, showing first 500; raise limit, "
                "or glob the directory for a narrower match]"
            )
        )

    def test_list_dir_limit_arg(self):
        for i in range(20):
            with open(os.path.join(self.tmp, f"g{i:03d}.txt"), "w") as f:
                f.write("x")
        out = h.tool_list_dir({"path": self.tmp, "limit": 5})
        self.assertEqual(len(out.split("\n")), 6)
        self.assertIn("[truncated: 20 entries, showing first 5;", out)

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
        self.assertTrue(
            r.startswith(
                "exit code: 0\n"
                + "x" * 20_000
                + "\n... [truncated: 30000 chars total, showing first 20000; re-run with a larger "
                "max_output, or redirect to a file and read it with read_file]"
            )
        )

    def test_max_output_arg(self):
        r = self.sh("python -c \"import sys; sys.stdout.write('x'*3000)\"", max_output=500)
        self.assertTrue(r.startswith("exit code: 0\n" + "x" * 500))
        self.assertIn("[truncated: 3000 chars total, showing first 500;", r)

    def test_timeout(self):
        out = self.sh("python -c \"import time; time.sleep(2)\"", timeout=1)
        self.assertEqual(out, "error: command timed out after 1s")

    @unittest.skipUnless(sys.platform == "win32", "PowerShell only on Windows")
    def test_uses_powershell_on_windows(self):
        # Write-Output is PowerShell syntax; cmd.exe would fail to run it.
        out = self.sh("Write-Output 'hello-ps'")
        self.assertEqual(out, "exit code: 0\nhello-ps")

    @unittest.skipUnless(sys.platform == "win32", "PowerShell only on Windows")
    def test_powershell_utf8_output(self):
        # Non-ASCII must survive the shell round-trip (UTF-8 on pwsh 7, forced on 5.1).
        out = self.sh("Write-Output 'héllo'")
        self.assertEqual(out, "exit code: 0\nhéllo")


class TestResolveShell(Base):
    def setUp(self):
        super().setUp()
        self._saved_cache = dict(h._SHELL_CACHE)
        h._SHELL_CACHE.clear()

    def tearDown(self):
        h._SHELL_CACHE.clear()
        h._SHELL_CACHE.update(self._saved_cache)
        super().tearDown()

    @staticmethod
    def _which(found):
        # found: dict name -> path (missing names simply absent => None)
        return lambda name: found.get(name)

    def test_non_windows_uses_shell_true(self):
        with mock.patch.object(h.os, "name", "posix"):
            self.assertEqual(h._resolve_shell("auto"), (None, ""))

    def test_auto_prefers_pwsh(self):
        with mock.patch.object(h.shutil, "which", side_effect=self._which({"pwsh": "C:/pwsh.exe", "powershell": "C:/powershell.exe"})):
            argv, prefix = h._resolve_shell("auto")
        self.assertEqual(argv, ["C:/pwsh.exe", "-NoProfile", "-NonInteractive", "-Command"])
        # pwsh 7 can also emit the ANSI code page on redirected stdout, so
        # the UTF-8-forcing prefix is applied to it as well.
        self.assertIn("OutputEncoding", prefix)

    def test_auto_falls_back_to_powershell(self):
        with mock.patch.object(h.shutil, "which", side_effect=self._which({"powershell": "C:/powershell.exe"})):
            argv, prefix = h._resolve_shell("auto")
        self.assertEqual(argv, ["C:/powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("OutputEncoding", prefix)

    def test_auto_falls_back_to_cmd(self):
        with mock.patch.object(h.shutil, "which", side_effect=self._which({})):
            self.assertEqual(h._resolve_shell("auto"), (None, ""))

    def test_force_pwsh(self):
        with mock.patch.object(h.shutil, "which", side_effect=self._which({"pwsh": "C:/pwsh.exe"})):
            argv, prefix = h._resolve_shell("pwsh")
        self.assertEqual(argv[0], "C:/pwsh.exe")
        self.assertIn("OutputEncoding", prefix)

    def test_force_pwsh_missing_falls_back(self):
        with mock.patch.object(h.shutil, "which", side_effect=self._which({"powershell": "C:/powershell.exe"})):
            argv, prefix = h._resolve_shell("pwsh")
        self.assertEqual(argv[0], "C:/powershell.exe")
        self.assertIn("OutputEncoding", prefix)

    def test_force_powershell(self):
        with mock.patch.object(h.shutil, "which", side_effect=self._which({"powershell": "C:/powershell.exe"})):
            argv, prefix = h._resolve_shell("powershell")
        self.assertEqual(argv[0], "C:/powershell.exe")
        self.assertIn("OutputEncoding", prefix)

    def test_force_cmd(self):
        with mock.patch.object(h.shutil, "which", side_effect=self._which({"pwsh": "C:/pwsh.exe"})):
            self.assertEqual(h._resolve_shell("cmd"), (None, ""))


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
        note = (
            "\n... [truncated: 60000 chars total, showing first 50000; "
            "raise max_chars, or fetch a narrower URL]"
        )
        self.assertTrue(out.endswith(note))
        self.assertEqual(len(out), 50_000 + len(note))

    def test_max_chars_arg(self):
        out = self._fetch("y" * 5_000, max_chars=1_000)
        self.assertTrue(out.startswith("y" * 1_000))
        self.assertIn("[truncated: 5000 chars total, showing first 1000;", out)

    def test_empty(self):
        self.assertEqual(self._fetch(""), "(empty)")

    def test_registered(self):
        all_names = [s["function"]["name"] for s in h.OPENAI_TOOLS]
        interactive_names = [s["function"]["name"] for s in h.OPENAI_TOOLS_INTERACTIVE]
        self.assertIn("fetch_url", all_names)
        self.assertIn("fetch_url", interactive_names)
        self.assertIn("fetch_url", h.DISPATCH)


class TestOutputCaps(unittest.TestCase):
    """Two layers: model-facing limit args, and harness-side ceilings that clamp them."""

    def test_limit_arg_default_clamp_and_fallback(self):
        self.assertEqual(h._limit_arg({}, "limit", 200, 5000), 200)
        self.assertEqual(h._limit_arg({"limit": 5}, "limit", 200, 5000), 5)
        self.assertEqual(h._limit_arg({"limit": 99999}, "limit", 200, 5000), 5000)
        self.assertEqual(h._limit_arg({"limit": 0}, "limit", 200, 5000), 1)
        self.assertEqual(h._limit_arg({"limit": -3}, "limit", 200, 5000), 1)
        self.assertEqual(h._limit_arg({"limit": "42"}, "limit", 200, 5000), 42)
        self.assertEqual(h._limit_arg({"limit": "abc"}, "limit", 200, 5000), 200)
        self.assertEqual(h._limit_arg({"limit": None}, "limit", 200, 5000), 200)

    def test_truncate_helper(self):
        out = h._truncate("z" * 100, 50, hint="narrow it")
        self.assertTrue(out.startswith("z" * 50))
        self.assertEqual(
            out[50:], "\n... [truncated: 100 chars total, showing first 50; narrow it]"
        )

    def test_truncate_noop_when_under_limit(self):
        self.assertEqual(h._truncate("short", 50, hint="x"), "short")

    def test_cap_count_helper(self):
        out = h._cap_count([str(i) for i in range(10)], 3, unit="files", hint="narrow")
        self.assertEqual(out, "0\n1\n2\n... [truncated: 10 files, showing first 3; narrow]")

    def test_cap_count_noop_when_under_limit(self):
        self.assertEqual(h._cap_count(["a", "b"], 5, unit="files"), "a\nb")

    def test_clip_line_helper(self):
        self.assertEqual(h._clip_line("q" * 500, 100), "q" * 100 + " …[+400 chars on this line]")
        self.assertEqual(h._clip_line("short", 100), "short")

    def test_execute_tool_backstop_caps_any_result(self):
        h.DISPATCH["_huge"] = lambda args: "h" * (h.TOOL_RESULT_LIMIT + 5_000)
        self.addCleanup(h.DISPATCH.pop, "_huge", None)
        out = h.execute_tool("_huge", "{}")
        self.assertTrue(out.startswith("h" * h.TOOL_RESULT_LIMIT))
        self.assertIn(f"showing first {h.TOOL_RESULT_LIMIT};", out)

    def test_execute_tool_backstop_applies_to_mcp_path(self):
        class HugeClient:
            def call_tool(self, name, args):
                return "m" * (h.TOOL_RESULT_LIMIT + 9_000)

        h.MCP_DISPATCH["_huge_mcp"] = (HugeClient(), "huge")
        self.addCleanup(h.MCP_DISPATCH.pop, "_huge_mcp", None)
        out = h.execute_tool("_huge_mcp", "{}")
        self.assertTrue(out.startswith("m" * h.TOOL_RESULT_LIMIT))
        self.assertIn("[truncated:", out)

    def test_mcp_text_result_capped(self):
        out = h._mcp_content_to_text({"content": [{"type": "text", "text": "m" * 40_000}]})
        self.assertTrue(out.startswith("m" * h.MCP_RESULT_LIMIT))
        self.assertIn(f"showing first {h.MCP_RESULT_LIMIT};", out)

    def test_mcp_structured_content_capped(self):
        out = h._mcp_content_to_text({"structuredContent": {"blob": "s" * 40_000}})
        self.assertLess(len(out), h.MCP_RESULT_LIMIT + 200)
        self.assertIn("[truncated:", out)

    def test_limit_args_are_exposed_in_schemas(self):
        expected = {
            "grep": "limit",
            "glob": "limit",
            "list_dir": "limit",
            "read_file": "max_chars",
            "run_shell": "max_output",
            "fetch_url": "max_chars",
        }
        for name, arg in expected.items():
            props = h.TOOLS[name][0]["function"]["parameters"]["properties"]
            self.assertIn(arg, props, f"{name} schema should expose {arg}")

    def test_cap_constants_are_ordered(self):
        # The backstop must sit above every per-tool cap, otherwise a tool that
        # already trimmed gets a second truncation note appended.
        for cap in (h.READ_FILE_LIMIT, h.SHELL_OUTPUT_LIMIT, h.FETCH_TEXT_LIMIT,
                    h.MCP_RESULT_LIMIT, h.SUBAGENT_SUMMARY_LIMIT):
            self.assertLess(cap, h.TOOL_RESULT_LIMIT)
        self.assertLess(h.GREP_TEXT_LIMIT, h.TOOL_RESULT_LIMIT)


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
        self.assertTrue(
            out.endswith(
                "\n... [truncated: 25000 chars total, showing first 20000; "
                "AGENTS.md is long; trim the project instructions]"
            )
        )


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
        self._old_usage = dict(h.USAGE_BY_CONV)
        self._old_next_conv = h._NEXT_CONV_ID
        self._old_steps = h.SUBAGENT_STEP_LIMIT
        self._old_chat = h.chat
        h._AGENT_DEPTH = 0
        h.MAX_SUBAGENT_DEPTH = 3
        h.OUTPUT_INDENT = ""
        h.MODEL = "test-model"
        h.TEMPERATURE = 0.2
        h.SUBAGENT_STEP_LIMIT = 0  # guard off unless a test asks for it
        h.USAGE_BY_CONV.clear()

        def no_live_api(*args, **kwargs):
            raise AssertionError("tests must not reach a live LLM server")

        h.chat = no_live_api  # a non-streaming fallback would hit the real API
        self.addCleanup(setattr, h, "_AGENT_DEPTH", self._old_depth)
        self.addCleanup(setattr, h, "MAX_SUBAGENT_DEPTH", self._old_max)
        self.addCleanup(setattr, h, "OUTPUT_INDENT", self._old_indent)
        self.addCleanup(setattr, h, "MODEL", self._old_model)
        self.addCleanup(setattr, h, "TEMPERATURE", self._old_temp)
        self.addCleanup(setattr, h, "SUBAGENT_STEP_LIMIT", self._old_steps)
        self.addCleanup(setattr, h, "chat", self._old_chat)
        self.addCleanup(h.USAGE_BY_CONV.clear)
        self.addCleanup(h.USAGE_BY_CONV.update, self._old_usage)
        self.addCleanup(setattr, h, "_NEXT_CONV_ID", self._old_next_conv)

    def _call(self, name, args, call_id=None):
        return {
            "id": call_id or f"call-{name}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    def _tool_turn(self, name, args, call_id=None):
        """An assistant turn that asks for exactly one tool call."""
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [self._call(name, args, call_id)],
        }

    def _scripted(self, decide):
        """Install a scripted stream_once; decide(messages, depth, turn) -> (msg, streamed).

        Drives the *real* run_agent/_agent_loop, so depth scoping and the
        sub-agent plumbing are exercised for real. `turn` counts model turns
        per nesting level. Never returns streamed=False: that would make
        run_agent fall back to chat(), which these tests must not reach.
        """
        seen = {}
        calls = []

        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
            depth = h._AGENT_DEPTH
            calls.append(depth)
            turn = seen.get(depth, 0)
            seen[depth] = turn + 1
            return decide(messages, depth, turn)

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
        return messages, calls

    def _fake_run_agent(self, behavior):
        old = h.run_agent
        h.run_agent = behavior
        self.addCleanup(setattr, h, "run_agent", old)

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

    def test_task_summary_capped(self):
        def fake_run_agent(messages, model, interactive=False, temperature=0.2, depth=0, conv_id=None):
            messages.append({"role": "assistant", "content": "s" * 30_000})
            return 0

        old = h.run_agent
        h.run_agent = fake_run_agent
        self.addCleanup(setattr, h, "run_agent", old)
        out = h.tool_task({"task": "do X"})
        self.assertTrue(out.startswith("exit code: 0\n" + "s" * h.SUBAGENT_SUMMARY_LIMIT))
        self.assertIn(
            f"showing first {h.SUBAGENT_SUMMARY_LIMIT};",
            out,
        )

    def test_task_runs_nested_agent(self):
        calls = []

        def fake_run_agent(messages, model, interactive=False, temperature=0.2, depth=0, conv_id=None):
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
        def boom(messages, model, interactive=False, temperature=0.2, depth=0, conv_id=None):
            raise RuntimeError("nope")

        old = h.run_agent
        h.run_agent = boom
        self.addCleanup(setattr, h, "run_agent", old)
        with self.assertRaises(RuntimeError):
            h.tool_task({"task": "do X"})
        self.assertEqual(h.OUTPUT_INDENT, "")

    def test_task_no_final_content(self):
        def fake_run_agent(messages, model, interactive=False, temperature=0.2, depth=0, conv_id=None):
            messages.append({"role": "assistant", "content": ""})
            return 2

        old = h.run_agent
        h.run_agent = fake_run_agent
        self.addCleanup(setattr, h, "run_agent", old)
        self.assertEqual(h.tool_task({"task": "do X"}), "exit code: 2")

    def test_task_cleans_up_subagent_usage(self):
        def fake_run_agent(messages, model, interactive=False, temperature=0.2, depth=0, conv_id=None):
            # Simulate the sub-agent's API call recording usage under its
            # own conversation id.
            h._record_usage(messages, {"prompt_tokens": 999999}, conv_id)
            messages.append({"role": "assistant", "content": "done"})
            return 0

        old = h.run_agent
        h.run_agent = fake_run_agent
        self.addCleanup(setattr, h, "run_agent", old)
        h.tool_task({"task": "do X"})
        # The sub-agent's usage entry must not survive the run: its
        # messages list is freed, and a recycled address would resurrect
        # the stale entry in /status.
        self.assertEqual(h.USAGE_BY_CONV, {})

    def test_task_usage_cleaned_up_on_error(self):
        def boom(messages, model, interactive=False, temperature=0.2, depth=0, conv_id=None):
            h._record_usage(messages, {"prompt_tokens": 424242}, conv_id)
            raise RuntimeError("nope")

        old = h.run_agent
        h.run_agent = boom
        self.addCleanup(setattr, h, "run_agent", old)
        with self.assertRaises(RuntimeError):
            h.tool_task({"task": "do X"})
        self.assertEqual(h.USAGE_BY_CONV, {})

    # --- depth accounting: _AGENT_DEPTH is scoped to each run_agent call ---

    def test_task_depth_limit_message_names_the_cap(self):
        h.MAX_SUBAGENT_DEPTH = 2
        h._AGENT_DEPTH = 2
        out = h.tool_task({"task": "do something"})
        self.assertTrue(out.startswith("error: sub-agent depth limit reached"))
        self.assertIn("capped at 2 level", out)
        self.assertIn("level 3", out)  # what the refused call's depth would have been

    def test_nested_task_calls_do_not_strand_later_top_level_calls(self):
        """Regression: run_agent used to leave _AGENT_DEPTH at the depth its
        sub-agents reached, so a later top-level task call in the same turn was
        refused with 'sub-agent depth limit reached' even at depth 0."""
        h.MAX_SUBAGENT_DEPTH = 2

        def decide(messages, depth, turn):
            if depth == 0:
                if turn < 2:
                    return (self._tool_turn("task", {"task": f"top {turn}"}), True)
                return ({"role": "assistant", "content": "parent done"}, True)
            if turn == 0:
                return (self._tool_turn("task", {"task": "deeper"}), True)  # nests to level 2
            return ({"role": "assistant", "content": f"sub summary at depth {depth}"}, True)

        messages, calls = self._scripted(decide)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        results = [m["content"] for m in messages if m["role"] == "tool"]
        self.assertEqual(len(results), 2)  # both top-level delegations ran
        for result in results:
            self.assertTrue(result.startswith("exit code: 0\nsub summary at depth 1"), result)
        self.assertNotIn("depth limit", "".join(results))
        self.assertEqual(h._AGENT_DEPTH, 0)

    def test_run_agent_depth_seen_by_nested_runs_is_incremented(self):
        h.MAX_SUBAGENT_DEPTH = 3
        depths = []

        def decide(messages, depth, turn):
            depths.append(depth)
            if depth == 0 and turn == 0:
                return (self._tool_turn("task", {"task": "deeper"}), True)
            return ({"role": "assistant", "content": f"summary at depth {depth}"}, True)

        messages, _ = self._scripted(decide)
        with contextlib.redirect_stdout(io.StringIO()):
            h.run_agent(messages, "m")
        # 0: the parent's first turn, 1: the sub-agent's, 0: the parent's turn
        # after the sub-agent returned — the parent is back at level 0.
        self.assertEqual(depths, [0, 1, 0])
        self.assertEqual(h._AGENT_DEPTH, 0)

    def test_run_agent_restores_depth_after_exit_tool(self):
        depths = []

        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
            depths.append(h._AGENT_DEPTH)
            return (
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [self._call("exit", {"code": 7, "message": "done"}, "c1")],
                },
                True,
            )

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        with contextlib.redirect_stdout(io.StringIO()):
            code = h.run_agent([{"role": "user", "content": "go"}], "m", depth=2)
        self.assertEqual(code, 7)
        self.assertEqual(depths, [2])
        self.assertEqual(h._AGENT_DEPTH, 0)

    def test_run_agent_restores_depth_on_exception(self):
        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
            raise RuntimeError("sub-agent exploded")

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        with self.assertRaises(RuntimeError):
            with contextlib.redirect_stdout(io.StringIO()):
                h.run_agent([{"role": "user", "content": "go"}], "m", depth=1)
        self.assertEqual(h._AGENT_DEPTH, 0)

    def test_task_failure_does_not_corrupt_parent_depth(self):
        h.MAX_SUBAGENT_DEPTH = 2

        def decide(messages, depth, turn):
            if depth == 0:
                if turn == 0:
                    return (self._tool_turn("task", {"task": "x"}), True)
                return ({"role": "assistant", "content": "recovered"}, True)
            raise RuntimeError("sub-agent exploded")

        messages, calls = self._scripted(decide)
        with contextlib.redirect_stdout(io.StringIO()):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0, 1, 0])
        results = [m["content"] for m in messages if m["role"] == "tool"]
        self.assertEqual(results, ["error: RuntimeError: sub-agent exploded"])
        self.assertEqual(h._AGENT_DEPTH, 0)

    # --- a double ESC during a sub-agent stops the whole delegation chain ---

    def test_task_bubbles_up_subagent_interrupt(self):
        def fake_run_agent(messages, model, interactive=False, temperature=0.2, depth=0, conv_id=None):
            raise h.StreamInterrupted(
                {
                    "role": "assistant",
                    "content": "half an answer",
                    "tool_calls": [self._call("read_file", {"path": "a"})],
                }
            )

        self._fake_run_agent(fake_run_agent)
        with self.assertRaises(h.SubagentInterrupted) as cm:
            h.tool_task({"task": "do X"})
        content = cm.exception.message["content"]
        self.assertIn(h.SUBAGENT_INTERRUPT_PREFIX, content)
        self.assertIn("half an answer", content)
        self.assertEqual(h.OUTPUT_INDENT, "")
        self.assertEqual(h._AGENT_DEPTH, 0)
        self.assertEqual(h.USAGE_BY_CONV, {})

    def test_execute_tool_does_not_swallow_subagent_interrupt(self):
        def fake_run_agent(messages, model, interactive=False, temperature=0.2, depth=0, conv_id=None):
            raise h.StreamInterrupted({"role": "assistant", "content": "partial"})

        self._fake_run_agent(fake_run_agent)
        with self.assertRaises(h.SubagentInterrupted):
            h.execute_tool("task", json.dumps({"task": "do X"}))

    def test_interrupt_stops_the_whole_turn(self):
        h.MAX_SUBAGENT_DEPTH = 2

        def decide(messages, depth, turn):
            if depth == 0:
                return (
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            self._call("task", {"task": "delegate"}, "call-task"),
                            self._call("todo", {"action": "list"}, "call-todo"),
                        ],
                    },
                    True,
                )
            raise h.StreamInterrupted({"role": "assistant", "content": "half the work"})

        messages, calls = self._scripted(decide)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0, 1])  # the parent does not ask the LLM again
        answered = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
        # Every tool call in the batch is answered: a server rejects an
        # assistant message whose tool_calls were left dangling.
        self.assertEqual(sorted(answered), ["call-task", "call-todo"])
        self.assertIn(h.SUBAGENT_INTERRUPT_PREFIX, answered["call-task"])
        self.assertIn("half the work", answered["call-task"])
        self.assertEqual(answered["call-todo"], "interrupted by user")
        self.assertEqual(h._AGENT_DEPTH, 0)

    def test_interrupt_bubbles_through_nested_subagents(self):
        h.MAX_SUBAGENT_DEPTH = 3

        def decide(messages, depth, turn):
            if depth < 2:
                return (self._tool_turn("task", {"task": "deeper"}), True)
            raise h.StreamInterrupted({"role": "assistant", "content": "deepest partial work"})

        messages, calls = self._scripted(decide)
        with contextlib.redirect_stdout(io.StringIO()):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0, 1, 2])  # stopped all the way up, no retries
        results = [m["content"] for m in messages if m["role"] == "tool"]
        self.assertEqual(len(results), 1)
        self.assertIn("deepest partial work", results[0])
        # The note is carried up unchanged rather than wrapped once per level.
        self.assertEqual(results[0].count(h.SUBAGENT_INTERRUPT_PREFIX), 1)

    def test_top_level_interrupt_still_returns_to_the_prompt(self):
        """The top-level agent is not a sub-agent: an interrupt there keeps the
        long-standing behaviour (keep the partial, return 0)."""

        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
            raise h.StreamInterrupted({"role": "assistant", "content": "partial answer"})

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        messages = [{"role": "user", "content": "go"}]
        with contextlib.redirect_stdout(io.StringIO()):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(messages[-1], {"role": "assistant", "content": "partial answer"})
        self.assertEqual(h._AGENT_DEPTH, 0)

    # --- runaway guard: model turns allowed per sub-agent ---

    def test_subagent_step_limit_stops_a_runaway(self):
        h.MAX_SUBAGENT_DEPTH = 2
        h.SUBAGENT_STEP_LIMIT = 3
        depths = []

        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
            depths.append(h._AGENT_DEPTH)
            return (self._tool_turn("todo", {"action": "list"}), True)

        old = h.stream_once
        h.stream_once = fake_stream_once
        self.addCleanup(setattr, h, "stream_once", old)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = h.tool_task({"task": "never ends"})
        self.assertEqual(depths, [1, 1, 1])  # exactly SUBAGENT_STEP_LIMIT turns
        self.assertTrue(out.startswith(f"exit code: {h.SUBAGENT_STEP_LIMIT_CODE}"), out)
        self.assertIn("step limit", out)
        self.assertIn("3 model turns", out)
        self.assertIn("this summary may be incomplete", out)
        self.assertIn("sub-agent stopped", buf.getvalue())
        self.assertEqual(h._AGENT_DEPTH, 0)
        self.assertEqual(h.OUTPUT_INDENT, "")

    def test_step_limit_zero_disables_the_guard(self):
        h.MAX_SUBAGENT_DEPTH = 2
        h.SUBAGENT_STEP_LIMIT = 0
        turns = []

        def decide(messages, depth, turn):
            turns.append((depth, turn))
            if turn < 4:
                return (self._tool_turn("todo", {"action": "list"}), True)
            return ({"role": "assistant", "content": "sub done"}, True)

        self._scripted(decide)
        with contextlib.redirect_stdout(io.StringIO()):
            out = h.tool_task({"task": "keep going"})
        self.assertEqual(out, "exit code: 0\nsub done")
        self.assertEqual(len(turns), 5)

    def test_top_level_agent_is_not_step_capped(self):
        h.SUBAGENT_STEP_LIMIT = 2
        turns = []

        def decide(messages, depth, turn):
            turns.append(depth)
            if turn < 5:
                return (self._tool_turn("todo", {"action": "list"}), True)
            return ({"role": "assistant", "content": "done"}, True)

        messages, _ = self._scripted(decide)
        with contextlib.redirect_stdout(io.StringIO()):
            code = h.run_agent(messages, "m")
        self.assertEqual(code, 0)
        self.assertEqual(turns, [0] * 6)  # 5 tool turns + the finishing one, uncapped

    def test_model_exit_code_is_not_mistaken_for_the_step_limit(self):
        def fake_run_agent(messages, model, interactive=False, temperature=0.2, depth=0, conv_id=None):
            messages.append({"role": "assistant", "content": "done"})
            return h.SUBAGENT_STEP_LIMIT_CODE  # the sub-agent's own `exit` code collides

        self._fake_run_agent(fake_run_agent)
        out = h.tool_task({"task": "do X"})
        self.assertEqual(out, f"exit code: {h.SUBAGENT_STEP_LIMIT_CODE}\ndone")
        self.assertNotIn("step limit", out)

    def test_step_limit_signal_leaves_no_state_behind(self):
        h.MAX_SUBAGENT_DEPTH = 2
        h.SUBAGENT_STEP_LIMIT = 2

        def decide(messages, depth, turn):
            return (self._tool_turn("todo", {"action": "list"}), True)  # a sub-agent that never ends

        _, calls = self._scripted(decide)
        with contextlib.redirect_stdout(io.StringIO()):
            out = h.tool_task({"task": "never ends"})
        self.assertEqual(calls, [1, 1])  # the guard fired instead of looping on
        self.assertIn("step limit", out)
        self.assertEqual(h._AGENT_DEPTH, 0)
        self.assertEqual(h.OUTPUT_INDENT, "")
        self.assertEqual(h.USAGE_BY_CONV, {})

    def test_clamp_int_bounds_cli_values(self):
        self.assertEqual(h._clamp_int(2, 0, h.SUBAGENT_DEPTH_CEILING), 2)
        self.assertEqual(h._clamp_int(-1, 0, h.SUBAGENT_DEPTH_CEILING), 0)
        self.assertEqual(h._clamp_int(999, 0, h.SUBAGENT_DEPTH_CEILING), h.SUBAGENT_DEPTH_CEILING)
        self.assertEqual(h._clamp_int(-5, 0, h.SUBAGENT_STEPS_CEILING), 0)

    def test_run_agent_exit_returns_code(self):
        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
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


def _wrap_rows(text, width, prompt="you> "):
    """Wrap `text` the way a terminal does, for comparing against a rendered
    screen: the prompt shares the first row, every logical line is cut into
    width-column pieces, and a line ending exactly on the width needs no extra
    row (the wrap is pending). Independent of the code under test."""
    rows = []
    for i, ln in enumerate(text.split("\n")):
        s = (prompt if i == 0 else "") + ln
        rows += [s[j:j + width] for j in range(0, len(s), width)] or [""]
    return rows


class _MiniTerm:
    """A tiny terminal model used to verify the line editor's ANSI redraws
    produce the correct screen (no stale/duplicate lines, cursor in the right
    place). It understands the subset of escapes the editor emits: cursor
    moves (A/B/C/D), clear-line (2K), CR, LF (with scroll at the bottom), and
    printable characters. Cursor moves are clamped to the grid, as a real
    terminal's are.

    Soft wrap is *pending*, as in real terminals (measured on Windows with
    CONOUT$ + GetConsoleScreenBufferInfo): the character that fills a row
    leaves the cursor on that row's **last** column and the row below is only
    entered when the *next* character is written. A cursor move (or CR) acts on
    that cell and cancels the pending wrap without moving anywhere, which is
    what the editor's relative moves rely on."""

    def __init__(self, rows=24, cols=80):
        self.rows = rows
        self.cols = cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.r = 0
        self.c = 0
        self.pending = False

    def _scroll(self):
        del self.grid[0]
        self.grid.append([" "] * self.cols)
        self.r = self.rows - 1

    def _down(self, col=None):
        """Go to the next row (scrolling at the bottom); optionally set its column."""
        self.r += 1
        if col is not None:
            self.c = col
        if self.r >= self.rows:
            self._scroll()

    def write(self, s: str) -> "_MiniTerm":
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch == "\x1b" and i + 1 < n and s[i + 1] == "[":
                j = i + 2
                params = ""
                while j < n and (s[j].isdigit() or s[j] == ";"):
                    params += s[j]
                    j += 1
                if j < n:
                    letter = s[j]
                    val = int(params) if params else 1
                    if letter == "A":
                        self.r = max(0, self.r - val)
                    elif letter == "B":
                        self.r = min(self.rows - 1, self.r + val)
                    elif letter == "C":
                        self.c = min(self.cols - 1, self.c + val)
                    elif letter == "D":
                        self.c = max(0, self.c - val)
                    elif letter == "K" and val == 2:
                        self.grid[self.r] = [" "] * self.cols
                    if letter in "ABCD":
                        self.pending = False
                    i = j + 1
                    continue
                i = j
                continue
            if ch == "\r":
                self.c = 0
                self.pending = False
            elif ch == "\n":
                self._down()  # LF alone keeps the column; the editor emits \r\n
            else:
                if self.pending:  # the wrap the previous character deferred
                    self._down(0)
                    self.pending = False
                self.grid[self.r][self.c] = ch
                self.c += 1
                if self.c >= self.cols:
                    self.c = self.cols - 1
                    self.pending = True
            i += 1
        return self

    def lines(self):
        """Non-empty, right-stripped visible lines."""
        return ["".join(row).rstrip() for row in self.grid if "".join(row).strip()]


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
        # The newline is written as an explicit CRLF: in raw mode a bare LF only
        # moves down, which would leave the row shifted right by the column.
        self.assertIn("you> a\r\nb", out)

    def test_multiline_render_does_not_reprint_lines(self):
        keys = [("char", "a"), "newline", ("char", "b"), "enter"]
        line, out = self.edit(keys)
        self.assertEqual(line, "a\nb")
        # The final render must draw the text once and leave the cursor at the
        # end; the cursor is positioned with ANSI moves, not by re-printing the
        # text (which would re-draw the lines after the newline on every key).
        self.assertTrue(out.endswith("you> a\r\nb\r\n"))
        self.assertNotIn("you> a\r\nb\ryou> a", out)

    def _narrow_edit(self, keys, width=10):
        real = h.terminal_width
        h.terminal_width = lambda: width
        self.addCleanup(setattr, h, "terminal_width", real)
        return self.edit(keys)

    def _drive(self, keys, width=10, start=4, prompt="you> "):
        """Play `keys` through a mini terminal, render by render.

        Returns one snapshot per render the editor emitted (index 0 is the
        first, empty render; the last is the commit redraw): the buffer it was
        showing, the whole screen as visible rows, and the cursor cell. The
        buffer/pos pair is mirrored *here*, with the editing rules restated
        independently of the code under test, so a test can compare each render
        with the wrapped text that should have been painted."""
        old_width, old_auto = h.terminal_width, h.AUTO_SEND
        h.terminal_width = lambda: width
        h.AUTO_SEND = True
        buf, pos = [], 0

        def apply(token):
            nonlocal pos
            if isinstance(token, tuple) and token[0] == "char":
                buf.insert(pos, token[1])
                pos += 1
            elif token == "left":
                pos = max(0, pos - 1)
            elif token == "right":
                pos = min(len(buf), pos + 1)
            elif token == "home":
                pos = 0
            elif token == "end":
                pos = len(buf)
            elif token == "backspace":
                if pos:
                    del buf[pos - 1]
                    pos -= 1
            elif token == "delete":
                if pos < len(buf):
                    del buf[pos]
            elif token == "newline":
                buf.insert(pos, "\n")
                pos += 1
            elif token == "ctrl_u":
                del buf[:pos]
                pos = 0

        class _Recorder:
            def __init__(self):
                self.parts = []

            def write(self, s):
                self.parts.append(s)

            def flush(self):
                pass

        rec = _Recorder()
        expected = [(0, "")]  # the prompt is rendered before the first key
        if not keys or keys[-1] not in ("enter", "ctrl_enter", "ctrl_d"):
            keys = list(keys) + ["enter"]  # the editor only exits on a send key

        def feed():
            prev = None
            for k in keys:
                if prev is not None:
                    # The editor has just applied `prev`, rendered, and is
                    # asking for the next key.
                    apply(prev)
                    expected.append((pos, "".join(buf)))
                yield k
                prev = k

        try:
            from contextlib import redirect_stdout
            with redirect_stdout(rec):
                line = h._edit_line(prompt, feed())
        finally:
            h.terminal_width, h.AUTO_SEND = old_width, old_auto

        committed = bool(keys) and keys[-1] in ("enter", "ctrl_enter", "ctrl_d")
        if committed:
            expected.append((pos, "".join(buf)))  # the commit redraw
        if len(rec.parts) != len(expected):
            self.fail(f"{len(rec.parts)} renders for {len(expected)} key states")

        term = _MiniTerm(rows=24, cols=width)
        term.r = start
        snaps = []
        last = len(expected) - 1
        for i, ((p, buffer), chunk) in enumerate(zip(expected, rec.parts)):
            term.write(chunk)
            snaps.append({
                "buffer": buffer,
                "pos": p,
                "rows": ["".join(r).rstrip() for r in term.grid],
                "r": term.r,
                "c": term.c,
                "commit": i == last and committed,
            })
        self.assertEqual(line, "".join(buf))
        return snaps

    def _assert_anchored(self, snaps, width=10, start=4, prompt="you> "):
        """Every render must paint the buffer on the same rows: the block may
        not drift up or down, and no row outside it may hold text (the reported
        bug: the cursor walk-back overshot and the block crept one row per
        keystroke, reprinting over the output above and duplicating its tail)."""
        for s in snaps:
            want = _wrap_rows(s["buffer"], width, prompt)
            tag = f"buffer {s['buffer']!r} pos {s['pos']}"
            self.assertEqual(
                s["rows"][start:start + len(want)],
                [row.rstrip() for row in want],
                f"{tag}: the block is not where the first render put it",
            )
            outside = [i for i, row in enumerate(s["rows"])
                       if row.strip() and not start <= i < start + len(want)]
            self.assertEqual(outside, [], f"{tag}: stale text on rows {outside}")
            if s["commit"]:
                # The submitted line ends on its last row; the cursor drops onto
                # the fresh row right below it, so the next output can't print
                # over the line.
                self.assertEqual(s["r"], start + len(want), f"{tag}: cursor after commit")
            else:
                self.assertTrue(start <= s["r"] < start + len(want), f"{tag}: cursor row {s['r']}")

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
        # the end (row 1 col 7) up to row 0 col 9 (one line UP, two cols
        # right). A down-move here was the reported "re-print the previous
        # line on every keypress" bug.
        self.assertIn("\x1b[1A\x1b[2C", out)
        self.assertNotIn("\x1b[1B\x1b[2C", out)

    def test_wrapped_line_stays_anchored_while_arrowing_left(self):
        # Reported bug: 32 chars in a 10-column terminal make a 4-row block.
        # Arrowing left was fine until the caret crossed onto a row above the
        # block's last one; from there every keystroke reprinted the whole
        # block one row higher and left its old tail rows behind as duplicates.
        snaps = self._drive([("char", c) for c in "abcdefghijklmnopqrstuvwxyz012345"]
                            + ["left"] * 32)
        self._assert_anchored(snaps)

    def test_wrapped_line_stays_anchored_with_typing_and_arrows(self):
        keys = ([("char", c) for c in "abcdefghijklmnopqrstuvwxyz012345"]
                + ["left"] * 20
                + [("char", c) for c in "XY"]
                + ["home", "right", "right", "left", "backspace", "end", "left", "delete"])
        snaps = self._drive(keys + ["enter"])
        self.assertEqual(snaps[-1]["buffer"], "bcdefghijklXYmnopqrstuvwxyz01234")
        self._assert_anchored(snaps)

    def test_multiline_block_stays_anchored_across_lines(self):
        # A real newline plus a soft wrap: 3 rows. Arrowing between the rows and
        # editing there must not shift the block either.
        keys = ([("char", c) for c in "01234567"]        # rows 0-1
                + ["newline", ("char", "x"), ("char", "y")]  # row 2
                + ["left"] * 7
                + [("char", "Z"), "left", ("char", "W"), "backspace", "home", "end"])
        snaps = self._drive(keys + ["enter"])
        self.assertEqual(snaps[-1]["buffer"], "0123Z4567\nxy")
        self._assert_anchored(snaps)

    def test_soft_wrap_boundary_cursor(self):
        # Prompt (5) + 6 chars: the caret at the end sits at row 1 col 1. One
        # left moves it to character position 5, whose cell is on the wrap
        # boundary — a terminal draws that in row 0's LAST column (the wrap is
        # pending there), so the editor moves up one row and 8 columns right.
        snaps = self._drive([("char", c) for c in "012345"] + ["left"] + ["enter"])
        self._assert_anchored(snaps)
        self.assertEqual((snaps[-2]["r"], snaps[-2]["c"]), (4, 9))
        # ...and a character typed there lands at the wrap point: the caret sat
        # at the end of row 0, so the text breaks exactly there.
        snaps = self._drive([("char", c) for c in "012345"] + ["left", ("char", "X")])
        self._assert_anchored(snaps)
        self.assertEqual(snaps[-2]["buffer"], "01234X5")
        self.assertEqual(snaps[-2]["rows"][4:7], ["you> 01234", "X5", ""])

    def test_soft_wrap_exact_boundary_cursor(self):
        # Prompt (5) + 5 chars fills row 0 exactly. A terminal cannot draw the
        # caret in column 10, so it stays in the last cell of row 0 (pending
        # wrap): the caret cell of position 5 and of position 4 is the same, and
        # a left-arrow from the end moves nothing at all.
        snaps = self._drive([("char", c) for c in "01234"] + ["left"] * 2 + ["enter"])
        self._assert_anchored(snaps)
        # (row, col) after typing the 5th char, then after the 1st and 2nd left.
        self.assertEqual([(s["r"], s["c"]) for s in snaps[5:7]], [(4, 9), (4, 9)])
        self.assertEqual((snaps[7]["r"], snaps[7]["c"]), (4, 8))

    def test_soft_wrap_then_newline_clears_all_rows(self):
        # Width 10, prompt "you> " (5 cols): 8 chars wrap to rows 0-1, and a
        # Ctrl+J newline drops the cursor to row 2. The next keystroke must
        # clear all three physical rows (move up 2), not just two, or the
        # stale first line is re-printed on every key.
        keys = [("char", c) for c in "01234567"] + ["newline", ("char", "x"), "enter"]
        line, out = self._narrow_edit(keys)
        self.assertEqual(line, "01234567\nx")
        self.assertIn(
            "\x1b[2A\r\x1b[2K\x1b[B\x1b[2K\x1b[B\x1b[2K\x1b[2Ayou> 01234567\r\nx", out
        )
        self.assertTrue(out.endswith("you> 01234567\r\nx\r\n"))

    def test_commit_with_caret_mid_block_leaves_no_stale_rows(self):
        # Submitting with the caret parked mid-block must redraw the line from
        # its first row and land the cursor on the row below the block, or the
        # agent's reply would print over the leftover rows of the input.
        snaps = self._drive([("char", c) for c in "0123456789ab"] + ["left"] * 9 + ["enter"])
        self._assert_anchored(snaps)
        last = snaps[-1]
        self.assertEqual(last["rows"][4:6], ["you> 01234", "56789ab"])
        self.assertEqual(last["r"], 6)
        self.assertEqual([i for i, row in enumerate(last["rows"]) if row.strip()], [4, 5])

    def test_multiline_cursor_does_not_reprint_previous_line(self):
        # Reported bug: with a multi-line buffer, arrowing left from the last
        # line onto an earlier line used to emit a cursor-DOWN move (the row
        # delta was inverted), so the cursor drifted one row below the text
        # and every following keypress re-printed the previous line. Play the
        # whole session through a terminal model and assert the final screen
        # shows the buffer exactly once, with no duplicated line.
        keys = (
            [("char", c) for c in "01234567"]            # wraps to rows 0-1 (width 10)
            + ["newline", ("char", "x"), ("char", "y")]  # + newline -> "xy" on row 2
            + ["left", "left", "left"]                   # cursor up onto row 1
            + [("char", "Z"), "left"]                    # more keys while on row 1
            + ["enter"]
        )
        line, out = self._narrow_edit(keys, width=10)
        self.assertEqual(line, "01234567Z\nxy")
        term = _MiniTerm(rows=24, cols=10).write(out)
        self.assertEqual(term.lines(), ["you> 01234", "567Z", "xy"])

    def test_edit_rows_counts_physical_rows(self):
        # Rows the prompt + text paints: ceiling division per logical line (a
        # line that soft-wraps without ending on a boundary still takes the next
        # row), an empty line still counts as one row.
        self.assertEqual(h._edit_rows("", 5, 10), 1)
        self.assertEqual(h._edit_rows("01234", 5, 10), 1)         # fills row 0 exactly
        self.assertEqual(h._edit_rows("012345", 5, 10), 2)
        self.assertEqual(h._edit_rows("0123456789ab", 5, 10), 2)  # 17 cols
        self.assertEqual(h._edit_rows("0123456789ab", 5, 10), 2)
        self.assertEqual(h._edit_rows("0123456789ab\nx", 5, 10), 3)
        self.assertEqual(h._edit_rows("abc\n", 5, 10), 2)

    def test_edit_cur_pending_boundary(self):
        self.assertEqual(h._edit_cur(0, 0, 10), (0, 0))
        self.assertEqual(h._edit_cur(0, 9, 10), (0, 9))
        self.assertEqual(h._edit_cur(0, 10, 10), (0, 9))   # filled: caret still on row 0
        self.assertEqual(h._edit_cur(0, 11, 10), (1, 1))
        self.assertEqual(h._edit_cur(0, 20, 10), (1, 9))   # two filled rows
        self.assertEqual(h._edit_cur(0, 21, 10), (2, 1))
        self.assertEqual(h._edit_cur(3, 7, 10), (3, 7))

    def test_edit_phys_pos_pending_wrap(self):
        self.assertEqual(h._edit_phys_pos("0123456789ab", 5, 12, 10), (1, 7))
        self.assertEqual(h._edit_phys_pos("01234", 5, 5, 10), (0, 9))
        self.assertEqual(h._edit_phys_pos("012345", 5, 5, 10), (0, 9))    # on the boundary
        self.assertEqual(h._edit_phys_pos("012345", 5, 6, 10), (1, 1))
        self.assertEqual(h._edit_phys_pos("0123456789", 5, 10, 10), (1, 5))
        # After a newline the column base is 0 again, on the row after the rows
        # the wrapped line occupied.
        self.assertEqual(h._edit_phys_pos("01234567\nxy", 5, 9, 10), (2, 0))
        self.assertEqual(h._edit_phys_pos("01234567\nxy", 5, 11, 10), (2, 2))

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

    def test_up_moves_caret_within_multiline_before_history(self):
        # A multi-line draft: up moves the caret up line-by-line (column
        # preserved) and does not touch the buffer until the caret reaches
        # the first line and up is pressed once more.
        h.HISTORY = ["cmd1", "cmd2"]
        keys = (
            [("char", "a"), "newline", ("char", "b"), "newline", ("char", "c")]
            + ["up", "up"]  # caret walks from line 3 to line 1 (column kept)
            + [("char", "X"), "enter"]  # types at the end of the first line
        )
        self.assertEqual(self.edit(keys)[0], "aX\nb\nc")

    def test_up_column_clamped_to_line_above(self):
        # Caret at column 4 of "bbbb": up lands at the end of the shorter
        # line "a", not beyond it.
        keys = [
            ("char", "a"), "newline",
            ("char", "b"), ("char", "b"), ("char", "b"), ("char", "b"),
            "up", ("char", "X"), "enter",
        ]
        self.assertEqual(self.edit(keys)[0], "aX\nbbbb")

    def test_up_from_first_line_recalls_history_down_restores_draft(self):
        # Up from the first line shows the previous command, but keeps the
        # draft: the first down returns to it, with the caret where it was.
        h.HISTORY = ["cmd1", "cmd2"]
        keys = (
            [("char", "a"), "newline", ("char", "b"), "newline", ("char", "c")]
            + ["up", "up"]  # caret to the first line (end of "a")
            + ["up"]  # recalls "cmd2" (draft saved, caret at end of line 1)
            + ["down"]  # back to the draft, caret where it was
            + [("char", "X"), "enter"]
        )
        self.assertEqual(self.edit(keys)[0], "aX\nb\nc")

    def test_down_restores_draft_after_browsing_further_up(self):
        # Browsing up through several entries, then a single down returns to
        # the saved draft (not to the entry just above it).
        h.HISTORY = ["cmd1", "cmd2"]
        keys = (
            [("char", "a"), "newline", ("char", "b")]
            + ["up"]  # caret to the first line (end of "a")
            + ["up", "up"]  # recalls "cmd2", then "cmd1"
            + ["down"]  # a single down returns to the saved draft
            + [("char", "X"), "enter"]
        )
        self.assertEqual(self.edit(keys)[0], "aX\nb")

    def test_editing_history_entry_discards_draft(self):
        # Editing the recalled entry discards the draft; down then walks
        # history forward as usual (clearing the line at the end).
        h.HISTORY = ["cmd1", "cmd2"]
        keys = (
            [("char", "a"), "newline", ("char", "b")]
            + ["up"]  # caret to the first line
            + ["up", "up"]  # recalls "cmd2", then "cmd1"
            + [("char", "X")]  # edits the recalled entry -> "cmd1X" (draft gone)
            + ["down", "down"]  # walks history forward: "cmd2", then empty
            + ["enter"]
        )
        self.assertEqual(self.edit(keys)[0], "")

    def test_down_moves_caret_within_multiline(self):
        # Symmetric to up: with the caret above the last line, down moves it
        # down within the buffer instead of touching history.
        keys = (
            [("char", "a"), "newline", ("char", "b"), "newline", ("char", "c")]
            + ["home"]  # caret at line 1 col 0
            + ["down"]  # to line 2 col 0
            + [("char", "X"), "enter"]
        )
        self.assertEqual(self.edit(keys)[0], "a\nXb\nc")

    def test_up_down_multiline_without_history(self):
        # No history: up/down just move the caret within the draft.
        keys = (
            [("char", "a"), "newline", ("char", "b")]
            + ["up", "down", "enter"]
        )
        self.assertEqual(self.edit(keys)[0], "a\nb")

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


class TestAutoSend(Base):
    def setUp(self):
        super().setUp()
        self._old_auto_send = h.AUTO_SEND
        h.AUTO_SEND = True
        self.addCleanup(setattr, h, "AUTO_SEND", self._old_auto_send)
        self._old_history = h.HISTORY
        h.HISTORY = []
        self.addCleanup(setattr, h, "HISTORY", self._old_history)

    def edit(self, keys):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            line = h._edit_line("you> ", iter(keys))
        return line, buf.getvalue()

    def test_default_is_on(self):
        self.assertTrue(h.AUTO_SEND)

    def test_enter_submits_when_on(self):
        self.assertEqual(self.edit([("char", "a"), "enter"])[0], "a")

    def test_ctrl_enter_submits_when_on(self):
        self.assertEqual(self.edit([("char", "a"), "ctrl_enter"])[0], "a")

    def test_enter_inserts_newline_when_off(self):
        h.AUTO_SEND = False
        line, _ = self.edit([("char", "a"), "enter", ("char", "b"), "ctrl_enter"])
        self.assertEqual(line, "a\nb")

    def test_ctrl_enter_submits_when_off(self):
        h.AUTO_SEND = False
        self.assertEqual(self.edit([("char", "a"), "ctrl_enter"])[0], "a")

    def test_ctrl_d_still_sends_when_off(self):
        h.AUTO_SEND = False
        self.assertEqual(self.edit([("char", "a"), "ctrl_d"])[0], "a")

    def test_newline_token_still_inserts_newline_when_off(self):
        h.AUTO_SEND = False
        line, _ = self.edit([("char", "a"), "newline", ("char", "b"), "ctrl_enter"])
        self.assertEqual(line, "a\nb")

    def test_set_auto_send_off(self):
        msg = h.set_auto_send("off")
        self.assertFalse(h.AUTO_SEND)
        self.assertIn("auto-send off", msg)

    def test_set_auto_send_on(self):
        h.AUTO_SEND = False
        msg = h.set_auto_send("ON")
        self.assertTrue(h.AUTO_SEND)
        self.assertIn("auto-send on", msg)

    def test_set_auto_send_no_arg_reports_state(self):
        h.AUTO_SEND = False
        self.assertEqual(h.set_auto_send(""), "auto-send: off")
        self.assertEqual(h.set_auto_send("   "), "auto-send: off")
        h.AUTO_SEND = True
        self.assertEqual(h.set_auto_send(""), "auto-send: on")

    def test_set_auto_send_bogus(self):
        self.assertEqual(h.set_auto_send("maybe"), "")


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

    def test_cr_is_enter_when_ctrl_not_held(self):
        with mock.patch.object(h, "_windows_ctrl_held", return_value=False):
            self.assertEqual(self.first(["\r"]), "enter")

    def test_cr_is_ctrl_enter_when_ctrl_held(self):
        with mock.patch.object(h, "_windows_ctrl_held", return_value=True):
            self.assertEqual(self.first(["\r"]), "ctrl_enter")


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

    def test_ctrl_enter_sequences(self):
        self.assertEqual(self.feed("13;5u"), "ctrl_enter")
        self.assertEqual(self.feed("27;5;13~"), "ctrl_enter")

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

    def test_home_config_path(self):
        self.assertEqual(
            h.MCP_HOME_CONFIG,
            os.path.join(os.path.expanduser("~"), ".harnless", "mcp.json"),
        )

    def test_load_into(self):
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"a": {"transport": "http", "url": "http://a"}}}, f)
        servers = {}
        h._load_mcp_config_into(servers, path)
        self.assertEqual(servers, {"a": {"transport": "http", "url": "http://a"}})

    def test_load_into_later_wins(self):
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"a": {"transport": "http", "url": "http://a"}}}, f)
        servers = {"a": {"transport": "http", "url": "http://old"}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            h._load_mcp_config_into(servers, path)
        self.assertEqual(servers["a"]["url"], "http://a")
        self.assertIn("duplicate mcp server name 'a'", buf.getvalue())

    def test_load_into_bad_file(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        servers = {}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            h._load_mcp_config_into(servers, path)
        self.assertEqual(servers, {})
        self.assertIn("failed to load mcp config", buf.getvalue())

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


class TestMcpPending(Base):
    def setUp(self):
        super().setUp()
        self._saved = (h.MCP_TOOLS, h.MCP_DISPATCH, h.MCP_CLIENTS, h.PENDING_MCP)
        h.MCP_TOOLS = []
        h.MCP_DISPATCH = {}
        h.MCP_CLIENTS = []
        h.PENDING_MCP = []

    def tearDown(self):
        h.MCP_TOOLS, h.MCP_DISPATCH, h.MCP_CLIENTS, h.PENDING_MCP = self._saved
        super().tearDown()

    def test_register_mcp_client_connects(self):
        server, port, _ = _fake_mcp_server("pw")
        try:
            c = h.MCPClient("s", {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"})
            ok = h._register_mcp_client(c)
            self.assertTrue(ok)
            self.assertEqual([s["function"]["name"] for s in h.MCP_TOOLS], ["pw"])
            self.assertIn("pw", h.MCP_DISPATCH)
            self.assertEqual(h.MCP_CLIENTS, [c])
        finally:
            server.shutdown()
            server.server_close()

    def test_register_mcp_client_connect_fail(self):
        c = h.MCPClient("s", {"transport": "http", "url": "http://127.0.0.1:1/mcp"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = h._register_mcp_client(c)
        self.assertFalse(ok)
        self.assertEqual(h.MCP_TOOLS, [])
        self.assertEqual(h.MCP_CLIENTS, [c])

    def test_enable_pending_mcp(self):
        server, port, _ = _fake_mcp_server("pw")
        try:
            h.PENDING_MCP.append(("s", {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"}))
            ok, msg = h._enable_pending_mcp("s")
            self.assertTrue(ok)
            self.assertEqual(h.PENDING_MCP, [])
            self.assertEqual([s["function"]["name"] for s in h.MCP_TOOLS], ["pw"])
        finally:
            server.shutdown()
            server.server_close()

    def test_enable_pending_mcp_unknown(self):
        ok, msg = h._enable_pending_mcp("nope")
        self.assertFalse(ok)
        self.assertIn("not pending", msg)

    def test_enable_pending_mcp_stays_on_fail(self):
        h.PENDING_MCP.append(("s", {"transport": "http", "url": "http://127.0.0.1:1/mcp"}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok, msg = h._enable_pending_mcp("s")
        self.assertFalse(ok)
        self.assertEqual(len(h.PENDING_MCP), 1)  # stays pending so the user can retry

    def test_tools_menu_enables_pending(self):
        server, port, _ = _fake_mcp_server("pw")
        try:
            h.PENDING_MCP.append(("pwserver", {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"}))
            # rows = built-ins + one row per mcp server + one row per mcp tool + pending
            n_servers = len({client.name for _, (client, _) in h.MCP_DISPATCH.items()})
            n_rows = len(h.OPENAI_TOOLS_INTERACTIVE) + n_servers + len(h.MCP_TOOLS)
            keys = ["down"] * n_rows + [("char", " "), "enter"]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                applied = h.tools_menu(iter(keys))
            self.assertTrue(applied)
            self.assertEqual(h.PENDING_MCP, [])
            self.assertIn("pw", [s["function"]["name"] for s in h.MCP_TOOLS])
        finally:
            server.shutdown()
            server.server_close()

    def test_tools_menu_rendering_shows_pending(self):
        h.PENDING_MCP.append(("pwserver", {"transport": "http", "url": "http://x/mcp"}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            h.tools_menu(iter(["enter"]))
        out = buf.getvalue()
        self.assertIn("mcp servers (disabled in config):", out)
        self.assertIn("[ ] pwserver", out)


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


class TestMcpStdioShellShim(Base):
    """Windows: commands resolving to .cmd/.bat shims (npx, npm, uvx) must be
    spawned with shell=True — CreateProcess (shell=False) can't launch them
    and raises FileNotFoundError [WinError 2]."""

    def _capture_popen(self, client):
        import subprocess

        calls = []
        real = subprocess.Popen

        def fake(args, **kw):
            calls.append((args, kw))
            raise RuntimeError("stop")

        self.addCleanup(setattr, subprocess, "Popen", real)
        subprocess.Popen = fake
        try:
            client.connect()
        except RuntimeError:
            pass
        return calls

    def test_cmd_shim_uses_shell(self):
        if os.name != "nt":
            self.skipTest("Windows-only")
        shim = os.path.join(self.tmp, "shim.cmd")
        with open(shim, "w") as f:
            f.write("@echo off\n")
        calls = self._capture_popen(
            h.MCPClient("shim", {"transport": "stdio", "command": shim, "args": ["x"]})
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].get("shell"))

    def test_real_exe_no_shell(self):
        calls = self._capture_popen(
            h.MCPClient("py", {"transport": "stdio", "command": sys.executable, "args": []})
        )
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1].get("shell"))


def _fake_mcp_server(tool_name):
    """Start a fake Streamable-HTTP MCP server that lists one tool. Returns (server, port, thread)."""
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
                result = {"tools": [{"name": tool_name, "description": "fake", "inputSchema": {"type": "object", "properties": {}}}]}
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
    return server, port, t


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

    def test_cli_home_config_auto_loaded(self):
        """Regression: ~/.harnless/mcp.json must be auto-loaded at startup."""
        import subprocess

        server, port, _ = _fake_mcp_server("add")
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".harnless"))
        with open(os.path.join(home, ".harnless", "mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"fake": {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"}}}, f)
        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "harnless.py",
                    "--no-color",
                    "--context-window", "1000",
                ],
                input="/tools\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("[X] add", proc.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_flag_overrides_home_config(self):
        """Regression: --mcp-config definitions must override the home config on name collision."""
        import subprocess

        server_a, port_a, _ = _fake_mcp_server("add")
        server_b, port_b, _ = _fake_mcp_server("sub")
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".harnless"))
        with open(os.path.join(home, ".harnless", "mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"fake": {"transport": "http", "url": f"http://127.0.0.1:{port_a}/mcp"}}}, f)
        cfg = os.path.join(self.tmp, "override.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"fake": {"transport": "http", "url": f"http://127.0.0.1:{port_b}/mcp"}}}, f)
        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "harnless.py",
                    "--no-color",
                    "--context-window", "1000",
                    "--mcp-config", cfg,
                ],
                input="/tools\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("[X] sub", proc.stdout)
            self.assertNotIn("[X] add", proc.stdout)
        finally:
            server_a.shutdown()
            server_a.server_close()
            server_b.shutdown()
            server_b.server_close()

    def test_cli_enabled_false_not_connected(self):
        """Regression: a server with enabled:false is not connected at startup."""
        import subprocess

        server, port, _ = _fake_mcp_server("pw")
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".harnless"))
        with open(os.path.join(home, ".harnless", "mcp.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"mcpServers": {"fake": {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp", "enabled": False}}},
                f,
            )
        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "harnless.py",
                    "--no-color",
                    "--context-window", "1000",
                ],
                input="/status\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("mcp servers (disabled)", proc.stdout)
            self.assertIn("fake (disabled in config; enable via /tools)", proc.stdout)
            tools_line = [l for l in proc.stdout.splitlines() if l.startswith("tools:")]
            self.assertTrue(tools_line)
            self.assertNotIn("pw", tools_line[0])
        finally:
            server.shutdown()
            server.server_close()


class TestToolsToggle(unittest.TestCase):
    def setUp(self):
        self._old_disabled = h.DISABLED_TOOLS
        h.DISABLED_TOOLS = set()
        self.addCleanup(setattr, h, "DISABLED_TOOLS", self._old_disabled)
        self._old_mcp = (h.MCP_TOOLS, h.MCP_DISPATCH, h.MCP_CLIENTS, h.PENDING_MCP)
        h.MCP_TOOLS = []
        h.MCP_DISPATCH = {}
        h.MCP_CLIENTS = []
        h.PENDING_MCP = []
        self.addCleanup(self._restore_mcp)

    def _restore_mcp(self):
        h.MCP_TOOLS, h.MCP_DISPATCH, h.MCP_CLIENTS, h.PENDING_MCP = self._old_mcp

    def _register(self, server, tools):
        c = h.MCPClient(server, {"transport": "stdio", "command": "x"})
        c.connect = lambda: {}
        c.list_tools = lambda: tools
        c.close = lambda: None
        h.register_mcp_tools([c])
        return c

    def _mcp_tool(self, name, desc="d"):
        return {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": {}}}

    def test_format_tools_all_on(self):
        out = h.format_tools()
        lines = out.split("\n")
        self.assertTrue(lines)
        for l in lines:
            if l.lstrip().startswith("["):
                self.assertTrue(l.lstrip().startswith("[X] "), l)
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

    def test_toggle_mcp_server_prefix(self):
        self._register("srv", [self._mcp_tool("t1"), self._mcp_tool("t2")])
        self.assertEqual(h.toggle_tools(["mcp:srv"]), [("mcp:srv", "off")])
        self.assertEqual(h.DISABLED_TOOLS, {"t1", "t2"})
        self.assertEqual(h.toggle_tools(["mcp:srv"]), [("mcp:srv", "on")])
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_toggle_mcp_server_prefix_mixed(self):
        self._register("srv", [self._mcp_tool("t1"), self._mcp_tool("t2")])
        h.DISABLED_TOOLS.add("t2")
        self.assertEqual(h.toggle_tools(["mcp:srv"]), [("mcp:srv", "on")])
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_toggle_mcp_server_prefix_unknown(self):
        self.assertEqual(h.toggle_tools(["mcp:nope"]), [("mcp:nope", "unknown")])
        self.assertEqual(h.DISABLED_TOOLS, set())

    def test_format_tools_groups_mcp(self):
        self._register("srv", [self._mcp_tool("t1", "d1"), self._mcp_tool("t2", "d2")])
        out = h.format_tools()
        lines = out.split("\n")
        self.assertIn("mcp servers:", lines)
        self.assertIn("[X] mcp: srv (2 tools)", lines)
        self.assertIn("  [X] t1 — d1", lines)
        self.assertIn("  [X] t2 — d2", lines)
        self.assertLess(lines.index("[X] mcp: srv (2 tools)"), lines.index("  [X] t1 — d1"))

    def test_format_tools_mcp_mixed_mark(self):
        self._register("srv", [self._mcp_tool("t1", "d1"), self._mcp_tool("t2", "d2")])
        h.DISABLED_TOOLS.add("t2")
        out = h.format_tools()
        self.assertIn("[-] mcp: srv (2 tools)", out)
        self.assertIn("  [X] t1 — d1", out)
        self.assertIn("  [ ] t2 — d2", out)

    def test_format_tools_mcp_all_off_mark(self):
        self._register("srv", [self._mcp_tool("t1"), self._mcp_tool("t2")])
        h.DISABLED_TOOLS.update({"t1", "t2"})
        out = h.format_tools()
        self.assertIn("[ ] mcp: srv (2 tools)", out)


class TestToolsMenu(unittest.TestCase):
    def setUp(self):
        self._old_disabled = h.DISABLED_TOOLS
        h.DISABLED_TOOLS = set()
        self.addCleanup(setattr, h, "DISABLED_TOOLS", self._old_disabled)
        self._old_mcp = (h.MCP_TOOLS, h.MCP_DISPATCH, h.MCP_CLIENTS, h.PENDING_MCP)
        h.MCP_TOOLS = []
        h.MCP_DISPATCH = {}
        h.MCP_CLIENTS = []
        h.PENDING_MCP = []
        self.addCleanup(self._restore_mcp)

    def _restore_mcp(self):
        h.MCP_TOOLS, h.MCP_DISPATCH, h.MCP_CLIENTS, h.PENDING_MCP = self._old_mcp

    def _register(self, server, tools):
        c = h.MCPClient(server, {"transport": "stdio", "command": "x"})
        c.connect = lambda: {}
        c.list_tools = lambda: tools
        c.close = lambda: None
        h.register_mcp_tools([c])
        return c

    def _mcp_tool(self, name, desc="d"):
        return {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": {}}}

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

    def test_mcp_group_rendering(self):
        self._register("srv", [self._mcp_tool("t1", "d1"), self._mcp_tool("t2", "d2")])
        n_builtin = len(h.OPENAI_TOOLS_INTERACTIVE)
        applied, out = self.menu(["down"] * n_builtin + [("char", "+"), "enter"])
        self.assertTrue(applied)
        self.assertIn("mcp servers:", out)
        self.assertIn("> [x] srv", out)
        self.assertIn("[x] t1", out)
        self.assertIn("[x] t2", out)
        self.assertIn("space toggles all", out)

    def test_mcp_group_collapsed_by_default(self):
        self._register("srv", [self._mcp_tool("t1", "d1"), self._mcp_tool("t2", "d2")])
        n_builtin = len(h.OPENAI_TOOLS_INTERACTIVE)
        applied, out = self.menu(["down"] * n_builtin + ["enter"])
        self.assertTrue(applied)
        self.assertIn("> [x] srv", out)
        self.assertNotIn("[x] t1", out)
        self.assertNotIn("[x] t2", out)
        self.assertIn("+/- expand/collapse", out)

    def test_expand_collapse_roundtrip(self):
        self._register("srv", [self._mcp_tool("t1", "d1"), self._mcp_tool("t2", "d2")])
        n_builtin = len(h.OPENAI_TOOLS_INTERACTIVE)
        applied, out = self.menu(["down"] * n_builtin + [("char", "+"), "enter"])
        self.assertTrue(applied)
        self.assertIn("[x] t1", out)
        applied, out = self.menu(
            ["down"] * n_builtin + [("char", "+"), ("char", "-"), "enter"]
        )
        self.assertTrue(applied)
        # the buffer accumulates every draw (incl. the expanded one), so
        # check only the final draw
        last = out.rsplit("tools —", 1)[-1]
        self.assertNotIn("[x] t1", last)
        self.assertNotIn("[x] t2", last)

    def test_expand_equals_alias(self):
        self._register("srv", [self._mcp_tool("t1", "d1")])
        n_builtin = len(h.OPENAI_TOOLS_INTERACTIVE)
        applied, out = self.menu(["down"] * n_builtin + [("char", "="), "enter"])
        self.assertTrue(applied)
        self.assertIn("[x] t1", out)

    def test_navigation_skips_collapsed_rows(self):
        self._register("srv", [self._mcp_tool("t1", "d1"), self._mcp_tool("t2", "d2")])
        h.PENDING_MCP.append(("pend", {"transport": "http", "url": "http://x/mcp"}))
        n_builtin = len(h.OPENAI_TOOLS_INTERACTIVE)
        # visible rows: built-ins, 'srv' (collapsed), 'pend' — so n_builtin+1
        # downs lands on the pending row, not on the hidden t1
        applied, out = self.menu(["down"] * (n_builtin + 1) + ["enter"])
        self.assertTrue(applied)
        self.assertIn("> [ ] pend", out)

    def test_server_row_toggles_all_off(self):
        self._register("srv", [self._mcp_tool("t1"), self._mcp_tool("t2")])
        n_builtin = len(h.OPENAI_TOOLS_INTERACTIVE)
        applied, _ = self.menu(["down"] * n_builtin + [("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {"t1", "t2"})

    def test_server_row_mixed_toggles_all_on(self):
        self._register("srv", [self._mcp_tool("t1"), self._mcp_tool("t2")])
        h.DISABLED_TOOLS.add("t2")
        n_builtin = len(h.OPENAI_TOOLS_INTERACTIVE)
        applied, out = self.menu(["down"] * n_builtin + [("char", " "), "enter"])
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, set())
        self.assertIn("[x] srv", out)

    def test_server_row_mixed_mark(self):
        self._register("srv", [self._mcp_tool("t1"), self._mcp_tool("t2")])
        h.DISABLED_TOOLS.add("t2")
        applied, out = self.menu(["enter"])
        self.assertTrue(applied)
        self.assertIn("[-] srv", out)

    def test_individual_mcp_tool_toggle(self):
        self._register("srv", [self._mcp_tool("t1"), self._mcp_tool("t2")])
        n_builtin = len(h.OPENAI_TOOLS_INTERACTIVE)
        # rows: built-ins, then the 'srv' server row (expand it), then its tools
        applied, _ = self.menu(
            ["down"] * n_builtin + [("char", "+"), "down", ("char", " "), "enter"]
        )
        self.assertTrue(applied)
        self.assertEqual(h.DISABLED_TOOLS, {"t1"})


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

    def test_ctrl_enter_selects(self):
        selected, _ = self.menu(["ctrl_enter"])
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
        self.assertEqual(messages[0]["role"], "user")
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
        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
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
        reminders = [m for m in messages if m.get("role") == "user" and "todo list" in (m.get("content") or "")]
        self.assertEqual(len(reminders), 1)
        self.assertIn("1. step", reminders[0]["content"])

    def test_output_capped(self):
        for i in range(300):
            h.tool_todo({"action": "add", "text": ("item %d " % i) * 40})
        out = h.tool_todo({"action": "list"})
        self.assertIn(f"showing first {h.TODO_BLOCK_LIMIT};", out)
        # the file on disk keeps the full list; only what enters the chat is trimmed
        with open(h.TODO_FILE, "r", encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 300)

    def test_reminder_capped(self):
        for i in range(300):
            h.tool_todo({"action": "add", "text": ("x " * 60) + str(i)})
        messages = []
        h._todo_reminder(messages)
        reminders = [m for m in messages if m.get("role") == "user"]
        self.assertEqual(len(reminders), 1)
        self.assertLess(len(reminders[0]["content"]), h.TODO_BLOCK_LIMIT + 200)


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

    def test_list_output_capped(self):
        for i in range(200):
            self.mem(action="add", text=("note %d " % i) * 30)
        out = self.mem(action="list", scope="project")
        self.assertIn(f"showing first {h.MEMORY_BLOCK_LIMIT};", out)

    def test_load_memory_capped(self):
        # load_memory feeds every system prompt, so an unbounded notes file
        # would silently eat context.
        for i in range(200):
            self.mem(action="add", text=("long note %d " % i) * 30)
        block = h.load_memory()
        self.assertLess(len(block), h.MEMORY_BLOCK_LIMIT + 200)
        self.assertIn("[truncated:", block)


class TestStateToolsRegistered(unittest.TestCase):
    def test_new_tools_in_both_lists(self):
        all_names = [s["function"]["name"] for s in h.OPENAI_TOOLS]
        interactive_names = [s["function"]["name"] for s in h.OPENAI_TOOLS_INTERACTIVE]
        for name in ("todo", "memory"):
            self.assertIn(name, all_names)
            self.assertIn(name, interactive_names)
            self.assertIn(name, h.DISPATCH)

    def test_gotcha_tool_removed(self):
        # pitfalls are recorded through the memory tool now, not a separate one
        for tools in (h.OPENAI_TOOLS, h.OPENAI_TOOLS_INTERACTIVE):
            self.assertNotIn("gotcha", [s["function"]["name"] for s in tools])
        self.assertNotIn("gotcha", h.DISPATCH)
        for name in ("tool_gotcha", "load_gotchas", "GOTCHAS_FILE", "GOTCHAS_LABEL"):
            self.assertFalse(hasattr(h, name))

    def test_system_prompt_mentions_state_tools(self):
        prompt = h.get_system_prompt("/x", "")
        for name in ("todo", "memory"):
            self.assertIn(name, prompt)
        # the pitfall-recording habit survives, routed through memory
        self.assertIn("root cause", prompt)
        self.assertIn("gotcha:", prompt)

    def test_memory_schema_covers_pitfall_notes(self):
        schema = next(s for s in h.OPENAI_TOOLS
                      if s["function"]["name"] == "memory")["function"]
        self.assertIn("pitfall", schema["description"])
        self.assertIn("gotcha:", schema["description"])

    def test_context_additions(self):
        old_proj, old_glob = h.MEMORY_PROJECT_FILE, h.MEMORY_GLOBAL_FILE
        h.MEMORY_PROJECT_FILE = "/nonexistent/proj.md"
        h.MEMORY_GLOBAL_FILE = "/nonexistent/glob.md"
        self.addCleanup(setattr, h, "MEMORY_PROJECT_FILE", old_proj)
        self.addCleanup(setattr, h, "MEMORY_GLOBAL_FILE", old_glob)
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
        for name in ("ask_user",):
            self.assertIn(name, all_names)
            self.assertIn(name, interactive_names)
            self.assertIn(name, h.DISPATCH)

    def test_system_prompt_mentions_interaction_tools(self):
        prompt = h.get_system_prompt("/x", "")
        self.assertIn("ask_user", prompt)

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
        self.assertTrue(
            out[0]["text"].endswith(
                "\n... [truncated: 60000 chars total, showing first 50000; "
                "the referenced file is large; reference a narrower file]"
            )
        )

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

    def test_record_usage_stores_by_conv_id(self):
        msgs = [{"role": "user", "content": "hi"}]
        h._record_usage(msgs, {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}, conv_id=7)
        self.assertEqual(h.USAGE_BY_CONV[7]["prompt_tokens"], 100)
        self.assertNotIn(id(msgs), h.USAGE_BY_CONV)

    def test_format_status_by_conv_id(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        h._record_usage(msgs, {"prompt_tokens": 170000, "completion_tokens": 10, "total_tokens": 170010}, conv_id=5)
        out = h.format_status(msgs, context_window=200000, conv_id=5)
        self.assertIn("context: 170000 tokens (server-reported) in 2 messages", out)
        # An address-keyed lookup must not find the conv-id entry.
        out2 = h.format_status(msgs, context_window=200000)
        self.assertIn("(estimated", out2)

    def test_chat_records_usage_by_conv_id(self):
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
            h.chat(msgs, "m", conv_id=3)
        self.assertEqual(h.USAGE_BY_CONV[3]["prompt_tokens"], 55)
        self.assertNotIn(id(msgs), h.USAGE_BY_CONV)

    def test_stream_chat_records_usage_by_conv_id(self):
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
            list(h.stream_chat(msgs, "m", conv_id=4))
        self.assertEqual(h.USAGE_BY_CONV[4]["prompt_tokens"], 170000)
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

        def fake_stream_chat(messages, model, interactive=False, temperature=0.2, watcher=None, progress=None, conv_id=None):
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

        def fake_stream_chat(messages, model, interactive=False, temperature=0.2, watcher=None, progress=None, conv_id=None):
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
        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
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
        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
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

        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
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

        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
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
        def fake_stream_once(messages, model, interactive=False, temperature=0.2, conv_id=None):
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
