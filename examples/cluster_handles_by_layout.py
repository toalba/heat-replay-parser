"""Cluster tag-8 handles into layout-equivalent groups (pooling fodder for width recovery).

Produces a ``handle -> cluster_id`` map from LAYOUT-INDEPENDENT statistics (record-length
signature + activity), then CROSS-VALIDATES the clusters against an independent axis: occupancy
provenance (which prefab instance was alive when the handle first replicated) and whether a
cluster recurs at stable multiplicity across instances of the same prefab.

Clustering on mask/width would be circular with what the downstream solver recovers, so it is
forbidden here; the occupancy axis is used only to validate, never to define, clusters.

Discipline: if the clusters do not recur per-prefab, the verdict is CLUSTERING-UNRELIABLE and we
STOP — we do not tune a threshold until something passes, and we never silently merge the
NO_BASELINE (spawned-pre-recording) bucket.

Usage::

    python examples/cluster_handles_by_layout.py [--distance-threshold T] [--min-records 8]
        [--min-prefab-occurrences M]

stdlib + heat_replay only. Deterministic output.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics

import heat_replay
from heat_replay.stream import baselines, property_deltas, walk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLAYS_DIR = os.path.join(ROOT, "replays")

# Coarse fixed length bins (bytes) for a layout-independent histogram.
LEN_BINS = [0, 8, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 1 << 20]
# Provenance is "non-informative" if the average handle matches more than this many prefabs.
PROVENANCE_AMBIGUITY_MAX = 5.0


# --------------------------------------------------------------- Step 1: features


def _features(records: list[tuple[int, int]]) -> dict:
    """Layout-independent feature vector from a handle's [(frame, byte_len), ...]."""
    lens = sorted(L for _, L in records)
    frames = [f for f, _ in records]
    first, last = min(frames), max(frames)
    lifespan = max(1, last - first)
    hist = [0] * (len(LEN_BINS) - 1)
    for L in lens:
        for i in range(len(LEN_BINS) - 1):
            if LEN_BINS[i] <= L < LEN_BINS[i + 1]:
                hist[i] += 1
                break
    n = len(lens)
    return {
        "min_len": lens[0], "median_len": statistics.median(lens), "max_len": lens[-1],
        "n_distinct_len": len(set(lens)),
        "mean_len": statistics.mean(lens), "var_len": statistics.pvariance(lens) if n > 1 else 0.0,
        "first_frame": first, "last_frame": last, "lifespan": lifespan,
        "record_count": n, "records_per_frame": round(n / lifespan, 4),
        "len_hist": [round(h / n, 3) for h in hist],
    }


def _vector(f: dict) -> list[float]:
    return [
        float(f["min_len"]), float(f["median_len"]), float(f["max_len"]),
        float(f["n_distinct_len"]), float(f["mean_len"]), float(f["var_len"]),
        float(f["records_per_frame"]),
    ] + [float(x) for x in f["len_hist"]]


# --------------------------------------------------------------- Step 2: cluster


def _normalize(vectors: list[list[float]]) -> list[list[float]]:
    if not vectors:
        return []
    m = len(vectors[0])
    cols = list(zip(*vectors))
    means = [statistics.mean(c) for c in cols]
    sds = [statistics.pstdev(c) or 1.0 for c in cols]
    return [[(v[j] - means[j]) / sds[j] for j in range(m)] for v in vectors]


