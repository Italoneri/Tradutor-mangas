from __future__ import annotations

import json

import pytest

from .config import Config
from .demo import DEMO_BASE, DemoError, build_demo, demo_root, published_chapters
from .models import Chapter, TranslatedBlock, TranslatedPage
from .store import save_chapter

JPEG = b"\xff\xd8\xff" + b"0" * 64


@pytest.fixture
def cfg(tmp_path):
    return Config(root=tmp_path)


def _processed_chapter(cfg, series="serie", chapter="001", engines=("free",), pages=2):
    source = cfg.library_dir / series / chapter
    source.mkdir(parents=True)
    for index in range(1, pages + 1):
        (source / f"p{index:04d}.jpg").write_bytes(JPEG)
    (source / "cover.jpg").write_bytes(JPEG)
    (cfg.library_dir / series / "cover.jpg").write_bytes(JPEG)

    for engine in engines:
        save_chapter(
            cfg,
            Chapter(
                series=series,
                chapter=chapter,
                engine=engine,
                pipeline_version=4,
                created_at="2026-01-01T00:00:00+00:00",
                pages=tuple(
                    TranslatedPage(
                        index=index,
                        image=f"p{index:04d}.jpg",
                        width=800,
                        height=1200,
                        blocks=(TranslatedBlock(id=f"b{index}", source_text="hi", text="oi"),),
                    )
                    for index in range(1, pages + 1)
                ),
            ),
        )


def test_copies_pages_and_every_engine_into_the_showcase(cfg):
    _processed_chapter(cfg, engines=("free", "claude"))

    library, copied = build_demo(cfg, [("serie", "001")])

    published = demo_root(cfg) / "serie" / "001"
    assert (published / "p0001.jpg").is_file()
    assert (published / "chapter.free.json").is_file()
    assert (published / "chapter.claude.json").is_file()
    assert copied == 4
    assert library.series[0].chapters[0].engines == ("claude", "free")


def test_writes_an_index_the_reader_can_use_unchanged(cfg):
    """Imagens e traducoes no mesmo diretorio: uma base so descreve a vitrine."""
    _processed_chapter(cfg)

    build_demo(cfg, [("serie", "001")])
    index = json.loads((demo_root(cfg) / "demo-library.json").read_text(encoding="utf-8"))

    assert index["library_base"] == DEMO_BASE
    assert index["output_base"] == DEMO_BASE
    assert index["series"][0]["cover"] == f"{DEMO_BASE}/serie/cover.jpg"
    assert index["series"][0]["chapters"][0]["page_count"] == 2


def test_keeps_earlier_chapters_when_publishing_another(cfg):
    _processed_chapter(cfg, chapter="001")
    _processed_chapter(cfg, chapter="002")

    build_demo(cfg, [("serie", "001")])
    build_demo(cfg, [("serie", "002")])

    assert sorted(published_chapters(cfg)) == [("serie", "001"), ("serie", "002")]


def test_drops_pages_that_no_longer_exist_when_republishing(cfg):
    """Republicar depois de reprocessar com menos paginas nao pode deixar orfa."""
    _processed_chapter(cfg, pages=3)
    build_demo(cfg, [("serie", "001")])

    (cfg.library_dir / "serie" / "001" / "p0003.jpg").unlink()
    build_demo(cfg, [("serie", "001")])

    assert not (demo_root(cfg) / "serie" / "001" / "p0003.jpg").exists()


def test_refuses_a_chapter_that_was_never_translated(cfg):
    source = cfg.library_dir / "serie" / "001"
    source.mkdir(parents=True)
    (source / "p0001.jpg").write_bytes(JPEG)

    with pytest.raises(DemoError, match="nao esta traduzido"):
        build_demo(cfg, [("serie", "001")])

    assert not demo_root(cfg).exists()


def test_refuses_a_chapter_that_does_not_exist(cfg):
    with pytest.raises(DemoError, match="pagina nenhuma"):
        build_demo(cfg, [("serie", "404")])


def test_never_reads_from_a_user_area(cfg):
    """A vitrine sai do acervo do dono, e o `Config` derivado e o do dono.

    Se `build_demo` fosse chamado com o `Config` de um testador, ele copiaria a
    area dele - por isso o comando so existe no CLI, onde quem digita e o dono.
    """
    _processed_chapter(cfg)
    build_demo(cfg, [("serie", "001")])

    assert demo_root(cfg).is_relative_to(cfg.root / "public")
    assert not demo_root(cfg).is_relative_to(cfg.data_dir)
