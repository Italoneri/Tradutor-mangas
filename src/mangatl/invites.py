"""Convites: o dono escolhe quem ganha conta, e o link e a unica copia do token.

Entrada por convite, e nao cadastro aberto, porque convite dispensa servidor de
e-mail: quem recebeu o link ja foi escolhido pelo dono, entao nao ha endereco a
confirmar. E porque ha um worker so, e a fila de todo mundo nao pode depender de
quantos desconhecidos apareceram no dia.

O token segue a regra das sessoes: o banco guarda o sha256, e o valor so existe
no link. O link leva o token no fragmento (`#`), e nao na query - fragmento nao
chega ao servidor no pedido da pagina nem aparece no log do Caddy.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Literal

from .accounts import User, create_user, find_account, get_user
from .db import in_hours, now, transaction
from .sessions import token_hash

INVITE_DAYS = 7
INVITE_PAGE = "/reader/convite.html"
MAX_NOTE_CHARS = 200
"""Lembrete do dono ("para o Joao"), nao um campo de texto livre."""

LOCAL_ORIGIN = "http://localhost:8000"


class InviteRefused(RuntimeError):
    """Token que nao existe, venceu ou ja foi usado.

    Uma excecao so para os tres casos, e uma resposta so: a diferenca entre "nao
    existe" e "ja foi usado" diria a quem varre quais tokens sao reais."""


class EmailTaken(RuntimeError):
    """O e-mail ja tem conta. So chega aqui quem tem um convite valido na mao."""


InviteState = Literal["open", "used", "expired"]


@dataclass(frozen=True)
class Invite:
    id: str
    """O hash do token. Identifica o convite para o dono revogar, e nao abre nada."""

    note: str | None
    created_at: str
    expires_at: str
    used_at: str | None
    used_by_email: str | None

    @property
    def state(self) -> InviteState:
        if self.used_at is not None:
            return "used"
        return "expired" if self.expires_at <= now() else "open"


def public_origin() -> str:
    """O endereco que entra no link: o mesmo `SITE_ADDRESS` que o Caddy atende.

    Sem dominio (vazio, ou `:80` da bateria do proxy) o link aponta para a porta
    local, que e onde o app responde fora do contêiner.
    """
    site = os.environ.get("SITE_ADDRESS", "").strip()
    if not site or site.startswith(":"):
        return LOCAL_ORIGIN
    return site if "://" in site else f"https://{site}"


def invite_link(token: str) -> str:
    return f"{public_origin()}{INVITE_PAGE}#{token}"


def create_invite(
    connection: sqlite3.Connection, created_by: User, note: str | None = None
) -> tuple[str, Invite]:
    """Grava o convite e devolve `(token, convite)`. O token nao volta mais depois."""
    note = (note or "").strip()[:MAX_NOTE_CHARS] or None
    token = secrets.token_urlsafe(32)
    invite = Invite(
        id=token_hash(token),
        note=note,
        created_at=now(),
        expires_at=in_hours(INVITE_DAYS * 24),
        used_at=None,
        used_by_email=None,
    )
    connection.execute(
        "INSERT INTO invites (token_hash, created_by, created_at, expires_at, note)"
        " VALUES (?, ?, ?, ?, ?)",
        (invite.id, created_by.id, invite.created_at, invite.expires_at, invite.note),
    )
    return token, invite


def list_invites(connection: sqlite3.Connection, limit: int = 100) -> list[Invite]:
    """Os convites mais recentes, abertos e usados. E-mail de quem usou, e nada mais."""
    rows = connection.execute(
        """
        SELECT invites.*, users.email AS used_by_email
        FROM invites LEFT JOIN users ON users.id = invites.used_by
        ORDER BY invites.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        Invite(
            id=row["token_hash"],
            note=row["note"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            used_at=row["used_at"],
            used_by_email=row["used_by_email"],
        )
        for row in rows
    ]


def revoke_invite(connection: sqlite3.Connection, invite_id: str) -> bool:
    """Apaga um convite que ninguem usou. O usado fica: e o registro de quem entrou."""
    cursor = connection.execute(
        "DELETE FROM invites WHERE token_hash = ? AND used_at IS NULL", (invite_id,)
    )
    return cursor.rowcount > 0


def accept_invite(
    connection: sqlite3.Connection,
    *,
    token: str,
    email: str,
    password_hash: str,
    tester: User | None = None,
) -> User:
    """Troca um convite valido por uma conta de membro. Tudo ou nada.

    Com `tester`, a conta nova **e o mesmo usuario**: o id nao muda, entao a area
    em disco, os capitulos e os jobs continuam dele sem `mv` nenhum. Quem testou e
    gostou nao perde o que subiu.

    O hash da senha chega pronto, e nao a senha: o scrypt custa ~100ms e nao tem
    por que rodar com a trava de escrita do banco na mao.
    """
    with transaction(connection):
        found = connection.execute(
            "SELECT token_hash FROM invites"
            " WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
            (token_hash(token), now()),
        ).fetchone()
        if found is None:
            raise InviteRefused("convite invalido, vencido ou ja usado")
        if find_account(connection, email) is not None:
            raise EmailTaken("este e-mail ja tem conta; entre com ele")

        member = (
            _promote(connection, tester, email, password_hash)
            if tester is not None
            else create_user(connection, kind="member", email=email, password_hash=password_hash)
        )
        connection.execute(
            "UPDATE invites SET used_by = ?, used_at = ? WHERE token_hash = ?",
            (member.id, now(), found["token_hash"]),
        )
    return member


def _promote(connection: sqlite3.Connection, tester: User, email: str, password_hash: str) -> User:
    connection.execute(
        "UPDATE users SET kind = 'member', email = ?, password_hash = ?, expires_at = NULL"
        " WHERE id = ? AND kind = 'tester'",
        (email.strip().lower(), password_hash, tester.id),
    )
    promoted = get_user(connection, tester.id)
    if promoted is None or promoted.kind != "member":
        raise InviteRefused("a sessao de teste expirou antes de aceitar o convite")
    return promoted
