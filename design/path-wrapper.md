# Design plan — `bagof.paths` path wrappers

Status: **REVIEWED DRAFT.** This is a design document, not an implementation.
It proposes the public API and internal architecture for the path-wrapper
feature of `bagof-paths`, driven by the reference implementation in
`neuroscales/abczarr` (`src/abczarr/_core/path.py`) and by its real downstream
use in that repo. Decisions marked **[settled]** were fixed in review, either
by running the prior art (universal-pathlib 0.3.10, cloudpathlib 0.25.0,
CPython 3.11 `pathlib`) or by house convention; the reasoning is recorded
inline. Decisions marked **[owner]** are genuinely open and need the repo
owner's call. See `fable-review.md` for the review memo.

---

## 1. What we are building

A **path wrapper**: a class that takes any path-like object — `pathlib.Path`,
`upath.UPath`, `cloudpathlib.AnyPath`, an fsspec path, a future/unknown driver —
and exposes one normalized `pathlib`-style API on top of it. For each member of
that API it does one of three things:

1. **Delegate** — the wrapped object implements it, so call through.
2. **Fall back** — the wrapped object lacks it, but we can synthesize it from
   more primitive operations it *does* have (e.g. `read_text` from
   `read_bytes`, `read_bytes` from `open`, `copy` from `shutil`).
3. **Raise** — it cannot be delegated or synthesized, so raise a single,
   consistent, well-named exception.

Two front-ends over the same surface:

- a **sync** wrapper (`Path`), and
- an **async** wrapper (`AsyncPath`) that additionally bridges a *sync* driver
  by running its blocking methods in a worker thread.

The reverse direction — `Path` over a natively *async* driver, driving its
coroutines to completion — is **deliberately deferred** (see §7). It requires
a dedicated background event-loop thread (the fsspec `sync()` pattern; a bare
`asyncio.run` cannot be nested inside a running loop), no current consumer
needs it, and in v1 `Path` raises the standard unsupported-operation error
with a message pointing at `AsyncPath`. Shipping it later is additive, so
deferring costs no API stability.

### Goals (in priority order)

1. **API stability.** The public surface is small, named deliberately, and
   changes rarely. Internals are free to move.
2. **Consistency.** The same call behaves the same way regardless of which
   driver backs it. Divergence between drivers is absorbed in one place, not
   scattered across method bodies. §5 records the measured divergences and the
   canonical answer for each.
3. **Extensibility.** Downstream packages (abczarr's `StorePath` is the proof
   case) add behavior, new drivers, and new protocols without editing this
   package and without re-deriving a class tree by hand. The v1 target is:
   **a downstream subclass is one class and one method override** (§6).
4. **Correctness on the whole 3.8–3.14 range**, with the `pathlib` surface
   itself having changed across those versions (`copy`/`move` in 3.14,
   `full_match` in 3.13, `walk_up`/`case_sensitive`/`follow_symlinks` kwargs
   added at various points).

### Non-goals (initially)

- Being a *filesystem* abstraction. We wrap path objects; we do not reimplement
  fsspec. I/O beyond what `pathlib`/`UPath` already offer is out of scope.
- **Per-protocol wrapper subclasses** (`S3Path`, `GCSPath`, …). Protocol-varying
  behavior is data, not types, in this design (§6). Nothing public dispatches
  on protocol at the class level, so this can be added later without breakage
  if a real need appears; the reverse (removing shipped classes) cannot.
- Sync-over-async bridging (§7), parameterized generics, and the long tail of
  `pathlib.PurePath` operator surface — scheduled, not first-cut.

---

## 2. Downstream use cases (evidence, not speculation)

From `abczarr` (the only current consumer), measured by grep across the repo:

- **Most-used members**, in order: `name` (53), `protocol` (33), `path` (32),
  `exists` (27), `open` (23), `parts` (12), `parent` (12), `as_uri` (7),
  `resolve` (7), `joinpath` (7), `unlink`/`rmdir` (6 each), `is_file`/`is_dir`
  (6 each), `suffix`/`stem`/`mkdir` (5 each), `with_suffix`, `iterdir`,
  `rglob`, `glob`, `read_*`/`write_*`, `absolute`, `stat`.
- **fsspec/cloud members are first-class**, not fringe: `protocol` and `path`
  are the #2 and #3 most-used members overall. Any "just be `pathlib`" design
  that treats these as extras is wrong for our actual consumer.
- The tensorstore driver builds kvstore specs from
  `{"bucket": self.bucket, "path": self.path}` — it needs the **bucket** and
  the **path-within-bucket** as separate values, not only the full
  after-scheme path. This motivates the `drive`/`path` canonicalization and
  the optional bucket-relative accessor in §5.
- Every `rmdir` call site that wants recursion passes `recursive=True`
  **explicitly** (`p.rmdir(recursive=True)` in `drivers/tensorstore.py`), so a
  safe non-recursive default costs the consumer nothing (§5).
- **Downstream subclasses the wrapper.** `abczarr.abc.path.StorePath` adds a
  `read_only` flag and then redeclares six protocol subclasses plus six more
  for the async variant — 12 near-empty classes, purely to thread the subclass
  through the reference's protocol dispatch. Worse, `register_subclass` writes
  into a registry **shared with the base**, so importing `abczarr.abc.path`
  silently changes what `WrappedPath("s3://…")` constructs everywhere. Also:
  because derivation happens via `type(self)(new_wrapped)`, the `read_only`
  flag is **lost on every derived path** (`p.parent`, `p / "x"`, glob results
  are all writable). §6 fixes all three.
- **Both sync and async are used**, with parallel class trees that in the
  reference have already drifted (the async tree has bugs the sync tree does
  not — §11). The design must make drift structurally hard, not merely
  discouraged (§3).

---

## 3. The core architecture **[settled: thin methods over a shared sync engine]**

The reference spells out every method by hand, twice (once sync, once async),
each body doing its own `hasattr`/`getattr` probing and its own kwargs
normalization. ~2400 lines, and the two copies have diverged.

Four candidate mechanisms were considered:

- **A — table-driven `__getattr__`/descriptors.** One registry, one dispatcher,
  minimal code. **Rejected**: the entire surface types as `Any`, which
  forfeits the thing a `py.typed` package exists for. mypy, pyright, and IDE
  completion are the first line of defense for a path API whose whole value
  is "reads like `pathlib`".
- **B — codegen (write the two `.py` files, or write methods into the class at
  build time à la `bagof-magic`).** One source of truth and real methods.
  **Rejected**: build-time method injection still needs a hand-maintained
  `.pyi` (magic pays that cost because `exec` is the *only* way to make a real
  `__init__` signature; nothing here needs `exec`). Checked-in generated files
  are reviewable but the org has no precedent or tooling for a generation
  step, and the surface is fixed and slow-moving — `pathlib` grows a couple of
  members per Python release. Codegen's payoff scales with churn we won't have.
- **D — one native core, other flavor as facade** (async-native core with a
  sync facade that drives the loop, or sync-native core with an async facade).
  An async-native core is **rejected** outright: it routes every local
  `pathlib` call through an event loop, punishing the overwhelmingly common
  case, and forces the nested-loop problem (§7) into the *default* path
  instead of the deferred one. But the sync-native half of this idea is right,
  and is folded into the chosen design below.
- **C — hand-written thin methods over a shared policy engine.** **Chosen.**

### The chosen shape

- **`_spec.py` — one `Member` table** describing the whole surface (§4): kind,
  kwargs-forwarding policy, result policy, fallback chain, capability
  requirement. This is the single source of truth for *behavior*.
- **`_engine.py` — one sync policy engine.** `call(wrapper, member, args,
  kwargs)` implements delegate → fallback → raise, kwargs normalization, and
  result re-wrapping, consulting the spec and the driver adapter (§5). All
  consistency logic lives here, written once, in sync code.
- **`Path`** methods are thin and real: full signature, full docstring, body
  is one line into the engine. Typing works, IDEs work, mkdocstrings works.
- **`AsyncPath`** methods are equally thin: `async def exists(...)` awaits the
  bridge (§7), which either (a) runs the *entire* engine call — delegation
  and any fallback composition included — in one worker thread when the
  driver is sync (one thread hop per operation, not per primitive), or
  (b) awaits the driver member directly when it is natively async.
