"""A fila de processamento, no banco e nao na memoria.

O registro em memoria que existia aqui antes estava certo enquanto o programa
rodava na sua maquina para voce: reiniciar perdia o historico, e o resultado que
importava ja estava em `output/`. Hospedado, ele erra por tres motivos de uma vez:

- **reiniciar perde o trabalho.** Um deploy no meio de um capitulo deixava o job
  em `running` para sempre, e a tela de quem esperava nunca mais saia dali;
- **"um por vez" com cinco testadores e uma fila de uma hora**, sem ninguem
  conseguir ver em que lugar da fila esta;
- **um job e de alguem.** Estado global em variavel de modulo nao sabe de quem, e
  a tela de progresso de um mostrava o capitulo do outro.

O desenho agora: quem atende HTTP so enfileira e le; quem processa e um worker em
processo separado. As duas pontas so se falam pelo banco, e e isso que faz o
reinicio ser um detalhe - `requeue_running` devolve para `pending` o que estava
rodando quando o processo caiu.

Prioridade e uma coluna e nao uma consulta esperta: job de dono passa na frente de
job de membro, que passa na frente de job de testador, e um numero na linha diz
isso sem ninguem precisar reconstruir a regra a partir de um `ORDER BY` com `JOIN`.
Entre iguais, a vez e de quem esta ha mais tempo sem rodar nada.

Cancelamento continua fora de escopo. Nao ha ponto de interrupcao seguro no meio
de um OCR, e matar o processo deixaria o `extract.json` escrito pela metade - que
e pior que esperar, porque parece completo na proxima execucao.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal, NamedTuple

from .accounts import User
from .db import now, transaction
from .models import Progress

LOG_LINES = 200
"""Ultimas linhas guardadas por job. O suficiente para ver onde parou sem o
`progress_json` virar um segundo arquivo de log."""

HISTORY = 20
"""Jobs que a tela de alguem mostra. Acima disso e historico, e historico mora em
`output/`."""

JobState = Literal["pending", "running", "done", "failed", "cancelled"]
"""`cancelled` nao e produzido por ninguem hoje: cancelar esta fora de escopo, e o
valor existe para o dia em que entrar sem que o contrato mude."""

OWNER_PRIORITY = 0
MEMBER_PRIORITY = 1
TESTER_PRIORITY = 2
"""Menor vai primeiro. O dono nao espera ninguem para usar a propria ferramenta, e
quem tem conta nao espera a curiosidade de um visitante terminar."""


def priority_for(user: User) -> int:
    if user.is_admin:
        return OWNER_PRIORITY
    return MEMBER_PRIORITY if user.has_account else TESTER_PRIORITY


MAX_ATTEMPTS = 3
"""Subidas do worker que um job pode derrubar antes de desistirem dele.

