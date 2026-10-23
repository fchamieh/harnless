"""One-off: the tools stage.

Everything a model can call, and the table that maps a call onto it, moves to
`harnless_core/toolkit.py`; what is left in the entry point is the CLI — argument parsing, the
REPL loop, the menus, and a facade over the package.

The move is only legal because of the seam the agent stage put in place: the loop runs a tool
call through the callable it was *handed*, so the module below it never has to know what the
tools are, and the table can sit beside the tools instead of above them.

(It is called `toolkit`, not `tools`: `tools` is what every function in this package calls its
local list of tool specs, and a local named after an imported module means every
`tools.<name>` the link stage writes silently reads the list instead.)
"""

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
CODEMOD = HERE / "codemod.py"

MANIFEST = '''    "tools": {"toolkit": [
        "tool_get_cwd", "tool_exit", "_prompt_line", "_print_question", "tool_ask_user",
        "tool_mkdir", "tool_read_file", "tool_write_file",
        "_HTMLTextExtractor", "_html_to_text", "tool_fetch_url", "tool_task",
        "_builtin_tool_names", "execute_tool", "format_tools", "toggle_tools", "tools_menu",
        "DISPATCH",
    ]},
'''

TOOLS = [
    "tool_get_cwd", "tool_exit", "_prompt_line", "_print_question", "tool_ask_user",
    "tool_mkdir", "tool_read_file", "tool_write_file",
    "_HTMLTextExtractor", "_html_to_text", "tool_fetch_url", "tool_task",
    "_builtin_tool_names", "execute_tool", "format_tools", "toggle_tools", "tools_menu",
    "DISPATCH",
]

# The tool table now lives in the same module as the dispatcher, so a test that swaps the
# table has to swap the one the dispatcher reads. Patching the facade instead would only
# change what `harnless.DISPATCH` *reports*.
TEST_PATCHES = [
    ('h.DISPATCH = ', 'h.toolkit.DISPATCH = '),
    ('setattr, h, "DISPATCH"', 'setattr, h.toolkit, "DISPATCH"'),
    ('setattr(h, "DISPATCH"', 'setattr(h.toolkit, "DISPATCH"'),
    ('h.DISPATCH[', 'h.toolkit.DISPATCH['),
    ('del h.DISPATCH[', 'del h.toolkit.DISPATCH['),
    ('h.DISPATCH.get(', 'h.toolkit.DISPATCH.get('),
    ('h.DISPATCH.items()', 'h.toolkit.DISPATCH.items()'),
    ('h.DISPATCH)', 'h.toolkit.DISPATCH)'),
]


def main():
    text = CODEMOD.read_text(encoding="utf-8")
    marker = '    "agent": {"agent": ['
    if marker not in text:
        raise SystemExit("the agent stage is not in the manifest yet")
    if '"tools": {"toolkit": [' not in text:
        text = text.replace(marker, MANIFEST + marker, 1)
        CODEMOD.write_text(text, encoding="utf-8", newline="\n")
        print("tools stage added to the manifest")

    src = pathlib.Path("harnless.py").read_text(encoding="utf-8")
    gone = [n for n in TOOLS
            if not re.search(rf"^(def|class) {re.escape(n)}\(|^{re.escape(n)} = ", src, re.M)]
    if gone:
        raise SystemExit(f"not in the entry point any more: {gone}")

    # main() already names the executor it hands the loop (execute=execute_tool); the link
    # stage rewrites that reference to toolkit.execute_tool on its own.
    if "execute=execute_tool," not in src:
        raise SystemExit("main() no longer says what its runs execute")

    tests = pathlib.Path("tests.py").read_text(encoding="utf-8")
    moved = 0
    for old, new in TEST_PATCHES:
        count = tests.count(old)
        if count:
            tests = tests.replace(old, new)
            moved += count
            print(f"  {old!r} -> {new!r}: {count}")
    tests = tests.replace("h.toolkit.DISPATCH = dict(DISPATCH)", "h.toolkit.DISPATCH = dict(h.toolkit.DISPATCH)")
    pathlib.Path("tests.py").write_text(tests, encoding="utf-8", newline="\n")
    print(f"{moved} test patches retargeted to the table the dispatcher reads")


if __name__ == "__main__":
    main()
