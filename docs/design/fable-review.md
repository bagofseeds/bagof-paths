# Review memo — `bagof.paths` path-wrapper design

Reviewer: Fable. Inputs: the draft plan, the full reference
(`abczarr/_core/path.py`, 2375 lines, + `asyncutils.py`), the downstream
consumer (`abczarr/abc/path.py`, `abczarr/drivers/tensorstore.py`), the
bagof-magic conventions, and **live probes** of universal-pathlib 0.3.10 and
cloudpathlib 0.25.0 on CPython 3.11 (house rule: settle judgement calls by
running the prior art). Everything below labeled "measured" was executed, not
recalled.

The draft was a strong plan: the delegate/fallback/raise triad, the kwargs
"forward only when non-default" idea, the spec-table instinct, and the bug
list were all sound and survive. What I changed is mostly *deciding* the
things it left open — and reversing it on one structural point (§6), where I
think the draft was solving the wrong problem well.

---

## 1. The six §13 questions, answered

### Q1 — Architecture: A (table dispatch), B (codegen), C (thin methods), or a fourth?

**C, decided, and I sharpened it into "thin methods over a single sync policy
engine".** Reasoning:

- A is disqualified outright for a `py.typed` package. A `__getattr__` surface
  types as `Any`; for a library whose entire pitch is "reads like pathlib",
  losing completion and checking on `exists`/`open`/`iterdir` forfeits the
  main asset. No amount of internal elegance buys that back.
- B (codegen) is the interesting rival, and the draft was right to keep it as
  fallback rather than pick it. My reason to reject it is different from the
  draft's: bagof-magic pays for compiled methods because `exec` is the *only*
  way to make a real `__init__` signature. Nothing here needs `exec` — every
  method's signature is known statically. Codegen's payoff scales with surface
  churn, and `pathlib` grows ~2 members per Python release. You'd be buying a
  generator, a check-it-in-or-build-it decision, and a `.pyi` discipline to
  save yourself sixty one-line bodies.
- The fourth option the draft missed (asked explicitly): **one native core,
  the other flavor as facade.** An *async-native* core is wrong — it routes
  every local `p.exists()` through an event loop and drags the nested-loop
  problem into the default path. But the *sync-native* half of the idea is
  right and I folded it in: all policy (delegate/fallback/normalize/rewrap)
  is written once, in sync code, in `_engine.py`; `Path` methods are one line
  into it; `AsyncPath` methods bridge *at the engine-call boundary* — the
  whole call (fallback composition included) runs in one worker thread for
  sync drivers. This kills the drift problem at its root: there is no async
  copy of the policy to drift.
- The two residual duplications (thin signatures ×2; ~8 async fallback
  mirrors for natively-async drivers with surface gaps) are pinned by an
  **automated parity test** — introspect both classes, assert same members,
  same signatures modulo `async`, every spec row present on both. The
  reference's sync/async drift becomes a CI failure. The draft proposed
  "a shared spec both layers consult" but never said how drift would be
  *caught*; the parity test is the missing enforcement.
- One trap I fixed before it was stepped in: the natural "attach shared
  docstrings at import time" move is invisible to mkdocstrings, because
  griffe reads statically. Full docstrings live on `Path`; `AsyncPath` gets
  one-line cross-references. Decide this now or discover it in phase 6.

### Q2 — Extensibility: how does `StorePath` inherit protocol dispatch without 12 classes?

**It doesn't — because protocol dispatch should not exist.** This is my one
outright reversal of the draft, which asked "mixin composition or generated
cross-product?" Both answers accept the premise that wrapper × protocol is a
real axis. Audit what the reference's protocol subclasses actually contain:
`BucketMixin.bucket` (an alias for `drive`) and a `VALID_PROTOCOLS` check.
That's the entire payload. Twelve hand-written classes per consumer, a
double-`__init__` `__new__`, and a shared-registry bug — all to deliver **one
property and one validation**, both of which are *data about a protocol*, not
behavior demanding a type.

So: delete the axis. Protocol facts live in a `ProtocolTraits` registry
(bucketed-ness, scheme aliases, URI rendering); `bucket` is a plain property
on `Path` reading the traits; construction is a boring `__init__` with no
dispatch; `type(StorePath("s3://…")) is StorePath` by construction. Both of
the draft's candidate mechanisms would have been worse: dynamic mixin
composition breaks pickling, `isinstance` across modules, and static typing;
a generated cross-product still leaves N classes to document and pickle.