Um capitulo que estoura o `mem_limit` mata o worker por OOM; o Docker o reinicia,
o job volta para a fila e mata de novo. Sem teto, a fila inteira fica parada atras
desse capitulo, sem erro nenhum na tela."""

CRASHED_ERROR = f"o worker caiu {MAX_ATTEMPTS} vezes neste capitulo; ele foi tirado da fila"


class Busy(RuntimeError):
    """Esta pessoa ja tem na fila todos os processamentos que pode ter."""


@dataclass(frozen=True)
class Job:
    id: str
    user_id: str
    series: str
    chapter: str
    engine: str
    state: JobState
    progress: Progress
    log: tuple[str, ...]
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    options: dict
    priority: int
    attempts: int = 0

    def snapshot(self) -> dict:
        """O que a tela recebe.

        `user_id` fica de fora: quem pergunta ja e o dono do job, e devolver o id
        seria oferecer um valor para alguem tentar numa URL.
        """
        return {
            "id": self.id,
            "series": self.series,
            "chapter": self.chapter,
            "engine": self.engine,
            "state": self.state,
            "progress": self.progress.model_dump(mode="json"),
            "log": list(self.log),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


def _to_job(row: sqlite3.Row) -> Job:
    progress = json.loads(row["progress_json"] or "{}")
    return Job(
        id=row["id"],
        user_id=row["user_id"],
        series=row["series"],
        chapter=row["chapter"],
        engine=row["engine"],
        state=row["state"],
        progress=Progress(**progress) if progress else Progress(phase="extract"),
        log=tuple(json.loads(row["log_json"] or "[]")),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
        options=json.loads(row["options_json"] or "{}"),
        priority=row["priority"],
        attempts=row["attempts"],
    )


def enqueue(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    series: str,
    chapter: str,
    engine: str,
    priority: int,
    force: bool = False,
    dry_run: bool = False,
    pages: tuple[str, ...] = (),
    max_active: int | None = 1,
) -> Job:
    """Poe um job na fila, ou levanta `Busy`.

    `max_active` conta o que esta na fila e o que esta rodando, e None e sem teto.
    O default e um, que e a regra de antes das contas: quem chama sem pensar nisso
    nao abre a fila sem querer.

    A checagem e a insercao acontecem na mesma transacao `IMMEDIATE`: separadas,
    dois pedidos simultaneos passariam os dois pela checagem antes de qualquer um
    inserir, e a pessoa passaria do teto.
    """
    job_id = uuid.uuid4().hex[:12]
    with transaction(connection):
        waiting = connection.execute(
            "SELECT count(*) AS n FROM jobs WHERE user_id = ? AND state IN ('pending', 'running')",
            (user_id,),
        ).fetchone()["n"]
        # O mesmo capitulo duas vezes e recusado para todo papel, dono incluso: o
        # segundo job refaria o primeiro e sobrescreveria o resultado dele.
        if has_active(connection, user_id, series, chapter):
            raise Busy("voce ja tem um processamento deste capitulo na fila; deixe terminar")
        if max_active is not None and waiting >= max_active:
            raise Busy(
                "voce ja tem um processamento na fila; deixe terminar"
                if max_active == 1
                else f"voce ja tem {waiting} processamentos na fila; o teto e {max_active}"
            )

        connection.execute(
            """
            INSERT INTO jobs (
                id, user_id, series, chapter, engine, state, progress_json,
                created_at, priority, options_json, log_json
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, '[]')
            """,
            (
                job_id,
                user_id,
                series,
                chapter,
                engine,
                json.dumps(Progress(phase="extract", detail="na fila").model_dump(mode="json")),
                now(),
                priority,
                json.dumps({"force": force, "dry_run": dry_run, "pages": list(pages)}),
            ),
        )
    return get_any(connection, job_id)


def claim_next(connection: sqlite3.Connection) -> Job | None:
    """Marca como `running` o proximo job que pode rodar, e devolve.

    Tres regras numa consulta so: prioridade por papel, no maximo um job por
    usuario ao mesmo tempo, e entre iguais a vez de quem esta ha mais tempo sem
    rodar nada. Sem a terceira, quem enfileirou dez capitulos as 9h passava na
    frente de quem enfileirou um as 9h05 dez vezes seguidas; com ela, as contas
    se alternam. Conta que nunca rodou nada vem primeiro: no sqlite, NULL ordena
    antes de qualquer data.

    Tudo dentro de uma transacao `IMMEDIATE`, para dois workers nao pegarem o
    mesmo job - a escolha e a marcacao tem que ser indivisiveis.
    """
    with transaction(connection):
        row = connection.execute(
            """
            SELECT candidate.* FROM jobs AS candidate
            WHERE candidate.state = 'pending'
              AND NOT EXISTS (
                  SELECT 1 FROM jobs AS mine
                  WHERE mine.user_id = candidate.user_id AND mine.state = 'running'
              )
            ORDER BY
                candidate.priority,
                (SELECT max(past.started_at) FROM jobs AS past WHERE past.user_id = candidate.user_id),
                candidate.created_at
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None

        connection.execute(
            "UPDATE jobs SET state = 'running', started_at = ?, attempts = attempts + 1"
            " WHERE id = ?",
            (now(), row["id"]),
        )
    return get_any(connection, row["id"])


def set_progress(
    connection: sqlite3.Connection, job_id: str, progress: Progress, log: tuple[str, ...] = ()
) -> None:
    connection.execute(
        "UPDATE jobs SET progress_json = ?, log_json = ? WHERE id = ?",
        (
            json.dumps(progress.model_dump(mode="json")),
            json.dumps(list(log)[-LOG_LINES:]),
            job_id,
        ),
    )


def finish(
    connection: sqlite3.Connection, job_id: str, *, state: JobState, error: str | None = None
) -> None:
    """Fecha o job. `finished_at` e `state` vao juntos, na mesma escrita.

    Separados, quem consulta poderia ler `failed` com `finished_at` ainda nulo - o
    job no meio da propria conclusao.
    """
    connection.execute(
        "UPDATE jobs SET state = ?, error = ?, finished_at = ? WHERE id = ?",
        (state, error, now(), job_id),
    )


