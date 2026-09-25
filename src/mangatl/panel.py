"""Painel e conteudo do usuario: quem esta pedindo decide o que sai.

A regra antiga era o endereco de origem - `/api/` so respondia para 127.0.0.1.
Ela era autenticacao disfarcada e morreu ao ser hospedada: atras de um proxy
reverso todo cliente chega com o IP do proxy, e a regra ou tranca todo mundo para
fora ou deixa todo mundo entrar como dono. No lugar dela ha papel de usuario, e
IP so vale para contar tentativa de senha.

Tres niveis de acesso, declarados rota a rota em vez de deduzidos do caminho:

    publico   vitrine e login, respondem sem sessao
    sessao    o proprio acervo: o id do usuario vem do cookie, nunca da URL
    dono      o que destroi ou custa dinheiro: apagar, editar serie, glossario

As rotas `/u/` sao o outro lado do mesmo assunto: `library/` e `output/` deixaram
de ser servidos por caminho, porque uma lista de pastas permitidas responde "esta
pasta pode sair na rede" e a pergunta virou "esta pasta pode sair para VOCE".

Este modulo depende do venv inteiro: pydantic, o store, o pipeline. O `serving.py`
continua sendo stdlib pura e continua servindo so o que e publico por natureza -
a PWA e a vitrine. Essa separacao e o que deixa obvio, lendo um arquivo curto, que
nada de acervo sai sem passar por aqui.

Ponto de extensao anotado e nao implementado: baixar capitulo de URL pediria uma
`import_from_url(url) -> list[Path]`. Qualquer site serio precisa de navegador
headless e quebra a cada mudanca de layout, entao a origem das imagens e upload.
"""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import traceback
import zipfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import IO, Literal, NamedTuple
from urllib.parse import unquote

from .accounts import User, area_config, create_user, find_owner, password_hash_of
from .config import Config
from .db import connect, in_hours
from .engines.base import available_engines
from .jobs import (
    OWNER_PRIORITY,
    TESTER_PRIORITY,
    Busy,
    enqueue,
    get as get_job,
    has_active,
    queue_position,
    recent as recent_jobs,
)
from .models import Chapter, SeriesMeta
from .quotas import (
    TESTER_MAX_PAGES_PER_CHAPTER,
    OutOfSpace,
    QuotaExceeded,
    check_disk,
    check_engine,
    check_incoming_pages,
    check_new_chapter,
    check_upload_bytes,
    engines_for,
    record_usage,
    upload_headroom,
)
from .serving import ReaderHandler, serve_handler
from .sessions import (
    REQUESTED_WITH,
    Session,
    TESTER_SESSION_HOURS,
    clear_login_attempts,
    clearing_cookie_header,
    cookie_header,
    csrf_is_valid,
    hours_for,
    issue,
    login_is_throttled,
    record_login_attempt,
    record_tester_signup,
    resolve,
    revoke,
    tester_signup_allowed,
    token_from_cookies,
    verify_password,
)
from .store import (
    COVER_STEM,
    IMAGE_SUFFIXES,
    INCOMING_SUFFIX,
    LIBRARY_FILENAME,
    _natural_key,
    build_library,
    chapter_filename,
    chapter_output_dir,
    discover_series,
    list_page_images,
    load_glossary,
    load_chapter,
    load_series_meta,
    save_chapter,
    save_glossary,
    save_library,
    save_series_meta,
)

log = logging.getLogger("mangatl.panel")

API_PREFIX = "/api/"
USER_PREFIX = "/u/"
"""Onde o conteudo do usuario e servido, depois de autorizado.

Nenhuma rota daqui recebe id de usuario. Ele vem da sessao, e so de la: um id na
URL e convite para trocar o numero e ler o acervo do vizinho."""

INTERNAL_REDIRECT_ENV = "INTERNAL_REDIRECT_PREFIX"
"""Prefixo interno que o proxy consome no meio da resposta.

Definido, o handler autoriza e devolve `X-Accel-Redirect` com o caminho; o proxy
le do disco e transmite, e o worker Python volta a atender na hora. Um capitulo
sao 155 JPEGs - com quatro leitores baixando, cada imagem segurando um worker
durante o download derruba o servidor.

Que este prefixo nao responda como rota publica e responsabilidade do
`Caddyfile`, nao deste modulo - e ja falhou uma vez, servindo `data/mangatl.db`
para a internet enquanto o `pytest` passava inteiro. Quem confere sao os casos 8
e 9 de `caddy_test.py`, com o proxy de pe.

Ausente, o Python transmite em pedacos. Funciona, e e divida anotada."""

SHOWCASE_ENV = "PUBLIC_SHOWCASE"
CONTACT_ENV = "CONTACT_EMAIL"


def contact_email() -> str | None:
    """Para onde vao os pedidos de remocao, se o responsavel pela instancia disse.

    Vem do ambiente, e nao do HTML dos termos: o mesmo `reader/` serve toda
    instancia, e cada uma tem o seu responsavel.
    """
    return os.environ.get(CONTACT_ENV, "").strip() or None


def showcase_is_public() -> bool:
    """Se quem chega sem sessao ve a vitrine ou a tela de entrar.

    Desligada por padrao porque a instalacao comum e de uma pessoa so, e nela a
    vitrine responde a pergunta errada: quem abre o endereco quer a propria
    biblioteca, e recebia um mostruario onde o botao do painel nem aparecia. Ligue
    numa instancia que existe para demonstrar o pipeline a quem nao tem conta.
    """
    return os.environ.get(SHOWCASE_ENV, "0").strip().lower() in {"1", "true", "yes", "on"}


TRUSTED_PROXIES_ENV = "TRUSTED_PROXIES"
REAL_IP_HEADER = "X-Real-IP"


def trusted_proxies() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """As redes de onde um `X-Real-IP` e aceito, lidas de `TRUSTED_PROXIES`.

    IP solto ou CIDR, separados por virgula. Vazio por padrao: sem proxy na frente,
    quem escreve o cabecalho e o cliente, e aceita-lo seria deixar cada pedido
    escolher o proprio IP - e com ele escapar do limite de tentativas de senha.
    """
    networks = []
    for item in os.environ.get(TRUSTED_PROXIES_ENV, "").split(","):
        if not item.strip():
            continue
        try:
            networks.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            # Entrada torta nao derruba o servidor, mas tambem nao some: sem ela o
            # proxy deixa de ser confiavel e o limite de login volta a ser um so.
            log.warning("operation=trusted_proxies invalid=%r", item.strip())
    return tuple(networks)


