from __future__ import annotations

import socketserver
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from .serving import is_servable, make_handler


@pytest.mark.parametrize(
    ("name", "path", "expected"),
    [
        ("nega o .env com a chave da api", "/.env", False),
        ("nega o .env percent-encoded", "/%2Eenv", False),
        ("nega a config do git", "/.git/config", False),
        ("nega fuga para fora da raiz", "/../../etc/passwd", False),
        ("nega fuga passando por uma pasta servivel", "/reader/../.env", False),
        ("nega fuga com barra invertida", "/reader/..%5C.env", False),
        ("nega pasta fora da lista", "/src/mangatl/cli.py", False),
        ("nega a raiz, que lista o .env", "/", False),
        ("aceita a pwa", "/reader/app.js", True),
        ("aceita o indice do leitor", "/reader/", True),
        ("aceita a vitrine publica", "/public/demo/obra/001/p0001.jpg", True),
        # Os dois saiam da lista quando o servidor passou a ter contas: acervo e de
        # alguem, e lista de pasta nao sabe responder "de quem". Quem responde sao
        # as rotas `/u/` do painel, que conferem a sessao antes de mandar um byte.
        ("nega a biblioteca por caminho", "/output/library.json", False),
        ("nega a imagem da pagina por caminho", "/library/manhwa/001/p0001.jpg", False),
        ("ignora a query string", "/reader/app.js?v=3", True),
    ],
)
def test_decides_what_leaves_the_machine(name: str, path: str, expected: bool):
    assert is_servable(path) is expected, name


@pytest.fixture
def reader_server(tmp_path: Path):
    """Sobe o handler real numa porta efemera, para provar a ligacao e nao so o filtro."""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-secreta\n", encoding="utf-8")
    (tmp_path / "reader").mkdir()
    (tmp_path / "reader" / "app.js").write_text("export const ok = 1;\n", encoding="utf-8")

    with socketserver.TCPServer(("127.0.0.1", 0), make_handler(tmp_path)) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd.server_address[1]
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize(
    ("name", "path", "expected"),
    [
        ("404 na chave da api", "/.env", 404),
        ("404 na listagem de pasta fora da lista", "/src/", 404),
        ("301 da raiz para o leitor, sem nunca lista-la", "/", 301),
        ("200 na pwa", "/reader/app.js", 200),
    ],
)
def test_serves_only_the_reader(reader_server: int, name: str, path: str, expected: int):
    connection = HTTPConnection("127.0.0.1", reader_server, timeout=5)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    connection.close()

    assert response.status == expected, name
    assert b"sk-secreta" not in body, name
