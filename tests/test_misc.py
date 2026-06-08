from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
import requests

from kanka_slurp import cli
from kanka_slurp.api import KankaSlurp
from kanka_slurp.logging_config import TqdmLoggingHandler, setup_logging
from kanka_slurp.parsers import ImageLinkRewriter


class DummyResponse:
    def __init__(
        self, status_code: int = 200, json_data=None, headers=None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def test_load_config_from_env_reads_expected_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KANKA_API_TOKEN", "token")
    monkeypatch.setenv("KANKA_CAMPAIGN_ID", "456")
    monkeypatch.setenv("KANKA_API_BASE", "https://example.invalid/api/")
    monkeypatch.setattr(cli, "load_dotenv", Mock(return_value=True))

    cfg = cli.load_config_from_env(".env")

    assert cfg == {
        "token": "token",
        "campaign": "456",
        "api_base": "https://example.invalid/api/",
    }


def test_main_wires_cli_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    slurper = Mock()
    kanka_ctor = Mock(return_value=slurper)
    monkeypatch.setattr(
        cli,
        "load_config_from_env",
        Mock(
            return_value={
                "token": "token",
                "campaign": "456",
                "api_base": "https://example.invalid/api/",
            }
        ),
    )
    monkeypatch.setattr(cli, "KankaSlurp", kanka_ctor)

    exit_code = cli.main(
        ["--out", "outdir", "--api-base", "https://override.invalid", "--verbose"]
    )

    assert exit_code == 0
    cast(Any, kanka_ctor).assert_called_once_with(
        "token",
        "456",
        api_base="https://override.invalid",
        out_dir="outdir",
        verbose=True,
    )
    slurper.slurp.assert_called_once_with(update=False)


def test_main_update_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    slurper = Mock()
    kanka_ctor = Mock(return_value=slurper)
    monkeypatch.setattr(
        cli,
        "load_config_from_env",
        Mock(
            return_value={
                "token": "token",
                "campaign": "456",
                "api_base": "https://example.invalid/api/",
            }
        ),
    )
    monkeypatch.setattr(cli, "KankaSlurp", kanka_ctor)

    exit_code = cli.main(["--update"])

    assert exit_code == 0
    slurper.slurp.assert_called_once_with(update=True)


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)

    slurper._mark_processed("entities", "99")

    assert slurper._is_processed("entities", "99") is True
    assert slurper.checkpoint_file.exists()
    assert '"99"' in slurper.checkpoint_file.read_text(encoding="utf-8")


def test_load_checkpoint_handles_invalid_json(tmp_path: Path) -> None:
    (tmp_path / ".checkpoint.json").write_text("{not-json}", encoding="utf-8")

    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)

    assert slurper.checkpoint == {}


def test_save_json_writes_file(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)

    slurper.save_json("entities", [{"id": 1, "name": "One"}])

    assert (tmp_path / "entities.json").read_text(encoding="utf-8") == (
        '[\n  {\n    "id": 1,\n    "name": "One"\n  }\n]'
    )


def test_get_retries_after_429(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)
    slurper.min_interval = 0.0
    sleep_mock = Mock()
    monkeypatch.setattr("kanka_slurp.api.time.sleep", sleep_mock)

    first = DummyResponse(
        status_code=429, headers={"Retry-After": "1"}, text="rate limited"
    )
    second = DummyResponse(status_code=200, json_data={"ok": True}, text="ok")
    slurper.session.get = Mock(side_effect=[first, second])

    resp = slurper._get("campaigns/123/entities")

    assert resp.json() == {"ok": True}
    assert slurper.session.get.call_count == 2
    sleep_mock.assert_called_once_with(1)


def test_get_raises_http_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)
    slurper.min_interval = 0.0
    monkeypatch.setattr("kanka_slurp.api.time.sleep", Mock())

    slurper.session.get = Mock(return_value=DummyResponse(status_code=500, text="boom"))

    with pytest.raises(requests.HTTPError):
        slurper._get("campaigns/123/entities")


