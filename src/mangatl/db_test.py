from __future__ import annotations

import sqlite3

import pytest

from .config import Config
from .db import MIGRATIONS, connect, migrate, now, transaction


@pytest.fixture
def cfg(tmp_path):
    return Config(root=tmp_path)


def test_creates_the_schema_and_reports_the_version(cfg):
    assert migrate(cfg) == len(MIGRATIONS)

    with connect(cfg) as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert {"users", "sessions", "jobs", "usage", "schema_migrations"} <= tables


def test_applies_each_migration_once(cfg):
    migrate(cfg)
    migrate(cfg)
    migrate(cfg)

    with connect(cfg) as connection:
        applied = connection.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()

    assert applied["n"] == len(MIGRATIONS)


def test_runs_in_wal(cfg):
    """Sem WAL, o worker escrevendo progresso bloqueia quem le a sessao."""
    migrate(cfg)

    with connect(cfg) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode == "wal"


def test_drops_the_session_when_the_user_goes(cfg):
    """A cascata e o que impede sessao viva apontando para usuario apagado."""
    migrate(cfg)
    with connect(cfg) as connection:
        connection.execute(
            "INSERT INTO users (id, kind, created_at) VALUES ('u', 'tester', ?)", (now(),)
        )
        connection.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen)"
            " VALUES ('h', 'u', ?, ?, ?)",
            (now(), now(), now()),
        )

        connection.execute("DELETE FROM users WHERE id = 'u'")
        left = connection.execute("SELECT count(*) AS n FROM sessions").fetchone()

    assert left["n"] == 0


def test_refuses_a_session_for_a_user_that_does_not_exist(cfg):
    migrate(cfg)
    with connect(cfg) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen)"
            " VALUES ('h', 'fantasma', ?, ?, ?)",
            (now(), now(), now()),
        )


def test_rolls_back_the_whole_transaction_on_failure(cfg):
    migrate(cfg)
    with connect(cfg) as connection:
        with pytest.raises(RuntimeError), transaction(connection):
            connection.execute(
                "INSERT INTO users (id, kind, created_at) VALUES ('u', 'tester', ?)", (now(),)
            )
            raise RuntimeError("a segunda escrita falhou")

        left = connection.execute("SELECT count(*) AS n FROM users").fetchone()

    assert left["n"] == 0


def test_refuses_an_unknown_kind_of_user(cfg):
    """Estado impossivel recusado pelo banco, e nao so por quem escreve nele."""
    migrate(cfg)
    with connect(cfg) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO users (id, kind, created_at) VALUES ('u', 'admin', ?)", (now(),)
        )


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("users", "sessions", "jobs", "usage")
    }


def test_rebuilds_users_without_losing_a_session_a_job_or_a_day_of_usage(cfg, monkeypatch):
    """A armadilha da migracao 6: `DROP TABLE users` com as chaves ligadas.

    A cascata apagaria sessoes, jobs e uso sem erro nenhum. O banco aqui tem o
    que um dono de verdade tem antes da migracao, e tudo precisa continuar la.
    """
    from . import db

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS[:5])
    migrate(cfg)
    with connect(cfg) as connection:
        connection.execute(
            "INSERT INTO users (id, kind, email, password_hash, created_at, password_origin)"
            " VALUES ('dono', 'owner', 'dono@example.com', 'hash', ?, 'env')",
            (now(),),
        )
        connection.execute(
            "INSERT INTO users (id, kind, created_at, expires_at) VALUES ('teste', 'tester', ?, ?)",
            (now(), now()),
        )
        for user in ("dono", "teste"):
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen)"
                " VALUES (?, ?, ?, ?, ?)",
                (f"h-{user}", user, now(), now(), now()),
            )
            connection.execute(
                "INSERT INTO jobs (id, user_id, series, chapter, engine, state, created_at)"
                " VALUES (?, ?, 'obra', '001', 'free', 'done', ?)",
                (f"j-{user}", user, now()),
            )
            connection.execute(
                "INSERT INTO usage (user_id, day, pages) VALUES (?, '2026-10-27', 12)", (user,)
            )
        before = _counts(connection)

    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
    migrate(cfg)

    with connect(cfg) as connection:
        after = _counts(connection)
        owner = connection.execute("SELECT * FROM users WHERE id = 'dono'").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]

    assert after == before == {"users": 2, "sessions": 2, "jobs": 2, "usage": 2}
    assert (owner["email"], owner["password_hash"], owner["password_origin"]) == (
        "dono@example.com",
        "hash",
        "env",
    )
    assert owner["status"] == "active"
    assert foreign_keys == 1


def test_accepts_a_member_after_the_rebuild(cfg):
    migrate(cfg)
    with connect(cfg) as connection:
        connection.execute(
            "INSERT INTO users (id, kind, email, created_at) VALUES ('m', 'member', 'm@example.com', ?)",
            (now(),),
        )
        connection.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen)"
            " VALUES ('h', 'm', ?, ?, ?)",
            (now(), now(), now()),
        )

        # A cascata tem que continuar valendo na tabela reconstruida.
        connection.execute("DELETE FROM users WHERE id = 'm'")
        left = connection.execute("SELECT count(*) FROM sessions").fetchone()[0]

    assert left == 0


def test_lets_app_and_worker_migrate_at_the_same_time(cfg):
    """Os dois sobem juntos no compose, e os dois chamam `migrate`."""
    import threading

    start = threading.Barrier(4)
    errors: list[BaseException] = []

    def boot() -> None:
        start.wait()
        try:
            migrate(cfg)
        except BaseException as error:  # noqa: BLE001 - o teste coleta para afirmar
            errors.append(error)

    threads = [threading.Thread(target=boot) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with connect(cfg) as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]

    assert errors == []
    assert versions == list(range(1, len(MIGRATIONS) + 1))
