from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from kanka_slurp import cli
from kanka_slurp.api import KankaSlurp
from kanka_slurp.constants import MAX_IMAGE_SIZE
from kanka_slurp.logging_config import TqdmLoggingHandler


class ResponseStub:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers=None,
        text: str = "",
        json_data=None,
        chunks=None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json_data = json_data
        self._chunks = chunks or [b"chunk"]
        self.closed = False

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def iter_content(self, _chunk_size: int):
        yield from self._chunks

    def close(self):
        self.closed = True


def test_init_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        KankaSlurp("", "123", out_dir=str(tmp_path))
    with pytest.raises(ValueError):
        KankaSlurp("token", "", out_dir=str(tmp_path))
    with pytest.raises(ValueError):
        KankaSlurp("token", "abc", out_dir=str(tmp_path))


def test_save_checkpoint_handles_ioerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    slurper.checkpoint = {"details__entities": ["1"]}
    write_text_mock = Mock(side_effect=OSError("boom"))
    monkeypatch.setattr(Path, "write_text", write_text_mock)
    slurper._save_checkpoint()

    write_text_mock.assert_called_once()


def test_get_handles_request_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    slurper.min_interval = 0.0
    monkeypatch.setattr("kanka_slurp.api.time.sleep", Mock())
    slurper.session.get = Mock(side_effect=requests.RequestException("network down"))

    with pytest.raises(requests.RequestException):
        slurper._get("campaigns/123/entities")


def test_fetch_paginated_handles_scalar_and_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    page_1 = Mock()
    page_1.json.return_value = {
        "data": {"id": 1},
        "meta": {"pagination": {"current_page": 1, "last_page": 2}},
    }
    page_2 = Mock()
    page_2.json.return_value = {"data": []}
    slurper._get = Mock(side_effect=[page_1, page_2])

    assert slurper.fetch_paginated("entities") == [{"id": 1}]


def test_fetch_paginated_falls_back_to_limit_increment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    page_1 = Mock()
    page_1.json.return_value = {"data": [{"id": 1}]}
    page_2 = Mock()
    page_2.json.return_value = {"data": []}
    slurper._get = Mock(side_effect=[page_1, page_2])

    assert slurper.fetch_paginated("entities", params={"limit": 1}) == [{"id": 1}]


def test_extract_helpers_handle_empty_values(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))

    assert slurper._extract_filename_from_url("") is None
    assert slurper._extract_id_from_url("") is None
    assert slurper._find_local_for_url("ftp://example.com/x.png") is None
    assert slurper._find_local_for_url("https://example.com/") is None


def test_find_local_for_url_unique_rglob_match(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    nested = tmp_path / "deep" / "unique.png"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(b"data")

    assert (
        slurper._find_local_for_url("https://cdn.example.com/unique.png")
        == "deep/unique.png"
    )


@pytest.mark.parametrize(
    "response, url",
    [
        (
            ResponseStub(
                headers={
                    "content-type": "image/png",
                    "content-length": str(MAX_IMAGE_SIZE + 1),
                }
            ),
            "https://cdn.example.com/oversize.png",
        ),
        (
            ResponseStub(
                status_code=404, headers={"content-type": "image/png"}, text="missing"
            ),
            "https://cdn.example.com/missing.png",
        ),
        (
            ResponseStub(headers={"content-type": "image/png", "content-length": "1"}),
            "https://cdn.example.com",
        ),
    ],
)
def test_download_image_rejection_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: ResponseStub, url: str
) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    monkeypatch.setattr("kanka_slurp.api.requests.get", Mock(return_value=response))

    assert slurper.download_image(url) is None
    assert response.closed is True


def test_download_image_exceeds_stream_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    response = ResponseStub(
        headers={"content-type": "image/png", "content-length": "1"},
        chunks=[b"x" * (MAX_IMAGE_SIZE + 1)],
    )
    monkeypatch.setattr("kanka_slurp.api.requests.get", Mock(return_value=response))

    assert slurper.download_image("https://cdn.example.com/big.png") is None
    assert response.closed is True


def test_convert_html_to_markdown_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))

    class BrokenHTML2Text:
        def handle(self, _html):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "kanka_slurp.api.html2text.HTML2Text", Mock(return_value=BrokenHTML2Text())
    )

    assert slurper.convert_html_to_markdown("<p>hi</p>") == "<p>hi</p>"


def test_fetch_items_details_skips_missing_and_processed_items(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    slurper.checkpoint["details__entities"] = ["2"]
    slurper.logger = Mock()
    slurper.session.get = Mock()

    slurper.fetch_items_details(
        "entities",
        [
            {"name": "Missing id", "urls": {}},
            {
                "id": 2,
                "name": "Processed",
                "urls": {"api": "https://api.kanka.io/1.0/campaigns/123/entities/2"},
            },
        ],
    )

    slurper.session.get.assert_not_called()


def test_fetch_items_details_handles_http_error(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    response = ResponseStub(
        status_code=500, headers={"content-type": "application/json"}, text="boom"
    )
    slurper.session.get = Mock(return_value=response)

    slurper.fetch_items_details(
        "entities",
        [
            {
                "id": 1,
                "name": "Bad",
                "urls": {"api": "https://api.kanka.io/1.0/campaigns/123/entities/1"},
            }
        ],
    )


def test_fetch_items_details_handles_unexpected_payloads(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    slurper._get = Mock(return_value=Mock(json=Mock(return_value={"data": []})))

    slurper.fetch_items_details("entities", [{"id": 1, "name": "Weird"}])


def test_fetch_items_details_downloads_primary_image(tmp_path: Path) -> None:
    slurper = KankaSlurp("token", "123", out_dir=str(tmp_path))
    payload = {
        "data": {
            "id": 1,
            "name": "Imagey",
            "type": "npc",
            "entry": "<p>body</p>",
            "image_full": "https://cdn.example.com/primary.png",
        }
    }
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    slurper.session.get = Mock(return_value=response)
    slurper.download_image = Mock(return_value="npc/primary.png")
    slurper.extract_and_download_files = Mock()

    slurper.fetch_items_details(
        "entities",
        [
            {
                "id": 1,
                "name": "Imagey",
                "type": "npc",
                "urls": {"api": "https://api.kanka.io/1.0/campaigns/123/entities/1"},
            }
        ],
    )

    slurper.download_image.assert_called_once_with(
        "https://cdn.example.com/primary.png", subdir="npc"
    )
    slurper.extract_and_download_files.assert_called_once()


def test_cli_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KANKA_API_TOKEN", raising=False)
    monkeypatch.delenv("KANKA_CAMPAIGN_ID", raising=False)
    monkeypatch.delenv("KANKA_CAMPAIGN", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", Mock(return_value=True))

    with pytest.raises(RuntimeError):
        cli.load_config_from_env(".env")


def test_logging_handler_emit_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = TqdmLoggingHandler()
    record = Mock()
    handler.format = Mock(side_effect=RuntimeError("boom"))
    handler.handleError = Mock()

    handler.emit(record)

    handler.handleError.assert_called_once_with(record)
