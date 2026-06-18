"""Typed decoders for reflected-event (tag 0x0a) blobs.

Each ``*ReplayEvent`` blob is a small fixed-layout serialized struct, validated against all
samples. Where a field's *meaning* is not yet confirmed (PlayerInput / ClientShoot), values are
decoded structurally but named neutrally and flagged.
"""

from __future__ import annotations

import json
import struct

from heat_replay.stream import Record


def decode_event(rec: Record) -> dict | None:
    """Decode a reflected-event record's blob into typed fields, or ``None`` if unknown/short.

    Confident decodes: MainSeed (seed), PlayerZoom (i32), BattleResult (length-prefixed JSON).
    Structural decodes (values correct, field names best-effort): PlayerInput (2×f32),
    ClientShoot (5×u32).
    """
    name = rec.event_name or ""
    b = rec.blob or b""

    if name.startswith("cw::MainSeedReplayEvent") and len(b) >= 4:
        return {"seed": int.from_bytes(b[:4], "little")}

    if name.startswith("cw::PlayerZoomReplayEvent") and len(b) >= 4:
        return {"zoom": int.from_bytes(b[:4], "little", signed=True)}  # -1 = default/reset

    if name.startswith("cw::PlayerInputReplayEvent") and len(b) >= 8:
        v0, v1 = struct.unpack("<2f", b[:8])
        return {"val0": v0, "val1": v1, "_semantics": "unverified"}

    if name.startswith("cw::ClientShootReplayEvent") and len(b) >= 20:
        f = struct.unpack("<5I", b[:20])
        return {"f0": f[0], "f1": f[1], "f2": f[2], "f3": f[3], "f4": f[4], "_semantics": "unverified"}

    if name.startswith("cw::BattleResultReplayEvent") and len(b) >= 4:
        n = int.from_bytes(b[:4], "little")
        s = b[4 : 4 + n].decode("utf-8", "replace")
        if len(b) < 4 + n:  # declared length exceeds available bytes
            return {"result_raw": s, "_truncated": True}
        try:
            return {"result": json.loads(s)}
        except json.JSONDecodeError:
            return {"result_raw": s}

    return None  # e.g. cw::TacticMapTriggerEvent — not yet characterised
