# bagof-paths

A uniform, `pathlib`-style API over any path-like object — the stdlib
`pathlib.Path`, [`universal-pathlib`](https://github.com/fsspec/universal_pathlib)'s
`UPath`, [`cloudpathlib`](https://github.com/drivendataorg/cloudpathlib)'s
`AnyPath`, and unknown drivers alike.

`bagof.paths` wraps a path object — or a URL string like `s3://bucket/key`,
picking a backend by scheme (universal-pathlib, else cloudpathlib; override
with `driver=` or `register_protocol`) — and exposes one consistent surface on
top of it. For each member of that surface it either:

- **delegates** to the wrapped object when it implements it,
- **falls back** to a synthesized implementation when it does not (for example,
  `read_text` from `read_bytes`, `read_bytes` from `open`, `copy` from
  `shutil`), or
- **raises** a single, well-named error when neither is possible.

A sync wrapper (`Path`) and an async wrapper (`AsyncPath`) share the same
surface; the async wrapper bridges a blocking driver by running it in a worker
thread.

## Install

```sh
pip install bagof-paths          # core: stdlib pathlib, typing_extensions only
pip install bagof-paths[upath]   # + universal-pathlib (each fsspec backend, e.g. s3fs, adds its own)
pip install bagof-paths[cloud]   # + cloudpathlib (each cloud, e.g. cloudpathlib[s3], adds its own SDK)
```

The core wraps stdlib `pathlib` with only `typing_extensions` as a
dependency; `upath` and `cloudpathlib` are optional extras (Python ≥ 3.9).

## Quick start

Wrap a path and use it like `pathlib`:

```pycon
>>> from bagof.paths import Path
>>> p = Path("/data/sets/train.zarr")
>>> p.name
'train.zarr'
>>> p.parent
Path('/data/sets')
>>> (p / "chunks").suffix
''
```

The same code works over a bucket once a backend is installed — the scheme
selects the driver:

```python
from bagof.paths import Path

def load(location: str) -> bytes:
    return Path(location).read_bytes()

load("/data/train.bin")           # local file, via pathlib
load("s3://my-bucket/train.bin")  # S3 object, via universal-pathlib
```

An unsupported operation is one named error, so a caller can handle it
uniformly:

```pycon
>>> from bagof.paths import UnsupportedPathOperation
>>> issubclass(UnsupportedPathOperation, NotImplementedError)
True
```

`AsyncPath` mirrors the I/O surface with coroutines (lexical members like
`name` and `parent` stay synchronous), running a blocking driver in a worker
thread:

```python
import asyncio
from bagof.paths import AsyncPath

async def main() -> None:
    p = AsyncPath("s3://my-bucket/train.bin")
    if await p.exists():
        data = await p.read_bytes()

asyncio.run(main())
```

The architecture and the reconciled cross-driver behaviour are written up in
[`docs/design/path-wrapper.md`](docs/design/path-wrapper.md); see
[the comparison page](docs/comparison.md) for how it relates to raw
`pathlib`/`UPath`/`AnyPath`.

The workflow wrappers intentionally track `bagofseeds/actions@main` so the
repository inherits shared CI updates without manually refreshing pinned
workflow SHAs.
