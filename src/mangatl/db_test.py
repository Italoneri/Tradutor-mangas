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
