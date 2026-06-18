#!/usr/bin/env python3
"""EXPERIMENTAL: header-tolerant decode + classification of `plain` delta properties.

The tag-8 delta stream splits into noop / plain (byte-aligned plain float) / raw (quantized).
This tool extracts the float trajectory of each `plain` handle and classifies it. It is HEURISTIC:

- Per-record headers vary in size, so per handle we pick the byte offset whose f32 reads are most
  consistently physical (finite, |v|<1e4); `purity` < 1.0 = some records misread by that fixed
  offset. Treat low-purity handles with suspicion.
- Handle numbers are per-replay — classifications do not transfer across matches.
- World coordinates are not here (they are quantized/`raw`). The `large` class tops out ~±26
  (velocities / offsets / distances), not map-scale.

So this yields scalar/angle properties — a partial decode — but not positions.
"""

from __future__ import annotations

import argparse
import collections
import json
import struct
import sys

import heat_replay


def _f32(b: bytes, off: int) -> float:
    return struct.unpack_from("<f", b, off)[0] if len(b) >= off + 4 else float("nan")


def classify_handle(records: list[tuple[int, bytes]]) -> dict | None:
    """Find the best float offset for a handle and classify its trajectory."""
    best = None
    for off in range(0, 7):
        vals = [(_f32(v, off)) for _, v in records if len(v) >= off + 4]
        good = [x for x in vals if x == x and abs(x) < 1e4 and (x == 0.0 or abs(x) > 1e-7)]
        if len(good) < max(4, 0.5 * len(vals)):
            continue
        score = len(good) / max(1, len(vals))
        if best is None or score > best[0]:
            best = (score, off, good)
    if not best:
        return None
    score, off, vals = best
    amax = max(abs(x) for x in vals)
    mono = all(a <= b for a, b in zip(vals, vals[1:])) or all(a >= b for a, b in zip(vals, vals[1:]))
    # exponent-variation test: a real float moves its IEEE-754 exponent across samples; a
    # misread-bytes artifact (the ±2.2 cluster) keeps the exponent pinned.
    exps = {(struct.unpack("<I", struct.pack("<f", x))[0] >> 23) & 0xFF for x in vals if x != 0.0}
    exp_pinned = len(exps) <= 1
    if amax == 0.0:
        kind = "zero/constant"
    elif mono and amax > 100:
        kind = "monotonic(counter/coord)"
    elif amax <= 3.1416:
        # split: pinned exponent => likely the quantized-byte artifact, not a genuine angle
        kind = "pinned(artifact-suspect)" if exp_pinned else "angle(rad)"
    elif amax <= 1.001:
        kind = "unit"
    elif amax < 20:
        kind = "scalar_small"
    else:
        kind = "scalar_large"
    return {
        "offset": off,
        "samples": len(vals),
        "purity": round(score, 2),
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
        "exp_distinct": len(exps),  # >1 = float exponent moves (real); 1 = pinned (artifact-suspect)
        "kind": kind,
    }


def build(replay: heat_replay.Replay) -> dict:
    by_handle: dict[int, list] = collections.defaultdict(list)
    for d in replay.property_deltas():
        if len(d.value) > 1:  # skip noops
            by_handle[d.handle].append((d.frame_id, d.value))
    out = {}
    for h, recs in by_handle.items():
        if any(len(v) > 7 for _, v in recs):
            c = classify_handle(recs)
            if c:
                out[h] = c
    kinds = collections.Counter(c["kind"] for c in out.values())
    return {
        "replay": replay.path,
        "map": replay.map_name,
        "mode": replay.game_mode,
        "warning": "HEURISTIC per-replay classification; positions not included (quantized).",
        "classified": len(out),
        "kind_counts": dict(kinds),
        "handles": {str(h): c for h, c in sorted(out.items())},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("replay")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    data = build(heat_replay.parse(args.replay))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        print(f"classified {data['classified']} handles -> {args.out}  {data['kind_counts']}")
    else:
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
