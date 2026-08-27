# Third-party notices

`bagof-paths` is distributed under the MIT license (see [`LICENSE`](LICENSE)).

Parts of it are copied from, or derived from, CPython's standard library,
which is licensed under the **Python Software Foundation License Version 2**.
A copy of that license, together with the PSF copyright notice its clause 2
requires us to retain, is in
[`LICENSE-PSF-2.0.txt`](LICENSE-PSF-2.0.txt).

> Copyright (c) 2001-2026 Python Software Foundation; All Rights Reserved

Clause 3 of that license requires a brief summary of the changes made. This
file is that summary.

## Where the derived code is

### `src/bagof/paths/_match.py`

Upstream sources, both from CPython 3.13:

- [`Lib/glob.py`](https://github.com/python/cpython/blob/3.13/Lib/glob.py) —
  the `translate` function.
- [`Lib/fnmatch.py`](https://github.com/python/cpython/blob/3.13/Lib/fnmatch.py)
  — the private `_translate` helper that `glob.translate` calls.

| Ours | Upstream |
| --- | --- |
| `_glob_translate` | `glob.translate` |
| `_fnmatch_translate` | `fnmatch._translate` |

## Summary of changes

- **Vendored to guarantee 3.13 semantics on older interpreters.** `full_match`
  and its `**`-aware, whole-path glob matching only exist in `pathlib` from
  Python 3.13. `bagof-paths` supports Python 3.8+, so the two translation
  helpers are carried here to give every interpreter the same matching
  behavior.
- **Reformatted, not rewritten.** The two functions are reflowed to this
  repository's 79-column style and given type annotations and lower-case
  parameter names (`star`, `question_mark` for `fnmatch._translate`'s `STAR`,
  `QUESTION_MARK`); the matching logic is unchanged. The reference to
  `fnmatch._translate` inside `glob.translate` is rewired to the vendored
  `_fnmatch_translate`.
- **No behavioral change.** `_match.full_match` is validated against
  `pathlib.PurePosixPath.full_match` on Python 3.13 across a matrix of paths
  and patterns; `match` delegates to the stdlib `PurePosixPath.match`, which
  is present on every supported version.