def client_ip(
    peer: str,
    real_ip: str | None,
    trusted: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str:
    """O IP de quem pediu: o do cabecalho so quando quem conectou e o proxy.

    Atras do Caddy todo pedido chega com o IP do Caddy, e a chave `ip:` do limite
    de login virava uma so para o mundo inteiro - cinco senhas erradas de qualquer
    pessoa trancavam o dono junto. O Caddy sobrescreve `X-Real-IP` com o endereco
    do socket dele, entao o valor que chega por ele e o do cliente, e nao o que o
    cliente escreveu.
    """
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not real_ip or not any(peer_address in network for network in trusted):
        return peer
    try:
        return str(ipaddress.ip_address(real_ip.strip()))
    except ValueError:
        return peer


MAX_COMPONENT_CHARS = 120
"""Nome de pasta mais longo que isso e engano ou ataque; o NTFS para em 255 e o
caminho inteiro tambem conta."""

MAX_PAGE_BYTES = 25 * 1024 * 1024
"""Pagina de manhwa em jpeg fica em centenas de KB; 25MB ja cobre PNG sem perda
de uma captura de rolagem inteira."""

MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
"""Um .cbz de capitulo longo passa raspando de 100MB."""

MAX_PAGES_PER_CHAPTER = 400
"""O capitulo medido aqui tem 155 fatias. Acima de 400 e pasta errada, nao capitulo."""

MAX_JSON_BYTES = 1024 * 1024
"""Corpo JSON do painel. Um glossario cheio nao passa de dezenas de KB; 1MB ja e
sinal de que veio coisa errada pelo cano."""

MAX_GLOSSARY_ENTRIES = 500
"""Acima disso nao e glossario de serie, e despejo de dicionario."""

MAX_ARCHIVE_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
"""Teto do descompactado, conferido somando `ZipInfo.file_size` ANTES de extrair.

Um zip de 1MB pode declarar 100GB. Somar o declarado nao e garantia contra zip que
mente, entao o laco de extracao tambem conta os bytes realmente escritos."""


# ---------- nomes vindos da rede ----------

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def safe_component(name: str) -> str | None:
    """Nome de serie ou capitulo aceitavel como nome de pasta, ou None.

    Recusa vazio, `.`, `..`, barra, barra invertida, dois-pontos e caractere de
    controle. Recusa nome que comece com ponto, que e como o `is_servable` marca o
    que nao sai na rede - uma serie chamada `.git` seria invisivel para o leitor.

    Acento e espaco passam: a serie que ja existe se chama "Eu me tornei a Neta
    Desprezada", e renomear a pasta quebraria todo caminho gravado nos JSONs.
    """
    if not name or len(name) > MAX_COMPONENT_CHARS:
        return None
    if name.startswith(".") or name in {".", ".."}:
        return None
    if any(char in name for char in "/\\:"):
        return None
    if _CONTROL_CHARS.search(name):
        return None
    return name


def safe_page_name(name: str) -> str | None:
    """Nome de arquivo de pagina: `safe_component` mais extensao de imagem."""
    if safe_component(name) is None:
        return None
    return name if PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES else None


def safe_archive_entries(names: Sequence[str]) -> list[tuple[int, str]] | None:
    """Posicao e nome-base de cada entrada que pode ser extraida, ou None.

    Devolve a posicao junto porque quem extrai precisa voltar ao `ZipInfo`
    correspondente: gravar pelo nome-base e o ponto, e o nome-base sozinho nao
    diz de qual entrada ele veio.
    """
    kept: list[tuple[int, str]] = []
    for index, raw in enumerate(names):
        name = raw.replace("\\", "/")
        if name.startswith("/") or ":" in name:
            return None

        parts = PurePosixPath(name).parts
        if any(part in {".", ".."} for part in parts):
            return None
        if not parts or name.endswith("/"):
            continue
        if parts[0] == "__MACOSX":
            continue

        page = safe_page_name(parts[-1])
        if page is not None:
            kept.append((index, page))
    return kept


def safe_archive_members(names: Sequence[str]) -> list[str] | None:
    """Entradas de um zip que podem ser extraidas, ou None se alguma for hostil.

    Zip carrega caminho dentro de si e a stdlib extrai o que estiver escrito -
    inclusive `../../.env`. Uma entrada hostil condena o arquivo inteiro em vez de
    ser pulada: zip com caminho de fuga nao e capitulo mal montado, e continuar
    extraindo o resto seria tratar ataque como tropeco.

    Diretorio e ignorado, assim como o `__MACOSX/` que o Finder enfia em todo zip.
    Entrada que nao e imagem tambem e ignorada - nao e hostil, so nao e pagina.
    """
    entries = safe_archive_entries(names)
    return None if entries is None else [name for _, name in entries]


# ---------- o que chega no corpo ----------


class Invalid(ValueError):
    """Pedido malformado.

    Existe para o handler poder desistir numa linha e ainda assim virar 422 com a
    mensagem legivel, em vez de 500 com stack trace - erro de quem chamou nao e
    defeito de quem atende.
    """


def json_body(body: bytes) -> object:
    if len(body) > MAX_JSON_BYTES:
        raise Invalid(f"corpo de {len(body)} bytes; o teto e {MAX_JSON_BYTES}")
    try:
        return json.loads(body or b"null")
    except ValueError as error:
        raise Invalid(f"corpo nao e JSON valido: {error}") from error


def validate_glossary(payload: object) -> dict[str, str]:
    """O glossario como o pipeline espera, ou `Invalid` com o que esta errado.

    Validar na borda e o que impede o arquivo de virar um campo minado: quem le
    depois e o motor de traducao, no meio de um capitulo, onde um valor que nao e
    string vira erro sem contexto nenhum.
    """
    if not isinstance(payload, dict):
        raise Invalid("o glossario e um objeto JSON de termo -> traducao")
    if len(payload) > MAX_GLOSSARY_ENTRIES:
        raise Invalid(f"{len(payload)} termos; o teto e {MAX_GLOSSARY_ENTRIES}")

    terms: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key.strip():
            raise Invalid("todo termo precisa ser texto e nao pode ser vazio")
        if not isinstance(value, str):
            raise Invalid(f"a traducao de {key!r} precisa ser texto")
        terms[key] = value
    return terms


_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"BM", ".bmp"),
)


def image_suffix(data: bytes) -> str | None:
    """Extensao deduzida dos bytes, ou None se nao for imagem que o leitor serve.

    Dos bytes e nao do `Content-Type`: o cabecalho e do cliente, e gravar
    `cover.jpg` com um executavel dentro seria acreditar nele.
    """
    for magic, suffix in _IMAGE_MAGIC:
        if data.startswith(magic):
            return suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


# ---------- roteador ----------

class Raw(NamedTuple):
    """Resposta que e um arquivo, e nao JSON.

    Existe porque as rotas de conteudo do usuario devolvem imagem: o handler
    autoriza e diz qual arquivo sai, e quem transmite decide entre entregar pelo
    proxy ou pelo Python. A rota nunca escreve no socket - assim ela continua
    testavel sem subir servidor.
    """

    path: Path
    content_type: str


class Context(NamedTuple):
    """O que uma rota precisa alem do proprio pedido.

    `cfg` ja vem apontado para a area de quem esta pedindo, e `base` e o projeto
    inteiro - o banco mora la, e uma rota que confunde os dois grava o banco
    dentro da pasta de um usuario.

    `connection` e aberta por requisicao e fechada no fim dela: conexao do sqlite3
    nao atravessa thread com seguranca, e o servidor e uma thread por conexao.

    O registro de jobs entra aqui e nao num modulo: ele guarda estado vivo, e
    estado vivo em variavel de modulo vaza entre servidores - inclusive entre dois
    testes que sobem o handler na mesma sessao.
    """

    cfg: Config
    base: Config
    connection: sqlite3.Connection
    session: Session | None
    token: str | None = None
    client_ip: str = ""
    """So para contar tentativa de senha e testador novo, nunca para autorizar.

    Vem de `client_ip`: o `X-Real-IP` so vale quando a conexao chega de um
    endereco em `TRUSTED_PROXIES`. Fora disso e o IP do socket - confiar num
    cabecalho que o cliente escreve e o mesmo que nao ter regra nenhuma."""

    @property
    def user(self) -> User | None:
        return self.session.user if self.session else None

    @property
    def is_owner(self) -> bool:
        return self.session is not None and self.session.is_owner


Body = bytes | Path
"""O corpo como o handler o recebe.

`bytes` para quase tudo: JSON e pagina cabem na memoria com folga. `Path` para
a rota do arquivo, cujo corpo pode passar de 100MB - um zip desses em `bytes` e
o processo inteiro segurando o upload na RAM enquanto atende o resto.
"""

Handler = Callable[[Context, tuple[str, ...], Body], tuple[int, object]]

Access = Literal["public", "session", "owner"]


class Route(NamedTuple):
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    max_body: int = MAX_JSON_BYTES
    """Teto do corpo, conferido pelo `Content-Length` antes de ler um byte."""

    access: Access = "session"
    """Quem pode chamar. O default e o lado seguro de errar: uma rota nova que
    esqueca a marca exige sessao, em vez de responder para qualquer um.

    `owner` esta nas que destroem ou custam dinheiro. `public` esta em login e em
    health, que precisam responder antes de existir sessao."""

    writes: bool = False
    """Se a rota muda o disco.

    Duas consequencias: ela exige CSRF, e ela e onde a sessao anonima nasce. A
    sessao nao nasce em `GET` de proposito - a home e uma pagina publica que robo
    de busca visita, e criar usuario a cada visita daria area em disco para cada
    um deles."""

    body_required: bool = False
    """Se a rota recusa pedido sem `Content-Length` declarado.

    Marcado rota a rota, e nao deduzido do metodo: `commit` e o descarte da area
    de espera sao POST e DELETE sem corpo nenhum, e criar capitulo aceita corpo
    vazio para pedir a sugestao de numero. Cliente nenhum concorda sobre mandar
    `Content-Length: 0` num pedido sem corpo, entao exigir pelo metodo devolveria
    411 no uso normal.

    O default e o lado seguro de errar: uma rota que le corpo e esquece a marca
    recebe b"" e recusa com 422 pela propria validacao, em vez de aceitar lixo.
    """

    user_locked: bool = False
    """Se a rota roda sozinha entre as rotas marcadas do mesmo usuario.

    O servidor e uma thread por conexao, e "conferir e gravar" nao e atomico: dez
    `PUT` simultaneos passavam todos pela conferencia antes de qualquer um gravar,
    e a area terminava acima da cota. Marcada nas rotas que ocupam disco e na que
    reescreve a traducao - duas correcoes ao mesmo tempo perderiam uma delas."""

    stream: bool = False
    """Se o corpo desce para um arquivo em vez de virar `bytes`.

    Marcada so onde o corpo e grande de verdade. Um zip de capitulo passa de
    100MB, e `rfile.read(length)` disso e um processo de 1GB segurando o upload
    inteiro na memoria - o handler recebe um `Path` e le de la.
    """


class RouteMatch(NamedTuple):
    """Resultado do casamento de rota.

    `route` None significa caminho certo e metodo errado, que e 405 e nao 404:
    dizer "nao existe" para `POST /api/health` esconderia o erro de quem chamou.
    """

    route: Route | None
    groups: tuple[str, ...] = ()
    allowed: tuple[str, ...] = ()


