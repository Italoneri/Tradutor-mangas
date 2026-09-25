"""O que apaga sozinho o que venceu.

Upload de testador expira e some. Isso nao e faxina: e uma das tres decisoes de
projeto que mantem este servidor sendo uma ferramenta de traducao em vez de um
acervo. Sem ela, um servidor que aceita upload anonimo vira, em algumas semanas,
um deposito de material de terceiros que ninguem decidiu hospedar.

Roda junto do worker, e nao num cron separado: cron e mais uma coisa para
configurar, esquecer de configurar e descobrir cheia. O worker ja e um laco que
acorda de tempos em tempos.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from .accounts import delete_user, expired_testers, users_root
from .config import Config
from .db import connect
from .store import INCOMING_SUFFIX
from .sessions import (
    purge_expired_sessions,
    purge_old_login_attempts,
    purge_old_tester_signups,
)

log = logging.getLogger("mangatl.cleanup")

EVERY_SECONDS = 15 * 60
"""Ritmo da varredura.

O prazo do testador e de 48 horas, entao quinze minutos de atraso na remocao nao
mudam nada para ninguem - e uma varredura a cada quinze minutos nao aparece em
grafico de CPU."""


UPLOAD_MAX_AGE_SECONDS = 60 * 60
"""Idade a partir da qual um `.upload` em `data/uploads/` e lixo.

O `finally` do handler apaga o temporario de todo pedido, mas so se o processo
continuar vivo: morto no meio de um upload, o arquivo fica. Uma hora cobre com
folga o upload mais lento que o `read_timeout` do proxy deixa acontecer."""

INCOMING_MAX_AGE_DAYS = 7
"""Idade a partir da qual uma area de espera esquecida e apagada, do dono inclusive.

Area de espera e upload que nao virou capitulo. Uma semana sem ninguem mexer e
desistencia, e o disco que ela ocupa conta contra o teto de todo mundo."""

SPOOL_SUFFIXES = (".upload", ".export")
"""O que o painel escreve em `data/uploads/` e apaga no fim do pedido: o corpo de
um upload grande e o arquivo de uma exportacao."""


def _older_than(path: Path, seconds: float, moment: float) -> bool:
    try:
        return moment - path.stat().st_mtime > seconds
    except FileNotFoundError:
        return False


def sweep_spool(cfg: Config, moment: float | None = None) -> int:
    """Apaga os temporarios de upload que sobraram de um processo morto."""
    spool = cfg.data_dir / "uploads"
    if not spool.is_dir():
        return 0
    moment = time.time() if moment is None else moment
    removed = 0
    for path in spool.iterdir():
        if path.suffix in SPOOL_SUFFIXES and _older_than(path, UPLOAD_MAX_AGE_SECONDS, moment):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def sweep_incoming(cfg: Config, moment: float | None = None) -> int:
    """Apaga as areas de espera abandonadas de todas as contas.

    Pela data da pasta, que muda a cada pagina que entra ou sai dela: uma area que
    ainda recebe upload nunca tem uma semana de idade.
    """
    root = users_root(cfg)
    if not root.is_dir():
        return 0
    moment = time.time() if moment is None else moment
    limit = INCOMING_MAX_AGE_DAYS * 24 * 60 * 60
    removed = 0
    for incoming in root.glob(f"*/{cfg.paths.library}/*/*{INCOMING_SUFFIX}"):
        if incoming.is_dir() and _older_than(incoming, limit, moment):
            shutil.rmtree(incoming, ignore_errors=True)
            removed += 1
    return removed


def sweep(cfg: Config) -> tuple[int, int]:
    """Apaga testador vencido, a area dele e as sessoes mortas.

    Devolve `(usuarios, bytes)`. O disco sai junto com a linha do banco, na mesma
    passagem: apagar so a linha deixaria um diretorio sem dono e sem ninguem para
    tentar de novo.
    """
    removed = 0
    freed = 0
    with connect(cfg) as connection:
        for user in expired_testers(connection):
            freed += delete_user(cfg, connection, user.id)
            removed += 1

        sessions = purge_expired_sessions(connection)
        purge_old_login_attempts(connection)
        purge_old_tester_signups(connection)

    spooled = sweep_spool(cfg)
    abandoned = sweep_incoming(cfg)

    if removed or sessions or spooled or abandoned:
        # Sem nome de arquivo e sem id de usuario: log de servidor nao guarda o
        # que o usuario subiu nem como ele se chama.
        log.info(
            "operation=cleanup users=%d freed_mb=%.1f sessions=%d uploads=%d incoming=%d",
            removed,
            freed / (1024 * 1024),
            sessions,
            spooled,
            abandoned,
        )
    return removed, freed
