"""O banco: identidade, sessao, fila e cota.

`sqlite3` da stdlib e um arquivo em `data/mangatl.db`. Nao ha ORM de proposito -
o esquema inteiro cabe em quatro tabelas, e migracao aqui e uma lista de scripts
numerados aplicados em ordem.

Tres decisoes que o resto do sistema depende:

**WAL.** Sem ele, o worker escrevendo o progresso de um job bloqueia o handler que
le a sessao, e o leitor trava enquanto um capitulo processa.

**Uma conexao por operacao.** O servidor e `ThreadingHTTPServer` e conexao do
sqlite3 nao atravessa thread com seguranca. Abrir e fechar custa microssegundos
contra os milissegundos de qualquer requisicao, e elimina a classe inteira de bug
de conexao compartilhada.

**`busy_timeout`.** Duas escritas simultaneas em WAL ainda serializam. Sem timeout,
a segunda levanta `database is locked` na hora, em vez de esperar os milissegundos
que a primeira leva.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Config

DB_FILENAME = "mangatl.db"

BUSY_TIMEOUT_MS = 5000
"""Espera antes de desistir de uma escrita concorrente.

Cinco segundos cobre com folga a escrita mais longa que existe aqui (o progresso
de um job, algumas centenas de bytes) e ainda falha rapido o suficiente para o
handler devolver erro em vez de pendurar a requisicao."""


def now() -> str:
    """Instante atual em UTC, no formato que todas as colunas de data usam.

    ISO 8601 em texto: ordena lexicograficamente igual a cronologicamente, que e o
    que faz `WHERE expires_at < ?` funcionar sem funcao de data no meio do indice.

    Milissegundos e nao segundos porque a fila ordena por `created_at`: dois jobs
    enfileirados no mesmo segundo empatavam, e empate ali significa que a ordem de
    atendimento passa a ser o que o banco decidir - uma fila que nao e FIFO sem
    ninguem ter escolhido isso.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def in_hours(hours: int) -> str:
    """Um instante no futuro, no mesmo formato de `now`.

    Existe para ninguem escrever a propria versao com outra precisao: comparar um
    `expires_at` gravado em segundos com um `now()` em milissegundos erra por um
    segundo, sempre para o lado de expirar cedo demais.
    """
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="milliseconds")


def minutes_ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")


MIGRATIONS: tuple[str, ...] = (
    # 1 - identidade, sessao, fila e cota
    """
    CREATE TABLE users (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL CHECK (kind IN ('owner', 'tester')),
        email         TEXT UNIQUE,
        password_hash TEXT,
        created_at    TEXT NOT NULL,
        expires_at    TEXT
    );

    CREATE INDEX users_expires_at ON users (expires_at) WHERE expires_at IS NOT NULL;

    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        last_seen  TEXT NOT NULL
    );

    CREATE INDEX sessions_user_id ON sessions (user_id);
    CREATE INDEX sessions_expires_at ON sessions (expires_at);

    CREATE TABLE jobs (
        id            TEXT PRIMARY KEY,
        user_id       TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        series        TEXT NOT NULL,
        chapter       TEXT NOT NULL,
        engine        TEXT NOT NULL,
        state         TEXT NOT NULL,
        progress_json TEXT NOT NULL DEFAULT '{}',
        error         TEXT,
        created_at    TEXT NOT NULL,
        finished_at   TEXT
    );

    CREATE INDEX jobs_user_id ON jobs (user_id, created_at DESC);
    CREATE INDEX jobs_state ON jobs (state, created_at);

    CREATE TABLE usage (
        user_id  TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        day      TEXT NOT NULL,
        pages    INTEGER NOT NULL DEFAULT 0,
        chapters INTEGER NOT NULL DEFAULT 0,
        bytes    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    );
    """,
    # 2 - tentativas de login, para o limite por IP e por e-mail
    """
    CREATE TABLE login_attempts (
        subject TEXT NOT NULL,
        at      TEXT NOT NULL
    );

    CREATE INDEX login_attempts_subject ON login_attempts (subject, at);
    """,
    # 3 - a fila deixa de viver na memoria
    """
    ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE jobs ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE jobs ADD COLUMN log_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE jobs ADD COLUMN started_at TEXT;

    CREATE INDEX jobs_queue ON jobs (state, priority, created_at);
    """,
    # 4 - job que derruba o worker desiste, e testador novo tem limite por IP
    """
    ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;

    CREATE TABLE tester_signups (
        ip_hash TEXT NOT NULL,
        at      TEXT NOT NULL
    );

    CREATE INDEX tester_signups_ip_hash ON tester_signups (ip_hash, at);
    """,
    # 5 - de onde veio a senha do dono, para o reinicio nao desfazer a troca
    """
    ALTER TABLE users ADD COLUMN password_origin TEXT
        CHECK (password_origin IS NULL OR password_origin IN ('env', 'panel'));
    """,
)
"""Scripts aplicados em ordem, uma vez cada.

Script aplicado nunca e editado - a maquina do dono ja rodou aquele texto e
mudar a linha nao muda o banco dela. Correcao entra como script novo no fim.
"""


