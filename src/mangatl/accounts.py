"""Quem e o usuario, e onde ficam os arquivos dele.

O id do usuario nunca vem do usuario: e um UUID gerado aqui e guardado na sessao.
Nome de serie e de capitulo continuam chegando pela rede e continuam passando por
`safe_component`, no painel; o id nao passa por ali porque ninguem o digita.

`user_path` e a segunda tranca. A primeira - `safe_component` - ja recusa `..` e
barra. Esta confere o resultado depois de resolver, e e a que pega o que a
primeira nao ve: symlink apontando para fora, juncao com caminho absoluto, e
qualquer normalizacao que o sistema de arquivos faca por conta propria.

Vazamento entre contas e o unico erro deste projeto que nao tem conserto depois de
acontecer, e por isso ele tem duas trancas em vez de uma.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Config
from .db import now

UserKind = Literal["owner", "tester"]

USERS_DIRNAME = "users"

_USER_ID = re.compile(r"\A[0-9a-f]{32}\Z")
"""Formato exato do `uuid4().hex`.

Recusar tudo que nao case com isto e o que torna o id inutil como caminho: `..`,
`/`, `%2e%2e` e nome de usuario inventado morrem nesta linha, antes de tocarem em
`Path`."""


def new_user_id() -> str:
    return uuid.uuid4().hex


def users_root(cfg: Config) -> Path:
    return cfg.data_dir / USERS_DIRNAME


def user_root(cfg: Config, user_id: str) -> Path | None:
    """A raiz da area do usuario, ou None se o id nao for um id."""
    if not _USER_ID.match(user_id):
        return None
    return users_root(cfg) / user_id


def user_path(cfg: Config, user_id: str, *parts: str) -> Path | None:
    """Caminho dentro da area do usuario, ou None se escapar dela.

    Resolve e confere que o resultado esta sob a raiz do usuario. Tres coisas
    morrem aqui e nao antes:

    - `joinpath` com parte absoluta descarta a base silenciosamente, e o
      resultado sai da area sem nenhum `..` aparecer no caminho;
    - symlink dentro da area apontando para fora resolve para fora;
    - a propria raiz pode estar atras de um link, entao ela tambem e resolvida
      antes da comparacao - comparar resolvido com nao-resolvido rejeitaria
      acesso legitimo.

    Devolve o caminho resolvido, e nao o montado: quem grava deve gravar no
    caminho que foi conferido, nao em outro parecido.
    """
    root = user_root(cfg, user_id)
    if root is None:
        return None

    try:
        resolved_root = root.resolve()
        resolved = root.joinpath(*parts).resolve()
    except (OSError, ValueError):
        return None

    if resolved != resolved_root and resolved_root not in resolved.parents:
        return None
    return resolved


def config_for_user(cfg: Config, user_id: str) -> Config | None:
    """O mesmo `Config`, com `library/` e `output/` na area do usuario.

    E toda a mudanca que o pipeline enxerga: `extract_chapter`, `translate_chapter`
    e o `store` continuam recebendo um `Config` e lendo `library_dir`, sem saber
    que usuarios existem. Essa indirecao e o que impede "conta" de virar um
    parametro em trinta assinaturas.
    """
    root = user_root(cfg, user_id)
    if root is None:
        return None
    return cfg.model_copy(update={"content_root": root})


# ---------- registros ----------


@dataclass(frozen=True)
class User:
    id: str
    kind: UserKind
    email: str | None
    created_at: str
    expires_at: str | None

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"


def user_from_row(row: sqlite3.Row) -> User:
    """Uma linha de `users` como registro.

    Publica porque `sessions.resolve` monta a sessao a partir de um JOIN e
    precisa da mesma conversao - duas versoes divergiriam no primeiro campo
    novo."""
    return User(
        id=row["id"],
        kind=row["kind"],
        email=row["email"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def get_user(connection: sqlite3.Connection, user_id: str) -> User | None:
    row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else user_from_row(row)


def sole_owner(connection: sqlite3.Connection) -> User | None:
    """O unico dono, ou None quando nao ha nenhum.

    Mais de um dono esta fora de escopo, e essa decisao compra uma coisa concreta:
    o CLI acha a area do dono sem ninguem digitar e-mail, e a migracao pode criar
    a conta antes de existir senha para ela. Com dois donos isto teria que virar
    um parametro em toda chamada.
    """
    row = connection.execute(
        "SELECT * FROM users WHERE kind = 'owner' ORDER BY created_at LIMIT 1"
    ).fetchone()
    return None if row is None else user_from_row(row)


def find_owner(connection: sqlite3.Connection, email: str) -> User | None:
    row = connection.execute(
        "SELECT * FROM users WHERE kind = 'owner' AND email = ?", (email.strip().lower(),)
    ).fetchone()
    return None if row is None else user_from_row(row)


def password_hash_of(connection: sqlite3.Connection, user_id: str) -> str | None:
    row = connection.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else row["password_hash"]


def create_user(
    connection: sqlite3.Connection,
    *,
    kind: UserKind,
    email: str | None = None,
    password_hash: str | None = None,
    expires_at: str | None = None,
    user_id: str | None = None,
) -> User:
    """Grava o usuario. Nao cria diretorio: quem escrever cria o seu.

    Criar a area no cadastro daria a cada robo que passa pela home um diretorio em
    disco. A area nasce no primeiro upload, junto com o primeiro arquivo.
    """
    user = User(
        id=user_id or new_user_id(),
        kind=kind,
        email=email.strip().lower() if email else None,
        created_at=now(),
        expires_at=expires_at,
    )
    connection.execute(
        "INSERT INTO users (id, kind, email, password_hash, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user.id, user.kind, user.email, password_hash, user.created_at, user.expires_at),
    )
    return user


def set_password_hash(connection: sqlite3.Connection, user_id: str, password_hash: str) -> None:
    connection.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
    )


def ensure_owner(connection: sqlite3.Connection, email: str | None, password: str | None) -> User | None:
    """Garante que existe um dono com essa credencial. Idempotente.

    Le `OWNER_EMAIL` e `OWNER_PASSWORD` uma vez, na subida, e grava so o hash: a
    senha em claro nao entra no banco nem no log. Rodar de novo com a mesma senha
    regrava o mesmo hash com sal novo, que e inofensivo; rodar com senha nova e
    como se troca a senha do dono.

    Sem e-mail e sem senha nao cria nada: a instancia sobe servindo a vitrine, e o
    painel fica sem dono ate alguem definir as duas variaveis.
    """
    from .sessions import hash_password, looks_like_email

    if not email or not password:
        return None
    if not looks_like_email(email):
        raise ValueError("OWNER_EMAIL nao parece um endereco de e-mail")

    owner = sole_owner(connection)
    if owner is None:
        return create_user(
            connection, kind="owner", email=email, password_hash=hash_password(password)
        )

    connection.execute(
        "UPDATE users SET email = ?, password_hash = ? WHERE id = ?",
        (email.strip().lower(), hash_password(password), owner.id),
    )
    return get_user(connection, owner.id)


def expired_testers(connection: sqlite3.Connection, moment: str | None = None) -> list[User]:
    rows = connection.execute(
        "SELECT * FROM users WHERE kind = 'tester' AND expires_at IS NOT NULL AND expires_at < ?",
        (moment or now(),),
    ).fetchall()
    return [user_from_row(row) for row in rows]


def area_config(cfg: Config, user: User) -> Config:
    """O `Config` que descreve a area deste usuario.

    O dono tem uma excecao, e ela e o que mantem a maquina local funcionando antes
    de `migrate-to-accounts`: enquanto o acervo dele estiver na raiz do projeto, e
    la que ele mora. Testador nao tem essa saida - a area dele nasce no primeiro
    upload e nunca foi a raiz.
    """
    derived = config_for_user(cfg, user.id)
    if derived is None:
        return cfg
    if user.is_owner and not derived.library_dir.is_dir():
        return cfg
    return derived


def owner_config(cfg: Config) -> Config:
    """O `Config` apontado para a area do dono, ou o proprio `cfg`.

    E o que faz `mangatl process` e `mangatl serve` continuarem funcionando depois
    da migracao sem ninguem passar um id na linha de comando. Antes da migracao o
    banco nem existe, e ai o acervo esta na raiz do projeto - que e onde o
    `cfg` sozinho ja aponta.
    """
    from .db import connect, database_path

    if not database_path(cfg).is_file():
        return cfg

    with connect(cfg) as connection:
        owner = sole_owner(connection)

    return cfg if owner is None else area_config(cfg, owner)


class MigrationRefused(RuntimeError):
    """A migracao parou antes de mover nada.

    Existe para separar "nao ha o que fazer" - que e sucesso e nao levanta nada -
    de "o disco esta num estado que eu nao sei interpretar", que e onde mover
    arquivo as cegas destruiria acervo."""


@dataclass(frozen=True)
class Migration:
    owner_id: str
    moved: tuple[str, ...]
    """Os diretorios que sairam da raiz. Vazio significa que ja estavam la."""


def migrate_to_accounts(cfg: Config) -> Migration:
    """Cria o dono e muda `library/` e `output/` para a area dele.

    Idempotente: rodar de novo nao move nada e nao levanta erro, porque o destino
    ja existe. Recusa, e nao presume, quando `data/users/` tem area de outro
    usuario - isso quer dizer que o servidor ja rodou aqui, e mover o acervo da
    raiz para dentro de uma dessas areas misturaria acervo de gente diferente.

    Move em vez de copiar de proposito: um capitulo pesa centenas de MB e duas
    copias do acervo e a receita para editar a errada. `shutil.move` dentro do
    mesmo volume e um rename, e portanto atomico por diretorio.
    """
    from .db import connect, migrate, transaction

    migrate(cfg)
    with connect(cfg) as connection, transaction(connection):
        owner = sole_owner(connection) or create_user(connection, kind="owner")

    root = users_root(cfg)
    if root.is_dir():
        strangers = sorted(entry.name for entry in root.iterdir() if entry.name != owner.id)
        if strangers:
            raise MigrationRefused(
                f"{root} ja tem area de {len(strangers)} outro(s) usuario(s):"
                f" {', '.join(strangers[:3])}. Mova o acervo a mao ou comece de um data/ vazio."
            )

    target = user_root(cfg, owner.id)
    moved: list[str] = []
    # `cfg.root / nome` e nao `cfg.library_dir`: este comando so faz sentido sobre
    # o acervo da raiz, e um `cfg` ja derivado apontaria para o destino.
    for name in (cfg.paths.library, cfg.paths.output):
        source, destination = cfg.root / name, target / name
        if destination.exists() or not source.is_dir():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        moved.append(name)

    return Migration(owner_id=owner.id, moved=tuple(moved))


def delete_user(cfg: Config, connection: sqlite3.Connection, user_id: str) -> int:
    """Apaga o usuario, a area dele em disco e - por cascata - sessoes e jobs.

    Devolve os bytes liberados. O disco sai primeiro: se a linha do banco sumisse
    antes e o `rmtree` falhasse, sobraria um diretorio sem dono e sem ninguem para
    tentar de novo.
    """
    area = user_root(cfg, user_id)
    freed = 0
    if area is not None and area.is_dir():
        freed = sum(path.stat().st_size for path in area.rglob("*") if path.is_file())
        shutil.rmtree(area, ignore_errors=True)
    connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return freed
