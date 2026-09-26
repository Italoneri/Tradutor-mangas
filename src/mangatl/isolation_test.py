"""A bateria da Fase 3.7 do plano, escrita como teste em vez de roteiro manual.

Sete checagens, e nenhuma pode falhar. Sao elas que dizem se o servidor guarda o
acervo de cada um ou se ele virou, sem ninguem decidir isso, um serviço de
distribuicao de capitulo.

Rodam contra o servidor de verdade, numa porta efemera, com cliente HTTP de
verdade - o que este arquivo precisa provar e a ligacao inteira, e nao que as
funcoes de caminho devolvem None.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from .accounts import ensure_owner, sole_owner
from .config import Config
from .db import connect, migrate
from .panel import make_panel_handler
from .panel_test import OWNER_EMAIL, OWNER_PASSWORD, Client
from .serving import _Server

JPEG = bytes.fromhex("ffd8ffe0") + b"0" * 32


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    """Servidor com o dono ja criado e um capitulo no acervo dele.

    Com a vitrine ligada: e so nela que a primeira escrita sem sessao abre um
    testador. Desligada, a mesma escrita leva 401 - e o que a Fase 7.1 corrigiu."""
    monkeypatch.setenv("PUBLIC_SHOWCASE", "1")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-secreta\n", encoding="utf-8")
    (tmp_path / "reader").mkdir()
    (tmp_path / "reader" / "app.js").write_text("export const ok = 1;\n", encoding="utf-8")

    cfg = Config(root=tmp_path)
    migrate(cfg)
    with connect(cfg) as connection:
        ensure_owner(connection, OWNER_EMAIL, OWNER_PASSWORD)
        owner_id = sole_owner(connection).id

    chapter = tmp_path / "data" / "users" / owner_id / "library" / "Segredo" / "001"
    chapter.mkdir(parents=True)
    (chapter / "p0001.jpg").write_bytes(JPEG)
    translation = tmp_path / "data" / "users" / owner_id / "output" / "Segredo" / "001"
    translation.mkdir(parents=True)
    (translation / "chapter.free.json").write_text(
        json.dumps(
            {
                "series": "Segredo",
                "chapter": "001",
                "engine": "free",
                "pipeline_version": 4,
                "created_at": "2026-01-01T00:00:00+00:00",
                "pages": [
                    {"index": 1, "image": "p0001.jpg", "width": 800, "height": 1200, "blocks": []}
                ],
            }
        ),
        encoding="utf-8",
    )

    with _Server(("127.0.0.1", 0), make_panel_handler(cfg)) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield Client(httpd.server_address[1])
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def _tester(server: Client) -> Client:
    """Um cliente com sessao de testador, nascida como nasce de verdade: no upload."""
    fresh = Client(server.port)
    status, _ = fresh.send("POST", "/api/series", json.dumps({"slug": "Minha"}).encode("utf-8"))
    assert status == 201
    assert fresh.cookie, "o primeiro upload tinha que ter aberto a sessao"
    return fresh


# ---------- 1. o acervo de um nao sai para o outro ----------


def test_answers_404_for_a_page_that_belongs_to_someone_else(server: Client):
    tester = _tester(server)

    status, body = tester.get("/u/pages/Segredo/001/p0001.jpg")

    # 404 e nao 403: 403 confirmaria que o capitulo existe, e a diferenca entre as
    # duas respostas e um indice do acervo alheio para quem souber varrer nomes.
    assert status == 404
    assert JPEG not in body


def test_answers_404_for_a_translation_that_belongs_to_someone_else(server: Client):
    tester = _tester(server)

    assert tester.get("/u/chapters/Segredo/001/chapter.free.json")[0] == 404


def test_never_lists_someone_elses_library(server: Client):
    tester = _tester(server)

    status, body = tester.get("/u/library")

    assert status == 200
    assert b"Segredo" not in body


def test_serves_the_owner_their_own_page(server: Client):
    """O outro lado da mesma moeda: isolar nao pode virar nao servir."""
    assert server.login() == 200

    status, body = server.get("/u/pages/Segredo/001/p0001.jpg")

    assert status == 200
    assert body == JPEG


# ---------- 2. caminho direto nao responde por acervo ----------


@pytest.mark.parametrize(
    "path",
    [
        "/library/Segredo/001/p0001.jpg",
        "/output/Segredo/001/chapter.free.json",
        "/output/library.json",
        "/data/mangatl.db",
    ],
)
def test_answers_404_for_content_asked_by_path(server: Client, path: str):
    assert server.login() == 200

    assert server.get(path)[0] == 404, path


# ---------- 3. fuga de caminho, nas tres formas ----------


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("cru", "/u/pages/../../../.env"),
        ("codificado", "/u/pages/%2e%2e/%2e%2e/%2e%2e/.env"),
        ("codificado duas vezes", "/u/pages/%252e%252e/%252e%252e/.env"),
        ("com barra invertida", "/u/pages/..%5C..%5C.env"),
        ("no ultimo segmento", "/u/pages/Segredo/001/..%2F..%2F..%2F.env"),
    ],
)
def test_answers_404_for_a_path_that_tries_to_leave_the_area(server: Client, name: str, path: str):
    assert server.login() == 200

    status, body = server.get(path)

    assert status == 404, name
    assert b"sk-secreta" not in body, name


# ---------- 4 e 5. sem sessao, e com sessao vencida ----------


@pytest.mark.parametrize(
    "path",
    ["/u/library", "/u/pages/Segredo/001/p0001.jpg", "/u/chapters/Segredo/001/chapter.free.json"],
)
def test_answers_401_without_a_session(server: Client, path: str):
    server.forget()

    assert server.get(path)[0] == 401, path


def test_never_opens_a_session_on_a_read(server: Client):
    """A home e publica: robo de busca nao pode ganhar area em disco por passar."""
    server.forget()

    _, _, headers = server.raw("GET", "/u/library")

    assert "set-cookie" not in headers


def test_answers_401_for_a_forged_cookie(server: Client):
    assert server.get("/u/library", cookie="sid=inventado-por-mim")[0] == 401


def test_answers_401_for_an_expired_session(server: Client, tmp_path: Path):
    assert server.login() == 200
    assert server.get("/u/library")[0] == 200

    with connect(Config(root=tmp_path)) as connection:
        connection.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'")

    assert server.get("/u/library")[0] == 401


# ---------- 6. o testador nunca alcanca o motor pago ----------


def test_refuses_the_paid_engine_for_a_tester(server: Client):
    tester = _tester(server)
    tester.send("POST", "/api/series/Minha/chapters", json.dumps({"chapter": "001"}).encode())
    tester.send("PUT", "/api/series/Minha/chapters/001/files/1.jpg", JPEG)
    tester.send("POST", "/api/series/Minha/chapters/001/commit")

    payload = json.dumps({"series": "Minha", "chapter": "001", "engine": "claude"})
    status, body = tester.send("POST", "/api/jobs", payload.encode("utf-8"))

    # 429 e nao 403: e cota, e a mensagem diz qual motor sobra.
    assert status == 429
    assert b"free" in body


def test_never_offers_the_paid_engine_to_a_tester(server: Client):
    tester = _tester(server)

    _, body = tester.get("/api/session")

    assert json.loads(body)["engines"] == ["free"]


# ---------- 7. rota de dono recusa testador ----------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/series/Minha/series.json"),
        ("PUT", "/api/series/Minha/glossary"),
        ("PUT", "/api/series/Minha/cover"),
        ("DELETE", "/api/series/Minha"),
    ],
)
def test_answers_403_for_an_owner_route_asked_by_a_tester(server: Client, method: str, path: str):
    tester = _tester(server)

    status, body = tester.send(method, path, b'{"title": "x"}')

    assert status == 403, path
    assert b"dono" in body


# ---------- CSRF ----------


def test_refuses_a_write_without_the_requested_with_header(server: Client):
    assert server.login() == 200

    status, _ = server.send(
        "POST", "/api/series", json.dumps({"slug": "Obra"}).encode("utf-8"), csrf=False
    )

    assert status == 403


def test_refuses_a_write_whose_origin_is_another_site(server: Client):
    assert server.login() == 200

    status, _, _ = server.raw(
        "POST",
        "/api/series",
        json.dumps({"slug": "Obra"}).encode("utf-8"),
        csrf=False,
        headers={"X-Requested-With": "fetch", "Origin": "https://site-do-atacante.example"},
    )

    assert status == 403


# ---------- 7.1 a vitrine desligada vale na API ----------


def _user_count(root: Path) -> int:
    with connect(Config(root=root)) as connection:
        return connection.execute("SELECT count(*) AS n FROM users").fetchone()["n"]


@pytest.mark.parametrize(
    ("showcase", "status", "users_created"),
    [
        ("0", 401, 0),
        ("1", 201, 1),
    ],
)
def test_opens_a_tester_session_only_when_the_showcase_is_public(
    server: Client,
    tmp_path: Path,
    monkeypatch,
    showcase: str,
    status: int,
    users_created: int,
):
    """A flag decidia a tela e nao a API: sem vitrine, a escrita anonima ainda criava
    usuario, gravava 40MB e enfileirava job. Agora a instalacao de uma pessoa so
    recusa do mesmo jeito que recusa a leitura."""
    monkeypatch.setenv("PUBLIC_SHOWCASE", showcase)
    before = _user_count(tmp_path)

    anonymous = Client(server.port)
    got, _ = anonymous.send("POST", "/api/series", json.dumps({"slug": "Minha"}).encode("utf-8"))

    assert got == status
    assert _user_count(tmp_path) - before == users_created
    assert (anonymous.cookie is not None) == bool(users_created)


@pytest.mark.parametrize(
    ("showcase", "path", "status"),
    [
        ("0", "/public/demo/demo-library.json", 404),
        ("0", "/public/%64emo/demo-library.json", 404),
        ("0", "/public/Demo/demo-library.json", 404),
        ("1", "/public/demo/demo-library.json", 200),
    ],
)
def test_serves_the_showcase_files_only_when_the_showcase_is_public(
    server: Client, tmp_path: Path, monkeypatch, showcase: str, path: str, status: int
):
    """A flag escondia a vitrine da tela e deixava os arquivos saindo por URL: um
    `public/demo/` gerado na maquina do dono viaja dentro da imagem, e o indice dele
    lista a obra para quem pedir o caminho direto."""
    demo = tmp_path / "public" / "demo"
    demo.mkdir(parents=True)
    (demo / "demo-library.json").write_text('{"series": []}', encoding="utf-8")
    monkeypatch.setenv("PUBLIC_SHOWCASE", showcase)

    got, _ = Client(server.port).get(path)

    assert got == status


# ---------- 7.7 apagar alcanca so a propria area ----------


def _tester_chapter(tester: Client, series: str, chapter: str) -> None:
    """Sobe e promove um capitulo de uma pagina na area do testador."""
    body = json.dumps({"slug": series}).encode("utf-8")
    assert tester.send("POST", "/api/series", body)[0] in {201, 409}
    base = f"/api/series/{series}/chapters"
    assert tester.send("POST", base, json.dumps({"chapter": chapter}).encode())[0] == 201
    assert tester.send("PUT", f"{base}/{chapter}/files/p0001.jpg", JPEG)[0] == 200
    assert tester.send("POST", f"{base}/{chapter}/commit")[0] == 200


def test_deletes_only_the_testers_chapter_with_the_owners_name(server: Client):
    tester = _tester(server)
    _tester_chapter(tester, "Segredo", "001")

    status, _ = tester.send("DELETE", "/api/series/Segredo/chapters/001")

    assert status == 200
    assert tester.get("/u/library")[1].count(b'"001"') == 0
    assert server.login() == 200
    assert server.get("/u/pages/Segredo/001/p0001.jpg") == (200, JPEG)


@pytest.mark.parametrize(
    "path",
    [
        "/api/series/Segredo/chapters/001",
        "/api/series/Minha/chapters/999",
        "/api/series/Nenhuma/chapters/001",
    ],
)
def test_answers_404_for_deleting_a_chapter_outside_the_own_area(server: Client, path: str):
    tester = _tester(server)

    assert tester.send("DELETE", path)[0] == 404


def test_lets_a_tester_discard_their_own_incoming_area(server: Client):
    tester = _tester(server)
    base = "/api/series/Minha/chapters"
    assert tester.send("POST", base, json.dumps({"chapter": "001"}).encode())[0] == 201
    assert tester.send("PUT", f"{base}/001/files/p0001.jpg", JPEG)[0] == 200

    assert tester.send("DELETE", f"{base}/001/incoming")[0] == 200
    assert json.loads(tester.get(f"{base}/001/incoming")[1])["exists"] is False


def test_refuses_to_delete_a_chapter_while_it_is_in_the_queue(server: Client):
    tester = _tester(server)
    _tester_chapter(tester, "Minha", "001")
    job = json.dumps({"series": "Minha", "chapter": "001", "engine": "free"}).encode()
    assert tester.send("POST", "/api/jobs", job)[0] == 202

    assert tester.send("DELETE", "/api/series/Minha/chapters/001")[0] == 409


def test_lets_the_owner_delete_a_whole_series(server: Client):
    assert server.login() == 200

    assert server.send("DELETE", "/api/series/Segredo")[0] == 200
    assert server.get("/u/pages/Segredo/001/p0001.jpg")[0] == 404
    assert server.get("/u/chapters/Segredo/001/chapter.free.json")[0] == 404
