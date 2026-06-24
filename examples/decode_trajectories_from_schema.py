#!/usr/bin/env python3
"""Decode moving-entity trajectories using the layout the replay's own schema provides.

The per-frame replication walk needs, per component, its property count and each property's wire
type. Both are carried in the embedded schema — read directly from ``Replay.protocol`` — given one
mapping the walk cannot get from the replay alone: which schema class each numeric component id
denotes. That mapping is build-specific; supply it as JSON ``{component_id: class_name}``. This
example looks for one at ``docs/corex_nid2class_full.json`` (not redistributed) and degrades to a
clear message if it is absent.

With the schema layout wired in, every fixed-width, packed-scalar, entity-reference and string
field is consumed by its codec, so the walk traverses far more of each packet than property counts
alone — lifting both the number of decoded positions and cross-packet identity resolution. Fields
whose codec is build-specific or not yet modelled (length-delimited vectors, nested replication,
some name-pool enumerations) still halt a packet's remaining walk; coverage scales as those are
added.

Run: ``python3 examples/decode_trajectories_from_schema.py [replay.replay]``
"""
from __future__ import annotations

import json
import os
import statistics
import sys

from heat_replay import parse
from heat_replay.replication import coherent_tracks, integrate_trajectories

# Bit widths for enumeration / name-pool wire types (observed; build-specific). Types absent here
# whose codec is unknown will halt that packet's walk.
ENUM_WIDTHS = {
    "cw::CaptureStatus": 8,
    "cw::DeviceStateCompressor": 6,
    "cw::FinishReason": 8,
    "cw::battle_stats::CStatsData": 14,
    "cw::battle_stats::CStatsPool": 10,
}

POSITION_COMPONENT = 14  # the moving world-transform component


def _find_map() -> dict | None:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "docs", "corex_nid2class_full.json")
    if not os.path.exists(path):
        return None
    raw = json.load(open(path))
    return {int(k): v for k, v in raw.items()}


def _find_inferred() -> dict:
    """Optional per-component field layouts for components that carry no schema class (so the
    schema cannot supply their property list). Build-specific external input, keyed
    ``{component_id: [wire_type, ...]}`` in wire order. Returns ``{}`` when absent."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "docs", "corex_inferred_unmapped_nids.json")
    if not os.path.exists(path):
        return {}
    raw = json.load(open(path))
    return {int(k): v for k, v in raw.items() if not k.startswith("_")}


def main(argv: list[str]) -> int:
    replay = argv[1] if len(argv) > 1 else None
    if replay is None:
        here = os.path.dirname(os.path.abspath(__file__))
        cand = [os.path.join(here, "..", "replays", f)
                for f in sorted(os.listdir(os.path.join(here, "..", "replays")))] \
            if os.path.isdir(os.path.join(here, "..", "replays")) else []
        cand = [c for c in cand if c.endswith(".replay")]
        if not cand:
            print("usage: decode_trajectories_from_schema.py <replay.replay>")
            return 2
        replay = cand[0]

    comp_classes = _find_map()
    if comp_classes is None:
        print("No component-id -> class map found (docs/corex_nid2class_full.json).")
        print("This mapping is build-specific and not carried in the replay; supply it to decode.")
        return 1

    rep = parse(replay)
    counts, types = rep.schema_replication_layout(comp_classes)
    print(f"replay: {os.path.basename(replay)}")
    print(f"schema layout: {len(counts)} components, {len(types)} typed properties")

    # Merge any supplied field layouts for components that carry no schema class (they would
    # otherwise halt every packet that touches them, truncating cross-packet identity).
    inferred = _find_inferred()
    for nid, fields in inferred.items():
        counts[nid] = len(fields)
        for i, ft in enumerate(fields):
            types[(nid, i)] = ft
    if inferred:
        print(f"merged field layouts for {len(inferred)} unmapped components")

    # Walk the per-frame channel once; derive both the raw per-slot tracks and the coherent
    # segments from that single pass.
    positions = rep.replication_positions(
        counts, position_component=POSITION_COMPONENT,
        field_types=types, enum_widths=ENUM_WIDTHS,
    )

    tracks = integrate_trajectories(positions)
    points = sum(len(v) for v in tracks.values())
    multi = sum(1 for v in tracks.values() if len(v) >= 3)
    print(f"raw per-id tracks: {len(tracks)} ids, {points} position points "
          f"({multi} with >= 3 points)")

    # Coherent, directly-plottable tracks: positions key on the reliable occupancy slot, and each
    # recycled slot is split into physically-plausible segments (no >150 m single-tick jumps).
    segs = coherent_tracks(positions)
    seg_pts = sum(len(s) for s in segs)
    steps = [sum((a[k] - b[k]) ** 2 for k in range(3)) ** 0.5
             for s in segs for a, b in zip(s, s[1:])]
    med = statistics.median(steps) if steps else 0.0
    print(f"coherent tracks: {len(segs)} segments, {seg_pts} points, "
          f"step median {med:.1f} m, longest {max((len(s) for s in segs), default=0)} points")
    print("Coherent-track count/length scale with codec coverage (cleaner walks -> better identity);"
          " unmodelled per-build codecs (Variant/name-pool enums/nested) still cap it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
