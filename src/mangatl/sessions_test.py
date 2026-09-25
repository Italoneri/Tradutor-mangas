from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from .accounts import create_user, ensure_owner
from .config import Config
from .db import connect, migrate, now
from .panel import make_panel_handler
from .panel_test import OWNER_EMAIL, OWNER_PASSWORD, Client
from .serving import _Server
from .sessions import (
    COOKIE_NAME,
    LOGIN_ATTEMPT_LIMIT,
    OWNER_SESSION_HOURS,
    TESTER_SESSION_HOURS,
    cookie_header,
    csrf_is_valid,
    hash_password,
    hours_for,
    issue,
    looks_like_email,
    origin_matches,
    purge_expired_sessions,
    resolve,
    revoke,
    token_from_cookies,
    token_hash,
    verify_password,
)


@pytest.fixture
def cfg(tmp_path):
    migrate(Config(root=tmp_path))
    return Config(root=tmp_path)


# ---------- senha ----------


def test_accepts_the_password_it_hashed():
    encoded = hash_password("uma senha qualquer")

    assert verify_password("uma senha qualquer", encoded)
    assert not verify_password("uma senha qualquex", encoded)


def test_salts_each_hash_separately():
    """Sal por usuario: duas contas com a mesma senha nao podem ter o mesmo hash,
    senao uma tabela pronta quebra as duas de uma vez."""
    assert hash_password("igual") != hash_password("igual")


@pytest.mark.parametrize(
    "encoded", [None, "", "lixo", "scrypt$x$8$1$aa$bb", "bcrypt$1$2$3$aa$bb", "scrypt$1$2$3"]
)
def test_answers_false_for_a_hash_it_cannot_read(encoded: str | None):
    """Nunca excecao: hash ilegivel e senha errada, e nao erro de servidor."""
    assert verify_password("qualquer", encoded) is False


# ---------- token e cookie ----------


def test_stores_the_hash_and_never_the_token(cfg):
    with connect(cfg) as connection:
        user = create_user(connection, kind="tester")
        token, _ = issue(connection, user.id, hours=TESTER_SESSION_HOURS)
        rows = connection.execute("SELECT token_hash FROM sessions").fetchall()

    assert rows[0]["token_hash"] == token_hash(token)
    assert token not in rows[0]["token_hash"]


def test_resolves_a_live_session_and_refuses_a_forged_one(cfg):
    with connect(cfg) as connection:
        user = create_user(connection, kind="tester")
        token, _ = issue(connection, user.id, hours=TESTER_SESSION_HOURS)

        assert resolve(connection, token).user.id == user.id
        assert resolve(connection, token + "a") is None
        assert resolve(connection, None) is None


def test_forgets_a_session_that_was_revoked(cfg):
    with connect(cfg) as connection:
        user = create_user(connection, kind="tester")
        token, _ = issue(connection, user.id, hours=TESTER_SESSION_HOURS)

        revoke(connection, token)

        assert resolve(connection, token) is None


def test_refuses_a_session_whose_time_is_up(cfg):
    with connect(cfg) as connection:
        user = create_user(connection, kind="tester")
        token, _ = issue(connection, user.id, hours=TESTER_SESSION_HOURS)
        connection.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'")

        assert resolve(connection, token) is None
        assert purge_expired_sessions(connection) == 1


def test_gives_the_tester_a_shorter_life_than_the_owner():
    assert hours_for("tester") == TESTER_SESSION_HOURS
    assert hours_for("owner") == OWNER_SESSION_HOURS
    assert hours_for("tester") < hours_for("owner")


def test_marks_the_cookie_http_only_and_same_site(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "1")
    header = cookie_header("abc", hours=48)

    assert header.startswith(f"{COOKIE_NAME}=abc")
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header
    assert "Secure" in header
    assert "Path=/" in header


