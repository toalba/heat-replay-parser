"""Track per-entity replication slots through the tag-4 movement channel.

The tag-4 record stream is the game's per-tick replication channel: ~1 packet per active
entity per frame, spanning the whole battle (tens of thousands of frames). Two packet kinds
share one layout::

    [seq:u32] [slot:u16] [class-sig:u16] [...] [bit-packed component journal]

* **spawn packets** additionally embed a prefab path (``/vehicles/...``, ``/game_prefabs/...``)
  right after the fixed header — these announce an entity entering a slot;
* **update packets** carry no path — they are the per-frame journal deltas for the entity
  currently occupying that slot.

The ``slot:u16`` at byte 4 is a *recycled* occupancy id: a slot is claimed by a spawn, driven
by a run of updates, then freed and re-used by a later entity. Linking each update run back to
the spawn that opened its slot gives a **prefab-identified, time-resolved track** for every
replicated entity — its identity and lifetime (frame span + update cadence) across the match.

This probe surfaces exactly that — identity and lifetime, which are fully determined by the
replay. It does **not** decode world positions: the journal body is a bit-packed *delta* stream
whose per-field quantization constants are not carried in the replay file, so a metric trajectory
needs an external parameter capture (see ``docs/HANDOFF.md``). The track this probe builds is the
scaffold that decode would attach to.

Stdlib + heat_replay only; deterministic output.
"""

from __future__ import annotations

import re
import struct
import sys
from collections import defaultdict

from heat_replay import stream

_PREFAB_RE = re.compile(rb"/([ -~]{4,90}?\.prefab)")
_HANDLE_OFFSET = 4  # byte offset of the slot:u16 within a tag-4 packet
_MIN_HEADER = 8


def _slot(packet: bytes) -> int:
    return struct.unpack_from("<H", packet, _HANDLE_OFFSET)[0]


def track(path: str):
    """Return ``(slot -> prefab, slot -> sorted frame list)`` for tag-4 entities."""
    spawn_prefab: dict[int, str] = {}
    update_frames: dict[int, list[int]] = defaultdict(list)
    walk = stream.walk(path)
    for record in walk.records:
        if record.tag != 4 or not record.packets:
            continue
        for packet in record.packets:
            if len(packet) < _MIN_HEADER:
                continue
            slot = _slot(packet)
            match = _PREFAB_RE.search(packet[:120])
            if match:
                # First spawn to claim a slot names it; later re-uses keep their own runs.
                spawn_prefab.setdefault(slot, match.group(1).decode("ascii", "replace"))
            else:
                update_frames[slot].append(record.frame_id)
    return spawn_prefab, update_frames


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <replay.replay>", file=sys.stderr)
        return 2
    spawn_prefab, update_frames = track(argv[1])

    rows = []
    for slot, frames in update_frames.items():
        frames.sort()
        rows.append((len(frames), slot, spawn_prefab.get(slot), frames[0], frames[-1]))
    rows.sort(reverse=True)

    linked = sum(1 for _, slot, pf, *_ in rows if pf)
    print(f"tag-4 slots: {len(update_frames)} driven by updates, "
          f"{len(spawn_prefab)} opened by a named spawn, {linked} update-runs prefab-identified")
    print(f"{'slot':>6}  {'updates':>7}  {'frames':>17}  prefab")
    for n, slot, prefab, lo, hi in rows[:25]:
        name = prefab.rsplit("/", 1)[-1] if prefab else "(slot re-used; unnamed run)"
        print(f"0x{slot:04x}  {n:>7}  {lo:>7}..{hi:<8}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
