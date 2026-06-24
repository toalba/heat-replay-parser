"""Round-trip validation of the per-frame (tag-4) replication walk.

The entity-id codec, the component-body framing, and the position decode are mutual inverses of a
crafted encoder here, so surviving encode->walk proves the read paths in isolation before they are
trusted on real replay bytes (the measure-first discipline of this repo). No fixtures needed.
"""
from __future__ import annotations

from heat_replay.bitstream import ReadStream
from heat_replay.packed_scalar import encode_scalar_bits
from heat_replay.replication import (
    POSITION,
    EntityIdResolver,
    FrameWalker,
    coherent_tracks,
    integrate_trajectories,
)


def _pack_bits(writes):
    """Pack (value, width) pairs LSB-first into bytes (matches ReadStream)."""
    acc = nbits = 0
    for val, n in writes:
        acc |= (val & ((1 << n) - 1)) << nbits
        nbits += n
    nbytes = (nbits + 7) // 8
    return acc.to_bytes(nbytes, "little") or b"\x00"


# --- entity-id resolver -------------------------------------------------------------------------
def _encode_delta(writes, prev, target):
    d = target - prev
    sign = 1 if d < 0 else 0
    mag = abs(d)
    widths = (4, 8, 16, 24)
    sel = next((i for i, w in enumerate(widths) if mag < (1 << w)), len(widths) - 1)
    writes += [(0, 1), (0, 1), (sign, 1), (sel, 2), (mag, widths[sel])]


def test_entity_id_delta_chain_roundtrip():
    for seq in ([5, 7, 6, 100, 99, 50000], [0, 1, 2, 3], [1000, 900, 1000, 1001]):
        writes = []
        prev = 0
        for t in seq:
            _encode_delta(writes, prev, t)
            prev = t
        rs = ReadStream(_pack_bits(writes))
        r = EntityIdResolver()
        r.begin_packet()
        assert [r.read(rs)[0] for _ in seq] == seq


def test_entity_id_get_and_moveandget():
    r = EntityIdResolver()
    r.ref = [11, 22, 33, 44]
    writes = [(1, 1), (1, 1), (0, 1), (1, 1), (3, 8)]  # Get, Get, MoveAndGet(idx=3)
    rs = ReadStream(_pack_bits(writes))
    assert r.read(rs)[0] == 11
    assert r.read(rs)[0] == 22
    assert r.read(rs)[0] == 44


def test_entity_id_out_of_range_keeps_alignment():
    r = EntityIdResolver()
    r.ref = [9]
    # Get (ok=9), Get (beyond -> None), then a Delta that must still decode from prev=9
    writes = [(1, 1), (1, 1)]
    _encode_delta(writes, 9, 13)
    rs = ReadStream(_pack_bits(writes))
    assert r.read(rs)[0] == 9
    assert r.read(rs)[0] is None
    assert r.read(rs)[0] == 13  # delta chain stayed anchored to the last known id


def test_packet_rollover_reference():
    r = EntityIdResolver()
    r.cur_list = [7, 8, None, 9]
    r.begin_packet()
    assert r.ref == [7, 8, 9]  # Nones dropped from the reference


# --- full body walk + position ------------------------------------------------------------------
def _vec3_bits(x, y, z):
    return encode_scalar_bits(x, POSITION) + encode_scalar_bits(y, POSITION) + encode_scalar_bits(z, POSITION)


def _craft_position_packet(entity_id, pos, pos_nid=14, ncount=4, slot=0):
    """A packet: 48-bit header (32-bit seq + 16-bit occupancy slot), one Delta entity, msgType 0,
    body = one delta-update of the position component with property 0 (position) present and the
    rest absent."""
    writes = []
    writes.append((0, 32))                       # header: sequence id
    writes.append((slot, 16))                    # header: occupancy slot (positions key on this)
    _encode_delta(writes, 0, entity_id)          # entity id (Delta from 0; still consumed)
    writes.append((1, 1))                        # msgType 0
    writes.append((1, 1))                        # comp_mode "1" -> 0 (delta body)
    writes.append((0, 1))                        # component ref: explicit id
    writes.append((pos_nid, 7))                  # component id
    writes.append((1, 1))                        # property 0 present
    writes += _vec3_bits(*pos)                   # the position value
    for _ in range(ncount - 1):
        writes.append((0, 1))                    # remaining properties absent
    writes.append((0, 1)); writes.append((1, 1)) # comp_mode "01" -> 3 stop
    return _pack_bits(writes)


