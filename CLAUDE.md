# CLAUDE.md — bagof-paths

Repo-specific guidance for coding agents. bagofseeds publishes standalone
**`bagof-*`** packages (this is one) and the **`fiery-*`** namespace; they
share packaging, CI, docs, and workflow conventions. For those shared
conventions see the org guide (`bagofseeds/.github`, `CONTRIBUTING.md` +
`CLAUDE.md`). This file records only what is specific to `bagof.paths`.

## What this package is

A **wrapper** over any path-like object — stdlib `pathlib.Path`,
universal-pathlib's `UPath`, cloudpathlib's `CloudPath`/`AnyPath`, or an
unknown driver — exposing one uniform, `pathlib`-style surface. For each
member of that surface it does one of three things:

- **delegate** to the wrapped object when it implements the member;
- **synthesize a fallback** from more primitive members it does have
  (`read_text` from `read_bytes`, `read_bytes` from `open`, `copy` from
  `shutil`, `walk` from `iterdir`, …);
- **raise** one named error, `UnsupportedPathOperation`, when neither is
  possible.

Anything a driver exposes that the surface does not name is still reachable
through `path.wrapped`. A sync `Path` and an async `AsyncPath` share the whole
surface; `AsyncPath` runs a synchronous driver in a worker thread.

## Layout

```
src/bagof/paths/
  __init__.py    # re-exports, and nothing else -- the public face
  _constants.py  # SCHEME_RE, member kinds/result policies, the member sets
                 #   (ADAPTER_MEMBERS, COMPUTED_MEMBERS, ACCESSOR_MEMBERS),
                 #   LOCAL_PROTOCOLS
  _errors.py     # UnsupportedPathOperation, NoDriverError
  _spec.py       # the Member table: the whole delegated surface, once
  _engine.py     # the policy engine: delegate -> normalize kwargs -> unwrap
                 #   wrapper args -> re-wrap path results -> else fallback ->
                 #   else raise
  _base.py       # BaseWrapper: construction/selection, identity, derivation,
                 #   the location + driver-specific accessor properties
  _fallbacks.py  # the synthesis functions named by Member.fallback
  _match.py      # vendored CPython 3.13 glob.translate (PSF-licensed) for
                 #   match/full_match, computed lexically on the canonical path
  _drivers.py    # driver adapters (rmdir/copy/move/walk divergence) +
                 #   register_driver
  _protocols.py  # ProtocolTraits, register_protocol, canonical_scheme
  _detect.py     # is_async_driver: whether a driver's members are coroutines
  _select.py     # driver selection: build a backend from a URL string
  _purepath.py   # PurePathMixin: the lexical surface, shared sync by both
  _path.py       # Path: thin, real-signature methods over the engine
  _async_path.py # AsyncPath + AsyncFile: the async surface over the bridge
  _bridge.py     # run a blocking callable in the default thread pool
tests/
  test_import.py, test_identity.py, test_derivation.py
  test_path_sync.py, test_path_io.py, test_fallbacks.py, test_match.py
  test_internals.py, test_drivers.py, test_async.py
  test_parity_gap.py          # the pathlib/UPath/cloud surface parity
  test_driver_selection.py    # URL construction + register_protocol
  test_parity.py              # sync/async surfaces stay in lockstep
  test_docstrings.py          # every pycon block in a docstring or page
  test_licensing.py
```

**Every module is private; `__init__.py` holds no code.** A name a user should
reach is re-exported there and listed in `__all__`. griffe builds the API
reference from dotted paths, so a public function and a module of the same
name would collide.

## How it works

- **The surface is described once, in `_spec.py`.** A `Member` says how a
  pathlib member is delegated: its `kind` (PURE = lexical, never async; IO =
  touches a filesystem, gets an async counterpart), how its result is treated
  (`SCALAR` / re-wrap a path / a tuple or iterator of paths), the kwargs
  forwarding policy (`normalize`: forward a keyword only when it differs from
  its default, so a driver with an older signature still works), and an
  optional `fallback` with the `needs` that gate it.
- **`_engine.py` is the one place the delegate/fallback/raise decision lives.**
  Every `Path`/`AsyncPath` method is a thin call into it, so the policy is
  written once rather than in sixty method bodies.
- **`_purepath.py` holds the lexical members** — they never block, so both
  wrappers inherit them as the same synchronous methods. IO members are the
  only thing written twice (a sync method on `Path`, an async one on
  `AsyncPath`); `test_parity.py` fails the moment the two drift.
