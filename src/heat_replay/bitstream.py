"""Bit-level reader for the packed sections of the ``.replay`` format.

The replication baseline-cache (tag-6 blobs) and property deltas (tag-8) are stored as a bit
stream: a 64-bit little-endian-first accumulator over an array of u64 words. This reader is the
foundation for decoding those sections.

Experimental: ``read_baseline_header`` decodes the baseline-cache header; decoding the per-property
value bodies that follow is not yet implemented.
"""

from __future__ import annotations

import struct


class ReadStream:
    """64-bit LSB-first bit reader."""

    def __init__(self, data: bytes):
        pad = (-len(data)) % 8
        buf = data + b"\x00" * pad
        self._words = list(struct.unpack(f"<{len(buf) // 8}Q", buf)) or [0]
        self.size_bits = len(data) * 8
        self.bits_read = 0
        self._wi = 0  # word index
        self._bo = 0  # bit offset within the current accumulator
        self._acc = self._words[0]

    def read_bits(self, n: int) -> int:
        """Read ``n`` bits (1..64), LSB-first. Raises on overrun."""
        if not 1 <= n <= 64:
            raise ValueError(f"read_bits: n={n} out of [1,64]")
        self.bits_read += n
        if self.size_bits < self.bits_read:
            raise EOFError(f"bit-stream overrun: {self.bits_read} > {self.size_bits}")
        off = self._bo
        if off + n <= 64:
            val = (self._acc >> off) & ((1 << n) - 1)
            self._bo = off + n
        else:  # spans into the next 64-bit word
            low = (self._acc >> off) if off < 64 else 0
            self._wi += 1
            self._acc = self._words[self._wi]
            rem = n - (64 - off)
            self._bo = rem
            val = (low | ((self._acc & ((1 << rem) - 1)) << (64 - off))) & ((1 << n) - 1)
        return val

    def read_uint(self, n: int = 32) -> int:
        return self.read_bits(n)

    def read_bool(self) -> bool:
        return self.read_bits(1) != 0

    def read_int(self, n: int = 32) -> int:
        """Read an ``n``-bit two's-complement signed integer."""
        v = self.read_bits(n)
        return v - (1 << n) if v & (1 << (n - 1)) else v

    def read_float32(self) -> float:
        """Read a 32-bit IEEE-754 float (the bits, reinterpreted)."""
        return struct.unpack("<f", struct.pack("<I", self.read_bits(32)))[0]

    def skip(self, n: int) -> None:
        """Advance the read cursor by ``n`` bits."""
        while n > 0:
            take = min(n, 64)
            self.read_bits(take)
            n -= take


# Width (in bits) of a component id in the baseline-cache header.
COMPONENT_ID_WIDTH = 7


def read_baseline_header(data: bytes, component_id_width: int = COMPONENT_ID_WIDTH) -> dict:
    """Decode the header of a baseline-cache blob.

    Returns ``{field0, flag, set1, set2, entity_count, header_bits}``. Decoding the per-component
    property bodies that follow is not yet implemented.
    """
    rs = ReadStream(data)
    field0 = rs.read_uint(32)
    flag = rs.read_bool()

    def unique_set() -> list[int] | None:
        if rs.read_bool():  # "all" flag
            return None
        count = rs.read_uint(32)
        if count > 1000:
            raise ValueError(f"implausible set count {count} (corrupt blob or wrong component_id_width)")
        return [rs.read_bits(component_id_width) for _ in range(count)]

    set1 = unique_set()
    set2 = unique_set()
    entity_count = rs.read_uint(32)
    return {
        "field0": field0,
        "flag": flag,
        "set1": set1,
        "set2": set2,
        "entity_count": entity_count,
        "header_bits": rs.bits_read,
    }
