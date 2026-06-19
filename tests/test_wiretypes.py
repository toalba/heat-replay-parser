"""Wire-type classification + primitive bit-decoder tests."""

import struct

import pytest

from heat_replay.bitstream import ReadStream
from heat_replay.wiretypes import WireType, classify, decode


def test_classify_exact_types():
    cases = {
        "CBool": WireType.BOOL,
        "CUint8": WireType.UINT8,
        "CUint32": WireType.UINT32,
        "CInt32": WireType.INT32,
        "CUint64": WireType.UINT64,
        "CEntityNetworkId": WireType.ENTITY_ID,
        "CPlayerId": WireType.PLAYER_ID,
        "CPlainFloat32": WireType.PLAIN_FLOAT32,
        "CPlainVec3": WireType.PLAIN_VEC3,
        "CFixed32": WireType.FIXED32,
        "CBounded32": WireType.BOUNDED32,
        "CFixedVec3": WireType.FIXED_VEC3,
        "CFixedQuat": WireType.FIXED_QUAT,
        "cw::CNormalizedQuat": WireType.NORMALIZED_QUAT,
        "CStdString": WireType.STRING,
    }
    for name, wt in cases.items():
        assert classify(name) is wt, name


def test_classify_fallbacks():
    assert classify("cw::ActionNameCompressor") is WireType.ENUM_POOL
    assert classify("vector<VariantCompressor>") is WireType.ENUM_POOL
    assert classify("CVectorMapUint8Uint8Compressor") is WireType.ENUM_POOL
    assert classify("CBallisticsHistoryInfoComposite") is WireType.COMPOSITE
    assert classify("vector<float>") is WireType.COMPOSITE
    assert classify(None) is WireType.UNKNOWN
    assert classify("TotallyUnknown") is WireType.UNKNOWN


def test_bit_width_and_decodable_flags():
    assert WireType.PLAIN_FLOAT32.bit_width == 32
    assert WireType.PLAIN_VEC3.bit_width == 96
    assert WireType.BOOL.bit_width == 1
    assert WireType.FIXED_QUAT.bit_width is None
    assert WireType.PLAIN_FLOAT32.is_decodable
    assert WireType.UINT32.is_decodable
    # quantized + structured types are not decodable from the type alone
    for wt in (WireType.FIXED32, WireType.BOUNDED32, WireType.FIXED_VEC3,
               WireType.FIXED_QUAT, WireType.STRING, WireType.ENUM_POOL, WireType.UNKNOWN):
        assert not wt.is_decodable, wt


def test_decode_plain_float_and_vec3_roundtrip():
    buf = struct.pack("<f", 123.5) + struct.pack("<3f", 1.0, -2.5, 3.25)
    rs = ReadStream(buf)
    assert decode(WireType.PLAIN_FLOAT32, rs) == 123.5
    assert decode(WireType.PLAIN_VEC3, rs) == (1.0, -2.5, 3.25)


def test_decode_integers_roundtrip():
    # bools and small ints packed LSB-first
    rs = ReadStream(bytes([0b101]))
    assert decode(WireType.BOOL, rs) is True
    assert decode(WireType.BOOL, rs) is False
    assert decode(WireType.BOOL, rs) is True

    rs = ReadStream(struct.pack("<I", 0xDEADBEEF))
    assert decode(WireType.UINT32, rs) == 0xDEADBEEF

    rs = ReadStream(bytes([0xFF]))  # -1 as int8
    assert decode(WireType.INT8, rs) == -1

    rs = ReadStream(struct.pack("<i", -12345))
    assert decode(WireType.INT32, rs) == -12345


def test_decode_quantized_returns_raw_bits():
    # FIXED32 / BOUNDED32 have no in-file scale, so we get the raw 32 bits, not real units.
    rs = ReadStream(struct.pack("<I", 0x01020304))
    assert decode(WireType.FIXED32, rs) == 0x01020304
    rs = ReadStream(struct.pack("<3I", 1, 2, 3))
    assert decode(WireType.FIXED_VEC3, rs) == (1, 2, 3)


def test_decode_rejects_unsupported_types():
    rs = ReadStream(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    for wt in (WireType.STRING, WireType.ENUM_POOL, WireType.COMPOSITE,
               WireType.UNKNOWN, WireType.FIXED_QUAT, WireType.NORMALIZED_QUAT):
        with pytest.raises(ValueError):
            decode(wt, rs)


def test_readstream_signed_and_skip():
    rs = ReadStream(bytes([0b0000_0011, 0xFF]))
    rs.skip(2)  # past the low two bits
    assert rs.read_bool() is False  # 3rd bit of 0b011 is 0
    rs2 = ReadStream(struct.pack("<f", -0.5))
    assert rs2.read_float32() == -0.5
