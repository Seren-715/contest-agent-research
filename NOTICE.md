# NOTICE

## AgentHack base image and SDK

The official image is `agenthack/base:1.1.1`, provided by the competition
organizers. It provides the `agenthack` Python SDK (`BaseAgent`, `AgentRuntime`,
`tool`) and the HTTP service. This repository keeps the SDK and uses its
`@tool` decorator; it does not vendor or modify the SDK.

## Platform-provided D-class package

The D-class adapter ports the source-extraction rules from the platform's public
`model_source.py` (package `research-experiments-20261020-source-v1`, protocol
`layered-d-v3`, source provenance `d-model-source-v1`). That file is provided by
the competition organizers for contestant use. No D-class algorithm is vendored
or pre-written in this repository.

## PaperCode

Reference: PaperCode, https://github.com/S0ra-ai/PaperCode, commit `67cad51`.
Used as a research/writing reference and as a source of prompt and evidence
discipline ideas. PaperCode is licensed under MPL-2.0; its third-party
components retain their own licenses (see the PaperCode `NOTICE.md`). This
repository does not ship PaperCode runtime code.

The B/C/E evidence pipeline is an independent Python reimplementation of the
following PaperCode design ideas: `MaterialIndex` (file hash, pages, title,
abstract, excerpt), `ClaimLedger` / Gate-Honest (fact sentences must cite a
specified source), `NoveltyCheckpoint` / `PriorWorkMap` (topic-overlap summary
before proposing an idea), `PaperSpine` (problem -> motivation -> method ->
evidence -> boundary), and the manuscript gate's section/placeholder checks.
No TypeScript source file is vendored; see `docs/PAPERCODE_REUSE.md` for the
mapping.

## Third-party Python packages

FastAPI (MIT), Uvicorn (BSD-3-Clause), HTTPX (BSD-3-Clause), Pydantic (MIT)
are installed as dependencies, not vendored.

`vendor/python/pypdf` contains the pure-Python pypdf 5.9.0 package (BSD-3-Clause)
solely so the official image can be built without PyPI access. It is an
unmodified copy of the installed package; its license text is retained in the
`pypdf-5.9.0.dist-info` directory.
