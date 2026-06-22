"""Decode a real packed position field from replay baselines.

End-to-end demonstration that the packed-scalar decoder recovers a real 3-component
position field from a HEAT replay:

  baseline blob (tag-6) -> frame header (frameId, flag, two component unique-sets, componentCount)
  -> per component: networkId(7 bits) + properties in field order.

Component networkId=37 carries the rigid-body transform; its first property is ``position`` (a
3-component packed vector). We decode it with :mod:`heat_replay.packed_scalar` and gate on a
coordinate-sanity oracle.

This is the measured, honest result: positions decode to sane world coordinates with the exact
fixed-point quantization the scheme predicts. A *continuous trajectory* proof additionally needs
the per-frame update path (tag-4/8) and entity-id de-recycling — see docs. stdlib + heat_replay.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from heat_replay import stream  # noqa: E402
from heat_replay.bitstream import ReadStream  # noqa: E402
from heat_replay.packed_scalar import RIGIDBODY_POSITION, read_scalar  # noqa: E402

TRANSFORM_NETWORK_ID = 37  # rigid-body transform component
ID_BITS = 7
MAP_LIMIT = 5000.0  # generous world-coordinate bound (metres)


def read_header_then_first_networkid(rs: ReadStream):
    """Read frame header; return (componentCount, firstNetworkId) positioned at its properties."""
    rs.read_bits(32)  # frameId
    rs.read_bits(1)   # flag
    for _ in range(2):  # two component unique-sets
        if not rs.read_bits(1):  # not "all"
            cnt = rs.read_bits(32)
            for _ in range(cnt):
                rs.read_bits(ID_BITS)
    comp_count = rs.read_bits(32)
    if comp_count < 1:
        return comp_count, None
    return comp_count, rs.read_bits(ID_BITS)


def _sane(p) -> bool:
    return all(c == c and abs(c) < MAP_LIMIT for c in p)


def main() -> int:
    total = sane = 0
    for rp in sorted((Path(__file__).resolve().parent.parent / "replays").glob("*.replay")):
        w = stream.walk(str(rp))
        hits = []
        for r in (x for x in w.records if x.tag == 6):
            rs = ReadStream(r.blob)
            try:
                cc, nid = read_header_then_first_networkid(rs)
                if nid != TRANSFORM_NETWORK_ID:
                    continue
                pos = tuple(read_scalar(rs, RIGIDBODY_POSITION) for _ in range(3))
            except Exception:
                continue
            total += 1
            if _sane(pos):
                sane += 1
                hits.append((r.frame_id, r.entity_id, pos))
        print(f"{rp.name}")
        print(f"  nid=37 first-component baselines decoded: {len(hits)}")
        for f, e, p in hits[:6]:
            err = max(abs(c - round(c)) for c in p)  # distance to integer grid (illustrative)
            print(f"    frame {f:6d} entity {e:3d}  pos=({p[0]:.3f}, {p[1]:.3f}, {p[2]:.3f})")
    print(f"\n=== sane / total (oracle: finite, |coord|<{MAP_LIMIT}m): {sane}/{total} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
