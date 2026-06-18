"""Command-line interface for heat_replay.

Subcommands:
  info    <file> [--json]                  header + plaintext (map/mode/build/player/win-loss)
  schema  <file> [--dump OUT] [--find NAME] [--stats]
                                           parse the embedded protocol schema
  stream  <file> [--json] [--assets]       parse the record stream (events/baselines/seed)
  summary <file> [--json]                  high-level match summary (roster/result/timeline)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

import collections

from heat_replay.container import read
from heat_replay.schema import dump_schema, parse_schema
from heat_replay.stream import baselines, events, referenced_assets, walk


def _cmd_info(args: argparse.Namespace) -> int:
    c = read(args.file)
    if args.json:
        d = {k: v for k, v in asdict(c).items() if k != "schema_text"}
        d["schema_text_len"] = len(c.schema_text)
        json.dump(d, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if c.is_dead:
        print(f"{c.path}\n  DEAD (null-filled / not a CR08 replay)")
        return 0
    print(c.path)
    print(f"  map / mode   : {c.map_name} / {c.game_mode}")
    print(f"  build        : {c.build}  ({c.branch})")
    print(f"  commit       : {c.commit}")
    print(f"  result       : {c.end_game_type}")
    print(f"  players       : {', '.join(c.players) or '(none found)'}")
    if c.local_player:
        print(f"  local player : {c.local_player}")
    print(f"  schema       : {len(c.schema_text)} B @ 0x{c.schema_start:x}")
    print(f"  stream start : 0x{c.stream_start:x}" if c.stream_start else "  stream start : ?")
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    c = read(args.file)
    if c.is_dead:
        print("dead file — no schema", file=sys.stderr)
        return 1
    proto = parse_schema(c.schema_text, c.commit or "")

    if args.dump:
        dump_schema(proto, args.dump)
        print(f"wrote {args.dump}")

    if args.find:
        needle = args.find
        cdef = proto.classes_by_name.get(needle)
        if cdef:
            print(f"{cdef.name} = 0x{cdef.id:02X}  ({len(cdef.fields)} fields)")
            for f in cdef.fields:
                t = f" : {f.type}" if f.type else ""
                print(f"    {f.name} = 0x{f.id:02X}{t}")
        elif needle in proto.flat_index:
            print(f"{needle} = 0x{proto.flat_index[needle]:02X}  (not a class — field/leaf)")
        else:
            hits = [n for n in proto.flat_index if needle.lower() in n.lower()]
            print(f"no exact match; {len(hits)} substring hits:")
            for n in sorted(hits)[:40]:
                print(f"    {n} = 0x{proto.flat_index[n]:02X}")
        return 0

    # default / --stats
    print(f"build        : {proto.build_commit}")
    print(f"classes      : {len(proto.classes_by_name)}")
    print(f"flat names   : {len(proto.flat_index)}")
    if args.stats:
        top = sorted(proto.classes_by_name.values(), key=lambda d: len(d.fields), reverse=True)
        print("widest classes:")
        for cdef in top[:15]:
            print(f"    {cdef.name:48} {len(cdef.fields)} fields")
    return 0


def _cmd_stream(args: argparse.Namespace) -> int:
    w = walk(args.file)
    tags = collections.Counter(r.tag for r in w.records)
    if args.json:
        out = {
            "records": len(w.records),
            "clean_eof": w.clean_eof,
            "seed": w.seed,
            "event_types": w.event_types,
            "tag_counts": dict(tags),
            "baselines": len(baselines(w)),
            "assets": len(referenced_assets(w)),
        }
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    print(args.file)
    print(f"  records      : {len(w.records)}  (clean EOF: {w.clean_eof})")
    print(f"  RNG seed     : 0x{w.seed:08x}" if w.seed else "  RNG seed     : ?")
    print(f"  event types  : {w.event_types}")
    print(f"  tag counts   : {dict(sorted(tags.items()))}")
    print(f"  baselines    : {len(baselines(w))} entity spawns")
    assets = referenced_assets(w)
    print(f"  assets       : {len(assets)} distinct paths")
    if args.assets:
        for a in assets:
            print(f"    {a}")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    import heat_replay

    s = heat_replay.parse(args.file).summary()
    if args.json:
        json.dump(s, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    commit = (s["commit"] or "(none)")[:12]
    seed = f"0x{s['seed']:08x}" if s["seed"] is not None else "?"
    print(f"{s['map']} / {s['mode']}   {s['result']}   build {commit}")
    print(f"  recorder : {s['recorder']}   seed {seed}")
    print(f"  frames   : {s['max_frame_id']}   records {s['record_count']}")
    print(f"  vehicles : {', '.join(s['vehicles'])}")
    print(f"  frontmen : {', '.join(s['frontmen'])}")
    print(f"  events   : {s['event_counts']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="heat-replay", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="header + plaintext metadata")
    p_info.add_argument("file")
    p_info.add_argument("--json", action="store_true", help="emit JSON")
    p_info.set_defaults(func=_cmd_info)

    p_schema = sub.add_parser("schema", help="parse the embedded protocol schema")
    p_schema.add_argument("file")
    p_schema.add_argument("--dump", metavar="OUT", help="write schema JSON to OUT")
    p_schema.add_argument("--find", metavar="NAME", help="look up a class/field by name")
    p_schema.add_argument("--stats", action="store_true", help="show class stats")
    p_schema.set_defaults(func=_cmd_schema)

    p_stream = sub.add_parser("stream", help="parse the record stream (events, baselines, seed)")
    p_stream.add_argument("file")
    p_stream.add_argument("--json", action="store_true", help="emit JSON")
    p_stream.add_argument("--assets", action="store_true", help="list all referenced asset paths")
    p_stream.set_defaults(func=_cmd_stream)

    p_sum = sub.add_parser("summary", help="high-level match summary (roster/result/timeline)")
    p_sum.add_argument("file")
    p_sum.add_argument("--json", action="store_true", help="emit JSON")
    p_sum.set_defaults(func=_cmd_summary)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
