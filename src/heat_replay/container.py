"""Container reader for World of Tanks: HEAT ``.replay`` files.

Reads the tagged ASCII header, reassembles the embedded protocol schema, marks the
start of the binary packet stream, and harvests the plaintext islands (recording
player handle, win/loss).

The schema region and ``stream_start`` are the hand-off to ``schema.py``. Stdlib only.
"""

from __future__ import annotations

import json
import os
import re

from heat_replay.model import Container
from heat_replay.schema import extract_schema_region

# Header layout (offsets from start of file).
_CONTAINER_TAG = 0x10  # first byte of an intact replay
_FORMAT_TAG = "CR08"  # ASCII bytes 0x01..0x04
_WORLD_TAG_MIN = 0x80  # the world-path tag byte at 0x05 has its high bit set (0x80, 0x84, ...)
_SCHEMA_START = 0xB8  # offset where the embedded protocol schema begins

# Plaintext-island patterns.
_HANDLE = re.compile(rb"\b[\w]{3,20}#[0-9]{3,6}\b")  # player handle name#NNNN
# `key: '...'` pairs in the build string (build / branch / commit).
_BUILD_FIELD = re.compile(rb"(\w+):\s*'([^']*)'")


def read(path: str | os.PathLike) -> Container:
    """Read a ``.replay`` from disk and return a parsed ``Container`` (see :func:`read_bytes`)."""
    path = os.fspath(path)
    with open(path, "rb") as fh:
        data = fh.read()
    return read_bytes(data, str(path))


def read_bytes(data: bytes, path: str = "<bytes>") -> Container:
    """Parse an in-memory ``.replay`` buffer into a ``Container``.

    Never raises on a malformed/dead file: a null-filled or non-CR08 input is reported
    via ``is_dead=True`` with the other fields left empty.
    """
    c = Container(path=str(path))

    # Dead-file guard: failed writes are ~99% 0x00 and lack the 0x10 "CR08" tag.
    if not _looks_intact(data):
        c.is_dead = True
        return c

    c.container_tag = data[0]
    c.format_tag = data[1:5].decode("latin-1")

    _parse_header(data, c)
    _parse_schema_region(data, c)
    _harvest_players(data, c)
    c.end_game_type = _find_end_game_type(data)

    return c


def _looks_intact(data: bytes) -> bool:
    """True iff the file starts with ``0x10 "CR08"`` and is not ~99% zero bytes."""
    if len(data) < _SCHEMA_START:  # too short to hold header + schema region
        return False
    if data[0] != _CONTAINER_TAG or data[1:5] != _FORMAT_TAG.encode("ascii"):
        return False
    # A failed write is almost entirely null bytes; reject if >99% are zero.
    if data.count(0) > 0.99 * len(data):
        return False
    return True


def _parse_header(data: bytes, c: Container) -> None:
    """Pull the world path (map/mode) and the build string from the tagged header."""
    # World path: high-bit tag at 0x05 (0x80, 0x84, 0x88, 0x9c — low bits vary), then an
    # ASCII path at 0x06 running up to the next tag byte.
    if len(data) > 5 and data[5] >= _WORLD_TAG_MIN:
        end = 6
        while end < len(data) and 0x20 <= data[end] <= 0x7E:
            end += 1
        c.world_path = data[6:end].decode("latin-1")
        _split_world_path(c)

    # Build string follows the 0x81 0x01 tag; parse its key: '...' fields.
    # It lives just before the schema, so scan the header region up to 0xb8.
    head = data[6:_SCHEMA_START]
    fields = {k.decode("latin-1"): v.decode("latin-1") for k, v in _BUILD_FIELD.findall(head)}
    c.build = fields.get("build")
    c.branch = fields.get("branch")
    c.commit = fields.get("commit")


def _split_world_path(c: Container) -> None:
    """``/worlds/<map>_<mode>.world`` → ``map_name`` and ``game_mode``.

    The LAST underscore-segment of the stem is the mode; the rest is the map name
    (e.g. ``02_vietnam_control`` → map ``02_vietnam``, mode ``control``).
    """
    if not c.world_path:
        return
    stem = c.world_path.rsplit("/", 1)[-1]
    if stem.endswith(".world"):
        stem = stem[: -len(".world")]
    if "_" in stem:
        c.map_name, _, c.game_mode = stem.rpartition("_")
    else:
        c.map_name = stem


def _parse_schema_region(data: bytes, c: Container) -> None:
    """Reassemble the schema text and locate the start of the packet stream.

    Delegates to :func:`heat_replay.schema.extract_schema_region`, the structural
    extractor. It reassembles the four bracket-balanced schema lists and
    returns the byte offset where the schema ends and the packet stream begins. The naive
    "stop at first large gap" rule is insufficient — the gaps between schema runs and the
    stream are all small, so the boundary is found structurally (depth-0 must be ``[``).
    """
    c.schema_start = _SCHEMA_START
    c.schema_text, c.stream_start = extract_schema_region(data, _SCHEMA_START)


def _harvest_players(data: bytes, c: Container) -> None:
    """Collect every distinct ``name#NNNN`` handle; set ``local_player`` if unambiguous."""
    handles = sorted({m.decode("latin-1") for m in _HANDLE.findall(data)})
    c.players = handles
    # Only the recording client's handle is normally present; if exactly one, it's the
    # recorder. With more than one we can't tell which, so leave local_player unset.
    if len(handles) == 1:
        c.local_player = handles[0]


def _find_end_game_type(data: bytes) -> str | None:
    """Brace-scan for the small JSON blob carrying ``m_endGameType`` → ``"Win"``/``"Lose"``."""
    for m in re.finditer(rb"m_endGameType", data):
        # The enclosing object opens at the nearest preceding '{' (within ~5000 bytes).
        start = data.rfind(b"{", max(0, m.start() - 5000), m.start())
        if start < 0:
            continue
        # Try to close it at a following '}' and parse the slice.
        limit = min(len(data), m.start() + 5000)
        end = data.find(b"}", m.start(), limit)
        while end != -1:
            try:
                obj = json.loads(data[start : end + 1].decode("latin-1"))
            except (ValueError, UnicodeDecodeError):
                obj = None
            if isinstance(obj, dict) and "m_endGameType" in obj:
                return _normalize_result(obj["m_endGameType"])
            end = data.find(b"}", end + 1, limit)
    return None


def _normalize_result(value: object) -> str | None:
    """Normalize the raw endgame value to exactly ``"Win"`` or ``"Lose"``."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v == "win":
        return "Win"
    if v in ("lose", "loose"):  # tolerate a "Loose" spelling variant
        return "Lose"
    return None
