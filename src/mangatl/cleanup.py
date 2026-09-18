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

from .accounts import delete_user, expired_testers
from .config import Config
from .db import connect
from .sessions import purge_expired_sessions, purge_old_login_attempts

log = logging.getLogger("mangatl.cleanup")

EVERY_SECONDS = 15 * 60
"""Ritmo da varredura.

O prazo do testador e de 48 horas, entao quinze minutos de atraso na remocao nao
mudam nada para ninguem - e uma varredura a cada quinze minutos nao aparece em
grafico de CPU."""


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

    if removed or sessions:
        # Sem nome de arquivo e sem id de usuario: log de servidor nao guarda o
        # que o usuario subiu nem como ele se chama.
        log.info(
            "operation=cleanup users=%d freed_mb=%.1f sessions=%d",
            removed,
            freed / (1024 * 1024),
            sessions,
        )
    return removed, freed
