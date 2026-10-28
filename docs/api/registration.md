# Registration and errors

How to add support for a new URL scheme or a new path library, and the
errors these paths can raise.

## ::: bagof.paths.register_protocol

## ::: bagof.paths.set_storage_options

## ::: bagof.paths.ProtocolTraits

## A path library of your own

Any object that looks like a path can be wrapped: it needs `__fspath__`, a
`parts` attribute, or a `path` attribute. The wrapper then uses the object's
own members, by name:

- **I/O** (`read_bytes`, `open`, `iterdir`, ...) is called on the object.
  Missing members are built from the ones it has where possible:
  `read_text` from `read_bytes`, `read_bytes` from `open`, and so on.
- **The path's parts** (`name`, `stem`, `suffix`, `parts`, `parent`,
  `joinpath`, `/`, `with_name`, `with_suffix`, `relative_to`, ...) must come
  from the object itself. They are not computed from its string, because a
  new path, such as a `parent`, has to keep the object's configuration (its
  credentials, client or session), which a string does not hold.
- **`with_segments(*parts)`**, pathlib's way to make a new path of the same
  kind, lets the wrapper build a related path, for instance without its
  query, or as a copy target. Give a remote path library one.

The simplest way to get all of this is to subclass a path class that already
has it, such as `pathlib.PurePosixPath` or universal-pathlib's `UPath`. A
member the object lacks raises `UnsupportedPathOperation` naming it.

For a test, a hand-written fake is rarely needed: universal-pathlib's
`memory://` is a real remote path that needs no network or credentials.

```python
from bagof.paths import Path

p = Path("memory://bucket/data/scan.nii.gz")
p.write_bytes(b"...")
p.name        # 'scan.nii.gz'
```

## ::: bagof.paths.register_driver

## ::: bagof.paths.NoDriverError

## ::: bagof.paths.UnsupportedPathOperation

## ::: bagof.paths.CommitError
