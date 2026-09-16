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

Este modulo depende do venv inteiro: pydantic, o store, o pipeline. O
`serving.py` continua sendo stdlib pura porque o `scripts/serve.py` roda no python
do Windows, sem venv. Manter essa fronteira e o que impede um import pesado de
derrubar o servidor que o celular usa.

Ponto de extensao anotado e nao implementado: baixar capitulo de URL pediria uma
`import_from_url(url) -> list[Path]`. Qualquer site serio precisa de navegador
headless e quebra a cada mudanca de layout, entao a origem das imagens e upload.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple
from urllib.parse import unquote

from .accounts import User, area_config, create_user, find_owner, password_hash_of
from .config import Config
from .db import connect
from .engines.base import available_engines
from .jobs import Busy, JobRegistry
from .models import SeriesMeta
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
    resolve,
    revoke,
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
    load_series_meta,
    save_glossary,
    save_series_meta,
)

API_PREFIX = "/api/"
USER_PREFIX = "/u/"
"""Onde o conteudo do usuario e servido, depois de autorizado.

Nenhuma rota daqui recebe id de usuario. Ele vem da sessao, e so de la: um id na
URL e convite para trocar o numero e ler o acervo do vizinho."""

INTERNAL_REDIRECT_ENV = "INTERNAL_REDIRECT_PREFIX"
"""Prefixo interno que o proxy serve e que nao e alcancavel de fora.

Definido, o handler autoriza e devolve `X-Accel-Redirect` com o caminho; o proxy
le do disco e transmite, e o worker Python volta a atender na hora. Um capitulo
sao 155 JPEGs - com quatro leitores baixando, cada imagem segurando um worker
durante o download derruba o servidor.

Ausente, o Python transmite em pedacos. Funciona, e e divida anotada."""

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

# ---------- cota do testador ----------
#
# Todas juntas, num lugar so, cada uma com o numero justificado. Espalhar estes
# valores pelas rotas e como nao te-los: ninguem consegue responder "o que um
# testador pode fazer aqui?" sem ler o servidor inteiro.

TESTER_MAX_PAGES_PER_CHAPTER = 12
"""Doze paginas mostram a qualidade da traducao tao bem quanto 155 e custam 1/13
do CPU. O capitulo medido aqui levou 216s para 155 fatias; doze levam ~17s."""

TESTER_MAX_CHAPTERS = 2
"""Dois bastam para comparar uma pagina facil com uma dificil. Mais que isso e
acervo, e acervo e o que este servidor nao e."""

TESTER_MAX_UPLOAD_BYTES = 40 * 1024 * 1024
"""Doze paginas de manhwa em jpeg nao passam de alguns MB; 40MB cobre PNG sem
perda e ainda e um teto que um disco pequeno aguenta vezes muitos testadores."""

TESTER_ENGINES: tuple[str, ...] = ("free",)
"""Sem `claude`: a chave da API e do dono, e a conta chega para ele.

E a trava de custo mais importante deste projeto. Ela vale na interface, que nao
oferece a opcao, e na API, que recusa - a interface sozinha e sugestao."""

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
    jobs: JobRegistry
    connection: sqlite3.Connection
    session: Session | None
    token: str | None = None
    client_ip: str = ""
    """So para contar tentativa de senha, nunca para autorizar.

    Atras de um proxy reverso este valor e o IP do proxy, a menos que alguem
    confie explicitamente no `X-Forwarded-For` - e confiar num cabecalho que o
    cliente escreve e o mesmo que nao ter regra nenhuma."""

    @property
    def user(self) -> User | None:
        return self.session.user if self.session else None

    @property
    def is_owner(self) -> bool:
        return self.session is not None and self.session.is_owner


Handler = Callable[[Context, tuple[str, ...], bytes], tuple[int, object]]

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


class RouteMatch(NamedTuple):
    """Resultado do casamento de rota.

    `route` None significa caminho certo e metodo errado, que e 405 e nao 404:
    dizer "nao existe" para `POST /api/health` esconderia o erro de quem chamou.
    """

    route: Route | None
    groups: tuple[str, ...] = ()
    allowed: tuple[str, ...] = ()