def test_drops_secure_only_when_asked(monkeypatch):
    """Cookie `Secure` nunca volta por `http://`, e ai a sessao pareceria nao
    existir no desenvolvimento local."""
    monkeypatch.setenv("COOKIE_SECURE", "0")

    assert "Secure" not in cookie_header("abc", hours=48)


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("cookie normal", f"{COOKIE_NAME}=abc123", "abc123"),
        ("entre outros", f"outro=1; {COOKIE_NAME}=abc123; mais=2", "abc123"),
        ("sem o nosso", "outro=1", None),
        ("vazio", "", None),
        ("ausente", None, None),
        ("valor vazio", f"{COOKIE_NAME}=", None),
        ("malformado nao derruba", "=;;;=x", None),
    ],
)
def test_reads_the_token_from_the_cookie_header(name: str, raw: str | None, expected: str | None):
    assert token_from_cookies(raw) == expected, name


# ---------- CSRF ----------


@pytest.mark.parametrize(
    ("name", "origin", "host", "expected"),
    [
        ("mesma origem", "https://tinta.example", "tinta.example", True),
        ("com porta", "http://127.0.0.1:8000", "127.0.0.1:8000", True),
        ("outro site", "https://atacante.example", "tinta.example", False),
        ("sufixo parecido", "https://tinta.example.evil.com", "tinta.example", False),
        ("origem nula", "null", "tinta.example", False),
        ("sem origem", None, "tinta.example", False),
        ("sem host", "https://tinta.example", None, False),
    ],
)
def test_compares_the_origin_with_the_host(name: str, origin, host, expected: bool):
    assert origin_matches(origin, host) is expected, name


def test_demands_the_header_and_the_origin_together():
    same = {"X-Requested-With": "fetch", "Origin": "https://t.example", "Host": "t.example"}

    assert csrf_is_valid(same)
    assert not csrf_is_valid({**same, "X-Requested-With": None})
    assert not csrf_is_valid({**same, "Origin": "https://outro.example"})
    # `Referer` cobre o cliente que nao manda `Origin`.
    assert csrf_is_valid({**same, "Origin": None, "Referer": "https://t.example/reader/"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dono@example.com", True),
        ("  dono@example.com ", True),
        ("dono@example", False),
        ("dono", False),
        ("@example.com", False),
        ("a@b.c" + "x" * 300, False),
    ],
)
def test_recognises_something_shaped_like_an_email(value: str, expected: bool):
    assert looks_like_email(value) is expected


# ---------- login pelo servidor de verdade ----------


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    """Com a vitrine ligada: e so nela que a primeira escrita sem sessao abre um
    testador. Desligada, a mesma escrita leva 401 - e o que a Fase 7.1 corrigiu."""
    monkeypatch.setenv("PUBLIC_SHOWCASE", "1")
    (tmp_path / "reader").mkdir()
    cfg = Config(root=tmp_path)
    migrate(cfg)
    with connect(cfg) as connection:
        ensure_owner(connection, OWNER_EMAIL, OWNER_PASSWORD)

    with _Server(("127.0.0.1", 0), make_panel_handler(cfg)) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield Client(httpd.server_address[1])
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def test_opens_a_session_for_the_right_password(server: Client):
    status, body, headers = server.raw(
        "POST",
        "/api/login",
        json.dumps({"email": OWNER_EMAIL, "password": OWNER_PASSWORD}).encode("utf-8"),
    )

    assert status == 200
    assert json.loads(body)["kind"] == "owner"
    assert headers["set-cookie"].startswith(f"{COOKIE_NAME}=")


def test_answers_the_same_for_a_wrong_password_and_an_unknown_email(server: Client):
    """Distinguir as duas entrega uma lista de quem tem conta aqui, de graca."""
    wrong = server.send(
        "POST",
        "/api/login",
        json.dumps({"email": OWNER_EMAIL, "password": "errada"}).encode("utf-8"),
    )
    unknown = server.send(
        "POST",
        "/api/login",
        json.dumps({"email": "ninguem@example.com", "password": OWNER_PASSWORD}).encode("utf-8"),
    )

    assert wrong == unknown
    assert wrong[0] == 401


def test_answers_429_after_five_wrong_attempts(server: Client):
    payload = json.dumps({"email": OWNER_EMAIL, "password": "errada"}).encode("utf-8")
    codes = [server.send("POST", "/api/login", payload)[0] for _ in range(LOGIN_ATTEMPT_LIMIT + 1)]

    assert codes[:LOGIN_ATTEMPT_LIMIT] == [401] * LOGIN_ATTEMPT_LIMIT
    assert codes[LOGIN_ATTEMPT_LIMIT] == 429


