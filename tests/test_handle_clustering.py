"""Trust the layout-clustering + provenance logic on synthetic data.

When K layout classes have distinct length signatures and occupancy windows are tight (so
provenance is informative), the building blocks must (a) recover K clusters, (b) narrow each
handle to few candidate prefabs, and (c) keep an injected noise point as its own cluster. This
proves the recovery logic before trusting its CLUSTERING-UNRELIABLE verdict on real data.
"""

import importlib.util
import os

_SPEC = importlib.util.spec_from_file_location(
    "cluster_handles_by_layout",
    os.path.join(os.path.dirname(__file__), "..", "examples", "cluster_handles_by_layout.py"),
)
clu = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(clu)


class _BL:
    def __init__(self, entity_id, frame_id, prefab):
        self.entity_id = entity_id
        self.frame_id = frame_id
        self.prefab = prefab


def test_single_linkage_recovers_k_classes_and_isolates_noise():
    # three tight blobs in feature space + one far outlier
    blobs = []
    for cx, cy in [(0, 0), (10, 10), (0, 20)]:
        for dx in range(6):
            blobs.append([cx + (dx % 2) * 0.1, cy + (dx // 2) * 0.1])
    blobs.append([100.0, 100.0])  # noise
    labels = clu._single_linkage(blobs, threshold=1.0)
    n_clusters = len(set(labels))
    assert n_clusters == 4, n_clusters  # 3 planted + 1 noise singleton
    # the noise point is alone in its cluster
    noise_label = labels[-1]
    assert labels.count(noise_label) == 1


def test_features_separate_distinct_length_signatures():
    # class A: small records; class B: large records -> different vectors -> different clusters
    recs_a = [(f, 14) for f in range(20)]
    recs_b = [(f, 120) for f in range(20)]
    fa, fb = clu._features(recs_a), clu._features(recs_b)
    va, vb = clu._vector(fa), clu._vector(fb)
    vectors = clu._normalize([va, vb, va, vb])
    labels = clu._single_linkage(vectors, threshold=0.5)
    assert len(set(labels)) == 2


def test_tight_occupancy_windows_give_informative_provenance():
    # non-overlapping windows for one recycled slot -> a handle's frame matches exactly one prefab
    bls = [
        _BL(5, 0, "/p/alpha.prefab"),
        _BL(5, 100, "/p/beta.prefab"),   # slot 5 reused at frame 100
        _BL(9, 0, "/p/gamma.prefab"),    # different slot, also alive
    ]
    windows = clu._occupancy_windows(bls)
    # at frame 50, slot 5 holds alpha and slot 9 holds gamma -> 2 candidates (still informative)
    cands = clu._candidate_prefabs(50, windows)
    assert cands == {"/p/alpha.prefab", "/p/gamma.prefab"}
    # at frame 150, slot 5 holds beta, slot 9 still gamma
    assert clu._candidate_prefabs(150, windows) == {"/p/beta.prefab", "/p/gamma.prefab"}
    # this is FEW candidates (informative) -- unlike real data's ~169
    assert len(cands) <= clu.PROVENANCE_AMBIGUITY_MAX
