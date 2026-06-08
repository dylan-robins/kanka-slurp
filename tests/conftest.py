from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kanka_slurp.api import KankaSlurp


@pytest.fixture
def slurper(tmp_path: Path) -> KankaSlurp:
    return KankaSlurp(
        token="test-token",
        campaign_id="123",
        out_dir=str(tmp_path),
        verbose=False,
    )
