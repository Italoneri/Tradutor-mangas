"""O que cada conta pode gastar, e o que o disco inteiro pode gastar.

Duas travas diferentes, e elas respondem perguntas diferentes:

**A cota da conta** existe para que ninguem consuma sozinho o servidor nem a
chave da API do dono. O testador tem a menor - doze paginas mostram a qualidade
da traducao tao bem quanto 155 e custam 1/13 do CPU. O membro tem a de `[members]`,
ajustavel conta a conta. O dono nao tem nenhuma.

**O teto de disco** existe para que muitas contas dentro da cota nao encham o
volume juntas. Cota individual e teto global sao coisas independentes: cem
uploads perfeitamente legais ainda enchem um disco de 20GB.

Para espaco, o numero que vale e o que esta no disco, e nao o que a tabela
`usage` diz: registro vira mentira no primeiro upload que falhou no meio, e
contar arquivo custa alguns milissegundos. Para ritmo - paginas por dia - a
tabela e a fonte certa, porque a pergunta e quanto entrou hoje, e nao quanto
sobrou.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .accounts import User
from .config import Config
from .db import now
from .store import INCOMING_SUFFIX, SOURCE_DIRNAME, list_page_images

MAX_PAGES_PER_CHAPTER = 400
"""O capitulo medido aqui tem 155 fatias. Acima de 400 e pasta errada, nao capitulo."""

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

TESTER_DISK_SHARE = 0.8
MEMBER_DISK_SHARE = 0.95
"""Fatia do teto global que cada papel alcanca. O resto e do dono.

