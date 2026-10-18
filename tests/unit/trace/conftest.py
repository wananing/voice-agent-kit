from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

SAMPLES = Path(__file__).parent / "samples"
SAMPLE_NAMES = ("chat", "tool", "interrupted", "error")


def load_sample(name: str) -> dict[str, Any]:
    with (SAMPLES / f"{name}.json").open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def samples() -> dict[str, dict[str, Any]]:
    return {n: copy.deepcopy(load_sample(n)) for n in SAMPLE_NAMES}
