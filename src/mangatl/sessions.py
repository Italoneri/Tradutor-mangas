"""Quem esta pedindo: sessao por cookie, senha do dono e as duas travas na porta.

Tres coisas que este modulo decide e que nao sao detalhe:

**O banco guarda o hash do token, nunca o token.** Um vazamento do arquivo
`mangatl.db` entrega hashes, e hash nao abre sessao de ninguem. E a mesma razao
pela qual ninguem guarda senha em claro; sessao viva vale tanto quanto senha.

**A sessao do testador nasce no primeiro upload, nao no primeiro acesso.** Quem
chama `issue` e a rota que escreve. Com a vitrine publica, a home e uma pagina
aberta que robo de busca, previa de link e monitor de uptime visitam; criar
usuario a cada `GET` encheria a tabela de visitante que nunca vai subir nada e
daria a cada robo uma area em disco.

**CSRF exige cabecalho E origem.** `SameSite=Lax` sozinho deixa passar `POST` de
formulario em outro site, que e exatamente a forma de CSRF que interessa aqui.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from .accounts import User, UserKind, user_from_row
from .db import in_hours, minutes_ago, now

COOKIE_NAME = "sid"
"""Nome curto e generico de proposito: um cookie chamado `mangatl_session`
anuncia o que roda aqui para quem so olhou o cabecalho."""

TOKEN_BYTES = 32
"""256 bits de `secrets.token_urlsafe`. Adivinhar esta fora de questao e o
tamanho nao pesa em cabecalho nenhum."""

TESTER_SESSION_HOURS = 48
"""Prazo da sessao anonima. Mesmo numero do prazo do usuario `tester`: a sessao
nao pode sobreviver ao dono dela."""

OWNER_SESSION_HOURS = 24 * 30
"""O dono entra de novo uma vez por mes. Mais curto vira atrito diario para quem
usa a ferramenta todo dia; mais longo e cookie que vale quase para sempre."""

LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_MINUTES = 15
"""Cinco erros em quinze minutos. Quem digitou errado tenta de novo; quem esta
varrendo senha para no quinto e leva 429 - sem isto a senha e questao de tempo."""

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_KEY_BYTES = 32
"""Parametros do `hashlib.scrypt`. N=32768 custa ~32MB e fracao de segundo por
verificacao: caro o bastante para tornar forca bruta offline inviavel, barato o
bastante para o login nao parecer travado."""

SCRYPT_MAX_MEMORY = 256 * 1024 * 1024
"""Teto de memoria passado ao OpenSSL, e nao um parametro do KDF.

O default da biblioteca e 32MB e recusa N=32768 com `memory limit exceeded` -
custo que se queria caro visto como abuso. O teto generoso aqui tambem limita o
estrago se um registro do banco pedir um N absurdo."""


def _secure_cookies() -> bool:
    """Se o cookie sai marcado `Secure`.

    Ligado por padrao porque o alvo e uma instancia hospedada em HTTPS. Desligar
    so faz sentido no desenvolvimento em `http://`, onde um cookie `Secure` nunca
    seria devolvido pelo navegador e a sessao pareceria nao existir.
    """
    return os.environ.get("COOKIE_SECURE", "1").strip().lower() not in {"0", "false", "no"}


# ---------- senha ----------


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def hash_password(password: str) -> str:
    """`scrypt$N$r$p$sal$chave`, tudo em base64 sem padding.

    Os parametros viajam junto com o hash para poderem subir depois sem invalidar
    o que ja esta gravado: quem verifica le o custo do proprio registro, e nao de
    uma constante que mudou.
    """
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_BYTES,
        maxmem=SCRYPT_MAX_MEMORY,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64encode(salt)}${_b64encode(key)}"


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4))


def verify_password(password: str, encoded: str | None) -> bool:
    """Se a senha casa. Falso para hash ausente ou ilegivel, nunca excecao.

    `compare_digest` e nao `==`: comparacao de string sai no primeiro byte
    diferente, e o tempo dela conta quantos bytes bateram.
    """
    if not encoded:
        return False

    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False

    try:
        n, r, p = (int(part) for part in parts[1:4])
        salt, expected = _b64decode(parts[4]), _b64decode(parts[5])
        found = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=SCRYPT_MAX_MEMORY,
        )
    except (ValueError, MemoryError):
        return False

    return hmac.compare_digest(found, expected)


# ---------- sessao ----------


def token_hash(token: str) -> str:
    """O que vai para o banco. sha256 puro basta: o token ja tem 256 bits de
    entropia, entao nao ha dicionario para derivar - o que scrypt existe para
    impedir em senha nao se aplica aqui."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    user: User
    expires_at: str

    @property
    def is_owner(self) -> bool:
        return self.user.is_owner


