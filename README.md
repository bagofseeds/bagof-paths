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

> **Status: in design.** The architecture and public surface are written up in
> [`docs/design/path-wrapper.md`](docs/design/path-wrapper.md); implementation
> is landing in phases. The core wraps stdlib `pathlib` with no third-party
> dependency; `upath` and `cloudpathlib` are optional extras.

The workflow wrappers intentionally track `bagofseeds/actions@main` so the
repository inherits shared CI updates without manually refreshing pinned
workflow SHAs.
