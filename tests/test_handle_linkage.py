"""Trust the handle->entity linkage recovery on synthetic data.

When linkage IS structural -- a SPARSE entity-id set and a SMALL dense component index packed
as ``handle = (entity_id << 7) | comp`` -- the analyzer must recover c=7 and ACCEPT it. This
proves the recovery logic before trusting its UNRESOLVED verdict on real (dense-E) data.
"""

import importlib.util
import os

_SPEC = importlib.util.spec_from_file_location(
    "link_handle_to_entity",
    os.path.join(os.path.dirname(__file__), "..", "examples", "link_handle_to_entity.py"),
)
link = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(link)


def _synthetic_structural():
    # SPARSE entity ids (not a dense prefix) so coverage is not trivial
    entities = [3, 17, 42, 99, 230, 501, 777, 1024]
    E = set(entities)
    prefab_of = {e: f"/x/e{e}.prefab" for e in entities}
    bf = {e: i for i, e in enumerate(entities)}  # spawn frames (early, distinct)
    hf = {}
    for e in entities:
        for comp in range(5):  # small dense component index 0..4
            h = (e << 7) | comp
            hf[h] = bf[e] + 10 + comp  # replicate after spawn (no temporal violation)
    handles = sorted(hf)
    return E, prefab_of, handles, hf, bf


def test_recovers_known_split():
    E, prefab_of, handles, hf, bf = _synthetic_structural()
    res = link.analyze_sets(E, prefab_of, handles, hf, bf, max_bits=16, cov_thresh=0.90)
    assert res["verdict"] == "ACCEPTED", res["verdict"]
    assert res["winner"]["name"] == "split_hi_7", res["winner"]["name"]
    assert res["winner"]["coverage"] >= 0.99
    assert res["winner"]["component"]["structural"]


def test_sparse_E_makes_coverage_nontrivial():
    E, prefab_of, handles, hf, bf = _synthetic_structural()
    res = link.analyze_sets(E, prefab_of, handles, hf, bf, max_bits=16, cov_thresh=0.90)
    assert not res["e_dense"]  # sparse E
    # the winning split must beat the null control (real linkage, not coincidence)
    assert res["winner"]["lift"] >= link.MIN_LIFT


def test_dense_E_is_flagged_nondiscriminating():
    # dense prefix 0..120 with handles packed every which way -> coverage is trivial
    E = set(range(121))
    prefab_of = {e: f"/x/e{e}.prefab" for e in E}
    bf = {e: 0 for e in E}  # all spawn at frame 0 -> temporal trivially satisfied
    hf = {}
    for base in range(0, 8000, 13):  # arbitrary handles spread out
        hf[base] = 100
    handles = sorted(hf)
    res = link.analyze_sets(E, prefab_of, handles, hf, bf, max_bits=16, cov_thresh=0.90)
    assert res["e_dense"]
    # multiple incompatible transforms pass trivially; none should be ACCEPTED
    assert res["verdict"] in ("NON-DISCRIMINATING", "AMBIGUOUS", "UNRESOLVED", "PARTIAL")
    assert res["winner"] is None