def _health(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
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
    if session is None:
        return {"authenticated": False, "kind": None, "engines": []}
    return {
        "authenticated": True,
        "kind": session.user.kind,
        "expires_at": session.expires_at,
        # O testador nunca ve `claude` na lista: a chave da API e do dono, e a
        # conta chega para ele. A interface esconde e a API recusa - as duas.
        "engines": available_engines() if session.is_owner else list(TESTER_ENGINES),
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
        payload={"authenticated": True, "kind": owner.kind, "expires_at": expires_at,
                 "engines": available_engines()},
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


def extract_archive(data: bytes, target: Path) -> list[str]:
    """Grava as paginas do zip na area de espera, uma entrada por vez.

    Nunca `extractall`: ele obedece ao caminho gravado dentro do zip, e o zip
    carrega o caminho que quiser. A ordem das checagens tambem importa - o veto
    sobre os nomes vem antes de qualquer byte sair, senao a entrada hostil ja
    escreveu quando a recusa acontece.

    O teto do descompactado e conferido duas vezes de proposito: o `file_size`
    declarado antes de abrir, porque um zip de 1MB pode dizer 100GB, e os bytes
    realmente escritos durante a copia, porque quem escreve o declarado e o zip.
    """
    buffer = io.BytesIO(data)
    if not zipfile.is_zipfile(buffer):
        raise Invalid("o corpo nao e um zip; .cbz tambem e zip, o nome nao decide")

    with zipfile.ZipFile(buffer) as archive:
        infos = archive.infolist()
        entries = safe_archive_entries([info.filename for info in infos])
        if entries is None:
            raise Invalid("o arquivo tem entrada com caminho de fuga; nada foi extraido")
        if not entries:
            raise Invalid("o arquivo nao tem nenhuma imagem")
        if len(entries) > MAX_PAGES_PER_CHAPTER:
            raise Invalid(f"{len(entries)} paginas; o teto e {MAX_PAGES_PER_CHAPTER}")

        names = [name for _, name in entries]
        if len(set(names)) != len(names):
            # Duas pastas dentro do zip com a mesma pagina: gravar pelo nome-base
            # faria uma apagar a outra, e o capitulo perderia pagina em silencio.
            raise Invalid("duas entradas do arquivo tem o mesmo nome de pagina")

        declared = sum(infos[index].file_size for index, _ in entries)
        if declared > MAX_ARCHIVE_EXPANDED_BYTES:
            raise Invalid(
                f"o arquivo declara {declared} bytes descompactados;"
                f" o teto e {MAX_ARCHIVE_EXPANDED_BYTES}"
            )

        written = 0
        for index, name in entries:
            with archive.open(infos[index]) as source, (target / name).open("wb") as sink:
                while chunk := source.read(64 * 1024):
                    written += len(chunk)
                    if written > MAX_ARCHIVE_EXPANDED_BYTES:
                        raise Invalid("o descompactado passou do teto no meio da extracao")
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

    (incoming / name).write_bytes(body)
    return HTTPStatus.OK, {"file": name, "bytes": len(body)}


def _put_archive(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    """Extrai um zip/cbz inteiro na area de espera.

    Falha apaga a area de espera toda: meio zip extraido e pior que zip nenhum,
    porque parece capitulo e o `commit` aceitaria.
    """
    slug, chapter = groups
    _, incoming = _chapter_paths(ctx.cfg, slug, chapter)
    if not incoming.is_dir():
        raise Invalid("area de espera nao existe; crie o capitulo antes")

    try:
        names = extract_archive(body, incoming)
    except Exception:
        shutil.rmtree(incoming, ignore_errors=True)
        raise

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

    E a unica remocao que o painel faz, e so apaga o que ele proprio escreveu.
    """
    _, incoming = _chapter_paths(ctx.cfg, groups[0], groups[1])
    if not incoming.is_dir():
        raise Invalid("nao ha area de espera para descartar")

    removed = len(_incoming_files(incoming))
    shutil.rmtree(incoming)
    return HTTPStatus.OK, {"removed": removed}


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
    engine = str(payload.get("engine", "")) or ctx.cfg.translation.engine
    if engine not in available_engines():
        raise Invalid(f"motor {engine!r} nao existe; ha {', '.join(available_engines())}")

    directory, _ = _chapter_paths(ctx.cfg, series, chapter)
    if not directory.is_dir():
        raise Invalid(f"capitulo {series}/{chapter} nao existe; promova a area de espera antes")

    try:
        job = ctx.jobs.start(
            ctx.cfg,
            series=directory.parent.name,
            chapter=directory.name,
            engine=engine,
            force=bool(payload.get("force")),
            dry_run=bool(payload.get("dry_run")),
        )
    except Busy as error:
        return HTTPStatus.CONFLICT, {"error": str(error)}

    return HTTPStatus.ACCEPTED, {"job_id": job.id, "job": job.snapshot()}


def _list_jobs(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    return HTTPStatus.OK, {"jobs": [job.snapshot() for job in ctx.jobs.recent()]}


def _get_job(ctx: Context, groups: tuple[str, ...], body: bytes) -> tuple[int, object]:
    job = ctx.jobs.get(groups[0])
    if job is None:
        return HTTPStatus.NOT_FOUND, {"error": f"job {groups[0]!r} nao existe neste processo"}
    return HTTPStatus.OK, job.snapshot()


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
    Route("POST", re.compile(rf"^/api/series/{_SLUG}/chapters$"), _create_chapter, writes=True),
    Route(
        "PUT",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/files/{_SLUG}$"),
        _put_page,
        MAX_PAGE_BYTES,
        writes=True,
        body_required=True,
    ),
    Route(
        "POST",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/archive$"),
        _put_archive,
        MAX_ARCHIVE_BYTES,
        writes=True,
        body_required=True,
    ),
    Route(
        "POST",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/commit$"),
        _commit_chapter,
        writes=True,
    ),
    Route("GET", re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/incoming$"), _get_incoming),
    Route("GET", re.compile(r"^/api/jobs$"), _list_jobs),
    Route("POST", re.compile(r"^/api/jobs$"), _create_job, writes=True, body_required=True),
    Route("GET", re.compile(rf"^/api/jobs/{_SLUG}$"), _get_job),
    Route(
        "DELETE",
        re.compile(rf"^/api/series/{_SLUG}/chapters/{_SLUG}/incoming$"),
        _delete_incoming,
        access="owner",
        writes=True,
    ),
)
"""Toda rota diz quem pode chama-la e se ela escreve.

As `owner` sao as que destroem ou mudam o que vale para o acervo inteiro: apagar
a area de espera, reescrever `series.json`, trocar a capa e editar o glossario.
Um testador nao precisa de nenhuma delas para ver a ferramenta funcionando, e dar
qualquer uma seria dar a ele a chave do acervo do dono."""


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
    expires_at = (datetime.now(UTC) + timedelta(hours=TESTER_SESSION_HOURS)).isoformat(
        timespec="seconds"
    )
    user = create_user(connection, kind="tester", expires_at=expires_at)
    token, session_expires = issue(connection, user.id, hours=TESTER_SESSION_HOURS)
    return Session(user=user, expires_at=session_expires), token


def _internal_redirect(cfg: Config, path: Path) -> str | None:
    """O caminho interno que o proxy serve, ou None para transmitir daqui.

    O prefixo mapeia a raiz do projeto dentro do proxy e NAO e alcancavel de fora -
    se for, toda a autorizacao deste modulo passa a ser decorativa, porque o
    caminho direto responde sem passar por aqui.
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


def make_panel_handler(cfg: Config, jobs: JobRegistry | None = None) -> type[ReaderHandler]:
    """Handler que serve o leitor e, para a propria maquina, tambem o painel.

    Estende o `ReaderHandler`: pedido que nao casa com `/api/` cai no `super()` e
    e servido como arquivo, exatamente como antes.

    `jobs` existe para o teste poder trocar o registro por um que nao dispara o
    pipeline de verdade. Em producao o default e o unico caminho.
    """

    registry = jobs or JobRegistry()

    class PanelHandler(ReaderHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            # A PWA e a vitrine saem da raiz do projeto. `library/` e `output/`
            # deixaram de ser servidos por caminho, entao esta base so sobra para o
            # leitor local - que continua sendo o uso principal desta ferramenta.
            self.content_root = cfg.library_dir.parent
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

            if session is None and route.writes and route.access != "public":
                # A sessao anonima nasce aqui, na primeira escrita, e nao no
                # primeiro `GET`: a home e publica e robo de busca a visita.
                session, token = _open_tester_session(connection)
                issued = token

            if session is None and route.access != "public":
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "entre para continuar"})
                return True
            if route.access == "owner" and (session is None or not session.is_owner):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "so o dono faz isso"})
                return True

            body = self._read_body(route)
            if body is None:
                return True

            context = Context(
                cfg=area_config(cfg, session.user) if session else cfg,
                base=cfg,
                jobs=registry,
                connection=connection,
                session=session,
                token=token,
                client_ip=self.client_address[0] if self.client_address else "",
            )

            try:
                status, payload = route.handler(context, groups, body)
            except Invalid as error:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
                return True
            except Exception:  # noqa: BLE001 - erro nosso vira 500, nunca stack na resposta
                self.log_error("falha em %s %s", method, path)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "falha no painel"})
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

        def _read_body(self, route: Route) -> bytes | None:
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
            return self.rfile.read(length)

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