def test_determine_entity_type_prefers_payload_and_falls_back_to_url(
    tmp_path: Path,
) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)

    assert (
        slurper._determine_entity_type(
            "entities",
            {"type": "Race"},
            {"type": "NPC"},
            "https://api.kanka.io/1.0/campaigns/123/entities/42",
        )
        == "npc"
    )
    assert (
        slurper._determine_entity_type(
            "entities",
            {},
            {},
            "https://api.kanka.io/1.0/campaigns/123/races/42",
        )
        == "races"
    )
    assert slurper._determine_entity_type("entities", {}, {}, None) == "entities"


def test_rewrite_image_links_handles_markdown_and_html(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)
    media_file = tmp_path / "media" / "pic.png"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"data")

    md = (
        '![alt](https://cdn.example.com/pic.png "title") '
        "[text](https://cdn.example.com/pic.png) "
        '<img src="https://cdn.example.com/pic.png" alt="x">'
    )
    rewritten = slurper._rewrite_image_links(md, "npc")

    assert '![alt](media/pic.png "title")' in rewritten
    assert "[text](media/pic.png)" in rewritten
    assert '<img src="media/pic.png" alt="x"/>' in rewritten


def test_generate_index(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)
    slurper._index = {
        "entities": [
            {"name": "B", "path": "npc/2-b.md", "type": "npc"},
            {"name": "A", "path": "pc/1-a.md", "type": "pc"},
        ]
    }

    slurper._generate_index()

    content = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "# Index for 123" in content
    assert "## entities" in content
    assert "### npc" in content
    assert "- [A](pc/1-a.md)" in content
    assert "- [B](npc/2-b.md)" in content


def test_slurp_orchestrates_entities_and_index(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)
    slurper.fetch_paginated = Mock(return_value=[{"id": 1}])
    slurper.extract_and_download_files = Mock()
    slurper.fetch_items_details = Mock()
    slurper._generate_index = Mock()
    slurper.logger = Mock()
    slurper._index = {
        "entities": [{"name": "A", "path": "entities/1-a.md", "type": "npc"}]
    }

    slurper.slurp()

    slurper.fetch_paginated.assert_called_once_with("entities")
    slurper.extract_and_download_files.assert_called_once_with([{"id": 1}], "entities")
    slurper.fetch_items_details.assert_called_once_with("entities", [{"id": 1}])
    slurper._generate_index.assert_called_once()


def test_slurp_update_mode_uses_update_mode(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)
    slurper.fetch_paginated = Mock(return_value=[{"id": 1}])
    slurper.extract_and_download_files = Mock()
    slurper.fetch_items_details = Mock(
        return_value={"updated": 1, "skipped": 0, "total": 1}
    )
    slurper._generate_index = Mock()
    slurper.logger = Mock()
    slurper._index = {
        "entities": [{"name": "A", "path": "entities/1-a.md", "type": "npc"}]
    }

    slurper.slurp(update=True)

    slurper.fetch_paginated.assert_called_once_with("entities")
    slurper.fetch_items_details.assert_called_once_with(
        "entities", [{"id": 1}], update_mode=True
    )
    slurper._generate_index.assert_called_once()


def test_image_link_rewriter_rewrites_src(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = Mock(return_value="media/pic.png")
    parser = ImageLinkRewriter(resolver)

    parser.feed('<p><img src="https://cdn.example.com/pic.png" alt="x"></p>')

    assert '<img src="media/pic.png" alt="x"/>' in parser.get_result()
    resolver.assert_called_once_with("https://cdn.example.com/pic.png")


def test_setup_logging_sets_debug_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kanka_slurp.logging_config.tqdm.write", Mock())

    logger = setup_logging(verbose=True)

    assert logger.level == 10
    assert any(isinstance(handler, TqdmLoggingHandler) for handler in logger.handlers)