def test_framewalker_decodes_position():
    target = (123.5, -44.25, 800.0)
    packet = _craft_position_packet(entity_id=42, pos=target, slot=37)
    w = FrameWalker(property_counts={14: 4})
    rows = list(w.walk_packets([packet]))
    assert rows, "no position decoded"
    slot, mode, pos = rows[0]
    assert slot == 37 and mode == 0          # position is keyed by the packet's occupancy slot
    for got, want in zip(pos, target):
        assert abs(got - want) < 1.0, f"{pos} != {target}"   # within packed-scalar quantization


def test_framewalker_blocks_on_unknown_width():
    # a component whose property width is not supplied blocks (no length prefix to skip it)
    writes = [(0, 48)]
    _encode_delta(writes, 0, 1)
    writes += [(1, 1), (1, 1), (0, 1), (50, 7), (1, 1)]  # comp 50 prop0 present, width unknown
    packet = _pack_bits(writes)
    w = FrameWalker(property_counts={50: 1})
    list(w.walk_packets([packet]))
    assert any(k.startswith("value:") for k in w.stats["blocked"])


def test_integrate_trajectories_seed_and_accumulate():
    # entity 1: add absolute (100,0,0), then two +1 x updates -> 101,102
    events = [
        (1, 1, (100.0, 0.0, 0.0)),
        (1, 0, (1.0, 0.0, 0.0)),
        (1, 0, (1.0, 0.0, 0.0)),
        (None, 0, (5.0, 5.0, 5.0)),  # unresolved id is dropped
    ]
    tracks = integrate_trajectories(events)
    assert list(tracks) == [1]
    xs = [p[0] for p in tracks[1]]
    assert xs == [100.0, 101.0, 102.0]


def test_coherent_tracks_split_on_teleport_and_reseed():
    # One recycled id: a coherent run, then a 1000 m jump (id reuse), then another run after an
    # absolute re-seed. Expect two separate coherent segments, neither containing the jump.
    events = [
        (7, 0, (0.0, 0.0, 0.0)),     # first sample -> seed
        (7, 0, (10.0, 0.0, 0.0)),    # +10
        (7, 0, (10.0, 0.0, 0.0)),    # +10 -> (20,0,0)
        (7, 0, (1000.0, 0.0, 0.0)),  # +1000 jump -> break
        (7, 1, (500.0, 0.0, 0.0)),   # absolute re-seed (new occupant)
        (7, 0, (5.0, 0.0, 0.0)),     # +5
        (7, 0, (5.0, 0.0, 0.0)),     # +5 -> (510,0,0)
        (None, 0, (1.0, 1.0, 1.0)),  # unresolved dropped
    ]
    segs = coherent_tracks(events, max_step=150.0, min_points=3)
    assert len(segs) == 2
    assert [p[0] for p in segs[0]] == [0.0, 10.0, 20.0]
    assert [p[0] for p in segs[1]] == [500.0, 505.0, 510.0]
    # every step within a segment is below the plausibility bound
    for seg in segs:
        for a, b in zip(seg, seg[1:]):
            assert abs(a[0] - b[0]) <= 150.0


def test_coherent_tracks_resume_after_break_with_update():
    # A break (1000 m jump) followed by *updates only* (no add): the post-break run must form its
    # own coherent segment, tracked relative to its first sample — not be folded into the first
    # segment and not crash. (The first sample after the break has no anchor, so it seeds relative.)
    events = [
        (7, 0, (0.0, 0.0, 0.0)),
        (7, 0, (10.0, 0.0, 0.0)),
        (7, 0, (10.0, 0.0, 0.0)),    # seg 1: 0,10,20
        (7, 0, (1000.0, 0.0, 0.0)),  # jump -> break (no add follows)
        (7, 0, (5.0, 0.0, 0.0)),     # post-break, update only -> relative seed at (5,0,0)
        (7, 0, (10.0, 0.0, 0.0)),    # +10 -> (15,0,0)
        (7, 0, (10.0, 0.0, 0.0)),    # +10 -> (25,0,0)  seg 2: 5,15,25
    ]
    segs = coherent_tracks(events, max_step=150.0, min_points=3)
    assert len(segs) == 2
    assert [p[0] for p in segs[0]] == [0.0, 10.0, 20.0]
    assert [p[0] for p in segs[1]] == [5.0, 15.0, 25.0]   # relative segment, no jump folded in
    for seg in segs:
        for a, b in zip(seg, seg[1:]):
            assert abs(a[0] - b[0]) <= 150.0
