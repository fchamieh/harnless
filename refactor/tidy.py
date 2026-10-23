"""One-off: tidy the residue a package split leaves behind.

After 18 moves the entry point had four section banners with nothing under them (the code they
headed had moved out) and a list of stdlib imports nothing used any more. Both are the kind of
leftover that makes a refactored file read worse than the file it replaced, and neither is
worth doing by hand across every module — so this walks each file and removes exactly two
things: a banner whose section is empty, and a top-level import no statement names.

Nothing here moves or rewrites code, so the test suite is the whole proof: run it before and
after and it says the same thing.
"""

import argparse
import ast
import pathlib
import re

BANNER = re.compile(r"^# (-{10,})[ -]*\n", re.M)


def module_files():
    root = pathlib.Path(__file__).resolve().parent.parent
    files = [root / "harnless.py"]
    files += sorted((root / "harnless_core").glob("*.py"))
    return files


def used_names(tree):
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.value.id for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    return used


def drop_empty_banners(text: str):
    """A banner is a heading for code, and a heading with nothing under it is noise."""
    marks = list(BANNER.finditer(text))
    lines = text.split("\n")
    gone = 0
    for i in reversed(range(len(marks))):
        start_line = text[:marks[i].start()].count("\n")
        end_line = text[:marks[i].end()].count("\n")
        next_line = (text[:marks[i + 1].start()].count("\n")
                     if i + 1 < len(marks) else len(lines))
        body = "\n".join(lines[end_line + 1:next_line])
        trailing = len(lines) if i + 1 == len(marks) else next_line
        body = "\n".join(lines[end_line + 1:trailing])
        if body.strip():
            continue
        # drop the banner and the blank lines that belonged to the empty section
        stop = end_line
        while stop + 1 < trailing and not lines[stop + 1].strip():
            stop += 1
        del lines[start_line:stop + 1]
        gone += 1
    return "\n".join(lines), gone


def drop_unused_imports(text: str):
    tree = ast.parse(text)
    used = used_names(tree)
    doomed = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = (alias.asname or alias.name).split(".")[0]
                if bound not in used:
                    doomed.append((node.lineno, node.end_lineno, bound))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            if all((a.asname or a.name) not in used for a in node.names):
                doomed.append((node.lineno, node.end_lineno, node.module or "?"))
    if not doomed:
        return text, 0
    lines = text.split("\n")
    for start, end, _ in sorted(doomed, key=lambda d: -d[0]):
        del lines[start - 1:end]
    return "\n".join(lines), len(doomed)


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    total_banners = total_imports = 0
    for path in module_files():
        text = path.read_text(encoding="utf-8")
        text, banners = drop_empty_banners(text)
        text, imports = drop_unused_imports(text)
        ast.parse(text)  # the tidy must still be a parseable module
        if banners or imports:
            path.write_text(text, encoding="utf-8", newline="\n")
            total_banners += banners
            total_imports += imports
            print(f"  {path.name}: {banners} empty banner(s), {imports} unused import(s)")
    print(f"tidied {total_banners} empty banners and {total_imports} unused imports")


if __name__ == "__main__":
    main()
