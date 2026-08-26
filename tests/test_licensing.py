"""The PSF license has to reach the people who install the package.

Parts of _match.py are derived from CPython's glob/fnmatch, so the PSF
license and the notice of changes must ship *with the distribution*, not
merely sit in the repository. Getting that wrong is silent: the build
succeeds and the file is simply absent.
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
