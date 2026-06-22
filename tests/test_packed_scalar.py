"""Round-trip validation of the variable-width packed-scalar decoder.

Encoder and decoder are mutual inverses, so a sweep that survives encode->decode to within
quantization precision proves the read path is correct in isolation before it is trusted on real
replay bytes (the measure-first discipline of this repo).
"""

from __future__ import annotations

from heat_replay.bitstream import ReadStream
from heat_replay.packed_scalar import (
    RIGIDBODY_POSITION,
    ScalarParams,
    _float_from,
    encode_scalar_bits,
    read_scalar,
)


def _pack_bits(writes):
    acc = nbits = 0
    for val, n in writes:
        acc |= (val & ((1 << n) - 1)) << nbits
        nbits += n
    nbytes = (nbits + 7) // 8
    return acc.to_bytes(nbytes + ((-nbytes) % 8), "little")


def _roundtrip(value: float, p: ScalarParams) -> float:
    return read_scalar(ReadStream(_pack_bits(encode_scalar_bits(value, p))), p)


def test_float_from_known_value():
    # 100.0 with exp_base=12 -> width 19, mantissa 409600.
    assert _float_from(409600, 12, 19, 0) == 100.0
    assert _float_from(409600, 12, 19, 1) == -100.0
    assert _float_from(0, 12, 0, 0) == 0.0


def test_roundtrip_position_sweep():
    p = RIGIDBODY_POSITION
    for v in [0.0, 1.0, -1.0, 3.5, 12.0, 38.6, -77.25, 100.0, 250.0, 512.0, 1000.0, -1500.0, 3999.0]:
        out = _roundtrip(v, p)
        if v == 0.0:
            assert out == 0.0
        else:
            assert abs(out - v) / abs(v) < 1e-3, f"{v} -> {out}"


def test_roundtrip_dense():
    p = RIGIDBODY_POSITION
    s = 0x12345678
    bad = 0
    for _ in range(2000):
        s = (s * 1103515245 + 12345) & 0x7FFFFFFF
        v = (s / 0x7FFFFFFF) * 2000.0 - 1000.0
        out = _roundtrip(v, p)
        if v != 0.0 and abs(out - v) / max(abs(v), 1e-6) >= 1e-3:
            bad += 1
    assert bad == 0, f"{bad}/2000 exceeded 0.1%"


def test_zero_selector():
    p = RIGIDBODY_POSITION
    assert read_scalar(ReadStream(_pack_bits([(0, p.selector_bits)])), p) == 0.0


# --- real-replay integration (skips when fixture replays are absent) ---------------------------

def _decode_transform_positions(path):
    from heat_replay import stream
    from heat_replay.bitstream import ReadStream
    w = stream.walk(str(path))
    out = []
    for r in (x for x in w.records if x.tag == 6):
        rs = ReadStream(r.blob)
        try:
            rs.read_bits(32); rs.read_bits(1)
            for _ in range(2):
                if not rs.read_bits(1):
                    for _ in range(rs.read_bits(32)):
                        rs.read_bits(7)
            cc = rs.read_bits(32)
            if cc < 1 or rs.read_bits(7) != 37:  # 37 = rigid-body transform component
                continue
            pos = tuple(read_scalar(rs, RIGIDBODY_POSITION) for _ in range(3))
        except Exception:
            continue
        out.append((r.frame_id, r.entity_id, pos))
    return out


def test_baseline_positions_decode_sane(sample_path):
    hits = _decode_transform_positions(sample_path)
    assert hits, "no transform baselines decoded"
    # every decoded position must be finite and within generous world bounds (oracle)
    for _, _, p in hits:
        assert all(c == c and abs(c) < 5000.0 for c in p), p


def test_static_entity_position_cross_replay(samples):
    # A static map entity (id 27) must decode to the SAME position in every replay (build-stable).
    seen = set()
    for path in samples.values():
        for f, e, p in _decode_transform_positions(path):
            if e == 27 and any(abs(c) > 1.0 for c in p):
                seen.add(tuple(round(c, 3) for c in p))
                break
    assert seen == {(-160.0, 0.0, 13.808)}, seen
