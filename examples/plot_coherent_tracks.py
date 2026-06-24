#!/usr/bin/env python3
"""Render the coherent moving-entity trajectories of a replay to a top-down SVG map.

Decodes per-frame positions, assembles physically-coherent track segments
(:meth:`Replay.replication_coherent_tracks`), and draws each as a polyline in the world's x/z
plane. Output is a self-contained SVG (stdlib only, deterministic) you can open in any browser.

Run: ``python3 examples/plot_coherent_tracks.py <replay.replay> [out.svg]``

The component-id -> class map and the optional unmapped-component layouts are build-specific and
read from ``docs/`` (not redistributed); see ``decode_trajectories_from_schema.py``.
"""
from __future__ import annotations

import json
import os
import sys

from heat_replay import parse

POSITION_COMPONENT = 14
ENUM_WIDTHS = {
    "cw::CaptureStatus": 8, "cw::DeviceStateCompressor": 6, "cw::FinishReason": 8,
    "cw::battle_stats::CStatsData": 14, "cw::battle_stats::CStatsPool": 10,
}
_PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0",
            "#f032e6", "#bcf60c", "#fabebe", "#008080", "#9a6324", "#800000",
            "#808000", "#000075", "#e6beff", "#aaffc3", "#ffd8b1", "#a9a9a9"]


def _load(name):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "docs", name)
    return json.load(open(path)) if os.path.exists(path) else None


def _layout(rep):
    raw = _load("corex_nid2class_full.json")
    if raw is None:
        return None
    counts, types = rep.schema_replication_layout({int(k): v for k, v in raw.items()})
    inferred = _load("corex_inferred_unmapped_nids.json") or {}
    for k, fields in inferred.items():
        if k.startswith("_"):
            continue
        nid = int(k)
        counts[nid] = len(fields)
        for i, ft in enumerate(fields):
            types[(nid, i)] = ft
    return counts, types


def to_svg(segs, size=900, pad=30):
    pts = [p for s in segs for p in s]
    if not pts:
        return "<svg xmlns='http://www.w3.org/2000/svg'/>"
    xs = [p[0] for p in pts]
    zs = [p[2] for p in pts]
    minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
    spanx, spanz = max(maxx - minx, 1.0), max(maxz - minz, 1.0)
    scale = (size - 2 * pad) / max(spanx, spanz)

    def proj(p):
        x = pad + (p[0] - minx) * scale
        y = pad + (maxz - p[2]) * scale            # flip z so north is up
        return x, y

    lines = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}' "
             f"viewBox='0 0 {size} {size}'>",
             f"<rect width='{size}' height='{size}' fill='#11151c'/>"]
    for i, s in enumerate(segs):
        col = _PALETTE[i % len(_PALETTE)]
        pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in (proj(p) for p in s))
        lines.append(f"<polyline fill='none' stroke='{col}' stroke-width='2' "
                     f"stroke-opacity='0.85' points='{pts_str}'/>")
        sx, sy = proj(s[0])
        lines.append(f"<circle cx='{sx:.1f}' cy='{sy:.1f}' r='3' fill='{col}'/>")   # start marker
    lines.append(f"<text x='{pad}' y='{size-10}' fill='#8a94a6' font-family='monospace' "
                 f"font-size='13'>{len(segs)} coherent tracks  |  world x/z meters  |  "
                 f"x:[{minx:.0f},{maxx:.0f}] z:[{minz:.0f},{maxz:.0f}]</text>")
    lines.append("</svg>")
    return "\n".join(lines)


def main(argv):
    if len(argv) < 2:
        print("usage: plot_coherent_tracks.py <replay.replay> [out.svg]")
        return 2
    replay = argv[1]
    out = argv[2] if len(argv) > 2 else os.path.splitext(os.path.basename(replay))[0] + "_tracks.svg"
    rep = parse(replay)
    layout = _layout(rep)
    if layout is None:
        print("No component-id -> class map (docs/corex_nid2class_full.json); cannot decode.")
        return 1
    counts, types = layout
    segs = rep.replication_coherent_tracks(
        counts, position_component=POSITION_COMPONENT, field_types=types, enum_widths=ENUM_WIDTHS)
    with open(out, "w") as fh:
        fh.write(to_svg(segs))
    pts = sum(len(s) for s in segs)
    print(f"{os.path.basename(replay)}: {len(segs)} coherent tracks, {pts} points -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
