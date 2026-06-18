"""Record-stream parser for HEAT ``.replay`` files.

The stream after ``Container.stream_start`` is a flat sequence of records, parsed to 100% byte
coverage (clean EOF) on every sample::

    record = tag:u8  frameId:u32-LE  body

``body`` depends on ``tag`` (the event-type discriminator):

- ``0x0a`` — reflected event (PlayerInput, MainSeed, ClientShoot, …): ``event_id:u8`` then, the
  first time an id is seen, a length-prefixed ``type_name`` registering id→name; then
  ``size:varint`` and ``size`` bytes of serialized blob.
- ``0x06`` — entity spawn / component baseline: ``prefab`` (length-prefixed string), ``u32``
  entity id, ``size:varint``, ``size`` bytes of component-baseline blob.
- ``3,4,5,7,8`` — network-packet list: 8-byte header, ``count:varint``, then ``count`` packets
  each ``size:varint`` (≤0x4000) + bytes.
- ``9`` — ``size:varint`` + blob.   ``1,2`` — 1 metadata byte.

Lengths use a tagged varint: ``n = (b0 & 3) + 1`` bytes, ``value = little-endian >> 2``.
Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from heat_replay.container import read_bytes

# Tag values (the per-record event-type discriminator).
TAG_META_A, TAG_META_B = 1, 2
TAG_PACKET_LIST = frozenset({3, 4, 5, 7, 8})
TAG_BASELINE = 0x06        # entity spawn / component baseline (setup/keyframe)
TAG_BLOB = 9
TAG_EVENT = 0x0A           # reflected custom event

_ASSET_RE = re.compile(rb"/[A-Za-z0-9_]+(?:/[A-Za-z0-9_.+]+)+\.[A-Za-z0-9_]+")


@dataclass
class Record:
    tag: int
    frame_id: int
    offset: int                     # absolute file offset of the record
    # tag 0x0a (reflected event):
    event_id: int | None = None
    event_name: str | None = None   # resolved from the id→name registry
    # tag 0x06 (entity baseline):
    prefab: str | None = None
    entity_id: int | None = None
    # bodies:
    blob: bytes | None = None       # tags 0x0a / 0x06 / 9
    packets: list[bytes] | None = None  # tags 3,4,5,7,8
    header8: bytes | None = None    # tags 3,4,5,7,8


@dataclass
class PropertyDelta:
    """One property delta from a tag-8 replication packet.

    Packet layout: ``01 + handle:u32-LE + value``. ``handle`` identifies the property; ``value`` is
    the raw delta bytes. Decoding ``value`` into a typed scalar/vector is not implemented — this
    layer gives the framing: which property changed, on which frame, and the raw bytes.
    """

    frame_id: int
    handle: int
    value: bytes


@dataclass(repr=False)
class StreamWalk:
    stream_start: int
    end: int
    clean_eof: bool
    event_types: dict[int, str] = field(default_factory=dict)  # event_id -> name
    seed: int | None = None
    records: list[Record] = field(default_factory=list)

    def __repr__(self) -> str:  # avoid dumping ~100k records in a REPL
        seed = f"0x{self.seed:08x}" if self.seed is not None else None
        return (
            f"StreamWalk(records={len(self.records)}, clean_eof={self.clean_eof}, "
            f"event_types={self.event_types}, seed={seed})"
        )


def read_len(raw: bytes, i: int) -> tuple[int, int]:
    """Tagged varint: ``n = (b0 & 3) + 1`` bytes, ``value = little-endian >> 2``."""
    if i >= len(raw):
        raise ValueError(f"truncated varint: offset 0x{i:x} past EOF")
    nbytes = (raw[i] & 0b11) + 1
    if i + nbytes > len(raw):
        raise ValueError(f"truncated varint at 0x{i:x}: need {nbytes} bytes, {len(raw) - i} remain")
    return int.from_bytes(raw[i : i + nbytes], "little") >> 2, i + nbytes


def _read_str(raw: bytes, i: int) -> tuple[str, int]:
    n, i = read_len(raw, i)
    return raw[i : i + n].decode("ascii", "replace"), i + n


def walk(path: str) -> StreamWalk:
    """Parse the record stream of a replay file (convenience wrapper over :func:`parse_stream`).

    Reads the file once.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    c = read_bytes(raw, path)
    if c.is_dead or c.stream_start is None:
        raise ValueError(f"{path}: dead or unreadable replay")
    return parse_stream(raw, c.stream_start)


