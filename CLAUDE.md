# heat-replay-parser — guidance for Claude Code

Standalone, dependency-free Python library + CLI that parses **World of Tanks: HEAT**
`.replay` files. `heat_replay.parse(path)` returns a fully-structured `Replay`; consumers
never touch raw bytes.

## Public-repo discipline (IMPORTANT)
This is a public repository. Keep all committed code and docs free of reverse-engineering
provenance — do **not** mention decompilers, tooling, binary/DLL names, address constants,
internal engine codenames, or vendor-internal type-system names in tracked files. Describe
formats by observed structure only. Internal RE notes live in `docs/` (gitignored). Before
committing, sweep for provenance terms and confirm clean.

## Layout
- `src/heat_replay/` — the library:
  - `container.py` — header + plaintext islands → `Container`
  - `schema.py` — the embedded protocol schema (recursive-descent parser) → `Protocol`
  - `stream.py` — the record stream (`walk`/`parse_stream`) → `StreamWalk`, plus
    `events`/`baselines`/`property_deltas`/`referenced_assets`
  - `bitstream.py` — `ReadStream` (LSB-first bit reader) + primitives
  - `wiretypes.py` — `WireType` enum, `classify()`, bit-level `decode()` primitives
  - `model.py` — shared dataclasses (`Container`, `Protocol`, `ClassDef`, `SchemaField`)
  - `events.py`, `summary.py`, `cli.py`, `__init__.py` (the `parse`/`Replay` facade)
- `examples/` — usage examples and standalone analysis probes (stdlib + heat_replay only,
  deterministic output)
- `tests/` — pytest; fixture replays are **not** redistributed (see `tests/conftest.py`),
  so fixture-dependent tests skip when `replays/*.replay` are absent
- `docs/` — internal notes (gitignored)

## Conventions
- stdlib only; no third-party deps, no numpy.
- Analysis probes follow a measure-first discipline: every recovery tool ships a synthetic
  round-trip unit test proving the logic before it is trusted on real data, and reports a
  clear verdict (including negative/UNRESOLVED) rather than forcing a result.
- Run the suite with `python -m pytest`.

## Decode status
Fully decoded and exposed: container/header, embedded schema (per-field wire types,
build-stable), the complete record stream (100% byte coverage), RNG seed, entity spawns,
asset inventory, match metadata/result, roster, typed events, and the per-field wire-type map.

Framed but not typed: the per-entity replication values (property journals). They are exposed
as raw `(frame, handle, value)` deltas via `property_deltas()`. Typed value decoding is not
implemented — the encoding is a bit-level presence mask + bit-packed fields whose per-type
quantization constants are not present in the replay, and there is no stable handle→class key.
Multiple offline reconstruction approaches have been investigated and found insufficient; the
detailed record is in `docs/` (gitignored).
