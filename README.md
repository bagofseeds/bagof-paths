# bagof-paths

**One path API for local files and the cloud.**

Wrap a path or a URL, then use it like `pathlib.Path`. The same code reads a
local file, an object on S3, or anything the underlying libraries reach.

```python
from bagof.paths import Path

Path("/data/train.zarr")           # a local file
Path("s3://my-bucket/train.zarr")  # an object on S3
```

Both give you the same methods: `read_bytes`, `exists`, `iterdir`, `/`, and
the rest of the `pathlib` surface.

```pycon
>>> from bagof.paths import Path
>>> p = Path("/data/sets/train.zarr")
>>> p.name
'train.zarr'
>>> p.parent
Path('/data/sets')
>>> p / "chunks"
Path('/data/sets/train.zarr/chunks')
```

## Features

* **One API for every path.** Local files, cloud storage, and unknown
  backends all use the same methods.
* **Missing methods are filled in.** When a library lacks a method,
  `bagof.paths` builds it from simpler ones. It reads text from bytes, and
  copies a folder by copying its files.
* **One error to catch.** An operation that cannot work raises a single
  `UnsupportedPathOperation`, the same for every library.
* **Async support.** `AsyncPath` gives you the same methods with `await`.
* **No required dependency.** Local paths use only the standard library.

## Installation

```sh
pip install bagof-paths          # local paths
pip install bagof-paths[upath]   # add remote paths via universal-pathlib
pip install bagof-paths[cloud]   # add cloud paths via cloudpathlib
```

A remote store also needs its own library, such as `s3fs` for `s3://` or
`cloudpathlib[s3]`.

## The same code, local or remote

`Path` reads the start of the string to choose a backend. A plain path is a
local file. A URL uses `universal-pathlib`, or `cloudpathlib` if you have it.

```python
from bagof.paths import Path

def load(location: str) -> bytes:
    return Path(location).read_bytes()

load("/data/train.bin")
load("s3://my-bucket/train.bin")
```

## Credentials

Pass connection details with `storage_options`. They go straight to the
backend.

```python
from bagof.paths import Path

Path(
    "s3://my-bucket/train.bin",
    storage_options={"key": "AKIA...", "secret": "...", "endpoint_url": "..."},
)
```

Leave `storage_options` off to use ambient credentials, such as environment
variables, `~/.aws/config`, or an instance role.

Set defaults once for a scheme with `register_protocol`, and a per-call
`storage_options` overrides them key by key.

## Async

`AsyncPath` turns the methods that touch storage into coroutines. The methods
that only describe a path (`name`, `parent`, `/`) stay synchronous.

```python
import asyncio
from bagof.paths import AsyncPath

async def main() -> None:
    p = AsyncPath("s3://my-bucket/train.bin")
    if await p.exists():
        data = await p.read_bytes()

asyncio.run(main())
```

## Learn more

See [how it compares](docs/comparison.md) to `pathlib`, `UPath`, and `AnyPath`.