def database_path(cfg: Config) -> Path:
    return cfg.data_dir / DB_FILENAME


@contextmanager
def connect(cfg: Config) -> Iterator[sqlite3.Connection]:
    """Conexao pronta, em autocommit, fechada no fim.

    `isolation_level=None` desliga o commit implicito do driver: cada `execute`
    vale por si, que e o certo para as escritas de uma instrucao so que dominam
    aqui. Quem precisa de varias instrucoes indivisiveis usa `transaction`.
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        database_path(cfg), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000
    )
    connection.row_factory = sqlite3.Row
    try:
        # `busy_timeout` PRIMEIRO, e isto nao e estilo. Ligar o WAL pede uma trava
        # exclusiva por um instante, e sem o timeout ja valendo essa linha falha na
        # hora com `database is locked` quando outro processo esta abrindo o banco
        # ao mesmo tempo - que e exatamente o que `app` e `worker` fazem ao subir
        # juntos. Medido: o worker morria no primeiro boot e so entrava no restart.
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Tudo entra ou nada entra.

    `IMMEDIATE` pega a trava de escrita ja no BEGIN. Com `DEFERRED`, duas
    transacoes que leem e depois escrevem chegam juntas na escrita e uma morre
    com `database is locked` sem chance de esperar - que e exatamente o caso de
    "conferiu a cota e gravou" deste projeto.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def _applied_versions(connection: sqlite3.Connection) -> set[int]:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    return {row["version"] for row in connection.execute("SELECT version FROM schema_migrations")}


def migrate(cfg: Config) -> int:
    """Aplica o que falta e devolve a versao final. Idempotente.

    Chamada em toda subida em vez de por comando separado: conferir uma tabela de
    quatro linhas nao custa nada, e a alternativa e alguem subir o servidor com o
    esquema velho e descobrir na primeira escrita.

    O script e o registro da versao vao no mesmo `executescript`, com BEGIN e
    COMMIT escritos a mao: o sqlite faz DDL dentro de transacao, entao ou a
    migracao inteira entrou e ficou registrada, ou nao aconteceu. Sem isso, uma
    queda entre as duas coisas deixaria a tabela criada e a versao ausente - e a
    proxima subida morreria em `table already exists`.
    """
    with connect(cfg) as connection:
        applied = _applied_versions(connection)
        for version, script in enumerate(MIGRATIONS, start=1):
            if version in applied:
                continue
            connection.executescript(
                "BEGIN;\n"
                f"{script}\n"
                "INSERT INTO schema_migrations (version, applied_at)"
                f" VALUES ({version}, '{now()}');\n"
                "COMMIT;"
            )
        return len(MIGRATIONS)