def _health(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Se o servidor esta de pe. O resto so para o dono.

    Publica porque monitor de uptime e o painel antes do login precisam dela. Mas
    versao do Python e presenca da chave da API sao inventario do servidor, e uma
    rota publica nao tem por que entrega-lo a quem so quer saber se ele responde.
    """
    if not ctx.is_owner:
        return HTTPStatus.OK, {"ok": True}
    return HTTPStatus.OK, {
        "ok": True,
        "engines": available_engines(),
        "detector": ctx.cfg.detect.backend,
        # Booleano, nunca o valor: a chave nao sai desta maquina por resposta nenhuma.
        "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "python": sys.version.split()[0],
    }
    # `root` saiu da resposta: e caminho absoluto do servidor, e uma rota publica
    # nao tem por que contar a quem pergunta como o disco dela esta organizado.


# ---------- sessao ----------


class _Issued(NamedTuple):
    """Resposta que vem com um cookie de sessao novo."""

    payload: object
    token: str
    hours: int


class _Cleared(NamedTuple):
    """Resposta que apaga o cookie de sessao."""

    payload: object


def _session_payload(session: Session | None) -> dict:
    # `showcase` responde tambem para quem nao tem sessao: e por ela que o leitor
    # sabe se a resposta a falta de sessao e um mostruario ou uma tela de entrar.
    if session is None:
        return {
            "authenticated": False,
            "kind": None,
            "engines": [],
            "showcase": showcase_is_public(),
            "contact": contact_email(),
        }
    return {
        "authenticated": True,
        "showcase": showcase_is_public(),
        "contact": contact_email(),
        "kind": session.user.kind,
        "expires_at": session.expires_at,
        # O testador nunca ve `claude` na lista: a chave da API e do dono, e a
        # conta chega para ele. A interface esconde e a API recusa - as duas.
        "engines": list(engines_for(session.user)),
    }


def _session(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Quem sou eu. Responde para quem nao tem sessao tambem, dizendo que nao tem."""
    return HTTPStatus.OK, _session_payload(ctx.session)


def _login(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Entra como dono. Unica rota que aceita senha, e a unica que pode ser varrida.

    A resposta e a mesma para e-mail que nao existe e para senha errada. Distinguir
    as duas entrega uma lista de quem tem conta aqui, de graca.
    """
    payload = json_body(body)
    if not isinstance(payload, dict):
        raise Invalid("esperava um objeto com email e password")

    email = str(payload.get("email", "")).strip()
    password = str(payload.get("password", ""))
    if not email or not password:
        raise Invalid("mande email e password")

    subjects = (f"ip:{ctx.client_ip}", f"email:{email}")
    if login_is_throttled(ctx.connection, subjects):
        return HTTPStatus.TOO_MANY_REQUESTS, {
            "error": "tentativas demais; espere alguns minutos"
        }

    owner = find_owner(ctx.connection, email)
    stored = password_hash_of(ctx.connection, owner.id) if owner else None
    if owner is None or not verify_password(password, stored):
        record_login_attempt(ctx.connection, subjects[0])
        record_login_attempt(ctx.connection, subjects[1])
        return HTTPStatus.UNAUTHORIZED, {"error": "e-mail ou senha nao conferem"}

    clear_login_attempts(ctx.connection, subjects)
    hours = hours_for(owner.kind)
    token, expires_at = issue(ctx.connection, owner.id, hours=hours)
    return HTTPStatus.OK, _Issued(
        payload=_session_payload(Session(user=owner, expires_at=expires_at)),
        token=token,
        hours=hours,
    )


def _logout(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Sai. Apaga a sessao do banco, e nao so o cookie do navegador.

    Cookie apagado sem a linha correspondente deixa um token vivo que continua
    valendo para quem o tiver copiado - sair tem que significar sair.
    """
    revoke(ctx.connection, ctx.token)
    return HTTPStatus.OK, _Cleared({"authenticated": False, "kind": None, "engines": []})


def _series(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Tudo que existe em library/, traduzido ou nao.

    `build_library` responderia outra pergunta - "o que da para ler" - e apagaria
    os dois estados que o painel existe para mostrar: a serie recem-criada e o
    capitulo enviado e ainda nao traduzido, que e o estado normal entre o upload e
    o botao de traduzir.
    """
    return HTTPStatus.OK, {
        "series": [state.model_dump(mode="json") for state in discover_series(ctx.cfg)]
    }


def _series_dir(cfg: Config, slug: str, *, must_exist: bool = True) -> Path:
    name = safe_component(slug)
    if name is None:
        raise Invalid(f"nome de serie inaceitavel: {slug!r}")
    directory = cfg.library_dir / name
    if must_exist and not directory.is_dir():
        raise Invalid(f"serie {name!r} nao existe")
    return directory


def _create_series(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Cria a pasta da serie e grava o titulo.

    O slug e o nome da pasta e nao muda depois; o titulo muda a vontade. Confundir
    os dois e a diferenca entre renomear a serie e reprocessar tudo.
    """
    payload = json_body(body)
    if not isinstance(payload, dict):
        raise Invalid("esperava um objeto com slug e title")

    directory = _series_dir(ctx.cfg, str(payload.get("slug", "")), must_exist=False)
    if directory.exists():
        return HTTPStatus.CONFLICT, {"error": f"serie {directory.name!r} ja existe"}

    title = payload.get("title", directory.name)
    if not isinstance(title, str):
        raise Invalid("title precisa ser texto")

    directory.mkdir(parents=True)
    save_series_meta(ctx.cfg, directory.name, SeriesMeta(title=title or directory.name))
    return HTTPStatus.CREATED, {"slug": directory.name, "title": title or directory.name}


def _get_series_meta(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    directory = _series_dir(ctx.cfg, groups[0])
    return HTTPStatus.OK, load_series_meta(ctx.cfg, directory.name).model_dump(mode="json")


def _put_series_meta(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    directory = _series_dir(ctx.cfg, groups[0])
    payload = json_body(body)
    if not isinstance(payload, dict):
        raise Invalid("esperava um objeto com title, cover e status")

    for field in ("title", "cover", "status"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            raise Invalid(f"{field} precisa ser texto")

    meta = SeriesMeta.model_validate(payload)
    save_series_meta(ctx.cfg, directory.name, meta)
    return HTTPStatus.OK, load_series_meta(ctx.cfg, directory.name).model_dump(mode="json")


def _put_cover(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Grava a capa da serie, com a extensao que os bytes disserem ser.

    As capas antigas saem junto: `_cover_url` escolhe entre `cover.*` pela ordem
    das extensoes, e deixar duas la significaria trocar a capa sem a troca aparecer.
    """
    directory = _series_dir(ctx.cfg, groups[0])
    suffix = image_suffix(body)
    if suffix is None:
        raise Invalid("o corpo nao e jpeg, png, webp nem bmp")

    for old_cover in directory.glob(f"{COVER_STEM}.*"):
        if old_cover.is_file():
            old_cover.unlink()

    target = directory / f"{COVER_STEM}{suffix}"
    target.write_bytes(body)
    return HTTPStatus.OK, {"cover": target.name, "bytes": len(body)}


def _get_glossary(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    directory = _series_dir(ctx.cfg, groups[0])
    return HTTPStatus.OK, load_glossary(ctx.cfg, directory.name)


def _put_glossary(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Grava o glossario da serie.

    E o arquivo que mais precisa de edicao recorrente: e ele que impede o
    personagem de mudar de nome no capitulo seguinte.
    """
    directory = _series_dir(ctx.cfg, groups[0])
    terms = validate_glossary(json_body(body))
    save_glossary(ctx.cfg, directory.name, terms)
    return HTTPStatus.OK, terms


# ---------- area de espera do upload ----------


def next_chapter_name(existing: Sequence[str]) -> str:
    """O proximo numero de capitulo, na largura que a serie ja usa.

    So capitulo puramente numerico conta. Uma serie com "extra" e "001" continua
    sugerindo "002"; uma serie so com nomes soltos sugere "001" e deixa a escolha
    com quem esta subindo.
    """
    numeric = [name for name in existing if name.isdigit()]
    if not numeric:
        return "001"
    last = max(numeric, key=_natural_key)
    return str(int(last) + 1).zfill(len(last))


def _chapter_paths(cfg: Config, slug: str, chapter: str) -> tuple[Path, Path]:
    """Onde o capitulo mora depois de pronto e enquanto sobe."""
    directory = _series_dir(cfg, slug)
    name = safe_component(chapter)
    if name is None:
        raise Invalid(f"nome de capitulo inaceitavel: {chapter!r}")
    return directory / name, directory / f"{name}{INCOMING_SUFFIX}"


def _incoming_files(incoming: Path) -> list[Path]:
    return list_page_images(incoming) if incoming.is_dir() else []


def _page_limit(user: User) -> int:
    """Quantas paginas este usuario pode ter num capitulo."""
    return MAX_PAGES_PER_CHAPTER if user.is_owner else TESTER_MAX_PAGES_PER_CHAPTER


def extract_archive(
    data: bytes | Path,
    target: Path,
    limit: int = MAX_PAGES_PER_CHAPTER,
    byte_ceiling: int | None = None,
) -> list[str]:
    """Grava as paginas do zip na area de espera, uma entrada por vez.

    Nunca `extractall`: ele obedece ao caminho gravado dentro do zip, e o zip
    carrega o caminho que quiser. A ordem das checagens tambem importa - o veto
    sobre os nomes vem antes de qualquer byte sair, senao a entrada hostil ja
    escreveu quando a recusa acontece.

    O teto do descompactado e conferido duas vezes de proposito: o `file_size`
    declarado antes de abrir, porque um zip de 1MB pode dizer 100GB, e os bytes
    realmente escritos durante a copia, porque quem escreve o declarado e o zip.

    `byte_ceiling` e a folga da cota de quem sobe. O zip de 40MB que cabia na cota
    podia ocupar gigabytes depois de aberto, porque so o compactado era conferido.
    Passar dela e `QuotaExceeded`, e nao `Invalid`: o arquivo e legitimo, quem nao
    tem espaco e a sessao.
    """
    # `zipfile` so precisa de algo que leia e posicione, e um arquivo aberto serve
    # tao bem quanto um buffer - com a diferenca de que o zip de 160MB fica no disco
    # e nao na memoria do processo que ainda esta atendendo todo mundo.
    with _archive_source(data) as buffer:
        if not zipfile.is_zipfile(buffer):
            raise Invalid("o corpo nao e um zip; .cbz tambem e zip, o nome nao decide")
        buffer.seek(0)
        return _extract_from(buffer, target, limit, byte_ceiling)


@contextmanager
def _archive_source(data: bytes | Path) -> Iterator[IO[bytes]]:
    if isinstance(data, Path):
        with data.open("rb") as handle:
            yield handle
        return
    yield io.BytesIO(data)


MAGIC_PREFIX_BYTES = 16
"""O suficiente para `image_suffix` reconhecer todos os formatos, webp incluso."""


def _refuse_expansion(total: int, byte_ceiling: int | None) -> None:
    """Levanta se `total` passou do teto que vale para esta extracao."""
    if byte_ceiling is not None and byte_ceiling < MAX_ARCHIVE_EXPANDED_BYTES:
        if total > byte_ceiling:
            raise QuotaExceeded(
                f"o arquivo descompactado passa de {byte_ceiling // (1024 * 1024)}MB,"
                " que e o que esta sessao ainda aceita."
            )
        return
    if total > MAX_ARCHIVE_EXPANDED_BYTES:
        raise Invalid(f"o descompactado passa do teto de {MAX_ARCHIVE_EXPANDED_BYTES} bytes")


def _extract_from(
    buffer: IO[bytes], target: Path, limit: int, byte_ceiling: int | None = None
) -> list[str]:
    with zipfile.ZipFile(buffer) as archive:
        infos = archive.infolist()
        entries = safe_archive_entries([info.filename for info in infos])
        if entries is None:
            raise Invalid("o arquivo tem entrada com caminho de fuga; nada foi extraido")
        if not entries:
            raise Invalid("o arquivo nao tem nenhuma imagem")
        if len(entries) > limit:
            raise Invalid(f"{len(entries)} paginas; este capitulo aceita mais {max(limit, 0)}")

        oversized = [name for index, name in entries if infos[index].file_size > MAX_PAGE_BYTES]
        if oversized:
            raise Invalid(f"{oversized[0]!r} passa de {MAX_PAGE_BYTES} bytes, o teto de uma pagina")

        names = [name for _, name in entries]
        if len(set(names)) != len(names):
            # Duas pastas dentro do zip com a mesma pagina: gravar pelo nome-base
            # faria uma apagar a outra, e o capitulo perderia pagina em silencio.
            raise Invalid("duas entradas do arquivo tem o mesmo nome de pagina")

        _refuse_expansion(sum(infos[index].file_size for index, _ in entries), byte_ceiling)

        # Os bytes magicos de todas antes de gravar qualquer uma: uma entrada que
        # nao e imagem com nome de `.jpg` condena o arquivo pelo mesmo raciocinio do
        # caminho de fuga, e recusar no meio deixaria meio capitulo escrito.
        for index, name in entries:
            with archive.open(infos[index]) as source:
                if image_suffix(source.read(MAGIC_PREFIX_BYTES)) is None:
                    raise Invalid(f"{name!r} nao e jpeg, png, webp nem bmp; nada foi extraido")

        written = 0
        for index, name in entries:
            with archive.open(infos[index]) as source, (target / name).open("wb") as sink:
                while chunk := source.read(64 * 1024):
                    written += len(chunk)
                    _refuse_expansion(written, byte_ceiling)
                    sink.write(chunk)

    return sorted(names, key=_natural_key)


def _create_chapter(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Abre a area de espera de um capitulo novo.

    Corpo vazio pede sugestao: o maior capitulo numerico que ja existe mais um.
    """
    directory = _series_dir(ctx.cfg, groups[0])
    payload = json_body(body)
    if payload is not None and not isinstance(payload, dict):
        raise Invalid("esperava um objeto com chapter, ou corpo vazio")

    asked = (payload or {}).get("chapter") or next_chapter_name(
        [entry.name for entry in directory.iterdir() if entry.is_dir()]
    )
    if not isinstance(asked, str):
        raise Invalid("chapter precisa ser texto")

    # Antes de criar a pasta, e nao depois: area de espera aberta ja e disco
    # ocupado, e recusar depois deixaria o lixo para a limpeza varrer.
    check_new_chapter(ctx.cfg, ctx.user)
    check_disk(ctx.base, ctx.user)

    chapter, incoming = _chapter_paths(ctx.cfg, directory.name, asked)
    if chapter.is_dir():
        return HTTPStatus.CONFLICT, {"error": f"capitulo {chapter.name!r} ja existe"}

    incoming.mkdir(exist_ok=True)
    return HTTPStatus.CREATED, {"chapter": chapter.name, "incoming": True, "files": []}


def _put_page(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Grava uma pagina na area de espera.

    Um arquivo por requisicao, corpo cru: `http.server` nao parseia
    `multipart/form-data` e o `cgi`, que parseava, saiu no Python 3.13. De brinde
    isso da barra de progresso por arquivo no front sem esforco nenhum.
    """
    slug, chapter, filename = groups
    name = safe_page_name(filename)
    if name is None:
        raise Invalid(f"nome de pagina inaceitavel: {filename!r}")

    _, incoming = _chapter_paths(ctx.cfg, slug, chapter)
    if not incoming.is_dir():
        raise Invalid("area de espera nao existe; crie o capitulo antes")

    # Pelos bytes, e nao pela extensao: gravar um executavel chamado `1.jpg` na
    # biblioteca seria acreditar no nome que o cliente escolheu.
    if image_suffix(body) is None:
        raise Invalid(f"{name!r} nao e jpeg, png, webp nem bmp")

    existing = _incoming_files(incoming)
    if len(existing) >= MAX_PAGES_PER_CHAPTER and not (incoming / name).exists():
        raise Invalid(
            f"{len(existing)} paginas na area de espera; o teto e {MAX_PAGES_PER_CHAPTER}"
        )

    # Sobrescrever uma pagina que ja subiu nao acrescenta pagina nenhuma.
    adding = 0 if (incoming / name).exists() else 1
    check_incoming_pages(ctx.cfg, ctx.user, incoming, adding)
    check_upload_bytes(ctx.cfg, ctx.user, len(body))
    check_disk(ctx.base, ctx.user, len(body))

    (incoming / name).write_bytes(body)
    record_usage(ctx.connection, ctx.user.id, pages=adding, bytes_=len(body))
    return HTTPStatus.OK, {"file": name, "bytes": len(body)}


def _put_archive(ctx: Context, groups: tuple[str, ...], body: Body) -> tuple[int, object]:
    """Extrai um zip/cbz inteiro na area de espera.

    Falha apaga a area de espera toda: meio zip extraido e pior que zip nenhum,
    porque parece capitulo e o `commit` aceitaria.

    O corpo chega como caminho, e nao como bytes: um zip de capitulo passa de
    100MB e a rota aceita ate 500MB. Quem apaga o temporario e o `_run`.
    """
    slug, chapter = groups
    _, incoming = _chapter_paths(ctx.cfg, slug, chapter)
    if not incoming.is_dir():
        raise Invalid("area de espera nao existe; crie o capitulo antes")

    size = body.stat().st_size if isinstance(body, Path) else len(body)

    check_upload_bytes(ctx.cfg, ctx.user, size)
    check_disk(ctx.base, ctx.user, size)

    # O zip comprime, entao o corpo nao diz quanto vai ocupar: a folga da cota vira
    # teto do descompactado, e o teto de paginas conta o que ja esta na espera.
    already = len(_incoming_files(incoming))
    try:
        names = extract_archive(
            body,
            incoming,
            limit=_page_limit(ctx.user) - already,
            byte_ceiling=upload_headroom(ctx.cfg, ctx.user),
        )
    except Exception:
        shutil.rmtree(incoming, ignore_errors=True)
        raise

    record_usage(ctx.connection, ctx.user.id, pages=len(names), bytes_=size)
    return HTTPStatus.OK, {"files": names, "count": len(names)}


def _get_incoming(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """O que ja subiu, na ordem que vai valer na leitura."""
    _, incoming = _chapter_paths(ctx.cfg, groups[0], groups[1])
    files = _incoming_files(incoming)
    return HTTPStatus.OK, {
        "exists": incoming.is_dir(),
        "files": [path.name for path in files],
        "bytes": sum(path.stat().st_size for path in files),
    }


def _delete_incoming(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Descarta a area de espera.

    E da sessao, e nao so do dono: a area vem do cookie e nao da URL, entao quem
    pede so alcanca a propria. Sem isto, um testador com upload quebrado ficava
    preso ate a sessao expirar.
    """
    _, incoming = _chapter_paths(ctx.cfg, groups[0], groups[1])
    if not incoming.is_dir():
        raise Invalid("nao ha area de espera para descartar")

    removed = len(_incoming_files(incoming))
    shutil.rmtree(incoming)
    return HTTPStatus.OK, {"removed": removed}


def _existing_series_dir(cfg: Config, slug: str) -> Path | None:
    """A pasta da serie nesta area, ou None - que vira 404, e nao 422.

    Para apagar, "nao existe" e "nao e seu" precisam da mesma resposta, pelo mesmo
    motivo das rotas `/u/`: a diferenca seria um indice do acervo alheio.
    """
    return _inside(cfg.library_dir, slug)


def _delete_chapter(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Apaga um capitulo da area de quem pede: as paginas e a traducao.

    E o que torna verdadeira a mensagem de cota do testador - "apague um para
    subir outro" - que ate aqui mandava fazer algo que nao existia.
    """
    series, chapter = groups
    directory = _existing_series_dir(ctx.cfg, series)
    pages = _inside(ctx.cfg.library_dir, series, chapter)
    if directory is None or pages is None or not pages.is_dir():
        return HTTPStatus.NOT_FOUND, NOT_FOUND_BODY
    if has_active(ctx.connection, ctx.user.id, directory.name, pages.name):
        return HTTPStatus.CONFLICT, {"error": "este capitulo esta na fila; espere terminar"}

    shutil.rmtree(pages)
    translation = _inside(ctx.cfg.output_dir, series, chapter)
    if translation is not None and translation.is_dir():
        shutil.rmtree(translation)
    _refresh_library(ctx.cfg)
    return HTTPStatus.OK, {"deleted": f"{directory.name}/{pages.name}"}


def _delete_series(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Apaga uma serie inteira. So o dono, e a tela pede confirmacao antes."""
    directory = _existing_series_dir(ctx.cfg, groups[0])
    if directory is None or not directory.is_dir():
        return HTTPStatus.NOT_FOUND, NOT_FOUND_BODY
    if has_active(ctx.connection, ctx.user.id, directory.name):
        return HTTPStatus.CONFLICT, {"error": "esta serie tem capitulo na fila; espere terminar"}

    shutil.rmtree(directory)
    translation = _inside(ctx.cfg.output_dir, directory.name)
    if translation is not None and translation.is_dir():
        shutil.rmtree(translation)
    _refresh_library(ctx.cfg)
    return HTTPStatus.OK, {"deleted": directory.name}


def _refresh_library(cfg: Config) -> None:
    """Regrava o `library.json`, que o leitor local le em vez do disco.

    Sem isto o capitulo apagado continua na estante ate o proximo job terminar.
    """
    if cfg.output_dir.is_dir():
        save_library(cfg, build_library(cfg))


def _commit_chapter(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Promove a area de espera a capitulo.

    O rename e o unico instante em que o capitulo passa a existir para o resto do
    sistema: ate aqui `discover_chapters` nao o enxerga, entao upload interrompido
    nunca vira meio capitulo traduzido.
    """
    chapter, incoming = _chapter_paths(ctx.cfg, groups[0], groups[1])
    files = _incoming_files(incoming)
    if not files:
        raise Invalid("area de espera vazia; nao ha o que promover")
    if chapter.exists():
        return HTTPStatus.CONFLICT, {"error": f"capitulo {chapter.name!r} ja existe"}

    incoming.rename(chapter)
    record_usage(ctx.connection, ctx.user.id, chapters=1)
    return HTTPStatus.OK, {"chapter": chapter.name, "files": [path.name for path in files]}


# ---------- jobs ----------


def _create_job(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Poe um capitulo para processar e devolve na hora.

    202 e nao 200: o trabalho leva minutos e o que volta e um recibo, nao o
    resultado. Quem chamou pergunta o progresso em `GET /api/jobs/<id>`.
    """
    payload = json_body(body)
    if not isinstance(payload, dict):
        raise Invalid("esperava um objeto com series, chapter e engine")

    series = str(payload.get("series", ""))
    chapter = str(payload.get("chapter", ""))
    engine = str(payload.get("engine", "")) or engines_for(ctx.user)[0]
    if engine not in available_engines():
        raise Invalid(f"motor {engine!r} nao existe; ha {', '.join(available_engines())}")
    # A trava de custo. A interface do testador nem mostra `claude`, e esta linha
    # e o que torna esconder irrelevante: a API recusa do mesmo jeito.
    check_engine(ctx.user, engine)

    directory, _ = _chapter_paths(ctx.cfg, series, chapter)
    if not directory.is_dir():
        raise Invalid(f"capitulo {series}/{chapter} nao existe; promova a area de espera antes")
    pages = _pages_to_retranslate(ctx.cfg, directory, engine, payload.get("pages"))

    try:
        job = enqueue(
            ctx.connection,
            user_id=ctx.user.id,
            series=directory.parent.name,
            chapter=directory.name,
            engine=engine,
            priority=OWNER_PRIORITY if ctx.is_owner else TESTER_PRIORITY,
            force=bool(payload.get("force")),
            dry_run=bool(payload.get("dry_run")),
            pages=pages,
        )
    except Busy as error:
        return HTTPStatus.CONFLICT, {"error": str(error)}

    return HTTPStatus.ACCEPTED, {"job_id": job.id, "job": _job_payload(ctx, job)}


MAX_PAGES_PER_RETRANSLATION = 20
"""Acima disso nao e corrigir paginas, e retraduzir o capitulo - que tem botao proprio."""


def _pages_to_retranslate(
    cfg: Config, directory: Path, engine: str, asked: object
) -> tuple[str, ...]:
    """As paginas de um pedido de retraducao parcial, conferidas, ou `()` para todas.

    Parcial so faz sentido sobre uma traducao que ja existe neste motor: o
    resultado e ela com as paginas pedidas trocadas, e sem ela uma pagina sozinha
    viraria um capitulo de uma pagina.
    """
    if asked is None:
        return ()
    if not isinstance(asked, list) or not asked or len(asked) > MAX_PAGES_PER_RETRANSLATION:
        raise Invalid(f"pages e uma lista de 1 a {MAX_PAGES_PER_RETRANSLATION} nomes de pagina")

    names: list[str] = []
    for item in asked:
        name = safe_page_name(item) if isinstance(item, str) else None
        if name is None or not (directory / name).is_file():
            raise Invalid(f"pagina {item!r} nao existe neste capitulo")
        names.append(name)

    if load_chapter(cfg, directory.parent.name, directory.name, engine) is None:
        raise Invalid(f"traduza o capitulo inteiro com {engine!r} antes de retraduzir uma pagina")
    return tuple(dict.fromkeys(names))


MAX_EDITED_TEXT_CHARS = 2000
"""Fala de balao passa raramente de duzentos caracteres; dois mil ja e colagem errada."""


def _put_block(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Corrige uma fala a mao, na traducao de um motor, e marca que foi a mao.

    E o conserto que faltava para erro de OCR ou de traducao: antes, uma fala
    errada so saia retraduzindo o capitulo inteiro. A marca `edited` e o que faz
    uma retraducao posterior devolver esta fala em vez de apaga-la.
    """
    series, chapter, engine = groups
    payload = json_body(body)
    if not isinstance(payload, dict):
        raise Invalid("esperava um objeto com page, block e text")
    page_index, block_id, text = payload.get("page"), payload.get("block"), payload.get("text")
    if not isinstance(page_index, int) or not isinstance(block_id, str) or not isinstance(text, str):
        raise Invalid("page e numero, block e text sao texto")
    if not text.strip() or len(text) > MAX_EDITED_TEXT_CHARS:
        raise Invalid(f"a fala precisa ter de 1 a {MAX_EDITED_TEXT_CHARS} caracteres")

    stored = _translation_of(ctx.cfg, series, chapter, engine)
    if stored is None:
        return HTTPStatus.NOT_FOUND, NOT_FOUND_BODY
    if has_active(ctx.connection, ctx.user.id, stored.series, stored.chapter):
        return HTTPStatus.CONFLICT, {"error": "este capitulo esta na fila; espere terminar"}

    updated = _with_edited_block(stored, page_index, block_id, text.strip())
    if updated is None:
        return HTTPStatus.NOT_FOUND, NOT_FOUND_BODY
    save_chapter(ctx.cfg, updated)
    return HTTPStatus.OK, {"page": page_index, "block": block_id, "text": text.strip(), "edited": True}


def _translation_of(cfg: Config, series: str, chapter: str, engine: str) -> Chapter | None:
    """A traducao deste motor na area de quem pede, ou None - que vira 404."""
    if engine not in available_engines():
        return None
    path = _inside(cfg.output_dir, series, chapter, chapter_filename(engine))
    if path is None or not path.is_file():
        return None
    return load_chapter(cfg, series, chapter, engine)


def _with_edited_block(
    stored: Chapter, page_index: int, block_id: str, text: str
) -> Chapter | None:
    """O capitulo com a fala trocada, ou None se a pagina ou o bloco nao existem."""
    page = next((item for item in stored.pages if item.index == page_index), None)
    if page is None or all(block.id != block_id for block in page.blocks):
        return None
    blocks = tuple(
        block.model_copy(update={"text": text, "edited": True}) if block.id == block_id else block
        for block in page.blocks
    )
    pages = tuple(
        item.model_copy(update={"blocks": blocks}) if item.index == page_index else item
        for item in stored.pages
    )
    return stored.model_copy(update={"pages": pages})


def _job_payload(ctx: Context, job) -> dict:  # noqa: ANN001 - `Job` importado tardiamente
    """O job como a tela o ve, com o lugar na fila quando ainda espera.

    "na fila" sem numero e indistinguivel de travado, e agora que a fila e
    compartilhada isso acontece de verdade: o capitulo de 155 fatias do dono
    segura o de 12 paginas do visitante por alguns minutos.
    """
    payload = job.snapshot()
    if job.state == "pending":
        payload["queue_position"] = queue_position(ctx.connection, job)
    return payload


def _list_jobs(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """So os jobs de quem esta pedindo.

    Sem o filtro, a tela de progresso de um testador listaria o capitulo que outro
    subiu - nome de serie e de capitulo inclusos. Nenhuma tela lista acervo alheio,
    e isto tambem e uma tela."""
    jobs = recent_jobs(ctx.connection, ctx.user.id)
    return HTTPStatus.OK, {"jobs": [_job_payload(ctx, job) for job in jobs]}


def _get_job(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    job = get_job(ctx.connection, groups[0], ctx.user.id)
    if job is None:
        # 404 e nao 403 pelo mesmo motivo das rotas de conteudo: 403 confirmaria
        # que o job existe e e de outra pessoa.
        return HTTPStatus.NOT_FOUND, {"error": f"job {groups[0]!r} nao existe"}
    return HTTPStatus.OK, _job_payload(ctx, job)


# ---------- conteudo do usuario ----------


USER_PAGES_BASE = "u/pages"
USER_CHAPTERS_BASE = "u/chapters"
"""Os prefixos que o indice de `/u/library` devolve no lugar de `library/` e
`output/`.

O leitor monta `<base>/<serie>/<cap>/<arquivo>` e nao sabe de onde a base veio: a
vitrine manda `public/demo`, o acervo manda estes dois, e a tela de leitura e a
mesma. Um caminho de renderizacao proprio para a vitrine faria a vitrine deixar de
demonstrar o produto."""


def _inside(base: Path, *parts: str) -> Path | None:
    """O caminho sob `base`, ou None se escapar dela.

    Duas trancas, e as duas importam. `safe_component` recusa `..`, barra e
    caractere de controle antes de qualquer `Path` existir. A resolucao confere
    que o resultado continua sob a base - e o que pega symlink, juncao com
    caminho absoluto e normalizacao do sistema de arquivos.
    """
    for part in parts:
        if safe_component(part) is None:
            return None
    try:
        root = base.resolve()
        resolved = base.joinpath(*parts).resolve()
    except (OSError, ValueError):
        return None
    return resolved if root in resolved.parents else None


NOT_FOUND_BODY = {"error": "nao existe"}
"""Uma resposta so para "nao existe" e "nao e seu".

403 confirmaria que o arquivo existe, e a diferenca entre as duas respostas e um
indice do acervo alheio para quem souber varrer nomes."""


def _content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _user_page(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Uma imagem de pagina do acervo de quem esta pedindo."""
    series, chapter, filename = groups
    if safe_page_name(filename) is None:
        return HTTPStatus.NOT_FOUND, NOT_FOUND_BODY

    path = _inside(ctx.cfg.library_dir, series, chapter, filename)
    if path is None or not path.is_file():
        return HTTPStatus.NOT_FOUND, NOT_FOUND_BODY
    return HTTPStatus.OK, Raw(path, _content_type(path))


def _user_chapter(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """A traducao de um capitulo do acervo de quem esta pedindo.

    O nome do arquivo entra inteiro na URL, e nao so o motor, para o leitor montar
    o endereco com o mesmo codigo que usa na vitrine - onde o arquivo se chama
    exatamente assim em disco.
    """
    series, chapter, engine = groups
    if engine not in available_engines():
        return HTTPStatus.NOT_FOUND, NOT_FOUND_BODY

    path = _inside(ctx.cfg.output_dir, series, chapter, chapter_filename(engine))
    if path is None or not path.is_file():
        return HTTPStatus.NOT_FOUND, NOT_FOUND_BODY
    return HTTPStatus.OK, Raw(path, "application/json; charset=utf-8")


def _user_library(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """O indice do acervo de quem esta pedindo.

    Montado na hora, e nao lido de `library.json`: aquele arquivo guarda caminhos
    com o prefixo `library/`, que deixou de ser alcancavel por caminho. O indice
    que sai daqui ja vem com as bases de `/u/`, e nenhuma delas leva id de usuario.
    """
    payload = build_library(ctx.cfg).model_dump(mode="json")
    payload["library_base"] = USER_PAGES_BASE
    payload["output_base"] = USER_CHAPTERS_BASE
    for entry in payload["series"]:
        entry["cover"] = _rebased_cover(entry["cover"], ctx.cfg)
        for chapter in entry["chapters"]:
            chapter["cover"] = _rebased_cover(chapter["cover"], ctx.cfg)
    return HTTPStatus.OK, payload


def _rebased_cover(cover: str | None, cfg: Config) -> str | None:
    """A capa apontando para `/u/pages/`, e nao para `library/`.

    `build_library` grava a capa com o prefixo do disco porque e assim que o leitor
    local a busca. Deixa-la assim aqui daria uma estante de capas quebradas: o
    unico caminho que responde na instancia hospedada e o autorizado.
    """
    if cover is None:
        return None
    prefix = f"{cfg.paths.library}/"
    return f"{USER_PAGES_BASE}/{cover[len(prefix):]}" if cover.startswith(prefix) else None


_SLUG = r"([^/]+)"

ROUTES: tuple[Route, ...] = (
    # Publicas: precisam responder antes de existir sessao.
    Route("GET", re.compile(r"^/api/health$"), _health, access="public"),
    Route("GET", re.compile(r"^/api/session$"), _session, access="public"),
    Route(
        "POST",
        re.compile(r"^/api/login$"),
        _login,
        access="public",
        writes=True,
        body_required=True,
    ),
    Route("POST", re.compile(r"^/api/logout$"), _logout, access="public", writes=True),
    # O conteudo do usuario. Nenhuma recebe id: ele vem da sessao.
    Route("GET", re.compile(r"^/u/library$"), _user_library),
    Route("GET", re.compile(rf"^/u/pages/{_SLUG}/{_SLUG}/{_SLUG}$"), _user_page),
    Route(
        "GET",
        re.compile(rf"^/u/chapters/{_SLUG}/{_SLUG}/chapter\.{_SLUG}\.json$"),
        _user_chapter,
    ),
    # O painel do proprio acervo.
    Route("GET", re.compile(r"^/api/series$"), _series),
    Route("POST", re.compile(r"^/api/series$"), _create_series, writes=True, body_required=True),
    Route("GET", re.compile(rf"^/api/series/{_SLUG}/series\.json$"), _get_series_meta),
    Route(
        "PUT",
        re.compile(rf"^/api/series/{_SLUG}/series\.json$"),
        _put_series_meta,
        access="owner",
        writes=True,
        body_required=True,
    ),
    Route(
        "PUT",
        re.compile(rf"^/api/series/{_SLUG}/cover$"),
        _put_cover,
        MAX_PAGE_BYTES,
        access="owner",
        writes=True,
        body_required=True,
    ),
    Route("GET", re.compile(rf"^/api/series/{_SLUG}/glossary$"), _get_glossary),
    Route(
        "PUT",
        re.compile(rf"^/api/series/{_SLUG}/glossary$"),
        _put_glossary,
        access="owner",
        writes=True,
        body_required=True,
    ),
    Route(
        "POST",
        re.compile(rf"^/api/series/{_SLUG}/chapters$"),
        _create_chapter,
        writes=True,
        user_locked=True,
    ),
    Route(
        "PUT",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/files/{_SLUG}$"),
        _put_page,
        MAX_PAGE_BYTES,
        writes=True,
        body_required=True,
        user_locked=True,
    ),
    Route(
        "POST",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/archive$"),
        _put_archive,
        MAX_ARCHIVE_BYTES,
        writes=True,
        body_required=True,
        stream=True,
        user_locked=True,
    ),
    Route(
        "POST",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/commit$"),
        _commit_chapter,
        writes=True,
    ),
    Route("GET", re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/incoming$"), _get_incoming),
    Route(
        "PUT",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/translations/{_SLUG}/blocks$"),
        _put_block,
        writes=True,
        body_required=True,
        user_locked=True,
    ),
    Route("GET", re.compile(r"^/api/jobs$"), _list_jobs),
    Route("POST", re.compile(r"^/api/jobs$"), _create_job, writes=True, body_required=True),
    Route("GET", re.compile(rf"^/api/jobs/{_SLUG}$"), _get_job),
    Route(
        "DELETE",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/incoming$"),
        _delete_incoming,
        writes=True,
    ),
    Route(
        "DELETE",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}$"),
        _delete_chapter,
        writes=True,
    ),
    Route("DELETE", re.compile(rf"^/api/series/{_SLUG}$"), _delete_series, access="owner", writes=True),
)
"""Toda rota diz quem pode chama-la e se ela escreve.

As `owner` sao as que mudam o acervo inteiro: apagar uma serie, reescrever
`series.json`, trocar a capa e editar o glossario. Apagar capitulo e area de
espera e da sessao: a area vem do cookie, entao o testador so alcanca a propria,
e sem essas duas ele nao tinha como sair da cota nem de um upload quebrado."""


def request_path(target: str) -> str:
    """O caminho do pedido, sem query e sem fragmento.

    Sem decodificar: `%2F` precisa continuar dentro de um segmento so, senao
    `/api/series/a%2Fb` casaria como se fossem duas pastas. Quem consome o grupo
    decodifica e passa por `safe_component`, que recusa a barra que aparecer.
    """
    return target.split("?", 1)[0].split("#", 1)[0]


def match_route(method: str, path: str) -> RouteMatch | None:
    """Rota que atende, ou None quando o caminho nao existe."""
    allowed: list[str] = []
    for route in ROUTES:
        found = route.pattern.match(path)
        if found is None:
            continue
        if route.method == method:
            return RouteMatch(route, tuple(unquote(group) for group in found.groups()))
        allowed.append(route.method)

    if allowed:
        return RouteMatch(None, allowed=tuple(allowed))
    return None


# ---------- ligacao HTTP ----------


def _open_tester_session(connection: sqlite3.Connection) -> tuple[Session, str]:
    """Cria o testador anonimo e a sessao dele. Sem cadastro, sem e-mail, sem senha.

    O prazo do usuario e o da sessao sao o mesmo numero de propostio: sessao que
    sobrevive ao dono dela e uma linha apontando para nada, e usuario que
    sobrevive a sessao e area em disco que ninguem alcanca mais.
    """
    expires_at = in_hours(TESTER_SESSION_HOURS)
    user = create_user(connection, kind="tester", expires_at=expires_at)
    token, session_expires = issue(connection, user.id, hours=TESTER_SESSION_HOURS)
    return Session(user=user, expires_at=session_expires), token


def _internal_redirect(cfg: Config, path: Path) -> str | None:
    """O caminho interno que o proxy serve, ou None para transmitir daqui.

    O prefixo mapeia a raiz do projeto dentro do proxy. Se ele responder de fora,
    toda a autorizacao deste modulo vira decoracao - existiria um caminho que nao
    passa por aqui. Mas quem garante isso e o `Caddyfile`, e nao esta funcao: aqui
    so se monta a string.

    Esta frase ja esteve escrita como se fosse promessa deste arquivo, e enquanto
    ela estava, o `Caddyfile` tinha um `handle_path /_internal/*` de primeiro nivel
    servindo `data/mangatl.db` para a internet. Comentario nao e controle. O
    controle sao os casos 8 e 9 de `caddy_test.py`, que pedem o prefixo de fora,
    com o proxy de pe, e exigem 404.
    """
    prefix = os.environ.get(INTERNAL_REDIRECT_ENV, "").strip()
    if not prefix:
        return None
    try:
        relative = path.resolve().relative_to(cfg.root.resolve())
    except ValueError:
        # Fora da raiz do projeto o proxy nao alcanca; transmitir daqui e o certo.
        return None
    return f"{prefix.rstrip('/')}/{relative.as_posix()}"


class UserLocks:
    """Uma trava por usuario, para conferir a cota e gravar sem outra thread no meio.

    Por usuario e nao global: o upload de um testador nao espera o do outro, que
    nao disputa a mesma cota. Vive dentro de um handler, e nao no modulo: estado
    vivo em variavel de modulo vaza entre dois servidores na mesma sessao de teste.

    O dicionario ganha um item por usuario que subiu algo neste processo. Sao
    dezenas de bytes por testador, e o processo reinicia a cada deploy.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @contextmanager
    def hold(self, user_id: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(user_id, threading.Lock())
        with lock:
            yield


def make_panel_handler(cfg: Config) -> type[ReaderHandler]:
    """Handler que serve o leitor e, para a propria maquina, tambem o painel.

    Estende o `ReaderHandler`: pedido que nao casa com `/api/` cai no `super()` e
    e servido como arquivo, exatamente como antes.

    Nao ha registro de jobs aqui: a fila mora no banco e quem a roda e o
    `mangatl worker`, noutro processo. Este handler so enfileira e le.
    """

    user_locks = UserLocks()

    class PanelHandler(ReaderHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            # Só a PWA e a vitrine saem por caminho, e as duas moram na raiz do
            # projeto. O acervo sai pelas rotas `/u/`, que conferem a sessão.
            super().__init__(*args, directory=str(cfg.root), **kwargs)

        def do_GET(self) -> None:  # noqa: N802 - assinatura herdada da stdlib
            if not self._serve_api("GET"):
                super().do_GET()

        def do_POST(self) -> None:  # noqa: N802 - assinatura herdada da stdlib
            self._serve_api("POST")

        def do_PUT(self) -> None:  # noqa: N802 - assinatura herdada da stdlib
            self._serve_api("PUT")

        def do_DELETE(self) -> None:  # noqa: N802 - assinatura herdada da stdlib
            self._serve_api("DELETE")

        def _serve_api(self, method: str) -> bool:
            """Atende o pedido se ele for nosso; devolve False para o resto.

            A ordem das checagens e deliberada: rota, CSRF, sessao, papel, corpo.
            CSRF antes da sessao porque um pedido forjado nao pode nem criar
            sessao anonima; corpo por ultimo porque ler megabytes de quem vai
            levar 401 e trabalho jogado fora.
            """
            path = request_path(self.path)
            if not path.startswith((API_PREFIX, USER_PREFIX)):
                # Metodo sem arquivo para servir nao tem para onde cair.
                if method != "GET":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "rota nao existe"})
                    return True
                return False

            match = match_route(method, path)
            if match is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"rota {method} {path} nao existe"})
                return True
            if match.route is None:
                self._send_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": f"{method} nao vale aqui; use {', '.join(match.allowed)}"},
                    headers={"Allow": ", ".join(match.allowed)},
                )
                return True

            route = match.route
            if route.writes and not csrf_is_valid(self._csrf_headers()):
                self._discard_small_body()
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": f"pedido sem origem conferida; mande {REQUESTED_WITH}"},
                )
                return True

            with connect(cfg) as connection:
                return self._dispatch(method, path, route, match.groups, connection)

        def _csrf_headers(self) -> dict[str, str | None]:
            return {
                name: self.headers.get(name)
                for name in (REQUESTED_WITH, "Origin", "Referer", "Host")
            }

        def _dispatch(
            self,
            method: str,
            path: str,
            route: Route,
            groups: tuple[str, ...],
            connection: sqlite3.Connection,
        ) -> bool:
            token = token_from_cookies(self.headers.get("Cookie"))
            session = resolve(connection, token)
            issued: str | None = None
            requester_ip = client_ip(
                self.client_address[0] if self.client_address else "",
                self.headers.get(REAL_IP_HEADER),
                trusted_proxies(),
            )

            if (
                session is None
                and route.writes
                and route.access != "public"
                and showcase_is_public()
            ):
                # A sessao anonima nasce aqui, na primeira escrita, e nao no
                # primeiro `GET`: a home e publica e robo de busca a visita.
                # So com a vitrine ligada: sem ela a instalacao e de uma pessoa, e
                # esconder o testador na tela enquanto a API o cria seria a flag
                # decidir so a aparencia.
                if not tester_signup_allowed(connection, requester_ip):
                    self._discard_small_body()
                    self._send_json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "sessoes de teste demais deste endereco; tente daqui a uma hora"},
                    )
                    return True
                session, token = _open_tester_session(connection)
                record_tester_signup(connection, requester_ip)
                issued = token

            if session is None and route.access != "public":
                self._discard_small_body()
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "entre para continuar"})
                return True
            if route.access == "owner" and (session is None or not session.is_owner):
                self._discard_small_body()
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "so o dono faz isso"})
                return True

            # A area sai antes do corpo porque o teto da cota depende dela, e o teto
            # precisa valer antes de o upload comecar a ocupar disco.
            area = area_config(cfg, session.user) if session else cfg
            ceiling = upload_headroom(area, session.user) if route.stream and session else None

            body = self._read_body(route, ceiling)
            if body is None:
                return True

            context = Context(
                cfg=area,
                base=cfg,
                connection=connection,
                session=session,
                token=token,
                client_ip=requester_ip,
            )

            # A sessao recem-aberta viaja no cookie ate nas respostas de erro. Sem
            # isto, uma primeira escrita que nao passa na validacao deixa a linha de
            # sessao no banco e nao entrega o cookie: o cliente volta sem sessao,
            # abre outra na tentativa seguinte, e quem erra em laco vira uma fabrica
            # de sessoes orfas.
            cookie: dict[str, str] = {}
            if issued is not None and session is not None:
                cookie["Set-Cookie"] = cookie_header(issued, hours=hours_for(session.user.kind))

            try:
                status, payload = self._run(route, context, groups, body)
            except Invalid as error:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)}, headers=cookie
                )
                return True
            except QuotaExceeded as error:
                # 429 com a mensagem que explica o teto, e nao erro generico: quem
                # bateu na cota precisa saber qual e ela para decidir o que fazer.
                self._send_json(
                    HTTPStatus.TOO_MANY_REQUESTS, {"error": str(error)}, headers=cookie
                )
                return True
            except OutOfSpace as error:
                # 507 e nao 429: o limite nao e desta pessoa, e tentar de novo em
                # um minuto nao adianta.
                self._send_json(
                    HTTPStatus.INSUFFICIENT_STORAGE, {"error": str(error)}, headers=cookie
                )
                return True
            except Exception:  # noqa: BLE001 - erro nosso vira 500, nunca stack na resposta
                # A resposta nunca leva stack; o log do servidor sempre leva. Sem o
                # traceback aqui, um 500 vira "falha em POST /rota" e nada mais, e a
                # causa morre no processo onde aconteceu.
                self.log_error("falha em %s %s\n%s", method, path, traceback.format_exc())
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "falha no painel"}, headers=cookie
                )
                return True

            self._respond(status, payload, issued=issued, kind=session.user.kind if session else None)
            return True

        def _respond(
            self, status: int, payload: object, *, issued: str | None, kind: str | None
        ) -> None:
            """Manda o que a rota devolveu, e o cookie que ela provocou.

            Tres formas de resposta e nao uma porque as tres tem naturezas
            diferentes: JSON, um arquivo que o proxy entrega, e a troca de sessao.
            Deixar a rota escrever no socket para cobrir os tres a tornaria
            impossivel de testar sem subir servidor.
            """
            headers: dict[str, str] = {}
            if isinstance(payload, _Issued):
                headers["Set-Cookie"] = cookie_header(payload.token, hours=payload.hours)
                payload = payload.payload
            elif isinstance(payload, _Cleared):
                headers["Set-Cookie"] = clearing_cookie_header()
                payload = payload.payload
            elif issued is not None and kind is not None:
                headers["Set-Cookie"] = cookie_header(issued, hours=hours_for(kind))

            if isinstance(payload, Raw):
                self._send_file(payload, headers)
                return
            self._send_json(status, payload, headers=headers)

        def _send_file(self, raw: Raw, headers: dict[str, str]) -> None:
            """Entrega o arquivo ja autorizado.

            Com `INTERNAL_REDIRECT_PREFIX` definido o Python sai do caminho dos
            bytes: responde vazio com o caminho interno, o proxy le do disco e
            transmite, e o worker volta a atender na hora. Sem ele, transmite em
            pedacos daqui - funciona, e e divida anotada.
            """
            internal = _internal_redirect(cfg, raw.path)
            if internal is not None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", raw.content_type)
                self.send_header("X-Accel-Redirect", internal)
                self.send_header("Cache-Control", "private, no-store")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                return

            size = raw.path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", raw.content_type)
            self.send_header("Content-Length", str(size))
            # `private` e `no-store`: e conteudo de uma conta, e cache
            # compartilhado servindo isto para outra pessoa e o vazamento que
            # todo o resto deste modulo existe para impedir.
            self.send_header("Cache-Control", "private, no-store")
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            with raw.path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, 64 * 1024)

        def _run(
            self, route: Route, context: Context, groups: tuple[str, ...], body: Body
        ) -> tuple[int, object]:
            """Roda o handler e apaga o arquivo temporario depois, de todo jeito.

            O `finally` cobre a recusa tambem: um zip que nao passa na validacao
            ocupa o mesmo disco que um que passa, e so o caminho de sucesso limpar
            transformaria cada erro em lixo permanente.
            """
            locked = route.user_locked and context.user is not None
            try:
                with user_locks.hold(context.user.id) if locked else nullcontext():
                    return route.handler(context, groups, body)
            finally:
                if isinstance(body, Path):
                    body.unlink(missing_ok=True)

        def _discard_small_body(self) -> None:
            """Le e joga fora um corpo pequeno antes de recusar o pedido.

            Responder e fechar com bytes ainda nao lidos no socket faz o sistema
            mandar RST em vez de FIN - no Windows o cliente perde a resposta e ve
            "conexao anulada" no lugar do 401 ou 403. Corpo grande nao e drenado:
            ler megabytes de quem vai ser recusado e o que as recusas existem para
            evitar, e ai a conexao fecha de proposito.
            """
            raw = self.headers.get("Content-Length", "")
            length = int(raw) if raw.isdigit() else 0
            if 0 < length <= MAX_JSON_BYTES:
                self.rfile.read(length)
            elif length:
                self.close_connection = True

        def _read_body(self, route: Route, ceiling: int | None) -> Body | None:
            """O corpo cru, ou None quando ja respondeu recusando.

            `http.server` nao decodifica `Transfer-Encoding: chunked` e o `cgi`,
            que parseava multipart, saiu no Python 3.13. Por isso o painel exige
            corpo cru com tamanho declarado: o `fetch` do navegador manda
            `Content-Length` para `Blob`, e o que nao manda e outra coisa.

            O teto e conferido no cabecalho, antes de ler um byte - recusar depois
            de receber 500MB nao protege de nada.
            """
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "mande Content-Length"})
                return None

            raw = self.headers.get("Content-Length")
            if raw is None:
                if route.body_required:
                    self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "mande Content-Length"})
                    return None
                return b""

            try:
                length = int(raw)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Content-Length nao e numero"})
                return None

            if length > route.max_body:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": f"corpo de {length} bytes; o teto desta rota e {route.max_body}"},
                )
                return None

            if ceiling is not None and length > ceiling:
                # A cota responde antes de o primeiro byte sair do cliente. Deixar
                # para conferir depois seria gravar 500MB no disco que a cota existe
                # para proteger, so para entao dizer que nao cabia.
                self._send_json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {
                        "error": f"corpo de {length // (1024 * 1024)}MB;"
                        f" esta sessao ainda aceita {ceiling // (1024 * 1024)}MB."
                    },
                )
                return None

            if not route.stream:
                return self.rfile.read(length)
            return self._spool_body(length)

        def _spool_body(self, length: int) -> Path | None:
            """Desce o corpo para um arquivo, em pedacos, e devolve o caminho.

            Ao lado do banco e nao em `/tmp`: no contêiner `/tmp` e a camada de
            escrita da imagem, e um zip de 500MB la dentro enche o que nao tem dono.
            Aqui e o mesmo volume do acervo, que e o volume que o teto de disco ja
            vigia.

            Recebimento incompleto apaga o arquivo e recusa: meio zip nao e zip, e
            o `zipfile` diria isso de um jeito bem menos claro.
            """
            spool = cfg.data_dir / "uploads"
            spool.mkdir(parents=True, exist_ok=True)

            handle, name = tempfile.mkstemp(dir=spool, suffix=".upload")
            path = Path(name)
            received = 0
            try:
                with os.fdopen(handle, "wb") as sink:
                    while received < length:
                        chunk = self.rfile.read(min(1024 * 1024, length - received))
                        if not chunk:
                            break
                        received += len(chunk)
                        sink.write(chunk)
            except Exception:
                path.unlink(missing_ok=True)
                raise

            if received != length:
                path.unlink(missing_ok=True)
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"recebi {received} de {length} bytes declarados"},
                )
                return None
            return path

        def _send_json(self, status: int, payload: object, *, headers: dict | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # O painel e estado vivo: resposta cacheada e progresso congelado.
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    return PanelHandler


def serve_panel(cfg: Config, port: int) -> None:
    """Sobe o leitor com o painel junto. So o `mangatl serve` chama isto."""
    serve_handler(make_panel_handler(cfg), port)
