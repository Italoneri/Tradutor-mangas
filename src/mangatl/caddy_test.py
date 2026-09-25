"""Os casos 8, 9 e 10 da bateria da Fase 3.7 - os que só existem com o proxy.

Os sete primeiros casos batem no app Python e rodam contra o handler direto, como
`isolation_test.py` faz. Estes três não: o buraco que eles vigiam mora no
`Caddyfile`, e teste que não cruza a camada que falha não protege nada.

O buraco foi real. O `Caddyfile` teve um `handle_path /_internal/*` de primeiro
nível - que parece o `internal` do nginx e não é, porque `internal` não existe no
Caddy - servindo a raiz do projeto para a internet. Com `data/` montado no
contêiner do proxy, `GET /_internal/data/mangatl.db` entregava o banco inteiro, e
com os `user_id` de dentro dele, o acervo de qualquer conta. O `pytest` passava
inteiro enquanto isso era verdade, porque o perfil `public` não sobe por padrão e
o proxy nem estava de pé.

Rodar:

    SITE_ADDRESS=":80" CADDY_HTTP_PORT=18080 ACME_EMAIL=a@b.com \\
        docker compose --profile public up -d
    MANGATL_PROXY_URL=http://127.0.0.1:18080 \\
    MANGATL_PROXY_EMAIL=... MANGATL_PROXY_PASSWORD=... \\
        docker compose run --rm dev python -m pytest src/mangatl/caddy_test.py

Sem `MANGATL_PROXY_URL` o módulo inteiro é pulado. É a concessão necessária para
o `pytest` continuar rodando sem Docker - e é também por isso que o plano manda
repetir estes três casos contra o domínio público: um `skip` tem a mesma cara
verde de um `pass`.
"""

from __future__ import annotations

import hashlib
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

PROXY_URL = os.environ.get("MANGATL_PROXY_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(
    not PROXY_URL,
    reason=(
        "precisa do proxy de pe: suba `docker compose --profile public up -d` e"
        " passe MANGATL_PROXY_URL. Sem isto os casos 8, 9 e 10 nao rodam, e o"
        " buraco que eles vigiam vive no Caddyfile, fora do alcance do pytest."
    ),
)

# Os caminhos que o `X-Accel-Redirect` usa. Nenhum deles pode responder de fora:
# o prefixo existe para o proxy consumir no meio da resposta, e nunca como rota.
INTERNAL_PATHS = [
    "/_internal/data/mangatl.db",
    "/_internal/data/users/",
    "/_internal/data/",
    "/_internal/",
    "/_internal/.env",
    "/_internal/public/README.md",
]

# Os mesmos alvos por caminho cru, que o `is_servable` deixou de cobrir quando a
# autorizacao passou a ser por rota. Aqui eles atravessam o proxy tambem.
RAW_PATHS = [
    "/data/mangatl.db",
    "/library/x/001/p0001.jpg",
    "/output/library.json",
    "/.env",
]


def fetch(path: str, *, cookie: str | None = None) -> tuple[int, bytes, dict[str, str]]:
    """Pedido cru ao proxy. Erro HTTP vira resposta, e nao excecao.

    `urlopen` levanta em 4xx, e um 404 aqui e o resultado esperado - a forma de
    afirmar sobre ele e captura-lo.
    """
    request = Request(f"{PROXY_URL}{path}")
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urlopen(request, timeout=30) as response:
            headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response.read(), headers
    except HTTPError as error:
        headers = {key.lower(): value for key, value in error.headers.items()}
        return error.code, error.read(), headers


