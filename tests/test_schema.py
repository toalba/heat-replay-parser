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


def test_field_wire_types_captured(samples):
    """Fields carry their wire-type name plus the trailing type-hash and flag."""
    proto = parse_schema(
        extract_schema_text(_read(next(iter(samples.values())))), REFERENCE_COMMIT
    )
    typed = [f for c in proto.classes_by_name.values() for f in c.fields if f.type]
    assert typed, "expected at least some fields with a wire type"
    # A typed field captures the trailing type-hash and flag from the schema.
    with_hash = [f for f in typed if f.type_hash is not None and f.flag is not None]
    assert with_hash, "expected typed fields to carry type_hash + flag"
    # The plain float/vec3 wire types must appear (the self-describing, decodable ones).
    type_names = {f.type for f in typed}
    assert "CPlainFloat32" in type_names
    assert "CPlainVec3" in type_names


def test_structural_class_detection_and_positional_types(samples):
    """Single-word components and positional field types are recovered from the schema.

    Class detection is structural (not name-suffix gated), so single-word components and
    zero-field markers parse; the field type is the positional second token, so arrays,
    namespaced enums and nested sub-components are all captured verbatim.
    """
    proto = parse_schema(
        extract_schema_text(_read(next(iter(samples.values())))), REFERENCE_COMMIT
    )
    # Single-word components the old suffix heuristic dropped.
    for name in ("Mana", "Driver", "Shoot", "Health"):
        assert name in proto.classes_by_name, name

    def ftype(cls, field):
        return next(f.type for f in proto.classes_by_name[cls].fields if f.name == field)

    assert ftype("Mana", "curValue") == "CPlainFloat32"           # primitive
    assert ftype("Shoot", "barrels") == "vector<CEntityNetworkId>"  # array
    assert ftype("Driver", "lastDriveMode") == "cw::CDrivingMode"   # namespaced enum

    # Nearly every field now carries a type token (the positional capture); the residual
    # untyped fields are genuinely type-less nested-replication entries in the schema.
    fields = [f for c in proto.classes_by_name.values() for f in c.fields]
    typed = [f for f in fields if f.type]
    assert len(typed) / len(fields) > 0.80


def test_every_field_maps_to_a_wire_category(samples):
    """Exhaustive wiring: every replicated field resolves to a concrete WireType.

    Typed fields classify by their token; the schema-typeless fields (no type token, just
    ``name=id``) are nested replication sets. Nothing should fall through to ``UNKNOWN`` —
    that is what makes the field map fully wired to the decode machinery.
    """
    from heat_replay.wiretypes import WireType, field_wire_type

    proto = parse_schema(
        extract_schema_text(_read(next(iter(samples.values())))), REFERENCE_COMMIT
    )
    unknown = [
        (c.name, f.name)
        for c in proto.classes_by_name.values()
        for f in c.fields
        if field_wire_type(f) is WireType.UNKNOWN
    ]
    assert not unknown, f"uncategorized fields: {unknown[:10]}"


def test_field_type_map_identical_across_samples(samples):
    """The (class, field) -> wire-type map is build-stable, like the flat index."""
    maps: dict[str, list] = {}
    for key, path in samples.items():
        proto = parse_schema(extract_schema_text(_read(path)), REFERENCE_COMMIT)
        maps[key] = sorted(
            (c.name, f.name, f.type)
            for c in proto.classes_by_name.values()
            for f in c.fields
        )
    keys = list(maps)
    reference = maps[keys[0]]
    assert reference, "expected a non-empty field-type map"
    for key in keys[1:]:
        assert maps[key] == reference, f"field-type map for {key!r} differs"