The piece the draft missed entirely, and which I consider the single most
important extensibility finding: **derivation**. The reference rewraps via
bare `type(self)(new_wrapped)`, which means `StorePath`'s `read_only` flag is
*silently dropped by every derived path* — `p.parent`, `p / "x"`, every glob
result of a read-only store is writable. Today. In production. The fix is
pathlib's own extension pattern (`with_segments`, whose documented purpose is
"pass information to derivative paths"): one overridable `with_wrapped()`
hook that every internal rewrap goes through. The entire `StorePath` port
becomes one class with one `__init__` and one override — 12 classes → 0, and
a live bug fixed. That is the make-or-break test the task named, and the
data-not-types answer passes it better than either option the draft offered.

If class-level protocol dispatch is ever genuinely needed, adding it later is
backward-compatible. Shipping it now and removing it is not. Under "API
stability first", speculative machinery loses.

### Q3 — Driver reconciliation: what do UPath and cloudpathlib actually do?

Measured, for `s3://bucket/key/a/b.txt` (gs:// identical except cloudpathlib
names the accessor `blob` not `key` — they couldn't agree with *themselves*):

| | UPath 0.3.10 | cloudpathlib 0.25.0 |
|---|---|---|
| `.protocol` | `'s3'` (`''` for local) | absent (`cloud_prefix='s3://'`) |
| `.path` | `'bucket/key/a/b.txt'` | absent (private `_no_prefix` matches) |
| `.drive` | `'bucket'` | `'bucket'` |
| `.root` | `'/'` | **absent** |
| `.anchor` | `'bucket/'` | `'s3://'` |
| `.parts` | `('bucket/','key','a','b.txt')` | `('s3://','bucket','key','a','b.txt')` |
| `rmdir` | `rmdir(recursive=True)` — **deletes non-empty trees by default** (verified live on `memory://`) | `rmdir()` no flag, must be empty, raises `DirectoryNotEmptyError`; recursive is `rmtree()` |
| `unlink` | `missing_ok=False` | **`missing_ok=True`** (their comment admits it's a legacy wart) |
| `copy`/`move` | native | native (+ `force_overwrite_to_cloud`) |
| `relative_to` | returns `Self` | **returns `PurePosixPath`** |
| `__fspath__` | raises `AttributeError` on cloud | **downloads the file** (network I/O; `ClientError` without creds) |
| `as_posix` | `'s3://bucket/…'` | **absent** |
| equality | `UPath(u) == AnyPath(u)` → `False`, both directions, even with equal `str` | same |
| `touch` | `(mode, exist_ok)` | `(exist_ok, mode)` — **argument order swapped** |

Canonical answers (now in §5 of the doc): UPath's shapes for
`protocol`/`path`/`parts`/`anchor`, because they preserve pathlib's algebra
(`anchor == drive + root == parts[0]`) which cloudpathlib's shapes break, and
because `.path`'s stated purpose is fsspec interop and UPath's value *is* the
fsspec convention. `rmdir` defaults non-recursive — 2-of-3 consensus
(pathlib + cloudpathlib) *and* the only safe default for a destructive op;
UPath's recursive-by-default is a data-loss hazard the wrapper must actively
defuse by passing `recursive=False` explicitly. `unlink` defaults
`missing_ok=False` (2-of-3; the third calls its own default a mistake).
Where genuinely irreconcilable — cross-driver equality — the wrapper defines
its own driver-independent `__eq__` on canonical `(protocol, str)`, which the
reference never defined at all (its TODO admits it).

Two findings here deserve emphasis beyond the table. First, the draft's
speculative table had both cloud rows wrong: it said AnyPath's `path` is "key
without prefix" (canonical must *include* the bucket, and both libraries
agree once you find cloudpathlib's `_no_prefix`) and that cloudpathlib's
`rmdir` "takes flag" (it takes none; the reference's `rmdir(recursive)`
delegation is a `TypeError` on every cloudpathlib path). This vindicates the
task's insistence on running the prior art. Second, the `__fspath__` row is a
trap nobody had flagged: `os.fspath()` on a wrapped cloudpathlib path
performs a **network download** as a side effect. The doc now settles this
(raise for non-local, with the alternatives named in the message) with an
explicit owner veto slot, because there is no three-way consensus to lean on.

### Q4 — anyio: hard-require, optional, or shim?

**None of the above: stdlib-only, no anyio relationship at all** — assessed on
the merits as the owner asked, and the recommendation is firm:

- The direction the consumer actually uses (async-over-sync) needs exactly
  `get_running_loop().run_in_executor(None, partial(fn, *a, **kw))` — which
  also fixes the reference's kwargs-in-`run_in_executor` bug (`asyncio.to_thread`
  is 3.9+; this spelling is 3.8-safe). anyio adds no correctness here: neither
  library can cancel a running sync call.
- The hard direction (sync-over-async) is where anyio has a real offer —
  `start_blocking_portal()` is precisely the needed loop portal. But it is a
  *convenience*, not a capability: the stdlib equivalent (daemon thread +
  `run_coroutine_threadsafe(...).result()`, plus a caller-has-running-loop
  guard that raises instead of deadlocking) is ~40 auditable lines and is the
  pattern fsspec has run in production for years. Same semantics, same
  hazards, same lifetime questions — minus a dependency whose current major
  requires Python ≥3.10 against our 3.8 floor (adopting it means owning an
  anyio-3.x-on-old-Pythons pin matrix forever).
- The *only* thing stdlib-only forfeits is `AsyncPath` under trio. Zero
  downstream evidence; the failure mode is a clean `RuntimeError` we can
  message well; and an anyio-backed bridge can be added later behind the same
  internal seam, purely additively. Note also that supporting `anyio.Path` as
  a *driver* never required depending on anyio — its members are ordinary
  coroutine functions the bridge detects and awaits.
- Sequencing: sync-over-async ships as the stdlib portal in v1.x; v1.0 raises
  at construction with a message pointing to `AsyncPath`. Deferring is free
  because adding it is additive; shipping it half-designed in v1.0 is not.

### Q5 — Error/capability model and public names

**Yes to `UnsupportedPathOperation` + `supports()`, with three refinements;
names settled as `Path`/`AsyncPath` and the surface cut down.**

- Exception bases: on 3.13+ inherit `pathlib.UnsupportedOperation` (itself a
  `NotImplementedError` subclass — this is stdlib's own answer to exactly
  this problem, adopted in 3.13), else `NotImplementedError`. Three lines,
  and `except pathlib.UnsupportedOperation` catches ours on modern Pythons.
- `supports()` must account for **fallbacks** (`supports("copy")` is true
  when the primitives exist) and must be documented as *static* — it answers
  "is this wired", not "will this call succeed on this particular path".
  The draft left both unstated; either omission would have produced a
  misleading API.
- Names: `Path` wins over `WrappedPath` on unanimous neighbour precedent —
  `anyio.Path`, `trio.Path` both shadow `pathlib.Path` and the ecosystem is
  fine; qualified import is the normal remedy. No `WrappedPath` alias (two
  names for the flagship class is a stability cost with no story for which
  one examples use). **Cut `wrap()`/`async_wrap()`** — `Path(obj)` already is
  the factory; a second spelling is surface without power. Cut the `PathLike`
  re-export. Per-protocol classes don't exist (Q2), so nothing to name.
  `touch`/`mkdir` return `None`, not `self` — all three neighbours agree, and
  fluent-return is the kind of stdlib divergence you can never walk back.

### Q6 — Structural things the draft missed that would hurt later

In rough order of pain avoided:

1. **No equality/hash design.** A path type is a dict key on day one. The
   drivers disagree with each other (`UPath(u) != AnyPath(u)`), the reference
   defines nothing, and retrofitting equality semantics is a breaking change.
   Now settled in §5 and scheduled in phase 2, where identity belongs.
2. **The derivation hook** (Q2) — both an extensibility mechanism and a live
   downstream bug (`read_only` loss).
3. **`AsyncPath.open()`** — 23 downstream uses, and the draft's async section
   never said what `open` *returns*. Answered: native async file passes
   through; sync file gets a small internal async adapter.
4. **`walk()` yields tuples**, and the reference wraps the whole
   `(dirpath, dirnames, filenames)` tuple in `type(self)` — broken for every
   driver, unnoticed in the draft's bug list. The spec's result policy now
   has a `walk`-tuple kind.
5. **`match`/`full_match` have a correct, cheap answer** the draft treated as
   an implement-or-raise dilemma: CPython 3.13's `glob.translate` is the
   reference implementation of pathlib's matching semantics, and this repo
   family already has the PSF-porting discipline (`NOTICE.md`,
   `LICENSE-PSF-2.0.txt`, a licensing test) to vendor it. Raising on the
   3.8–3.12 range for a *lexical* operation would have been a needless hole.
6. **Extras can't reach 3.8**: universal-pathlib and cloudpathlib now require
   ≥3.9, so the 3.8 CI leg is core-only by necessity — which conveniently is
   the leg that proves the dependency-free story. The draft's CI plan would
   have discovered this as a red pipeline.
7. **Testing the cloud matrix without credentials**: cloudpathlib ships local
   test doubles (`cloudpathlib.local`) and UPath has `memory://`; the
   consistency matrix is executable in CI. The draft named the test file but
   not the mechanism.

---

## 2. The owner's generic-adapter question

The owner's instinct — one generic duck-typed adapter that "probably works"
on any unknown object, with named adapters *inheriting* from it and
overriding only divergences — **holds up under measurement, and I adopted it
as the settled §5 structure.** I probed it concretely: a generic chain of
"trust a matching attribute, else parse `str(p)`, else synthesize" produces
the canonical `protocol`/`path`/`drive` for pathlib, UPath (s3, memory,
local), *and* cloudpathlib with zero per-driver knowledge — cloudpathlib has
no `.protocol`/`.path` at all and the parse branch canonicalizes it for free.

The measured override budget is small and clean, which is what makes the
inheritance model honest rather than aspirational: **pathlib needs 0
overrides; UPath needs 1; cloudpathlib needs 3; anyio.Path needs 0** (async
nativeness is detected per-member by the bridge, not declared). The UPath
override is the one to respect: its bare `rmdir()` recursively deletes
non-empty trees, so the generic adapter's bare delegation would silently
inherit a data-loss default — a named adapter forced to say
`recursive=False` explicitly is not boilerplate, it is the safety property.
That is also the honest limit of duck-typing: the generic adapter converts
"unknown driver" from an error into best-effort (the right default for
extensibility), but the known families stay *registered* precisely because
some divergences are dangerous rather than cosmetic. Two rules keep the tree
maintainable: named adapters override methods, never re-implement the
generic chain; and a mis-read exotic object's remedy is a small registered
adapter, i.e. exactly the mechanism the named families already use.

---

## 3. What I changed in the design doc, summarized

- §1/§7: sync-over-async scoped as deferred-but-designed (stdlib loop
  portal, fsspec pattern, v1.x), with a clean construction-time error in
  v1.0. "Ideally bridges both ways" is now a plan, not an aspiration.
- §2: added the tensorstore `bucket`+`path` evidence, the explicit
  `rmdir(recursive=True)` call sites (proving the safe default is free), and
  the `read_only`-loss and registry-clobbering findings.
- §3: decision made (C, as sync-engine + thin methods + parity test +
  docstring policy); options A/B/D recorded with the reasons they lost.
- §4: added the target policy for path-taking members, the two kwargs
  refinements (always-forward for divergent driver defaults; keyword-only
  forwarding for swapped orders — both measured needs), exception bases,
  `supports()` semantics, and the `glob.translate` port decision.
- §5: replaced the speculative table with the measured one (two rows of the
  original were factually wrong), wrote a canonical answer per concept,
  restructured the mechanism around the generic-adapter inheritance model
  with measured override counts, and settled `rmdir`, `unlink`,
  `__fspath__` (flagged for veto), and equality.
- §6: reversed to no-protocol-subclasses; traits registry + `with_wrapped`
  derivation hook; construction de-magicked.
- §7: full anyio-vs-stdlib ledger with a firm stdlib-only recommendation;
  bridge granularity, iterator adaptation both directions, the async `open`
  answer.
- §8: surface cut to five names; naming settled with reasons.
- §9/§10: added `_engine`/`_bridge`/`_match`/`_base` and the parity +
  reference-bug + licensing tests; extras/CI reality for 3.8; PSF
  obligations; no anyio extra.
- §11: bug list grown from 10 to 15 (unwrapped `parent`/`parents`; the walk
  tuple bug; `rmdir` positional-arg `TypeError` on cloudpathlib +
  dangerous default; derivation state loss; `-> str` operator annotations)
  and one *corrected*: the reference's `anchor` is not a wrong default, it
  is unconditional infinite recursion (`getattr(self, "anchor", …)` invokes
  the property itself; the default only rescues `AttributeError`).
- §12: identity (`__eq__`, `with_wrapped`, `_match`) pulled into phase 2 —
  hardest-to-change-later goes first.
- §13: rewritten from six open questions to six settled decisions plus five
  crisply-scoped owner calls.

## 4. Where I disagree with the original draft

1. **§6's framing.** Asking "mixins or generated cross-product?" presupposes
   protocol dispatch is worth keeping. The evidence (empty subclasses,
   one-property payload, three bugs in the machinery) says the axis itself
   is the defect. This is the change most worth debating me on, because it
   is a reversal rather than a refinement — but the burden should be on
   protocol *types* to justify themselves, and nothing in the consumer does.
2. **"Codegen is the fallback if duplication proves too costly."** I removed
   the hedge. The duplication under C-with-sync-engine is thin signatures
   plus ~8 fallback mirrors, all parity-tested; if that "proves too costly"
   something else is wrong. Keeping B alive as a fallback invites
   relitigation of the architecture mid-port.
3. **The draft's §5 table** presented recalled values as facts; two of five
   cloud rows were wrong (AnyPath `path`, cloudpathlib `rmdir`), and the
   errors were in the direction that would have produced wrong canonical
   decisions. Not a process criticism — the draft itself asked for this
   verification — but the corrected values change the design.
4. **`Path` naming hedge** ("leaning Path with a WrappedPath alias"): half
   the hedge is right. The alias is pure cost; anyio/trio precedent settles
   the shadowing concern.

## 5. Risks the draft underweights

- **UPath's destructive `rmdir` default.** This is not a compatibility
  nuance; it is the wrapper's most likely path to deleting user data. The
  engine must pass `recursive=False` explicitly to UPath, the consistency
  matrix must include a "non-empty dir, bare rmdir → error, data intact" row
  for every driver, and any future "generic driver" path must never delegate
  `rmdir` blind. (The generic-adapter model makes this concrete: it is the
  one override UPath *must* have.)
- **`__fspath__` as a side-effect channel.** `os.fspath()` triggering an S3
  download (cloudpathlib) or returning a URL string that `open()` chokes on
  (the reference's fallback) are both silent-wrongness of the worst kind,
  because `os.fspath` is called implicitly by third-party code. Whatever the
  owner decides on my raise-for-non-local proposal, it must be an explicit
  decision with a test.
- **UPath 0.2 → 0.3 churn.** The measured shapes are 0.3.x; UPath has
  historically moved (its own `rmdir`/`unlink` signatures differ across
  versions). The adapter tests should pin minimum driver versions in the
  extras and CI should run the oldest supported driver version at least once,
  or canonicalization claims will rot silently.
- **Thread-per-call costs in `AsyncPath`.** One hop per operation is fine;
  one hop per *iterated item* (bridged `iterdir`/`glob` on big cloud
  listings) may not be. Not a v1 blocker, but the bridge should keep the
  iterator adaptation behind its own seam so batching can be added without
  API change.
- **The fallback driver is a growth magnet.** The dependency-free driver
  only needs pure-path + protocol behavior, but it sits one PR away from
  becoming a half-filesystem (the reference's `FallbackPath` already grew
  glob). The non-goal in §1 should be enforced in review: concrete I/O in
  the fallback driver is out of scope, state 3 is the correct answer there.

## 6. Bottom line

The plan as now written is safe to commit to: the three hardest-to-reverse
surfaces — identity (`__eq__`/`__hash__`), derivation (`with_wrapped`), and
the public namespace (five names) — are decided and scheduled first; the
sync/async pair is structurally drift-proof rather than hopefully so; both
bridge directions are stdlib-designed with the hard one sequenced, not
hand-waved; and every driver-divergence decision now traces to a measured
behavior or a 2-of-3 neighbour consensus, per house rule. The remaining owner
calls (§13: `__fspath__` veto, `key` accessor, portal timing, anyio veto,
driver preference) are real preference calls, not design gaps — none of them
blocks phases 1–3.
