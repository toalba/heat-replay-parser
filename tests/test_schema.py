"""Schema parser tests.

Covers extraction, full-input parse, expected magnitudes, and the headline property:
the flat ``name -> id`` index is byte-identical across all four live samples (build
stability), which is what makes the schema a reusable per-build artifact.
"""

from __future__ import annotations

import pytest

from heat_replay import REFERENCE_COMMIT
from heat_replay.schema import extract_schema_text, parse_schema

# The four concatenated schema lists carry 667 distinct quoted names in every sample;
# assert comfortably below that (the documented "~667").
MIN_FLAT_NAMES = 600


def _read(path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def test_extract_starts_with_protocol(sample_path):
    raw = _read(sample_path)
    text = extract_schema_text(raw)
    assert text.startswith("['Protocol'"), text[:32]
    # The Protocol registry closes with its own composite id.
    assert text.rstrip().endswith("]")


def test_parse_consumes_all_input(sample_path):
    raw = _read(sample_path)
    text = extract_schema_text(raw)
    # parse_schema raises on any trailing tokens; reaching the asserts means it consumed
    # the whole input.
    proto = parse_schema(text, REFERENCE_COMMIT)
    assert proto is not None


def test_flat_index_magnitude(sample_path):
    raw = _read(sample_path)
    proto = parse_schema(extract_schema_text(raw), REFERENCE_COMMIT)
    assert len(proto.flat_index) >= MIN_FLAT_NAMES
    assert proto.classes_by_name, "expected at least one class"
    # ids are a single byte and context-local, so they are NOT globally unique across
    # classes; classes_by_id therefore has at most as many entries as classes_by_name.
    assert 0 < len(proto.classes_by_id) <= len(proto.classes_by_name)


def test_build_commit_recorded(sample_path):
    raw = _read(sample_path)
    proto = parse_schema(extract_schema_text(raw), REFERENCE_COMMIT)
    assert proto.build_commit == REFERENCE_COMMIT


def test_flat_index_identical_across_all_samples(samples):
    """The cross-sample stability property: the name -> id map is identical everywhere."""
    indices: dict[str, dict[str, int]] = {}
    for key, path in samples.items():
        raw = _read(path)
        proto = parse_schema(extract_schema_text(raw), REFERENCE_COMMIT)
        indices[key] = proto.flat_index

    keys = list(indices)
    reference = indices[keys[0]]
    assert len(reference) >= MIN_FLAT_NAMES
    for key in keys[1:]:
        assert indices[key] == reference, (
            f"flat_index for {key!r} differs from {keys[0]!r}"
        )


def test_known_classes_and_fields(samples):
    """Spot-check a documented message and one of its field ids for sanity."""
    proto = parse_schema(
        extract_schema_text(_read(next(iter(samples.values())))), REFERENCE_COMMIT
    )
    # Documented in format-notes.md.
    assert "cw::AttemptToShootMessage" in proto.classes_by_name
    assert "cw::NetHitMessage" in proto.classes_by_name
    shoot = proto.classes_by_name["cw::AttemptToShootMessage"]
    field_names = {f.name for f in shoot.fields}
    assert "frameId" in field_names
    assert "gunNetworkId" in field_names
