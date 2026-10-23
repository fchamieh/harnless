"""One-off: print git's visible set and the harness's visible set, per pattern.

Why this exists: the engine in search.py translates a gitignore pattern into a regex, and its
`**` rule was written from gitignore(5) ("other consecutive asterisks are considered regular
asterisks", so `a**/b` behaves like `a*/b`). A Linux run of TestGitignoreAgainstGit (git 2.47.3)
disagreed with that; a Windows run (git 2.55.0) agreed. gitignore(5) is not the whole story —
git's wildmatch() takes a run of two or more stars as a globstar in cases the manual does not
list, including "a run right after a literal prefix (`a**/b`)".

Each pattern gets its own work tree, so no pattern borrows another's verdict and no path
collides with a directory another case needed. Nothing is `git add`ed, so both sides see
.gitignore and the sets are directly comparable.

    python refactor/git_semantics.py                      # your git
    wsl -d Debian -u root bash ./refactor/semantics.sh    # Debian's git
"""

import os
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

CASES = [
    ("O**/S.txt", ["O/S.txt", "Oq/S.txt", "O/a/S.txt", "O/a/b/S.txt", "xO/S.txt", "O/a/T.txt"]),
    ("as**/O.txt", ["as/O.txt", "asx/O.txt", "as/a/O.txt", "a/as/O.txt"]),
    ("a**/b**/c", ["a/b/c/f.txt", "a/x/b/c/f.txt", "a/b/x/c/f.txt", "a/x/b/y/c/f.txt"]),
    ("dir/**x", ["dir/x/f.txt", "dir/ax/f.txt", "dir/sub/x/f.txt", "dirx/x/f.txt"]),
    ("**suf/O.txt", ["suf/O.txt", "asuf/O.txt", "a/suf/O.txt", "a/b/suf/O.txt"]),
    ("p/**/M.txt", ["p/M.txt", "p/a/M.txt", "p/a/b/M.txt"]),
    ("**/L1.txt", ["L1.txt", "a/L1.txt", "a/b/L1.txt"]),
]


def visible_sets(tree):
    # SEMANTICS_IGNORECASE=0 asks git in case-sensitive mode even on Windows, so the version
    # can be tested without the platform's usual core.ignorecase=true muddying the comparison.
    ignorecase = os.environ.get("SEMANTICS_IGNORECASE", "true" if os.name == "nt" else "false")
    out = subprocess.run(
        ["git", "-c", f"core.ignorecase={ignorecase}",
         "ls-files", "-z", "--others", "--exclude-standard", "--", "."],
        cwd=tree, capture_output=True, check=True,
    ).stdout
    git_side = {p for p in out.decode("utf-8", "replace").split("\0") if p}
    harness_side = {
        os.path.relpath(fp, tree).replace("\\", "/") for fp in _iter_files(str(tree))
    }
    return git_side, harness_side


def main():
    global _iter_files
    import harnless as h  # the engine under test

    _iter_files = h.search._iter_files
    version = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
    root = pathlib.Path("./_git_semantics")
    shutil.rmtree(root, ignore_errors=True)

    print(version)
    disagreements = 0
    for index, (pattern, paths) in enumerate(CASES):
        tree = root / f"case{index}"
        tree.mkdir(parents=True, exist_ok=True)
        (tree / ".gitignore").write_text(pattern + "\n", encoding="utf-8")
        for rel in paths:
            file = tree / rel
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "."], cwd=tree, check=True)

        git_side, harness_side = visible_sets(tree)
        same = git_side == harness_side
        disagreements += 0 if same else 1
        print(f"\n{pattern!r}  {'agree' if same else 'DIFFER'}")
        for rel in sorted(set(paths) | git_side | harness_side):
            print(f"    {rel:20s} git={'see' if rel in git_side else 'ig '}"
                  f"  harness={'see' if rel in harness_side else 'ig '}")
        for extra in sorted(git_side - harness_side):
            if extra not in paths:
                print(f"    git sees {extra!r} which was not in the case")
        for extra in sorted(harness_side - git_side):
            if extra not in paths:
                print(f"    harness sees {extra!r} which was not in the case")
    print(f"\n{disagreements} of {len(CASES)} patterns disagree")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
