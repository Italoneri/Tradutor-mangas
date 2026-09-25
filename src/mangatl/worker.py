"""O processo que tira job da fila e roda o pipeline.

Processo separado do que atende HTTP, e essa separacao compra tres coisas
concretas:

- **o servidor continua respondendo.** O pipeline segura um nucleo por minutos, e
  no mesmo processo isso e a tela de progresso congelando junto;
- **o pipeline pesado nao mora no processo exposto.** Torch, OpenCV e Tesseract
  ficam onde ninguem manda requisicao;
- **reiniciar o servidor nao mata o capitulo no meio.** Sao dois ciclos de vida.

O worker nao conhece usuario: ele pega o proximo job, pede a area de quem o criou
e entrega um `Config` ao pipeline, que continua sem saber que contas existem.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

from .accounts import area_config, get_user
from .cleanup import EVERY_SECONDS as CLEANUP_EVERY_SECONDS
from .cleanup import sweep
from .config import Config
from .db import connect
from .engines.base import create_engine
from .jobs import LOG_LINES, Job, claim_next, finish, requeue_running, set_progress
from .models import Progress
from .pipeline import extract_chapter, translate_chapter
from .store import build_library, save_library

log = logging.getLogger("mangatl.worker")

IDLE_SECONDS = 2.0
"""Espera entre duas varreduras da fila vazia.

Dois segundos e o atraso maximo entre subir um capitulo e ver a barra andar; e
curto o bastante para ninguem achar que travou, e longo o bastante para uma fila
vazia nao acordar o disco dez vezes por segundo."""

PROGRESS_EVERY_SECONDS = 1.0
"""Ritmo maximo de escrita do progresso no banco.

Sem isto, um capitulo de 155 fatias escreveria centenas de vezes numa tabela que
outra pessoa esta lendo - e o ganho seria uma barra que anda mais suave do que
qualquer olho percebe."""


class _Collector(logging.Handler):
    """Manda as linhas dos loggers `mangatl.*` para o job que esta rodando."""

    def __init__(self, lines: deque[str]) -> None:
        super().__init__(level=logging.INFO)
        self._lines = lines

    def emit(self, record: logging.LogRecord) -> None:
        self._lines.append(f"{record.levelname} {record.name} {record.getMessage()}")


@contextmanager
def _collecting_into(lines: deque[str]) -> Iterator[None]:
    """Anexa o coletor enquanto o job roda, e o solta doa o que doer.

    Removido no `finally` porque handler esquecido no logger faz o job seguinte
    escrever tambem no anterior - e a tela mostraria linha de um job no outro.
    """
    handler = _Collector(lines)
    logger = logging.getLogger("mangatl")
    # O nivel tambem sobe: sem `--verbose` ninguem configurou o logger, ele herda
    # o WARNING do root e a linha de INFO morre antes de chegar em handler nenhum.
    before = logger.level
    logger.setLevel(min(logging.INFO, before or logging.INFO))
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(before)


def _run_job(base: Config, job: Job) -> None:
    """Roda um job ate o fim, gravando progresso no caminho.

    Abre a propria conexao: o worker e outro processo, e conexao do sqlite3 nao
    atravessa nem processo nem thread.
    """
    lines: deque[str] = deque(maxlen=LOG_LINES)
    last_write = 0.0

    with connect(base) as connection:
        user = get_user(connection, job.user_id)
        if user is None:
            # O testador venceu e a limpeza levou a conta enquanto o job esperava.
            finish(connection, job.id, state="cancelled", error="a conta do job nao existe mais")
            return
        cfg = area_config(base, user)

    def advance(update: Progress) -> None:
        nonlocal last_write
        moment = time.monotonic()
        if moment - last_write < PROGRESS_EVERY_SECONDS:
            return
        last_write = moment
        with connect(base) as progress_connection:
            set_progress(progress_connection, job.id, update, tuple(lines))

    outcome = "done"
    error: str | None = None
    try:
        with _collecting_into(lines):
            # Retraduzir uma pagina e reler e retraduzir so ela; o resto do capitulo
            # sai do cache de extracao e da traducao que ja existe.
            pages = frozenset(job.options.get("pages") or ())
            report = extract_chapter(
                cfg,
                job.series,
                job.chapter,
                force=bool(job.options.get("force")),
                progress=advance,
                force_pages=pages,
            )
            if not job.options.get("dry_run"):
                translate_chapter(
                    cfg,
                    report.extraction,
                    create_engine(job.engine, cfg),
                    advance,
                    only_pages=pages,
                )

            # Sem isto o capitulo novo nao aparece para quem le: o indice sai do
            # `library.json` salvo, e nao do disco.
            save_library(cfg, build_library(cfg))
    except Exception as failure:  # noqa: BLE001 - a falha e o resultado do job
        outcome = "failed"
        error = f"{type(failure).__name__}: {failure}"
        log.exception("operation=job id=%s falhou", job.id)

    with connect(base) as connection:
        set_progress(
            connection,
            job.id,
            Progress(phase="library", done=1, total=1, detail="pronto" if outcome == "done" else "falhou"),
            tuple(lines),
        )
        finish(connection, job.id, state=outcome, error=error)


def run_once(base: Config) -> bool:
    """Pega e roda um job, se houver. Devolve se rodou alguma coisa."""
    with connect(base) as connection:
        job = claim_next(connection)
    if job is None:
        return False

    log.info("operation=job_start id=%s series=%s engine=%s", job.id, job.series, job.engine)
    _run_job(base, job)
    return True


def run_forever(base: Config, *, stop: threading.Event | None = None) -> None:
    """Roda a fila ate pedirem para parar.

    Na subida, todo job em `running` volta para `pending`: ninguem estava rodando
    durante o reinicio, e um job preso ali e uma tela que nunca sai do lugar.
    """
    with connect(base) as connection:
        requeued = requeue_running(connection)
    if requeued:
        log.info("operation=requeue jobs=%d", requeued)

    halt = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, lambda *_: halt.set())

    log.info("operation=worker_ready")
    swept_at = 0.0
    while not halt.is_set():
        # A limpeza mora aqui e nao num cron: cron e mais uma coisa para
        # configurar, esquecer de configurar e descobrir cheia.
        if time.monotonic() - swept_at > CLEANUP_EVERY_SECONDS:
            swept_at = time.monotonic()
            sweep(base)

        if not run_once(base):
            halt.wait(IDLE_SECONDS)
