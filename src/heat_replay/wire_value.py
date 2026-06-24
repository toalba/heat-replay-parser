"""Per-field value consumption for the replication journal, dispatched by a field's
schema-declared wire type.

The per-frame journal stores, for each present property, a value encoded by that property's
codec. Most codecs are **self-delimiting** — the bits they consume are determined as they are
read (a selector picks a width; a length prefix precedes bytes) — so a value can be skipped to
stay byte-exact even when its semantic meaning is not needed. :func:`consume` advances the read
cursor by exactly one value of the given type and returns the decoded position for the one type
that carries one (the packed 3-vector), ``None`` otherwise.

The field type strings are the schema's own (e.g. ``CBool``, ``CUint32``, ``CFixedVec3``,
``CEntityNetworkId``, ``CStdString``) — the same vocabulary :mod:`heat_replay.wiretypes` maps.
Codecs not yet modelled (length-delimited vectors, nested replication sub-schemes, name-pool
enumerations whose width is build-specific) raise :class:`Unsupported`; the caller supplies a
width for enumerations via ``enum_width``. All codecs here have round-trip unit tests.
"""
from __future__ import annotations

from heat_replay.bitstream import ReadStream
from heat_replay.packed_scalar import ScalarParams, read_scalar, read_vec3

# Fixed-width primitives: schema type name -> bit width.
_FIXED = {
    "CBool": 1,
    "CUint8": 8, "CUint16": 16, "CUint32": 32, "CUint64": 64,
    "CInt8": 8, "CInt16": 16, "CInt32": 32, "CInt64": 64,
    "CPlainFloat32": 32, "CPlainFloat64": 64,
    "CPlayerId": 8,
}

# The standard packed-scalar tier table (selector picks one of these mantissa widths). Validated
# against the moving transform's position, whose decoded coordinate lands on a known map point.
_STD = ScalarParams(tier_table=(12, 16, 20, 24), exp_base=12, scale=1.0)

# Entity-reference delta widths: a 2-bit selector indexes this table.
_ENTITY_REF_W = (4, 8, 16, 24)

# Array/vector length packer: a 2-bit selector indexes this tier table, then that many bits hold
# the value directly (the smallest tier that fits is used). The same packer encodes a vector's
# element count, the index of the first changed element, and the gaps between changed elements.
_ARRAY_LEN_W = (1, 4, 8, 16)

# A variable-length byte store ("variable storage") is a length-prefixed byte vector: it uses the
# array length packer for the count and 8 bits per element, like ``vector<CUint8>``.
_VARIABLE_STORAGE = "cw::VariableStorageCompressor"


class Unsupported(Exception):
    """Raised for a field type whose codec is not modelled (so its bit length is unknown and the
    walk cannot continue past it without desyncing)."""

    def __init__(self, field_type: str | None):
        self.field_type = field_type
        super().__init__(field_type or "<nested>")


