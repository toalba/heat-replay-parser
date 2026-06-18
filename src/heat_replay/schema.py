"""Embedded protocol schema parser for World of Tanks: HEAT .replay files.

The container header carries, starting at ~``0xb8``, a ~412 KB textual schema that
describes every network class/message and its fields. It is stored as four
bracket-balanced top-level lists, each a separate printable ASCII run separated by
short binary tags::

    ['Protocol'=CA,6=0E,'Classes'=24,[...]=CC]        # named class/field registry
    ['AutoAimingEvent'=D9,...]=EA]                     # event registry
    [['EffectiveVisibilityShareEvent'=62,...]=73]      # message/event field layouts
    [d3be0c19c35e=1F,...=C4]                            # ~375 KB hash -> id lookup table

Together they carry ~667 distinct quoted names. This module reassembles that text from
raw bytes (:func:`extract_schema_text`), parses the list-sequence with a recursive-descent
parser (:func:`parse_schema`) into the shared :class:`Protocol` model, and serialises it to
deterministic JSON (:func:`dump_schema`).

Stdlib only.
"""

from __future__ import annotations

import json
import re

from heat_replay.model import ClassDef, Protocol, SchemaField

# ---------------------------------------------------------------------------
# Extraction: reassemble the schema text from raw replay bytes.
# ---------------------------------------------------------------------------

_PRINTABLE = range(0x20, 0x7F)  # 0x20..0x7e inclusive
_MIN_RUN = 4  # minimum printable-run length to count as schema text
_MAX_GAP = 64  # binary gap larger than this terminates the schema region


def _printable_runs(raw: bytes, start: int):
    """Yield ``(begin, end)`` spans of printable runs (>= ``_MIN_RUN``) from ``start``."""
    n = len(raw)
    i = start
    while i < n:
        if raw[i] in _PRINTABLE:
            j = i
            while j < n and raw[j] in _PRINTABLE:
                j += 1
            if j - i >= _MIN_RUN:
                yield (i, j)
            i = j
        else:
            i += 1


def extract_schema_region(raw: bytes, start: int = 0xB8) -> tuple[str, int]:
    """Reassemble the schema text AND locate the start of the packet stream.

    Returns ``(schema_text, stream_start)`` where ``stream_start`` is the byte offset at
    which the schema region ends and the binary packet stream begins (the end of the last
    accepted schema run).

    The schema is four bracket-balanced top-level lists, each a printable ASCII run
    (bytes ``0x20..0x7e``, run length >= ``_MIN_RUN``) separated by short binary tags.
    We concatenate consecutive list-runs, bridging short (<= ``_MAX_GAP`` byte) binary
    gaps, and stop when either:

    * a binary gap exceeds ``_MAX_GAP`` bytes (the start of the packet stream), or
    * the next printable run does not begin a list — i.e. it has no ``[`` — which is how
      the first packet-stream run (``|cw::PlayerInputReplayEvent...``) is rejected even
      though only a small gap precedes it.

    This structural terminator (rather than a gap-size heuristic alone) is required: the
    gaps between the four schema runs and the first stream run are all small (<= 6 bytes),
    so only the "depth-0 must be ``[``" rule reliably finds the true boundary.

    A run may carry a stray leading byte left over from the preceding tag (e.g. a bare
    ``9`` before ``[[``); we trim anything before the first ``[`` of each run so the
    reassembled text is clean grammar. ``start`` defaults to ``0xb8``; the opening ``[``
    can sit a byte or two before that, so we back up through contiguous printable bytes
    to ensure the result begins with ``['Protocol'``.
    """
    n = len(raw)
    if start < 0:
        start = 0
    if start >= n:
        return "", start

    # Back up to the true start of the printable run that contains `start`, so a leading
    # '[' immediately before the default offset is not lost.
    i = start
    while i > 0 and raw[i - 1] in _PRINTABLE:
        i -= 1

    out: list[str] = []
    depth = 0
    prev_end: int = i  # end of the last accepted schema run == stream boundary

    for (s, e) in _printable_runs(raw, i):
        if s - prev_end > _MAX_GAP:
            break  # large binary gap -> packet stream begins
        chunk = raw[s:e].decode("ascii")
        # A schema run is one or more bracket-balanced top-level lists. Stray tag nibbles
        # can leak in between lists (e.g. ']=73]F[d3be...' or a bare '9' before '[['); at
        # depth 0 the only legal character is '[', so we drop everything else there.
        if depth == 0 and "[" not in chunk:
            break  # not a list run (e.g. '|cw::PlayerInputReplayEvent') -> stream begins
        for c in chunk:
            if depth == 0 and c != "[":
                continue  # inter-list junk
            out.append(c)
            if c == "[":
                depth += 1
            elif c == "]":
                depth = max(0, depth - 1)  # guard against malformed extra ']'
        prev_end = e

    return "".join(out), prev_end


def extract_schema_text(raw: bytes, start: int = 0xB8) -> str:
    """Reassemble the embedded schema text from a raw ``.replay`` byte buffer.

    Thin wrapper over :func:`extract_schema_region` that drops the ``stream_start`` offset.
    """
    return extract_schema_region(raw, start)[0]


