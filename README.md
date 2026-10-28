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
* **No required dependency.** Local paths use only the standard library, and a
  URL can be parsed and rebuilt with no backend installed.

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

Some libraries open files only by name. `is_local()` tells you whether a path
is a local file you can hand to them; it reads only the path, never storage.
On a remote path `os.fspath` raises `NoLocalPathError`, a `TypeError`.

```pycon
>>> from bagof.paths import Path
>>> Path("/data/scan.nii").is_local()
True
>>> Path("file:///data/scan.nii").is_local()
True
```

To hand a remote file to such a library, take a local copy. It keeps the
file's exact name, and it is removed when the block ends; a local path is used
as it is.

```python
with Path("s3://bucket/slides/case1.svs").local_copy() as local:
    slide = openslide.OpenSlide(local)
```

Some formats come with companion files the library opens by itself (an
Analyze `.hdr` and its `.img`, a MIRAX slide and its tile directory). Take
their copies in one `local_stage()`, held at the same time, and they sit side
by side as they do in the store:

```python
with Path.local_stage():
    with hdr.local_copy() as h, img.local_copy() as i:
        image = nibabel.load(h)
```

Changes to a copy are not written back.

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

Set defaults once for a scheme with `set_storage_options`, and a per-call
`storage_options` overrides them key by key.

```python
from bagof.paths import set_storage_options

set_storage_options("s3", {"endpoint_url": "https://minio.local"})
```

## Writing safely

A writer that fails halfway leaves a broken file, and a reader that has the
file open, or memory-mapped, can see a torn one. `staged()` gives a staging
area named like the target: create the new version there, and it replaces
the target in one step when the block ends. An error leaves the target as it
was.

```python
with Path("out/scan.nii.gz").staged() as tmp:
    nibabel.save(image, tmp)          # a library that saves by name

with Path("s3://bucket/store.zarr").staged() as tmp:
    zarr.save(tmp, data)              # staged locally, uploaded on success

Path("out/table.csv").write_text(text, atomic=True)
```

The new version can be a file or a whole directory. `staged(copy=True)` starts
it as a copy of the current one, for updates in place (on a remote path, that
is a full download). A file is replaced atomically on POSIX and on object
stores; a directory is all-or-nothing, but briefly missing while it is swapped.

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

For a cloud store whose library speaks async (such as `s3://` through `s3fs`),
`AsyncPath` talks to it directly, with no threads. For every other path it runs
the synchronous driver in a worker thread, so the same code works either way.

## Parsing without a backend

Every method that only describes a path — `name`, `parent`, `suffix`,
`with_name`, `/`, `match` — works on a URL even when no path library is
installed. A `Path` falls back to plain parsing, and raises only when an
actual read or write is attempted.

```python
from bagof.paths import Path

# With no backend installed, the URL is still parsed.
p = Path("s3://my-bucket/data/train.zarr")
p.name                 # 'train.zarr'
p.parent               # Path('s3://my-bucket/data')
p / "chunks"           # Path('s3://my-bucket/data/train.zarr/chunks')

p.read_bytes()         # raises UnsupportedPathOperation until a backend is installed
```

For a value that must never touch storage, use `PurePath`. It has the same
parsing surface as `Path` and none of the I/O methods, so a read or write is
not even offered.

```pycon
>>> from bagof.paths import Path, PurePath
>>> pure = PurePath("/data/sets/train.zarr")
>>> pure.name
'train.zarr'
>>> pure.with_suffix(".zip")
PurePath('/data/sets/train.zip')
>>> pure == Path("/data/sets/train.zarr")
True
```

A URL's query is not part of its name, on any scheme: everything from the
first `?` is left out (and, in an `http` URL, from the first `#`). So a
presigned or versioned URL still has the right suffix, and its name can name
a file in another store. The path itself keeps the query, so a read still
sends the token. Outside `http`, a `#` is part of the name, as in
`Invoice #12.pdf`.

```pycon
>>> url = PurePath("https://example.org/data/brain.nii.gz?token=abc")
>>> url.name
'brain.nii.gz'
>>> url.suffixes
['.nii', '.gz']
>>> url.parent
PurePath('https://example.org/data')
>>> str(url)
'https://example.org/data/brain.nii.gz?token=abc'
>>> url.without_query()
PurePath('https://example.org/data/brain.nii.gz')
```

A `PurePath` and a `Path` to the same place compare equal and hash alike, so
either one looks the other up in a set or a dictionary. This mirrors
`pathlib`, where a `PurePath` equals the `Path` beside it.

## Learn more

See [how it compares](docs/comparison.md) to `pathlib`, `UPath`, and `AnyPath`.
