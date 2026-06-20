"""Trust the mask-width least-squares solver on synthetic [mask][fields] data.

If the solver recovers known field widths exactly from a synthetic flat-mask record set,
then a failure on real replay data means the data is not a flat mask -- not a solver bug.
"""

import importlib.util
import os
import random

_SPEC = importlib.util.spec_from_file_location(
    "recover_mask_widths",
    os.path.join(os.path.dirname(__file__), "..", "examples", "recover_mask_widths.py"),
)
rmw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rmw)


def _build_records(field_widths, n, seed, intercept=0, per_field_overhead=0):
    """Synthesize flat-mask records: body_bits = intercept + Σ (w_i + k) over present fields."""
    rng = random.Random(seed)
    W = len(field_widths)
    A, b = [], []
    for _ in range(n):
        present = [rng.randint(0, 1) for _ in range(W)]
        # guarantee variety / full rank: occasionally force a single bit
        if rng.random() < 0.3:
            present = [0] * W
            present[rng.randrange(W)] = 1
        body = intercept + sum(
            (field_widths[i] + per_field_overhead) for i in range(W) if present[i]
        )
        A.append([1.0] + [float(x) for x in present])
        b.append(float(body))
    return A, b


def test_solver_recovers_known_widths_exactly():
    widths = [32, 32, 96, 8, 16, 1]
    A, b = _build_records(widths, n=200, seed=1)
    x, rank_def = rmw.solve_lstsq(A, b)
    assert rank_def == []
    recovered = [round(v) for v in x[1:]]
    assert recovered == widths
    assert round(x[0]) == 0  # intercept


def test_solver_recovers_intercept_and_overhead():
    widths = [32, 96, 12, 7]
    A, b = _build_records(widths, n=300, seed=2, intercept=5, per_field_overhead=2)
    x, _ = rmw.solve_lstsq(A, b)
    assert round(x[0]) == 5  # intercept
    # each present field carries +2 overhead => solved widths = width + 2
    recovered = [round(v) for v in x[1:]]
    assert recovered == [w + 2 for w in widths]


def test_solver_flags_rank_deficient_columns():
    # two fields that ALWAYS co-occur are not separately identifiable
    rng = random.Random(3)
    A, b = [], []
    w0, w1, w2 = 32, 8, 16
    for _ in range(150):
        c = rng.randint(0, 1)           # cols 1 and 2 always share this bit
        c3 = rng.randint(0, 1)
        body = (w0 + w1) * c + w2 * c3
        A.append([1.0, float(c), float(c), float(c3)])
        b.append(float(body))
    x, rank_def = rmw.solve_lstsq(A, b)
    assert rank_def, "expected a rank-deficient column for the co-occurring pair"


def test_leading_bit_helpers_lsb_msb():
    v = bytes([0b1000_0001])
    # lsb-first: bit 0 is the low bit
    assert rmw.bit_at(v, 0, "lsb") == 1
    assert rmw.bit_at(v, 1, "lsb") == 0
    assert rmw.bit_at(v, 7, "lsb") == 1
    # msb-first: bit 0 is the high bit
    assert rmw.bit_at(v, 0, "msb") == 1
    assert rmw.bit_at(v, 1, "msb") == 0
    assert rmw.bit_at(v, 7, "msb") == 1
    assert rmw.leading_popcount(v, 8, "lsb") == 2
