"""heat_replay — decoder for World of Tanks: HEAT ``.replay`` files.

Library entry point. Consumers do not touch bytes — just::

    import heat_replay
    r = heat_replay.parse("match.replay")
    print(r.map_name, r.game_mode, r.result, hex(r.seed))
    for ev in r.events():          # reflected events (PlayerInput, ClientShoot, …)
        ...
    for spawn in r.baselines():    # entity spawns (prefab + component blob)
        ...

``parse()`` returns a fully-structured :class:`Replay`; nothing requires the caller to parse the
container, schema, or record stream themselves. Replication-packet values (tags 4/8) are exposed
as structured records with raw payloads; decoding those values into typed data is not implemented.
"""

from __future__ import annotations

from dataclasses import dataclass

from heat_replay.container import Container, read_bytes
from heat_replay.events import decode_event
from heat_replay.model import Protocol
from heat_replay.objects import ReplicatedObject
from heat_replay.objects import moving_objects as _moving_objects
from heat_replay.objects import replicated_objects as _objects
from heat_replay.schema import parse_schema
from heat_replay.stream import PropertyDelta, Record, StreamWalk
from heat_replay.stream import baselines as _baselines
from heat_replay.stream import events as _events
from heat_replay.stream import parse_stream
from heat_replay.stream import property_deltas as _deltas
from heat_replay.stream import referenced_assets as _assets
from heat_replay.summary import build_summary as _summary
from heat_replay.summary import frontmen as _frontmen
from heat_replay.summary import roster as _roster
from heat_replay.wiretypes import WireType, classify, field_wire_type

__version__ = "0.1.0"

# Reference build these replays/schemas correspond to.
REFERENCE_COMMIT = "95e710fa95a277b1cf14017fc6076dcf691bcd5c"

__all__ = [
    "parse",
    "parse_bytes",
    "Replay",
    "DeadReplayError",
    "PropertyDelta",
    "Record",
    "StreamWalk",
    "Protocol",
    "Container",
    "ReplicatedObject",
    "WireType",
    "classify",
    "field_wire_type",
    "decode_event",
    "REFERENCE_COMMIT",
    "__version__",
]


class DeadReplayError(ValueError):
    """Raised by :func:`parse` for a null-filled / corrupt / non-CR08 file."""


