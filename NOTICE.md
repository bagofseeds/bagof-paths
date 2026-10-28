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

### `src/bagof/paths/_base.py`

Upstream source, from CPython 3.14:

- [`Lib/urllib/request.py`](https://github.com/python/cpython/blob/3.14/Lib/urllib/request.py)
  — `url2pathname` and the private `_is_local_authority`, which
  `pathlib.Path.from_uri` calls.

| Ours | Upstream |
| --- | --- |
| `_local_from_file_uri` | `url2pathname(require_scheme=True)` and the checks of `Path.from_uri` |
| `_hostname` | the hostname comparison of `_is_local_authority` |

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

### `_base.py`

- **Derived to guarantee 3.14 semantics on every interpreter.** Python 3.14
  reads a `file:` URI as a URL (its query and fragment are not part of the
  path), where 3.13 and earlier read a `?` literally. `bagof-paths` supports
  Python 3.8+, so the rules are carried here to give every interpreter the
  same answer.
- **Restructured.** The URL is split with `urllib.parse.urlsplit` and
  decoded with `urllib.parse.unquote` directly, and the checks `from_uri`
  adds (a `file:` scheme, an absolute result) are folded into one function.
  A `URLError` is raised as `ValueError`, as `from_uri` does.
- **Narrowed.** The optional `resolve_host` look-up (a DNS query) is left
  out: an authority is local only when it is empty, `localhost`, or this
  machine's hostname, which is `url2pathname`'s default.
- **No behavioral change.** `_local_from_file_uri` is validated against
  `pathlib.Path.from_uri` on Python 3.14 across a matrix of `file:` URIs.
