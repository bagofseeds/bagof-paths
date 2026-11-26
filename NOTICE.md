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

Two small parts are derived from universal-pathlib, under the MIT license;
its notice and those parts are listed [at the end](#universal-pathlib).

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

### `src/bagof/paths/_glob.py`

Upstream sources, both from CPython 3.14:

- [`Lib/glob.py`](https://github.com/python/cpython/blob/3.14/Lib/glob.py) —
  the private `_GlobberBase` class (its selectors).
- [`Lib/pathlib/__init__.py`](https://github.com/python/cpython/blob/3.14/Lib/pathlib/__init__.py)
  — `Path.glob`, `Path.rglob` and the private `PurePath._parse_pattern`.

| Ours | Upstream |
| --- | --- |
| `_Globber` (`selector`, `special_selector`, `literal_selector`, `wildcard_selector`, `recursive_selector`, `select_exists`) | `glob._GlobberBase` |
| `parse_pattern` | `PurePath._parse_pattern` |
| `select` | `Path.glob` / `Path.rglob` |

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

### `_match.py`

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

### `_glob.py`

- **Derived to guarantee 3.14 semantics on every interpreter and driver.**
  `pathlib`'s `glob` changed in 3.13 (a pattern ending in `**` or `/`),
  and `universal-pathlib` and `cloudpathlib` each have their own, so the
  3.14 selectors are carried here and run over every driver's listing.
- **Restructured.** The selectors walk path objects instead of strings
  (the relative name a regular expression is matched against is carried
  beside each path), and they do no I/O: each listing, directory test and
  existence test is yielded as a request and its answer sent back, so one
  generator serves synchronous and asynchronous callers. The trailing
  separator a string would carry is a `dir_only` flag on the existence
  test. Pattern parsing is written for `posixpath`/`ntpath` directly (no
  `splitroot`, which is 3.12+), and `Path.glob`'s normalization of the
  result strings is not needed for path objects.
- **No behavioral change.** The matches are validated against
  `pathlib.Path.glob` on Python 3.14 across a matrix of patterns, case
  rules and symbolic links (`tests/test_glob.py`).

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

## universal-pathlib

Parts of `bagof-paths` are derived from
[universal-pathlib](https://github.com/fsspec/universal_pathlib), which is
licensed under the MIT license, reproduced here as it requires:

> MIT License
>
> Copyright (c) 2022, Andrew Fulton
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

Upstream sources, from universal-pathlib 0.3:

| Ours | Upstream |
| --- | --- |
| `_drivers.UPathAdapter._listing` | `UPath.iterdir` (`upath/core.py`): which entries of `fs.ls` are children and how they are built |
| `_pathinfo._MTIME_KEYS` | the key order of `UPathStatResult.st_mtime` (`upath/_stat.py`) |

The listing asks `fs.ls` for detail, keeps each entry's type, checks for a
directory only when the listing is empty, and merges a file and a directory
of one name into the directory; the key list adds fsspec memory's
`created`. `glob`/`rglob` are not derived from universal-pathlib: they are
the CPython selectors of `_glob.py` above.