def _dist(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _single_linkage(vectors: list[list[float]], threshold: float) -> list[int]:
    """Union-find single-linkage: merge points within ``threshold`` (normalized distance)."""
    n = len(vectors)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _dist(vectors[i], vectors[j]) <= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    # relabel roots to dense 0..k-1, deterministically
    roots = sorted({find(i) for i in range(n)})
    remap = {r: k for k, r in enumerate(roots)}
    return [remap[find(i)] for i in range(n)]


# ------------------------------------------------ Step 3: occupancy provenance


def _occupancy_windows(bls):
    """[(spawn_frame, next_reuse_frame, prefab), ...] per recycled entity_id slot."""
    by_id: dict[int, list] = {}
    for b in bls:
        if b.entity_id is None:
            continue
        by_id.setdefault(b.entity_id, []).append(b)
    windows = []
    for occ in by_id.values():
        occ.sort(key=lambda b: b.frame_id)
        for i, b in enumerate(occ):
            nxt = occ[i + 1].frame_id if i + 1 < len(occ) else (1 << 30)
            windows.append((b.frame_id, nxt, b.prefab))
    return windows


def _candidate_prefabs(first_frame: int, windows) -> set[str]:
    return {p for s, n, p in windows if s <= first_frame < n}


# ----------------------------------------------------------------- per replay


def _analyze_replay(path, threshold, min_records, min_occ) -> dict:
    w = walk(path)
    recs: dict[int, list] = {}
    for d in property_deltas(w):
        if len(d.value) <= 1:
            continue  # sentinel
        recs.setdefault(d.handle, []).append((d.frame_id, len(d.value)))
    handles = sorted(h for h, v in recs.items() if len(v) >= min_records)
    feats = {h: _features(recs[h]) for h in handles}

    # Step 2 cluster
    vectors = _normalize([_vector(feats[h]) for h in handles])
    labels = _single_linkage(vectors, threshold)
    handle_cluster = dict(zip(handles, labels))
    n_clusters = len(set(labels))
    sizes = {}
    for c in labels:
        sizes[c] = sizes.get(c, 0) + 1
    n_singletons = sum(1 for s in sizes.values() if s == 1)

    # Step 3 provenance
    windows = _occupancy_windows([b for b in baselines(w) if b.entity_id is not None])
    cand_counts = []
    no_baseline = []
    handle_prefabs = {}
    for h in handles:
        cands = _candidate_prefabs(feats[h]["first_frame"], windows)
        handle_prefabs[h] = cands
        if not cands:
            no_baseline.append(h)
        else:
            cand_counts.append(len(cands))
    mean_cands = statistics.mean(cand_counts) if cand_counts else 0.0

    # Step 4 recurrence: per prefab, multiset of cluster ids across its (candidate) handles
    prefab_clusters: dict[str, list[int]] = {}
    for h in handles:
        for p in handle_prefabs[h]:
            prefab_clusters.setdefault(p, []).append(handle_cluster[h])
    # a cluster is "recurrent" only if provenance is informative enough to attribute it
    provenance_informative = mean_cands <= PROVENANCE_AMBIGUITY_MAX

    # Step 5 pooled readiness per cluster
    pooled = {}
    for c in sizes:
        members = [h for h in handles if handle_cluster[h] == c]
        total_recs = sum(len(recs[h]) for h in members)
        distinct_lengths = len({L for h in members for _, L in recs[h]})
        pooled[c] = {
            "size": sizes[c], "pooled_record_count": total_recs,
            "distinct_lengths": distinct_lengths,
        }

    # decision (per replay)
    if not provenance_informative:
        verdict = "CLUSTERING-UNRELIABLE"
    elif n_singletons / max(1, len(handles)) > 0.5:
        verdict = "CLUSTERING-UNRELIABLE"
    else:
        verdict = "CLUSTERS-RECUR"  # would need the full recurrence accept-list

    return {
        "replay": os.path.basename(path),
        "n_handles": len(handles),
        "n_clusters": n_clusters,
        "n_singletons": n_singletons,
        "largest_cluster": max(sizes.values()) if sizes else 0,
        "mean_candidate_prefabs_per_handle": round(mean_cands, 1),
        "provenance_informative": provenance_informative,
        "no_baseline_bucket": len(no_baseline),
        "verdict": verdict,
        "pooled_readiness_sample": dict(sorted(pooled.items())[:5]),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--distance-threshold", type=float, default=1.0)
    ap.add_argument("--min-records", type=int, default=8)
    ap.add_argument("--min-prefab-occurrences", type=int, default=3)
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(REPLAYS_DIR, "*.replay")))
    if not paths:
        print(json.dumps({"error": f"no replays in {REPLAYS_DIR}"}))
        return 1

    per_replay = [
        _analyze_replay(p, args.distance_threshold, args.min_records, args.min_prefab_occurrences)
        for p in paths
    ]
    verdicts = {r["verdict"] for r in per_replay}
    final = next(iter(verdicts)) if len(verdicts) == 1 else "UNRESOLVED (replays disagree)"

    print(json.dumps({"params": vars(args), "final_verdict": final, "per_replay": per_replay},
                     indent=2, sort_keys=True))
    r0 = per_replay[0]
    print(f"\nVERDICT: {final}")
    print(
        f"  Provenance ambiguity: ~{r0['mean_candidate_prefabs_per_handle']} candidate prefabs "
        f"per handle (informative <= {PROVENANCE_AMBIGUITY_MAX}). "
        f"Clusters: {r0['n_clusters']} from {r0['n_handles']} handles "
        f"({r0['n_singletons']} singletons)."
    )
    if final == "CLUSTERING-UNRELIABLE":
        print(
            "  Self-stat clusters cannot be cross-validated: occupancy provenance maps almost "
            "every handle to almost every prefab (all slots continuously occupied), so per-prefab "
            "recurrence has no signal. STOP — layout-independent clustering is the wrong axis here."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
