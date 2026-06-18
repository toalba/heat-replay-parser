"""Golden-value tests for the container reader.

Fixtures come from conftest.py: ``samples`` (short-key -> Path) and ``sample_path``
(parametrized over all 4 live samples).
"""

from __future__ import annotations

from heat_replay import REFERENCE_COMMIT
from heat_replay.container import read


def test_all_samples_common_invariants(sample_path):
    """Every live sample shares the same header/build/result invariants."""
    c = read(sample_path)

    assert c.is_dead is False
    assert c.container_tag == 0x10
    assert c.format_tag == "CR08"
    assert c.commit == REFERENCE_COMMIT
    assert c.build == "Release"
    assert c.branch == "release/1.0/live"

    # Plaintext islands.
    assert len(c.players) >= 1
    assert c.end_game_type in {"Win", "Lose"}

    # Schema region / stream boundary.
    assert c.schema_start == 0xB8
    assert c.stream_start is not None
    assert c.stream_start > c.schema_start
    assert len(c.schema_text) > 100_000


def test_vietnam_golden(samples):
    """02_vietnam_control → map 02_vietnam, mode control, Win."""
    c = read(samples["vietnam"])
    assert c.world_path == "/worlds/02_vietnam_control.world"
    assert c.map_name == "02_vietnam"
    assert c.game_mode == "control"
    assert c.players == ["ebike_channel#34921"]
    assert c.local_player == "ebike_channel#34921"
    assert c.end_game_type == "Win"


def test_nordoko_golden(samples):
    """04_nordoko_conquest → mode conquest; schema reassembles to start with ['Protocol'."""
    c = read(samples["nordoko"])
    assert c.map_name == "04_nordoko"
    assert c.game_mode == "conquest"
    assert c.end_game_type == "Lose"
    # Focused reassembly check: nordoko's schema run begins at a '[' at 0xb8.
    assert c.schema_text.startswith("['Protocol'")


def test_local_player_unset_when_ambiguous(samples):
    """friendshipdam carries two handles, so local_player can't be disambiguated."""
    c = read(samples["friendshipdam"])
    assert len(c.players) >= 2
    assert c.local_player is None


def test_dead_file_guard(tmp_path):
    """A null-filled / non-CR08 file is reported as dead without crashing."""
    dead = tmp_path / "dead.replay"
    dead.write_bytes(b"\x00" * 100_000)
    c = read(dead)
    assert c.is_dead is True
    assert c.container_tag is None
    assert c.commit is None
    assert c.players == []
    assert c.stream_start is None