def _consume_bits(rs: ReadStream, width: int) -> None:
    for _ in range(width // 32):
        rs.read_bits(32)
    if width % 32:
        rs.read_bits(width % 32)


def _consume_entity_ref(rs: ReadStream) -> None:
    """An entity reference: ``1`` take-from-list (no extra bits); ``01`` index (8 bits); ``00``
    signed delta (1 sign bit + 2-bit selector + a tier-width magnitude). Self-delimiting."""
    if rs.read_bits(1) == 1:                       # take from reference list at cursor
        return
    if rs.read_bits(1) == 1:                       # absolute index into the list
        rs.read_bits(8)
        return
    rs.read_bits(1)                                # sign
    width = _ENTITY_REF_W[rs.read_bits(2)]
    if width:
        rs.read_bits(width)


def _read_array_len(rs: ReadStream) -> int:
    """Read an array-length-packed integer: a 2-bit selector picks a tier width from
    :data:`_ARRAY_LEN_W`, then that many bits carry the value directly."""
    width = _ARRAY_LEN_W[rs.read_bits(2)]
    return rs.read_bits(width) if width else 0


# Element compressors that read a fixed run of bits per element. A game-item reference is a fixed
# 16-byte (128-bit) identifier read verbatim.
_ELEMENT_FIXED_BITS = {"CGameItemCompressor": 128}


def _element_codec(inner: str):
    """Return a callable consuming one element of an array whose element type is ``inner``, or
    ``None`` if that element type's codec is not modelled. ``inner`` may be namespaced."""
    name = inner.split("::")[-1]
    if name in _FIXED:
        width = _FIXED[name]
        return lambda rs: _consume_bits(rs, width)
    if name in _ELEMENT_FIXED_BITS:
        width = _ELEMENT_FIXED_BITS[name]
        return lambda rs: _consume_bits(rs, width)
    if name == "CEntityNetworkId":
        return _consume_entity_ref
    if name in ("CFixed32", "CBounded32"):
        return lambda rs: read_scalar(rs, _STD)
    if name == "CFixedVec3":
        return lambda rs: read_vec3(rs, _STD)
    return None


def _consume_array(rs: ReadStream, element, mode: int) -> None:
    """Consume one length-prefixed array. ``element`` consumes a single element.

    Two wire forms, selected by the component update ``mode`` (1 = add / absolute, 0 = update /
    delta) — both self-delimiting:

    * **absolute**: ``count`` (array-length packed) followed by ``count`` elements.
    * **delta**: a sign bit, a packed count delta, a packed first-changed-index (``+1``; ``0`` means
      no changes), then for each changed element its value followed by a packed gap to the next
      changed index (a ``0`` gap terminates), then the grown tail — ``count_delta`` appended
      elements when the array grew (``sign`` clear), none otherwise. The unchanged elements are
      copied from the reference and consume no bits, so no reference state is needed to stay aligned.
    """
    if mode == 1:                                  # absolute
        for _ in range(_read_array_len(rs)):
            element(rs)
        return
    sign = rs.read_bits(1)                          # delta
    count_delta = _read_array_len(rs)
    if _read_array_len(rs) != 0:                    # first-changed-index + 1
        while True:
            element(rs)                             # changed element value
            if _read_array_len(rs) == 0:            # gap to next changed index (0 terminates)
                break
    for _ in range(count_delta if sign == 0 else 0):
        element(rs)                                 # grown tail (appended elements)


def _array_inner(field_type: str) -> str | None:
    """If ``field_type`` is an array type (``vector<INNER>``), return ``INNER``; else ``None``."""
    if field_type and field_type.startswith("vector<") and field_type.endswith(">"):
        return field_type[len("vector<"):-1]
    return None


def consume(field_type: str | None, rs: ReadStream, enum_width: int | None = None,
            mode: int = 1):
    """Consume one value of ``field_type`` from ``rs``.

    Returns the decoded ``(x, y, z)`` for a packed 3-vector (``CFixedVec3``) — the only type that
    carries a position — and ``None`` for every other type. ``enum_width``, if given, is the bit
    width used for an enumeration / name-pool type whose width is build-specific. ``mode`` is the
    component update mode (1 = add / absolute, 0 = update / delta) and selects the wire form for
    delta-capable codecs (arrays); fixed and other self-delimiting scalar codecs ignore it because
    their on-wire size is identical in both modes. Raises :class:`Unsupported` when the type's codec
    is not modelled and no ``enum_width`` is supplied.
    """
    t = field_type
    if t in _FIXED:
        _consume_bits(rs, _FIXED[t])
        return None
    if t == "CPlainVec3":
        rs.read_bits(32)
        rs.read_bits(32)
        rs.read_bits(32)
        return None
    if t == "CEntityNetworkId":
        _consume_entity_ref(rs)
        return None
    if t == "CFixedVec3":
        return read_vec3(rs, _STD)
    if t in ("CFixed32", "CBounded32"):
        read_scalar(rs, _STD)
        return None
    if t in ("CFixedQuat", "cw::CNormalizedQuat", "CNormalizedQuat"):
        # A packed quaternion transmits three components (the fourth is reconstructed); each is a
        # packed scalar with the same selector/tier framing as a packed vector component.
        read_scalar(rs, _STD)
        read_scalar(rs, _STD)
        read_scalar(rs, _STD)
        return None
    if t == "CStdString":
        n = rs.read_bits(16)
        rs.skip(n * 8)
        return None
    if t == _VARIABLE_STORAGE:                     # length-prefixed byte store == vector<CUint8>
        _consume_array(rs, lambda r: _consume_bits(r, 8), mode)
        return None
    inner = _array_inner(t) if t else None
    if inner is not None:
        element = _element_codec(inner)
        if element is None:
            raise Unsupported(t)                   # element codec not modelled yet
        _consume_array(rs, element, mode)
        return None
    if enum_width is not None:
        rs.read_bits(enum_width)
        return None
    raise Unsupported(t)
