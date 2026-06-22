#!/usr/bin/env python3
"""Dump a parsed replay to a single JSON document — everything heat_replay decodes.

Usage:
  python examples/replay_to_json.py <replay> [--out FILE] [--raw]

Without --raw: metadata, roster, summary, battle result, decoded event timeline, identified
objects (with spawn positions), assets, the per-field wire-type map, and stream counts.
With --raw: also the full record stream and raw property-deltas (large; bytes shown as hex).
stdlib + heat_replay only.
"""
from __future__ import annotations
import argparse, dataclasses, json, sys

import heat_replay


def _v(x):
    """Replay facade mixes methods and properties — call if callable."""
    return x() if callable(x) else x


def _default(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    if isinstance(o, tuple):
        return list(o)
    return str(o)


def export(r, include_raw: bool = False) -> dict:
    out = {
        "meta": {k: _v(getattr(r, k)) for k in
                 ("path", "map_name", "game_mode", "build", "commit", "result", "recorder", "seed")},
        "players": _v(r.players),
        "roster": _v(r.roster),
        "frontmen": _v(r.frontmen),
        "summary": _v(r.summary),
        "battle_result": _v(r.battle_result),
        "event_types": _v(r.event_types),
        "decoded_events": [{"frame": f, "event": n, "fields": fields}
                           for (f, n, fields) in _v(r.decoded_events)],
        "objects": _v(r.objects),                 # dataclasses -> _default
        "assets": _v(r.assets),
        "field_types": _v(r.field_types),
        "counts": {
            "records": len(_v(r.records)),
            "baselines": len(_v(r.baselines)),
            "events": len(_v(r.events)),
            "property_deltas": len(_v(r.property_deltas)),
        },
    }
    if include_raw:
        out["records"] = _v(r.records)
        out["property_deltas"] = _v(r.property_deltas)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("replay")
    ap.add_argument("--out")
    ap.add_argument("--raw", action="store_true",
                    help="also dump the full record stream + raw property-deltas (large)")
    args = ap.parse_args(argv)
    data = export(heat_replay.parse(args.replay), include_raw=args.raw)
    text = json.dumps(data, indent=2, default=_default)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out} ({len(text):,} bytes); "
              f"counts={data['counts']} objects={len(data['objects'])} "
              f"decoded_events={len(data['decoded_events'])}")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
