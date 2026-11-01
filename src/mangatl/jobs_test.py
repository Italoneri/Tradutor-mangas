from __future__ import annotations

import pytest

from .accounts import User, create_user
from .config import Config
from .db import connect, migrate
from .jobs import (
    CRASHED_ERROR,
    HISTORY,
    MAX_ATTEMPTS,
    MEMBER_PRIORITY,
    OWNER_PRIORITY,
    TESTER_PRIORITY,
    Busy,
    claim_next,
    enqueue,
    finish,
    get,
    get_any,
    priority_for,
    queue_position,
    recent,
    requeue_running,
    set_progress,
)
from .models import Progress


@pytest.fixture
def cfg(tmp_path) -> Config:  # noqa: ANN001
    migrate(Config(root=tmp_path))
    return Config(root=tmp_path)


@pytest.fixture
def people(cfg):  # noqa: ANN001
    """Um dono e dois testadores, que e a mistura que a fila existe para arbitrar."""
    with connect(cfg) as connection:
        owner = create_user(connection, kind="owner", email="dono@example.com")
        first = create_user(connection, kind="tester")
        second = create_user(connection, kind="tester")
    return owner, first, second


def _queue(connection, user, chapter="001", priority=TESTER_PRIORITY):  # noqa: ANN001
    return enqueue(
        connection,
        user_id=user.id,
        series="Obra",
        chapter=chapter,
        engine="free",
        priority=priority,
    )


# ---------- enfileirar ----------


def test_stores_the_job_as_pending(cfg, people):
    owner, _, _ = people
    with connect(cfg) as connection:
        job = _queue(connection, owner, priority=OWNER_PRIORITY)

        assert job.state == "pending"
        assert get(connection, job.id, owner.id).chapter == "001"


def test_refuses_a_second_job_for_the_same_person(cfg, people):
    _, tester, _ = people
    with connect(cfg) as connection:
        _queue(connection, tester, "001")

        with pytest.raises(Busy):
            _queue(connection, tester, "002")


def test_lets_two_people_queue_at_the_same_time(cfg, people):
    _, first, second = people
    with connect(cfg) as connection:
        _queue(connection, first)
        _queue(connection, second)

        assert len(recent(connection, first.id)) == 1
        assert len(recent(connection, second.id)) == 1


# ---------- ordem da fila ----------


def test_runs_the_owner_before_the_tester(cfg, people):
    """O dono nao espera a curiosidade de um visitante terminar."""
    owner, tester, _ = people
    with connect(cfg) as connection:
        _queue(connection, tester)
        _queue(connection, owner, priority=OWNER_PRIORITY)

        assert claim_next(connection).user_id == owner.id


def test_runs_the_older_first_among_equals(cfg, people):
    _, first, second = people
    with connect(cfg) as connection:
        older = _queue(connection, first)
        _queue(connection, second)

        assert claim_next(connection).id == older.id


def test_never_runs_two_jobs_of_the_same_person(cfg, people):
    """A trava que impede uma pessoa de ocupar a fila inteira."""
    _, tester, other = people
    with connect(cfg) as connection:
        mine = _queue(connection, tester)
        claim_next(connection)
        finish(connection, mine.id, state="done")
        _queue(connection, tester, "002")
        _queue(connection, other)

        claimed = claim_next(connection)
        second = claim_next(connection)

    # Os dois rodam, mas nunca dois do mesmo ao mesmo tempo.
    assert {claimed.user_id, second.user_id} == {tester.id, other.id}


def test_claims_nothing_when_the_queue_is_empty(cfg):
    with connect(cfg) as connection:
        assert claim_next(connection) is None


def test_reports_how_many_are_ahead(cfg, people):
    owner, tester, other = people
    with connect(cfg) as connection:
        mine = _queue(connection, tester)
        _queue(connection, owner, priority=OWNER_PRIORITY)
        _queue(connection, other)

        assert queue_position(connection, get_any(connection, mine.id)) == 1


