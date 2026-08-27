"""Every ``pycon`` block in a docstring or a page must be true.

The examples in the README, the comparison page, and the public docstrings
show what the interpreter actually prints, so they are only useful if they
still do. This runs them.

Only ``pycon`` blocks are executed. A plain ``python`` block is an
illustration (often a cloud example that needs a backend and network), so it
is rendered on the site but never run here -- which is why every ``pycon``
block is written to stand on its own.
"""

import doctest
import io
import re
import textwrap
from pathlib import Path as _FsPath

import pytest
import typing_extensions as tx

import bagof.paths as paths

#: Modules whose public docstrings carry runnable examples.
SOURCES = ["_path.py", "_async_path.py"]

#: Hand-written pages, relative to the repository root.
PAGES = ["README.md", "docs/comparison.md"]


def _root() -> tx.Optional[_FsPath]:
    """The repository root, or None when running from an installed copy."""
    root = _FsPath(paths.__file__).resolve().parents[3]
    return root if (root / "README.md").is_file() else None


def _globals() -> dict:
    """The namespace the examples run against.

    ``bagof.paths`` uses ``from __future__ import annotations``; doctest
    inherits a module's future flags from the globals it is handed, which
    would turn annotations in an example into strings -- not how a reader's
    own module behaves. Drop the flag, keep the package's public names.
    """
    return {k: v for k, v in vars(paths).items() if k != "annotations"}


def _blocks(path: _FsPath) -> tx.List[tx.Tuple[int, str]]:
    """Every ``pycon`` block in a file, with its 1-based line number."""
    source = path.read_text()
    found = []
    for match in re.finditer(r"```pycon\n(.*?)[ ]*```", source, re.S):
        line = source[: match.start()].count("\n") + 1
        found.append((line, textwrap.dedent(match.group(1)) + "\n"))
    return found


def _files() -> tx.List[_FsPath]:
    files = [_FsPath(paths.__file__).parent / name for name in SOURCES]
    root = _root()
    if root is not None:
        # The hand-written pages ship in a checkout, not in an installed copy.
        files += [root / page for page in PAGES]
    return [path for path in files if path.is_file()]


CASES = [
    pytest.param(path.name, line, body, id=f"{path.name}:{line}")
    for path in _files()
    for line, body in _blocks(path)
]


def test_there_are_examples_to_check() -> None:
    # A refactor that moves the examples should not make this file pass by
    # finding nothing.
    assert len(CASES) >= 4


@pytest.mark.parametrize("name,line,body", CASES)
def test_pycon_block(name: str, line: int, body: str) -> None:
    test = doctest.DocTestParser().get_doctest(
        body, _globals(), f"{name}:{line}", name, line
    )
    runner = doctest.DocTestRunner(
        optionflags=doctest.ELLIPSIS | doctest.IGNORE_EXCEPTION_DETAIL,
    )
    report = io.StringIO()
    result = runner.run(test, out=report.write)
    assert not result.failed, report.getvalue()
