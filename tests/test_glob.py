"""The shared glob: pathlib 3.14's `glob`/`rglob` on every driver (#64).

`Path.glob`/`rglob` (and `AsyncPath`'s) run one glob -- CPython 3.14's
selectors over the vendored `glob.translate` -- on every driver and Python
version. The tables are measured on CPython 3.14.6 (Linux) with
`pathlib.Path.glob`; on 3.14 the tests also re-measure them. Every driver is
held to them, with the reconciled differences named where they are tested
(design §5).
"""

import asyncio
import os
import pathlib
import sys
import warnings

import pytest
import typing_extensions as tx

from bagof.paths import AsyncPath, Path, UnsupportedPathOperation

FILES = [
    "a.txt", ".hidden.txt", "B.TXT", "sub/inner.txt", "sub/.dot/x.txt",
    "sub/deeper/x.nii", "sub/deeper/y.txt", "sub2/a.txt",
]
DIRS = ["empty"]
_HAS_LINKS = hasattr(os, "symlink") and os.name != "nt"

# (method, pattern, case_sensitive) -> the matches, relative to the root
# ("." for the root itself), sorted.
_GLOB_314 = {
    ("glob", "*", None): [
        ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub2"
    ],
    ("glob", "*", False): [
        ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub2"
    ],
    ("glob", "**", None): [
        ".", ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("glob", "**", False): [
        ".", ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("glob", "**/", None): [
        ".", "empty", "sub", "sub/.dot", "sub/deeper", "sub2"
    ],
    ("glob", "**/", False): [
        ".", "empty", "sub", "sub/.dot", "sub/deeper", "sub2"
    ],
    ("glob", "**/*", None): [
        ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("glob", "**/*", False): [
        ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("glob", "**/**", None): [
        ".", ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("glob", "**/**", False): [
        ".", ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("glob", "**/*.NII", None): [],
    ("glob", "**/*.NII", False): ["sub/deeper/x.nii"],
    ("glob", "**/*.txt", None): [
        ".hidden.txt", "a.txt", "sub/.dot/x.txt", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "**/*.txt", False): [
        ".hidden.txt", "B.TXT", "a.txt", "sub/.dot/x.txt", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "**/*/", None): [
        "empty", "sub", "sub/.dot", "sub/deeper", "sub2"
    ],
    ("glob", "**/*/", False): [
        "empty", "sub", "sub/.dot", "sub/deeper", "sub2"
    ],
    ("glob", "**/.dot", None): ["sub/.dot"],
    ("glob", "**/.dot", False): ["sub/.dot"],
    ("glob", "**/deeper", None): ["sub/deeper"],
    ("glob", "**/deeper", False): ["sub/deeper"],
    ("glob", "**/deeper/*", None): ["sub/deeper/x.nii", "sub/deeper/y.txt"],
    ("glob", "**/deeper/*", False): ["sub/deeper/x.nii", "sub/deeper/y.txt"],
    ("glob", "**/sub/**", None): [
        "sub", "sub/.dot", "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii",
        "sub/deeper/y.txt", "sub/inner.txt"
    ],
    ("glob", "**/sub/**", False): [
        "sub", "sub/.dot", "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii",
        "sub/deeper/y.txt", "sub/inner.txt"
    ],
    ("glob", "**/x.nii", None): ["sub/deeper/x.nii"],
    ("glob", "**/x.nii", False): ["sub/deeper/x.nii"],
    ("glob", "*.TXT", None): ["B.TXT"],
    ("glob", "*.TXT", False): [".hidden.txt", "B.TXT", "a.txt"],
    ("glob", "*.txt", None): [".hidden.txt", "a.txt"],
    ("glob", "*.txt", False): [".hidden.txt", "B.TXT", "a.txt"],
    ("glob", "*/", None): ["empty", "sub", "sub2"],
    ("glob", "*/", False): ["empty", "sub", "sub2"],
    ("glob", "*/*", None): [
        "sub/.dot", "sub/deeper", "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "*/*", False): [
        "sub/.dot", "sub/deeper", "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "*/**/*.txt", None): [
        "sub/.dot/x.txt", "sub/deeper/y.txt", "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "*/**/*.txt", False): [
        "sub/.dot/x.txt", "sub/deeper/y.txt", "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "*/*/", None): ["sub/.dot", "sub/deeper"],
    ("glob", "*/*/", False): ["sub/.dot", "sub/deeper"],
    ("glob", "*/.dot/*", None): ["sub/.dot/x.txt"],
    ("glob", "*/.dot/*", False): ["sub/.dot/x.txt"],
    ("glob", "*/x.nii", None): [],
    ("glob", "*/x.nii", False): [],
    ("glob", ".*", None): [".hidden.txt"],
    ("glob", ".*", False): [".hidden.txt"],
    ("glob", "./a.txt", None): ["a.txt"],
    ("glob", "./a.txt", False): ["a.txt"],
    ("glob", ".hidden.txt", None): [".hidden.txt"],
    ("glob", ".hidden.txt", False): [".hidden.txt"],
    ("glob", "?.txt", None): ["a.txt"],
    ("glob", "?.txt", False): ["B.TXT", "a.txt"],
    ("glob", "SUB/*", None): [],
    ("glob", "SUB/*", False): ["sub/.dot", "sub/deeper", "sub/inner.txt"],
    ("glob", "[!a]*", None): [".hidden.txt", "B.TXT", "empty", "sub", "sub2"],
    ("glob", "[!a]*", False): [".hidden.txt", "B.TXT", "empty", "sub", "sub2"],
    ("glob", "[ab].txt", None): ["a.txt"],
    ("glob", "[ab].txt", False): ["B.TXT", "a.txt"],
    ("glob", "a.txt/", None): [],
    ("glob", "a.txt/", False): [],
    ("glob", "b.txt", None): [],
    ("glob", "b.txt", False): ["B.TXT"],
    ("glob", "nope", None): [],
    ("glob", "nope", False): [],
    ("glob", "s*/", None): ["sub", "sub2"],
    ("glob", "s*/", False): ["sub", "sub2"],
    ("glob", "sub", None): ["sub"],
    ("glob", "sub", False): ["sub"],
    ("glob", "sub/", None): ["sub"],
    ("glob", "sub/", False): ["sub"],
    ("glob", "sub/*", None): ["sub/.dot", "sub/deeper", "sub/inner.txt"],
    ("glob", "sub/*", False): ["sub/.dot", "sub/deeper", "sub/inner.txt"],
    ("glob", "sub/**", None): [
        "sub", "sub/.dot", "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii",
        "sub/deeper/y.txt", "sub/inner.txt"
    ],
    ("glob", "sub/**", False): [
        "sub", "sub/.dot", "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii",
        "sub/deeper/y.txt", "sub/inner.txt"
    ],
    ("glob", "sub/**/", None): ["sub", "sub/.dot", "sub/deeper"],
    ("glob", "sub/**/", False): ["sub", "sub/.dot", "sub/deeper"],
    ("glob", "sub/**/*.nii", None): ["sub/deeper/x.nii"],
    ("glob", "sub/**/*.nii", False): ["sub/deeper/x.nii"],
    ("glob", "sub/../a.txt", None): ["sub/../a.txt"],
    ("glob", "sub/../a.txt", False): ["sub/../a.txt"],
    ("glob", "sub//inner.txt", None): ["sub/inner.txt"],
    ("glob", "sub//inner.txt", False): ["sub/inner.txt"],
    ("glob", "sub/inner.txt", None): ["sub/inner.txt"],
    ("glob", "sub/inner.txt", False): ["sub/inner.txt"],
    ("glob", "sub/nope/*", None): [],
    ("glob", "sub/nope/*", False): [],
    ("rglob", "*", None): [
        ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("rglob", "*", False): [
        ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("rglob", "**", None): [
        ".", ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("rglob", "**", False): [
        ".", ".hidden.txt", "B.TXT", "a.txt", "empty", "sub", "sub/.dot",
        "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("rglob", "*.TXT", None): ["B.TXT"],
    ("rglob", "*.TXT", False): [
        ".hidden.txt", "B.TXT", "a.txt", "sub/.dot/x.txt", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2/a.txt"
    ],
    ("rglob", "*.txt", None): [
        ".hidden.txt", "a.txt", "sub/.dot/x.txt", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2/a.txt"
    ],
    ("rglob", "*.txt", False): [
        ".hidden.txt", "B.TXT", "a.txt", "sub/.dot/x.txt", "sub/deeper/y.txt",
        "sub/inner.txt", "sub2/a.txt"
    ],
    ("rglob", "*/", None): ["empty", "sub", "sub/.dot", "sub/deeper", "sub2"],
    ("rglob", "*/", False): ["empty", "sub", "sub/.dot", "sub/deeper", "sub2"],
    ("rglob", ".*", None): [".hidden.txt", "sub/.dot"],
    ("rglob", ".*", False): [".hidden.txt", "sub/.dot"],
    ("rglob", "deeper", None): ["sub/deeper"],
    ("rglob", "deeper", False): ["sub/deeper"],
    ("rglob", "sub/*", None): ["sub/.dot", "sub/deeper", "sub/inner.txt"],
    ("rglob", "sub/*", False): ["sub/.dot", "sub/deeper", "sub/inner.txt"],
    ("rglob", "x.*", None): ["sub/.dot/x.txt", "sub/deeper/x.nii"],
    ("rglob", "x.*", False): ["sub/.dot/x.txt", "sub/deeper/x.nii"],
}

# (method, pattern, recurse_symlinks) -> the matches on the same tree with
# `link-sub -> sub`, `link-a.txt -> a.txt` and `dangling.txt -> gone`.
_LINKS_314 = {
    ("glob", "*", False): [
        ".hidden.txt", "B.TXT", "a.txt", "dangling.txt", "empty", "link-a.txt",
        "link-sub", "sub", "sub2"
    ],
    ("glob", "*", True): [
        ".hidden.txt", "B.TXT", "a.txt", "dangling.txt", "empty", "link-a.txt",
        "link-sub", "sub", "sub2"
    ],
    ("glob", "**", False): [
        ".", ".hidden.txt", "B.TXT", "a.txt", "dangling.txt", "empty",
        "link-a.txt", "link-sub", "sub", "sub/.dot", "sub/.dot/x.txt",
        "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt", "sub/inner.txt",
        "sub2", "sub2/a.txt"
    ],
    ("glob", "**", True): [
        ".", ".hidden.txt", "B.TXT", "a.txt", "dangling.txt", "empty",
        "link-a.txt", "link-sub", "link-sub/.dot", "link-sub/.dot/x.txt",
        "link-sub/deeper", "link-sub/deeper/x.nii", "link-sub/deeper/y.txt",
        "link-sub/inner.txt", "sub", "sub/.dot", "sub/.dot/x.txt",
        "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt", "sub/inner.txt",
        "sub2", "sub2/a.txt"
    ],
    ("glob", "**/", False): [
        ".", "empty", "sub", "sub/.dot", "sub/deeper", "sub2"
    ],
    ("glob", "**/", True): [
        ".", "empty", "link-sub", "link-sub/.dot", "link-sub/deeper", "sub",
        "sub/.dot", "sub/deeper", "sub2"
    ],
    ("glob", "**/*.txt", False): [
        ".hidden.txt", "a.txt", "dangling.txt", "link-a.txt", "sub/.dot/x.txt",
        "sub/deeper/y.txt", "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "**/*.txt", True): [
        ".hidden.txt", "a.txt", "dangling.txt", "link-a.txt",
        "link-sub/.dot/x.txt", "link-sub/deeper/y.txt", "link-sub/inner.txt",
        "sub/.dot/x.txt", "sub/deeper/y.txt", "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "**/deeper/**", False): [
        "sub/deeper", "sub/deeper/x.nii", "sub/deeper/y.txt"
    ],
    ("glob", "**/deeper/**", True): [
        "link-sub/deeper/x.nii", "link-sub/deeper/y.txt", "sub/deeper/x.nii",
        "sub/deeper/y.txt"
    ],
    ("glob", "**/x.nii", False): ["sub/deeper/x.nii"],
    ("glob", "**/x.nii", True): ["link-sub/deeper/x.nii", "sub/deeper/x.nii"],
    ("glob", "*/", False): ["empty", "link-sub", "sub", "sub2"],
    ("glob", "*/", True): ["empty", "link-sub", "sub", "sub2"],
    ("glob", "*/*", False): [
        "link-sub/.dot", "link-sub/deeper", "link-sub/inner.txt", "sub/.dot",
        "sub/deeper", "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "*/*", True): [
        "link-sub/.dot", "link-sub/deeper", "link-sub/inner.txt", "sub/.dot",
        "sub/deeper", "sub/inner.txt", "sub2/a.txt"
    ],
    ("glob", "*/deeper/*", False): [
        "link-sub/deeper/x.nii", "link-sub/deeper/y.txt", "sub/deeper/x.nii",
        "sub/deeper/y.txt"
    ],
    ("glob", "*/deeper/*", True): [
        "link-sub/deeper/x.nii", "link-sub/deeper/y.txt", "sub/deeper/x.nii",
        "sub/deeper/y.txt"
    ],
    ("glob", "link-sub/*", False): [
        "link-sub/.dot", "link-sub/deeper", "link-sub/inner.txt"
    ],
    ("glob", "link-sub/*", True): [
        "link-sub/.dot", "link-sub/deeper", "link-sub/inner.txt"
    ],
    ("glob", "link-sub/**", False): [
        "link-sub", "link-sub/.dot", "link-sub/.dot/x.txt", "link-sub/deeper",
        "link-sub/deeper/x.nii", "link-sub/deeper/y.txt", "link-sub/inner.txt"
    ],
    ("glob", "link-sub/**", True): [
        "link-sub", "link-sub/.dot", "link-sub/.dot/x.txt", "link-sub/deeper",
        "link-sub/deeper/x.nii", "link-sub/deeper/y.txt", "link-sub/inner.txt"
    ],
    ("rglob", "*", False): [
        ".hidden.txt", "B.TXT", "a.txt", "dangling.txt", "empty", "link-a.txt",
        "link-sub", "sub", "sub/.dot", "sub/.dot/x.txt", "sub/deeper",
        "sub/deeper/x.nii", "sub/deeper/y.txt", "sub/inner.txt", "sub2",
        "sub2/a.txt"
    ],
    ("rglob", "*", True): [
        ".hidden.txt", "B.TXT", "a.txt", "dangling.txt", "empty", "link-a.txt",
        "link-sub", "link-sub/.dot", "link-sub/.dot/x.txt", "link-sub/deeper",
        "link-sub/deeper/x.nii", "link-sub/deeper/y.txt", "link-sub/inner.txt",
        "sub", "sub/.dot", "sub/.dot/x.txt", "sub/deeper", "sub/deeper/x.nii",
        "sub/deeper/y.txt", "sub/inner.txt", "sub2", "sub2/a.txt"
    ],
    ("rglob", "*.txt", False): [
        ".hidden.txt", "a.txt", "dangling.txt", "link-a.txt", "sub/.dot/x.txt",
        "sub/deeper/y.txt", "sub/inner.txt", "sub2/a.txt"
    ],
    ("rglob", "*.txt", True): [
        ".hidden.txt", "a.txt", "dangling.txt", "link-a.txt",
        "link-sub/.dot/x.txt", "link-sub/deeper/y.txt", "link-sub/inner.txt",
        "sub/.dot/x.txt", "sub/deeper/y.txt", "sub/inner.txt", "sub2/a.txt"
    ],
    ("rglob", "*/", False): [
        "empty", "link-sub", "sub", "sub/.dot", "sub/deeper", "sub2"
    ],
    ("rglob", "*/", True): [
        "empty", "link-sub", "link-sub/.dot", "link-sub/deeper", "sub",
        "sub/.dot", "sub/deeper", "sub2"
    ],
}

_CASES = sorted(_GLOB_314, key=repr)
_LINK_CASES = sorted(_LINKS_314, key=repr)


def _rel(paths: object, root: object) -> list:
    base = str(root).rstrip("/")
    out = []
    for path in paths:
        text = str(path)
        assert text.startswith(base), (text, base)
        out.append(text[len(base):].lstrip("/") or ".")
    return sorted(out)


def _kwargs(flag: object, name: str) -> dict:
    return {} if flag is None else {name: flag}


def _local_tree(root: pathlib.Path) -> pathlib.Path:
    for name in FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x")
    for name in DIRS:
        (root / name).mkdir()
    return root


def _link_tree(root: pathlib.Path) -> pathlib.Path:
    _local_tree(root)
    (root / "link-sub").symlink_to(root / "sub", True)
    (root / "link-a.txt").symlink_to(root / "a.txt")
    (root / "dangling.txt").symlink_to(root / "gone")
    return root


# -- the stdlib, on every Python version --------------------------------------
@pytest.mark.parametrize("method, pattern, case", _CASES)
def test_local_glob_is_pathlib_314s(
    tmp_path: pathlib.Path, method: str, pattern: str, case: object,
) -> None:
    root = _local_tree(tmp_path)
    kwargs = _kwargs(case, "case_sensitive")
    got = _rel(getattr(Path(root), method)(pattern, **kwargs), root)
    assert got == _GLOB_314[method, pattern, case]


@pytest.mark.skipif(sys.version_info < (3, 14), reason="pathlib 3.14 glob")
@pytest.mark.parametrize("method, pattern, case", _CASES)
def test_the_table_is_pathlib_314s_own(
    tmp_path: pathlib.Path, method: str, pattern: str, case: object,
) -> None:
    root = _local_tree(tmp_path)
    kwargs = _kwargs(case, "case_sensitive")
    got = _rel(getattr(root, method)(pattern, **kwargs), root)
    assert got == _GLOB_314[method, pattern, case]


@pytest.mark.skipif(not _HAS_LINKS, reason="no symbolic links here")
@pytest.mark.parametrize("method, pattern, recurse", _LINK_CASES)
def test_local_links_as_pathlib_314(
    tmp_path: pathlib.Path, method: str, pattern: str, recurse: bool,
) -> None:
    root = _link_tree(tmp_path)
    got = _rel(
        getattr(Path(root), method)(pattern, recurse_symlinks=recurse), root
    )
    assert got == _LINKS_314[method, pattern, recurse]
    if sys.version_info >= (3, 14):
        native = getattr(root, method)(pattern, recurse_symlinks=recurse)
        assert got == _rel(native, root)


def test_relative_root_and_lazy_errors(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _local_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert sorted(str(p) for p in Path(".").glob("*/*.txt")) == [
        os.path.join("sub", "inner.txt"), os.path.join("sub2", "a.txt"),
    ]
    assert [str(p) for p in Path(".").glob("**/")][0] == "."
    # A bad pattern raises at the call, as pathlib's does.
    with pytest.raises(NotImplementedError):
        Path(".").glob("/abs")
    with pytest.raises(ValueError):
        Path(".").glob(".")
    with pytest.raises(TypeError):
        Path(".").glob(b"*")
    assert [p.name for p in Path(".").glob(pathlib.PurePath("sub2/*"))] == [
        "a.txt"
    ]


def test_matches_carry_their_listing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _local_tree(tmp_path)
    matches = list(Path(root).rglob("*"))
    calls = []
    for name in ("stat", "lstat"):
        original = getattr(os, name)

        def counted(
            *args: object, _original: object = original, **kw: object
        ) -> object:
            calls.append(args)
            return _original(*args, **kw)

        monkeypatch.setattr(os, name, counted)
    kinds = {
        _rel([m], root)[0]: (m.pathinfo.is_dir(), m.pathinfo.is_file())
        for m in matches
    }
    assert calls == []
    assert kinds["sub/deeper"] == (True, False)
    assert kinds["sub/deeper/x.nii"] == (False, True)


def test_stdlib_lists_each_directory_once_per_step(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As pathlib 3.14: one os.scandir per directory a `**` walks, and one
    # more for each directory the selector after it lists.
    root = _local_tree(tmp_path)
    scans = []
    original = os.scandir

    def counted(path: object = ".") -> object:
        scans.append(os.fspath(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", counted)
    assert len(list(Path(root).rglob("*.nii"))) == 1
    walked = 1 + 5  # the root and its five directories
    assert len(scans) == 2 * walked
    scans.clear()
    assert len(list(Path(root).rglob("*.nii", recurse_symlinks=True))) == 1
    assert len(scans) == walked  # one regular expression filters the walk


# -- universal-pathlib over memory:// -----------------------------------------
_COUNTED = ("ls", "find", "info", "isdir", "isfile", "exists", "glob", "walk")


def _count(monkeypatch: pytest.MonkeyPatch, cls: type) -> dict:
    """Count calls made to `cls` from outside it (not its own inner calls)."""
    counts = dict.fromkeys(_COUNTED, 0)
    depth = [0]
    for name in _COUNTED:
        original = getattr(cls, name)

        def counted(
            self: object, *args: object, _name: str = name,
            _original: object = original, **kwargs: object,
        ) -> object:
            if not depth[0]:
                counts[_name] += 1
            depth[0] += 1
            try:
                return _original(self, *args, **kwargs)
            finally:
                depth[0] -= 1

        monkeypatch.setattr(cls, name, counted)
    return counts


def _memory_tree(name: str) -> object:
    UPath = pytest.importorskip("upath").UPath
    root = UPath(f"memory://bagofpaths-glob/{name}")
    for file in FILES:
        (root / file).write_text("x")
    for folder in DIRS:
        (root / folder).mkdir(exist_ok=True)
    return root


# What an object store answers differently, whatever the driver: it has no
# `..` entry, so a pattern through one names nothing.
_NO_DOTDOT = {("glob", "sub/../a.txt", None), ("glob", "sub/../a.txt", False)}


def _store_expected(
    key: tuple, empty_dirs: bool = True, dotdot: tx.Optional[list] = None,
) -> list:
    if key in _NO_DOTDOT:
        return [] if dotdot is None else dotdot
    expected = _GLOB_314[key]
    if not empty_dirs:  # a bucket has no empty directory
        expected = [p for p in expected if p != "empty"]
    return expected


@pytest.mark.parametrize("method, pattern, case", _CASES)
def test_upath_glob_is_pathlib_314s(
    method: str, pattern: str, case: object,
) -> None:
    root = _memory_tree("same")
    kwargs = _kwargs(case, "case_sensitive")
    got = _rel(getattr(Path(root), method)(pattern, **kwargs), root)
    assert got == _store_expected((method, pattern, case))


# Where UPath's own glob answers otherwise (universal-pathlib 0.3.11): it
# ignores case_sensitive (with a warning), and its patterns are not
# normalized, so "./a.txt" and "sub//inner.txt" match nothing.
_UPATH_OWN = {("glob", "./a.txt"), ("glob", "sub//inner.txt")}


@pytest.mark.parametrize("method, pattern, case", _CASES)
def test_upath_agrees_with_upaths_own_where_it_is_pathlib_314s(
    method: str, pattern: str, case: object,
) -> None:
    root = _memory_tree("own")
    kwargs = _kwargs(case, "case_sensitive")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        native = _rel(getattr(root, method)(pattern, **kwargs), root)
    ours = _rel(getattr(Path(root), method)(pattern, **kwargs), root)
    if (method, pattern) in _UPATH_OWN or (
        case is False and native != ours
    ):
        assert native != _GLOB_314[method, pattern, case]
        return
    assert ours == native


def test_upath_recursive_glob_is_one_find(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = pytest.importorskip("fsspec.implementations.memory")
    root = _memory_tree("calls")
    counts = _count(monkeypatch, memory.MemoryFileSystem)
    matches = list(Path(root).rglob("*.nii"))
    assert _rel(matches, root) == ["sub/deeper/x.nii"]
    assert {k: v for k, v in counts.items() if v} == {"find": 1}
    assert matches[0].pathinfo.is_file()  # from the listing
    assert {k: v for k, v in counts.items() if v} == {"find": 1}
    counts.update(dict.fromkeys(counts, 0))
    assert len(list(Path(root).glob("*/*.txt"))) == 2
    # No `**`: one listing per directory the pattern reaches (and, as
    # UPath's own iterdir, an empty one checks it is a directory).
    assert {k: v for k, v in counts.items() if v} == {"ls": 4, "isdir": 1}


def test_async_over_upath_is_the_same_glob() -> None:
    root = _memory_tree("async")

    async def go() -> list:
        return [p async for p in AsyncPath(root).glob("**/*.txt")]

    found = asyncio.run(go())
    assert _rel(found, root) == _GLOB_314["glob", "**/*.txt", None]
    assert all(isinstance(p, AsyncPath) for p in found)


# -- cloudpathlib -------------------------------------------------------------
def _cloud_tree(name: str) -> object:
    local = pytest.importorskip("cloudpathlib.local")
    root = local.LocalS3Client().CloudPath(f"s3://bagofpaths-glob/{name}")
    for file in FILES:
        (root / file).write_text("x")
    return root


@pytest.mark.parametrize("method, pattern, case", _CASES)
def test_cloudpathlib_glob_is_pathlib_314s(
    method: str, pattern: str, case: object,
) -> None:
    root = _cloud_tree("same")
    kwargs = _kwargs(case, "case_sensitive")
    got = _rel(getattr(Path(root), method)(pattern, **kwargs), root)
    assert got == _cloud_expected((method, pattern, case))


def _cloud_expected(key: tuple) -> list:
    # cloudpathlib's own join resolves a "..", so the match through one
    # names the file it reaches.
    return _store_expected(key, empty_dirs=False, dotdot=["a.txt"])


# Where cloudpathlib's own glob answers otherwise (cloudpathlib 0.26): its
# selectors are pathlib 3.12's, so a pattern ending in "**" matches
# directories only and a trailing "/" is ignored; ".." raises.
_CLOUD_OWN = {
    "**", "**/*/", "**/**", "**/sub/**", "*/", "a.txt/", "sub/**", "*/*/",
    "sub/../a.txt",
}


@pytest.mark.parametrize("method, pattern, case", _CASES)
def test_cloudpathlib_agrees_with_its_own_where_it_is_pathlib_314s(
    method: str, pattern: str, case: object,
) -> None:
    root = _cloud_tree("own")
    kwargs = _kwargs(case, "case_sensitive")
    expected = _cloud_expected((method, pattern, case))
    ours = _rel(getattr(Path(root), method)(pattern, **kwargs), root)
    assert ours == expected
    try:
        native = _rel(getattr(root, method)(pattern, **kwargs), root)
    except Exception:  # noqa: BLE001 - cloudpathlib's own refusal
        native = None
    if pattern in _CLOUD_OWN or (method == "rglob" and pattern in (
        "**", "*/"
    )):
        assert native != expected
    elif case is False and sys.version_info < (3, 12):
        # Before 3.12 its selectors are the running pathlib's, which take no
        # case_sensitive: it is dropped, and its glob matches by case.
        assert native == _cloud_expected((method, pattern, None))
    else:
        assert native == expected


def test_cloudpathlib_recursive_glob_is_one_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = pytest.importorskip("cloudpathlib.local")
    root = _cloud_tree("calls")
    listings = []
    original = local.LocalS3Client._list_dir

    def counted(self: object, path: object, recursive: bool = False) -> object:
        listings.append(recursive)
        return original(self, path, recursive=recursive)

    monkeypatch.setattr(local.LocalS3Client, "_list_dir", counted)
    matches = list(Path(root).rglob("*.nii"))
    assert _rel(matches, root) == ["sub/deeper/x.nii"]
    assert listings == [True]  # one recursive listing
    assert matches[0].pathinfo.is_file()
    assert listings == [True]


# -- AsyncPath over a natively async store ------------------------------------
def _async_store() -> object:
    fsspec = pytest.importorskip("fsspec")
    wrapper = pytest.importorskip("fsspec.implementations.asyn_wrapper")
    memory = pytest.importorskip("fsspec.implementations.memory")

    class _GlobAsyncFS(wrapper.AsyncFileSystemWrapper):
        protocol = "memglobasync"
        async_impl = True

        def __init__(self, *args: object, **kwargs: object) -> None:
            for drop in ("asynchronous", "skip_instance_cache"):
                kwargs.pop(drop, None)
            super().__init__(memory.MemoryFileSystem(), asynchronous=True)

        @classmethod
        def _strip_protocol(cls, path: str) -> str:
            text = path.replace("memglobasync://", "memory://")
            return memory.MemoryFileSystem._strip_protocol(text)

    fsspec.register_implementation("memglobasync", _GlobAsyncFS, clobber=True)
    return memory.MemoryFileSystem


async def _async_tree(name: str) -> AsyncPath:
    root = AsyncPath(f"memglobasync://bagofpaths-glob/{name}")
    for file in FILES:
        await (root / file).write_text("x")
    for folder in DIRS:
        await (root / folder).mkdir(parents=True, exist_ok=True)
    return root


@pytest.mark.parametrize("method, pattern, case", _CASES)
def test_async_store_glob_is_pathlib_314s(
    method: str, pattern: str, case: object,
) -> None:
    _async_store()
    from bagof.paths._async_fsspec import AsyncFSPath

    async def go() -> list:
        root = await _async_tree("same")
        assert isinstance(root.wrapped, AsyncFSPath)
        kwargs = _kwargs(case, "case_sensitive")
        found = [p async for p in getattr(root, method)(pattern, **kwargs)]
        return _rel(found, root)

    assert asyncio.run(go()) == _store_expected((method, pattern, case))


def test_async_store_recursive_glob_is_one_find(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _async_store()
    counts = _count(monkeypatch, memory)

    async def go() -> object:
        root = await _async_tree("calls")
        counts.update(dict.fromkeys(counts, 0))
        found = [p async for p in root.rglob("*.nii")]
        listed = {k: v for k, v in counts.items() if v}
        return found, listed, await found[0].pathinfo.is_file(), root

    found, listed, is_file, root = asyncio.run(go())
    assert _rel(found, root) == ["sub/deeper/x.nii"]
    assert listed == {"find": 1} and is_file
    assert {k: v for k, v in counts.items() if v} == {"find": 1}


# -- drivers that cannot list -------------------------------------------------
class _GlobOnly(os.PathLike):
    """A driver with its own glob and no iterdir."""

    def __init__(self, real: object) -> None:
        self._real = pathlib.Path(real)

    def __fspath__(self) -> str:
        return os.fspath(self._real)

    def __str__(self) -> str:
        return str(self._real)

    def glob(self, pattern: str) -> object:
        return (_GlobOnly(p) for p in self._real.glob(pattern))


class _ListOnly(_GlobOnly):
    """A driver with iterdir and no glob of its own."""

    glob = None  # type: ignore[assignment]

    def __truediv__(self, other: object) -> "_ListOnly":
        return _ListOnly(self._real / other)

    @property
    def name(self) -> str:
        return self._real.name

    def iterdir(self) -> object:
        return (_ListOnly(p) for p in self._real.iterdir())

    def exists(self) -> bool:
        return self._real.exists()

    def is_dir(self) -> bool:
        return self._real.is_dir()

    def is_file(self) -> bool:
        return self._real.is_file()


def test_a_driver_that_cannot_list_keeps_its_glob(
    tmp_path: pathlib.Path,
) -> None:
    root = _local_tree(tmp_path)
    found = list(Path(_GlobOnly(root)).glob("*.txt"))
    assert _rel(found, root) == _rel(root.glob("*.txt"), root)  # its own
    with pytest.raises(UnsupportedPathOperation):
        Path(_GlobOnly(root)).rglob("*.txt")


def test_a_driver_that_only_lists_can_glob(tmp_path: pathlib.Path) -> None:
    root = _local_tree(tmp_path)
    path = Path(_ListOnly(root))
    assert path.supports("glob") and path.supports("rglob")
    got = _rel(path.glob("**/*.txt"), root)
    assert got == _GLOB_314["glob", "**/*.txt", None]


# -- the pieces ---------------------------------------------------------------
def test_windows_patterns_parse_as_pathlib_does() -> None:
    from bagof.paths._glob import parse_pattern

    assert parse_pattern("a\\b/c", windows=True) == ["a", "b", "c"]
    assert parse_pattern("a\\", windows=True) == ["a", ""]
    assert parse_pattern("a/./b//", windows=False) == ["a", "b", ""]
    for anchored in ("C:x", "\\x", "/x"):
        with pytest.raises(NotImplementedError):
            parse_pattern(anchored, windows=True)
    with pytest.raises(ValueError):
        parse_pattern("./", windows=False)


def test_a_tree_answers_only_below_its_base(tmp_path: pathlib.Path) -> None:
    from bagof.paths._glob import Tree
    from bagof.paths._pathinfo import Facts

    def key(path: Path) -> str:
        return str(path)

    base = Path(tmp_path / "base")
    child = Path(tmp_path / "base" / "d")
    tree = Tree(str(base), key, {str(base): [child]})
    assert tree.scan(base) == [child]
    assert tree.scan(Path(tmp_path / "base" / "d")) == []  # listed: empty
    assert tree.scan(Path(tmp_path / "elsewhere")) is None
    link = base._listed(
        (tmp_path / "base" / "link"),
        Facts({"exists": True, "symlink": True, "dir": False, "file": False},
              listed=True),
    )
    assert tree.scan(link) is None  # a link is listed the ordinary way


def test_upath_a_file_and_a_directory_of_one_name_are_one_child() -> None:
    root = _memory_tree("collide")
    (root / "x" / "y").write_text("below")
    (root / "x").write_text("file")  # a key beside a prefix of one name
    for method, pattern in (("rglob", "*"), ("glob", "*")):
        found = [
            p for p in getattr(Path(root), method)(pattern)
            if p.name in ("x", "y")
        ]
        names = _rel(found, root)
        assert names == (["x", "x/y"] if method == "rglob" else ["x"])
        assert next(p for p in found if p.name == "x").pathinfo.is_dir()


# -- universal-pathlib over the local disk ------------------------------------
def _local_upath(root: pathlib.Path, url: bool) -> object:
    UPath = pytest.importorskip("upath").UPath
    return UPath(root.as_uri() if url else os.fspath(root))


@pytest.mark.parametrize("url", [False, True], ids=["posix", "file-url"])
@pytest.mark.parametrize("method, pattern, case", _CASES)
def test_local_upath_glob_is_pathlib_314s(
    tmp_path: pathlib.Path, url: bool, method: str, pattern: str,
    case: object,
) -> None:
    # A local store is walked one directory at a time, as pathlib walks it.
    root = _local_upath(_local_tree(tmp_path), url)
    kwargs = _kwargs(case, "case_sensitive")
    got = _rel(getattr(Path(root), method)(pattern, **kwargs), root)
    assert got == _GLOB_314[method, pattern, case]


@pytest.mark.skipif(not _HAS_LINKS, reason="no symbolic links here")
@pytest.mark.parametrize("url", [False, True], ids=["posix", "file-url"])
@pytest.mark.parametrize("method, pattern, recurse", _LINK_CASES)
def test_local_upath_links_as_pathlib_314(
    tmp_path: pathlib.Path, url: bool, method: str, pattern: str,
    recurse: bool,
) -> None:
    root = _local_upath(_link_tree(tmp_path), url)
    got = _rel(
        getattr(Path(root), method)(pattern, recurse_symlinks=recurse), root
    )
    assert got == _LINKS_314[method, pattern, recurse]


# -- a store whose recursive listing cannot help ------------------------------
def _find_that(how: str) -> object:
    memory = pytest.importorskip("fsspec.implementations.memory")
    original = memory.MemoryFileSystem.find

    def find(
        self: object, path: str, *args: object, **kwargs: object
    ) -> object:
        if how == "fails":
            raise PermissionError(path)
        found = original(self, path, *args, **kwargs)
        if how == "ignores-detail":
            return sorted(found)
        found = dict(found)  # with "." and ".." entries, as some stores do
        for name in (".", ".."):
            found[path.rstrip("/") + "/sub/" + name] = {
                "name": path.rstrip("/") + "/sub/" + name,
                "type": "directory", "size": 0,
            }
        return found

    return find


@pytest.mark.parametrize("how", ["fails", "ignores-detail", "dot-entries"])
@pytest.mark.parametrize("pattern", ["**", "**/*.txt", "sub/**", "**/*/"])
def test_upath_glob_without_a_useful_recursive_listing(
    monkeypatch: pytest.MonkeyPatch, how: str, pattern: str,
) -> None:
    # Walked one directory at a time instead (or with the extra names
    # dropped): the same matches.
    memory = pytest.importorskip("fsspec.implementations.memory")
    root = _memory_tree(f"find-{how}")
    monkeypatch.setattr(memory.MemoryFileSystem, "find", _find_that(how))
    got = _rel(Path(root).glob(pattern), root)
    assert got == _store_expected(("glob", pattern, None))


def test_upath_listing_drops_dot_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A store that lists "." and ".." (as FTP does): not children, as
    # UPath's own iterdir has it.
    memory = pytest.importorskip("fsspec.implementations.memory")
    root = _memory_tree("dot-ls")
    original = memory.MemoryFileSystem.ls

    def ls(
        self: object, path: str, detail: bool = True, **kw: object
    ) -> object:
        found = original(self, path, detail=detail, **kw)
        dots = [path.rstrip("/") + "/" + name for name in (".", "..")]
        if detail:
            dots = [{"name": n, "type": "directory", "size": 0} for n in dots]
        return dots + list(found)

    monkeypatch.setattr(memory.MemoryFileSystem, "ls", ls)
    ours = sorted(c.name for c in Path(root).iterdir())
    assert ours == sorted(c.name for c in root.iterdir())
    assert "." not in ours and ".." not in ours
    assert _rel(Path(root).glob("*"), root) == _store_expected(
        ("glob", "*", None)
    )


# -- cloudpathlib without its lazy walk ---------------------------------------
@pytest.mark.parametrize("walk", ["eager", "none"])
def test_cloudpathlib_without_a_lazy_walk(
    monkeypatch: pytest.MonkeyPatch, walk: str,
) -> None:
    # An older cloudpathlib (a walk with no `lazy`, or none at all) lists
    # each directory with its iterdir: the same matches, unlisted children.
    local = pytest.importorskip("cloudpathlib.local")
    root = _cloud_tree(f"walk-{walk}")
    cls = type(root)
    original = cls.walk

    def eager(self: object, top_down: bool = True) -> object:
        return original(self, top_down=top_down)

    monkeypatch.setattr(cls, "walk", eager if walk == "eager" else None)
    assert isinstance(root.client, local.LocalS3Client)
    children = list(Path(root).iterdir())
    assert all(c._listing_facts() is None for c in children)
    assert sorted(c.name for c in children) == sorted(
        c.name for c in root.iterdir()
    )
    for pattern in ("**/*.txt", "*"):
        got = _rel(Path(root).glob(pattern), root)
        assert got == _cloud_expected(("glob", pattern, None))
    sub = next(c for c in children if c.name == "sub")
    assert sub.pathinfo.is_dir() and not sub.pathinfo.is_file()


# -- one name, a file and a directory, in one recursive listing ---------------
def test_a_recursive_listing_with_a_file_named_as_a_directory() -> None:
    # A listing that reports `x` as a file, and `x/y` below it: `x` is one
    # child, a directory, as a directory listing has it.
    from bagof.paths._glob import tree_from
    from bagof.paths._pathinfo import typed_facts

    base = Path("/store")
    x = base._listed(base.wrapped / "x", typed_facts(False))
    y = base._listed(base.wrapped / "x" / "y", typed_facts(False))
    tree = tree_from(
        "/store", str,
        [("/store", "/store/x", x), ("/store/x", "/store/x/y", y)],
    )
    (child,) = tree.scan(base)
    assert child == x and child.pathinfo.is_dir()
    assert not child.pathinfo.is_file()
    assert tree.scan(child) == [y]


def test_a_driver_that_names_iterdir_without_one_cannot_glob(
    tmp_path: pathlib.Path,
) -> None:
    # `iterdir` is there but not callable: the shared glob asks it to list,
    # which says so, rather than delegating to itself again.
    class _Disabled(_GlobOnly):
        glob = None  # type: ignore[assignment]
        iterdir = None

    path = Path(_Disabled(_local_tree(tmp_path)))
    for method in ("glob", "rglob"):
        with pytest.raises(UnsupportedPathOperation, match="iterdir"):
            list(getattr(path, method)("*.txt"))
