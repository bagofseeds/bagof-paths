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
  drivers raise `UnsupportedPathOperation` with a message naming the
  alternatives (`str(p)` for the URL, `p.path` for the fsspec path,
  cloudpathlib's own `.fspath` for a cached local copy). Rationale: returning
  `"s3://…"` makes `open(os.fspath(p))` fail confusingly, and a silent
  network download inside an `os.fspath` call is a category error for a
  wrapper whose promise is consistency. This intentionally makes wrapped
  cloud paths unusable with APIs that call `os.fspath` — which they are.
  *(Owner may veto toward cloudpathlib-style download-on-demand; do it
  explicitly if so, never as an ambient side effect.)*
- **Equality/hash [settled].** The reference defines neither (its own TODO).
  Wrapper equality: `a == b` iff both are wrappers of the same *front-end
  family* (sync/async compare equal to each other? No — same family only) and
  their canonical `(protocol, str)` match — **driver-independent**, because
  the drivers themselves disagree (`UPath(u) != AnyPath(u)`), and the whole
  point of the wrapper is that the driver is an implementation detail.
  `hash` follows. Ordering operators: not in v1 (pathlib orders only
  same-flavour paths; no consumer evidence). Windows case-insensitivity is
  explicitly not modeled (we compare canonical strings).
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
