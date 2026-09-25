from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from ..config import Config, ModelPricing
from ..models import BBox, ExtractedBlock, ExtractedPage
from .base import TranslationError
from .claude import ChunkTranslation, ClaudeEngine, estimate_usd


class FakeMessages:
    """Dublê do endpoint de mensagens: guarda cada chamada e devolve respostas em fila."""

    def __init__(self, responses: list[object]) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self.messages = FakeMessages(responses)


def response_with(
    lines: list[dict],
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 1000,
    output_tokens: int = 200,
):
    payload = ChunkTranslation.model_validate({"lines": lines}).model_dump_json()
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", text=""), SimpleNamespace(type="text", text=payload)],
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
        ),
    )


def refusal_response():
    return SimpleNamespace(
        content=[],
        stop_reason="refusal",
        stop_details=SimpleNamespace(category="other"),
        usage=SimpleNamespace(input_tokens=10, output_tokens=0, cache_read_input_tokens=0),
    )


def truncated_response():
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"lines": [')],
        stop_reason="max_tokens",
        stop_details=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=16000, cache_read_input_tokens=0),
    )


@pytest.fixture
def chapter_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "library" / "serie" / "001"
    directory.mkdir(parents=True)
    for name in ("001.jpg", "002.jpg", "003.jpg"):
        cv2.imwrite(str(directory / name), np.full((1200, 800, 3), 255, dtype=np.uint8))
    return directory


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(root=tmp_path)


def page(index: int, block_count: int = 1) -> ExtractedPage:
    return ExtractedPage(
        index=index,
        image=f"{index:03d}.jpg",
        width=800,
        height=1200,
        image_sha256="0" * 64,
        blocks=tuple(
            ExtractedBlock(
                id=f"p{index:03d}-b{position:02d}",
                bbox=BBox(x=10 * position, y=20 * position, w=100, h=50),
                raw_text=f"LINE {position}",
                confidence=80.0,
            )
            for position in range(1, block_count + 1)
        ),
    )


def engine_with(cfg: Config, responses: list[object]) -> tuple[ClaudeEngine, FakeClient]:
    client = FakeClient(responses)
    return ClaudeEngine(cfg, client=client), client


def test_sends_the_page_image_alongside_the_ocr_text(cfg: Config, chapter_dir: Path):
    engine, client = engine_with(
        cfg,
        [response_with([{"page_index": 1, "block_id": "p001-b01", "source_text": "LINE 1", "translation": "FALA 1"}])],
    )

    engine.translate_chapter([page(1)], {}, chapter_dir)

    content = client.messages.calls[0]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text", "image", "text"]
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert "p001-b01" in content[2]["text"]


