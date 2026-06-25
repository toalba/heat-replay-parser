"""Per-frame (tag-4) replication channel: stateful body walk, entity identity, and positions.

The tag-4 record stream is the game's per-tick replication channel. Each packet carries a fixed
header followed by a **self-delimiting per-entity message stream**: for each entity an id selector,
a message type, then (for full-sync / delta / add messages) a component journal. The journal is a
run of components — each an explicit component id or a small back-reference into a recently-seen
cache — and for the components that carry data, one presence bit per property with the changed
property values inline.

This module implements that walk as a parameterized engine. The *framing* (header width, id codec,
message-type and component-mode prefixes, the component back-reference cache, the per-property
presence bits) is fully determined by the bitstream and is encoded here directly. The *value sizes*
— how many bits each (component, property) value occupies, and how many properties each component
has — are **not** in the replay file; they are a property of the build and must be supplied by the
caller as ``property_counts`` and ``value_widths`` tables. Without them the walk can frame the
entity-id stream but cannot traverse component bodies (there is no per-component length prefix), so
value decode is gated on those tables. Position is the one value codec whose layout is known: a
packed-scalar 3-vector, decoded via :mod:`heat_replay.packed_scalar`.

What is decoded here is validated by construction: the component back-reference cache is a persistent
move-to-front list of the four most-recently-seen components carried across packets (a per-packet
reset does not fit the data), and where per-property widths are supplied the walk consumes exactly
the bits they predict. Entity identity across packets uses the id codec resolved against the prior
packet's id-list (:class:`EntityIdResolver`); it is correct wherever that prior list is complete,
which in turn needs a complete body walk — so dense per-entity trajectories scale with value-width
coverage. All codec logic has synthetic round-trip tests (``tests/test_replication.py``).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Iterator

from heat_replay import stream, wire_value
from heat_replay.bitstream import ReadStream
from heat_replay.packed_scalar import ScalarParams, read_vec3

# --- structural constants (read directly from the bitstream layout) ----------------------------
_HEADER_BITS = 48                 # fixed per-packet header (sequence id / tick / reference field)
_NID_BITS = 7                     # explicit component id width
_ENTITY_DELTA_W = (4, 8, 16, 24)  # 2-bit selector -> entity-id delta width
_FRAME_DELTA_W = (3, 6, 12, 32)   # 2-bit selector -> frame-id delta width
# The moving transform's position is a packed-scalar 3-vector; this tier table / selector width is
# the one fixed across the samples (validated against externally-supplied per-property widths and
# against a decoded coordinate landing on a known map position).
POSITION = ScalarParams((12, 16, 20, 24), 12, 1.0)
_CACHE_SIZE = 4
_MAX_NEST = 8                     # recursion bound for nested (variant) record bodies


class Blocked(Exception):
    """Raised when the walk reaches a value whose size is not supplied (it cannot continue past an
    unknown-width property in the same packet — there is no per-component length prefix)."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


# --- entity identity ---------------------------------------------------------------------------
class EntityIdResolver:
    """Resolves the per-entity id selector against the prior packet's id-list.

    Selector: ``1`` Get (next id from the reference list at the cursor); ``01`` MoveAndGet (8-bit
    index, move cursor there, take that id); ``00`` Delta (sign + 2-bit selector + width bits, id =
    prev +/- delta). Resolved ids accumulate into this packet's list, which becomes the reference
    for the next packet. Bit alignment is always preserved; a Get/MoveAndGet beyond the (possibly
    truncated) reference list yields ``None`` without desyncing the stream, and ``prev`` only
    advances on a known id so the delta chain stays anchored to the last known value.
    """

    def __init__(self) -> None:
        self.ref: list[int] = []
        self.cur_list: list[int | None] = []
        self.cursor = 0
        self.prev = 0

    def begin_packet(self) -> None:
        if self.cur_list:
            # keep only resolved ids as the reference (Nones cannot be indexed against)
            self.ref = [x for x in self.cur_list if x is not None]
        self.cur_list = []
        self.cursor = 0
        self.prev = 0

    def read(self, rs: ReadStream) -> tuple[int | None, str]:
        if rs.read_bits(1) == 1:                       # Get
            mode = "get"
            eid = self.ref[self.cursor] if self.cursor < len(self.ref) else None
            self.cursor += 1
        elif rs.read_bits(1) == 1:                     # MoveAndGet
            mode = "mag"
            idx = rs.read_bits(8)
            self.cursor = idx
            eid = self.ref[idx] if idx < len(self.ref) else None
            self.cursor += 1
        else:                                          # Delta
            mode = "delta"
            sign = rs.read_bits(1)
            width = _ENTITY_DELTA_W[rs.read_bits(2)]
            dv = rs.read_bits(width) if width else 0
            eid = self.prev - dv if sign else self.prev + dv
        self.cur_list.append(eid)
        if eid is not None:
            self.prev = eid
        return eid, mode