def parse_stream(raw: bytes, stream_start: int) -> StreamWalk:
    """Parse the record stream from an in-memory buffer. Exact (not heuristic); asserts clean EOF."""
    n = len(raw)
    i = stream_start

    names: dict[int, str] = {}
    seed: int | None = None
    records: list[Record] = []

    while i < n:
        start = i
        if n - i < 5:  # need tag(1) + frameId(u32)
            raise ValueError(f"truncated record at 0x{i:x}: only {n - i} bytes remain")
        tag = raw[i]
        i += 1
        frame_id = int.from_bytes(raw[i : i + 4], "little")
        i += 4
        rec = Record(tag=tag, frame_id=frame_id, offset=start)

        if tag == TAG_EVENT:
            if i >= n:
                raise ValueError(f"truncated event record at 0x{start:x}: missing event_id")
            rec.event_id = raw[i]
            i += 1
            if rec.event_id not in names:
                names[rec.event_id], i = _read_str(raw, i)
            rec.event_name = names[rec.event_id]
            size, i = read_len(raw, i)
            rec.blob = raw[i : i + size]
            i += size
            if seed is None and rec.event_name.startswith("cw::MainSeedReplayEvent") and len(rec.blob) >= 4:
                seed = int.from_bytes(rec.blob[:4], "little")

        elif tag == TAG_BASELINE:
            rec.prefab, i = _read_str(raw, i)
            if i + 4 > n:
                raise ValueError(f"truncated baseline record at 0x{start:x}: missing entity id")
            rec.entity_id = int.from_bytes(raw[i : i + 4], "little")
            i += 4
            size, i = read_len(raw, i)
            rec.blob = raw[i : i + size]
            i += size

        elif tag in TAG_PACKET_LIST:
            rec.header8 = raw[i : i + 8]
            i += 8
            count, i = read_len(raw, i)
            pkts = []
            for _ in range(count):
                psize, i = read_len(raw, i)
                pkts.append(raw[i : i + psize])
                i += psize
            rec.packets = pkts

        elif tag == TAG_BLOB:
            size, i = read_len(raw, i)
            rec.blob = raw[i : i + size]
            i += size

        elif tag in (TAG_META_A, TAG_META_B):
            rec.blob = raw[i : i + 1]
            i += 1

        else:
            raise ValueError(f"unknown record tag 0x{tag:02x} at offset 0x{start:x}")

        if i > n:
            raise ValueError(f"record at 0x{start:x} (tag 0x{tag:02x}) overran EOF")
        records.append(rec)

    return StreamWalk(
        stream_start=stream_start,
        end=i,
        clean_eof=(i == n),
        event_types=names,
        seed=seed,
        records=records,
    )


# --- convenience views -------------------------------------------------------

def events(w: StreamWalk) -> list[Record]:
    """Reflected custom-event records (tag 0x0a)."""
    return [r for r in w.records if r.tag == TAG_EVENT]


def baselines(w: StreamWalk) -> list[Record]:
    """Entity spawn / component-baseline records (tag 0x06)."""
    return [r for r in w.records if r.tag == TAG_BASELINE]


def property_deltas(w: StreamWalk) -> list[PropertyDelta]:
    """Decode tag-8 replication packets into property deltas (frame, handle, raw value).

    Each tag-8 packet is ``01 + handle:u32 + value``; packets that don't match are skipped.
    Decoding the raw ``value`` into typed data is not implemented — see :class:`PropertyDelta`.
    """
    out: list[PropertyDelta] = []
    for r in w.records:
        if r.tag != 8 or not r.packets:
            continue
        for p in r.packets:
            if len(p) >= 5 and p[0] == 1:
                out.append(PropertyDelta(r.frame_id, int.from_bytes(p[1:5], "little"), p[5:]))
    return out


def referenced_assets(w: StreamWalk) -> list[str]:
    """All distinct asset paths referenced across the stream (sorted).

    Sources: tag-0x06 baseline ``prefab`` fields (the spawned entities) plus any path strings
    embedded in record blobs / network packets (abilities, effects spawned mid-match).
    """
    out: set[str] = set()

    def scan(buf: bytes) -> None:
        for m in _ASSET_RE.finditer(buf):
            if m.group().count(b"/") >= 2:
                out.add(m.group().decode("ascii", "replace"))

    for r in w.records:
        if r.prefab and r.prefab.count("/") >= 2:
            out.add(r.prefab)
        if r.blob:
            scan(r.blob)
        if r.packets:
            for p in r.packets:
                scan(p)
    return sorted(out)