- **`_drivers.py` absorbs the operations backends genuinely disagree on** —
  `rmdir` recursion, native `copy`/`move`, `walk` — behind adapters selected
  by driver type. A `GenericAdapter` duck-types any driver; named subclasses
  override only the points a known driver diverges. **Data-loss defaults are
  defused here** (UPath's bare `rmdir()` recurses, so the wrapper always
  passes `recursive=False`).
- **`_base.py` builds and identifies the wrapper.** Identity is
  driver-independent: `__eq__`/`__hash__` on a canonical `(protocol, path)`
  key, scheme aliases folded, family-scoped (a sync path never equals an async
  one). Derivation flows through one hook, `with_wrapped`, so a subclass's
  state rides onto every derived path.
- **`AsyncPath` handles two kinds of driver.** A *synchronous* driver is run
  on a sync view of the same driver in a worker thread (`_bridge.run` =
  `loop.run_in_executor` + `functools.partial`, stdlib only, 3.8-safe), so no
  policy is written twice. A *natively-async* driver (coroutine members, e.g.
  `anyio.Path`) is awaited directly through the `_call`/`_aiter`/`open` seam;
  the few members it lacks (`copy`, `walk`, ...) fall back to a **local stdlib
  view** run in a thread, and results are re-wrapped back into the driver's
  family. Which strategy applies is `_is_async_driver(wrapped)`, worked out
  once per driver type and cached; a non-local async driver has no local
  view, so a member it lacks raises rather than round-tripping through a cache
  path.

### Adding a member to the surface

1. Add a `Member` to `_spec.py` (pick `kind`, `result`, `normalize`, and a
   `fallback`/`needs` if it can be synthesized).
2. If it can be synthesized, add the function to `_fallbacks.py` and its name
   to `FALLBACKS`.
3. Add a thin method on `Path` (sync) and, for an IO member, on `AsyncPath`
   (async, bridged) with **matching signatures** — `test_parity.py` enforces
   this.
4. Lexical members go on `PurePathMixin` instead (shared, sync on both).
5. A driver-specific *accessor* (a property like `bucket`/`fs`) lives on
   `BaseWrapper` and is listed in `ACCESSOR_MEMBERS`; `supports()` answers for
   it from the driver **class**, never by reading the property (reading
   universal-pathlib's `fs`/`info` builds the filesystem and can raise).

### Driver selection (`_select.py` + `_protocols.py`)

`Path("s3://…")` picks a backend: explicit `driver=` > a protocol's registered
preference > universal-pathlib > cloudpathlib's concrete class > `NoDriverError`.
universal-pathlib is the automatic default (it builds any fsspec URL lazily);
cloudpathlib is the fallback, selected from its own registry — never `AnyPath`,
which silently returns a local path for an unknown scheme. Only
universal-pathlib's *unknown-scheme* `ValueError` becomes `NoDriverError`; a
recognised scheme's real error, and any preferred driver's error, propagate.
`register_protocol` carries a scheme's traits (bucketed, aliases, absolute) and
optional preferred driver in one call; register protocols **at import time**,
since traits feed a path's canonical identity.

**Credentials ride on `storage_options`.** `Path(url, storage_options={...})`
forwards a connection dict to the chosen factory. Per-scheme defaults are set
with `set_storage_options(scheme, {...})`, which keeps the scheme's other
traits; `register_protocol` **replaces a scheme wholesale**, so it is for
*defining* a scheme, not for adding options to a built-in one (doing that
would drop `bucketed`/`absolute` and detach aliases). A per-call dict
overrides the per-scheme default key by key; the merge lives in
`_protocols.merged_storage_options`, used by both `_select.build` and the
explicit-`driver=` path in `_build_from_string` (so an explicit driver still
gets the scheme's defaults). The factory contract is `driver(text,
**options)`, called with the URL alone when there are no options (so a
historical `str -> path` driver still works). A per-scheme default keys on the
leading scheme, so an fsspec **chain** URL (`simplecache::s3://...`, whose
identity scheme is empty) carries per-call options only. `storage_options` is valid
only for a remote URL: a local path, a local scheme, or a pre-built driver
object with options is a `TypeError`. cloudpathlib takes a `Client`, not an
options dict, so options + only cloudpathlib available is a `TypeError`
pointing at `driver=`. Secrets never reach a printed surface:
`ProtocolTraits.__repr__` redacts `storage_options` to `<redacted>`; the
`path.storage_options` accessor deliberately returns the live dict (secrets and
all) and says so. A **synchronous `Path` refuses an async driver** at
construction (`is_async_driver` in `_detect.py`, shared with `AsyncPath`) since
a sync method over coroutine members returns un-awaited coroutines.

