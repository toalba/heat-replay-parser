"""Wire-type classification and primitive decoders for replicated fields.

The embedded schema names a wire type for every replicated field (see
:class:`heat_replay.model.SchemaField`). This module turns those type-name strings into a
small :class:`WireType` enum and provides bit-level decoders for the *self-describing*
types — the ones whose representation is fully determined by the type alone:

* booleans and fixed-width integers (``CBool``, ``CUint8/16/32/64``, ``CInt8/32/64``,
  ``CEntityNetworkId``, ``CPlayerId``),
* full-precision floats (``CPlainFloat32``) and float triples (``CPlainVec3``).

The *quantized* types (``CFixed32``, ``CBounded32``, ``CFixedVec3``, ``CFixedQuat``,
``cw::CNormalizedQuat``) carry their bit-width / numeric bound at C++ registration time —
those constants are **not** in the replay file (they are baked into the engine binary at
registration time). We can read their raw bits, but mapping
them to real units (meters/radians) needs the per-type constant, so they are exposed as
raw integers, not typed values.

Important: this module decodes a value from a bit stream *at the current cursor*. It does
**not** know where in a property-journal entry a given field begins — the journal stream
is bit-packed relative-delta (see ``docs/DECODING.md``), and that field-layout linkage is
not yet established. These decoders are correct primitives; wiring them to live journal
bytes is the remaining, separately-validated step.
"""

from __future__ import annotations

import enum
import re

from heat_replay.bitstream import ReadStream


class WireType(enum.Enum):
    """Semantic category of a replicated field's wire encoding."""

    BOOL = enum.auto()
    UINT8 = enum.auto()
    UINT16 = enum.auto()
    UINT32 = enum.auto()
    UINT64 = enum.auto()
    INT8 = enum.auto()
    INT32 = enum.auto()
    INT64 = enum.auto()
    ENTITY_ID = enum.auto()
    PLAYER_ID = enum.auto()
    # full precision (self-describing, decodable to real values)
    PLAIN_FLOAT32 = enum.auto()
    PLAIN_VEC3 = enum.auto()
    # quantized (raw bits only — per-type constant not in the file)
    FIXED32 = enum.auto()
    BOUNDED32 = enum.auto()
    FIXED_VEC3 = enum.auto()
    FIXED_QUAT = enum.auto()
    NORMALIZED_QUAT = enum.auto()
    # variable-length / structured (framing only)
    STRING = enum.auto()
    ENUM_POOL = enum.auto()  # *Compressor name/enum pools
    COMPOSITE = enum.auto()  # composites, vectors, maps, etc.
    UNKNOWN = enum.auto()

    @property
    def bit_width(self) -> int | None:
        """Fixed bit width of the encoding, or ``None`` if variable / unknown."""
        return _BIT_WIDTH.get(self)

    @property
    def is_decodable(self) -> bool:
        """True if the type can be decoded to a real value from the type alone.

        Excludes the quantized types (need a per-type constant) and the
        variable/structured types (need field-layout context).
        """
        return self in _DECODABLE


_BIT_WIDTH: dict[WireType, int] = {
    WireType.BOOL: 1,
    WireType.UINT8: 8,
    WireType.INT8: 8,
    WireType.UINT16: 16,
    WireType.UINT32: 32,
    WireType.INT32: 32,
    WireType.ENTITY_ID: 32,
    WireType.PLAYER_ID: 32,
    WireType.PLAIN_FLOAT32: 32,
    WireType.FIXED32: 32,
    WireType.BOUNDED32: 32,
    WireType.UINT64: 64,
    WireType.INT64: 64,
    WireType.PLAIN_VEC3: 96,
    WireType.FIXED_VEC3: 96,
}

_DECODABLE: frozenset[WireType] = frozenset(
    {
        WireType.BOOL,
        WireType.UINT8,
        WireType.UINT16,
        WireType.UINT32,
        WireType.UINT64,
        WireType.INT8,
        WireType.INT32,
        WireType.INT64,
        WireType.ENTITY_ID,
        WireType.PLAYER_ID,
        WireType.PLAIN_FLOAT32,
        WireType.PLAIN_VEC3,
    }
)

# Exact type-name -> WireType. Covers every type seen in the live schemas.
_EXACT: dict[str, WireType] = {
    "CBool": WireType.BOOL,
    "CUint8": WireType.UINT8,
    "CUint16": WireType.UINT16,
    "CUint32": WireType.UINT32,
    "CUint64": WireType.UINT64,
    "CInt8": WireType.INT8,
    "CInt32": WireType.INT32,
    "CInt64": WireType.INT64,
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

_COMPRESSOR_RE = re.compile(r"Compressor>?$")
_COMPOSITE_RE = re.compile(r"(Composite|vector<|map<|Map[A-Z])")


def classify(type_name: str | None) -> WireType:
    """Map a schema wire-type name to a :class:`WireType` (``UNKNOWN`` if unrecognised)."""
    if not type_name:
        return WireType.UNKNOWN
    wt = _EXACT.get(type_name)
    if wt is not None:
        return wt
    if _COMPRESSOR_RE.search(type_name):
        return WireType.ENUM_POOL
    if _COMPOSITE_RE.search(type_name):
        return WireType.COMPOSITE
    return WireType.UNKNOWN


def decode(wire_type: WireType, rs: ReadStream):
    """Decode one value of ``wire_type`` from ``rs`` at the current bit cursor.

    Returns a typed value for :attr:`WireType.is_decodable` types. For the quantized types
    (FIXED32 / BOUNDED32 / FIXED_VEC3 / quats) it returns the *raw bits* (an int, or a
    tuple of ints) since the scale/bound needed to produce real units is not in the file.
    Raises :class:`ValueError` for variable-length / structured / unknown types, which
    cannot be decoded without field-layout context.
    """
    if wire_type is WireType.BOOL:
        return rs.read_bool()
    if wire_type in (WireType.UINT8, WireType.UINT16, WireType.UINT32, WireType.UINT64,
                     WireType.ENTITY_ID, WireType.PLAYER_ID):
        return rs.read_bits(wire_type.bit_width)
    if wire_type in (WireType.INT8, WireType.INT32, WireType.INT64):
        return rs.read_int(wire_type.bit_width)
    if wire_type is WireType.PLAIN_FLOAT32:
        return rs.read_float32()
    if wire_type is WireType.PLAIN_VEC3:
        return (rs.read_float32(), rs.read_float32(), rs.read_float32())
    # quantized: raw bits only (per-type constant not in the file)
    if wire_type in (WireType.FIXED32, WireType.BOUNDED32):
        return rs.read_bits(32)
    if wire_type is WireType.FIXED_VEC3:
        return (rs.read_bits(32), rs.read_bits(32), rs.read_bits(32))
    raise ValueError(f"{wire_type.name} is not decodable from the type alone")
