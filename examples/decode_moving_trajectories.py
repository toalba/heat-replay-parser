"""Decode per-entity moving trajectories from the per-frame (tag-4) replication channel.

The framing, entity identity, and position codec are implemented in :mod:`heat_replay.replication`.
The walk needs two build-specific tables that are **not carried in the replay file**: the property
count per component and the bit width of each non-position property value. Supply them from a
runtime capture (a JSON ``{component_id: count}`` and optionally ``{"nid:prop": width}``); this
example loads them from a path given on the command line, or from ``docs/runtime_propcount.json``
if present, and otherwise explains what is missing.

What you get:
* ``replication_positions`` — every decoded moving-transform position as
  ``(entity_id, mode, (x, y, z))`` (mode 1 = absolute add, 0 = delta update);
* ``replication_trajectories`` — those integrated into per-entity tracks.

Coverage note (honest): a position is decoded once its packet is walked up to the moving-transform
component, and an entity id resolves once the prior packet's id-list is complete. Both improve with
the value-width table — without it the walk stops at the first component carrying an unsized value,
so trajectories are sparse. Identity and lifetime alone (no positions) need no tables — see
``track_tag4_entities.py``.

Stdlib + heat_replay only; deterministic output.
"""
from __future__ import annotations

import json
import os
import sys

import heat_replay


def _load_tables(arg_path: str | None):
    path = arg_path or os.path.join(os.path.dirname(__file__), "..", "docs", "runtime_propcount.json")
    if not os.path.exists(path):
        return None, None
    raw = json.load(open(path))
    counts = {int(k): v for k, v in (raw.get("counts", raw)).items() if str(k).lstrip("-").isdigit()}
    widths = {tuple(int(x) for x in k.split(":")): v for k, v in raw.get("widths", {}).items()}
    return counts, (widths or None)


def main(argv):
    if len(argv) < 2:
        print("usage: decode_moving_trajectories.py <replay> [property_counts.json]")
        return 2
    replay_path = argv[1]
    counts, widths = _load_tables(argv[2] if len(argv) > 2 else None)
    if not counts:
        print("No property-count table found. The per-frame value walk needs a runtime-captured")
        print("table ({component_id: property_count}); pass its path as the 2nd argument.")
        print("Without it, only identity+lifetime are available (see track_tag4_entities.py).")
        return 1

    replay = heat_replay.parse(replay_path)
    positions = replay.replication_positions(counts, widths)
    tracks = replay.replication_trajectories(counts, widths)
    multi = {k: v for k, v in tracks.items() if len(v) >= 3}

    print(f"position events:        {len(positions)}")
    print(f"  with resolved entity: {sum(1 for e in positions if e[0] is not None)}")
    print(f"distinct entities:      {len(tracks)}")
    print(f"tracks (>=3 points):    {len(multi)}")
    for eid in sorted(multi, key=lambda k: -len(multi[k]))[:5]:
        pts = multi[eid]
        print(f"  entity {eid}: {len(pts)} points, first {tuple(round(c,1) for c in pts[0])} "
              f"last {tuple(round(c,1) for c in pts[-1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
