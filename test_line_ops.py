import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("harness", os.path.join(os.getcwd(), "fadiz-harness.py"))
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

ok = 0
fail = 0
rp = os.path.realpath("./t.txt")

def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"PASS {name}")
    else:
        fail += 1
        print(f"FAIL {name}\n  got:  {got!r}\n  want: {want!r}")

# --- write_file full overwrite
check("write full", h.tool_write_file({"path": "./t.txt", "content": "a\nb\nc\nd\n"}), f"wrote 8 chars to {rp}")

# --- read_file basics
check("read all numbered", h.tool_read_file({"path": "./t.txt"}), "1: a\n2: b\n3: c\n4: d")
check("read no numbers", h.tool_read_file({"path": "./t.txt", "line_numbers": False}), "a\nb\nc\nd")
check("read range", h.tool_read_file({"path": "./t.txt", "offset": 2, "lines": 2}), "2: b\n3: c\n[lines 2-3 of 4]")
check("read beyond eof", h.tool_read_file({"path": "./t.txt", "offset": 9}), "(empty)")
check("read bad offset", h.tool_read_file({"path": "./t.txt", "offset": 0}), "error: offset must be >= 1")
check("read bad lines", h.tool_read_file({"path": "./t.txt", "lines": -1}), "error: lines must be >= 0")

# --- write_file insert (lines=0)
check("insert before line 2", h.tool_write_file({"path": "./t.txt", "content": "x", "offset": 2, "lines": 0}), f"inserted 1 line(s) before line 2 in {rp}")
check("after insert", h.tool_read_file({"path": "./t.txt", "line_numbers": False}), "a\nx\nb\nc\nd")

# --- write_file replace range
check("replace lines 3-4", h.tool_write_file({"path": "./t.txt", "content": "q\nr", "offset": 3, "lines": 2}), f"replaced lines 3-4 of {rp} with 2 line(s)")
check("after replace", h.tool_read_file({"path": "./t.txt", "line_numbers": False}), "a\nx\nq\nr\nd")

# --- write_file append at EOF
check("append at eof", h.tool_write_file({"path": "./t.txt", "content": "end", "offset": 6}), f"inserted 1 line(s) before line 6 in {rp}")
check("after append", h.tool_read_file({"path": "./t.txt", "line_numbers": False}), "a\nx\nq\nr\nd\nend")

# --- write_file errors
check("offset beyond", h.tool_write_file({"path": "./t.txt", "content": "z", "offset": 99}), "error: offset 99 is beyond end of file (6 lines)")
check("missing file", h.tool_write_file({"path": "./nope.txt", "content": "z", "offset": 1}), "error: file does not exist, cannot modify line range: " + os.path.realpath("./nope.txt"))

# --- patch_file with offset/lines scope
h.tool_write_file({"path": "./t.txt", "content": "foo\nbar\nfoo\nbaz\nfoo\n"})
check("scoped patch ok", h.tool_patch_file({"path": "./t.txt", "old_string": "foo", "new_string": "FOO", "offset": 3, "lines": 1}), f"patched {rp}: replaced 1 occurrence(s)")
check("scoped result", h.tool_read_file({"path": "./t.txt", "line_numbers": False}), "foo\nbar\nFOO\nbaz\nfoo")
check("scoped not found", h.tool_patch_file({"path": "./t.txt", "old_string": "bar", "new_string": "BAR", "offset": 4, "lines": 1}), "error: old_string not found in lines 4-4")
h.tool_write_file({"path": "./t.txt", "content": "foo\nfoo\nfoo\n"})
check("scoped multiple", h.tool_patch_file({"path": "./t.txt", "old_string": "foo", "new_string": "X", "offset": 1, "lines": 2}), "error: old_string found 2 times in lines 1-2; narrow the range or add context")
check("scoped to eof", h.tool_patch_file({"path": "./t.txt", "old_string": "foo", "new_string": "LAST", "offset": 3}), f"patched {rp}: replaced 1 occurrence(s)")
check("scoped eof result", h.tool_read_file({"path": "./t.txt", "line_numbers": False}), "foo\nfoo\nLAST")
check("unscoped multiple still errors", h.tool_patch_file({"path": "./t.txt", "old_string": "foo", "new_string": "X"}), "error: old_string found 2 times; provide more context, use offset/lines, or set replace_all")

# multi-line old_string inside range
h.tool_write_file({"path": "./t.txt", "content": "l1\nmid\nl3\nmid\nl5\n"})
check("multiline scoped", h.tool_patch_file({"path": "./t.txt", "old_string": "mid\nl3", "new_string": "MID\nL3", "offset": 2, "lines": 2}), f"patched {rp}: replaced 1 occurrence(s)")
check("multiline scoped result", h.tool_read_file({"path": "./t.txt", "line_numbers": False}), "l1\nMID\nL3\nmid\nl5")

# --- list_dir / copy / move / delete
import shutil
os.makedirs("./st/sub", exist_ok=True)
os.makedirs("./st/empty", exist_ok=True)
h.tool_write_file({"path": "./st/a.txt", "content": "one\ntwo\nthree\n"})
h.tool_write_file({"path": "./st/sub/b.txt", "content": "one\n"})
rp_st_a = os.path.realpath("./st/a.txt")
check("list dir", h.tool_list_dir({"path": "./st"}), "a.txt\nempty/\nsub/")
check("list dir empty", h.tool_list_dir({"path": "./st/empty"}), "(empty)")
check("list dir missing", h.tool_list_dir({"path": "./st/missing"}), "error: not a directory: ./st/missing")
check("copy", h.tool_copy_file({"src": "./st/a.txt", "dst": "./st/c.txt"}), f"copied {rp_st_a} -> {os.path.realpath('./st/c.txt')}")
check("move", h.tool_move_file({"src": "./st/c.txt", "dst": "./st/sub/d.txt"}), f"moved {os.path.realpath('./st/c.txt')} -> {os.path.realpath('./st/sub/d.txt')}")
check("delete", h.tool_delete_file({"path": "./st/sub/d.txt"}), f"deleted {os.path.realpath('./st/sub/d.txt')}")
check("delete missing", h.tool_delete_file({"path": "./st/nope.txt"}), "error: file does not exist: ./st/nope.txt")
check("copy missing", h.tool_copy_file({"src": "./st/nope.txt", "dst": "./st/x.txt"}), "error: source does not exist: ./st/nope.txt")

# --- grep context / file_pattern
h.tool_write_file({"path": "./st/g1.txt", "content": "l1\nAAA mid\nl3\nl9\nl10\nAAA end\n"})
check("grep context", h.tool_grep({"path": "./st", "pattern": "AAA", "context": 1}),
      "  st/g1.txt:1: l1\n> st/g1.txt:2: AAA mid\n  st/g1.txt:3: l3\n--\n  st/g1.txt:5: l10\n> st/g1.txt:6: AAA end")
check("grep file_pattern", h.tool_grep({"path": "./st", "pattern": "AAA", "file_pattern": ".*g1.*"}),
      "st/g1.txt:2: AAA mid\nst/g1.txt:6: AAA end")
check("grep file_pattern no match", h.tool_grep({"path": "./st", "pattern": "AAA", "file_pattern": "nope.*"}), "no matches")

# --- run_shell output cap
r = h.tool_run_shell({"command": 'python -c "import sys; sys.stdout.write(\'x\'*30000)"'})
check("bash truncation", r.startswith("exit code: 0\n" + "x" * 20000 + "\n... [truncated, 30000 chars total]"), True)

os.remove("./t.txt")
shutil.rmtree("./st")
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