# --- bitstream sub-readers (structure only) ----------------------------------------------------
def _read_msgtype(rs: ReadStream) -> int:
    if rs.read_bits(1):
        return 0
    if rs.read_bits(1):
        return 1
    return 2 if rs.read_bits(1) else 3


def _read_comp_mode(rs: ReadStream) -> int:   # "1"->0 delta, "01"->3 stop, "001"->1 add, "000"->2 remove
    if rs.read_bits(1):
        return 0
    if rs.read_bits(1):
        return 3
    return 1 if rs.read_bits(1) else 2


def _read_frame(rs: ReadStream) -> None:
    if rs.read_bits(1):
        return
    width = (3, 7)[rs.read_bits(1)]
    if width:
        rs.read_bits(width)


def _read_delta_frame(rs: ReadStream, branch_on: int) -> None:
    if rs.read_bits(1) == branch_on:
        width = _FRAME_DELTA_W[rs.read_bits(2)]
        if width:
            rs.read_bits(width)


def _read_spawn_string(rs: ReadStream) -> None:
    # A 16-bit marker; the sentinel value introduces an inline length-prefixed string (a 16-bit
    # length then that many bytes, with no byte alignment); any other value is a prefab handle.
    if (rs.read_bits(16) & 0xFFFF) == 0xFFFF:
        n = rs.read_bits(16)
        rs.skip(n * 8)


def _read_static_data(rs: ReadStream) -> int:
    """Consume the per-entity static-data block and return its 2-bit type code (which selects the
    full-sync body framing in :meth:`FrameWalker._packet`)."""
    typ = rs.read_bits(2)
    rs.read_bits(1)
    rs.read_bits(8)
    if typ in (0, 1):
        if rs.read_bits(1):
            rs.read_bits(24)
    if typ in (1, 2):
        _read_spawn_string(rs)
        rs.read_bits(16)
    return typ


