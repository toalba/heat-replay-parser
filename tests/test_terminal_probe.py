"""Trust the terminal probe's two legs on synthetic data before reading its real-data verdict.

Leg A: a planted smooth 3xf32 position stream (with id) must be flagged POSITION-LIKE via
continuity, and a random-f32 decoy must be rejected.
Leg B: records with a planted [ceil(log2(n))-bit mask][32-bit plain field] must surface the 32
anchor at the planted width and not at a wrong width.
"""

import importlib.util
import math
import os
import random
import struct

_SPEC = importlib.util.spec_from_file_location(
    "terminal_probe",
    os.path.join(os.path.dirname(__file__), "..", "examples",
                 "terminal_position_and_schemawidth_probe.py"),
)
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)


def test_leg_a_flags_smooth_position_stream():
    # 5 entities, each moving smoothly; record = [id:u32][x,y,z f32][pad]
    records = []
    starts = {eid: (eid * 10.0, eid * 5.0, 0.0) for eid in range(5)}
    for frame in range(40):
        for eid, (x0, y0, z0) in starts.items():
            x, y, z = x0 + frame * 1.5, y0 + frame * 0.5, z0 + frame * 0.1
            buf = struct.pack("<I", eid) + struct.pack("<3f", x, y, z) + b"\x00" * 8
            records.append((frame, buf))
    cand = tp.scan_source(records, coord_max=2000.0, order="lsb")
    # the validated claim is continuity-based detection, not the exact offset (overlapping
    # float bytes make several offsets read smoothly)
    assert cand is not None and cand["position_like"], cand
    assert cand["id_offset"] == 0  # the recurring entity-id column is found


def test_leg_a_rejects_random_decoy():
    rng = random.Random(7)
    records = []
    for frame in range(60):
        eid = rng.randrange(5)
        x, y, z = (rng.uniform(-1500, 1500) for _ in range(3))  # no continuity
        buf = struct.pack("<I", eid) + struct.pack("<3f", x, y, z) + b"\x00" * 8
        records.append((frame, buf))
    cand = tp.scan_source(records, coord_max=2000.0, order="lsb")
    # either no candidate, or one that is NOT position-like (steps too large)
    assert cand is None or not cand["position_like"], cand


def test_leg_b_recovers_32_anchor_at_planted_width():
    # n fields => W = ceil(log2(n)); plant field 0 width 32, others small. Build records whose
    # body length = sum of present-field widths under a W-bit mask.
    widths = [32, 8, 16, 1, 12]
    W = max(1, (len(widths) - 1).bit_length())  # ceil(log2(5)) = 3
    rng = random.Random(3)
    records = []
    for _ in range(120):
        present = [rng.randint(0, 1) for _ in range(W)]
        if rng.random() < 0.3:
            present = [0] * W
            present[rng.randrange(W)] = 1
        body_bits = sum(widths[i] for i in range(W) if present[i])
        total_bits = W + body_bits
        nbytes = (total_bits + 7) // 8
        # pack mask into the leading W bits (lsb-first), rest zero -- _anchors_at_width only
        # reads the mask bits + total length, so exact body content is irrelevant.
        val = 0
        for i in range(W):
            val |= present[i] << i
        records.append(val.to_bytes(max(nbytes, 1), "little"))
    hits = tp._anchors_at_width(records, W, "lsb")
    assert any(tp.ANCHOR_F32[0] <= h["width"] <= tp.ANCHOR_F32[1] for h in hits), hits
    # at a wrong width the 32 anchor should not cleanly appear
    wrong = tp._anchors_at_width(records, W + 5, "lsb")
    assert hits != wrong


def test_leg_b_residual_gate_rejects_garbage_fit():
    # record lengths uncorrelated with the mask bits => no clean linear fit => NO anchor, even
    # if a solved width lands near 32 by chance. This is the gate that killed the false positive.
    rng = random.Random(11)
    W = 3
    records = []
    for _ in range(120):
        present = [rng.randint(0, 1) for _ in range(W)]
        total_bits = W + rng.randrange(8, 800)  # length independent of the mask
        val = sum(present[i] << i for i in range(W))
        records.append(val.to_bytes(max((total_bits + 7) // 8, 1), "little"))
    assert tp._anchors_at_width(records, W, "lsb") == []
