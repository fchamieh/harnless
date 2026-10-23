"""One-off split: the tool schemas move to harnless_core/toolspec.py.

TOOLS was one table holding two different things: the contract the model is shown (644 lines
of JSON schema) and the function that answers the call. The contract is data and belongs in a
module; the functions are the harness's behaviour and stay in harnless.py as DISPATCH.

The schema text is carried over verbatim from the source (ast.get_source_segment), so this
script cannot change what the model is told — only where the text lives.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "harnless.py"
TARGET = ROOT / "harnless_core" / "toolspec.py"

DOCSTRING = '''"""toolspec: the contract the model is shown.

One entry per built-in tool: the OpenAI function schema (name, description, parameters) and
the order they are advertised in. Descriptions here are model-facing copy — they are what the
model reads when it decides which tool to call and with what arguments.

The other half of the pairing — which function answers each call — is DISPATCH in harnless.py:
schemas are data, implementations are behaviour. Keys must match.
"""'''


def main():
    src = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assign = next(
        n for n in tree.body if isinstance(n, ast.Assign) and
        any(getattr(t, "id", None) == "TOOLS" for t in n.targets)
    )
    entries = []
    for key, value in zip(assign.value.keys, assign.value.values):
        if not isinstance(key, ast.Constant) or not isinstance(value, ast.Tuple):
            raise SystemExit(f"unexpected TOOLS entry at line {key.lineno}")
        if len(value.elts) != 2 or not isinstance(value.elts[1], (ast.Name, ast.Attribute)):
            raise SystemExit(f"TOOLS[{key.value!r}] is not (schema, function)")
        schema = ast.get_source_segment(src, value.elts[0])
        impl = ast.get_source_segment(src, value.elts[1])
        if schema is None or impl is None:
            raise SystemExit(f"no source segment for the {key.value} entry")
        entries.append((key.value, schema, impl))

    # The schema literals were written nested inside `name: ( ... , fn)`, one level deeper
    # than they sit here; undo exactly that.
    def dedent(segment: str) -> str:
        lines = segment.split("\n")
        out = "\n".join([lines[0]] + [ln[4:] if ln.startswith("    ") else ln for ln in lines[1:]])
        # get_source_segment stops after the schema's own closing brace, which in
        # `name: ({...}, fn),` leaves that entry's comma inside the segment.
        return out.rstrip().rstrip(",")

    # every entry carries its own comma, so the joiner is a plain newline
    body = "\n".join(f'    "{name}": {dedent(schema)},' for name, schema, _ in entries)
    dispatch = "\n".join(f'    "{name}": {fn},' for name, _, fn in entries)

    TARGET.write_text(
        DOCSTRING
        + "\n\nSPECS = {\n"
        + body.rstrip(",")
        + "\n}\n\n"
        + '''# `exit` ends a run: it is a tool a one-shot run may call, never one the REPL offers.
NOT_FOR_REPL = frozenset({"exit"})

OPENAI_TOOLS = [spec for spec in SPECS.values()]
OPENAI_TOOLS_INTERACTIVE = [
    spec for name, spec in SPECS.items() if name not in NOT_FOR_REPL
]
''',
        encoding="utf-8",
        newline="\n",
    )

    lines = src.split("\n")
    head = (
        "# The contract the model reads lives in harnless_core/toolspec.py; this is the other\n"
        "# half: which function answers each call. Keys must match toolspec.SPECS.\n"
        "DISPATCH = {\n" + dispatch + "\n}"
    )
    # Replace the TOOLS table plus the three derived lines that followed it.
    end = assign.end_lineno
    while end < len(lines) and not lines[end].startswith("DISPATCH = "):
        end += 1
    replaced = lines[: assign.lineno - 1] + head.split("\n") + lines[end + 1 :]
    SOURCE.write_text("\n".join(replaced), encoding="utf-8", newline="\n")
    print(f"toolspec.py: {len(entries)} schemas")
    print(f"harnless.py: {len(lines)} -> {len(replaced)} lines (DISPATCH keeps {len(entries)} functions)")


if __name__ == "__main__":
    main()