def hours_for(kind: UserKind) -> int:
    return OWNER_SESSION_HOURS if kind == "owner" else TESTER_SESSION_HOURS


def issue(connection: sqlite3.Connection, user_id: str, *, hours: int) -> tuple[str, str]:
    """Abre uma sessao e devolve `(token, expires_at)`.

    O token so existe aqui e no cookie: o banco recebe o hash, e nao ha como
    recuperar o valor depois - o que significa que uma sessao perdida nao e
    recuperavel, e isso esta certo.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = in_hours(hours)
    moment = now()
    connection.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen)"
        " VALUES (?, ?, ?, ?, ?)",
        (token_hash(token), user_id, moment, expires_at, moment),
    )
    return token, expires_at


def resolve(connection: sqlite3.Connection, token: str | None) -> Session | None:
    """A sessao viva desse token, ou None.

    Um `JOIN` e nao duas consultas: sessao sem usuario e um estado que a cascata
    do banco ja torna impossivel, e conferir de novo aqui seria codigo defendendo
    contra algo que nao pode acontecer.

    `last_seen` e atualizado no caminho, e nao ha escrita fora disso: e o unico
    dado que diz quais sessoes estao de fato em uso.
    """
    if not token:
        return None

    row = connection.execute(
        """
        SELECT users.*, sessions.expires_at AS session_expires_at
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token_hash = ? AND sessions.expires_at > ?
        """,
        (token_hash(token), now()),
    ).fetchone()
    if row is None:
        return None

    connection.execute(
        "UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now(), token_hash(token))
    )
    return Session(user=user_from_row(row), expires_at=row["session_expires_at"])


def revoke(connection: sqlite3.Connection, token: str | None) -> None:
    if token:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def purge_expired_sessions(connection: sqlite3.Connection) -> int:
    cursor = connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
    return cursor.rowcount


# ---------- cookie ----------


def cookie_header(token: str, *, hours: int) -> str:
    """O `Set-Cookie` da sessao.

    `HttpOnly` tira o token do alcance de qualquer script, que e o que transforma
    um XSS em incomodo em vez de roubo de sessao. `SameSite=Lax` cobre navegacao
    de terceiro sem quebrar o link que alguem mandou no WhatsApp. `Max-Age` junto
    de `Path=/` para o cookie morrer sozinho mesmo se o servidor sumir.
    """
    parts = [
        f"{COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={hours * 3600}",
    ]
    if _secure_cookies():
        parts.append("Secure")
    return "; ".join(parts)


def clearing_cookie_header() -> str:
    parts = [f"{COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
    if _secure_cookies():
        parts.append("Secure")
    return "; ".join(parts)


def token_from_cookies(raw: str | None) -> str | None:
    """O token do cabecalho `Cookie`, ou None.

    `SimpleCookie` levanta em cabecalho malformado, e cabecalho malformado chega -
    de robo, de proxy velho e de quem esta testando. Isso e ausencia de sessao,
    nao erro de servidor.
    """
    if not raw:
        return None
    try:
        jar = SimpleCookie()
        jar.load(raw)
    except Exception:  # noqa: BLE001 - cookie quebrado e "sem sessao", nao 500
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel and morsel.value else None


# ---------- limite de tentativas ----------


def record_login_attempt(connection: sqlite3.Connection, subject: str) -> None:
    connection.execute(
        "INSERT INTO login_attempts (subject, at) VALUES (?, ?)", (subject.lower(), now())
    )


def recent_login_attempts(connection: sqlite3.Connection, subject: str) -> int:
    since = minutes_ago(LOGIN_ATTEMPT_WINDOW_MINUTES)
    row = connection.execute(
        "SELECT count(*) AS n FROM login_attempts WHERE subject = ? AND at > ?",
        (subject.lower(), since),
    ).fetchone()
    return row["n"]


def login_is_throttled(connection: sqlite3.Connection, subjects: tuple[str, ...]) -> bool:
    """Se alguma das chaves ja estourou o limite.

    Duas chaves, IP e e-mail, porque cada uma sozinha tem um buraco: so por IP,
    uma botnet distribui a varredura; so por e-mail, um atacante tranca a conta do
    dono de proposito. Juntas, nenhuma das duas paga o preco inteiro.
    """
    return any(recent_login_attempts(connection, subject) >= LOGIN_ATTEMPT_LIMIT for subject in subjects)


def clear_login_attempts(connection: sqlite3.Connection, subjects: tuple[str, ...]) -> None:
    """Login certo zera o contador. Sem isso, quem errou quatro vezes e acertou
    continuaria a um erro do bloqueio pelos quinze minutos seguintes."""
    for subject in subjects:
        connection.execute("DELETE FROM login_attempts WHERE subject = ?", (subject.lower(),))


def clear_every_login_attempt(connection: sqlite3.Connection) -> int:
    """Zera o limite de tentativas inteiro. So a linha de comando chama isto.

    E a porta dos fundos do dono: a chave `email:` existe para ninguem varrer a
    senha dele, e por isso mesmo qualquer um pode tranca-la de proposito. Quem tem
    shell na maquina nao precisa esperar quinze minutos para entrar.
    """
    return connection.execute("DELETE FROM login_attempts").rowcount


TESTER_SIGNUPS_PER_IP_PER_HOUR = 5
"""Sessoes de teste novas que um mesmo endereco abre por hora.

