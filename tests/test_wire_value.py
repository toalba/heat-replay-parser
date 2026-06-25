"""Bit-consumption + round-trip validation for the per-field value codecs.

Each codec must advance the read cursor by exactly the bits it wrote, or the self-delimiting walk
desyncs. These craft a known bit layout and assert ``consume`` lands on the next field boundary
(the measure-first discipline: prove the read path in isolation before trusting it on real bytes).
"""
from __future__ import annotations

import math

from heat_replay import wire_value
from heat_replay.bitstream import ReadStream
from heat_replay.packed_scalar import encode_scalar_bits
from heat_replay.wire_value import _STD


def _pack(writes):
    """Pack (value, width) pairs LSB-first into bytes (matches ReadStream)."""
    acc = nbits = 0
    for val, n in writes:
        acc |= (val & ((1 << n) - 1)) << nbits
        nbits += n
    return acc.to_bytes((nbits + 7) // 8, "little") or b"\x00"


def _consumes(field_type, writes, expect_bits, enum_width=None, mode=1):
    """Assert consume(field_type) reads exactly expect_bits, then a sentinel survives intact."""
    sentinel = 0b1011
    rs = ReadStream(_pack(writes + [(sentinel, 4)]))
    wire_value.consume(field_type, rs, enum_width, mode)
    assert rs.bits_read == expect_bits, f"{field_type}: read {rs.bits_read}, expected {expect_bits}"
    assert rs.read_bits(4) == sentinel, f"{field_type}: desynced (sentinel corrupt)"


def _enc_len(v):
    """Encode an integer with the array-length packer (2-bit selector + smallest fitting tier)."""
    for sel, w in enumerate((1, 4, 8, 16)):
        if v < (1 << w):
            return [(sel, 2), (v, w)]
    raise ValueError(v)


def test_fixed_widths():
    cases = {
        "CBool": 1, "CUint8": 8, "CUint16": 16, "CUint32": 32, "CUint64": 64,
        "CInt8": 8, "CInt32": 32, "CInt64": 64, "CPlainFloat32": 32, "CPlayerId": 8,
    }
    for t, bits in cases.items():
        _consumes(t, [(0, bits)], bits)


def test_plain_vec3_is_96_bits():
    _consumes("CPlainVec3", [(0, 32), (0, 32), (0, 32)], 96)


def test_entity_ref_take_from_list_is_1_bit():
    _consumes("CEntityNetworkId", [(1, 1)], 1)


def test_entity_ref_index_is_10_bits():
    _consumes("CEntityNetworkId", [(0, 1), (1, 1), (42, 8)], 10)


def test_entity_ref_delta_widths():
    # "00" prefix + sign + 2-bit selector + tier width (4/8/16/24)
    for sel, w in enumerate((4, 8, 16, 24)):
        _consumes("CEntityNetworkId", [(0, 1), (0, 1), (0, 1), (sel, 2), (0, w)], 5 + w)


def test_stdstring_len_prefixed_no_alignment():
    # 16-bit length n, then n*8 bits, with no byte alignment in between
    for n in (0, 1, 5, 17):
        _consumes("CStdString", [(n, 16)] + [(0, 8)] * n, 16 + n * 8)


def test_packed_quat_is_three_scalars():
    # CFixedQuat / CNormalizedQuat transmit three packed-scalar components.
    for t in ("CFixedQuat", "cw::CNormalizedQuat"):
        writes = []
        for comp in (0.5, -0.25, 0.125):
            writes += encode_scalar_bits(comp, _STD)
        _consumes(t, writes, sum(n for _, n in writes))


def test_enum_width_is_used():
    for w in (3, 6, 8, 14):
        _consumes("cw::SomeEnum", [(0, w)], w, enum_width=w)


def test_array_absolute_counts():
    # vector<CUint8> add/absolute: array-length-packed count, then count*8 bits.
    for n in (0, 1, 5, 17, 300):
        writes = _enc_len(n) + [(0, 8)] * n
        expect = sum(w for _, w in writes)
        _consumes("vector<CUint8>", writes, expect, mode=1)


def test_array_absolute_entity_elements():
    # vector<CEntityNetworkId>: count, then each element is a self-delimiting entity reference.
    writes = _enc_len(3) + [(1, 1), (1, 1), (1, 1)]  # 3 take-from-list refs (1 bit each)
    _consumes("vector<CEntityNetworkId>", writes, sum(w for _, w in writes), mode=1)


def test_array_game_item_elements_are_128_bits():
    # vector<CGameItemCompressor>: count, then each element is a fixed 16-byte (128-bit) id.
    writes = _enc_len(2) + [(0, 32)] * 8  # 2 elements * 128 bits = 8 * 32-bit writes
    _consumes("vector<network::compression::CGameItemCompressor>", writes,
              sum(w for _, w in writes), mode=1)


def test_variable_storage_is_byte_vector():
    for n in (0, 4, 200):
        writes = _enc_len(n) + [(0, 8)] * n
        _consumes("cw::VariableStorageCompressor", writes, sum(w for _, w in writes), mode=1)


def test_array_delta_grow_only():
    # update/delta: sign=0 (grew), countDelta=2, first=0 (no in-place changes), then 2 tail bytes.
    writes = [(0, 1)] + _enc_len(2) + _enc_len(0) + [(0, 8), (0, 8)]
    _consumes("vector<CUint8>", writes, sum(w for _, w in writes), mode=0)


def test_array_delta_in_place_changes():
    # sign=0, countDelta=0 (same size), first=3 (index 2 changed), elem + gap=2, elem + gap=0 (end).
    writes = ([(0, 1)] + _enc_len(0) + _enc_len(3)
              + [(0, 8)] + _enc_len(2) + [(0, 8)] + _enc_len(0))
    _consumes("vector<CUint8>", writes, sum(w for _, w in writes), mode=0)


def test_array_delta_shrink_has_no_tail():
    # sign=1 (shrank), countDelta=3, first=0 (no changes); shrink => no tail bytes consumed.
    writes = [(1, 1)] + _enc_len(3) + _enc_len(0)
    _consumes("vector<CUint8>", writes, sum(w for _, w in writes), mode=0)


def test_unsupported_without_enum_width_raises():
    rs = ReadStream(_pack([(0, 8)]))
    try:
        wire_value.consume("vector<network::SomeUnknownCompressor>", rs, None)
    except wire_value.Unsupported as exc:
        assert exc.field_type == "vector<network::SomeUnknownCompressor>"
    else:
        raise AssertionError("expected Unsupported")


# --- variant (tagged-record) values -------------------------------------------------------------
def test_variant_scalar_absolute_roundtrip():
    # A single VariantCompressor (add/absolute): a 32-bit type tag, then the tagged record body
    # (here consumed by a stub nested reader that knows each tag's body width).
    body = {7: 5, 42: 13, 100: 0}

    def nested(tag, rs, mode):
        assert mode == 1
        rs.skip(body[tag])

    for tag, w in body.items():
        writes = [(tag, 32), (0, w)]
        rs = ReadStream(_pack(writes + [(0b1011, 4)]))
        wire_value.consume("VariantCompressor", rs, None, mode=1, nested=nested)
        assert rs.bits_read == 32 + w, (tag, rs.bits_read)
        assert rs.read_bits(4) == 0b1011


def test_variant_vector_absolute_roundtrip():
    # vector<VariantCompressor> (add/absolute): array-length-packed count, then each element is a
    # 32-bit tag + its record body.
    body = {7: 5, 42: 13, 100: 0}
    tags = [7, 42, 7, 100]

    def nested(tag, rs, mode):
        rs.skip(body[tag])

    writes = _enc_len(len(tags))
    for tag in tags:
        writes += [(tag, 32), (0, body[tag])]
    expect = sum(w for _, w in writes)
    rs = ReadStream(_pack(writes + [(0b1011, 4)]))
    wire_value.consume("vector<VariantCompressor>", rs, None, mode=1, nested=nested)
    assert rs.bits_read == expect
    assert rs.read_bits(4) == 0b1011


def test_variant_delta_mode_is_unsupported():
    # The update/delta form reuses prior-tick per-element state a stateless walk lacks -> unmodelled.
    def nested(tag, rs, mode):  # pragma: no cover - must not be reached
        raise AssertionError("nested reader must not run in delta mode")

    for t in ("VariantCompressor", "vector<VariantCompressor>"):
        rs = ReadStream(_pack([(0, 32)]))
        try:
            wire_value.consume(t, rs, None, mode=0, nested=nested)
        except wire_value.Unsupported:
            pass
        else:
            raise AssertionError(f"{t}: expected Unsupported in delta mode")
        assert rs.bits_read == 0, f"{t}: consumed bits before refusing"


def test_variant_without_nested_reader_is_unsupported():
    for t in ("VariantCompressor", "vector<VariantCompressor>"):
        rs = ReadStream(_pack([(0, 32)]))
        try:
            wire_value.consume(t, rs, None, mode=1, nested=None)
        except wire_value.Unsupported:
            pass
        else:
            raise AssertionError(f"{t}: expected Unsupported without a nested reader")
        assert rs.bits_read == 0


def test_fixed_vec3_roundtrip_and_consumption():
    vec = (12.5, -3.25, 100.0)
    writes = []
    for comp in vec:
        writes += encode_scalar_bits(comp, _STD)
    expect_bits = sum(n for _, n in writes)
    rs = ReadStream(_pack(writes + [(0b101, 3)]))
    out = wire_value.consume("CFixedVec3", rs, None)
    assert out is not None
    assert rs.bits_read == expect_bits
    assert rs.read_bits(3) == 0b101  # not desynced
    for got, want in zip(out, vec):
        assert math.isclose(got, want, rel_tol=0.02, abs_tol=0.5), (got, want)