def _consume(rs: ReadStream, width: int) -> None:
    for _ in range(width // 32):
        rs.read_bits(32)
    if width % 32:
        rs.read_bits(width % 32)


# --- the walk ----------------------------------------------------------------------------------
class FrameWalker:
    """Walks the tag-4 per-frame channel.

    ``property_counts``: ``{component_id: number_of_properties}`` (required to traverse bodies).
    ``value_widths``: ``{(component_id, property_index): fixed_bit_width}`` for non-position values
    (optional; takes priority over ``field_types``). ``field_types``:
    ``{(component_id, property_index): wire_type_name}`` from the embedded schema — each value is
    then consumed by its codec via :mod:`heat_replay.wire_value` (self-delimiting types stay
    byte-exact without an explicit width). ``enum_widths``: ``{wire_type_name: bit_width}`` for
    enumeration / name-pool types whose width is build-specific. A property whose width/codec is
    unknown blocks that packet's remaining walk. The moving transform component is identified by
    ``position_component`` (its property 0 = packed position, property 1 = packed rotation).
    """

    def __init__(self, property_counts: dict[int, int],
                 value_widths: dict[tuple[int, int], int] | None = None,
                 position_component: int = 14,
                 field_types: dict[tuple[int, int], str] | None = None,
                 enum_widths: dict[str, int] | None = None,
                 component_count: int | None = None) -> None:
        self.pc = dict(property_counts)
        self.vw = dict(value_widths or {})
        self.field_types = dict(field_types or {})
        self.enum_widths = dict(enum_widths or {})
        self.pos_nid = position_component
        # The number of valid component ids (ids are 0..component_count-1). The id field is wider
        # than strictly needed, so a *correctly aligned* walk never reads an id at or above this;
        # an id >= component_count therefore means a prior value codec mis-consumed and the cursor
        # drifted. The fraction of such reads is a build-independent desync rate (see ``stats``).
        self.n_comps = component_count
        self.cur_slot: int | None = None
        self.cache: list[int] = []
        self._depth = 0
        self.resolver = EntityIdResolver()
        self.stats = {"packets": 0, "clean": 0, "blocked": defaultdict(int), "positions": 0,
                      "nid_reads": 0, "nid_oob": 0, "entity_reads": 0, "entity_unresolved": 0}

    def _value(self, rs: ReadStream, nid: int, i: int, mode: int):
        width = self.vw.get((nid, i))
        if width is not None:
            _consume(rs, width)
            return None
        ftype = self.field_types.get((nid, i))
        if ftype is not None:
            try:
                v = wire_value.consume(ftype, rs, self.enum_widths.get(ftype), mode,
                                       nested=self._nested)
            except wire_value.Unsupported:
                raise Blocked(f"codec:{ftype}")
            return v if (nid == self.pos_nid and i == 0) else None
        if nid == self.pos_nid and i == 0:
            return read_vec3(rs, POSITION)
        if nid == self.pos_nid and i == 1:
            read_vec3(rs, POSITION)
            return None
        raise Blocked(f"value:{nid}.{i}")

    def _nested(self, tag: int, rs: ReadStream, mode: int) -> None:
        """Consume one nested replicated record — the body of a variant (tagged-record) element.

        A variant element's 32-bit tag is the component id of the concrete replicated type it
        carries, so its body is laid out exactly like a top-level component body: one presence bit
        per property, the value inline when the bit is set. This recurses through :meth:`_value`,
        reusing the same per-component layout (``property_counts`` / ``field_types``). A tag outside
        the component-id range, an unknown layout, or excessive nesting raises
        :class:`wire_value.Unsupported`, so the caller treats the variant as an unmodelled codec and
        blocks the packet (no over-read)."""
        if self.n_comps is not None and not (0 <= tag < self.n_comps):
            raise wire_value.Unsupported("variant-tag")
        n = self.pc.get(tag)
        if n is None:
            raise wire_value.Unsupported("variant-class")
        if self._depth >= _MAX_NEST:
            raise wire_value.Unsupported("variant-depth")
        self._depth += 1
        try:
            for i in range(n):
                if rs.read_bits(1):
                    self._value(rs, tag, i, mode)
        except Blocked:
            # A nested-record property with no modelled codec: re-surface as Unsupported so the
            # caller attributes the block to the variant field itself (per this method's contract),
            # rather than letting the inner property's Blocked escape uncaught.
            raise wire_value.Unsupported("variant-body") from None
        finally:
            self._depth -= 1

    def _body(self, rs: ReadStream, out: list) -> None:
        mode = _read_comp_mode(rs)
        guard = 0
        while mode != 3 and guard < 128:
            guard += 1
            if rs.read_bits(1) == 0:
                nid = rs.read_bits(_NID_BITS)
                self.stats["nid_reads"] += 1
                if self.n_comps is not None and nid >= self.n_comps:
                    self.stats["nid_oob"] += 1
            else:
                idx = rs.read_bits(2)
                if idx >= len(self.cache):
                    raise Blocked("cache-miss")
                nid = self.cache[idx]
            if nid in self.cache:
                self.cache.remove(nid)
            self.cache.insert(0, nid)
            del self.cache[_CACHE_SIZE:]
            if mode in (0, 1):
                n = self.pc.get(nid)
                if n is None:
                    raise Blocked(f"count:{nid}")
                for i in range(n):
                    if rs.read_bits(1):
                        v = self._value(rs, nid, i, mode)
                        if nid == self.pos_nid and i == 0 and v is not None:
                            out.append((self.cur_slot, mode, v))
                            self.stats["positions"] += 1
            mode = _read_comp_mode(rs)

    def _packet(self, packet: bytes, out: list) -> None:
        # The packet header carries a recycled per-entity occupancy slot (a little-endian u16 at
        # byte 4); it is read directly and never fails, so it is the reliable identity to which the
        # decoded positions are attributed. The per-entity-message id codec is still consumed (it is
        # part of the framing) but its resolved value is not used to key positions.
        self.cur_slot = (packet[4] | (packet[5] << 8)) if len(packet) >= 6 else None
        rs = ReadStream(packet)
        rs.read_bits(_HEADER_BITS)
        self.resolver.begin_packet()
        guard = 0
        while rs.bits_read + 8 < rs.size_bits and guard < 256:
            guard += 1
            eid, _ = self.resolver.read(rs)
            self.stats["entity_reads"] += 1
            if eid is None:
                self.stats["entity_unresolved"] += 1
            mt = _read_msgtype(rs)
            if mt == 0:
                self._body(rs, out)
            elif mt == 1:
                _read_delta_frame(rs, 0)
                _read_delta_frame(rs, 1)
                rs.read_bits(1)
                self._body(rs, out)
            elif mt == 2:
                _read_frame(rs)
                _read_static_data(rs)
                rs.read_bits(1)
                self._body(rs, out)
            else:
                rs.read_bits(1)

    def walk_packets(self, packets: Iterable[bytes]) -> Iterator[tuple]:
        """Yield ``(slot, mode, position)`` for every position decoded. ``slot`` is the packet's
        recycled per-entity occupancy id (read directly from the header — the reliable identity);
        ``mode`` is 1 (add / absolute) or 0 (update / delta)."""
        for packet in packets:
            self.stats["packets"] += 1
            out: list = []
            try:
                self._packet(packet, out)
                self.stats["clean"] += 1
            except Blocked as exc:
                self.stats["blocked"][exc.why] += 1
            except Exception as exc:  # defensive: a malformed packet must not abort the stream
                self.stats["blocked"]["error:" + type(exc).__name__] += 1
            for row in out:
                yield row


def integrate_trajectories(events: Iterable[tuple]) -> dict:
    """Turn ``(slot, mode, position)`` events into per-slot position tracks. Seeds absolute from
    add-frames (mode 1), accumulates update-frame deltas (mode 0); a slot first seen on an update is
    tracked relative to that first sample. Returns ``{slot: [position, ...]}`` (events with a
    ``None`` key are dropped)."""
    pos: dict[int, list[float]] = {}
    tracks: dict[int, list[tuple]] = defaultdict(list)
    for eid, mode, p in events:
        if eid is None:
            continue
        if eid not in pos:
            pos[eid] = [p[0], p[1], p[2]]
        elif mode == 1:
            pos[eid] = [p[0], p[1], p[2]]
        else:
            pos[eid] = [pos[eid][k] + p[k] for k in range(3)]
        tracks[eid].append(tuple(pos[eid]))
    return dict(tracks)


def _dist(a, b) -> float:
    return sum((a[k] - b[k]) ** 2 for k in range(3)) ** 0.5


def coherent_tracks(events: Iterable[tuple], max_step: float = 150.0,
                    min_points: int = 3) -> list[list[tuple]]:
    """Split the ``(slot, mode, position)`` event stream into physically-coherent track segments.

    Each position is attributed to its packet's occupancy slot — a reliable identity, but one that
    is **recycled**: a slot freed by one entity is later reclaimed by another, so accumulating a
    slot's deltas into a single track can stitch together more than one physical occupant and
    produce implausible jumps. This integrates each slot's events (seed absolute on a mode-1 add,
    accumulate mode-0 deltas) but starts a **new segment** whenever (a) an absolute re-seed (mode 1)
    arrives or (b) a single step exceeds ``max_step`` metres — a one-tick jump that large is slot
    reuse, not motion. The result is a list of segments (each a list of positions), only those with
    at least ``min_points`` points; events with a ``None`` key are dropped. ``max_step`` is the only
    tunable and is a plain physical-plausibility bound, not a decoded quantity.

    A segment that begins on an update with no preceding add (a slot's first sample, or the sample
    right after a break) is tracked **relative** to that first sample — its shape is correct but its
    absolute offset is unknown until an add anchors it.

    Returns coherent, directly-plottable tracks; their number and length scale with how completely
    each packet is walked (more decoded positions per slot → longer tracks)."""
    by: dict[int, list[tuple]] = defaultdict(list)
    for eid, mode, p in events:
        if eid is not None:
            by[eid].append((mode, p))
    segments: list[list[tuple]] = []
    for seq in by.values():
        seg: list[tuple] = []
        cur: list[float] | None = None
        for mode, p in seq:
            if mode == 1:                                # add: absolute (re-)seed -> flush, restart
                if len(seg) >= min_points:
                    segments.append(seg)
                cur = [p[0], p[1], p[2]]
                seg = [tuple(cur)]
                continue
            if cur is None:                              # no anchor yet (track start, or the sample
                cur = [p[0], p[1], p[2]]                 # right after a break): begin a segment
                seg = [tuple(cur)]                       # tracked relative to this first sample
                continue                                 # (seg is always empty here, so no flush)
            nxt = [cur[k] + p[k] for k in range(3)]
            if _dist(cur, nxt) > max_step:               # implausible one-tick jump: break the track
                if len(seg) >= min_points:               # (the next sample re-anchors a new segment)
                    segments.append(seg)
                seg, cur = [], None
                continue
            cur = nxt
            seg.append(tuple(cur))
        if len(seg) >= min_points:
            segments.append(seg)
    return segments
