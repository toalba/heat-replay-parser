# heat-replay-parser

A standalone, dependency-free Python library + CLI for parsing **World of Tanks: HEAT** `.replay`
files. The full record stream is parsed to **100% byte coverage** on every sample.

## Install

```bash
pip install -e .
```

## Use as a library

```python
import heat_replay

r = heat_replay.parse("match.replay")        # or heat_replay.parse_bytes(raw_bytes)

r.map_name, r.game_mode, r.result            # "02_vietnam", "control", "Win"
r.recorder, r.seed                           # player handle, RNG seed
r.clean                                      # True = stream parsed to exact EOF

r.summary()                                  # dict: metadata + roster + frontmen + event counts
r.roster()                                   # [{"nation": "russian", "vehicle": "r01_t_62a"}, ...]
r.battle_result()                            # {"m_endGameType": "Win", ...}
r.assets()                                   # all referenced asset paths (tanks/abilities/effects)

for frame, name, fields in r.decoded_events():   # typed events
    ...   # PlayerInput / ClientShoot / PlayerZoom / MainSeed / BattleResult

r.events(); r.baselines(); r.records         # lower-level: typed records
r.property_deltas()                          # replication deltas: (frame, handle, raw value)
r.field_types()                              # per-field wire types (build-stable across replays)
r.protocol                                   # the embedded protocol schema (263 classes / 712 fields)

# moving trajectories from the per-frame channel — the schema supplies the per-component layout
# (property count + wire order + types); the one piece not in the replay is which schema class each
# numeric component id denotes (build-specific), supplied as {component_id: class_name}:
counts, types = r.schema_replication_layout(component_classes)
r.replication_trajectories(counts, field_types=types)       # {slot: [(x, y, z), ...]} per occupancy slot
r.replication_coherent_tracks(counts, field_types=types)    # [[(x, y, z), ...]] clean, plottable segments
```

The schema names a **wire type** for every replicated field (`CPlainFloat32`, `CFixedVec3`,
`CBounded32`, …). `field_types()` surfaces them, and `heat_replay.wiretypes` provides a
`WireType` enum, `classify()`, and bit-level `decode()` primitives for the self-describing
types (plain floats/vec3/ints/bools). See [What's decoded](#whats-decoded) for the per-codec status.

`parse()` returns a fully-structured `Replay` — consumers never touch raw bytes for the container,
schema, record stream, events, roster, or summary.

## CLI

```
heat-replay info    <file> [--json]              header: map/mode/build/player/result
heat-replay schema  <file> [--dump OUT] [--find NAME] [--stats]
heat-replay stream  <file> [--json] [--assets]   record stream: events/baselines/seed
heat-replay summary <file> [--json]              high-level match summary
```

## What's decoded

- **Fully**: container/header, embedded protocol schema (263 classes / 712 fields), the complete
  record stream (100% coverage), RNG seed, entity spawns + prefabs, asset inventory, match
  metadata & result, roster (vehicle types + frontmen), event timeline.
- **Field types**: the schema's wire type for every replicated field is parsed and exposed via
  `field_types()` (build-stable across replays). The per-component property count and wire order
  come from the same schema, so the full per-component layout is available via
  `schema_replication_layout()` given a `{component_id: class_name}` map.
- **Per-frame replication / trajectories** (`heat_replay.replication` + `wire_value`): the
  self-delimiting body walk and the value codecs for fixed primitives, packed scalars/vectors,
  packed quaternions, entity references, length-prefixed strings, and length-prefixed arrays /
  byte stores (absolute and delta forms) are implemented and round-trip tested. The moving world
  position decodes to real map coordinates. Decoded positions are keyed by the packet's recycled
  occupancy **slot** (a reliable per-entity identity); `replication_trajectories()` assembles raw
  per-slot tracks and `replication_coherent_tracks()` splits them into physically-coherent,
  directly-plottable segments (`examples/plot_coherent_tracks.py` renders an SVG).
- **Structurally** (correct framing, raw values exposed): the `PlayerInput`/`ClientShoot` event
  fields (values correct, some field meanings unconfirmed).
- **Remaining**: a few codecs still halt a packet's walk where present — nested replication
  sub-schemes, name-pool enumerations whose bit width is build-specific, and per-archetype variant
  arrays. Full per-frame density across every entity additionally needs the stateful full-sync
  framing. Trajectory coverage scales directly as these are added; raw deltas for any field are
  always available via `property_deltas()`.

## Tests

```bash
python -m pytest
```

The full suite parses real `.replay` files, which are **not** redistributed here.
Drop your own under `replays/` (matching the names in `tests/conftest.py`) to exercise
them — tests that need a sample skip automatically when it's absent.

## License

MIT — see [LICENSE](LICENSE).