Sem teto, a cota por sessao nao limita nada: quem apaga o cookie ganha outra
sessao, outros 40MB e outro lugar na fila. Cinco cobrem uma casa com varios
aparelhos atras do mesmo roteador."""


def _ip_hash(ip: str) -> str:
    """O IP como hash: o banco conta, mas nao guarda endereco de ninguem."""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def tester_signup_allowed(connection: sqlite3.Connection, ip: str) -> bool:
    row = connection.execute(
        "SELECT count(*) AS n FROM tester_signups WHERE ip_hash = ? AND at > ?",
        (_ip_hash(ip), minutes_ago(60)),
    ).fetchone()
    return row["n"] < TESTER_SIGNUPS_PER_IP_PER_HOUR


def record_tester_signup(connection: sqlite3.Connection, ip: str) -> None:
    connection.execute(
        "INSERT INTO tester_signups (ip_hash, at) VALUES (?, ?)", (_ip_hash(ip), now())
    )


def purge_old_tester_signups(connection: sqlite3.Connection) -> int:
    return connection.execute(
        "DELETE FROM tester_signups WHERE at <= ?", (minutes_ago(60),)
    ).rowcount


def purge_old_login_attempts(connection: sqlite3.Connection) -> int:
    since = minutes_ago(LOGIN_ATTEMPT_WINDOW_MINUTES)
    cursor = connection.execute("DELETE FROM login_attempts WHERE at <= ?", (since,))
    return cursor.rowcount


# ---------- CSRF ----------

REQUESTED_WITH = "X-Requested-With"

_EMAIL = re.compile(r"\A[^@\s]+@[^@\s.]+\.[^@\s]+\Z")


def looks_like_email(value: str) -> bool:
    """Suficiente para recusar lixo antes de tocar no banco.

    Nao tenta ser RFC 5322: validar e-mail por expressao regular e um beco sem
    saida conhecido, e o unico endereco que importa aqui e o que o dono digitou no
    `OWNER_EMAIL`."""
    return bool(_EMAIL.match(value.strip())) and len(value.strip()) <= 254


def origin_matches(origin_or_referer: str | None, host: str | None) -> bool:
    """Se o pedido veio de uma pagina deste mesmo host.

    Ausente responde False: navegador manda `Origin` em todo pedido que escreve, e
    a ausencia e sinal de cliente que nao e navegador - que nao tem por que usar as
    rotas de escrita do painel.
    """
    if not origin_or_referer or not host:
        return False
    if origin_or_referer == "null":
        return False
    parsed = urlsplit(origin_or_referer)
    return bool(parsed.netloc) and parsed.netloc == host


def csrf_is_valid(headers: dict[str, str | None]) -> bool:
    """As duas travas: cabecalho que so script da propria pagina poe, e origem.

    O cabecalho sozinho ja barra formulario HTML de outro site, que nao consegue
    mandar cabecalho nenhum. A origem sozinha ja barra o mesmo caso. As duas juntas
    porque cada uma some num cenario diferente: proxy que corta cabecalho
    desconhecido, e cliente que nao manda `Origin`.
    """
    if not headers.get(REQUESTED_WITH):
        return False
    host = headers.get("Host")
    return origin_matches(headers.get("Origin"), host) or origin_matches(
        headers.get("Referer"), host
    )
