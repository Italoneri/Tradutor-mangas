from __future__ import annotations

import pytest

from .accounts import create_user, get_user
from .config import Config
from .db import connect, migrate, now
from .invites import (
    EmailTaken,
    InviteRefused,
    accept_invite,
    create_invite,
    list_invites,
    public_origin,
    revoke_invite,
)


@pytest.fixture
def connection(tmp_path):
    cfg = Config(root=tmp_path)
    migrate(cfg)
    with connect(cfg) as opened:
        yield opened


@pytest.fixture
def owner(connection):
    return create_user(connection, kind="owner", email="dono@example.com")


def _accept(connection, token: str, email: str = "membro@example.com", tester=None):
    return accept_invite(connection, token=token, email=email, password_hash="hash", tester=tester)


def test_turns_an_invite_into_a_member_account_once(connection, owner):
    token, _ = create_invite(connection, owner, "para o Joao")

    member = _accept(connection, token)
    with pytest.raises(InviteRefused):
        _accept(connection, token, email="outro@example.com")

    assert (member.kind, member.email, member.expires_at) == ("member", "membro@example.com", None)
    [invite] = list_invites(connection)
    assert (invite.state, invite.used_by_email, invite.note) == ("used", "membro@example.com", "para o Joao")


def _invented(connection, token: str) -> str:
    return "x" * len(token)


def _expired(connection, token: str) -> str:
    connection.execute("UPDATE invites SET expires_at = ?", (now(),))
    return token


def _revoked(connection, token: str) -> str:
    connection.execute("DELETE FROM invites")
    return token


@pytest.mark.parametrize(
    ("name", "spoil"),
    [("token inventado", _invented), ("convite vencido", _expired), ("convite revogado", _revoked)],
)
def test_refuses_every_dead_invite_the_same_way(connection, owner, name, spoil):
    token, _ = create_invite(connection, owner)

    with pytest.raises(InviteRefused):
        _accept(connection, spoil(connection, token))

    assert connection.execute("SELECT count(*) FROM users WHERE kind = 'member'").fetchone()[0] == 0, name


def test_refuses_an_email_that_already_has_an_account_and_keeps_the_invite(connection, owner):
    token, _ = create_invite(connection, owner)

    with pytest.raises(EmailTaken):
        _accept(connection, token, email=" Dono@Example.com ")

    assert list_invites(connection)[0].state == "open"


def test_promotes_the_tester_in_place_so_the_area_and_the_jobs_stay(connection, owner):
    tester = create_user(connection, kind="tester", expires_at=now())
    connection.execute(
        "INSERT INTO jobs (id, user_id, series, chapter, engine, state, created_at)"
        " VALUES ('j', ?, 'obra', '001', 'free', 'done', ?)",
        (tester.id, now()),
    )
    token, _ = create_invite(connection, owner)

    member = _accept(connection, token, tester=tester)

    assert member.id == tester.id
    assert get_user(connection, tester.id).kind == "member"
    assert connection.execute("SELECT user_id FROM jobs").fetchone()[0] == tester.id


def test_revokes_only_an_invite_nobody_used(connection, owner):
    used_token, used = create_invite(connection, owner)
    _, unused = create_invite(connection, owner)
    _accept(connection, used_token)

    assert revoke_invite(connection, unused.id) is True
    assert revoke_invite(connection, used.id) is False
    assert [invite.id for invite in list_invites(connection)] == [used.id]


@pytest.mark.parametrize(
    ("site_address", "expected"),
    [
        ("", "http://localhost:8000"),
        (":80", "http://localhost:8000"),
        ("tinta.example.com", "https://tinta.example.com"),
        ("http://127.0.0.1:18080", "http://127.0.0.1:18080"),
    ],
)
def test_builds_the_link_from_the_address_caddy_serves(monkeypatch, site_address, expected):
    monkeypatch.setenv("SITE_ADDRESS", site_address)

    assert public_origin() == expected