@pytest.fixture(scope="module")
def cookie() -> str:
    """Sessao de dono pelo proxy, para o caso 10."""
    email = os.environ.get("MANGATL_PROXY_EMAIL", "")
    password = os.environ.get("MANGATL_PROXY_PASSWORD", "")
    if not email or not password:
        pytest.skip("caso 10 precisa de MANGATL_PROXY_EMAIL e MANGATL_PROXY_PASSWORD")

    request = Request(
        f"{PROXY_URL}/api/login",
        data=json.dumps({"email": email, "password": password}).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("X-Requested-With", "fetch")
    request.add_header("Origin", PROXY_URL)
    with urlopen(request, timeout=30) as response:
        assert response.status == 200, "o dono nao entrou pelo proxy"
        issued = response.getheader("Set-Cookie")
    assert issued, "o login nao devolveu cookie"
    return issued.split(";", 1)[0]


# ---------- caso 8 e 9 ----------


@pytest.mark.parametrize("path", INTERNAL_PATHS)
def test_refuses_the_internal_prefix_from_outside(path: str):
    """Caso 8 e 9. O prefixo do `X-Accel-Redirect` nao e rota publica.

    404 e nao 403: 403 confirmaria que ha algo ali.
    """
    status, body, _ = fetch(path)

    assert status == 404, f"{path} respondeu {status}"
    assert b"SQLite" not in body, "o banco saiu pela resposta"


def test_never_serves_the_database_through_the_proxy():
    """O alvo que motivou esta bateria, afirmado sozinho.

    O banco tem o e-mail do dono, o hash da senha dele, todos os `user_id` e os
    hashes de sessao. Com o repositorio publico, o caminho nao e adivinhacao - e
    documentacao.
    """
    status, body, _ = fetch("/_internal/data/mangatl.db")

    assert status == 404
    assert len(body) < 1024, "veio corpo grande demais para um 404"


@pytest.mark.parametrize("path", RAW_PATHS)
def test_refuses_the_raw_content_paths_through_the_proxy(path: str):
    status, _, _ = fetch(path)

    assert status == 404, f"{path} respondeu {status}"


def test_still_refuses_a_session_route_without_a_cookie():
    """O contraponto que prova que o proxy encaminha de verdade.

    Sem ele, um Caddy que respondesse 404 para tudo passaria nos casos acima sem
    nunca falar com o app - foi o que aconteceu na primeira tentativa de rodar
    esta bateria, contra uma porta que outro processo ja ocupava.
    """
    status, _, _ = fetch("/u/library")

    assert status == 401


# ---------- caso 10 ----------


def test_delivers_the_whole_chapter_through_the_proxy(cookie: str):
    """Caso 10. A correcao mexeu no caminho de entrega; isto e a regressao.

    Confere as duas metades: que toda imagem do capitulo chega, e que quem a
    transmitiu foi o proxy. O `Etag` e a prova barata disso - `http.server` nao
    emite nenhum, e o `file_server` do Caddy emite sempre.
    """
    status, body, _ = fetch("/u/library", cookie=cookie)
    assert status == 200
    library = json.loads(body)

    series = next((item for item in library["series"] if item["chapters"]), None)
    if series is None:
        pytest.skip("a conta nao tem capitulo traduzido para conferir")
    chapter = series["chapters"][0]

    status, meta, _ = fetch(
        f"/u/chapters/{series['series']}/{chapter['chapter']}"
        f"/chapter.{chapter['engines'][0]}.json",
        cookie=cookie,
    )
    assert status == 200
    pages = json.loads(meta)["pages"]
    assert len(pages) == chapter["page_count"]

    for page in pages:
        path = f"/u/pages/{series['series']}/{chapter['chapter']}/{page['image']}"
        status, image, headers = fetch(path, cookie=cookie)

        assert status == 200, f"{path} respondeu {status}"
        assert len(image) > 0, f"{path} chegou vazio"
        assert headers.get("etag"), f"{path} veio sem Etag - quem transmitiu foi o Python"
        assert "x-accel-redirect" not in headers, "o cabecalho interno vazou para o cliente"


def test_hands_the_bytes_to_the_proxy_and_not_to_python(cookie: str):
    """O app responde vazio; o corpo cheio so existe do outro lado do proxy.

    Se um dia o `handle_response` parar de casar, isto falha aqui em vez de virar
    um servidor lento sem ninguem entender por que.
    """
    direct = os.environ.get("MANGATL_APP_URL", "").rstrip("/")
    if not direct:
        pytest.skip("precisa de MANGATL_APP_URL apontando para o app sem proxy")

    status, body, _ = fetch("/u/library", cookie=cookie)
    assert status == 200
    library = json.loads(body)
    series = next((item for item in library["series"] if item["chapters"]), None)
    if series is None:
        pytest.skip("a conta nao tem capitulo traduzido para conferir")

    chapter = series["chapters"][0]
    status, meta, _ = fetch(
        f"/u/chapters/{series['series']}/{chapter['chapter']}"
        f"/chapter.{chapter['engines'][0]}.json",
        cookie=cookie,
    )
    assert status == 200
    page = json.loads(meta)["pages"][0]
    path = f"/u/pages/{series['series']}/{chapter['chapter']}/{page['image']}"

    request = Request(f"{direct}{path}")
    request.add_header("Cookie", cookie)
    with urlopen(request, timeout=30) as response:
        from_app = response.read()
        redirect = response.getheader("X-Accel-Redirect")

    _, from_proxy, _ = fetch(path, cookie=cookie)

    assert redirect, "o app nao pediu a entrega ao proxy"
    assert redirect.startswith("/_internal/"), redirect
    assert from_app == b"", "o Python transmitiu os bytes que o proxy deveria transmitir"
    assert len(from_proxy) > 0
    assert hashlib.sha256(from_proxy).digest(), "resposta ilegivel"


# ---------- 7.2: o IP que o app ve e o que o Caddy escreveu ----------


def _login_attempt(email: str, spoofed_ip: str) -> int:
    request = Request(
        f"{PROXY_URL}/api/login",
        data=json.dumps({"email": email, "password": "errada"}).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("X-Requested-With", "fetch")
    request.add_header("Origin", PROXY_URL)
    request.add_header("X-Real-IP", spoofed_ip)
    try:
        with urlopen(request, timeout=30) as response:
            return response.status
    except HTTPError as error:
        return error.code


def test_overwrites_the_real_ip_header_the_client_sent():
    """O cliente troca o `X-Real-IP` a cada tentativa, e o limite pega assim mesmo.

    Se o Caddy repassasse o cabecalho do cliente, cada tentativa contaria para um
    IP diferente e o limite nunca fecharia. Fecha porque o `header_up` sobrescreve
    com o endereco do socket - o mesmo nas seis.

    Tranca o IP de quem roda a bateria por quinze minutos. Rode por ultimo, e
    depois `docker compose exec app mangatl reset-login`.
    """
    # Um e-mail por tentativa: com um so, a chave `email:` fecharia sozinha e o
    # teste passaria sem dizer nada sobre o IP.
    codes = [
        _login_attempt(f"bateria-{n}@example.com", f"203.0.113.{n}") for n in range(1, 7)
    ]

    assert codes[:5] == [401] * 5
    assert codes[5] == 429
