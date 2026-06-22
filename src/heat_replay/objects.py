"""Replicated-object identification and position reading.

Builds the list of replicated entities (vehicles, players, projectiles, map objects) from the
entity-baseline records: each is identified by its prefab path and grouped into lifetimes (entity
slots are recycled, so a change of prefab on the same slot id starts a new object).

Where a transform-like component is reachable at the head of a baseline, a 3-component packed
``position`` (variable-width packed scalar; see :mod:`heat_replay.packed_scalar`) is decoded and
attached as a position sample.

NOTE — position-reading is **preliminary/unvalidated**: the head-component network id used here
(:data:`_TRANSFORM_NETWORK_ID`) is provisional and a later component-map revision indicates it is not
the moving-object transform; decoded samples pass only a finite-coordinate sanity bound, not a
ground-truth check. Object **identification** (prefab → category, lifetime segmentation) is the
reliable, validated part of this module. A correct dense per-frame trajectory requires the stateful
replication delta client over the full component set with per-component compressor configs — see docs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from heat_replay.bitstream import ReadStream
from heat_replay.packed_scalar import RIGIDBODY_POSITION, read_scalar
from heat_replay.stream import StreamWalk

# Recovered constants (build-stable): the rigid-body transform component's network id, and the
# component-id bit width used in the baseline frame header.
_TRANSFORM_NETWORK_ID = 37
_ID_BITS = 7
_MAP_LIMIT = 1.0e5  # finite-coordinate sanity bound (metres)

Vec3 = tuple[float, float, float]


def classify_prefab(prefab: str) -> str:
    """Coarse category for a prefab path."""
    p = prefab.lower()
    if "/vehicles/" in p:
        return "vehicle"
    if "network_player" in p or "/player" in p:
        return "player"
    if "shell" in p or "projectile" in p or "rocket" in p or "missile" in p:
        return "projectile"
    if "/abilities/" in p or "ability" in p:
        return "ability"
    return "other"


@dataclass
class ReplicatedObject:
    """One replicated entity lifetime."""

    entity_id: int
    prefab: str
    category: str
    first_frame: int
    last_frame: int
    # decoded transform position samples: (frame_id, (x, y, z)). May be empty when the transform
    # is not reachable at a baseline head for this object.
    positions: list[tuple[int, Vec3]] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Last path segment of the prefab (e.g. ``a20_m60a2``)."""
        return self.prefab.rstrip("/").split("/")[-1].split(".")[0]

    @property
    def moved(self) -> bool:
        """True if decoded positions show real displacement (> 1 m)."""
        if len(self.positions) < 2:
            return False
        xs = [p for _, p in self.positions]
        return any(
            sum((a - b) ** 2 for a, b in zip(xs[0], q)) ** 0.5 > 1.0 for q in xs[1:]
        )

    def __repr__(self) -> str:
        return (
            f"ReplicatedObject(id={self.entity_id}, name={self.name!r}, cat={self.category}, "
            f"frames={self.first_frame}..{self.last_frame}, pos_samples={len(self.positions)})"
        )


def _decode_head_transform_position(blob: bytes) -> Vec3 | None:
    """If the baseline's first replicated component is the transform, decode its position."""
    rs = ReadStream(blob)
    try:
        rs.read_bits(32)  # frameId
        rs.read_bits(1)   # flag
        for _ in range(2):  # two component unique-sets
            if not rs.read_bits(1):
                cnt = rs.read_bits(32)
                if cnt > 1000:
                    return None
                for _ in range(cnt):
                    rs.read_bits(_ID_BITS)
        if rs.read_bits(32) < 1:  # componentCount
            return None
        if rs.read_bits(_ID_BITS) != _TRANSFORM_NETWORK_ID:
            return None
        pos = (read_scalar(rs, RIGIDBODY_POSITION),
               read_scalar(rs, RIGIDBODY_POSITION),
               read_scalar(rs, RIGIDBODY_POSITION))
    except Exception:
        return None
    if not all(c == c and abs(c) < _MAP_LIMIT for c in pos):
        return None
    return pos


def replicated_objects(w: StreamWalk) -> list[ReplicatedObject]:
    """Identify all replicated-entity lifetimes, with decoded transform positions where reachable.

    Entity slot ids are recycled; a change of prefab on the same id starts a new lifetime.
    """
    objs: list[ReplicatedObject] = []
    # current open lifetime per entity slot
    cur: dict[int, ReplicatedObject] = {}
    for r in w.records:
        if r.tag != 6 or r.prefab is None or r.entity_id is None:
            continue
        eid, prefab, frame = r.entity_id, r.prefab, r.frame_id
        o = cur.get(eid)
        if o is None or o.prefab != prefab:  # new lifetime (spawn or recycled slot)
            o = ReplicatedObject(eid, prefab, classify_prefab(prefab), frame, frame)
            cur[eid] = o
            objs.append(o)
        o.last_frame = frame
        pos = _decode_head_transform_position(r.blob) if r.blob else None
        if pos is not None:
            o.positions.append((frame, pos))
    return objs


def moving_objects(w: StreamWalk) -> list[ReplicatedObject]:
    """Replicated objects that have at least one decoded position (subset of all, position-readable)."""
    return [o for o in replicated_objects(w) if o.positions]