def requeue_running(connection: sqlite3.Connection) -> int:
    """Devolve para a fila o que ficou marcado como rodando, e desiste do reincidente.

    Chamado na subida do worker. Ninguem estava rodando durante o reinicio, e um
    job preso em `running` e uma tela que nunca sai do lugar. Mas um job que ja
    esteve rodando `MAX_ATTEMPTS` vezes quando o worker caiu e, com toda chance, a
    causa da queda - devolve-lo seria derrubar o worker de novo.
    """
    with transaction(connection):
        connection.execute(
            "UPDATE jobs SET state = 'failed', error = ?, finished_at = ?"
            " WHERE state = 'running' AND attempts >= ?",
            (CRASHED_ERROR, now(), MAX_ATTEMPTS),
        )
        cursor = connection.execute(
            "UPDATE jobs SET state = 'pending', started_at = NULL WHERE state = 'running'"
        )
    return cursor.rowcount


def has_active(
    connection: sqlite3.Connection, user_id: str, series: str, chapter: str | None = None
) -> bool:
    """Se esta pessoa tem job esperando ou rodando nesta serie - ou neste capitulo.

    Apagar o que o worker esta lendo transformaria o job num erro de arquivo
    sumido no meio do OCR; recusar a remocao enquanto ele vive e mais honesto.
    """
    row = connection.execute(
        """
        SELECT 1 FROM jobs
        WHERE user_id = ? AND series = ? AND (? IS NULL OR chapter = ?)
          AND state IN ('pending', 'running')
        LIMIT 1
        """,
        (user_id, series, chapter, chapter),
    ).fetchone()
    return row is not None


def get_any(connection: sqlite3.Connection, job_id: str) -> Job | None:
    """O job, sem filtrar por dono. So o worker usa: ele roda job de todo mundo."""
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return None if row is None else _to_job(row)


def get(connection: sqlite3.Connection, job_id: str, user_id: str) -> Job | None:
    """O job, se for desta pessoa.

    `user_id` nao e opcional de proposito: um default faria a chamada sem filtro
    parecer certa, e ela e exatamente o bug que entrega o progresso de um capitulo
    alheio - com nome de serie e de capitulo junto.
    """
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
    ).fetchone()
    return None if row is None else _to_job(row)


def recent(connection: sqlite3.Connection, user_id: str, limit: int = HISTORY) -> list[Job]:
    rows = connection.execute(
        "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    return [_to_job(row) for row in rows]


def queue_position(connection: sqlite3.Connection, job: Job) -> int:
    """Quantos jobs passam na frente deste. Zero quando ele e o proximo.

    A tela mostra isto porque "na fila" sem numero e indistinguivel de travado.
    A conta repete a regra do `claim_next` em vez de contar quem e mais antigo:
    com a vez alternando entre contas, a idade do job sozinha mente.
    """
    # Sem LIMIT de proposito: o que esta na fila ja e limitado pelo teto de jobs
    # ativos de cada conta, e cortar a lista erraria a posicao.
    waiting = [
        _Turn(row["id"], row["user_id"], row["priority"], row["created_at"])
        for row in connection.execute(
            "SELECT id, user_id, priority, created_at FROM jobs WHERE state = 'pending'"
        )
    ]
    last_start = {
        row["user_id"]: row["last_start"]
        for row in connection.execute(
            "SELECT user_id, max(started_at) AS last_start FROM jobs GROUP BY user_id"
        )
    }
    return claim_order(waiting, last_start).index(job.id) if job.state == "pending" else 0


class _Turn(NamedTuple):
    id: str
    user_id: str
    priority: int
    created_at: str


def claim_order(waiting: list[_Turn], last_start: dict[str, str | None]) -> list[str]:
    """A ordem em que o `claim_next` pegaria estes jobs, com um worker so.

    Cada job pego passa a contar como o inicio mais recente da conta dele, e por
    isso a conta vai para o fim da vez. `~` ordena depois de qualquer data ISO.
    """
    started = dict(last_start)
    left = list(waiting)
    order: list[str] = []
    while left:
        chosen = min(left, key=lambda turn: (turn.priority, started.get(turn.user_id) or "", turn.created_at))
        left.remove(chosen)
        order.append(chosen.id)
        started[chosen.user_id] = f"~{len(order):06d}"
    return order