# ---------- sobreviver ao reinicio ----------


def test_puts_running_jobs_back_in_the_queue_on_boot(cfg, people):
    """Ninguem estava rodando durante o reinicio; um job preso em `running` e uma
    tela que nunca sai do lugar."""
    _, tester, _ = people
    with connect(cfg) as connection:
        job = _queue(connection, tester)
        claim_next(connection)
        assert get_any(connection, job.id).state == "running"

        assert requeue_running(connection) == 1

        back = get_any(connection, job.id)
        assert back.state == "pending"
        assert back.started_at is None


def test_keeps_the_options_across_a_restart(cfg, people):
    """`--force` perdido no reinicio faria o job voltar reaproveitando o OCR que
    ele existia para refazer."""
    _, tester, _ = people
    with connect(cfg) as connection:
        job = enqueue(
            connection,
            user_id=tester.id,
            series="Obra",
            chapter="001",
            engine="free",
            priority=TESTER_PRIORITY,
            force=True,
            dry_run=True,
            pages=("p2.jpg",),
        )
        claim_next(connection)
        requeue_running(connection)

        assert get_any(connection, job.id).options == {
            "force": True,
            "dry_run": True,
            "pages": ["p2.jpg"],
        }


# ---------- progresso e fim ----------


def test_keeps_the_progress_and_the_last_log_lines(cfg, people):
    _, tester, _ = people
    with connect(cfg) as connection:
        job = _queue(connection, tester)
        set_progress(
            connection,
            job.id,
            Progress(phase="extract", done=3, total=12, detail="p0003.jpg"),
            ("INFO uma linha", "INFO outra"),
        )

        stored = get_any(connection, job.id)

    assert stored.progress.done == 3
    assert stored.progress.detail == "p0003.jpg"
    assert stored.log == ("INFO uma linha", "INFO outra")


def test_records_the_failure_as_the_result_of_the_job(cfg, people):
    _, tester, _ = people
    with connect(cfg) as connection:
        job = _queue(connection, tester)
        finish(connection, job.id, state="failed", error="RuntimeError: estourou")

        stored = get_any(connection, job.id)

    assert stored.state == "failed"
    assert stored.error == "RuntimeError: estourou"
    assert stored.finished_at is not None


# ---------- isolamento ----------


def test_never_hands_a_job_to_someone_else(cfg, people):
    _, tester, other = people
    with connect(cfg) as connection:
        job = _queue(connection, tester)

        assert get(connection, job.id, other.id) is None
        assert recent(connection, other.id) == []


def test_keeps_the_user_id_out_of_the_snapshot(cfg, people):
    """Devolver o id seria oferecer um valor para alguem tentar numa URL."""
    _, tester, _ = people
    with connect(cfg) as connection:
        job = _queue(connection, tester)

    assert "user_id" not in job.snapshot()


def test_shows_only_the_most_recent_jobs(cfg, people):
    _, tester, _ = people
    with connect(cfg) as connection:
        for number in range(HISTORY + 5):
            job = _queue(connection, tester, f"{number:03d}")
            finish(connection, job.id, state="done")

        assert len(recent(connection, tester.id)) == HISTORY


def test_gives_up_on_a_job_that_took_the_worker_down_three_times(cfg, people):
    """Um capitulo que estoura a memoria mata o worker; o Docker o reinicia e o job
    voltava para a fila para matar de novo, com a fila inteira parada atras dele."""
    _, first, second = people
    with connect(cfg) as connection:
        crashing = _queue(connection, first)
        waiting = _queue(connection, second)

        for _ in range(MAX_ATTEMPTS):
            assert claim_next(connection).id == crashing.id
            requeue_running(connection)

        dead = get_any(connection, crashing.id)
        assert dead.state == "failed"
        assert dead.error == CRASHED_ERROR
        assert dead.finished_at is not None
        assert claim_next(connection).id == waiting.id


