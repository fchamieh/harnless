"""One-off: free a local name that a module alias needs.

`harnless_core/tools.py` is imported under the name `tools`, and several functions bind a
local called `tools` (the specs a turn advertises, the tools one mcp server exposes). A local
with a module's name is not cosmetics: inside that function every `tools.<name>` the link
stage writes would read the list instead of the module.

Token-based, for two reasons: the literal word inside menu strings ('(3 tools)') and the
f-string middles stay untouched, and a keyword argument named `tools=` (as in
`stream_once(..., tools=tools)`) must keep its name — only the value being passed is the
local.

usage: python refactor/rename_local.py <file> <name> <replacement>
"""

import ast
import io
import pathlib
import sys
import tokenize


def main():
    target_path = pathlib.Path(sys.argv[1])
    target, replacement = sys.argv[2], sys.argv[3]

    src = target_path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    regions = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign):
                bound.update(t.id for t in inner.targets if isinstance(t, ast.Name))
            elif isinstance(inner, (ast.AugAssign, ast.NamedExpr)) and isinstance(inner.target, ast.Name):
                bound.add(inner.target.id)
            elif isinstance(inner, (ast.For, ast.AsyncFor)) and isinstance(inner.target, ast.Name):
                bound.add(inner.target.id)
            elif isinstance(inner, ast.comprehension) and isinstance(inner.target, ast.Name):
                bound.add(inner.target.id)
        bound.update(a.arg for a in getattr(node.args, "args", []))
        bound.update(a.arg for a in getattr(node.args, "kwonlyargs", []))
        if target in bound:
            regions.append((node.lineno, node.end_lineno, node.name))

    if not regions:
        print(f"{target_path.name}: nothing binds `{target}` locally")
        return

    lines = src.split("\n")
    offsets = [0]
    for line in lines[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)

    tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    edits = []
    for i, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string != target:
            continue
        if not any(start <= tok.start[0] <= end for start, end, _ in regions):
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        prv = tokens[i - 1] if i else None
        is_keyword = (
            nxt is not None and nxt.type == tokenize.OP and nxt.string == "="
            and prv is not None and prv.type == tokenize.OP and prv.string in ("(", ",")
        )
        if is_keyword:
            continue  # the callee's parameter name, not the local
        edits.append((offsets[tok.start[0] - 1] + tok.start[1],
                      offsets[tok.end[0] - 1] + tok.end[1]))

    for start, end in sorted(edits, key=lambda pair: -pair[0]):
        src = src[:start] + replacement + src[end:]

    target_path.write_text(src, encoding="utf-8", newline="\n")
    ast.parse(src)
    for _, _, name in regions:
        print(f"  {target_path.name}: {name} — local `{target}` renamed")
    print(f"{len(edits)} name tokens rewritten in {target_path.name}")


if __name__ == "__main__":
    main()
