#!/usr/bin/env python3
"""Example consumer of the heat_replay library: dump replication property deltas as JSON.

Usage:
    python examples/dump_property_deltas.py <replay.path> [--out deltas.json] [--limit N]

Shows how a downstream app uses the library without touching any bytes itself: call
``heat_replay.parse`` and read the structured ``Replay`` object. Property deltas are the
replication updates ``(frame, handle, raw value)``; the raw value is emitted as a hex string
(decoding it into typed data is not implemented).
"""

from __future__ import annotations

import argparse
import json
import struct
import sys

import heat_replay


def _decode_value(v: bytes) -> dict:
    """Classify a delta value.

    - ``noop``  — the empty ``01 <handle> 00`` delta.
    - ``plain`` — a byte-aligned value with a plain float field present.
    - ``raw``   — quantized / bit-packed; decoding into typed data is not implemented.

    A concrete float value is intentionally not emitted: the per-record header is variable-size,
    so a fixed-offset read would misalign on some records.
    """
    if len(v) == 1:
        return {"kind": "noop"}
    if len(v) >= 7:
        f = struct.unpack_from("<f", v, 3)[0]
        if f == f and abs(f) < 1e6 and (f == 0.0 or abs(f) > 1e-6):
            return {"kind": "plain"}
    return {"kind": "raw"}


def deltas_to_dict(replay: heat_replay.Replay, limit: int | None = None) -> dict:
    deltas = replay.property_deltas()
    rows = [
        {
            "frame": d.frame_id,
            "handle": d.handle,
            "len": len(d.value),
            "value": d.value.hex(),
            "decoded": _decode_value(d.value),
        }
        for d in (deltas[:limit] if limit is not None else deltas)
    ]
    return {
        "replay": replay.path,
        "map": replay.map_name,
        "mode": replay.game_mode,
        "result": replay.result,
        "seed": replay.seed,
        "frame_count": max((r.frame_id for r in replay.records), default=0),
        "delta_count": len(deltas),
        "deltas_emitted": len(rows),
        "note": (
            "value=raw hex. decoded.kind: noop (empty) | plain (byte-aligned, plain float field "
            "present; concrete value not emitted as the per-record header is variable-size) | "
            "raw (quantized/bit-packed; typed decoding not implemented)."
        ),
        "deltas": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("replay")
    ap.add_argument("--out", help="write full JSON here (default: stdout)")
    ap.add_argument("--limit", type=int, default=None, help="only emit the first N deltas")
    args = ap.parse_args(argv)

    replay = heat_replay.parse(args.replay)  # the entire library surface in one call
    data = deltas_to_dict(replay, args.limit)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        print(f"wrote {data['deltas_emitted']}/{data['delta_count']} deltas to {args.out}")
    else:
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
