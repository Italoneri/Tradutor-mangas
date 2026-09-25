from __future__ import annotations

import pytest

from .config import Config
from .models import (
    BBox,
    Chapter,
    Detection,
    ExtractedBlock,
    ExtractedPage,
    Extraction,
    TranslatedBlock,
    TranslatedPage,
)
from .ocr import BlockReading
from .pipeline import _drop_repeated_readings, keep_manual_edits, translate_chapter
from .store import save_chapter


def reading(x: int, y: int, w: int, h: int, text: str, confidence: float = 90.0):
    box = BBox(x=x, y=y, w=w, h=h)
    return (Detection(bbox=box, text_bbox=box), BlockReading(text, confidence))


def texts(readings) -> list[str]:
    return [block.text for _, block in readings]


def boxes(readings) -> list[BBox]:
    return [detection.bbox for detection, _ in readings]


def test_keeps_a_single_reading_untouched():
    entrada = [reading(0, 0, 100, 50, "OLA")]

    kept, removed = _drop_repeated_readings(entrada)

    assert kept == entrada
    assert removed == []


def test_drops_the_same_text_read_from_an_overlapping_box():
    painel = reading(0, 1236, 998, 230, "LORD OF THE ZHUGE CLAN.")
    balao = reading(570, 1036, 360, 417, "LORD OF THE ZHUGE CLAN.")

    kept, removed = _drop_repeated_readings([painel, balao])

    assert texts(kept) == ["LORD OF THE ZHUGE CLAN."]
    assert len(removed) == 1


def test_keeps_the_tighter_box_of_the_pair():
    """A caixa menor e a do balao; a maior e o painel que o contem."""
    painel = reading(0, 1236, 998, 230, "FALA")
    balao = reading(570, 1236, 360, 200, "FALA")

    kept, _ = _drop_repeated_readings([painel, balao])

    assert boxes(kept) == [BBox(x=570, y=1236, w=360, h=200)]


def test_keeps_the_tighter_box_whichever_comes_first():
    painel = reading(0, 1236, 998, 230, "FALA")
    balao = reading(570, 1236, 360, 200, "FALA")

    a, _ = _drop_repeated_readings([painel, balao])
    b, _ = _drop_repeated_readings([balao, painel])

    assert boxes(a) == boxes(b)


def test_keeps_identical_text_in_boxes_that_do_not_touch():
    """Duas falas iguais em baloes distintos sao repeticao legitima da HQ."""
    entrada = [reading(0, 0, 100, 50, "..."), reading(500, 700, 100, 50, "...")]

    kept, removed = _drop_repeated_readings(entrada)

    assert len(kept) == 2
    assert removed == []


def test_keeps_different_texts_in_overlapping_boxes():
    entrada = [reading(0, 0, 400, 300, "PRIMEIRA"), reading(100, 100, 200, 150, "SEGUNDA")]

    kept, removed = _drop_repeated_readings(entrada)

    assert texts(kept) == ["PRIMEIRA", "SEGUNDA"]
    assert removed == []


def test_collapses_three_readings_of_one_bubble():
    entrada = [
        reading(0, 0, 900, 400, "FALA"),
        reading(100, 50, 400, 300, "FALA"),
        reading(150, 80, 200, 120, "FALA"),
    ]

    kept, removed = _drop_repeated_readings(entrada)

    assert len(kept) == 1
    assert boxes(kept) == [BBox(x=150, y=80, w=200, h=120)]
    assert len(removed) == 2


@pytest.mark.parametrize("entrada", [[], [reading(0, 0, 10, 10, "")]])
def test_survives_degenerate_input(entrada):
    kept, removed = _drop_repeated_readings(entrada)

    assert len(kept) == len(entrada)
    assert removed == []


# ---------- correcao a mao e retraducao parcial ----------


def _block(block_id: str, text: str, *, edited: bool = False) -> TranslatedBlock:
    return TranslatedBlock(id=block_id, source_text="EN", text=text, edited=edited)


def _page(index: int, image: str, *blocks: TranslatedBlock) -> TranslatedPage:
    return TranslatedPage(index=index, image=image, width=10, height=10, blocks=blocks)


def _chapter(*pages: TranslatedPage) -> Chapter:
    return Chapter(
        series="Obra", chapter="001", engine="free", pipeline_version=4,
        created_at="2026-01-01T00:00:00+00:00", pages=pages,
    )


@pytest.mark.parametrize(
    ("name", "previous", "fresh", "expected"),
    [
        ("sem traducao anterior fica a nova", None, [_block("b1", "novo")], ["novo"]),
        (
            "fala editada sobrevive a retraducao",
            [_block("b1", "corrigido", edited=True)],
            [_block("b1", "novo")],
            ["corrigido"],
        ),
        (
            "fala nao editada e trocada",
            [_block("b1", "velho")],
            [_block("b1", "novo")],
            ["novo"],
        ),
        (
            "fala editada que o OCR perdeu volta",
            [_block("b1", "corrigido", edited=True)],
            [_block("b2", "outro")],
            ["outro", "corrigido"],
        ),
    ],
)
def test_keeps_manual_edits_across_a_retranslation(name, previous, fresh, expected):
    before = None if previous is None else _chapter(_page(1, "p1.jpg", *previous))

    merged = keep_manual_edits(before, [_page(1, "p1.jpg", *fresh)])

    assert [block.text for block in merged[0].blocks] == expected, name


class _Echo:
    """Motor falso: traduz cada fala para `<pagina>:novo` e conta o que recebeu."""

    name = "free"
    model = None

    def __init__(self) -> None:
        self.asked: list[str] = []

    def translate_chapter(self, pages, glossary, chapter_dir, progress=None):  # noqa: ANN001
        self.asked.extend(page.image for page in pages)
        return [
            _page(page.index, page.image, *(_block(b.id, f"{page.image}:novo") for b in page.blocks))
            for page in pages
        ]


def _extraction(*images: str) -> Extraction:
    return Extraction(
        series="Obra",
        chapter="001",
        pipeline_version=4,
        pages=tuple(
            ExtractedPage(
                index=index, image=image, width=10, height=10, image_sha256="x",
                blocks=(ExtractedBlock(id="b1", bbox=BBox(x=0, y=0, w=5, h=5), raw_text="EN", confidence=90),),
            )
            for index, image in enumerate(images, start=1)
        ),
    )


def test_retranslates_only_the_asked_page_and_keeps_the_rest(tmp_path):
    cfg = Config(root=tmp_path)
    (cfg.library_dir / "Obra" / "001").mkdir(parents=True)
    save_chapter(
        cfg,
        _chapter(_page(1, "p1.jpg", _block("b1", "velho 1")), _page(2, "p2.jpg", _block("b1", "velho 2"))),
    )
    engine = _Echo()

    chapter = translate_chapter(cfg, _extraction("p1.jpg", "p2.jpg"), engine, only_pages=frozenset({"p2.jpg"}))

    assert engine.asked == ["p2.jpg"]
    assert [page.blocks[0].text for page in chapter.pages] == ["velho 1", "p2.jpg:novo"]


def test_translates_everything_when_there_is_nothing_to_splice_into(tmp_path):
    cfg = Config(root=tmp_path)
    (cfg.library_dir / "Obra" / "001").mkdir(parents=True)
    engine = _Echo()

    chapter = translate_chapter(cfg, _extraction("p1.jpg", "p2.jpg"), engine, only_pages=frozenset({"p2.jpg"}))

    assert engine.asked == ["p1.jpg", "p2.jpg"]
    assert len(chapter.pages) == 2
