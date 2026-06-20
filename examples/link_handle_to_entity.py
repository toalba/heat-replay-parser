"""Resolve tag-8 handle -> tag-6 entity_id -> prefab (-> class) by id arithmetic.

Tests candidate transforms ``entity_id = T(handle)`` against the tag-6 baseline entity_ids as
ground truth, then chains entity_id -> prefab -> class. Decodes no field value.

Discipline note: the ground-truth entity_id set is a *dense low prefix* (0..N), so any transform
that compresses handles into that range scores ~100% coverage **trivially**, and because most
entities spawn early the temporal causal check is also near-trivially satisfied. Coverage alone
therefore cannot select a transform. This tool guards against that with:

  * a trivial-coverage flag (derived-id range subset of a dense E),
  * a null control (coverage against a same-size random id set; low lift => non-informative),
  * a component-id structure test (a real component index is small/dense, not uniform),
  * a non-discrimination rule: if mutually-incompatible transforms all "pass", ACCEPT none.

A transform is only ACCEPTED if it passes coverage + temporal AND is non-trivial (structural
component distribution, meaningful lift) AND is the unique such transform, consistently across
all four replays.

Usage::

    python examples/link_handle_to_entity.py [--max-split-bits 16] [--coverage-threshold 0.90]

stdlib + heat_replay only. Deterministic output.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random

import heat_replay
from heat_replay.stream import baselines, property_deltas, walk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLAYS_DIR = os.path.join(ROOT, "replays")

TEMPORAL_MAX = 0.05      # max fraction of causal (spawn-before-replicate) violations
DENSE = 0.95             # E is "dense" if it fills >= this fraction of [0, max]
MIN_LIFT = 0.10          # coverage must beat the null control by at least this much
COMP_STRUCT_RATIO = 0.5  # split is "structural" if distinct comp ids <= this * 2^c


def _transforms(max_bits: int):
    """Yield ``(name, formula, fn)`` candidate handle -> entity_id transforms."""
    yield ("identity", "entity_id = handle", lambda h: h)
    for b in range(1, max_bits + 1):
        yield (f"mask_low_{b}", f"entity_id = handle & {(1 << b) - 1}",
               lambda h, b=b: h & ((1 << b) - 1))
        yield (f"split_hi_{b}", f"entity_id = handle >> {b}  (comp = handle & {(1 << b) - 1})",
               lambda h, b=b: h >> b)


def _component_stats(handles, c: int) -> dict:
    comps = [h & ((1 << c) - 1) for h in handles]
    distinct = len(set(comps))
    space = 1 << c
    small = sum(1 for x in comps if x < 16) / len(comps) if comps else 0.0
    return {
        "comp_bits": c,
        "distinct_comp": distinct,
        "comp_space": space,
        "distinct_ratio": round(distinct / space, 3),
        "frac_comp_lt16": round(small, 3),
        # structural if the component index is small/dense, not uniform over the space
        "structural": distinct <= max(1, int(COMP_STRUCT_RATIO * space)) or small >= 0.8,
    }


def _evaluate(fn, handles, E, hf, bf) -> dict:
    matched = 0
    viol = 0
    derived_max = 0
    for h in handles:
        e = fn(h)
        derived_max = max(derived_max, e)
        if e in E:
            matched += 1
            if hf[h] < bf.get(e, -1):
                viol += 1
    cov = matched / len(handles) if handles else 0.0
    return {
        "coverage": round(cov, 4),
        "temporal_violation": round(viol / matched, 4) if matched else None,
        "matched": matched,
        "derived_max": derived_max,
    }


def _null_coverage(fn, handles, n_E, e_max, seed) -> float:
    """Coverage if the ground-truth ids were a random same-size set over the same span."""
    rng = random.Random(seed)
    pool = list(range(e_max + 1))
    rng.shuffle(pool)
    fake = set(pool[:n_E])
    matched = sum(1 for h in handles if fn(h) in fake)
    return matched / len(handles) if handles else 0.0


def analyze_sets(E, prefab_of, handles, hf, bf, max_bits, cov_thresh, seed=12345) -> dict:
    """Core analysis over prepared id sets (importable for unit testing)."""
    e_max = max(E) if E else 0
    e_density = len(E) / (e_max + 1) if e_max else 0.0
    e_dense = e_density >= DENSE

    results = []
    for name, formula, fn in _transforms(max_bits):
        ev = _evaluate(fn, handles, E, hf, bf)
        null = _null_coverage(fn, handles, len(E), e_max, seed)
        lift = round(ev["coverage"] - null, 4)
        trivial = e_dense and ev["derived_max"] <= e_max
        entry = {
            "name": name, "formula": formula,
            **ev, "null_coverage": round(null, 4), "lift": lift, "trivial": trivial,
        }
        if name.startswith("split_hi_"):
            entry["component"] = _component_stats(handles, int(name.rsplit("_", 1)[1]))
        results.append(entry)

    # passing = coverage + temporal ok
    def passes(r):
        return (
            r["coverage"] >= cov_thresh
            and r["temporal_violation"] is not None
            and r["temporal_violation"] <= TEMPORAL_MAX
        )

    passing = [r for r in results if passes(r)]
    # non-trivial = beats null meaningfully AND (for splits) structural component dist
    non_trivial = [
        r for r in passing
        if not r["trivial"]
        and r["lift"] >= MIN_LIFT
        and (("component" not in r) or r["component"]["structural"])
    ]

    if len(non_trivial) == 1:
        verdict = "ACCEPTED"
        winner = non_trivial[0]
    elif len(non_trivial) > 1:
        verdict = "AMBIGUOUS"
        winner = None
    elif passing:
        # things pass only because coverage/temporal are trivially satisfied
        verdict = "NON-DISCRIMINATING"
        winner = None
    else:
        best = max(results, key=lambda r: r["coverage"])
        verdict = "PARTIAL" if best["coverage"] >= 0.5 else "UNRESOLVED"
        winner = None

    return {
        "e_count": len(E), "e_max": e_max, "e_density": round(e_density, 3),
        "e_dense": e_dense, "n_handles": len(handles),
        "n_passing_transforms": len(passing),
        "passing_transform_names": sorted(r["name"] for r in passing),
        "n_non_trivial": len(non_trivial),
        "verdict": verdict,
        "winner": winner,
        "transforms": results,
    }


def _prefab_to_class(prefabs, class_names) -> dict:
    """Best-effort prefab-stem -> schema class name (suffix match); unresolved otherwise."""
    mapping = {}
    lowered = {c.lower(): c for c in class_names}
    for p in prefabs:
        stem = os.path.basename(p).rsplit(".", 1)[0].lower()
        hit = lowered.get(stem) or next((c for lc, c in lowered.items() if lc.endswith(stem)), None)
        mapping[p] = hit
    return mapping


def _analyze_replay(path, max_bits, cov_thresh) -> dict:
    w = walk(path)
    E = {}
    bf = {}
    prefab_of = {}
    for b in baselines(w):
        if b.entity_id is None:
            continue
        E[b.entity_id] = True
        bf[b.entity_id] = min(bf.get(b.entity_id, 1 << 30), b.frame_id)
        prefab_of.setdefault(b.entity_id, b.prefab)
    Eset = set(E)

    hf = {}
    for d in property_deltas(w):
        if len(d.value) <= 1:
            continue
        hf[d.handle] = min(hf.get(d.handle, 1 << 30), d.frame_id)
    handles = sorted(hf)

    res = analyze_sets(Eset, prefab_of, handles, hf, bf, max_bits, cov_thresh)
    res["replay"] = os.path.basename(path)

    # prefab -> class (separate sub-problem; reported even if unresolved)
    r = heat_replay.parse(path)
    class_names = list(r.protocol.classes_by_name) if r.protocol else []
    prefabs = sorted(set(prefab_of.values()))
    p2c = _prefab_to_class(prefabs, class_names)
    res["prefab_to_class_resolved"] = sum(1 for v in p2c.values() if v)
    res["distinct_prefabs"] = len(prefabs)
    res["prefab_class_sample"] = {p: p2c[p] for p in prefabs[:8]}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-split-bits", type=int, default=16)
    ap.add_argument("--coverage-threshold", type=float, default=0.90)
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(REPLAYS_DIR, "*.replay")))
    if not paths:
        print(json.dumps({"error": f"no replays in {REPLAYS_DIR}"}))
        return 1

    per_replay = [
        _analyze_replay(p, args.max_split_bits, args.coverage_threshold) for p in paths
    ]
    verdicts = {r["verdict"] for r in per_replay}
    winners = {
        r["winner"]["name"] if r["winner"] else None for r in per_replay
    }
    if len(verdicts) == 1 and verdicts == {"ACCEPTED"} and len(winners) == 1:
        final = "ACCEPTED"
    elif verdicts == {"NON-DISCRIMINATING"}:
        final = "UNRESOLVED (id-arithmetic non-discriminating)"
    else:
        final = "UNRESOLVED" if "UNRESOLVED" in verdicts or len(verdicts) > 1 else verdicts.pop()

    report = {
        "params": {"max_split_bits": args.max_split_bits, "coverage_threshold": args.coverage_threshold},
        "final_verdict": final,
        "winning_transform": next(iter(winners)) if final == "ACCEPTED" else None,
        "per_replay": [
            {k: v for k, v in r.items() if k != "transforms"} for r in per_replay
        ],
        "transforms_first_replay": per_replay[0]["transforms"],
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    print(f"\nVERDICT: {final}")
    r0 = per_replay[0]
    print(
        f"  E is dense ({r0['e_density']:.2f} of [0..{r0['e_max']}]); "
        f"{r0['n_passing_transforms']} transforms 'pass' coverage+temporal but "
        f"{r0['n_non_trivial']} survive the non-triviality guards."
    )
    if final.startswith("UNRESOLVED"):
        print(
            "  Mutually-incompatible transforms pass only because E is a dense prefix and "
            "baselines spawn early. Id-arithmetic linkage has no discriminating power here. "
            "Fallback = baseline-blob length + spawn-order correlation (separate task)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
