from __future__ import annotations

from pathlib import Path

import pytest

from kanka_slurp.api import KankaSlurp


@pytest.fixture
def slurper(tmp_path: Path) -> KankaSlurp:
    return KankaSlurp(
        token="test-token",
        campaign_id="123",
        out_dir=str(tmp_path),
        verbose=False,
    )