@dataclass(repr=False)
class Replay:
    """A fully-parsed HEAT replay. The single object consumers work with."""

    container: Container
    protocol: Protocol | None
    stream: StreamWalk

    def __repr__(self) -> str:  # avoid dumping ~100k records in a REPL
        return (
            f"Replay(map={self.map_name!r}, mode={self.game_mode!r}, result={self.result!r}, "
            f"records={len(self.records)}, clean={self.clean})"
        )

    # --- header / metadata -------------------------------------------------
    @property
    def path(self) -> str:
        return self.container.path

    @property
    def map_name(self) -> str | None:
        return self.container.map_name

    @property
    def game_mode(self) -> str | None:
        return self.container.game_mode

    @property
    def build(self) -> str | None:
        return self.container.build

    @property
    def commit(self) -> str | None:
        return self.container.commit

    @property
    def result(self) -> str | None:
        """``"Win"`` / ``"Lose"`` (from the BattleResult JSON island)."""
        return self.container.end_game_type

    @property
    def recorder(self) -> str | None:
        """The recording player's ``name#NNNN`` handle, if uniquely determinable."""
        return self.container.local_player

    @property
    def players(self) -> list[str]:
        return self.container.players

    # --- stream ------------------------------------------------------------
    @property
    def seed(self) -> int | None:
        """The RNG seed (`MainSeedReplayEvent`)."""
        return self.stream.seed

    @property
    def event_types(self) -> dict[int, str]:
        """Registered reflected-event id → name."""
        return self.stream.event_types

    @property
    def records(self) -> list[Record]:
        """Every record in the stream (events, baselines, network packets, …)."""
        return self.stream.records

    @property
    def clean(self) -> bool:
        """True if the stream parsed to exact EOF (100% coverage).

        Parsing raises on any mid-stream overrun, so for a successfully-returned ``Replay`` this
        is effectively always ``True``; it is ``False`` only in the degenerate case where the
        stream offset already points at/past EOF.
        """
        return self.stream.clean_eof

    def events(self) -> list[Record]:
        """Reflected custom-event records (PlayerInput / ClientShoot / Zoom / …)."""
        return _events(self.stream)

    def baselines(self) -> list[Record]:
        """Entity spawn / component-baseline records (prefab + blob)."""
        return _baselines(self.stream)

    def assets(self) -> list[str]:
        """All distinct asset paths referenced (tanks, abilities, effects, …)."""
        return _assets(self.stream)

    def property_deltas(self) -> list[PropertyDelta]:
        """Replication property deltas: (frame, handle, raw value) over the match.

        Framing is decoded; decoding the raw value into typed data is not implemented.
        """
        return _deltas(self.stream)

    def field_types(self) -> list[dict]:
        """Every replicated field with its wire type, across all schema classes.

        Returns ``[{"class", "field", "type", "wire_type", "decodable"}]`` (sorted). The
        schema names a wire type for every field; ``wire_type`` is the :class:`WireType`
        category and ``decodable`` flags the self-describing types (plain floats, ints,
        bools) that need no per-type quantization constant. This map is build-stable
        (identical across replays of the same build).
        """
        out: list[dict] = []
        if self.protocol is None:
            return out
        for cname in sorted(self.protocol.classes_by_name):
            for f in self.protocol.classes_by_name[cname].fields:
                wt = field_wire_type(f)
                out.append(
                    {
                        "class": cname,
                        "field": f.name,
                        "type": f.type,
                        "wire_type": wt.name,
                        "decodable": wt.is_decodable,
                    }
                )
        return out

    def decoded_events(self) -> list[tuple[int, str, dict]]:
        """Reflected events with their decoded fields: ``(frame_id, event_name, fields)``.

        Skips event types without a decoder (currently only `TacticMapTriggerEvent`).
        """
        out = []
        for r in self.events():
            fields = decode_event(r)
            if fields is not None:
                out.append((r.frame_id, r.event_name or "", fields))
        return out

    def battle_result(self) -> dict | None:
        """The `BattleResultReplayEvent` payload (decoded JSON).

        e.g. ``{"__type__": "cw::BattleResultReplayEvent_ver1", "m_endGameType": "Win"}``.
        """
        for r in self.events():
            if (r.event_name or "").startswith("cw::BattleResultReplayEvent"):
                fields = decode_event(r)
                if fields and "result" in fields:
                    return fields["result"]
        return None

    def objects(self) -> list[ReplicatedObject]:
        """All replicated-entity lifetimes, identified by prefab, with decoded transform positions
        where the transform component is reachable at a baseline head.

        Each :class:`ReplicatedObject` carries ``entity_id``, ``prefab``, ``category`` (vehicle /
        player / projectile / …), the frame span it was seen, and any decoded ``positions``.
        """
        return _objects(self.stream)

    def moving_objects(self) -> list[ReplicatedObject]:
        """Replicated objects with at least one decoded position (the position-readable subset)."""
        return _moving_objects(self.stream)

    def roster(self) -> list[dict]:
        """Distinct vehicle types in the match (``[{"nation", "vehicle"}]``)."""
        return _roster(self)

    def frontmen(self) -> list[str]:
        """Distinct frontman/commander archetypes referenced."""
        return _frontmen(self)

    def summary(self) -> dict:
        """A compact JSON-serialisable summary of the whole match (metadata + roster + counts)."""
        return _summary(self)


def parse(path: str, *, with_schema: bool = True) -> Replay:
    """Parse a ``.replay`` into a :class:`Replay`. Raises :class:`DeadReplayError` if unreadable.

    Reads the file exactly once.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    return parse_bytes(raw, str(path), with_schema=with_schema)


def parse_bytes(raw: bytes, path: str = "<bytes>", *, with_schema: bool = True) -> Replay:
    """Parse an in-memory ``.replay`` buffer into a :class:`Replay`."""
    container = read_bytes(raw, path)
    if container.is_dead or container.stream_start is None:
        raise DeadReplayError(f"{path}: null-filled / corrupt / not a CR08 replay")
    stream = parse_stream(raw, container.stream_start)
    protocol = (
        parse_schema(container.schema_text, container.commit or "")
        if with_schema and container.schema_text
        else None
    )
    return Replay(container=container, protocol=protocol, stream=stream)
