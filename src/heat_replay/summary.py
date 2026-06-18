"""High-level match summary derived from the decoded layers (baselines + reflected events).

Per-player vehicle/team assignment is not recoverable; what is available is the set of vehicle
types and frontmen present, plus the event timeline.
"""

from __future__ import annotations

import collections
import re

_VEHICLE = re.compile(r"^/vehicles/(\w+)/(\w+)/\2\.prefab$")
_FRONTMAN = re.compile(r"^/frontmen/(\w+)/(\w+)/")


def roster(replay) -> list[dict]:
    """Distinct vehicle types present in the match (from entity baselines).

    Returns ``[{"nation": ..., "vehicle": ...}]``. Per-player assignment is not recoverable;
    this is the set of tanks in the battle.
    """
    seen: dict[str, dict] = {}
    for b in replay.baselines():
        m = _VEHICLE.match(b.prefab or "")
        if m:
            seen.setdefault(m.group(2), {"nation": m.group(1), "vehicle": m.group(2)})
    return sorted(seen.values(), key=lambda d: d["vehicle"])


def frontmen(replay) -> list[str]:
    """Distinct frontman/commander archetypes referenced (e.g. ``assault/aslt001``)."""
    out = set()
    for b in replay.baselines():
        m = _FRONTMAN.match(b.prefab or "")
        if m:
            out.add(f"{m.group(1)}/{m.group(2)}")
    return sorted(out)


def build_summary(replay) -> dict:
    """A compact, JSON-serialisable summary of the whole match."""
    records = replay.records
    max_frame = max((r.frame_id for r in records), default=0)

    # single pass for roster + frontmen
    veh: dict[str, dict] = {}
    fmen: set[str] = set()
    for b in replay.baselines():
        mv = _VEHICLE.match(b.prefab or "")
        if mv:
            veh.setdefault(mv.group(2), {"nation": mv.group(1), "vehicle": mv.group(2)})
        mf = _FRONTMAN.match(b.prefab or "")
        if mf:
            fmen.add(f"{mf.group(1)}/{mf.group(2)}")

    # single pass for event counts + battle result
    event_counts: collections.Counter = collections.Counter()
    battle_result = None
    for frame_id, name, fields in replay.decoded_events():
        event_counts[name] += 1
        if battle_result is None and name.startswith("cw::BattleResultReplayEvent"):
            battle_result = fields.get("result")

    return {
        "path": replay.path,
        "map": replay.map_name,
        "mode": replay.game_mode,
        "build": replay.build,
        "commit": replay.commit,
        "result": replay.result,
        "recorder": replay.recorder,
        "seed": replay.seed,
        "max_frame_id": max_frame,
        "record_count": len(records),
        "vehicles": sorted(veh),
        "frontmen": sorted(fmen),
        "event_counts": dict(event_counts),
        "battle_result": battle_result,
    }
