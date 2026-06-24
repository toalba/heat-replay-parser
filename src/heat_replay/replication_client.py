"""Stateful per-frame replication client — layout-independent components.

The simple :class:`heat_replay.replication.FrameWalker` resolves entity identity against the
immediately-prior packet and walks each packet's entities while bits remain. The engine's real
receive path is **stateful** in two ways this module captures:

1. **Reference-packet identity** — a packet resolves its entity-id stream against a *specific*
   earlier packet named by a reference-frame id in the packet header, not merely the prior packet.
   :class:`ReferenceRegistry` stores the resolved id-list per frame and serves the right reference
   list back.
2. **Target-frame gating of full-syncs** — a full-sync for an entity is applied only if its target
   frame is newer than the frame already applied to that entity; an outdated full-sync is skipped.
   :class:`FrameSyncGate` is that check, kept separate from the bit reads.

Both are self-contained and unit-tested with synthetic data. They consume frame ids as plain
integers, so they do **not** depend on the per-packet header bit layout — only the *wiring* that
feeds them the header's frame / reference-frame fields does. That wiring (the packet- and
chunk-header parse, and the count-bounded entity loop) is added once the header layout is fixed;
these components are the parts that can be built and proven ahead of it.
"""
from __future__ import annotations


class ReferenceRegistry:
    """Per-frame store of the resolved entity-id list.

    While a packet for frame ``F`` is walked, the ids it resolves are recorded under ``F`` via
    :meth:`record`. A later packet that names ``F`` as its reference frame seeds its id resolver
    from :meth:`reference_list` ``(F)`` — the engine's reference-packet model, which is more precise
    than "the immediately-prior packet" because the per-tick channel's sequence ids are reused.
    """

    def __init__(self) -> None:
        self._by_frame: dict[int, list[int]] = {}

    def record(self, frame: int, id_list) -> None:
        """Store (a copy of) the resolved id-list produced while walking ``frame``'s packet."""
        self._by_frame[int(frame)] = [x for x in id_list if x is not None]

    def reference_list(self, reference_frame: int) -> list[int]:
        """The id-list a packet referencing ``reference_frame`` resolves against; ``[]`` if that
        frame was never recorded (a dropped/missing reference — resolution then yields ``None``
        without desyncing, matching the existing resolver's truncation handling)."""
        return list(self._by_frame.get(int(reference_frame), []))

    def __contains__(self, frame: int) -> bool:
        return int(frame) in self._by_frame

    def __len__(self) -> int:
        return len(self._by_frame)


class FrameSyncGate:
    """The full-sync target-frame gate.

    A full-sync carries a target frame. It is processed only when that frame is **newer** than the
    last frame already applied to the entity; an equal-or-older full-sync is an outdated retransmit
    and is skipped (its body is not consumed as an update). :meth:`should_process` is the pure
    check; the caller advances the per-entity high-water mark via :meth:`note_applied` once a body
    is actually applied — so a body that is present but skipped does not move the mark.
    """

    def __init__(self) -> None:
        self._last: dict[int, int] = {}

    def should_process(self, entity_id: int, target_frame: int) -> bool:
        """True iff ``target_frame`` is strictly newer than the last frame applied to ``entity_id``."""
        return int(target_frame) > self._last.get(int(entity_id), -1)

    def note_applied(self, entity_id: int, target_frame: int) -> None:
        """Record that ``target_frame`` was applied to ``entity_id`` (monotonic high-water mark)."""
        eid = int(entity_id)
        self._last[eid] = max(self._last.get(eid, -1), int(target_frame))

    def last_frame(self, entity_id: int) -> int:
        """The last frame applied to ``entity_id`` (``-1`` if never)."""
        return self._last.get(int(entity_id), -1)
