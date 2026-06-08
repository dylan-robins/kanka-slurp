from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import requests

from kanka_slurp.api import KankaSlurp
from kanka_slurp.models import EntityMetadata


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        json_data: Any = None,
        text: str = "",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._json_data = json_data
        self.text = text
        self._chunks = chunks or [b"data"]
        self.closed = False

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def iter_content(self, _chunk_size: int):
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


def test_download_image_closes_response_on_rejection(monkeypatch: pytest.MonkeyPatch, slurper: KankaSlurp) -> None:
    response = FakeResponse(
        headers={
            "content-type": "text/html; charset=utf-8",
            "content-length": "10",
        },
        text="not an image",
    )
    monkeypatch.setattr("kanka_slurp.api.requests.get", Mock(return_value=response))

    assert slurper.download_image("https://example.com/bad.png") is None
    assert response.closed is True


def test_download_image_caches_and_closes_response(monkeypatch: pytest.MonkeyPatch, slurper: KankaSlurp) -> None:
    response = FakeResponse(
        headers={
            "content-type": "image/png",
            "content-length": "4",
        },
        chunks=[b"abcd"],
    )
    monkeypatch.setattr("kanka_slurp.api.requests.get", Mock(return_value=response))

    result = slurper.download_image("https://cdn.example.com/assets/shared.png", subdir="media")

    assert result == "media/shared.png"
    assert response.closed is True
    assert (Path(slurper.out_path) / result).exists()
    assert slurper._find_local_for_url("https://cdn.example.com/assets/shared.png") == "media/shared.png"


def test_find_local_for_url_returns_none_for_ambiguous_basenames(slurper: KankaSlurp) -> None:
    first = slurper.out_path / "alpha" / "shared.png"
    second = slurper.out_path / "beta" / "shared.png"
    first.parent.mkdir(parents=True, exist_ok=True)
    second.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    assert slurper._find_local_for_url("https://cdn.example.com/shared.png") is None


def test_fetch_items_details_rewrites_links_after_embedded_downloads(
    monkeypatch: pytest.MonkeyPatch,
    slurper: KankaSlurp,
) -> None:
    image_url = "https://cdn.example.com/images/shared.png"
    detail_payload = {
        "data": {
            "id": 42,
            "name": "Sample Entity",
            "type": "npc",
            "entry": f'<p>Look at <img src="{image_url}" alt="sample"></p>',
            "attachments": {
                "full": image_url,
            },
        }
    }

    session_response = Mock()
    session_response.json.return_value = detail_payload
    session_response.raise_for_status.return_value = None
    slurper.session.get = Mock(return_value=session_response)

    image_response = FakeResponse(
        headers={
            "content-type": "image/png",
            "content-length": "4",
        },
        chunks=[b"abcd"],
    )
    monkeypatch.setattr("kanka_slurp.api.requests.get", Mock(return_value=image_response))

    slurper.fetch_items_details(
        "entities",
        [
            {
                "id": 42,
                "name": "Sample Entity",
                "type": "npc",
                "urls": {"api": "https://api.kanka.io/1.0/campaigns/123/entities/42"},
            }
        ],
    )

    md_path = slurper.out_path / "npc" / "42-sample-entity.md"
    assert md_path.exists()
    content = md_path.read_text(encoding="utf-8")
    assert "media/shared.png" in content
    assert image_response.closed is True


def test_build_markdown_filename_uses_id_and_name(slurper: KankaSlurp) -> None:
    assert slurper._build_markdown_filename("42", "Sample Entity") == "42-sample-entity.md"
    assert slurper._build_markdown_filename("42", "  ") == "42.md"


def test_fetch_items_details_update_mode_skips_unchanged_and_rewrites_changed(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path), verbose=False)

    slurper._save_item_markdown(
        "entities",
        "1",
        "<p>unchanged</p>",
        EntityMetadata(
            id="1",
            name="Kept Name",
            entity_type="npc",
            updated_at="2026-01-01T00:00:00Z",
        ),
    )
    slurper._save_item_markdown(
        "entities",
        "2",
        "<p>old</p>",
        EntityMetadata(
            id="2",
            name="Old Name",
            entity_type="npc",
            updated_at="2026-01-01T00:00:00Z",
        ),
    )

    changed_response = Mock()
    changed_response.json.return_value = {
        "data": {
            "id": 2,
            "name": "New Name",
            "type": "npc",
            "entry": "<p>updated</p>",
            "updated_at": "2026-02-01T00:00:00Z",
        }
    }
    changed_response.raise_for_status.return_value = None
    slurper.session.get = Mock(return_value=changed_response)
    slurper.download_image = Mock(return_value=None)
    slurper.extract_and_download_files = Mock()

    result = slurper.fetch_items_details(
        "entities",
        [
            {
                "id": 1,
                "name": "Kept Name",
                "type": "npc",
                "updated_at": "2026-01-01T00:00:00Z",
                "urls": {"api": "https://api.kanka.io/1.0/campaigns/123/entities/1"},
            },
            {
                "id": 2,
                "name": "New Name",
                "type": "npc",
                "updated_at": "2026-02-01T00:00:00Z",
                "urls": {"api": "https://api.kanka.io/1.0/campaigns/123/entities/2"},
            },
        ],
        update_mode=True,
    )

    assert result == {"updated": 1, "skipped": 1, "total": 2}
    assert slurper.session.get.call_count == 1
    assert (tmp_path / "npc" / "1-kept-name.md").exists()
    assert (tmp_path / "npc" / "2-new-name.md").exists()
    assert not (tmp_path / "npc" / "2-old-name.md").exists()
    assert "updated" in (tmp_path / "npc" / "2-new-name.md").read_text(encoding="utf-8")


def test_fetch_paginated_uses_meta_pagination(monkeypatch: pytest.MonkeyPatch, slurper: KankaSlurp) -> None:
    page_1 = Mock()
    page_1.json.return_value = {
        "data": [{"id": 1}],
        "meta": {"pagination": {"current_page": 1, "last_page": 2}},
    }
    page_2 = Mock()
    page_2.json.return_value = {
        "data": [{"id": 2}],
        "meta": {"pagination": {"current_page": 2, "last_page": 2}},
    }
    slurper._get = Mock(side_effect=[page_1, page_2])

    items = slurper.fetch_paginated("entities")

    assert items == [{"id": 1}, {"id": 2}]
