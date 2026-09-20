"""O que um testador pode gastar, e o que o disco inteiro pode gastar.

Duas travas diferentes, e elas respondem perguntas diferentes:

**A cota do testador** existe para que a curiosidade de um visitante nao consuma o
servidor nem a chave da API do dono. Doze paginas mostram a qualidade da traducao
tao bem quanto 155 e custam 1/13 do CPU.

**O teto de disco** existe para que muitos testadores dentro da cota nao encham o
volume juntos. Cota individual e teto global sao coisas independentes: cem
uploads perfeitamente legais ainda enchem um disco de 20GB.

O numero que vale e o que esta no disco, e nao o que a tabela `usage` diz. A
tabela e registro - serve para o dono olhar o movimento - e registro vira mentira
no primeiro upload que falhou no meio. Contar arquivo custa alguns milissegundos
e nunca diverge.
"""

from __future__ import annotations

import os
from pathlib import Path

from .accounts import User
from .config import Config
from .db import now
from .store import INCOMING_SUFFIX, SOURCE_DIRNAME, list_page_images

TESTER_MAX_PAGES_PER_CHAPTER = 12
"""Doze paginas mostram a qualidade da traducao tao bem quanto 155 e custam 1/13
do CPU. O capitulo medido aqui levou 216s para 155 fatias; doze levam ~17s."""

TESTER_MAX_CHAPTERS = 2
"""Dois bastam para comparar uma pagina facil com uma dificil. Mais que isso e
acervo, e acervo e o que este servidor nao e."""

TESTER_MAX_UPLOAD_BYTES = 40 * 1024 * 1024
"""Doze paginas de manhwa em jpeg nao passam de alguns MB; 40MB cobre PNG sem
perda e ainda e um teto que um disco pequeno aguenta vezes muitos testadores."""

TESTER_ENGINES: tuple[str, ...] = ("free",)
"""Sem `claude`: a chave da API e do dono, e a conta chega para ele.

E a trava de custo mais importante deste projeto. Ela vale na interface, que nao
oferece a opcao, e na API, que recusa - interface sozinha e sugestao."""

DISK_CEILING_ENV = "MAX_TOTAL_DISK_BYTES"
DEFAULT_DISK_CEILING = 20 * 1024 * 1024 * 1024
"""Teto do que todas as areas somadas podem ocupar.

Atingido, upload novo leva 507 em vez de encher o volume - um disco cheio derruba
o banco junto, e ai nem o dono entra."""


class QuotaExceeded(RuntimeError):
    """O usuario passou do que pode. Vira 429 com a mensagem legivel."""


class OutOfSpace(RuntimeError):
    """O disco do servidor chegou ao teto. Vira 507, e nao e culpa de quem pediu."""


def engines_for(user: User) -> tuple[str, ...]:
    from .engines.base import available_engines

    return tuple(available_engines()) if user.is_owner else TESTER_ENGINES


def check_engine(user: User, engine: str) -> None:
    allowed = engines_for(user)
    if engine not in allowed:
        raise QuotaExceeded(
            f"motor {engine!r} nao esta disponivel nesta conta; use {', '.join(allowed)}"
        )


def directory_bytes(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def area_bytes(cfg: Config) -> int:
    """Quanto o acervo deste usuario ocupa, contando a area de espera."""
    return directory_bytes(cfg.library_dir) + directory_bytes(cfg.output_dir)


def chapter_names(cfg: Config) -> list[str]:
    """Capitulos deste usuario, promovidos ou ainda na area de espera.

    A area de espera conta: quem sobe dois capitulos e nao promove nenhum ja
    ocupou o disco de dois capitulos, e a cota existe por causa do disco.
    """
    if not cfg.library_dir.is_dir():
        return []
    found: list[str] = []
    for series_dir in cfg.library_dir.iterdir():
        if not series_dir.is_dir():
            continue
        for chapter_dir in series_dir.iterdir():
            if not chapter_dir.is_dir() or chapter_dir.name == SOURCE_DIRNAME:
                continue
            found.append(f"{series_dir.name}/{chapter_dir.name.removesuffix(INCOMING_SUFFIX)}")
    return sorted(set(found))


def check_new_chapter(cfg: Config, user: User) -> None:
    if user.is_owner:
        return
    existing = chapter_names(cfg)
    if len(existing) >= TESTER_MAX_CHAPTERS:
        raise QuotaExceeded(
            f"esta sessao ja tem {len(existing)} capitulo(s); o teste vai ate"
            f" {TESTER_MAX_CHAPTERS}. Apague um para subir outro."
        )


def check_incoming_pages(cfg: Config, user: User, incoming: Path, adding: int = 1) -> None:
    """Se cabem mais `adding` paginas na area de espera deste capitulo."""
    if user.is_owner:
        return
    present = len(list_page_images(incoming)) if incoming.is_dir() else 0
    if present + adding > TESTER_MAX_PAGES_PER_CHAPTER:
        raise QuotaExceeded(
            f"o teste aceita ate {TESTER_MAX_PAGES_PER_CHAPTER} paginas por capitulo,"
            f" e este ja tem {present}. Doze paginas ja mostram a qualidade da traducao."
        )


def upload_headroom(cfg: Config, user: User) -> int | None:
    """Quantos bytes esta area ainda aceita, ou None quando nao ha teto.

    Existe para o upload que chega em pedacos: sem ela, a unica forma de saber que
    o zip nao cabe seria receber o zip inteiro primeiro, e ai os 500MB ja estao no
    disco que a cota existia para proteger.
    """
    if user.is_owner:
        return None
    return max(0, TESTER_MAX_UPLOAD_BYTES - area_bytes(cfg))


def check_upload_bytes(cfg: Config, user: User, incoming_bytes: int) -> None:
    """Se o upload cabe no que esta sessao ainda pode gravar."""
    if user.is_owner:
        return
    used = area_bytes(cfg)
    if used + incoming_bytes > TESTER_MAX_UPLOAD_BYTES:
        remaining = max(0, TESTER_MAX_UPLOAD_BYTES - used)
        raise QuotaExceeded(
            f"esta sessao pode gravar {TESTER_MAX_UPLOAD_BYTES // (1024 * 1024)}MB no total"
            f" e ja usou {used // (1024 * 1024)}MB; cabem mais {remaining // 1024}KB."
        )


def disk_ceiling() -> int:
    raw = os.environ.get(DISK_CEILING_ENV, "").strip()
    if not raw.isdigit():
        return DEFAULT_DISK_CEILING
    return int(raw)


def check_disk(base: Config, incoming_bytes: int = 0) -> None:
    """Se o servidor inteiro ainda tem espaco para este upload."""
    from .accounts import users_root

    used = directory_bytes(users_root(base))
    if used + incoming_bytes > disk_ceiling():
        raise OutOfSpace("o servidor esta sem espaco para uploads novos; tente mais tarde")


def record_usage(connection, user_id: str, *, pages: int = 0, chapters: int = 0, bytes_: int = 0) -> None:
    """Soma o movimento do dia na tabela `usage`.

    Registro, e nao a fonte da verdade: quem decide se cabe e o disco, logo acima.
    Esta tabela existe para o dono conseguir responder "quanto isto esta sendo
    usado?" sem varrer o volume inteiro.
    """
    connection.execute(
        """
        INSERT INTO usage (user_id, day, pages, chapters, bytes) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (user_id, day) DO UPDATE SET
            pages    = pages + excluded.pages,
            chapters = chapters + excluded.chapters,
            bytes    = bytes + excluded.bytes
        """,
        (user_id, now()[:10], pages, chapters, bytes_),
    )
