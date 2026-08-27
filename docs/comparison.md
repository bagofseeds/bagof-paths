---
icon: material/compare
---

# How it compares

`bagof.paths` is not a filesystem. It is a thin, uniform surface *over*
`pathlib`, [`universal-pathlib`][upath] (`UPath`), and
[`cloudpathlib`][cloudpathlib] (`CloudPath`/`AnyPath`) — so one piece of code
reads and writes local files, `s3://` objects, and anything else those drivers
reach, through the same API and with the same behaviour.

[upath]: https://github.com/fsspec/universal_pathlib
[cloudpathlib]: https://github.com/drivendataorg/cloudpathlib

## At a glance

|  | pathlib | UPath | cloudpathlib | bagof-paths |
| --- | --- | --- | --- | --- |
| Local filesystem | yes | yes | as a local path | **yes** |
| Cloud object stores (`s3`/`gs`/`az`) | no | via fsspec | native SDKs | **via either** |
| One API across all backends | — | mostly | mostly | **yes** |
| A missing operation | `AttributeError` / `NotImplementedError` | varies by backend | varies by backend | **one named error, or synthesized** |
| Behaviour reconciled across backends | n/a | partly | partly | **yes (measured)** |
| Async API | no | no | no | **`AsyncPath`** |
| Equality across drivers | n/a | by driver type | by driver type | **driver-independent** |
| Build from a URL string | local only | yes | yes | **yes (picks a backend)** |
| Teach it a new driver/protocol | subclass | fsspec plugin | subclass + register | **`register_driver` / `register_protocol`, or just wrap it** |
| Runtime dependency | none | `universal-pathlib` + `fsspec` | `cloudpathlib` + cloud SDKs | **`typing_extensions`; backends optional** |

The wrapper does not replace those drivers — it delegates to them. What it adds
is *uniformity*: a single surface, a single failure mode, one identity model,
and one async story, no matter which driver is underneath.

## The same task, three ways

Read a file that might be local **or** on a bucket, the same code either way:

```python
from bagof.paths import Path

def load(location: str) -> bytes:
    return Path(location).read_bytes()

load("/data/train.bin")        # a local file
load("s3://my-bucket/train.bin")  # an object on S3 (needs a backend installed)
```

`Path(location)` picks a driver from the scheme: local paths use stdlib
`pathlib`; a URL uses `universal-pathlib` (or `cloudpathlib`) if one is
installed. `read_bytes` then means the same thing everywhere.

The lexical surface — the pieces of a path, and pattern matching — reads the
same across every backend (`match`/`full_match` are computed on the canonical
path, so they never depend on the driver):

```pycon
>>> from bagof.paths import Path
>>> p = Path("/data/sets/train.zarr")
>>> p.name
'train.zarr'
>>> p.parent
Path('/data/sets')
>>> p.suffix
'.zarr'
>>> (p / "chunks").match("*/chunks")
True
```

## What each layer is for

### pathlib

The standard-library path type. Perfect for local filesystems, and the thing
`bagof.paths` wraps for a local path. It has no notion of a remote store and no
async API.

### universal-pathlib (`UPath`)

`UPath("s3://…")` gives a `pathlib`-like object over any
[fsspec](https://filesystem-spec.readthedocs.io/) filesystem — the widest
reach, and the driver `bagof.paths` reaches for first. It builds lazily (no
cloud SDK needed just to construct a path). `bagof.paths` wraps a `UPath` and
smooths over the places its surface differs from stdlib `pathlib`.

### cloudpathlib (`CloudPath` / `AnyPath`)

A focused, well-typed path type for the major clouds (`s3`, `gs`, `azure`),
backed by the official SDKs and a local cache. `bagof.paths` can select its
concrete class as a fallback, and wraps a `CloudPath` you hand it.

### bagof-paths

One surface over all of the above (and unknown drivers), for code that should
not care which one is underneath. It **delegates** each member to the driver,
**synthesizes** a fallback when the driver lacks it (`read_text` from
`read_bytes`, `copy` from `shutil`, `walk` from `iterdir`, …), or **raises**
one named error, `UnsupportedPathOperation`, when neither is possible. Anything
a driver exposes that the surface does not is still reachable through
`path.wrapped`.

## Things bagof-paths adds

- **One failure mode.** Where a raw driver raises `AttributeError`, a bare
  `NotImplementedError`, or a backend-specific exception for an unsupported
  operation, the wrapper raises `UnsupportedPathOperation` (a subclass of
  `pathlib.UnsupportedOperation` where it exists), naming the operation and the
  driver.
- **Reconciled behaviour.** Divergences that would silently corrupt data are
  fixed in one place — e.g. `UPath`'s bare `rmdir()` deletes a non-empty tree,
  so the wrapper always removes non-recursively unless you ask otherwise.
- **Driver-independent identity.** Two wrappers pointing at the same location
  compare and hash equal regardless of which driver backs them, and scheme
  aliases (`s3`/`s3a`, `gs`/`gcs`) fold together.
- **An async surface.** `AsyncPath` exposes the I/O members as coroutines (the
  lexical ones stay synchronous), running a synchronous driver in a worker
  thread so the event loop is never stalled — no driver has to be async.
- **Extensibility without a class.** `register_protocol` teaches it a new
  scheme's traits and preferred driver; `register_driver` teaches it a new
  backend's divergent operations. Both are one call.
