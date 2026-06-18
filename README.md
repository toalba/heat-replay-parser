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
r.protocol                                   # the embedded protocol schema (189 classes / 667 fields)
```

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

- **Fully**: container/header, embedded protocol schema (189 classes / 667 fields), the complete
  record stream (100% coverage), RNG seed, entity spawns + prefabs, asset inventory, match
  metadata & result, roster (vehicle types + frontmen), event timeline.
- **Structurally** (correct framing, raw values exposed): replication property deltas, and the
  `PlayerInput`/`ClientShoot` event fields (values correct, some field meanings unconfirmed).
- **Not implemented**: typed decoding of the quantized replication values (e.g. positions/health).
  Those are exposed as raw deltas via `property_deltas()`.

## Tests

```bash
python -m pytest
```

The full suite parses real `.replay` files, which are **not** redistributed here.
Drop your own under `replays/` (matching the names in `tests/conftest.py`) to exercise
them — tests that need a sample skip automatically when it's absent.

## License

MIT — see [LICENSE](LICENSE).