def test_counts_each_claim_as_an_attempt(cfg, people):
    _, tester, _ = people
    with connect(cfg) as connection:
        job = _queue(connection, tester)
        claim_next(connection)
        requeue_running(connection)
        claim_next(connection)

        assert get_any(connection, job.id).attempts == 2


# ---------- contas de membro ----------


@pytest.fixture
def clock(monkeypatch):
    """Um relogio que sempre anda.

    No teste, dois `claim_next` seguidos caem no mesmo milissegundo, e o empate do
    inicio mais recente cairia no `created_at`. Num worker de verdade cada job leva
    minutos, e esse empate nao existe.
    """
    from datetime import UTC, datetime, timedelta

    from . import jobs

    ticks = iter(range(1, 10_000))
    start = datetime(2026, 10, 27, tzinfo=UTC)
    monkeypatch.setattr(
        jobs, "now", lambda: (start + timedelta(seconds=next(ticks))).isoformat(timespec="milliseconds")
    )


def _member(connection, name: str):  # noqa: ANN001
    return create_user(connection, kind="member", email=f"{name}@example.com")


def _queue_member(connection, user, chapter: str, max_active: int | None = 3):  # noqa: ANN001
    return enqueue(
        connection,
        user_id=user.id,
        series="Obra",
        chapter=chapter,
        engine="free",
        priority=MEMBER_PRIORITY,
        max_active=max_active,
    )


def test_puts_owner_then_member_then_tester():
    def user(kind: str) -> User:
        return User(id="a" * 32, kind=kind, email=None, created_at="", expires_at=None)

    assert [priority_for(user(kind)) for kind in ("owner", "member", "tester")] == [
        OWNER_PRIORITY,
        MEMBER_PRIORITY,
        TESTER_PRIORITY,
    ]
    assert OWNER_PRIORITY < MEMBER_PRIORITY < TESTER_PRIORITY


def test_refuses_the_job_past_the_account_ceiling(cfg):
    with connect(cfg) as connection:
        member = _member(connection, "m")
        for chapter in ("001", "002", "003"):
            _queue_member(connection, member, chapter)

        with pytest.raises(Busy, match="o teto e 3"):
            _queue_member(connection, member, "004")


def test_refuses_the_same_chapter_twice_even_without_a_ceiling(cfg, people):
    owner, _, _ = people
    with connect(cfg) as connection:
        _queue_member(connection, owner, "001", max_active=None)
        _queue_member(connection, owner, "002", max_active=None)

        with pytest.raises(Busy, match="deste capitulo"):
            _queue_member(connection, owner, "001", max_active=None)


def _drain(connection) -> list[str]:  # noqa: ANN001
    """O que um worker so faria: pega, termina, pega o proximo."""
    order: list[str] = []
    while (job := claim_next(connection)) is not None:
        order.append(job.id)
        finish(connection, job.id, state="done")
    return order


def test_takes_turns_between_members_instead_of_draining_the_first(cfg, clock):
    """Quem enfileirou tres antes nao passa tres vezes na frente de quem veio depois."""
    with connect(cfg) as connection:
        first, second = _member(connection, "a"), _member(connection, "b")
        early = [_queue_member(connection, first, f"00{n}").id for n in (1, 2, 3)]
        late = [_queue_member(connection, second, f"00{n}").id for n in (1, 2, 3)]

        order = _drain(connection)

    assert order == [early[0], late[0], early[1], late[1], early[2], late[2]]


def test_predicts_the_same_order_the_worker_follows(cfg, people, clock):
    owner, tester, _ = people
    with connect(cfg) as connection:
        first, second = _member(connection, "a"), _member(connection, "b")
        queued = [
            _queue_member(connection, first, "001"),
            _queue_member(connection, first, "002"),
            _queue_member(connection, second, "001"),
            _queue(connection, tester),
            _queue_member(connection, owner, "001", max_active=None),
        ]
        predicted = sorted(queued, key=lambda job: queue_position(connection, job))

        assert [job.id for job in predicted] == _drain(connection)
