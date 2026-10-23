"""One-off: the slash-command menu moves down to the input layer.

`tool_ask_user` reads its answer with the same line editor the REPL uses, and it passes the
REPL's command menu to it as the completion callback (`lineedit.readline_prompt(prompt,
commands_menu)`). With the tools in their own module, that menu can no longer stay in the
entry point: a package module may not import the thing that imports it.

So the menu goes where the editor is — `lineedit.py`, the module that already owns the raw
key iterators, the cursor-editing prompt and the watchers — and the command list it renders
goes to `config.py`, because it is fixed data that nothing rebinds.
"""

import ast
import pathlib

MOVES = [
    ("REPL_COMMANDS", "config"),
    ("commands_menu", "lineedit"),
]


def statement(src: str, name: str):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node
    return None


def main():
    source = pathlib.Path("harnless.py")
    src = source.read_text(encoding="utf-8")
    cuts = []
    for name, module in MOVES:
        node = statement(src, name)
        if node is None:
            print(f"  {name}: already out of the entry point")
            continue
        cuts.append((node.lineno, node.end_lineno, name, module,
                     ast.get_source_segment(src, node)))

    # cut from the bottom up so the earlier line numbers stay valid
    lines = src.split("\n")
    for start, end, name, module, text in sorted(cuts, key=lambda c: -c[0]):
        del lines[start - 1:end]
    src = "\n".join(lines)

    for _, _, name, module, text in cuts:
        if module == "lineedit":
            # inside its own module the menu calls the key iterators directly
            text = text.replace("lineedit.", "")
        target = pathlib.Path("harnless_core") / f"{module}.py"
        body = target.read_text(encoding="utf-8")
        if f"def {name}(" in body or f"{name} = " in body:
            print(f"  {name}: already in {module}.py")
            continue
        target.write_text(body.rstrip("\n") + "\n\n\n" + text.strip("\n") + "\n",
                          encoding="utf-8", newline="\n")
        print(f"  {name} -> harnless_core/{module}.py")

    if any(m == "lineedit" for _, _, _, m, _ in cuts):
        body = pathlib.Path("harnless_core/lineedit.py").read_text(encoding="utf-8")
        if "import shutil" not in body:
            body = body.replace("import os\n", "import os\nimport shutil\n", 1)
            pathlib.Path("harnless_core/lineedit.py").write_text(body, encoding="utf-8", newline="\n")
            print("  lineedit.py: import shutil (the menu sizes itself to the terminal)")

    source.write_text(src, encoding="utf-8", newline="\n")

    codemod = pathlib.Path("refactor/codemod.py")
    text = codemod.read_text(encoding="utf-8")
    added = 0
    for name, module in MOVES:
        marker = f'    "{module}": {{"{module}": ['
        if marker in text and f'"{name}"' not in text.split(marker, 1)[1].split("]]", 1)[0]:
            text = text.replace(marker, marker + f"\n        \"{name}\",", 1)
            added += 1
    codemod.write_text(text, encoding="utf-8", newline="\n")
    print(f"{len(cuts)} statements relocated, {added} manifest entries added")


if __name__ == "__main__":
    main()
