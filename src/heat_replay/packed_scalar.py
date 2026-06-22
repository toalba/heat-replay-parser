"""Decoder for the variable-width bit-packed scalars in the replication journal.

The per-entity property journals store floating-point fields not as raw IEEE-754 but as a
variable-width packed form on the LSB-first bit stream (:class:`heat_replay.bitstream.ReadStream`).
Each packed scalar is:

    sel       = read_bits(selector_bits)          # selector_bits = log2(len(tier_table))
    width     = tier_table[sel]                    # 0  => the value is 0.0
    if width == 0: value = 0.0
    else:
        mant  = read_bits(width)
        if mant == 0: value = 0.0
        else:
            while width-1 == 32 or (mant >> (width-1)) == 0:   # strip leading zero bits
                width -= 1
            sign  = read_bits(1)
            value = _float_from(mant, exp_base, width, sign)
    value *= scale

``_float_from`` rebuilds an IEEE-754 float from the stripped mantissa, a per-field exponent base,
the significant-bit count, and a sign bit:

    biasedExp = width - exp_base + 126
    bits      = (sign<<8 | biasedExp) << 23 | (mant << (24 - width)) & 0x7fffff

A 3-component vector is three consecutive scalar reads. Per-field parameters (``tier_table``,
``exp_base``, ``scale``) are tabulated in :class:`ScalarParams`; the rigid-body transform's
``position`` field parameters are :data:`RIGIDBODY_POSITION`.

The decoder is validated by an encode/decode round-trip (tests/test_packed_scalar.py) before being
trusted on real replay bytes; ``_to_fixed_point`` is the matching encoder used only by that test.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from heat_replay.bitstream import ReadStream

_SIGNIFICAND_BITS = 24


def _bits_to_f32(b: int) -> float:
    return struct.unpack("<f", struct.pack("<I", b & 0xFFFFFFFF))[0]


def _f32_to_bits(f: float) -> int:
    return struct.unpack("<I", struct.pack("<f", f))[0]


def _float_from(mantissa: int, exp_base: int, width: int, sign: int) -> float:
    """Rebuild an IEEE-754 float from a stripped mantissa + exponent base + bit width + sign."""
    if width == 0:
        return 0.0
    if width > _SIGNIFICAND_BITS:
        raise ValueError(f"width {width} exceeds significand bits ({_SIGNIFICAND_BITS})")
    biased_exp = (width - exp_base) + 0x7E
    if not (0 <= biased_exp <= 0xFF):
        raise ValueError(f"biased exponent {biased_exp} out of range (exp_base={exp_base})")
    bits = (((sign & 1) << 8 | biased_exp) << 23) | ((mantissa << (24 - width)) & 0x7FFFFF)
    return _bits_to_f32(bits)


def _to_fixed_point(value: float, exp_base: int, max_bits: int):
    """Inverse of :func:`_float_from` — encoder used only by the round-trip test.

    Returns ``(mantissa, width, sign)`` where ``width`` is the count of significant mantissa bits.
    """
    b = _f32_to_bits(value)
    sign = (b >> 0x1F) & 1
    exp_field = (b >> 0x17) & 0xFF
    if exp_field == 0:
        return 0, 0, sign
    iv = (exp_base - 0x7E) + exp_field
    u = (b & 0x7FFFFF) | 0x800000
    shift = _SIGNIFICAND_BITS - iv
    if shift > 0:
        u = u + ((1 << (shift - 1)) & u)
        iv = iv + (u >> _SIGNIFICAND_BITS)
    if iv <= max_bits:
        if iv > 0:
            return (u >> (shift & 0x1F)) & 0xFFFFFFFF, iv, sign
        return 0, 0, sign
    return (0xFFFFFFFF >> (0x20 - max_bits)) & 0xFFFFFFFF, max_bits, sign


@dataclass(frozen=True)
class ScalarParams:
    """Per-field parameters of a variable-width packed scalar.

    ``tier_table`` is the set of candidate bit widths (the selector is ``log2(len)`` bits wide);
    ``exp_base`` the exponent base; ``scale`` the post-multiply factor.
    """

    tier_table: tuple[int, ...]
    exp_base: int
    scale: float = 1.0

    @property
    def selector_bits(self) -> int:
        return max(1, (len(self.tier_table) - 1).bit_length())


# Rigid-body transform component, ``position`` field (a 3-component packed vector).
RIGIDBODY_POSITION = ScalarParams(
    tier_table=(0, 6, 9, 12, 15, 18, 21, 24), exp_base=12, scale=1.0
)


def read_scalar(rs: ReadStream, p: ScalarParams) -> float:
    """Read one variable-width packed scalar from an LSB-first bit stream."""
    sel = rs.read_bits(p.selector_bits)
    # selector_bits rounds up to a power of two; a non-power-of-2 tier table can therefore
    # be addressed by a selector value past its end. Valid streams never emit one; clamp so
    # a malformed/misaligned stream degrades gracefully instead of raising IndexError.
    if sel >= len(p.tier_table):
        sel = len(p.tier_table) - 1
    width = p.tier_table[sel]
    if width == 0:
        return 0.0
    mant = rs.read_bits(width)
    if mant == 0:
        return 0.0
    while width - 1 == 32 or (mant >> (width - 1)) == 0:
        width -= 1
    sign = rs.read_bits(1)
    return _float_from(mant, p.exp_base, width, sign) * p.scale


def read_vec3(rs: ReadStream, p: ScalarParams) -> tuple[float, float, float]:
    """Read three consecutive packed scalars as a vector."""
    return (read_scalar(rs, p), read_scalar(rs, p), read_scalar(rs, p))


# --- encoder (round-trip test only) ----------------------------------------------------------

def _smallest_tier(tier_table: tuple[int, ...], need: int) -> int:
    best = None
    for i, t in enumerate(tier_table):
        if t >= need and (best is None or t < tier_table[best]):
            best = i
    if best is None:
        raise ValueError(f"no tier fits {need} bits in {tier_table}")
    return best


def encode_scalar_bits(value: float, p: ScalarParams) -> list[tuple[int, int]]:
    """Encode a scalar to ``(value, nbits)`` LSB-first writes — test harness only."""
    mant, width, sign = _to_fixed_point(value / p.scale if p.scale else value,
                                        p.exp_base, p.tier_table[-1])
    if width == 0:
        if p.tier_table[0] == 0:
            return [(0, p.selector_bits)]
        sel = _smallest_tier(p.tier_table, 1)
        return [(sel, p.selector_bits), (0, p.tier_table[sel])]
    sel = _smallest_tier(p.tier_table, width)
    return [(sel, p.selector_bits), (mant, p.tier_table[sel]), (sign, 1)]
