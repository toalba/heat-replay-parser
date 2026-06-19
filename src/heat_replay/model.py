"""Shared data structures for the heat_replay package.

``container.py`` produces a ``Container``; ``schema.py`` consumes ``Container.schema_text`` and
produces a ``Protocol``. Both modules import from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Container:
    """Result of reading the header + plaintext islands of a .replay.

    ``schema_text`` and ``stream_start`` are the hand-off to the schema/stream parsers.
    """

    path: str
    is_dead: bool = False  # null-filled / corrupt write

    # header
    container_tag: int | None = None  # expected 0x10
    format_tag: str | None = None  # expected "CR08"
    world_path: str | None = None  # /worlds/<map>_<mode>.world
    map_name: str | None = None
    game_mode: str | None = None
    build: str | None = None
    branch: str | None = None
    commit: str | None = None

    # plaintext islands
    players: list[str] = field(default_factory=list)  # all distinct name#NNNN seen
    local_player: str | None = None  # recorder, if determinable
    end_game_type: str | None = None  # "Win" | "Lose"

    # schema region / stream boundary
    schema_start: int | None = None  # offset of schema text (≈0xb8)
    schema_text: str = ""  # reassembled printable runs (input to the schema parser)
    stream_start: int | None = None  # offset where the binary packet stream begins


@dataclass
class SchemaField:
    """A field of a network class.

    In the embedded schema a field is a small list ``[ name=id, TypeName=id, int, int ]``.
    We keep the field ``name``/``id``, the referenced wire-type name (``type``, e.g.
    ``CPlainFloat32`` / ``CFixedVec3``), and the two trailing integers: ``type_hash`` (the
    reflection type id of the wire type — constant per type, not a per-field parameter) and
    ``flag`` (a per-field flag). The wire-type name determines how the field's bytes are
    encoded; see :mod:`heat_replay.wiretypes`.
    """

    name: str
    id: int
    type: str | None = None  # referenced wire-type name, e.g. "CPlainFloat32", if present
    type_hash: int | None = None  # trailing type-hash (per wire type, not per field)
    flag: int | None = None  # trailing per-field flag


@dataclass
class ClassDef:
    """A network class/message defined in the embedded schema."""

    name: str
    id: int
    fields: list[SchemaField] = field(default_factory=list)


@dataclass
class Protocol:
    """Parsed embedded protocol schema for one build."""

    build_commit: str
    classes_by_id: dict[int, ClassDef] = field(default_factory=dict)
    classes_by_name: dict[str, ClassDef] = field(default_factory=dict)
    flat_index: dict[str, int] = field(default_factory=dict)  # every quoted name -> id
