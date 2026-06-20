"""Disambiguate the tag-8 delta-record layout using ONLY bit statistics.

Decides between three candidate layouts for the per-entry payload (the bytes after the
``01 + handle:u32`` prefix that :func:`heat_replay.stream.property_deltas` already strips):

  (a) bit-level presence mask : ``[mask bits][changed fields]``
  (b) sequential streaming    : fields appended in fixed order, stop when nothing changed
  (c) per-field tagged stream : ``[field-id][value]...`` in arbitrary order

No value is ever decoded. The discriminators are purely bit-level:

  * bit-prefix containment  -- (a) breaks early; (b)/(c) keep short records as bit-prefixes
  * first-field stability    -- (b) has identical leading bits across a handle's records;
                                (c)/(a) do not

The correct in-byte bit order (LSB-first vs MSB-first) is decided as a side effect: the
one that maximizes containment wins. The verdict and winning order must agree across all
four sample replays (the layout is build-stable) or the result is INCONCLUSIVE.

Usage::

    python examples/disambiguate_delta_layout.py [--tail-tolerance-bits N]

stdlib + heat_replay only. Deterministic output.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics

import heat_replay
from heat_replay.stream import property_deltas

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLAYS_DIR = os.path.join(ROOT, "replays")

# verdict thresholds (per spec)
CONTAIN_HIGH = 0.9
CONTAIN_LOW = 0.5
FF_HIGH_BITS = 16  # "tens+ of identical leading bits" => same first field => (b)
FF_ZERO_BITS = 4   # "near-zero / noisy" => leading bits vary => (c)/(a)
BIT_ORDER_CLOSE = 0.1  # |containment_lsb - containment_msb| below this => order inconclusive


def _bit_lcp(a: bytes, b: bytes, order: str) -> int:
    """Longest common *bit* prefix of two byte strings, under ``order`` (``lsb``/``msb``)."""
    n = min(len(a), len(b))
    bits = 0
    for i in range(n):
        if a[i] == b[i]:
            bits += 8
            continue
        x = a[i] ^ b[i]
        k = 0
        if order == "lsb":  # bit 0 emitted first; leading equal bits = trailing-zero count
            while k < 8 and not (x >> k) & 1:
                k += 1
        else:  # msb: bit 7 emitted first; leading equal bits counted from the top
            while k < 8 and not (x >> (7 - k)) & 1:
                k += 1
        return bits + k
    return bits  # the shorter is a full byte-prefix of the longer


def _common_prefix_bits(records: list[bytes], order: str) -> int:
    """Length (bits) of the prefix shared by ALL records.

    Equals ``min_b LCP(r0, b)`` for any fixed reference ``r0`` in the set (the common
    prefix is a property of the set, independent of the reference).
    """
    if len(records) < 2:
        return len(records[0]) * 8 if records else 0
    ref = records[0]
    return min(_bit_lcp(ref, b, order) for b in records[1:])


def _analyze_order(by_handle: dict[int, list[bytes]], order: str, tail_tol: int) -> dict:
    """Containment fraction + first-field-stability stats for one bit order."""
    contained = 0
    pairs = 0
    ff_bits: list[int] = []
    for handle in sorted(by_handle):
        recs = by_handle[handle]
        # shortest record S, chosen deterministically
        s = min(recs, key=lambda v: (len(v), v))
        s_bits = len(s) * 8
        longer = [v for v in recs if len(v) > len(s)]
        for ell in longer:
            pairs += 1
            if _bit_lcp(s, ell, order) >= s_bits - tail_tol:
                contained += 1
        # first-field stability: common bit prefix across ALL of this handle's records
        ff_bits.append(_common_prefix_bits(sorted(recs), order))
    ff_bits.sort()
    return {
        "containment_fraction": round(contained / pairs, 4) if pairs else None,
        "first_field_bits": {
            "median": round(statistics.median(ff_bits), 1) if ff_bits else None,
            "p10": ff_bits[max(0, int(0.10 * (len(ff_bits) - 1)))] if ff_bits else None,
            "p90": ff_bits[int(0.90 * (len(ff_bits) - 1))] if ff_bits else None,
        },
        "n_handles": len(by_handle),
        "n_pairs": pairs,
    }


def _verdict(containment: float | None, ff_median: float | None) -> str:
    if containment is None or ff_median is None:
        return "INCONCLUSIVE"
    if containment >= CONTAIN_HIGH and ff_median >= FF_HIGH_BITS:
        return "(b) sequential streaming"
    if containment >= CONTAIN_HIGH and ff_median <= FF_ZERO_BITS:
        return "(c) tagged stream"
    if containment <= CONTAIN_LOW:
        return "(a) bit mask"
    return "INCONCLUSIVE"


SENTINEL = b"\x00"  # 1-byte "nothing changed this tick" keepalive (~23k/replay, always 0x00)


def _analyze_replay(path: str, tail_tol: int, drop_sentinel: bool) -> dict:
    r = heat_replay.parse(path)
    by_handle: dict[int, list[bytes]] = {}
    for d in property_deltas(r.stream):
        if drop_sentinel and d.value == SENTINEL:
            continue  # a 1-byte 0x00 keepalive degenerates the shortest-record (S) tests
        by_handle.setdefault(d.handle, []).append(d.value)
    # keep handles with >= 2 distinct lengths and >= 8 records
    kept = {
        h: recs
        for h, recs in by_handle.items()
        if len(recs) >= 8 and len({len(v) for v in recs}) >= 2
    }
    orders = {o: _analyze_order(kept, o, tail_tol) for o in ("lsb", "msb")}

    cl = orders["lsb"]["containment_fraction"] or 0.0
    cm = orders["msb"]["containment_fraction"] or 0.0
    winning = "lsb" if cl >= cm else "msb"
    order_close = abs(cl - cm) < BIT_ORDER_CLOSE
    win = orders[winning]
    verdict = _verdict(win["containment_fraction"], win["first_field_bits"]["median"])
    # The bit order only matters for (b)/(c), where the verdict rests on HIGH containment
    # under one specific order. For (a) — low containment under BOTH orders — the order is
    # irrelevant (a mask breaks prefixes either way), so a "close" flag is not ambiguity.
    needs_order = verdict in ("(b) sequential streaming", "(c) tagged stream")
    if order_close and needs_order:
        verdict = "INCONCLUSIVE"
    return {
        "replay": os.path.basename(path),
        "kept_handles": len(kept),
        "orders": orders,
        "winning_bit_order": winning if needs_order else "n/a (mask breaks both orders)",
        "bit_order_close": order_close,
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tail-tolerance-bits",
        type=int,
        default=16,
        help="bits the final field of S may be cut by and still count as contained (default 16)",
    )
    ap.add_argument(
        "--keep-sentinel",
        action="store_true",
        help="keep the 1-byte 0x00 keepalive records (default: drop them; they degenerate "
        "the shortest-record tests because S becomes 8 bits < tail tolerance)",
    )
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(REPLAYS_DIR, "*.replay")))
    if not paths:
        print(json.dumps({"error": f"no replays in {REPLAYS_DIR}"}))
        return 1

    drop_sentinel = not args.keep_sentinel
    per_replay = [
        _analyze_replay(p, args.tail_tolerance_bits, drop_sentinel) for p in paths
    ]

    verdicts = {r["verdict"] for r in per_replay}
    orders = {r["winning_bit_order"] for r in per_replay}
    if len(verdicts) == 1 and len(orders) == 1 and "INCONCLUSIVE" not in verdicts:
        final_verdict = next(iter(verdicts))
        final_order = next(iter(orders))
    else:
        final_verdict = "INCONCLUSIVE"
        final_order = None

    report = {
        "tail_tolerance_bits": args.tail_tolerance_bits,
        "dropped_sentinel": drop_sentinel,
        "final_verdict": final_verdict,
        "winning_bit_order": final_order,
        "per_replay": per_replay,
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    # one-line human summary
    if final_verdict == "INCONCLUSIVE":
        detail = "; ".join(
            f"{r['replay'][:18]}: {r['verdict']} ({r['winning_bit_order']}, "
            f"contain={r['orders'][r['winning_bit_order']]['containment_fraction']})"
            for r in per_replay
        )
        print(f"\nVERDICT: INCONCLUSIVE — replays disagree or fell in the dead band. {detail}")
    else:
        # for (a) the order is "n/a"; report stats from lsb (both orders agree at ~0)
        stat_order = final_order if final_order in ("lsb", "msb") else "lsb"
        c = statistics.median(
            r["orders"][stat_order]["containment_fraction"] for r in per_replay
        )
        ffm = statistics.median(
            r["orders"][stat_order]["first_field_bits"]["median"] for r in per_replay
        )
        print(
            f"\nVERDICT: {final_verdict} — consistent across {len(per_replay)} replays "
            f"(bit order: {final_order}, median containment={c:.3f}, "
            f"median first-field bits={ffm:.0f})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
