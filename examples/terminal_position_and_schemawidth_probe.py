"""TERMINAL offline probe: the two unexploited hypotheses from the WoWS-parser comparison.

LEG A -- dedicated clear-byte position packet. The WoWS parser (same engine family) carries
position as a byte-aligned packet, not a bit-packed property delta. Scan every byte-aligned
source (packet-list packets, reflected-event blobs by name, tag-9 blobs) for an f32 triple, and
use frame-to-frame CONTINUITY (a moving entity's position is smooth) -- not just a range band --
as the discriminator.

LEG B -- schema-driven mask width. In the predecessor engine family, bit-packed index widths
are computed as ceil(log2(count)) from the schema, never inferred. Maybe the flat-width search
failed only
because it scanned arbitrary W. For each schema-derived W (ceil(log2(n)) and n), ask
existentially: does ANY handle's records, solved at that fixed W, surface a plain-field anchor
(width 32 / 96) that the free-W search could not?

This is the LAST statistical task. The deliverable ends by selecting one of two NON-statistical
forks; it does not propose a fifth statistical fallback.

Usage::

    python examples/terminal_position_and_schemawidth_probe.py [--coord-max 2000]
        [--bit-order {lsb,msb}] [--leg {a,b,both}]

stdlib + heat_replay only. Deterministic output. Reuses the proven solver from
recover_mask_widths.py (does not re-derive it).
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import math
import os
import statistics
import struct

import heat_replay
from heat_replay.stream import property_deltas, walk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLAYS_DIR = os.path.join(ROOT, "replays")

# Reuse the proven least-squares solver + bit helper from the prior task.
_SPEC = importlib.util.spec_from_file_location(
    "recover_mask_widths", os.path.join(os.path.dirname(__file__), "recover_mask_widths.py")
)
rmw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rmw)

# Leg A tuning.
SCAN_BYTE_CAP = 160        # only scan position offsets within the first N bytes (bounded; logged)
ID_OFFSETS = (0, 4, 8, 12)  # candidate entity-id columns live near the front
MAX_PACKETS_SCAN = 4000    # subsample cap per source for the offset search
INBAND_MIN = 0.80          # fraction of triples finite + within band to consider an offset
STEP_RATIO = 0.05          # median per-frame step must be < coord_max * this to be "smooth"
# Leg B tuning.
ANCHOR_F32 = (29, 35)      # solved width window counted as a 32-bit plain float
ANCHOR_VEC3 = (93, 99)     # ... as a 96-bit plain vec3
# A solved width is only a real anchor if it comes from a CLEAN fit. A rank-starved solver lands
# widths near 32 by accident, so without this gate "anchor appearance" is pure noise (it rises
# with W and is no higher at schema-derived W than at arbitrary W).
ANCHOR_RESID_MAX = 2.0     # RMS residual (bits) the fit must beat for an anchor to count


# ----------------------------------------------------------------- Leg A


def _f32_triple(buf: bytes, off: int, order: str):
    fmt = "<3f" if order == "lsb" else ">3f"
    if off + 12 > len(buf):
        return None
    t = struct.unpack_from(fmt, buf, off)
    return t if all(math.isfinite(c) for c in t) else None


def _u32(buf: bytes, off: int, order: str) -> int | None:
    fmt = "<I" if order == "lsb" else ">I"
    if off + 4 > len(buf):
        return None
    return struct.unpack_from(fmt, buf, off)[0]


def scan_source(records, coord_max, order):
    """records = [(frame, bytes)]. Find the best (id_off, pos_off) whose triples are in-band and
    continuous per recurring id. Returns the best candidate dict or None."""
    if not records:
        return None
    sample = records[: MAX_PACKETS_SCAN]
    min_len = min(len(b) for _, b in sample)
    pos_offsets = range(0, min(min_len - 11, SCAN_BYTE_CAP), 4)

    # candidate id columns: a front u32 with FEW distinct values (recurs => groups entities)
    id_cands = []
    for ido in ID_OFFSETS:
        if ido + 4 > min_len:
            continue
        vals = [_u32(b, ido, order) for _, b in sample]
        distinct = len(set(vals))
        if 1 < distinct <= max(2, len(sample) // 5):  # recurs across many packets
            id_cands.append(ido)
    id_cands = id_cands or [None]  # also allow a single ordered stream (no id)

    best = None
    for ido in id_cands:
        for po in pos_offsets:
            triples = []
            for fr, b in sample:
                t = _f32_triple(b, po, order)
                if t is None:
                    continue
                gid = _u32(b, ido, order) if ido is not None else 0
                triples.append((gid, fr, t))
            if not triples:
                continue
            inband = [
                tr for tr in triples
                if all(abs(c) <= coord_max for c in tr[2]) and any(abs(c) > 1.0 for c in tr[2])
            ]
            inband_frac = len(inband) / len(triples)
            if inband_frac < INBAND_MIN:
                continue
            # continuity: per id, order by frame, median euclidean step
            groups: dict[int, list] = {}
            for gid, fr, t in inband:
                groups.setdefault(gid, []).append((fr, t))
            steps = []
            for seq in groups.values():
                seq.sort()
                for (f0, a), (f1, c) in zip(seq, seq[1:]):
                    steps.append(math.dist(a, c))
            if not steps:
                continue
            med_step = statistics.median(steps)
            cand = {
                "id_offset": ido, "pos_offset": po, "inband_frac": round(inband_frac, 3),
                "median_step": round(med_step, 2), "n_triples": len(triples),
                "n_groups": len(groups),
                "position_like": inband_frac >= INBAND_MIN and med_step < coord_max * STEP_RATIO
                and ido is not None,
            }
            if best is None or (cand["position_like"], -cand["median_step"]) > (
                best["position_like"], -best["median_step"]
            ):
                best = cand
    return best


def leg_a(path, coord_max, order):
    w = walk(path)
    sources: dict[str, list] = {}
    for r in w.records:
        if r.packets:
            for p in r.packets:
                sources.setdefault(f"packetlist_tag{r.tag}", []).append((r.frame_id, p))
        elif r.tag == 0x0A and r.blob is not None:
            sources.setdefault(f"event:{r.event_name}", []).append((r.frame_id, r.blob))
        elif r.tag == 9 and r.blob is not None:
            sources.setdefault("tag9_blob", []).append((r.frame_id, r.blob))
    results = {}
    any_pos = False
    for name, recs in sorted(sources.items()):
        cand = scan_source(recs, coord_max, order)
        if cand:
            results[name] = cand
            any_pos = any_pos or cand["position_like"]
    return {"any_position_like": any_pos, "sources": results, "scan_byte_cap": SCAN_BYTE_CAP}


# ----------------------------------------------------------------- Leg B


def _anchors_at_width(records, W, order):
    """Solve fixed-W mask widths for one handle; return plain-anchor widths from a CLEAN fit.

    An anchor only counts if the fit is clean (RMS residual <= ANCHOR_RESID_MAX); otherwise a
    width near 32 is solver noise from an underdetermined system, not real structure.
    """
    if min(len(v) for v in records) * 8 <= W:
        return []
    A = [[1.0] + [float(rmw.bit_at(v, i, order)) for i in range(W)] for v in records]
    b = [float(len(v) * 8 - W) for v in records]
    x, _ = rmw.solve_lstsq(A, b)
    resid = (
        sum((sum(A[k][j] * x[j] for j in range(len(x))) - b[k]) ** 2 for k in range(len(b)))
        / len(b)
    ) ** 0.5
    if resid > ANCHOR_RESID_MAX:
        return []  # fit is garbage -> any "anchor" is coincidence
    hits = []
    for i, wsol in enumerate(x[1:]):
        if ANCHOR_F32[0] <= wsol <= ANCHOR_F32[1] or ANCHOR_VEC3[0] <= wsol <= ANCHOR_VEC3[1]:
            hits.append({"bit": i, "width": round(wsol, 2)})
    return hits


def leg_b(path, order, min_records=12):
    r = heat_replay.parse(path)
    # per-class typed-field count n (the embedded schema has no client-visible flag, so use
    # total typed fields per class as n -- stated assumption).
    ns = set()
    for cd in r.protocol.classes_by_name.values():
        n = sum(1 for f in cd.fields if f.type)
        if n >= 1:
            ns.add(n)
    candidate_W = set()
    for n in ns:
        candidate_W.add(max(1, (n - 1).bit_length()))  # ceil(log2(n))
        if n <= 48:
            candidate_W.add(n)                          # flat n-bit bitmap
    candidate_W = sorted(w for w in candidate_W if 1 <= w <= 48)

    recs: dict[int, list] = {}
    for d in property_deltas(r.stream):
        if len(d.value) > 1:
            recs.setdefault(d.handle, []).append(d.value)
    handles = [h for h, v in recs.items() if len(v) >= min_records]

    def anchor_handles(W):
        return sum(1 for h in handles if _anchors_at_width(recs[h], W, order))

    per_W = {W: anchor_handles(W) for W in candidate_W}
    schema_hits = sum(per_W.values())

    # NULL CONTROL: anchor base rate at NON-schema widths. If schema Ws do not beat this, any
    # schema-W anchors are coincidence, not a real schema-derived index-width model.
    control_W = [w for w in range(1, 49) if w not in set(candidate_W)]
    control_counts = [anchor_handles(w) for w in control_W]
    control_mean = statistics.mean(control_counts) if control_counts else 0.0
    schema_mean = statistics.mean(per_W.values()) if per_W else 0.0

    # confirmed only if clean-fit anchors actually appear AND schema Ws beat the null base rate
    confirmed = schema_hits > 0 and schema_mean > 2 * max(0.5, control_mean)
    return {
        "schema_field_counts": sorted(ns),
        "candidate_widths": candidate_W,
        "n_handles": len(handles),
        "anchor_handles_per_schema_width": per_W,
        "schema_mean_anchor_handles": round(schema_mean, 2),
        "null_control_mean_anchor_handles": round(control_mean, 2),
        "schema_width_confirmed": confirmed,
        "free_W_baseline": "free-W search (recover_mask_widths) found NO plain anchors at any W",
    }


# ----------------------------------------------------------------- driver


FORK = (
    "FORK (both legs NO): offline single-replay reconstruction is provably insufficient for typed "
    "value decode. The project now chooses, with no further statistical analysis:\n"
    "  (i)  pursue the client gamedata schema-definition equivalent (client resource extraction "
    "-- the dependency the reference parsers in this engine family rely on), OR\n"
    "  (ii) ship the no-constants ceiling: framing (entity/frame/changed-property), reflected "
    "events (seed, battle result, input event types), and any clear-byte packets."
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coord-max", type=float, default=2000.0)
    ap.add_argument("--bit-order", choices=["lsb", "msb"], default="lsb")
    ap.add_argument("--leg", choices=["a", "b", "both"], default="both")
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(REPLAYS_DIR, "*.replay")))
    if not paths:
        print(json.dumps({"error": f"no replays in {REPLAYS_DIR}"}))
        return 1

    a_results = b_results = None
    if args.leg in ("a", "both"):
        a_per = [{"replay": os.path.basename(p), **leg_a(p, args.coord_max, args.bit_order)}
                 for p in paths]
        a_results = {
            "any_position_like": all(r["any_position_like"] for r in a_per) and len(a_per) > 0
            and all(r["any_position_like"] for r in a_per),
            "consistent": len({r["any_position_like"] for r in a_per}) == 1,
            "per_replay": a_per,
        }
    if args.leg in ("b", "both"):
        b_per = [{"replay": os.path.basename(p), **leg_b(p, args.bit_order)} for p in paths]
        b_results = {
            "schema_width_confirmed": all(r["schema_width_confirmed"] for r in b_per),
            "consistent": len({r["schema_width_confirmed"] for r in b_per}) == 1,
            "per_replay": b_per,
        }

    a_pos = bool(a_results and a_results["any_position_like"] and a_results["consistent"])
    b_conf = bool(b_results and b_results["schema_width_confirmed"] and b_results["consistent"])

    if a_pos:
        verdict = "A: POSITION-LIKE FOUND"
        fork = ("NEXT (non-statistical): wire the clear-byte position stream into per-entity state "
                "tracking. The mask never held positions. STOP statistical work.")
    elif b_conf:
        verdict = "B: SCHEMA-WIDTH-CONFIRMED"
        fork = ("NEXT (non-statistical): decode per-class using schema-derived mask widths "
                "(ceil(log2(n))). Mask geometry is arithmetic from the schema. STOP statistical work.")
    else:
        verdict = "BOTH NO"
        fork = FORK

    report = {
        "params": {"coord_max": args.coord_max, "bit_order": args.bit_order, "leg": args.leg},
        "project_verdict": verdict,
        "leg_a": a_results,
        "leg_b": b_results,
        "fork": fork,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"\n=== PROJECT VERDICT: {verdict} ===")
    print(fork)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
