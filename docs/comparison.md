---
icon: material/compare
---

# How it compares

`bagof.paths` is not a new filesystem. It sits on top of the path libraries
you already know: the standard library's `pathlib`,
[`universal-pathlib`][upath] (`UPath`), and [`cloudpathlib`][cloudpathlib]
(`CloudPath` and `AnyPath`). It gives them one shared set of methods, so the
same code reads and writes local files, `s3://` objects, and anything else
those libraries reach.

[upath]: https://github.com/fsspec/universal_pathlib
[cloudpathlib]: https://github.com/drivendataorg/cloudpathlib

## At a glance

|  | pathlib | UPath | cloudpathlib | bagof-paths |
| --- | --- | --- | --- | --- |
| Local files | yes | yes | as a local path | **yes** |
| Cloud storage (`s3` / `gs` / `az`) | no | yes | yes | **yes, through either** |
| One set of methods for all of them | n/a | mostly | mostly | **yes** |
| When a method is missing | error | depends | depends | **filled in, or one clear error** |
| Same behaviour on every store | n/a | mostly | mostly | **yes** |
| An `await` version | no | no | no | **`AsyncPath`** |
| Two paths to the same place are equal | n/a | only if same library | only if same library | **always** |
| Make one from a URL | local only | yes | yes | **yes** |
| Add support for another store | subclass | fsspec plugin | subclass + register | **one function call, or just wrap it** |
| What it needs installed | nothing | `universal-pathlib` | `cloudpathlib` + a cloud library | **nothing for local; a library per store** |

## The same task, three ways

Read a file that might be local or in a bucket, with one function:

```python
from bagof.paths import Path

def load(where: str) -> bytes:
    return Path(where).read_bytes()

load("/data/train.bin")           # a local file
load("s3://my-bucket/train.bin")  # an object on S3 (with a cloud library installed)
```

`Path(where)` looks at the start of the string to decide what kind of path it
is. A plain path is a local file. A URL uses `universal-pathlib`, or
`cloudpathlib` if you have it. `read_bytes` then means the same thing in both
cases.

The parts that only describe a path read the same whatever is underneath:

```pycon
>>> from bagof.paths import Path
>>> p = Path("/data/sets/train.zarr")
>>> p.name
'train.zarr'
>>> p.suffix
'.zarr'
>>> (p / "chunks").match("*/chunks")
True
```

## What each one is for

### pathlib

The standard library's path type. It is great for local files, and it is what
`bagof.paths` uses for a local path. It has no idea about cloud storage, and no
`await` version.

### universal-pathlib (`UPath`)

`UPath("s3://…")` gives you a `pathlib`-style path over a wide range of remote
stores. It has the widest reach, and it is the first thing `bagof.paths` tries
for a URL. It does not need a cloud library to make the path, only to read or
write.

### cloudpathlib (`CloudPath` / `AnyPath`)

A focused, well-typed path for the big three clouds (`s3`, `gs`, `azure`). It
uses the official cloud libraries and a local cache. `bagof.paths` can use it
too, and it wraps a `CloudPath` you already have.

### bagof-paths

One set of methods over all of the above, and over a path library it has never
seen. For each method it uses the underlying library when it can, builds the
method from simpler ones when it cannot, or raises one clear error when neither
is possible. Anything a library offers that these methods do not name is still
there, on `path.wrapped`.

## What bagof-paths adds

* **One error for every library.** When an operation is not possible, you get
  the same `UnsupportedPathOperation`. It names the operation and the library.
* **Safe defaults.** Where two libraries disagree in a way that could lose
  data, it picks the safe answer. Removing a folder does not delete a non-empty
  tree unless you ask for that.
* **Paths compare by where they point.** Two paths to the same place are equal
  even when they come from different libraries. Different spellings of a scheme
  (`s3` and `s3a`, `gs` and `gcs`) count as the same.
* **An `await` version.** `AsyncPath` gives you the same methods as coroutines.
  It runs a blocking library in a background thread, so your event loop keeps
  moving.
* **Extensible.** Add a new URL scheme, or a new library's quirks, with a
  single function call.
