"""Synthetic round-trip tests for the stateful replication client's layout-independent components.

These prove the reference-packet identity model and the target-frame full-sync gate in isolation —
no fixtures, no binary — before they are wired to the (binary-derived) packet-header parse.
"""
from __future__ import annotations

from heat_replay.replication_client import FrameSyncGate, ReferenceRegistry


def test_reference_registry_serves_recorded_frame():
    reg = ReferenceRegistry()
    reg.record(10, [100, 101, 102])
    reg.record(11, [100, 101, 102, 200])
    # a packet referencing frame 10 resolves against frame 10's list, not the most recent
    assert reg.reference_list(10) == [100, 101, 102]
    assert reg.reference_list(11) == [100, 101, 102, 200]
    assert 10 in reg and 11 in reg


def test_reference_registry_missing_frame_is_empty_not_error():
    reg = ReferenceRegistry()
    reg.record(5, [1, 2])
    # a dropped/never-seen reference frame yields an empty list (resolver then yields None safely)
    assert reg.reference_list(999) == []
    assert 999 not in reg


def test_reference_registry_drops_none_ids_and_copies():
    reg = ReferenceRegistry()
    src = [1, None, 3]
    reg.record(7, src)
    assert reg.reference_list(7) == [1, 3]          # unresolved ids are not stored as references
    src.append(4)
    assert reg.reference_list(7) == [1, 3]          # stored list is a copy, not aliased


def test_frame_sync_gate_skips_outdated_processes_newer():
    gate = FrameSyncGate()
    # first sight of entity 7 at frame 5: newer than -1 -> process
    assert gate.should_process(7, 5) is True
    gate.note_applied(7, 5)
    # an equal-or-older full-sync (frame 5 or 4) is outdated -> skip
    assert gate.should_process(7, 5) is False
    assert gate.should_process(7, 4) is False
    # a newer full-sync (frame 6) -> process
    assert gate.should_process(7, 6) is True
    gate.note_applied(7, 6)
    assert gate.last_frame(7) == 6


def test_frame_sync_gate_is_per_entity():
    gate = FrameSyncGate()
    gate.note_applied(1, 10)
    # entity 2 is independent of entity 1's high-water mark
    assert gate.should_process(2, 3) is True
    assert gate.should_process(1, 3) is False
    assert gate.last_frame(2) == -1                  # should_process alone does not advance the mark


def test_frame_sync_gate_present_but_skipped_does_not_advance():
    gate = FrameSyncGate()
    gate.note_applied(9, 20)
    # a body present but gated-out (older) must not move the high-water mark
    assert gate.should_process(9, 15) is False
    assert gate.last_frame(9) == 20