- The **only genuine duplication** is (i) the thin signatures and (ii) the
  handful (~8) of fallback compositions that must exist in an async spelling
  for natively-async drivers whose surface has gaps. Both are pinned by an
  **automated parity test** (`tests/test_parity.py`): introspect `Path` and
  `AsyncPath`, assert the same member set and the same signatures modulo
  `async`, and assert every spec entry has a method on both classes and vice
  versa. Drift becomes a test failure, not a code review hope.

### Docstrings under this shape

mkdocstrings/griffe reads docstrings **statically**, so attaching shared
docstrings at import time is invisible to the docs build. Policy: full
docstrings (house style, real `pycon`) live on `Path`; each `AsyncPath`
member carries a one-line docstring — "Async version of
[`Path.exists`][bagof.paths.Path.exists]." — plus any async-specific note
(e.g. what `open` returns, §7). This is honest, renders correctly, and keeps
the drift surface near zero. The parity test checks the cross-reference
exists.

---

## 4. Delegate / fallback / raise, made explicit

Each `Member` spec entry carries:

- `name` — the pathlib member.
- `kind` — pure-path (lexical, always available, never async) vs. concrete
  (touches a filesystem, can be async).
- `result` — scalar / re-wrap (return value is a path → re-wrap via the
  derivation hook, §6) / iterator-of-paths / `walk`-tuple (re-wrap only the
  first element of each `(dirpath, dirnames, filenames)` tuple — the
  reference wraps the whole tuple, §11 bug 12).
- `kwargs` — the forwarding policy per keyword. The default rule is the
  reference's best idea, kept: forward a keyword **only when it differs from
  the canonical default**, so drivers with older signatures still work. Two
  refinements the reference lacks:
  - When a driver's *own* default differs from the canonical default (e.g.
    cloudpathlib `unlink(missing_ok=True)` vs. canonical `False`), the
    adapter marks the keyword **always-forward** for that driver — otherwise
    "don't pass defaults" silently adopts the driver's divergent behavior.
  - When a driver takes the same keywords in a different *order* (cloudpathlib
    `touch(exist_ok, mode)` vs. pathlib `touch(mode, exist_ok)`), the engine
    forwards by keyword, never positionally.
- `fallback` — an optional synthesis chain (below).
- `requires` — which primitive member(s) must exist for the fallback to work
  (e.g. `open`); if absent, state 3.

**Path-taking members get a target policy.** `copy`, `move`, `rename`,
`replace`, `copy_into`, `move_into`, `samefile`, `relative_to`,
`is_relative_to` accept `str | os.PathLike | Path`. A wrapper argument is
unwrapped before delegation; a returned/constructed target is wrapped in the
*source's* family via the derivation hook, with the same driver class where
the target was a plain string. (The reference's TODO list asks for exactly
this; it never implemented it.)