def test_locks_out_even_the_right_password_once_throttled(server: Client):
    """Senao o limite so atrasa quem varre, e nao para nada."""
    wrong = json.dumps({"email": OWNER_EMAIL, "password": "errada"}).encode("utf-8")
    for _ in range(LOGIN_ATTEMPT_LIMIT):
        server.send("POST", "/api/login", wrong)

    assert server.login() == 429


def test_clears_the_counter_when_the_password_is_right(server: Client):
    wrong = json.dumps({"email": OWNER_EMAIL, "password": "errada"}).encode("utf-8")
    for _ in range(LOGIN_ATTEMPT_LIMIT - 1):
        server.send("POST", "/api/login", wrong)

    assert server.login() == 200

    # De volta a zero: quem errou quatro vezes e acertou nao pode ficar a um erro
    # do bloqueio pelos quinze minutos seguintes.
    codes = [server.send("POST", "/api/login", wrong)[0] for _ in range(LOGIN_ATTEMPT_LIMIT)]
    assert codes == [401] * LOGIN_ATTEMPT_LIMIT


def test_ends_the_session_in_the_database_and_not_only_in_the_browser(server: Client, tmp_path):
    assert server.login() == 200
    stolen = server.cookie

    server.send("POST", "/api/logout")

    assert server.get("/u/library", cookie=stolen)[0] == 401


def test_opens_the_anonymous_session_only_when_something_is_written(server: Client):
    _, _, read = server.raw("GET", "/api/series")
    assert "set-cookie" not in read

    _, _, written = server.raw(
        "POST", "/api/series", json.dumps({"slug": "Minha"}).encode("utf-8")
    )
    assert written["set-cookie"].startswith(f"{COOKIE_NAME}=")


def test_gives_the_anonymous_session_an_expiry(server: Client, tmp_path: Path):
    server.send("POST", "/api/series", json.dumps({"slug": "Minha"}).encode("utf-8"))

    with connect(Config(root=tmp_path)) as connection:
        row = connection.execute(
            "SELECT expires_at FROM users WHERE kind = 'tester'"
        ).fetchone()

    assert row is not None
    assert row["expires_at"] > now()


def test_hands_over_the_new_session_even_when_the_write_is_refused(server: Client, tmp_path: Path):
    """O cookie sai junto com o 422, e nao so com o 201.

    A sessao anonima nasce antes do handler rodar. Se o cookie so viajasse na
    resposta boa, toda escrita recusada deixaria a linha no banco sem ninguem
    para usa-la, e a tentativa seguinte abriria outra - quem erra em laco viraria
    uma fabrica de sessoes orfas.
    """
    status, _, headers = server.raw(
        "POST", "/api/series", json.dumps({"slug": "../fora"}).encode("utf-8")
    )

    assert status == 422
    assert headers["set-cookie"].startswith(f"{COOKIE_NAME}=")


def test_reuses_that_session_instead_of_opening_another(server: Client, tmp_path: Path):
    server.send("POST", "/api/series", json.dumps({"slug": "../fora"}).encode("utf-8"))
    server.send("POST", "/api/series", json.dumps({"slug": "../fora"}).encode("utf-8"))
    server.send("POST", "/api/series", json.dumps({"slug": "Minha"}).encode("utf-8"))

    with connect(Config(root=tmp_path)) as connection:
        testers = connection.execute("SELECT COUNT(*) AS n FROM users WHERE kind = 'tester'")
        assert testers.fetchone()["n"] == 1


def test_says_the_instance_is_private_by_default(server: Client, monkeypatch):
    monkeypatch.delenv("PUBLIC_SHOWCASE")

    _, body, _ = server.raw("GET", "/api/session")

    assert json.loads(body)["showcase"] is False


def test_says_the_instance_shows_a_showcase_when_asked(server: Client, monkeypatch):
    """A vitrine e opcional e desligada por padrao.

    Ligada, ela responde a quem nao tem conta. Desligada, a mesma falta de sessao
    vira a tela de entrar - que e o que a instalacao de uma pessoa so quer, e o
    que faltava para o dono achar o proprio painel.
    """
    monkeypatch.setenv("PUBLIC_SHOWCASE", "1")

    _, body, _ = server.raw("GET", "/api/session")

    assert json.loads(body)["showcase"] is True