def test_marks_the_system_prompt_for_caching(cfg: Config, chapter_dir: Path):
    engine, client = engine_with(cfg, [response_with([])])

    engine.translate_chapter([page(1)], {}, chapter_dir)

    system = client.messages.calls[0]["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}


def test_puts_the_series_glossary_in_the_system_prompt(cfg: Config, chapter_dir: Path):
    engine, client = engine_with(cfg, [response_with([])])

    engine.translate_chapter([page(1)], {"Sect Master": "Mestre da Seita"}, chapter_dir)

    system_text = " ".join(block["text"] for block in client.messages.calls[0]["system"])
    assert "Sect Master -> Mestre da Seita" in system_text


def test_asks_for_a_json_schema_response(cfg: Config, chapter_dir: Path):
    engine, client = engine_with(cfg, [response_with([])])

    engine.translate_chapter([page(1)], {}, chapter_dir)

    output_config = client.messages.calls[0]["output_config"]
    assert output_config["format"]["type"] == "json_schema"
    assert "lines" in json.dumps(output_config["format"]["schema"])


def test_splits_pages_into_chunks_of_the_configured_size(cfg: Config, chapter_dir: Path):
    cfg = cfg.model_copy(update={"translation": cfg.translation.model_copy(update={"chunk_pages": 2})})
    engine, client = engine_with(cfg, [response_with([]), response_with([])])

    engine.translate_chapter([page(1), page(2), page(3)], {}, chapter_dir)

    assert len(client.messages.calls) == 2


def test_maps_translations_back_to_their_bounding_boxes(cfg: Config, chapter_dir: Path):
    engine, _ = engine_with(
        cfg,
        [response_with([{"page_index": 1, "block_id": "p001-b01", "source_text": "LINE 1", "translation": "FALA 1"}])],
    )

    pages = engine.translate_chapter([page(1)], {}, chapter_dir)

    assert pages[0].blocks[0].bbox == BBox(x=10, y=20, w=100, h=50)
    assert pages[0].blocks[0].text == "FALA 1"


def test_keeps_lines_the_detector_missed_without_a_box(cfg: Config, chapter_dir: Path):
    engine, _ = engine_with(
        cfg,
        [response_with([{"page_index": 1, "block_id": None, "source_text": "SFX", "translation": "TUM"}])],
    )

    blocks = engine.translate_chapter([page(1)], {}, chapter_dir)[0].blocks

    assert blocks[0].bbox is None
    assert blocks[0].id == "p001-new01"


def test_drops_items_the_model_marked_as_not_speech(cfg: Config, chapter_dir: Path):
    engine, _ = engine_with(
        cfg,
        [
            response_with(
                [
                    {"page_index": 1, "block_id": "p001-b01", "source_text": "ruido", "translation": "  "},
                    {"page_index": 1, "block_id": "p001-b02", "source_text": "LINE 2", "translation": "FALA 2"},
                ]
            )
        ],
    )

    blocks = engine.translate_chapter([page(1, block_count=2)], {}, chapter_dir)[0].blocks

    assert [block.text for block in blocks] == ["FALA 2"]


def test_preserves_the_order_the_model_returned(cfg: Config, chapter_dir: Path):
    engine, _ = engine_with(
        cfg,
        [
            response_with(
                [
                    {"page_index": 1, "block_id": "p001-b02", "source_text": "LINE 2", "translation": "SEGUNDA"},
                    {"page_index": 1, "block_id": "p001-b01", "source_text": "LINE 1", "translation": "PRIMEIRA"},
                ]
            )
        ],
    )

    blocks = engine.translate_chapter([page(1, block_count=2)], {}, chapter_dir)[0].blocks

    assert [block.text for block in blocks] == ["SEGUNDA", "PRIMEIRA"]


def test_returns_no_pages_for_an_empty_chapter(cfg: Config, chapter_dir: Path):
    engine, client = engine_with(cfg, [])

    assert engine.translate_chapter([], {}, chapter_dir) == []
    assert client.messages.calls == []


def test_raises_when_the_model_refuses(cfg: Config, chapter_dir: Path):
    engine, _ = engine_with(cfg, [refusal_response()])

    with pytest.raises(TranslationError, match="recusado"):
        engine.translate_chapter([page(1)], {}, chapter_dir)


def test_raises_when_the_response_is_truncated(cfg: Config, chapter_dir: Path):
    engine, _ = engine_with(cfg, [truncated_response()])

    with pytest.raises(TranslationError, match="chunk_pages"):
        engine.translate_chapter([page(1)], {}, chapter_dir)


def test_raises_when_the_page_image_is_missing(cfg: Config, tmp_path: Path):
    engine, _ = engine_with(cfg, [response_with([])])

    with pytest.raises(TranslationError, match="nao consegui ler a imagem"):
        engine.translate_chapter([page(1)], {}, tmp_path)


def test_logs_token_usage_and_estimated_cost(cfg: Config, chapter_dir: Path, caplog):
    engine, _ = engine_with(cfg, [response_with([], input_tokens=20000, output_tokens=4000)])

    with caplog.at_level("INFO", logger="mangatl.claude"):
        engine.translate_chapter([page(1)], {}, chapter_dir)

    assert "input_tokens=20000" in caplog.text
    assert "output_tokens=4000" in caplog.text


@pytest.mark.parametrize(
    ("name", "pages", "expected"),
    [
        ("o capitulo medido do README", 40, 0.15),
        ("capitulo vazio nao custa", 0, 0.0),
        ("o de 155 fatias", 155, 0.58),
    ],
)
def test_estimates_the_cost_from_the_measured_average(name: str, pages: int, expected: float):
    sonnet = ModelPricing(input=2.0, output=10.0, cache_read=0.2)

    assert estimate_usd(sonnet, pages) == pytest.approx(expected, abs=0.01), name
