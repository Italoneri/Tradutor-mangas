"""O banco: identidade, sessao, fila e cota.

`sqlite3` da stdlib e um arquivo em `data/mangatl.db`. Nao ha ORM de proposito -
o esquema inteiro cabe em poucas tabelas, e migracao aqui e uma lista de scripts
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
from typing import NamedTuple

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


class TableRebuild(NamedTuple):
    """Migracao que reconstroi uma tabela, e por isso roda com as chaves desligadas.

    O sqlite nao altera `CHECK`, entao mudar um exige criar a tabela de novo. Com
    `foreign_keys = ON`, o `DROP TABLE` da tabela velha e um `DELETE` implicito, e
    o `ON DELETE CASCADE` das filhas apaga todas as linhas delas sem erro nenhum.
    E o procedimento de 12 passos da documentacao do sqlite ("Making Other Kinds
    Of Table Schema Changes"), e o pragma so muda fora de transacao - por isso a
    marca, e nao o script decidindo sozinho.
    """

    script: str


MIGRATIONS: tuple[str | TableRebuild, ...] = (
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
    # 6 - o papel `member`, e o que o dono ajusta por conta
    TableRebuild(
        """
        CREATE TABLE users_new (
            id               TEXT PRIMARY KEY,
            kind             TEXT NOT NULL CHECK (kind IN ('owner', 'member', 'tester')),
            email            TEXT UNIQUE,
            password_hash    TEXT,
            created_at       TEXT NOT NULL,
            expires_at       TEXT,
            password_origin  TEXT
                CHECK (password_origin IS NULL OR password_origin IN ('env', 'panel')),
            status           TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'disabled')),
            last_login_at    TEXT,
            disk_limit_bytes INTEGER,
            pages_per_day    INTEGER,
            max_pending_jobs INTEGER
        );

        INSERT INTO users_new (id, kind, email, password_hash, created_at, expires_at, password_origin)
            SELECT id, kind, email, password_hash, created_at, expires_at, password_origin FROM users;

        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;

        CREATE INDEX users_expires_at ON users (expires_at) WHERE expires_at IS NOT NULL;
        """
    ),
    # 7 - convite, redefinicao de senha e o registro do que o dono fez
    """
    CREATE TABLE invites (
        token_hash TEXT PRIMARY KEY,
        created_by TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        note       TEXT,
        used_by    TEXT REFERENCES users (id) ON DELETE SET NULL,
        used_at    TEXT
    );

    CREATE INDEX invites_created_at ON invites (created_at);

    CREATE TABLE password_resets (
        token_hash TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at    TEXT
    );

    CREATE INDEX password_resets_user_id ON password_resets (user_id);

    CREATE TABLE audit_log (
        at        TEXT NOT NULL,
        actor_id  TEXT,
        action    TEXT NOT NULL,
        target_id TEXT,
        detail    TEXT
    );

    CREATE INDEX audit_log_at ON audit_log (at);
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

    `app` e `worker` sobem juntos e os dois chamam isto. Por isso cada migracao
    pega a trava de escrita e so entao confere se ja foi aplicada: medido na
    migracao 6, o `app` lia a lista de versoes antes de o `worker` gravar a dele,
    tentava de novo e morria na subida.
    """
    with connect(cfg) as connection:
        _applied_versions(connection)
        for version, migration in enumerate(MIGRATIONS, start=1):
            _apply(connection, version, migration)
        return len(MIGRATIONS)


def _apply(connection: sqlite3.Connection, version: int, migration: str | TableRebuild) -> None:
    """Aplica uma migracao e registra a versao na mesma transacao. Tudo ou nada.

    O sqlite faz DDL dentro de transacao, entao ou a migracao inteira entrou e
    ficou registrada, ou nao aconteceu. Instrucao por instrucao, e nao
    `executescript`: ele faria COMMIT da transacao aberta antes de rodar o script,
    e a trava pega para conferir a versao se soltaria antes da hora.

    Um `TableRebuild` roda com as chaves desligadas, e o `foreign_key_check` e o
    passo que a documentacao pede antes do COMMIT: com as chaves desligadas o
    banco aceita qualquer coisa, e e ali que uma copia que esqueceu linha aparece
    como filha orfa em vez de virar dado perdido.
    """
    rebuild = isinstance(migration, TableRebuild)
    script = migration.script if rebuild else migration
    if rebuild:
        connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if version in _applied_versions(connection):
            connection.execute("ROLLBACK")
            return
        for statement in _statements(script):
            connection.execute(statement)
        orphans = connection.execute("PRAGMA foreign_key_check").fetchall() if rebuild else []
        if orphans:
            raise RuntimeError(
                f"migracao {version}: {len(orphans)} linha(s) sem dono depois da reconstrucao"
            )
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (version, now())
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        if rebuild:
            connection.execute("PRAGMA foreign_keys = ON")


def _statements(script: str) -> list[str]:
    """As instrucoes de um script de migracao.

    Separar por `;` basta aqui porque nenhum script tem gatilho nem texto com
    ponto e virgula; `complete_statement` recusa o script se um dia tiver.
    """
    statements = [part.strip() for part in script.split(";") if part.strip()]
    for statement in statements:
        if not sqlite3.complete_statement(f"{statement};"):
            raise ValueError(f"instrucao incompleta na migracao: {statement[:60]!r}")
    return statements
