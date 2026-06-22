"""Replicated-object identification + position-reading tests (fixture-gated)."""

from __future__ import annotations

import heat_replay


def test_objects_identified(sample_path):
    r = heat_replay.parse(str(sample_path))
    objs = r.objects()
    assert objs, "no replicated objects identified"
    # every object has identity + a frame span
    for o in objs:
        assert o.prefab and o.entity_id is not None
        assert o.first_frame <= o.last_frame
        assert o.category in {"vehicle", "player", "projectile", "ability", "other"}
    cats = {o.category for o in objs}
    # a real battle has vehicles and players
    assert "vehicle" in cats and "player" in cats


def test_object_positions_sane(sample_path):
    r = heat_replay.parse(str(sample_path))
    for o in r.moving_objects():
        assert o.positions
        for _, p in o.positions:
            assert all(c == c and abs(c) < 1.0e5 for c in p), (o.name, p)


def test_moving_objects_subset(sample_path):
    r = heat_replay.parse(str(sample_path))
    objs = r.objects()
    mov = r.moving_objects()
    keys = {(o.entity_id, o.prefab, o.first_frame) for o in objs}
    assert all((o.entity_id, o.prefab, o.first_frame) in keys for o in mov)
    assert all(o.positions for o in mov)
    assert len(mov) == sum(1 for o in objs if o.positions)


def test_summary_has_object_stats(sample_path):
    r = heat_replay.parse(str(sample_path))
    s = r.summary()
    assert "objects_by_category" in s and s["objects_by_category"]
    assert s.get("position_readable_objects", 0) >= 0
