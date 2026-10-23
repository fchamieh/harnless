"""harnless_core: the harness, split into layers.

Layer order - a module may only reference names from modules listed above it:

    config, state                 fixed tunables / mutable runtime state
    errors, caps, paths, render   shared low-level behaviour
    journal                       session recording and resume
    (tools, client, agent loop    - later stages of the split)

harnless.py stays the entry point and the facade: it imports this package and
re-exports it, so `import harnless` still resolves every harness name.
"""
