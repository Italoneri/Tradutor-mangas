from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from .export import render_page, write_cbz, write_pdf
from .models import BBox, Chapter, TranslatedBlock, TranslatedPage

WIDTH, HEIGHT = 400, 600
BUBBLE = BBox(x=100, y=100, w=200, h=120)


def _page(index: int, image: str, *, kind: str = "bubble", text: str = "ola mundo") -> TranslatedPage:
    block = TranslatedBlock(id="b1", bbox=BUBBLE, source_text="HELLO WORLD", text=text, kind=kind)
    return TranslatedPage(index=index, image=image, width=WIDTH, height=HEIGHT, blocks=(block,))


def _chapter(*pages: TranslatedPage) -> Chapter:
    return Chapter(
        series="Obra", chapter="001", engine="free", pipeline_version=4,
        created_at="2026-01-01T00:00:00+00:00", pages=pages,
    )


@pytest.fixture
def pages_dir(tmp_path: Path) -> Path:
    """Duas paginas cinza-escuro: qualquer branco nelas foi o export que pintou."""
    for name in ("p1.jpg", "p2.jpg"):
        Image.new("RGB", (WIDTH, HEIGHT), (40, 40, 40)).save(tmp_path / name)
    return tmp_path


def _white_pixels(image: Image.Image, box: BBox) -> int:
    crop = image.crop((box.x, box.y, box.x + box.w, box.y + box.h)).convert("L")
    return sum(crop.histogram()[241:])


def test_paints_the_translation_inside_the_bubble(pages_dir: Path):
    image = render_page(pages_dir / "p1.jpg", _page(1, "p1.jpg"))

    assert _white_pixels(image, BUBBLE) > 0
    outside = BBox(x=0, y=HEIGHT - 150, w=WIDTH, h=150)
    assert _white_pixels(image, outside) == 0, "o branco vazou para fora do balao"


def test_leaves_the_art_alone_for_a_line_outside_a_bubble(pages_dir: Path):
    """Caixa branca so faz sentido dentro de balao; fora dele ela taparia arte."""
    bubble = render_page(pages_dir / "p1.jpg", _page(1, "p1.jpg"))
    caption = render_page(pages_dir / "p1.jpg", _page(1, "p1.jpg", kind="free"))

    assert _white_pixels(caption, BUBBLE) < _white_pixels(bubble, BUBBLE)


def test_keeps_a_long_line_inside_the_bubble_height(pages_dir: Path):
    long = "uma fala comprida demais para caber no balao sem encolher a fonte " * 3
    image = render_page(pages_dir / "p1.jpg", _page(1, "p1.jpg", text=long))

    below = BBox(x=0, y=BUBBLE.y + BUBBLE.h + 40, w=WIDTH, h=HEIGHT - BUBBLE.y - BUBBLE.h - 40)
    assert _white_pixels(image, below) == 0


def test_writes_one_image_per_page_into_the_cbz(pages_dir: Path, tmp_path: Path):
    target = write_cbz(_chapter(_page(1, "p1.jpg"), _page(2, "p2.jpg")), pages_dir, tmp_path / "c.cbz")

    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ["0001.jpg", "0002.jpg"]
        with archive.open("0001.jpg") as first:
            assert Image.open(first).size == (WIDTH, HEIGHT)


def test_writes_one_pdf_page_per_page(pages_dir: Path, tmp_path: Path):
    target = write_pdf(_chapter(_page(1, "p1.jpg"), _page(2, "p2.jpg")), pages_dir, tmp_path / "c.pdf")

    data = target.read_bytes()
    assert data.startswith(b"%PDF")
    # `/Type /Page` sem o `s` de `/Pages`, que e o no raiz da arvore de paginas.
    assert len(re.findall(rb"/Type\s*/Page(?!s)", data)) == 2