Sem ela, contas que enchiam o volume faziam o upload do dono levar 507 - um
limite pensado para protege-lo sendo usado contra ele. O membro vai mais longe
que o testador porque o acervo dele fica, mas a folga final continua do dono."""


class QuotaExceeded(RuntimeError):
    """O usuario passou do que pode. Vira 429 com a mensagem legivel."""


class OutOfSpace(RuntimeError):
    """O disco do servidor chegou ao teto. Vira 507, e nao e culpa de quem pediu."""


@dataclass(frozen=True)
class Limits:
    """O que uma conta pode gastar. None e sem teto."""

    disk_bytes: int | None
    pages_per_chapter: int
    chapters: int | None
    series: int | None
    pages_per_day: int | None
    active_jobs: int | None
    """Jobs na fila ou rodando ao mesmo tempo."""
    disk_share: float


OWNER_LIMITS = Limits(
    disk_bytes=None,
    pages_per_chapter=MAX_PAGES_PER_CHAPTER,
    chapters=None,
    series=None,
    pages_per_day=None,
    active_jobs=None,
    disk_share=1.0,
)

TESTER_MAX_SERIES = TESTER_MAX_CHAPTERS
"""Uma serie por capitulo possivel. Serie vazia nao ocupa quase nada em bytes, mas
sem teto um laco de `POST /api/series` criava pastas sem fim dentro da cota de
40MB - e o teto de disco conta bytes, nao pastas."""

TESTER_LIMITS = Limits(
    disk_bytes=TESTER_MAX_UPLOAD_BYTES,
    pages_per_chapter=TESTER_MAX_PAGES_PER_CHAPTER,
    chapters=TESTER_MAX_CHAPTERS,
    series=TESTER_MAX_SERIES,
    pages_per_day=None,
    active_jobs=1,
    disk_share=TESTER_DISK_SHARE,
)
"""Sem teto diario: os 40MB e os dois capitulos ja seguram o ritmo de quem so esta
testando. Um job por vez, como sempre foi."""


def limits_for(cfg: Config, user: User) -> Limits:
    """Os numeros que valem para esta conta: o ajuste dela, ou o padrao do papel."""
    if user.is_unmetered:
        return OWNER_LIMITS
    if not user.has_account:
        return TESTER_LIMITS
    defaults = cfg.members
    return Limits(
        disk_bytes=_first_set(user.disk_limit_bytes, defaults.disk_bytes),
        pages_per_chapter=MAX_PAGES_PER_CHAPTER,
        chapters=None,
        series=None,
        pages_per_day=_first_set(user.pages_per_day, defaults.pages_per_day),
        active_jobs=_first_set(user.max_pending_jobs, defaults.active_jobs),
        disk_share=MEMBER_DISK_SHARE,
    )


def _first_set(adjusted: int | None, default: int) -> int:
    return default if adjusted is None else adjusted


def _whose(user: User) -> str:
    return "esta conta" if user.has_account else "esta sessao"


def engines_for(user: User) -> tuple[str, ...]:
    from .engines.base import available_engines

    return tuple(available_engines()) if user.is_admin else TESTER_ENGINES


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
    ceiling = limits_for(cfg, user).chapters
    if ceiling is None:
        return
    existing = chapter_names(cfg)
    if len(existing) >= ceiling:
        raise QuotaExceeded(
            f"esta sessao ja tem {len(existing)} capitulo(s); o teste vai ate"
            f" {ceiling}. Apague um para subir outro."
        )


def check_new_series(cfg: Config, user: User) -> None:
    ceiling = limits_for(cfg, user).series
    if ceiling is None:
        return
    existing = [entry for entry in cfg.library_dir.iterdir() if entry.is_dir()] if cfg.library_dir.is_dir() else []
    if len(existing) >= ceiling:
        raise QuotaExceeded(
            f"esta sessao ja tem {len(existing)} obra(s); o teste vai ate {ceiling}."
            " Use uma das que ja existem."
        )


def check_incoming_pages(cfg: Config, user: User, incoming: Path, adding: int = 1) -> None:
    """Se cabem mais `adding` paginas na area de espera deste capitulo.

    So o teto do testador tem mensagem propria: o das contas e o de 400, que o
    painel ja confere antes com a mensagem de "pasta errada".
    """
    ceiling = limits_for(cfg, user).pages_per_chapter
    if ceiling >= MAX_PAGES_PER_CHAPTER:
        return
    present = len(list_page_images(incoming)) if incoming.is_dir() else 0
    if present + adding > ceiling:
        raise QuotaExceeded(
            f"o teste aceita ate {ceiling} paginas por capitulo,"
            f" e este ja tem {present}. Doze paginas ja mostram a qualidade da traducao."
        )


def upload_headroom(cfg: Config, user: User) -> int | None:
    """Quantos bytes esta area ainda aceita, ou None quando nao ha teto.

    Existe para o upload que chega em pedacos: sem ela, a unica forma de saber que
    o zip nao cabe seria receber o zip inteiro primeiro, e ai os 500MB ja estao no
    disco que a cota existia para proteger.
    """
    ceiling = limits_for(cfg, user).disk_bytes
    if ceiling is None:
        return None
    return max(0, ceiling - area_bytes(cfg))


def check_upload_bytes(cfg: Config, user: User, incoming_bytes: int) -> None:
    """Se o upload cabe no que esta conta ainda pode gravar."""
    ceiling = limits_for(cfg, user).disk_bytes
    if ceiling is None:
        return
    used = area_bytes(cfg)
    if used + incoming_bytes > ceiling:
        remaining = max(0, ceiling - used)
        raise QuotaExceeded(
            f"{_whose(user)} pode gravar {ceiling // (1024 * 1024)}MB no total"
            f" e ja usou {used // (1024 * 1024)}MB; cabem mais {remaining // 1024}KB."
        )


def pages_today(connection: sqlite3.Connection, user_id: str) -> int:
    """Paginas que esta conta subiu hoje, no dia UTC de `now()`."""
    row = connection.execute(
        "SELECT pages FROM usage WHERE user_id = ? AND day = ?", (user_id, now()[:10])
    ).fetchone()
    return row["pages"] if row else 0


def daily_pages_left(connection: sqlite3.Connection, cfg: Config, user: User) -> int | None:
    """Quantas paginas esta conta ainda sobe hoje, ou None quando nao ha teto."""
    ceiling = limits_for(cfg, user).pages_per_day
    if ceiling is None:
        return None
    return max(0, ceiling - pages_today(connection, user.id))


def check_daily_pages(connection: sqlite3.Connection, cfg: Config, user: User, adding: int) -> None:
    left = daily_pages_left(connection, cfg, user)
    if left is not None and adding > left:
        ceiling = limits_for(cfg, user).pages_per_day
        raise QuotaExceeded(
            f"{_whose(user)} sobe ate {ceiling} paginas por dia e hoje ja subiu"
            f" {ceiling - left}; o contador volta a zero a meia-noite UTC."
        )


def disk_ceiling() -> int:
    raw = os.environ.get(DISK_CEILING_ENV, "").strip()
    if not raw.isdigit():
        return DEFAULT_DISK_CEILING
    return int(raw)


def check_disk(base: Config, user: User, incoming_bytes: int = 0) -> None:
    """Se o servidor inteiro ainda tem espaco para este upload, deste usuario."""
    from .accounts import users_root

    ceiling = int(disk_ceiling() * limits_for(base, user).disk_share)
    used = directory_bytes(users_root(base))
    if used + incoming_bytes > ceiling:
        raise OutOfSpace("o servidor esta sem espaco para uploads novos; tente mais tarde")


def record_usage(connection, user_id: str, *, pages: int = 0, chapters: int = 0, bytes_: int = 0) -> None:
    """Soma o movimento do dia na tabela `usage`.

    Para espaco e registro, e quem decide se cabe e o disco, logo acima. Para o
    teto diario de paginas ela e a fonte: `check_daily_pages` le daqui.
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