# ---------------------------------------------------------------------------
# Tokenizer.
# ---------------------------------------------------------------------------

# Order matters: punctuation, quoted strings, then bare words (ints / hashes / hex ids).
_TOKEN_RE = re.compile(
    r"""
      (?P<lbrack>\[)
    | (?P<rbrack>\])
    | (?P<comma>,)
    | (?P<eq>=)
    | (?P<qname>'[^']*')
    | (?P<word>[0-9A-Za-z_:]+)
    """,
    re.VERBOSE,
)


class _Tok:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: str, pos: int) -> None:
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"_Tok({self.kind!r}, {self.value!r}, @{self.pos})"


def _tokenize(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    pos = 0
    n = len(text)
    while pos < n:
        c = text[pos]
        if c in " \t\r\n":
            pos += 1
            continue
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ValueError(f"unexpected character {c!r} at offset {pos}")
        kind = m.lastgroup
        value = m.group()
        toks.append(_Tok(kind, value, pos))
        pos = m.end()
    return toks


# ---------------------------------------------------------------------------
# Recursive-descent parser -> generic node tree.
#
# Node shapes:
#   ("leaf", key, id_int)                  for  key '=' id
#   ("list", [child, ...], id_int|None)    for  '[' ... ']' ('=' id)?
# A "key" is one of: ("name", str) | ("int", str) | ("hash", str)
# ---------------------------------------------------------------------------


def _classify_key(tok: _Tok):
    if tok.kind == "qname":
        return ("name", tok.value[1:-1])  # strip surrounding quotes
    if tok.kind == "word":
        v = tok.value
        if v.isdigit():
            return ("int", v)
        return ("hash", v)  # non-decimal bare word used as a key (rare)
    raise ValueError(f"expected key, got {tok.kind} {tok.value!r} at {tok.pos}")


class _Parser:
    def __init__(self, toks: list[_Tok]) -> None:
        self.toks = toks
        self.i = 0

    def _peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _expect(self, kind: str) -> _Tok:
        t = self._peek()
        if t is None or t.kind != kind:
            got = "EOF" if t is None else f"{t.kind} {t.value!r}@{t.pos}"
            raise ValueError(f"expected {kind}, got {got}")
        return self._next()

    def parse(self):
        """Parse a sequence of one or more top-level lists; the schema is four of them.

        Asserts the entire token stream is consumed — leftover tokens mean the grammar
        or extraction is wrong, so we raise on them.
        """
        roots = []
        while self._peek() is not None:
            if not self._is_lbrack():
                extra = self._peek()
                raise ValueError(
                    f"trailing tokens after schema: {extra.kind} {extra.value!r} @ "
                    f"{extra.pos} ({len(self.toks) - self.i} tokens left)"
                )
            roots.append(self._parse_list())
        if len(roots) == 1:
            return roots[0]
        # Wrap multiple top-level lists in a synthetic root so downstream walks are uniform.
        return ("list", roots, None)

    def _parse_list(self):
        self._expect("lbrack")
        children = []
        if not self._is_rbrack():
            children.append(self._parse_entry())
            while self._is_comma():
                self._next()  # consume comma
                children.append(self._parse_entry())
        self._expect("rbrack")
        list_id = None
        if self._is_eq():
            self._next()
            list_id = self._parse_id()
        return ("list", children, list_id)

    def _parse_entry(self):
        t = self._peek()
        if t is None:
            raise ValueError("unexpected EOF parsing entry")
        if t.kind == "lbrack":
            return self._parse_list()
        # key '=' id
        key = _classify_key(self._next())
        self._expect("eq")
        id_int = self._parse_id()
        return ("leaf", key, id_int)

    def _parse_id(self) -> int:
        t = self._expect("word")
        try:
            return int(t.value, 16)
        except ValueError as exc:  # pragma: no cover - guards malformed ids
            raise ValueError(f"invalid hex id {t.value!r} @ {t.pos}") from exc

    def _is_lbrack(self) -> bool:
        t = self._peek()
        return t is not None and t.kind == "lbrack"

    def _is_rbrack(self) -> bool:
        t = self._peek()
        return t is not None and t.kind == "rbrack"

    def _is_comma(self) -> bool:
        t = self._peek()
        return t is not None and t.kind == "comma"

    def _is_eq(self) -> bool:
        t = self._peek()
        return t is not None and t.kind == "eq"


# ---------------------------------------------------------------------------
# Model derivation from the node tree.
# ---------------------------------------------------------------------------


def _is_class_name(name: str) -> bool:
    """Heuristic: does a quoted name denote a network class/message (vs a field)?

    Class names are namespaced (``cw::Foo``) or are top-level CamelCase
    message/event/singleton types (``AutoAimingEvent``, ``ScenarioStartEvent``).
    Field names are lowerCamelCase (``fromPoint``) or sentinel words like ``default``.
    """
    if "::" in name:
        return True
    if not name or not name[0].isupper():
        return False
    # Top-level CamelCase type that reads like a message/event/state/etc.
    return bool(
        re.search(
            r"(Message|Event|State|Singleton|Data|Pool|Condition|Transition|Handler|"
            r"Info|Status|Action|Compressor|Component|Machine|Replication)$",
            name,
        )
    )


# Known low-level type names that, when they appear as a field's sibling leaf,
# describe that field's wire type.
_TYPE_NAME_RE = re.compile(
    r"^(C[A-Z]\w*|"  # CInt32, CFloat, CUInt8, CString, ...
    r"u?int\d+|float\d*|double|bool|string|"
    r".*Vector.*|.*Vec\d.*|handle|Handle|EntityId|.*Ref)$"
)


def _looks_like_type(name: str) -> bool:
    return bool(_TYPE_NAME_RE.match(name))


def _walk(node, flat_index: dict[str, int]) -> None:
    """Populate ``flat_index`` with every quoted name -> id across the whole tree."""
    kind = node[0]
    if kind == "leaf":
        key, id_int = node[1], node[2]
        if key[0] == "name":
            flat_index.setdefault(key[1], id_int)
    else:  # list
        for child in node[1]:
            _walk(child, flat_index)


def _collect_fields(class_list_node) -> list[SchemaField]:
    """Build the field list for a class from its associated sub-list node.

    A class entry in the tree looks like::

        leaf('cw::Foo'=ID)  ,  leaf('default'=ID)  ,  list[ field-defs ... ]

    Each field def inside the sub-list is itself a small list whose first leaf is the
    field's ``name=id`` and whose subsequent leaves may include a type name
    (``'CInt32'=...``). We capture the field name + id, and the type when one of the
    sibling leaves is a recognised type name.
    """
    fields: list[SchemaField] = []
    for child in class_list_node[1]:
        if child[0] == "list":
            name = None
            fid = None
            ftype = None
            for sub in child[1]:
                if sub[0] == "leaf" and sub[1][0] == "name":
                    nm = sub[1][1]
                    if name is None:
                        name = nm
                        fid = sub[2]
                    elif ftype is None and _looks_like_type(nm):
                        ftype = nm
            if name is not None and fid is not None:
                fields.append(SchemaField(name=name, id=fid, type=ftype))
        elif child[0] == "leaf" and child[1][0] == "name":
            # A bare named leaf directly inside the class list is also a field.
            fields.append(SchemaField(name=child[1][1], id=child[2]))
    return fields


def _build_classes(node, proto: Protocol) -> None:
    """Find class definitions in the tree and populate ``proto`` class maps.

    A class is a named leaf whose name passes :func:`_is_class_name`, immediately
    followed (within the same parent list) by a sibling list node that holds its
    fields. We scan every list level so nested class registries are caught too.
    """
    if node[0] != "list":
        return
    children = node[1]
    for idx, child in enumerate(children):
        if child[0] == "leaf" and child[1][0] == "name" and _is_class_name(child[1][1]):
            cname = child[1][1]
            cid = child[2]
            # Find the next sibling list node that holds the field defs.
            field_list = None
            for follow in children[idx + 1 :]:
                if follow[0] == "list":
                    field_list = follow
                    break
                if follow[0] == "leaf" and follow[1][0] == "name" and _is_class_name(follow[1][1]):
                    break  # ran into the next class without a field list
            fields = _collect_fields(field_list) if field_list is not None else []
            cdef = ClassDef(name=cname, id=cid, fields=fields)
            # First definition wins for stability; same name shouldn't recur.
            proto.classes_by_name.setdefault(cname, cdef)
            proto.classes_by_id.setdefault(cid, cdef)
        # Recurse into every list to catch nested class registries.
        if child[0] == "list":
            _build_classes(child, proto)


def parse_schema(text: str, commit: str) -> Protocol:
    """Tokenize + recursive-descent parse the schema text into a :class:`Protocol`.

    Raises if the grammar does not consume the entire input — leftover tokens mean the
    extraction or grammar assumptions are wrong.
    """
    toks = _tokenize(text)
    tree = _Parser(toks).parse()  # raises on trailing tokens

    proto = Protocol(build_commit=commit)
    _walk(tree, proto.flat_index)
    _build_classes(tree, proto)
    return proto


# ---------------------------------------------------------------------------
# Deterministic JSON dump.
# ---------------------------------------------------------------------------


def _proto_to_dict(proto: Protocol) -> dict:
    classes = []
    for name in sorted(proto.classes_by_name):
        cdef = proto.classes_by_name[name]
        classes.append(
            {
                "name": cdef.name,
                "id": cdef.id,
                "id_hex": f"{cdef.id:02X}",
                "fields": [
                    {
                        "name": f.name,
                        "id": f.id,
                        "id_hex": f"{f.id:02X}",
                        "type": f.type,
                    }
                    for f in cdef.fields
                ],
            }
        )
    return {
        "build_commit": proto.build_commit,
        "class_count": len(proto.classes_by_name),
        "flat_index_count": len(proto.flat_index),
        "classes": classes,
        "flat_index": {k: proto.flat_index[k] for k in sorted(proto.flat_index)},
    }


def dump_schema(proto: Protocol, out_path: str) -> None:
    """Write a deterministic (sorted-key) JSON representation of ``proto``."""
    data = _proto_to_dict(proto)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