The coercion is written once (`_args.py`, issues #30/#31) and follows
`pathlib`: a wrapper becomes its driver; a driver object (anything with
`parts` or `protocol`) is kept, since it carries its own configuration and
cloudpathlib's `__fspath__` downloads; any other `os.PathLike` is read with
`os.fspath`; `bytes` (or a `__fspath__` returning bytes) is a `TypeError`,
never decoded (a URL or key is UTF-8, `os.fsdecode` is not). A `Member` marks
its path arguments (`paths=True`), so `write_bytes(b"...")` data is untouched.
On a remote path a URL segment of `joinpath`/`/` counts as absolute, as an
absolute path does in `pathlib` and as the same URL given as a wrapper
already did: the join restarts from it, built through the driver's
`with_segments`; a URL of another scheme is a `ValueError` (universal-pathlib
refuses to combine protocols too). A local path keeps `pathlib`'s reading,
where `"s3://b"` is a name. Measured: universal-pathlib appends a URL string
(`memory://b/memory://b/d`) and `str()`s bytes (`b/b'x'`); cloudpathlib
appends a URL string too.

**State 3 (raise) gets one exception type.** `bagof.paths.UnsupportedPathOperation`.
Bases **[settled]**: `pathlib.UnsupportedOperation` where it exists (3.13+,
itself a `NotImplementedError` subclass), else `NotImplementedError` — so both
`except NotImplementedError` and, on modern Pythons, `except
pathlib.UnsupportedOperation` catch it. The conditional base is three lines in
`_errors.py`. The message names the *operation* and the *driver class*
(`"S3Path (cloudpathlib) does not support chmod(), and it cannot be
synthesized"`), never an internal helper — house error-message rules apply.
The reference raises a scattered mix (`NotImplementedError`,
`FileNotFoundError`, bare `ValueError`); we standardize.

**Capability introspection is public.** `path.supports("copy") -> bool` and
`path.capabilities() -> frozenset[str]`, computed from the spec + adapter +
`hasattr` probes, **fallbacks included** (`supports("copy")` is `True` when
the driver lacks `copy` but has the primitives the fallback needs). Documented
caveat: this is *static* support — a driver may still raise at call time for a
particular path (`as_uri` on a relative path, per-filesystem
`NotImplementedError`s inside fsspec). `supports` answers "is this operation
wired", not "will this call succeed".

### Fallbacks to port (verified in the reference, cleaned up)

- `read_text` ⇐ `read_bytes().decode()`; `read_bytes` ⇐ `open("rb").read()`.
- `write_text` ⇐ `write_bytes(data.encode())`; `write_bytes` ⇐ `open("wb")`.
- `copy`/`copy_into`/`move`/`move_into` ⇐ `shutil` + `is_dir`/`is_file` —
  local drivers only; for a non-local driver without native `copy` the chain
  is `read_bytes`/`write_bytes` streaming for files, else state 3.
- `match`/`full_match` **[settled: implement correctly, do not raise]** — the
  reference falls back to `fnmatch`, marked `# FIXME`, which gets `**` and
  right-anchoring wrong. CPython 3.13 ships the correct pattern→regex
  translation as `glob.translate`; **port it** into `_match.py` for the whole
  3.8–3.14 range. The repo already has the PSF-porting discipline
  (`LICENSE-PSF-2.0.txt` + `NOTICE.md` component table, as in `bagof-magic`);
  this is a small, well-tested function and shipping a quietly-wrong matcher
  or an arbitrarily-raising one are both worse. Where the driver has a native
  `match`/`full_match`, delegate (UPath and cloudpathlib both do).
- `as_url` ⇐ `as_uri()` — a plain URI is the driver-independent URL, so a
  local path answers `as_url()` with its `file://` URI; presigning and other
  keyword options are driver-native (a driver that supports them ships its own
  `as_url`). `joinuri` stays **delegate-or-raise**: `urljoin(as_uri(), …)`
  yields a bare URL string with no unambiguous way to re-wrap it as a path for
  a non-URL driver, so it is offered only where the driver (universal-pathlib)
  implements it.

---

## 5. Absorbing driver divergence (measured, and the canonical answers)

This is where "consistency" is won or lost. The table below is **measured**
(universal-pathlib 0.3.10, cloudpathlib 0.25.0, CPython 3.11), for
`s3://bucket/key/a/b.txt` (gs:// behaves identically except cloudpathlib
names the accessor `blob` instead of `key`):

| Concept | pathlib | UPath | cloudpathlib | **Canonical (wrapper)** |
|---|---|---|---|---|
| `protocol` | — | `'s3'`; `''` for local | absent (`cloud_prefix = 's3://'`) | `'s3'`; `''` for local (UPath's shape) |
| `path` | — (`str(p)`) | `'bucket/key/a/b.txt'` | absent (private `_no_prefix` matches UPath) | everything after `scheme://`, bucket included; `str(p)` for local — the fsspec convention, which is `.path`'s stated purpose |
| `drive` | `''` | `'bucket'` | `'bucket'` | `'bucket'` for bucketed protocols, else native |
| `root` | `'/'` | `'/'` | **absent** | `'/'` |
| `anchor` | drive+root | `'bucket/'` | `'s3://'` | `'bucket/'` — preserves pathlib's algebra `anchor == drive + root == parts[0]`, which cloudpathlib's answer breaks |
| `parts` | native | `('bucket/', 'key', 'a', 'b.txt')` | `('s3://', 'bucket', 'key', 'a', 'b.txt')` | UPath's shape, same reason |
| `rmdir` | `rmdir()`, must be empty | `rmdir(recursive=True)` — **deletes non-empty trees by default** (verified on `memory://`) | `rmdir()`, no flag, raises `DirectoryNotEmptyError`; recursive is a separate `rmtree()` | `rmdir(recursive=False)`; see below |
| `unlink` | `missing_ok=False` (kw-only) | `missing_ok=False` (positional) | `missing_ok=True` — a documented legacy wart | `missing_ok=False`, kw-only; **always** forwarded to cloudpathlib |
| `copy`/`move` | 3.14+ only | native, `copy(target, **kw)` | native, `(target, follow_symlinks=True, preserve_metadata=False, force_overwrite_to_cloud=None)` | delegate; fallback per §4 on old pathlib |
| `relative_to` | returns `Self` | returns `Self` | **returns `PurePosixPath`** | wrapper re-wraps the result, absorbing this |
| `__fspath__` | `str(p)` | **raises `AttributeError`** on cloud paths | **downloads the file** to a local cache and returns the cache path (network I/O; raises `ClientError` without credentials) | see below |
| `as_posix` | native | `'s3://bucket/…'` | **absent** | synthesize from `str` |
| equality | native | `UPath(u) == AnyPath(u)` is `False` both ways even when `str` is equal | same | wrapper-level equality; see below |
| bucket-relative | — | — (compute `path` minus `drive`) | `.key` (s3) / `.blob` (gs) | see below |
| `glob` | `recurse_symlinks` 3.13+ | default `False` | default `True` (moot: no cloud symlinks) | pathlib defaults |
| `touch` | `(mode, exist_ok)` | `(mode, exist_ok)` | `(exist_ok, mode)` — **order differs** | forward by keyword only |
| `exists`/`is_*` on an error | < 3.14: raise any `OSError` but "not found" (`ENAMETOOLONG`, `EACCES` on a parent); 3.14: `os.path.exists`/`isfile`/`isdir`, the rest catch `(OSError, ValueError)` — always `False` | the backend's own | the backend's own | local path: 3.14 on every version, `False`; `stat()` still raises. Remote: the driver's own (issue #24) |

**Mechanism [settled: one generic adapter, named adapters override only
divergences].** The adapter layer is an inheritance tree rooted in a single
**`GenericAdapter`** that duck-types its way to the canonical answers on *any*
path-like object, with named subclasses (`UPathAdapter`,
`CloudPathAdapter`, …) overriding **only** the points where a family
measurably diverges. The generic adapter is simultaneously the base class and
the default: an unknown driver needs **no registration** — it gets the
generic adapter and "probably works", degrading member-by-member through the
fallback chain rather than failing wholesale. The adapter is chosen once per
wrapped object (`isinstance` against a registry for the named families, generic
otherwise) and cached on the wrapper.

The generic adapter's probe order is: **trust a matching attribute, else parse
the string, else synthesize.** E.g. `protocol` = `.protocol` if it is a `str`,
else the scheme parsed from `str(p)`, else `''`; `path` = `.path` if `str`,
else `str(p)` minus `scheme://`. This was verified against all three families:
the generic chain alone produces the canonical `protocol`/`path`/`drive` for
pathlib, UPath (s3/memory/local), **and** cloudpathlib — cloudpathlib has no
`.protocol`/`.path` at all, and the parse-the-string branch canonicalizes it
for free.

Measured override budget for the named adapters (this is the evidence the
inheritance model holds up — the divergences are few and clean):

- **`pathlib`**: zero overrides; the generic adapter is exact.
- **`UPathAdapter`**: one override, but a mandatory one — `rmdir` forwarding
  must pass `recursive=` **explicitly both ways**, because UPath's bare
  `rmdir()` deletes non-empty trees (the generic adapter's bare delegation
  would silently inherit a data-loss default). Nothing else: UPath is already
  canonical-shaped.
- **`CloudPathAdapter`**: three overrides — (i) canonical `parts` (from which
  `root`/`anchor` derive), since cloudpathlib's native parts embed the scheme;
  (ii) recursive `rmdir` maps to `rmtree()`; (iii) `unlink` marks `missing_ok`
  always-forward, since cloudpathlib's own default (`True`) diverges from
  canonical. Its missing `as_posix` needs no override — the generic
  synthesize-from-`str` branch covers it.
- **`anyio.Path`**: zero overrides; async-nativeness is detected per bound
  member by the bridge (§7), not declared by the adapter, so even this needs
  no registration.

Two rules keep the model honest. First, a named adapter overrides a *method*,
never copies the generic logic — so a fix to the generic chain reaches every
family. Second, duck-typing is documented as best-effort: an exotic object
whose `.path` attribute means something else will be mis-read, and the remedy
is registering a small adapter (`register_driver(cls, adapter)`), which is
exactly what the named families are. Adding a driver touches no wrapper
method. The registry is **module-level and shared by `Path` and `AsyncPath`**
— a driver's nature does not depend on which wrapper holds it — which also
removes the reference's clobbered-registry bug by removing the second
registry.

**Canonical decisions worth spelling out:**

- **`rmdir` [settled].** Default is non-recursive/empty-only — the 2-of-3
  consensus (pathlib, cloudpathlib), and the only *safe* default for a
  destructive operation; UPath's recursive-by-default is the outlier and a
  data-loss hazard we refuse to inherit. `rmdir(recursive=True)` is the
  explicit opt-in (maps to UPath `rmdir(recursive=True)`, cloudpathlib
  `rmtree()`, `shutil.rmtree` for local). The engine must pass
  `recursive=False` **explicitly** to UPath. The only current consumer
  already passes `recursive=True` explicitly at every recursive call site,
  so this costs nothing downstream.
- **`__fspath__` [settled, flagged].** Three-way disagreement with no
  consensus: pathlib returns the string, UPath refuses, cloudpathlib performs
  a network download. Decision: delegate for local drivers; for non-local
  drivers raise `NoLocalPathError` -- an `UnsupportedPathOperation` that
  is also a `TypeError`, so the usual `try: os.fspath(x) except TypeError`
  probe falls through (issue #23) -- with a message naming the
  alternatives (`str(p)` for the URL, `p.path` for the fsspec path,
  cloudpathlib's own `.fspath` for a cached local copy). Rationale: returning
  `"s3://…"` makes `open(os.fspath(p))` fail confusingly, and a silent
  network download inside an `os.fspath` call is a category error for a
  wrapper whose promise is consistency. This intentionally makes wrapped
  cloud paths unusable with APIs that call `os.fspath` — which they are.
  *(Owner may veto toward cloudpathlib-style download-on-demand; do it
  explicitly if so, never as an ambient side effect.)*
- **Equality/hash [settled; revised by issue #22].** The reference defines
  neither (its own TODO). `a == b` iff their canonical `(protocol, path)`
  locations match — **driver-independent**, because the drivers themselves
  disagree (`UPath(u) != AnyPath(u)`), and the whole point of the wrapper is
  that the driver is an implementation detail. **Front-end-independent**
  too: `PurePath`, `Path` and `AsyncPath` at one location are equal and hash
  alike (as `anyio.Path` equals `pathlib.Path`), so equality is transitive.
  (The first rule kept sync and async apart; it was dropped with #22.)
  - *Unwrapped operands.* `__eq__` reads the other operand as a path when it
    is a driver object (`pathlib`, `UPath`, `CloudPath`, `anyio.Path`: the
    `_args.is_driver` test, never its `__fspath__`, which downloads on
    cloudpathlib) or a plain `os.PathLike` (read with `os.fspath`, so a
    local name). A `str`/`bytes` is never a path — every library agrees —
    and any other type gives `NotImplemented`. The reflected direction
    works because `pathlib`'s and universal-pathlib's `__eq__` return
    `NotImplemented` for a foreign type; cloudpathlib's returns `False`, so
    `cloud == wrapper` stays `False` (only `wrapper == cloud` is true).
    Motivation: brainhops compared a returned wrapper with the
    `pathlib.Path` it passed in and silently got `False`.
  - *Local identity carries the flavour.* A local key is
    `("", path, PurePosixPath | PureWindowsPath)` — the flavour of a
    `pathlib` driver, else the platform's — because `pathlib` never equals a
    POSIX path to a Windows one, and the hash below is the flavour's.
  - *Hash.* A local path hashes as `hash(flavour(path))`, i.e. exactly as
    `pathlib` does on the running version (3.8–3.11 hash the case-folded
    parts tuple, 3.12+ the case-folded string; both computed by `pathlib`
    itself, never re-implemented). A remote path keeps
    `hash((canonical_scheme, path))`, which is universal-pathlib's own hash
    for a canonical scheme. Equal-hash cannot hold everywhere: cloudpathlib
    hashes `(class name, URL)`, and universal-pathlib hashes an alias as
    spelled (`UPath("s3a://…")`), so for those `x == y` but
    `x in {y}` may be false. Documented, not fixable from our side.
  - *Location only.* `storage_options` and other connection state do not
    count (`Path(UPath(u, anon=True)) == UPath(u, anon=False)`), unlike
    universal-pathlib, which compares options too.

  Ordering operators: not in v1 (pathlib orders only same-flavour paths; no
  consumer evidence). Windows case-insensitivity is explicitly not modeled
  in equality (we compare canonical strings): `Path(PureWindowsPath("C:/A"))`
  does not equal `PureWindowsPath("c:/a")`, though their hashes agree.
- **A wrapper as a `pathlib` 3.14 argument [settled; #30 row 7].**
  `pathlib` 3.14 takes any argument with `with_segments` for one of its own
  paths (`with_segments` is on our surface, so a wrapper qualifies) and
  then reads two private members: `relative_to` reads `path._tail`, and
  `copy`/`copy_into`/`move` call `target._copy_from(source, **kwargs)`.
  Both names are unchanged from 3.14.0 to the 3.15 development branch.
  The wrapper answers them from its public surface only, so it behaves as
  the `os.PathLike` it is: `_tail` is the parts after the anchor;
  `_copy_from` has `pathlib` copy to the local file name
  (`source.copy(source.with_segments(os.fspath(target)), **kwargs)`), so
  the copy is `pathlib`'s own. A remote target raises `NoLocalPathError`
  (a `TypeError`, as `pathlib` raises for an argument with no file name),
  and `move` then keeps its source; a source that is not a local
  `pathlib.Path` (a `pathlib_abc` caller) is refused rather than rebuilt
  in the wrong store. Hiding `with_segments` instead would drop a public
  member. `anyio.Path` has the same gap upstream.
- **Dependency-free lexical driver [settled].** With no backend installed, a
  remote URL is backed by a stdlib-only lexical driver (`_pure_driver.py`) that
  reproduces the `(protocol, path)` identity of a real backend so a path means
  the same location whether or not `universal-pathlib`/`cloudpathlib` is
  present. Two measured divergences were reconciled toward
  `universal-pathlib`, the package default:
  - *Bucket root.* `universal-pathlib` normalizes a bare bucket to a trailing
    slash (`s3://bucket` has path `bucket/`, name `""`, and its parent is the
    bucket root again), while `cloudpathlib` keeps `bucket` (name `bucket`).
    The lexical driver converges on the `universal-pathlib` spelling; a
    regression test pins it.
  - *`.` and `//` in a key.* The lexical driver delegates to `PurePosixPath`,
    which collapses a `.` segment and a doubled slash; `universal-pathlib`
    preserves both. An ordinary key contains neither, so identity is exact for
    real paths and differs only for these pathological spellings. Reproducing
    the raw form lexically is not possible without re-implementing each
    backend's `_strip_protocol`, so this is a documented residual divergence.
- **A URL's query is not part of its name [settled, owner].** Measured
  against universal-pathlib 0.3.10, cloudpathlib 0.26.0, fsspec/s3fs
  2026.9.0, gcsfs 2026.10.1 and adlfs 2026.8.0, by the key or request each
  backend's I/O actually addresses:

  | URL | fsspec I/O addresses | cloudpathlib | UPath `name` | **wrapper `name`** |
  |---|---|---|---|---|
  | `https://h/d/x.gz?t=1#f` | sends `/d/x.gz?t=1`, never the fragment | `HttpPath.name == 'x.gz'` | `'x.gz?t=1#f'` | `'x.gz'` |
  | `https://h/d/x.gz?s=a/b.zip` | sends the query whole | `'x.gz'` | `'b.zip'` | `'x.gz'` |
  | `s3://b/d/x.gz?versionId=v` | key `d/x.gz`, version `v` | key `d/x.gz?versionId=v` | `'x.gz?versionId=v'` | `'x.gz'` |
  | `s3://b/d/x?version=2` | key `d/x?version=2` | same key | same | `'x'` |
  | `s3://b/d/Invoice #12.pdf` | key as written | same | same | `'Invoice #12.pdf'` |
  | `gs://b/d/x.gz?generation=123` | key `d/x.gz`, generation 123 (version-aware) | key with the query | `'x.gz?generation=123'` | `'x.gz'` |
  | `gs://b/d/x.gz#123` | key `d/x.gz`, generation 123 (version-aware) | key `d/x.gz#123` | `'x.gz#123'` | `'x.gz#123'` |
  | `az://c/d/x.gz?versionid=v` | key `d/x.gz`, version `v` | (needs credentials) | `'x.gz?versionid=v'` | `'x.gz'` |
  | `memory://d/x#1?q=1`, `zip://`, ... | key as written | — | same | `'x#1'` |

  The backends answer per scheme, so a name faithful to each store would
  change with the store, and the owner's use case is working *across*
  stores: copying local to cloud, naming a local output after a cloud
  input. The rule is therefore one for every remote URL:

  - **An unencoded `?` starts the query.** That is RFC 3986's generic
    syntax, and `s3://` (which has no formal specification) is read the same
    way by smart_open. S3's own HTTP API agrees: the SDK sends a key's
    literal `?` as `%3F`, and `?versionId=` is a real query parameter
    there. A `?` in an object name is legal but rare -- Windows forbids it
    in file names, S3 lists it as needing special handling, bucket names
    cannot contain it -- and every report of one is a bug report.
  - **A `#` starts a fragment only in an `http(s)` URL**, where it is never
    sent. Everywhere else it is a character of the name: Windows allows it,
    names like `Invoice #12.pdf` are ordinary, and every store library reads
    it so. The cost is gcsfs's `#<generation>` shorthand, which stays part
    of the name; `?generation=` is the query spelling.

  The lexical members (`name`/`stem`/`suffix(es)`/`parts`/`parent(s)`,
  `match`/`full_match`, `relative_to`, `with_*`, `/`) leave the query (and
  an http fragment) out. The string, `path`, the identity key and every I/O
  call keep it, since a token or version id may be what reaches the object.
  A derived path drops it, as `urljoin` and cloudpathlib's `HttpPath` do: it
  names a different object. `without_query()` returns the whole path
  without it. Both delimiters are protocol traits (`query`, on for every
  scheme; `fragment`, on for `http`/`https`), so a store whose keys hold a
  literal `?` opts out with `register_protocol(scheme, query=False)`.

  The cost, accepted: in a store, two keys that differ only after a `?`
  (`d/x?v=1`, `d/x?v=2`) share a name, so `parent / child.name` does not
  reach the child. Where the wrapper itself builds a path from a name --
  `copy_into`/`move_into` within one store, the fallback `walk` -- it uses
  the store's own name, so it never merges two such objects (measured
  before the fix on `memory://`: three objects in, two out). Two gaps are
  left for a follow-up: no URL spelling reaches a key that does contain a
  `?` (no backend decodes `%3F`), and `?VersionId=` (boto3's capitalisation)
  is not the `?versionId=` s3fs understands, so it reads a missing key.

  The view is rebuilt through the driver's own `with_segments`, so its
  configuration (storage options, a client) rides along; a driver without
  one keeps its own answer, and `without_query()` raises for it.

  No path library has a query API (pathlib, UPath and cloudpathlib offer
  only `with_name`/`with_stem`/`with_suffix`/`with_segments`); the URL
  libraries do (yarl `with_query(None)`/`with_fragment(None)`/
  `without_query_params`, httpx `copy_with(query=None)`, furl
  `remove(query=...)`). Getters and setters for the query are not offered
  yet.

  **File URIs [settled].** A `file:` URI is a URL (RFC 8089): Python 3.14's
  `Path.from_uri` (`url2pathname(require_scheme=True)`, "Discard query and
  fragment") reads `file:///tmp/x?.nii` as `/tmp/x`, and `as_uri()` writes a
  `?` in a name as `%3F`; 3.13's `from_uri`, universal-pathlib and fsspec
  read it literally. The wrapper follows 3.14 on every version -- one
  implementation for `Path(url)`, `Path.from_uri` and `PurePath`, checked
  against 3.14's `from_uri` -- since delegating to the running pathlib made
  the answer version-dependent. That also refuses a non-local authority
  (`file://host/share/x`), which was silently read as `/share/x`.
  `local://` is fsspec's name for the local filesystem; as in any non-http
  URL its query is not part of the path and a `#` is, and the rest is read
  as fsspec reads it, without percent-decoding.

  Measuring this also showed the native async driver building its filesystem
  without fsspec's `_get_kwargs_from_urls`, so `AsyncPath("s3://...?versionId=v")`
  read the *latest* version where `Path` read `v`; it now merges those options
  under the caller's, as UPath does.
- **Staged writes [settled, owner] (issue #19).** `path.staged()` yields a
  path *inside a private staging directory*, named exactly like the target
  and not yet created; whatever the caller creates there -- a file or a
  directory -- is moved onto the target on commit. The name is the target's,
  so a library that saves by name and reads the extension (nibabel) sees no
  difference, and one that refuses an existing path (zarr) is happy too.
  Measured before deciding (CPython 3.13, fsspec 2026.9.0, s3fs 2026.9.0,
  cloudpathlib 0.26.0):

  - `tempfile.mkstemp` creates `0600`; a naive "temp file + replace" turned a
    `0644` target into `0600`. The staged entry is created by the caller with
    the normal umask, and a replaced file gets the old one's mode.
  - fsspec file objects commit on `close()` even when the `with` block raised
    (`memory://` kept `b"half"`); cloudpathlib uploaded the partial content
    too. A streamed remote write is never safe; committing uploads a finished
    local file instead.
  - fsspec transactions are not a foundation: they only hold back writes that
    go through `fs.open` (s3fs's own `pipe_file`/`put_file`, used by zarr and
    by uploads, ignore them); they live on the cached filesystem instance, so
    another caller's write to the same store was swallowed into ours and would
    have been discarded with it; and the local transaction writes in the
    system temp directory and `shutil.move`s it, a copy across file systems.
  - An in-place rewrite under an open memory map killed the reader with
    SIGBUS; after `os.replace` the map kept reading the old content, and an
    open reader kept the old bytes. Replacing is what keeps readers safe.

  Commit: nothing staged leaves the target as it was (silently); a file over
  a file is one `os.replace` after `fsync`, then the directory is fsynced
  (atomic on POSIX); a directory or a change of type moves the old entry
  aside *into* the staging directory (same file system) and the new one in,
  moving the old one back if that fails (all-or-nothing, not atomic). The
  staging directory is hidden beside the target, resolved through symbolic
  links; a remote target is staged in a local temporary directory and
  uploaded: a file with the driver's own upload (universal-pathlib's
  `fs.put_file`, cloudpathlib's `upload_from`; s3fs/gcsfs/adlfs create the
  object only when the upload completes), a directory file by file, then
  stale objects are deleted (per-file atomic only). A failed commit leaves
  the target as it was (or restored) and keeps the staged data, past garbage
  collection, in `CommitError(OSError)`; a dropped stage is cleaned by a
  finalizer. `copy=True` starts the staged entry as a copy (a download, for
  a remote target) for read-modify-write jobs. `replace_type=False` refuses
  a change between file and directory (`CommitError` with `EISDIR` or
  `ENOTDIR`); the check sits in the commit, just before the old entry is
  touched, since a directory can appear after any earlier check.

  `atomic=` on `write_bytes`, `write_text` and `open` is built on it (`open`
  needs a writing mode; `a`/`r+` start from a copy), and is **the plain
  write, made atomic**: wherever the staging area would otherwise show, it
  acts as `pathlib` does. It refuses a directory at the target
  (`IsADirectoryError`, before staging and again at commit), never
  replacing it (#45). A failure, a failed commit included, removes the
  staging directory and raises the error itself, not `CommitError` (#46):
  the caller still holds its data and has no stage to commit again. An
  `OSError` that names an entry of the staging directory is re-raised
  naming the target (#47): the caller never saw that directory, and it is
  gone when the error is read. `staged()` keeps its defaults: a change of
  type is a new version; the data is kept, since its caller may have built
  something costly (a caller that wants it dropped keeps the `Stage` and
  `discard()`s it on `CommitError`); and an error raised while the caller
  writes at the staged path names that path, which is the one it wrote.
  Making the staging directory is the exception: the caller has not seen
  it yet, so a failure there names the target on every stage.

  Not adopted: driver transactions (above); one transaction spanning several
  unrelated files, which cannot be all-or-nothing once the first commit has
  happened; a symlink-swap for atomic directory replacement, which turns the
  target into a link. Prior art the shape follows: Hadoop/Spark output
  committers (write under `_temporary/` beside the output, rename at commit;
  the S3A *staging committer* stages on local disk and uploads at commit),
  the staging area of the version-control tool (the name), Go's `renameio`
  (temp file in the same directory, `CloseAtomicallyReplace`/`Cleanup`),
  Rust's `tempfile::NamedTempFile::persist`, and fsspec's
  `commit`/`discard` vocabulary.
- **Local copies for by-name libraries [settled, owner] (#20).** OpenSlide,
  h5py, nibabel memory maps and netCDF open a file by name only; refusing
  `__fspath__` on a remote path (above) sends them here instead. Measured
  prior art: fsspec's `open_local()` needs a caching chain
  (`simplecache::...`), refuses a plain `memory://` URL, gives the copy a
  *hashed name with no extension* (useless to a library that picks its format
  from the extension), never cleans up, and a glob (`simplecache::.../slide*`)
  fetched a MIRAX `.mrxs` but missed its tile directory. cloudpathlib's
  `.fspath` keeps the name, but its cache lifetime is a global client
  setting, not a scope.

  `local_copy()` downloads a remote file or tree under its exact name; a local
  path is used as it is. With `with`, the copy goes when *its own* block ends.
  Taken by hand it stays until its `cleanup()` (or its stage closes) and is
  **never** removed by garbage collection: a caller may hand the name to a
  library and drop the handle while the file is still in use, so a GC
  finalizer would be a use-after-free. Companion files (`.hdr`/`.img`,
  `.mhd`/`.raw`, `.mrxs` + its directory) are not fetched on demand -- no
  wrapper can intercept a library's own opens -- so the caller fetches them
  into one `local_stage()` and holds them at the same time. A stage lays its
  copies out as `<scheme>/<bucket-or-host>/<key>`, so copies from one store
  keep their relative positions whatever the order they are fetched in, and
  ref-counts them: a path held twice is downloaded once and removed with its
  last holder; the stage removes leftovers when it closes. Explicit
  (`local_copy(stage=...)`) and implicit (the innermost active stage) both
  work. The active stage is a `ContextVar`, so each thread and asyncio task
  sees its own; executor threads do not inherit context variables, so the
  async bridge now runs every worker call in `contextvars.copy_context()`
  (as `asyncio.to_thread` does), or an `AsyncPath.local_copy()` over a
  synchronous driver would miss the stage. Two versions of one key
  (`x?versionId=1`, `x?versionId=2`) would share a slot, so the second is
  refused in that stage. Downloads are our own, not cloudpathlib's cache, so
  lifetimes are the same on every driver; a download lands under a hidden
  `.partial-` name first, so a failed one never leaves a half file a later
  fetch would reuse.
- **Bucket-relative accessor [owner].** The tensorstore evidence (§2) wants
  bucket + path-within-bucket. Candidate: a `key` property (`path` minus
  `drive`; equals `path` when `drive` is empty), name borrowed from S3/
  cloudpathlib. It is one property, but it is also a *name commitment* on the
  most stability-sensitive tier, and cloudpathlib itself couldn't settle on
  one name (`key` vs `blob`). Ship in v1 or let downstream compute it?
- **Residual, documented divergence.** Values that pass *through* the wrapper
  uncanonicalized: `stat()` results (fsspec and cloud stats differ in fields),
  `owner`/`group` (state 3 on cloud), `open()` file-object flavor (§7). These
  are documented per-member rather than papered over.

---

## 6. Construction, derivation, and the extensibility story **[settled: no protocol-subclass axis]**

The reference's construction path is its most fragile part: `__new__` builds a
throwaway instance (running `__init__` twice), dispatches on `.protocol` to a
subclass registry that is **shared mutable state across the sync tree, the
async tree, and every downstream subclass** — after importing
`abczarr.abc.path`, plain `WrappedPath("s3://…")` dispatches into *StorePath*
classes. The 12-class downstream boilerplate exists only to keep that dispatch
from dropping the subclass.

The review question was: mixins composed on demand, or an auto-generated
cross-product? **Answer: neither — delete the axis.** Audit what the protocol
subclasses actually contain: a `bucket` property aliasing `drive`
(`BucketMixin`) and a `VALID_PROTOCOLS` check. Every StorePath protocol
subclass is empty (`...`). Twelve hand-written classes and the whole dispatch
machinery deliver **one property and one validation** — both of which are
*data about a protocol*, not behavior demanding a type.

Redesign:

1. **Protocol-varying behavior is data.** A `ProtocolTraits` registry
   (`_protocols.py`) keyed by scheme: is it bucketed (drives `drive`/`bucket`
   canonicalization), how does `as_uri` render, is it always-absolute, which
   schemes alias it (`s3`/`s3a`, `gs`/`gcs`). The adapters and the fallback
   driver consult it. Downstream adds a protocol with one call —
   `register_protocol("lakefs", bucketed=True, …)` — no classes. The `bucket`
   property lives on `Path` itself and reads the traits (raising
   `UnsupportedPathOperation` for non-bucketed protocols).
2. **Construction is boring.** `Path(obj_or_str, *, driver=None)` parses the
   protocol from a string input, picks the driver class (explicit `driver=` >
   registry preference per protocol > availability order UPath → cloudpathlib
   → fallback driver), wraps, done. No `__new__` tricks, no dispatch, no
   double-init. `type(Path("s3://…")) is Path`, and
   `type(StorePath("s3://…")) is StorePath` — subclassing just works.
3. **One derivation hook.** Every place the wrapper produces a *new* wrapper —
   `parent`, `joinpath`, `/`, glob/iterdir results, `copy` targets — goes
   through a single overridable method, `with_wrapped(driver_obj)` (default:
   `type(self)(driver_obj)`). This is `pathlib`'s own extension pattern
   (`with_segments`: "Subclasses may override this method to pass information
   to derivative paths"), adapted to wrapping. A downstream subclass carrying
   state overrides it **once**:

   the entire `StorePath` port becomes one class: an `__init__` accepting
   `read_only` and a `with_wrapped` that forwards it. Twelve classes become
   zero, and it *fixes a live reference bug* — today `StorePath.parent`
   silently drops `read_only`.
4. **If a downstream truly needs `isinstance`-by-protocol**, it can branch on
   `self.protocol` or maintain its own mapping. Class-level protocol dispatch
   is out of scope for v1 and is recorded as a non-goal; adding it later
   (e.g. as opt-in mixin composition) is backward-compatible, so we do not
   pay its complexity on speculation.
5. **A driver of one's own must bring its lexical members [settled, owner]
   (#21).** The wrapper delegates `name`, `parent`, `with_suffix`, `/`, ...
   to the driver and does not compute them from the driver's string. A
   value (`name`, `suffix`) could be parsed safely, but a *path* result must
   be a driver object of the same kind with the same configuration, and the
   string does not carry it: measured, rebuilding from the string dropped a
   universal-pathlib path's `storage_options` and gave a cloudpathlib path a
   different, default client. Every supported driver implements the lexical
   members itself (pathlib -- including 3.8, without `with_segments` --
   universal-pathlib, cloudpathlib, `anyio.Path`, and both of ours), so the
   case is only a minimal third-party driver or a test double. Those are
   documented to subclass a path class that has the members, or to provide
   them and `with_segments`; a test double is better replaced by
   universal-pathlib's `memory://`.

---

## 7. The async engine

The reference's `ensure_coroutine` is built on
`loop.run_in_executor(None, func, *args, **kwargs)` — but `run_in_executor`
**does not accept kwargs**, so every threaded call that passes one raises
`TypeError` (§11 bug 7); it also uses soft-deprecated bare
`asyncio.get_event_loop()`, assumes async-native drivers for iteration, and
has no async→sync direction.

**Recommendation: stdlib-only, no anyio dependency — not even as an extra.**
The owner asked for this assessed on the merits rather than assumed; here is
the ledger.

What the bridge actually needs, direction by direction:

- **Async-over-sync** (`AsyncPath` over a sync driver) — the direction the
  real consumer uses. Stdlib is fully sufficient on 3.8+:
  `loop = asyncio.get_running_loop(); await loop.run_in_executor(None,
  functools.partial(fn, *args, **kwargs))` (`asyncio.to_thread` itself is
  3.9+, so `run_in_executor` + `partial` is the spelling). That fixes the
  reference's kwargs bug and its deprecated `get_event_loop()` in one line.
  What would anyio add here? `to_thread.run_sync` has the same fundamental
  limits (a running sync call cannot be cancelled by anyone), so no
  correctness gain — only a capacity limiter we don't need and trio support
  (below).
- **Sync-over-async** (`Path` over a natively-async driver) — the genuinely
  hard direction, because `asyncio.run` cannot be nested inside a running
  loop. The stdlib design is a **loop portal**: one lazily-started,
  process-wide daemon thread running an event loop, with sync callers using
  `asyncio.run_coroutine_threadsafe(coro, portal_loop).result()`. This is the
  battle-tested fsspec `sync()` pattern, ~40 lines including the mandatory
  guard: if the *caller's* thread already has a running loop, raise a clear
  error ("you are inside an event loop; use AsyncPath") instead of
  deadlocking. anyio ships this ready-made as
  `anyio.from_thread.start_blocking_portal()` — this is anyio's one genuine
  offer. It is a convenience, not a capability: the portal semantics,
  deadlock hazard, and lifetime management are identical either way, and 40
  auditable lines beat a dependency whose current major needs Python ≥3.10
  against our 3.8 floor (supporting 3.8 would mean pinning anyio 3.x on old
  interpreters — a version matrix we'd own forever).
- **Natively-async drivers do not require depending on anyio.** If the *user*
  installs anyio and hands us an `anyio.Path`, its members are ordinary
  coroutine functions; the bridge detects and awaits them. Driver support and
  dependency are separate questions.

What we give up with stdlib-only is exactly one thing: **`AsyncPath` under
trio**. There is zero downstream evidence of trio use; under trio,
`get_running_loop()` raises a clean `RuntimeError` (we wrap it in a clear
message), so the failure mode is honest, not corrupt. If a trio user ever
materializes, an anyio-backed bridge can be added behind the same internal
seam as a purely additive change. Verdict: dependency-free core is a stated
house value with a concrete payoff (the wrapper works everywhere abczarr
does); trio support is speculative. Stdlib-only wins. **[owner]** retains a
veto, but the recommendation is firm.

Bridge behavior:

- **Direction and granularity.** Per bound member,
  `inspect.iscoroutinefunction` / `isasyncgenfunction` decides: natively
  async → await directly; sync → run the **whole engine call** (delegation
  plus any fallback composition) in **one** worker thread, so a fallback like
  read-via-open costs one hop, not one per primitive.
- **Iteration** must handle both shapes: a sync-generator driver bridged into
  an async generator (next-item-per-thread-hop; the reference handles only
  natively-async iterators, which none of the current drivers even are), and
  a natively-async generator passed through.
- **`open()` on `AsyncPath`** (23 downstream uses — this gap matters):
  a natively-async driver's file object (e.g. `anyio.Path.open`) passes
  through; a sync driver's file object is wrapped in a small async file
  adapter (async context manager + async `read`/`write`/`readline`/iteration/
  `close`, each a thread hop). The adapter is internal; its type is not part
  of the public surface, only its protocol is documented.
- **Sync-over-async ships as the stdlib loop portal above, sequenced after
  the core** (§1): it is additive, so v1.0 raises `UnsupportedPathOperation`
  at *construction* time when `Path` receives a natively-async driver, with a
  message pointing to `AsyncPath`, and the portal lands in v1.x.
  **[owner]**: confirm that sequencing (or pull the portal into v1.0 — the
  design is fixed either way).

---

## 8. Proposed public surface

```
bagof.paths
    Path                      # the sync wrapper
    AsyncPath                 # the async wrapper
    UnsupportedPathOperation  # the state-3 exception
    NoDriverError             # construction: no driver builds this scheme
    NoLocalPathError          # os.fspath on a remote path (also a TypeError)
    ProtocolTraits            # a scheme's traits (introspection)
    register_driver           # adapter registration (advanced, public)
    register_protocol         # protocol traits registration (advanced, public)
```

Driver selection is implemented (see §6.2). `Path("s3://bucket/key")` picks a
backend from the scheme: an explicit `driver=` (a path class or `str -> path`
callable) wins, then a protocol's registered preference, then the availability
order **universal-pathlib → cloudpathlib**. universal-pathlib is the automatic
default — it builds any fsspec URL lazily (no cloud SDK needed to construct)
and covers the widest scheme set; cloudpathlib is the fallback when it is
absent, selected as its *concrete* implementation class (never `AnyPath`,
whose answer for an unrecognised scheme is a silent local path). A scheme no
installed backend can build raises `NoDriverError` (a `ValueError`) — never a
silent local path. `register_protocol(scheme, *, bucketed=…, aliases=…,
driver=…)` carries a scheme's traits and its optional preferred driver in one
call; `ProtocolTraits` takes keyword arguments only so new traits stay
backward-compatible. Register protocols at import time — traits participate in
a path's canonical identity (scheme aliases fold, `s3`≡`s3a`).

Settled naming calls, with reasons:

- **`Path` [settled].** The prior art is unanimous among wrapper-flavored
  path packages: `anyio.Path`, `trio.Path`, `upath.UPath`-as-`Path` in
  downstream aliases. Qualified imports (`from bagof.paths import Path as
  BPath`, or `import bagof.paths as bp`) are the normal resolution, exactly
  as with `anyio.Path` vs `pathlib.Path`. No `WrappedPath` alias: two names
  for the flagship class is an API-stability cost with no story for which one
  examples use.
- **No `wrap()`/`async_wrap()` [settled].** `Path(obj)` already *is* the
  factory; a second spelling of the same construction is surface without
  power. `Path.from_uri` stays (pathlib 3.13 parity).
- **No `PathLike` re-export [settled].** `os.PathLike` is one import away;
  re-exporting stdlib names invites `bagof.paths.PathLike is os.PathLike`
  confusion for zero savings.
- **No per-protocol classes** (§6), so nothing to name.
- `supports`/`capabilities` are methods on both wrapper classes, not module
  functions.

Everything else — spec, engine, adapters, bridge, fallback driver — is
private and free to move.

---

## 9. Module layout (house style: private modules, `__init__` re-exports only)

```
src/bagof/paths/
    __init__.py       # re-exports + __all__, no code
    _path.py          # Path: thin methods, the full docstrings
    _async_path.py    # AsyncPath: thin methods, one-line cross-ref docstrings
    _base.py          # shared: wrapped, adapter cache, protocol/path/drive,
                      #   __eq__/__hash__/__repr__, with_wrapped, supports()
    _spec.py          # the Member table: the pathlib surface, once
    _engine.py        # delegate/fallback/raise + kwargs/result policy (sync)
    _bridge.py        # sync↔thread hop, native-async detection, iterator and
                      #   file-object adaptation
    _fallbacks.py     # synthesis functions, sync + the small async mirrors
    _drivers.py       # GenericAdapter + named subclasses + register_driver
    _protocols.py     # protocol parsing, ProtocolTraits + register_protocol
    _fallback_driver.py  # the dependency-free protocol-aware driver
    _match.py         # glob.translate port (PSF; NOTICE.md entry required)
    _errors.py        # UnsupportedPathOperation, message construction
    _constants.py     # sentinels, scheme alias tables
tests/
    test_path_sync.py
    test_path_async.py
    test_parity.py            # sync/async surface lockstep (see §3)
    test_fallbacks.py         # each fallback vs a stub driver lacking the member
    test_drivers.py           # adapter canonicalization: measured table of §5
    test_consistency.py       # same call, same result, every driver (parametrized)
    test_capabilities.py
    test_reference_bugs.py    # §11 regression pins
    test_licensing.py         # PSF files reach the wheel (per bagof-magic)
    test_import.py
```

Testing the driver matrix without cloud credentials: cloudpathlib ships
local-filesystem test doubles (`cloudpathlib.local`) built for exactly this;
UPath covers `memory://` via fsspec; the fallback driver and stub drivers
cover the degraded rows. `test_consistency.py` runs the *same* parametrized
operations across pathlib / UPath-memory / cloudpathlib-local / fallback and
asserts identical canonical results — this test *is* priority #2 rendered
executable.

---

## 10. Packaging, dependencies, conventions

- Package `bagof.paths` (namespace), from the `bagof-things` template. Rename
  `things`→`paths` in `pyproject.toml` (`[project]`, URLs,
  `versioningit.write` → `src/bagof/paths/_version.py`), README, docs.
- **No hard third-party dep beyond `typing_extensions`, and no anyio at all**
  (§7). Wrapping a stdlib `pathlib.Path` works with `upath`/`cloudpathlib`
  absent; the fallback driver covers protocol-aware pure-path ops; `AsyncPath`
  runs on stdlib asyncio. An `anyio.Path` handed to `AsyncPath` works because
  its members are coroutine functions — that requires the *user* to have
  anyio, not us.
- Extras: `upath = ["universal-pathlib"]`, `cloud = ["cloudpathlib"]`,
  `all = [...]`, plus `test`/`docs` as in siblings. **Note:** current
  universal-pathlib and cloudpathlib require Python ≥3.9, so the 3.8 CI leg
  runs core-only (fallback driver + stdlib pathlib) — which is also exactly
  the leg that proves the no-deps story. Full-matrix legs run 3.9+.
- Conventions carried from `bagof-magic/CLAUDE.md`: `from __future__ import
  annotations` everywhere; all typing via `import typing_extensions as tx`;
  no runtime PEP 604/585; ruff line-length 79, target py38; codespell;
  `py.typed` (real, since §3 keeps real signatures — no `.pyi` needed);
  every module private, `__init__.py` holds no code; internals tested by
  importing the defining module.
- **PSF port obligations:** `_match.py` (and anything else ported from
  CPython) goes into `NOTICE.md`'s component table and summary of changes;
  `LICENSE-PSF-2.0.txt` at repo root, listed in `license-files`;
  `tests/test_licensing.py` enforces, as in bagof-magic.
- Add a `bagof-paths/CLAUDE.md` recording these decisions once implementation
  starts.

---

## 11. Bugs in the reference to NOT port (fix on the way in)

From the deep read of `path.py` / `asyncutils.py`, each verified against the
source:

1. `__hash__`: `hasattr(self.wrapped, ".__hash__")` — leading dot, always
   False.
2. `__fspath__`: `getattr(self.wrapped, "__fspath__", "__str__")()` — calls
   the *string* `"__str__"` when absent → `TypeError`.
3. `FallbackPath.__init__`: `protocol[::-3]` where `protocol[:-3]` was meant
   (every-third-character-reversed instead of strip-`://`).
4. `register_driver`'s decorator-factory branch calls `register_subclass` —
   drivers registered through the decorator land in the wrong table.
5. `chmod()` takes no `mode` argument — the one thing chmod is for.
6. Async `_copy_fallback` tests `if self.is_dir():` on an un-awaited
   coroutine — always truthy, so every copy takes the directory branch.
7. `run_in_executor(None, func, *args, **kwargs)` — `run_in_executor` cannot
   take kwargs; every threaded call passing one raises `TypeError`.
8. `anchor`: `getattr(self, "anchor", …)` — the property calls *itself*;
   `getattr`'s default only rescues `AttributeError`, so this is infinite
   recursion (`RecursionError`), not a working default.
9. Shared `_DRIVER_REGISTRY`/`_SUBCLASS_REGISTRY` class vars on the common
   base: the sync tree, async tree, **and downstream StorePath trees** all
   write the same dicts — importing `abczarr.abc.path` changes what
   `WrappedPath("s3://…")` dispatches to globally (§6).
10. `__new__` double-init: `__init__` runs on a throwaway instance and again
    on the dispatched instance.
11. `parent` and `parents` return the **unwrapped driver object** (`return
    self.wrapped.parent`) — the #12 most-used member downstream escapes the
    wrapper entirely.
12. `walk()` yields `type(self)(p)` where `p` is a `(dirpath, dirnames,
    filenames)` tuple — constructs a wrapper from a tuple; broken for every
    driver.
13. `rmdir` delegates `self.wrapped.rmdir(recursive)` positionally:
    `TypeError` on cloudpathlib (takes no argument), and combined with
    `recursive: bool = True` it inherits UPath's delete-non-empty-by-default
    hazard (§5).
14. Derivation via bare `type(self)(p)` drops subclass state — `StorePath`'s
    `read_only` is silently lost on `parent`, `/`, glob results (§6).
15. `__truediv__`/`__rtruediv__` are annotated `-> str` while returning
    wrappers (typing bug that `py.typed` would ship).

`tests/test_reference_bugs.py` pins each with a regression test so the port
cannot silently reintroduce them.

---

## 12. Proposed phases (each independently reviewable / shippable)

1. **Skeleton + packaging.** Rename template to `bagof.paths`, wire
   pyproject/docs/CI (incl. the 3.8 core-only leg), `py.typed`, empty
   modules, `test_import`. (No behavior.)
2. **Pure-path core over one driver.** `_base` + `_spec` + engine skeleton +
   the pure-path surface (`parts`, `name`, `parent`, `joinpath`, `/`,
   `with_*`, `as_uri`) over stdlib `pathlib` only, **including
   `__eq__`/`__hash__`/`__repr__`, `with_wrapped`, and the `_match.py` port**
   — the identity/derivation decisions are the hardest to change later, so
   they go first.
3. **Concrete sync surface + fallbacks.** `exists`/`open`/`stat`/`iterdir`/
   `walk`/`mkdir`/`unlink`/`rmdir`/read-write/copy-move, kwargs
   normalization, fallback synthesis, `supports()`/`capabilities()`,
   `test_reference_bugs.py`.
4. **Driver adapters + protocol traits.** UPath/cloudpathlib/fallback-driver
   adapters, `register_driver`/`register_protocol`, the §5 canonicalization
   table as `test_drivers.py`, and `test_consistency.py` across the matrix.
5. **Async wrapper.** `AsyncPath` + `_bridge` (thread hop, native-async
   detection, iterator + file adaptation), `test_parity.py`, plus a
   natively-async-driver test leg (`anyio.Path`, user-installed for the
   test only — no package dependency).
6. **Surface parity.** Everything `pathlib`/`UPath`/`cloudpathlib` implement
   that phases 2–5 did not yet expose: the extended status queries
   (`is_mount`/`is_socket`/`is_fifo`/`is_block_device`/`is_char_device`/
   `is_junction`/`is_reserved`), permissions and ownership
   (`chmod`/`lchmod`/`owner`/`group`), links
   (`symlink_to`/`hardlink_to`/`link_to`), `with_segments`, the
   local-filesystem constructors (`home`/`cwd`/`from_uri`), the recursive
   copy/remove aliases (`copytree`/`rmtree`), the driver-specific accessors
   (`info`/`storage_options`/`fs`/`bucket`/`key`/`client`/`cloud_prefix`/
   `fspath`/`etag`), and the cloud transfer/cache members
   (`as_url`/`download_to`/`upload_from`/`clear_cache`/`joinuri`). Each is
   delegated, synthesized, or delegate-or-raise on the same spec/engine path
   as the rest, with the async surface kept in lockstep. Anything left out is
   still reachable through `path.wrapped`.
7. **Driver selection.** `Path("s3://…")` builds a backend from the scheme
   (§6.2): explicit `driver=` > a protocol's registered preference >
   universal-pathlib > cloudpathlib's concrete class > `NoDriverError`. The
   public `register_protocol`/`ProtocolTraits` registry (bucketed, aliases,
   absolute, preferred driver); scheme aliases fold into the canonical
   identity; the scheme is lower-cased and fsspec chains
   (`simplecache::s3://…`) route to selection rather than becoming a local
   path. The dependency-free fallback driver and a `to()` cross-driver
   converter stay deferred (the availability tail is an error branch the
   fallback later appends to — non-breaking).
8. **Docs + polish.** mkdocstrings pages, `pycon`-tested examples on the
   oldest supported interpreter, a comparison page vs raw
   `pathlib`/`UPath`/`AnyPath`, `CLAUDE.md`.

The §3/§5/§6 decisions above are settled, so phases 1–3 can start
immediately; §13's remaining owner calls gate only parts of phases 4–5.

---

## 13. Decisions

Settled in review (reasoning in the sections cited):

1. **Architecture (§3):** thin real methods on both wrappers over a single
   shared sync policy engine + member spec; async bridges at the engine-call
   boundary; parity locked by test, not discipline. Not table-driven
   dispatch (kills typing), not codegen (cost without churn), not an
   async-native core (taxes the common case).
2. **Extensibility (§6):** no protocol-subclass axis at all. Protocol
   behavior is registry data (`ProtocolTraits`); construction is plain;
   derivation goes through one overridable `with_wrapped` hook (pathlib's
   `with_segments` pattern). `StorePath` ports as one class and one
   override, and stops losing `read_only` on derived paths.
3. **Driver reconciliation (§5):** canonical shapes chosen per the measured
   table — UPath-shaped `protocol`/`path`/`parts`/`anchor` (preserves
   pathlib's algebra), pathlib-consensus `rmdir` (non-recursive default;
   `recursive=True` opt-in) and `unlink` (`missing_ok=False`), wrapper-level
   driver-independent equality, `__fspath__` raises for non-local drivers.
   Adapter structure: one duck-typed `GenericAdapter` that is both base class
   and unregistered default, with named adapters overriding only measured
   divergences (pathlib 0, UPath 1, cloudpathlib 3 overrides).
4. **Async engine (§7):** stdlib-only — no anyio, not even as an extra; the
   only capability lost is `AsyncPath` under trio, which no consumer needs
   and which can be added behind the same seam later. Async-over-sync via
   `run_in_executor` + `partial` (3.8-safe); per-member direction detection;
   whole-call thread granularity; sync-over-async via a ~40-line stdlib loop
   portal (fsspec pattern), sequenced to v1.x with a clean construction-time
   error in v1.0.
5. **Errors/capabilities (§4):** one `UnsupportedPathOperation`, based on
   `pathlib.UnsupportedOperation` where available else
   `NotImplementedError`; public `supports()`/`capabilities()` that account
   for fallbacks and are documented as static.
6. **Names (§8):** `Path` / `AsyncPath` (anyio/trio precedent), no
   `wrap()`/aliases/`PathLike` re-export; return `None` from
   `touch`/`mkdir` like all three neighbours (no fluent-`self` divergence).

Remaining **[owner]** decisions:

1. **`__fspath__` veto (§5):** the settled answer (raise for non-local) is a
   judgement call without prior-art consensus; the alternative is
   cloudpathlib-style download-on-demand. Confirm or veto.
2. **Bucket-relative accessor (§5):** ship a `key` property in v1 (tensorstore
   evidence says useful; naming is a permanent commitment), or let downstream
   compute `path`-minus-`drive`?
3. **Sync-over-async timing (§7):** the loop-portal design is fixed and the
   recommendation is v1.x; confirm that, or pull it into v1.0, or defer
   indefinitely until asked.
4. **anyio veto (§7):** the firm recommendation is stdlib-only with trio
   unsupported; overrule only if trio support is wanted on day one.
5. **Driver preference policy (§6.2):** when both UPath and cloudpathlib are
   installed and the input is a string URL, who wins per protocol? Proposed
   default: UPath for everything it supports (broader scheme coverage, fsspec
   alignment with `.path`/`.protocol`), overridable per protocol via
   `register_protocol(..., prefer="cloudpathlib")`. The reference's own TODO
   wanted per-scheme choice; this is where it lives.