## Conventions specific to this repo (do not regress)

1. **Wide Python (3.8+).** Runtime code must stay old-compatible: no walrus in
   paths 3.8 rejects, no PEP 604 `|` or PEP 585 `list[...]` in *values*, never
   subscript an abc/builtin generic at runtime. Modern typing lives in
   **annotations only** — every module starts with `from __future__ import
   annotations`.
2. **All typing goes through `import typing_extensions as tx`.** Do not import
   from `typing` or `collections.abc`.
3. **The core stays dependency-free.** Only `typing_extensions`; `upath` and
   `cloudpathlib` are optional extras (Python ≥ 3.9), so the 3.8 CI leg is
   core-only. Tests against the real backends use `pytest.importorskip`. The
   coverage job installs the backends (they are in `tests/requirements.txt`),
   so adapter/accessor code measured there is exercised against them.
4. **Never leak an internal name into a user-facing error.** Name the
   operation and the driver, never a private helper.
5. **`match`/`full_match` are computed lexically** on the canonical path via
   the vendored `_match.py`, never delegated — so they are identical across
   drivers and Python versions.

## When a behaviour is a judgement call, check the neighbours first

`pathlib`, `UPath`, and `cloudpathlib` have met most of these questions.
Before deciding one by argument, **find out what they do — by running it**:

```sh
python3 -m venv /tmp/priorart && /tmp/priorart/bin/pip install -q universal-pathlib cloudpathlib
```

The design records where their behaviours are reconciled and why (see
`design/path-wrapper.md` §5, the measured divergence table). Where a
divergence would silently corrupt data (UPath's recursive `rmdir`,
cloudpathlib's `unlink(missing_ok=True)` default), the wrapper picks the safe
answer and there is a regression test for it.

## Documentation style (`README.md`, `docs/*.md`, public docstrings)

Docs are for Python developers who want a uniform path API — not experts in
fsspec, cloud SDKs, or this wrapper's internals.

1. **Lead with what the user writes**; show the simplest spelling first.
2. **Say what it does, not how it works** — no internal names or private
   attributes in a public docstring (mkdocstrings renders it as a doc page).
3. **Real `pycon`, not pseudo-code.** `tests/test_docstrings.py` runs every
   ```pycon``` block in a docstring, the README, and `docs/comparison.md`, on
   the 3.8 core-only leg too. So a runnable example must use a **local** path
   (a `s3://` example needs a backend and would fail there) — put cloud
   illustrations in a plain ```python``` fence, which the site renders but the
   test never runs.
4. **Short sentences, no filler.**

## Gate before a PR

```sh
pip install .[test]
cd /tmp && python -m pytest <repo>/tests -q     # from a neutral cwd
ruff check src tests
codespell src tests docs
```

**Run more than one Python before pushing** if you touched anything
version-sensitive — `pathlib` gained members across versions (`walk` 3.12,
`copy`/`info` 3.14; `is_junction` 3.12; `hardlink_to` 3.10) and drops others
(`link_to` removed 3.12). CI covers 3.8 and current, and those ends are where
things differ; a member present on one interpreter and synthesized on another
will only show the gap on the version that lacks it. The coverage job runs a
single current interpreter, so a fallback that only fires on old Pythons needs
a test that drives it with a driver lacking the native member (see the
walk-fallback and `hardlink_to`-fallback tests) or it reads as uncovered.

## Deferred (not yet built)

- A **dependency-free fallback driver** so a remote scheme works with no
  backend installed (today `Path("s3://…")` needs `upath` or `cloudpathlib`).
  The selection availability tail is an error branch it would append to.
- A **sync-over-async portal** -- `Path` (synchronous) over an async driver,
  by driving an event loop. (`AsyncPath` over an async driver is done; the
  reverse is not.) Also an async *cloud* path: no mature async cloud path
  object exists, so async `s3://` would mean adapting fsspec's
  `AsyncFileSystem` (filesystem-shaped, not a path).
- A **`to()`** converter between drivers.
