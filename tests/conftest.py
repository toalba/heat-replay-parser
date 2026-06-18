import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPLAYS = ROOT / "replays"

# Live (intact) sample fixtures, by short key. All correspond to build 95e710fa.
SAMPLES = {
    "vietnam": "02_vietnam_control_2026_06_18_00_19_36_37ad861d.replay",
    "sunstroke": "03_sunstroke_control_2026_06_17_23_24_31_4db64cfa.replay",
    "nordoko": "04_nordoko_conquest_2026_06_17_23_37_12_a9e07f57.replay",
    "friendshipdam": "05_friendshipdam_conquest_2026_06_17_23_53_52_855ae54d.replay",
}


@pytest.fixture
def replay_dir() -> pathlib.Path:
    return REPLAYS


@pytest.fixture
def samples() -> dict[str, pathlib.Path]:
    paths = {k: REPLAYS / v for k, v in SAMPLES.items()}
    missing = [p.name for p in paths.values() if not p.exists()]
    if missing:
        pytest.skip(f"sample replays not present: {', '.join(missing)}")
    return paths


@pytest.fixture(params=list(SAMPLES), ids=list(SAMPLES))
def sample_path(request) -> pathlib.Path:
    path = REPLAYS / SAMPLES[request.param]
    if not path.exists():
        pytest.skip(f"sample replay not present: {path.name}")
    return path
