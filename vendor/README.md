# vendor

## `d-class/`

The published platform package `research-experiments-20261020-source-v1` is
committed here (public contest material) so teammates and CI can run the D
self-tests without downloading it again.

It contains `method.md`, `protocol.json`, `sample.json`, `worker.py`,
`artifacts.py`, the public sample requests and the **empty** `implementation.py`
stubs. It does not contain a reference algorithm, private test data or answers.

Never submit a pre-written algorithm: the official D source must be generated
through the platform Gateway in the current case session and saved verbatim.

The separate `-local` draft in `~/Downloads` is not used; the published package
here is authoritative.

## `reference/`

Local-only D reference implementations live in `vendor/reference/`. The whole
directory is git-ignored and is not copied into the image. It is for local
self-tests only; see [docs/D_REFERENCES.md](../docs/D_REFERENCES.md).
