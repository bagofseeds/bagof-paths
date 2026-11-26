"""The PSF and MIT notices have to reach the people who install the package.

Parts of _match.py, _glob.py and _base.py are derived from CPython, so the
PSF license and the notice of changes must ship *with the distribution*,
not merely sit in the repository. Two small parts are derived from
universal-pathlib, whose MIT license asks for its copyright and permission
notice in every copy; NOTICE.md carries them. Getting that wrong is silent:
the build succeeds and the text is simply absent.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (ROOT / "pyproject.toml").is_file(),
    reason="running from an installed copy, not a checkout",
)


def _license_files() -> list:
    """The `license-files` entries declared in `pyproject.toml`."""
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r"^license-files\s*=\s*\[(.*?)\]", text, re.M | re.S)
    assert match, "pyproject.toml declares no license-files"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_psf_license_and_notice_are_declared() -> None:
    declared = _license_files()
    assert "LICENSE" in declared
    assert "LICENSE-PSF-2.0.txt" in declared
    assert "NOTICE.md" in declared


@pytest.mark.parametrize("name", _license_files())
def test_every_declared_license_file_exists(name: str) -> None:
    assert (ROOT / name).is_file(), f"{name} is declared but missing"


@pytest.mark.parametrize("name", _license_files())
def test_every_declared_license_file_is_at_the_root(name: str) -> None:
    # A subdirectory pattern is accepted by the config and then silently
    # ignored by every setuptools before 77 -- the file never reaches the
    # wheel.
    assert "/" not in name, f"{name} must be at the repository root"


def test_the_psf_license_carries_the_copyright_notice() -> None:
    # Clause 2 requires the notice to be retained in a derivative work.
    text = (ROOT / "LICENSE-PSF-2.0.txt").read_text()
    assert "Python Software Foundation; All Rights Reserved" in text
    assert "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in text


def test_the_notice_summarises_the_changes() -> None:
    # Clause 3 requires a summary of the changes made to Python.
    text = (ROOT / "NOTICE.md").read_text()
    assert "Summary of changes" in text
    assert "glob" in text


def test_the_notice_names_each_psf_derived_module() -> None:
    text = (ROOT / "NOTICE.md").read_text()
    for module in ("_match.py", "_glob.py", "_base.py"):
        assert f"### `src/bagof/paths/{module}`" in text, module
        assert f"### `{module}`" in text, module  # its summary of changes


def test_the_notice_carries_the_mit_notice_of_universal_pathlib() -> None:
    # MIT: "The above copyright notice and this permission notice shall be
    # included in all copies or substantial portions of the Software."
    text = (ROOT / "NOTICE.md").read_text()
    assert "## universal-pathlib" in text
    assert "Copyright (c) 2022, Andrew Fulton" in text
    assert "Permission is hereby granted, free of charge" in text
    assert "The above copyright notice and this permission notice" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text


@pytest.mark.parametrize(
    "module, marker",
    [
        ("_drivers.py", "universal-pathlib's UPath.iterdir (MIT; NOTICE.md)"),
        ("_pathinfo.py", "universal-pathlib's own stat (MIT; NOTICE.md)"),
    ],
)
def test_the_mit_derived_code_points_at_the_notice(
    module: str, marker: str
) -> None:
    # The code NOTICE.md names says so where it is, so the two stay paired.
    source = ROOT / "src" / "bagof" / "paths" / module
    assert marker in " ".join(source.read_text().split())
