from __future__ import annotations

import threading

import pytest

from .accounts import create_user, user_path
from .cleanup import sweep
from .config import Config
from .db import connect, in_hours, migrate
from .jobs import OWNER_PRIORITY, TESTER_PRIORITY, enqueue, get_any
from .sessions import issue, resolve
from . import worker


@pytest.fixture
def cfg(tmp_path) -> Config:  # noqa: ANN001
    migrate(Config(root=tmp_path))
    return Config(root=tmp_path)


@pytest.fixture
def owner(cfg):  # noqa: ANN001
    with connect(cfg) as connection:
        return create_user(connection, kind="owner", email="dono@example.com")


def job_state(cfg, job_id):  # noqa: ANN001, ANN201
    with connect(cfg) as connection:
        found = get_any(connection, job_id)
    return None if found is None else found.state


# ---------- o laco ----------


def test_does_nothing_when_the_queue_is_empty(cfg):
    assert worker.run_once(cfg) is False


def test_marks_the_job_failed_instead_of_dying(cfg, owner):
    """O capitulo nao existe em disco. A falha e o resultado do job, e o worker
    continua de pe para o proximo - um worker que morre no primeiro erro deixa a
    fila inteira parada."""
    with connect(cfg) as connection:
        job = enqueue(
            connection,
            user_id=owner.id,
            series="Fantasma",
            chapter="001",
            engine="free",
            priority=OWNER_PRIORITY,
        )

    assert worker.run_once(cfg) is True

    with connect(cfg) as connection:
        stored = get_any(connection, job.id)

    assert stored.state == "failed"
    assert stored.error
    assert stored.finished_at is not None


def test_cancels_a_job_whose_account_went_away_while_it_waited(cfg):
    """A limpeza pode levar o testador entre o worker pegar o job e abrir a area.

    A cascata do banco apaga o job junto com o usuario, entao esta linha so e
    alcancavel nessa corrida - e sem ela o worker estouraria com AttributeError
    em vez de fechar o job.
    """
    with connect(cfg) as connection:
        tester = create_user(connection, kind="tester", expires_at=in_hours(-1))
        job = enqueue(
            connection,
            user_id=tester.id,
            series="Obra",
            chapter="001",
            engine="free",
            priority=TESTER_PRIORITY,
        )
        # Sem a cascata: o que sobra e exatamente o estado da corrida.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM users WHERE id = ?", (tester.id,))

    worker._run_job(cfg, job)

    assert job_state(cfg, job.id) == "cancelled"


def test_stops_when_asked(cfg):
    stop = threading.Event()
    stop.set()

    worker.run_forever(cfg, stop=stop)  # volta na hora, sem travar o teste


def test_puts_running_jobs_back_in_the_queue_on_boot(cfg, owner):
    with connect(cfg) as connection:
        job = enqueue(
            connection,
            user_id=owner.id,
            series="Obra",
            chapter="001",
            engine="free",
            priority=OWNER_PRIORITY,
        )
        connection.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (job.id,))

    stop = threading.Event()
    stop.set()
    worker.run_forever(cfg, stop=stop)

    assert job_state(cfg, job.id) == "pending"


# ---------- limpeza ----------


def test_removes_an_expired_tester_with_their_disk_and_session(cfg):
    with connect(cfg) as connection:
        tester = create_user(connection, kind="tester", expires_at=in_hours(-1))
        token, _ = issue(connection, tester.id, hours=48)
    page = user_path(cfg, tester.id, "library", "Obra", "001", "p0001.jpg")
    page.parent.mkdir(parents=True)
    page.write_bytes(b"x" * 2048)

    removed, freed = sweep(cfg)

    assert removed == 1
    assert freed == 2048
    assert not user_path(cfg, tester.id).exists()
    with connect(cfg) as connection:
        assert resolve(connection, token) is None


def test_keeps_a_tester_who_still_has_time(cfg):
    with connect(cfg) as connection:
        create_user(connection, kind="tester", expires_at=in_hours(48))

    assert sweep(cfg) == (0, 0)

    with connect(cfg) as connection:
        assert connection.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 1


def test_never_removes_the_owner(cfg, owner):
    """O dono nao tem `expires_at`, e o acervo dele mora nesse disco."""
    with connect(cfg) as connection:
        connection.execute("UPDATE users SET expires_at = ? WHERE id = ?", (in_hours(-1), owner.id))

    assert sweep(cfg) == (0, 0)
