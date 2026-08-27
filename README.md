# bagof-paths

**One path API for local files and the cloud.**

Hand it a local path or a cloud URL. You get back something that works like
`pathlib.Path`, whichever kind of path it is.

```python
from bagof.paths import Path

Path("/data/train.zarr")           # a local file
Path("s3://my-bucket/train.zarr")  # an object on S3
```

Both give you the same methods — `read_bytes`, `exists`, `iterdir`, `/`, and
the rest — so the code that uses them does not have to care where the path
lives.

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

## Install

```sh
pip install bagof-paths          # local paths, nothing else needed
pip install bagof-paths[upath]   # add cloud and other remote paths (universal-pathlib)
pip install bagof-paths[cloud]   # add cloud paths (cloudpathlib)
```

Local paths work on their own. To open `s3://`, `gs://`, `az://` and the
like, install one of the extras above, plus the small library for your store
(for example `s3fs`, or `cloudpathlib[s3]`).

## The same code, wherever the file lives

Read a file without knowing, or caring, whether it is local or remote:

```python
from bagof.paths import Path

def load(where: str) -> bytes:
    return Path(where).read_bytes()

load("/data/train.bin")
load("s3://my-bucket/train.bin")
```

## It fills in the gaps

Not every library offers every method. When one is missing, `bagof.paths`
builds it from the pieces that are there — `read_text` from `read_bytes`,
copying a folder by copying its files, and so on — so you can count on the
full set of `pathlib` methods even when the library underneath is smaller.

When a method genuinely cannot work for a given path, you get the same clear
error every time, instead of a different surprise for each library.

## Async, too

`AsyncPath` gives you the same paths, with `await`:

```python
import asyncio
from bagof.paths import AsyncPath

async def main() -> None:
    p = AsyncPath("s3://my-bucket/train.bin")
    if await p.exists():
        data = await p.read_bytes()

asyncio.run(main())
```

The parts that only describe a path — its name, its parent, joining with `/` —
stay plain; only the parts that actually touch storage need `await`.

## Learn more

See [how it compares](docs/comparison.md) to plain `pathlib`, `UPath`, and
`AnyPath`.
