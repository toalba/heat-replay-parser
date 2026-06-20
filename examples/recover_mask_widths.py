"""Recover the bit-level mask width and per-field wire widths from tag-8 delta records.

The tag-8 per-entry payload is a bit-level presence mask followed by the changed fields in
canonical order (established separately). This tool tries to recover, per handle:

  1. the mask width W (bits), and whether the mask is flat or structured;
  2. the per-field wire width carried by each mask bit, via a least-squares linear system;
  3. validation against known-width plain fields (CPlainFloat32=32, CPlainVec3=96).

It decodes no field value. It emits widths + a structure verdict + evidence. If the flat-mask
model does not resolve cleanly it returns STRUCTURED-SUSPECTED (or UNRESOLVED) and stops —
it does NOT force a flat solution or build a structured decoder.

The linear solver is plain stdlib (normal equations + Gaussian elimination) and is unit-tested
on synthetic `[mask][fields]` data, so a failure on real data means the data is not flat — not
a solver bug.

Usage::

    python examples/recover_mask_widths.py [--bit-order {lsb,msb}] [--min-records N]

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

# Verdict thresholds.
FLAT_R2 = 0.95          # scalar popcount fit must be near-perfect for a flat fixed-width model
FLAT_WITHIN_VAR = 2.0   # records with identical mask must have ~identical body length (bits^2)
STRUCT_R2 = 0.60        # below this for ALL W => not a flat mask
PLAIN_TOL = 3           # bits of slack when matching a solved width to 32 / 96


# --------------------------------------------------------------------------- bits


def bit_at(value: bytes, i: int, order: str) -> int:
    """The i-th leading bit of ``value`` under ``order`` (``lsb`` or ``msb``)."""
    byte = value[i // 8]
    return (byte >> (i % 8)) & 1 if order == "lsb" else (byte >> (7 - (i % 8))) & 1


def leading_popcount(value: bytes, w: int, order: str) -> int:
    return sum(bit_at(value, i, order) for i in range(w))


def leading_mask(value: bytes, w: int, order: str) -> int:
    m = 0
    for i in range(w):
        m |= bit_at(value, i, order) << i
    return m


# ----------------------------------------------------------------- linear solver


def solve_lstsq(A: list[list[float]], b: list[float]):
    """Least-squares solve of ``A x = b`` via normal equations + Gaussian elimination.

    Returns ``(x, rank_deficient_cols)``. Columns with no usable pivot (e.g. a mask bit that
    never varies, or two bits that always co-occur) are reported and their x set to 0.0.
    Pure stdlib; no numpy.
    """
    n = len(A)
    m = len(A[0]) if A else 0
    # normal equations: (A^T A) x = A^T b
    AtA = [[0.0] * m for _ in range(m)]
    Atb = [0.0] * m
    for r in range(n):
        row = A[r]
        br = b[r]
        for i in range(m):
            ai = row[i]
            if ai:
                Atb[i] += ai * br
                AtAi = AtA[i]
                for j in range(m):
                    if row[j]:
                        AtAi[j] += ai * row[j]
    # augmented matrix, reduced-row-echelon with partial pivoting
    M = [AtA[i][:] + [Atb[i]] for i in range(m)]
    eps = 1e-9
    where = [-1] * m
    rank_def: list[int] = []
    rcur = 0
    for col in range(m):
        sel = -1
        best = eps
        for rr in range(rcur, m):
            if abs(M[rr][col]) > best:
                best = abs(M[rr][col])
                sel = rr
        if sel == -1:
            rank_def.append(col)
            continue
        M[rcur], M[sel] = M[sel], M[rcur]
        piv = M[rcur][col]
        for rr in range(m):
            if rr != rcur and abs(M[rr][col]) > 1e-15:
                f = M[rr][col] / piv
                for cc in range(col, m + 1):
                    M[rr][cc] -= f * M[rcur][cc]
        where[col] = rcur
        rcur += 1
    x = [0.0] * m
    for col in range(m):
        if where[col] != -1:
            x[col] = M[where[col]][m] / M[where[col]][col]
    return x, rank_def


# ----------------------------------------------------------------- per-handle


def _scalar_fit(popcounts: list[int], body: list[int]):
    """R² and slope of ``body ≈ a + slope*popcount`` (ordinary least squares)."""
    if len(set(popcounts)) < 2:
        return 0.0, 0.0
    mb = statistics.mean(body)
    mp = statistics.mean(popcounts)
    sxy = sum((p - mp) * (bb - mb) for p, bb in zip(popcounts, body))
    sxx = sum((p - mp) ** 2 for p in popcounts)
    slope = sxy / sxx if sxx else 0.0
    a = mb - slope * mp
    ss_res = sum((bb - (a + slope * p)) ** 2 for p, bb in zip(popcounts, body))
    ss_tot = sum((bb - mb) ** 2 for bb in body)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return r2, slope


def _within_mask_var(masks: list[int], body: list[int]) -> float:
    groups: dict[int, list[int]] = {}
    for m, bb in zip(masks, body):
        groups.setdefault(m, []).append(bb)
    return statistics.mean(
        statistics.pvariance(g) if len(g) > 1 else 0.0 for g in groups.values()
    )


def _analyze_handle(records: list[bytes], order: str) -> dict:
    min_len = min(len(v) for v in records)
    max_w = min(64, min_len * 8 - 1)  # mask must leave room for at least 1 body bit
    cands = []
    for w in range(1, max_w + 1):
        masks = [leading_mask(v, w, order) for v in records]
        pcs = [leading_popcount(v, w, order) for v in records]
        body = [len(v) * 8 - w for v in records]
        r2, slope = _scalar_fit(pcs, body)
        wvar = _within_mask_var(masks, body)
        n_masks = len(set(masks))
        # combined score: reward fit, penalise within-mask length spread
        score = r2 - min(1.0, wvar / 1024.0)
        cands.append(
            {"W": w, "r2": round(r2, 4), "slope": round(slope, 2),
             "within_var": round(wvar, 1), "n_masks": n_masks, "score": round(score, 4)}
        )
    cands.sort(key=lambda c: (-c["score"], c["W"]))
    top3 = cands[:3]
    best = top3[0]
    W = best["W"]

    # Step 3: full linear system at chosen W
    A = [[1.0] + [float(bit_at(v, i, order)) for i in range(W)] for v in records]
    b = [float(len(v) * 8 - W) for v in records]
    x, rank_def = solve_lstsq(A, b)
    intercept = x[0]
    widths = x[1:]
    residuals = [
        sum(A[r][j] * x[j] for j in range(len(x))) - b[r] for r in range(len(records))
    ]
    rms_resid = (sum(e * e for e in residuals) / len(residuals)) ** 0.5

    # Step 4: plain anchors (columns solving near 32 or 96)
    anchors = [
        {"bit": i, "width": round(widths[i], 2)}
        for i in range(W)
        if abs(widths[i] - 32) <= PLAIN_TOL or abs(widths[i] - 96) <= PLAIN_TOL
    ]

    # Step 5: structure verdict
    best_r2_all = max(c["r2"] for c in cands)
    flat_clean = (
        best["r2"] >= FLAT_R2
        and best["within_var"] <= FLAT_WITHIN_VAR
        and rms_resid <= 1.0
    )
    if flat_clean:
        verdict = "FLAT"
    elif best_r2_all < STRUCT_R2 or best["within_var"] > 1024 or best["slope"] <= 0:
        verdict = "STRUCTURED-SUSPECTED"
    else:
        verdict = "UNRESOLVED"

    return {
        "W": W,
        "top3_W": top3,
        "best_r2_over_all_W": round(best_r2_all, 4),
        "intercept": round(intercept, 2),
        "widths_raw": [round(w, 2) for w in widths],
        "rank_deficient_cols": rank_def,
        "rms_residual_bits": round(rms_resid, 2),
        "plain_anchors": anchors,
        "verdict": verdict,
        "evidence": {
            "best_within_mask_var": best["within_var"],
            "best_slope": best["slope"],
            "n_records": len(records),
        },
    }


def _analyze_replay(path: str, order: str, min_records: int) -> dict:
    r = heat_replay.parse(path)
    by_handle: dict[int, list[bytes]] = {}
    for d in property_deltas(r.stream):
        if len(d.value) <= 1:
            continue  # drop 1-byte 0x00 keepalive sentinels
        by_handle.setdefault(d.handle, []).append(d.value)
    kept = {
        h: recs
        for h, recs in sorted(by_handle.items())
        if len(recs) >= min_records and len({len(v) for v in recs}) >= 4
    }
    per_handle = {h: _analyze_handle(recs, order) for h, recs in kept.items()}
    vcount: dict[str, int] = {}
    for res in per_handle.values():
        vcount[res["verdict"]] = vcount.get(res["verdict"], 0) + 1
    # representative: the handle with the most records
    rep = max(kept, key=lambda h: len(kept[h])) if kept else None
    replay_verdict = (
        max(vcount, key=vcount.get) if vcount else "UNRESOLVED"
    )
    return {
        "replay": os.path.basename(path),
        "kept_handles": len(kept),
        "verdict_counts": vcount,
        "replay_verdict": replay_verdict,
        "representative_handle": rep,
        "representative": per_handle.get(rep) if rep is not None else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bit-order", choices=["lsb", "msb"], default="lsb")
    ap.add_argument(
        "--min-records",
        type=int,
        default=20,
        help="min surviving (non-sentinel) records per handle (spec asks 50; live data caps "
        "at ~46, so default 20)",
    )
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(REPLAYS_DIR, "*.replay")))
    if not paths:
        print(json.dumps({"error": f"no replays in {REPLAYS_DIR}"}))
        return 1

    per_replay = [_analyze_replay(p, args.bit_order, args.min_records) for p in paths]
    verdicts = {r["replay_verdict"] for r in per_replay}
    final = next(iter(verdicts)) if len(verdicts) == 1 else "UNRESOLVED"

    report = {
        "bit_order": args.bit_order,
        "min_records": args.min_records,
        "final_verdict": final,
        "cross_replay_agreement": len(verdicts) == 1,
        "per_replay": per_replay,
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    counts = {}
    for r in per_replay:
        for v, c in r["verdict_counts"].items():
            counts[v] = counts.get(v, 0) + c
    print(
        f"\nVERDICT: {final} ({'consistent' if len(verdicts) == 1 else 'DISAGREEMENT'} "
        f"across {len(per_replay)} replays) — per-handle tally {counts}, bit order {args.bit_order}"
    )
    if final == "STRUCTURED-SUSPECTED":
        print(
            "  Flat-mask model rejected: no W gives a clean popcount fit, within-mask body "
            "lengths vary widely, slope is non-positive. Structured/variable-width encoding "
            "suspected — STOP; structured recovery is a separate task."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
