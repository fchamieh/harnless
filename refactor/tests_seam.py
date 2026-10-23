"""One-off: every test that runs the agent now says which tools that run may use.

`run_agent(..., execute=...)` is required, so each `h.run_agent(...)` call in the suite gains
`execute=h.execute_tool` — the same thing main() and tool_task pass. Fakes that stand in for
run_agent gain an `execute` parameter so the harness can call them the way it calls the real
thing (a fake that could not be called like the real thing would hide a wiring change).
"""

import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent.parent / "tests.py"


def offsets(src: str):
    line_start = [0]
    for line in src.split("\n"):
        line_start.append(line_start[-1] + len(line) + 1)

    def at(lineno, col):  # 1-based lineno, 0-based col -> absolute offset
        return line_start[lineno - 1] + col

    return at


def main():
    src = TESTS.read_text(encoding="utf-8")
    at = offsets(src)
    tree = ast.parse(src)
    inserts = []  # (offset, text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "run_agent":
                continue
            if any(k.arg == "execute" for k in node.keywords):
                continue
            last = node.args[-1] if node.args else node.keywords[-1]
            inserts.append((at(last.end_lineno, last.end_col_offset), ", execute=h.execute_tool"))
        elif (
            isinstance(node, ast.FunctionDef)
            and node.name.endswith("run_agent")
            and not node.name.startswith("_")
        ):
            if any(a.arg == "execute" for a in node.args.args):
                continue
            seg = ast.get_source_segment(src, node)
            head = seg.split(":", 1)[0]  # `def fake_run_agent(...)`
            inserts.append((at(node.lineno, node.col_offset + len(head) - 1), ", execute=None"))

    for offset, text in sorted(inserts, key=lambda pair: -pair[0]):
        src = src[:offset] + text + src[offset:]

    before = src.count("execute=h.execute_tool")
    TESTS.write_text(src, encoding="utf-8", newline="\n")
    ast.parse(src)
    print(f"tests.py: {before} run_agent calls now pass their tools")


if __name__ == "__main__":
    main()
