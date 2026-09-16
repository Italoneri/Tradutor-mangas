"""A vitrine publica: os capitulos que qualquer visitante le sem criar nada.

E uma terceira categoria de conteudo, ao lado de `reader/` (codigo) e da area do
usuario (privada). Tres propriedades, e as tres sao o que a tornam segura:

**Sem sessao e sem cookie.** Servida por caminho, como `reader/` ja e. A home
publica nao pode criar usuario a cada `GET`, senao todo robo de busca ganha uma
area em disco.

**Imutavel.** Gerada nesta maquina, pelo pipeline normal, e copiada para dentro da
imagem. O servidor hospedado nunca escreve aqui. Por ser imutavel e igual para
todos, e a unica parte do conteudo que o service worker pode guardar em cache.

**Escolhida a mao.** Acervo de usuario nunca vira vitrine automaticamente, nem com
botao de "tornar publico". Quem publica e o dono, copiando arquivo com este
comando - e responde pelo que publicou.

O truque que faz a vitrine usar o leitor de sempre: imagens e `chapter.*.json` do
capitulo moram no MESMO diretorio, entao um `Config` com `library` e `output`
apontando os dois para `public/demo` descreve a vitrine inteira. `build_library`
roda sobre ele sem saber que isto e uma vitrine, e o leitor monta as URLs com o
mesmo codigo que usa para o acervo.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .models import Library
from .store import (
    LIBRARY_FILENAME,
    SERIES_FILENAME,
    build_library,
    chapter_output_dir,
    find_cover,
    list_page_images,
)

PUBLIC_DIRNAME = "public"
DEMO_DIRNAME = "demo"
DEMO_LIBRARY_FILENAME = "demo-library.json"

DEMO_BASE = f"{PUBLIC_DIRNAME}/{DEMO_DIRNAME}"
"""Prefixo das URLs da vitrine, relativo a raiz servida.

O mesmo valor entra em `library_base` e em `output_base` do indice: na vitrine os
dois sao o mesmo diretorio, e e isso que dispensa o leitor de saber onde ele
esta."""


class DemoError(RuntimeError):
    """Nao ha o que copiar, e copiar meio capitulo seria pior."""


def demo_root(cfg: Config) -> Path:
    return cfg.root / PUBLIC_DIRNAME / DEMO_DIRNAME


def demo_config(cfg: Config) -> Config:
    """O mesmo `Config` descrevendo a vitrine em vez do acervo.

    `content_root` volta a None porque a vitrine mora na raiz do projeto, e nao
    na area de usuario nenhum - ela e codigo publicado, do ponto de vista do
    deploy, e viaja dentro da imagem.
    """
    paths = cfg.paths.model_copy(update={"library": DEMO_BASE, "output": DEMO_BASE})
    return cfg.model_copy(update={"content_root": None, "paths": paths})


@dataclass(frozen=True)
class _Publishable:
    """Um capitulo conferido, com os arquivos que vao sair daqui."""

    series: str
    chapter: str
    pages: tuple[Path, ...]
    translations: tuple[Path, ...]
    chapter_cover: Path | None


def _resolve(cfg: Config, series: str, chapter: str) -> _Publishable:
    """O capitulo pronto para copiar, ou `DemoError` com o que falta."""
    source = cfg.library_dir / series / chapter
    pages = list_page_images(source) if source.is_dir() else []
    if not pages:
        raise DemoError(f"{series}/{chapter} nao tem pagina nenhuma em {source}")

    translations = sorted(chapter_output_dir(cfg, series, chapter).glob("chapter.*.json"))
    if not translations:
        raise DemoError(
            f"{series}/{chapter} nao esta traduzido; rode `mangatl process` antes de publicar"
        )

    return _Publishable(
        series=series,
        chapter=chapter,
        pages=tuple(pages),
        translations=tuple(translations),
        chapter_cover=find_cover(source),
    )


def _copy_chapter(target: _Publishable, destination: Path) -> int:
    """Grava as paginas e as traducoes. Devolve quantos arquivos sairam."""
    # O destino e recriado do zero: capitulo republicado depois de reprocessar com
    # menos paginas deixaria as paginas velhas la, e o leitor as mostraria.
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)

    for path in (*target.pages, *target.translations):
        shutil.copy2(path, destination / path.name)
    if target.chapter_cover is not None:
        shutil.copy2(target.chapter_cover, destination / target.chapter_cover.name)
    return len(target.pages) + len(target.translations)


def _copy_series_metadata(cfg: Config, series: str, destination: Path) -> None:
    source = cfg.library_dir / series
    destination.mkdir(parents=True, exist_ok=True)

    cover = find_cover(source)
    if cover is not None:
        shutil.copy2(cover, destination / cover.name)

    meta = source / SERIES_FILENAME
    if meta.is_file():
        shutil.copy2(meta, destination / SERIES_FILENAME)


def save_demo_library(cfg: Config, library: Library) -> Path:
    path = demo_root(cfg) / DEMO_LIBRARY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(library.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def build_demo(cfg: Config, targets: Sequence[tuple[str, str]]) -> tuple[Library, int]:
    """Copia os capitulos para `public/demo/` e regenera o indice.

    Aditivo: os capitulos nomeados sao (re)copiados e o indice e remontado a
    partir de tudo que estiver na vitrine. Assim `build-demo A 001` seguido de
    `build-demo A 002` publica os dois, sem precisar repetir a lista inteira.

    Devolve o indice e quantos arquivos foram copiados nesta chamada.
    """
    if not targets:
        raise DemoError("nenhum capitulo indicado")

    # Confere todos antes de copiar qualquer um. Uma vitrine meio escrita parece
    # publicada e nao esta, e o leitor mostraria o capitulo que chegou a copiar
    # como se fosse a escolha do dono.
    publishable = [_resolve(cfg, series, chapter) for series, chapter in targets]

    root = demo_root(cfg)
    copied = 0
    for target in publishable:
        _copy_series_metadata(cfg, target.series, root / target.series)
        copied += _copy_chapter(target, root / target.series / target.chapter)

    # O indice sai de uma varredura da vitrine, e nao da lista de alvos: e o mesmo
    # `build_library` do acervo, sobre um `Config` que descreve a vitrine.
    library = build_library(demo_config(cfg))
    save_demo_library(cfg, library)

    # `build_library` escreveria `library.json` na mesma pasta se alguem chamasse
    # `save_library`; deixar um arquivo com esse nome aqui faria o leitor abrir o
    # errado dependendo de quem escreveu por ultimo.
    stale = root / LIBRARY_FILENAME
    if stale.is_file():
        stale.unlink()

    return library, copied


def published_chapters(cfg: Config) -> Iterable[tuple[str, str]]:
    """Os pares (serie, capitulo) que ja estao na vitrine."""
    library = build_library(demo_config(cfg))
    return [(entry.series, chapter.chapter) for entry in library.series for chapter in entry.chapters]
