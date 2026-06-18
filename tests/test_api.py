"""Public library API tests — the single entry point consumers use."""

import heat_replay


def test_parse_returns_full_replay(sample_path):
    r = heat_replay.parse(str(sample_path))
    assert r.clean
    assert r.map_name and r.game_mode
    assert r.commit == heat_replay.REFERENCE_COMMIT
    assert r.result in {"Win", "Lose"}
    assert r.seed and r.seed != 0
    assert r.events() and r.baselines()
    assert len(r.assets()) > 500
    assert r.protocol is not None and len(r.protocol.classes_by_name) > 100


def test_property_deltas(sample_path):
    r = heat_replay.parse(str(sample_path))
    deltas = r.property_deltas()
    assert len(deltas) > 1000
    # framing invariants: handles are u32, values are bytes, frames within range
    frames = [rec.frame_id for rec in r.records]
    lo, hi = min(frames), max(frames)
    assert all(isinstance(d.handle, int) and 0 <= d.handle < 2**32 for d in deltas)
    assert all(isinstance(d.value, bytes) for d in deltas)
    assert all(lo <= d.frame_id <= hi for d in deltas)


def test_decoded_events(sample_path):
    r = heat_replay.parse(str(sample_path))
    decoded = r.decoded_events()
    assert decoded
    names = {n for _, n, _ in decoded}
    assert any(n.startswith("cw::PlayerInputReplayEvent") for n in names)
    # MainSeed decodes to the same seed the stream extracted
    seeds = [f["seed"] for _, n, f in decoded if n.startswith("cw::MainSeedReplayEvent")]
    assert seeds and seeds[0] == r.seed


def test_battle_result(sample_path):
    r = heat_replay.parse(str(sample_path))
    br = r.battle_result()
    assert br is not None
    # the decoded BattleResult JSON must agree with the container-derived win/loss
    assert br["m_endGameType"] == r.result


def test_summary_and_roster(sample_path):
    r = heat_replay.parse(str(sample_path))
    s = r.summary()
    assert s["map"] and s["mode"]
    assert s["result"] in {"Win", "Lose"}
    assert s["max_frame_id"] > 1000
    assert len(s["vehicles"]) >= 6  # multiple tank types present
    assert s["battle_result"]["m_endGameType"] == r.result
    # roster entries are well-formed
    assert all(set(v) == {"nation", "vehicle"} for v in r.roster())
    import json

    json.dumps(s)  # must be JSON-serialisable


def test_parse_without_schema(sample_path):
    r = heat_replay.parse(str(sample_path), with_schema=False)
    assert r.protocol is None
    assert r.clean  # stream still fully parsed


def test_dead_replay_raises(tmp_path):
    import pytest

    dead = tmp_path / "dead.replay"
    dead.write_bytes(b"\x00" * 4096)
    with pytest.raises(heat_replay.DeadReplayError):
        heat_replay.parse(str(dead))
