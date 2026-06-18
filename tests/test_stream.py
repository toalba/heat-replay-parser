"""Record-stream parser tests."""

from heat_replay.stream import baselines, events, read_len, referenced_assets, walk


def test_walk_reaches_clean_eof(sample_path):
    w = walk(str(sample_path))
    assert w.clean_eof, f"did not reach EOF: ended at {w.end}"
    assert len(w.records) > 10_000  # 85k-148k in practice


def test_read_len_varint():
    # (b0 & 3) + 1 bytes, value = LE >> 2
    def enc(v, cls):
        return ((v << 2) | cls).to_bytes(cls + 1, "little")

    assert read_len(b"\x20", 0) == (8, 1)        # class 0, 1 byte
    assert read_len(b"\x7c", 0) == (31, 1)
    assert read_len(enc(64, 1), 0) == (64, 2)    # class 1, 2 bytes
    assert read_len(enc(100_000, 2), 0) == (100_000, 3)    # class 2, 3 bytes
    assert read_len(enc(1_000_000, 3), 0) == (1_000_000, 4)  # class 3, 4 bytes


def test_read_len_truncated_raises():
    import pytest

    with pytest.raises(ValueError):
        read_len(b"", 0)               # offset past EOF
    with pytest.raises(ValueError):
        read_len(b"\x01", 0)           # class 1 needs 2 bytes, only 1


def test_walk_truncated_raises(tmp_path, samples):
    import pytest

    raw = (samples["vietnam"]).read_bytes()
    trunc = tmp_path / "truncated.replay"
    trunc.write_bytes(raw[: len(raw) - 3])  # cut mid-record
    with pytest.raises(ValueError):
        walk(str(trunc))


def test_event_types_registered(sample_path):
    w = walk(str(sample_path))
    names = set(w.event_types.values())
    assert any(n.startswith("cw::PlayerInputReplayEvent") for n in names)
    assert any(n.startswith("cw::MainSeedReplayEvent") for n in names)
    assert any(n.startswith("cw::BattleResultReplayEvent") for n in names)


def test_rng_seed_present(sample_path):
    w = walk(str(sample_path))
    assert w.seed is not None and w.seed != 0


def test_reflected_events_and_baselines(sample_path):
    w = walk(str(sample_path))
    evs = events(w)
    bls = baselines(w)
    assert evs, "no reflected (tag 0x0a) events"
    assert bls, "no entity-baseline (tag 0x06) records"
    # every reflected event resolved to a registered name
    assert all(r.event_name for r in evs)
    # baselines carry prefab paths + entity ids
    assert all(r.prefab and r.prefab.startswith("/") for r in bls)


def test_referenced_assets(sample_path):
    assets = referenced_assets(walk(str(sample_path)))
    assert len(assets) > 500
    assert any(a.startswith("/vehicles/") for a in assets)
    assert any("network_player.prefab" in a for a in assets)
