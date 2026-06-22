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

Framed, and now partially typed: the per-entity replication values (property journals) are
exposed as raw `(frame, handle, value)` deltas via `property_deltas()`. Typed value decoding is
**in progress** (no longer blocked). The wire format is now understood:

- The bit stream is **LSB-first**. A journal frame is `frameId:u32, flag:1, two component
  unique-sets, componentCount:u32`, then per component `networkId:7bits` followed by **every
  property in field order**. There is **no separate per-property presence bitmask** — earlier
  offline analysis mis-read the concatenated per-property selector bits as a mask. "No change /
  zero" is encoded inside each property's selector.
- Float fields are **variable-width packed scalars**: a selector picks a bit-width tier, then a
  stripped mantissa + sign rebuild an IEEE-754 value (see `heat_replay.packed_scalar`,
  round-trip unit-tested). A plain float is full precision packed at a bit offset, not byte-aligned.
- Validated end-to-end on real data: a static/initial **world position** (a plain 3×float32 field,
  not byte-aligned) decodes from tag-6 baselines to sane world coordinates (373/373 across the four
  samples; a static map entity yields the identical coordinate in every replay). Position is split
  by purpose across components: the static/initial position is the plain-float triple; the **moving
  per-frame world transform** is a separate component carrying a packed-scalar `CFixedVec3` position
  + packed-quaternion rotation (codec implemented; per-field tier/scale still being calibrated
  against continuity); a third "local transform" is parent-relative (often origin). (An earlier note
  attributed the validated triple to the rigid-body transform — it is actually the plain-float
  storage component; the rigid-body transform is the packed one.)

Object identification + positions: `Replay.objects()` returns every replicated-entity lifetime
(identified by prefab → category: vehicle / player / projectile / ability / …), segmented across
recycled entity slots, with decoded positions where a plain-float position field sits at a baseline
head. `Replay.moving_objects()` is the position-readable subset; `summary()` carries
`objects_by_category`. Identification is reliable and complete. Position reading is currently
limited to baseline-head plain-float positions (correct for static/map objects; dense moving
trajectories require decoding the packed transform through a full per-component walk).

Component table fully typed from the embedded schema: the replicated components are **not**
disjoint from the schema — earlier the schema parser's class detector keyed on a name-suffix
heuristic and silently dropped every single-word component (Mana, Driver, Shoot, …) and every
zero-field marker. `schema.py` now detects classes **structurally** (a name leaf carrying the
`default` sentinel or a parseable field list is a class), lifting the parse from 189 to 263
classes. Field types are read positionally (the second token of each field def), so arrays
(`vector<…>`), namespaced enums (`cw::…`) and nested sub-components are captured verbatim — **592
of 712 fields now carry a type** (was ~336); the residual 120 are genuinely type-less
nested-replication entries in the schema. `wiretypes.field_wire_type()` maps **every** replicated field (712/712, 0 unknown) to a concrete
category: fixed primitives (`CBool`(1b), `CUint8/16/32/64`, `CInt8/32/64`, `CPlainFloat32`(32b),
`CPlainVec3`(96b), `CPlayerId`), quantized packed scalars (`CFixed32`/`CFixedVec3`/`CBounded32`/
`CFixedQuat`), variable-length (`CEntityNetworkId`/`CStdString`), enum/name pools (`*Compressor`,
`cw::*` scalars), composites/arrays (nested → recurse), and `NESTED_REPLICATION` for the
schema-typeless fields (a nested replication sub-scheme with no inlined type). Field categorisation
is exhaustive — the field map is fully wired to the decode machinery (`Replay.field_types()`).

Moving-entity tracking over time: the per-tick replication channel keys every packet by a
recycled entity *slot* (a `u16` in the packet header); spawn packets in that channel embed a
prefab path, so each per-frame update run links back to the prefab that opened its slot. This
yields a **prefab-identified, time-resolved track** (identity + lifetime: frame span + update
cadence) for every replicated entity across the whole match — see
`examples/track_tag4_entities.py`. Identity and lifetime are fully determined by the replay;
world positions are not (next paragraph).

Now framed and validated — the per-frame (tag-4) channel: each packet carries a fixed header (a
`u16` sequence id that increments and **wraps**, a tick counter, and a reference field) followed by
a **self-delimiting per-entity message stream** (a reference-relative entity-id codec → message
type → full-sync/delta body). This framing is **solved** — proven by a 0-error self-delimiting parse
over ~100k packets across the samples, with deterministic alignment on spawn messages. Because the
sequence id is a `u16` that wraps and reuses values, cross-packet reference resolution must take the
**most-recent prior** occurrence (encoded as a tested requirement).

Still open — a dense per-frame *world-space* trajectory now reduces to two bounded steps on top of
the framing: (1) per-entity **value** decode (resolving the entity-id codec against a wrap-aware
sequence-id map) for cross-packet tracking, then (2) the **per-component property value codecs** to
consume each message body. Quantized positional fields additionally need their per-field
quantization constants, which are **not carried in the replay file** (external parameter capture).
The self-delimiting packed-scalar path is implemented and the entity track above is the scaffold a
decode attaches to. Detailed record in `docs/` (gitignored).
