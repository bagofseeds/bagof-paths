"""Smoke tests."""

import importlib


def test_package_is_importable() -> None:
    """The package should be importable after installation."""
    module = importlib.import_module("bagof.paths")
    assert module is not None


def test_package_ships_py_typed() -> None:
    """The package advertises inline types (PEP 561)."""
    import os

    import bagof.paths

    pkg_dir = os.path.dirname(bagof.paths.__file__)
    assert os.path.isfile(os.path.join(pkg_dir, "py.typed"))
