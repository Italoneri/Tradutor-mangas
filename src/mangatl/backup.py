"""Copia de seguranca de `data/`: o banco e o acervo de todas as contas.

Dois arquivos e nao um, porque as duas metades pedem copias diferentes:

- **o banco** sai pela API de backup do sqlite. Copiar `mangatl.db` com o WAL
  aberto nao e backup: as escritas recentes moram no `-wal`, e o arquivo copiado
  sozinho e um banco de minutos atras - ou corrompido, se a copia pegar o meio de
  um checkpoint;
- **o acervo** e so arquivo, e vai num `tar.gz` de `data/users/`.

Roda com o servidor de pe. A API de backup le uma fotografia consistente do banco
mesmo com o `app` e o `worker` escrevendo.
"""

from __future__ import annotations

import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .accounts import users_root
from .config import Config
from .db import database_path

BACKUPS_DIRNAME = "backups"
DATABASE_COPY = "mangatl.db"
LIBRARY_ARCHIVE = "users.tar.gz"


@dataclass(frozen=True)
class Backup:
    directory: Path
    database: Path
    library: Path | None
    """None quando ainda nao ha conta com acervo nenhum em disco."""


def default_backup_dir(cfg: Config, moment: datetime | None = None) -> Path:
    stamp = (moment or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return cfg.data_dir / BACKUPS_DIRNAME / stamp


def backup(cfg: Config, target: Path | None = None) -> Backup:
    """Grava o banco e o acervo em `target`, ou numa pasta nova com a hora de agora."""
    source = database_path(cfg)
    if not source.is_file():
        raise FileNotFoundError(f"nao ha banco em {source}; nada para copiar")

    directory = target or default_backup_dir(cfg)
    directory.mkdir(parents=True, exist_ok=False)

    database = directory / DATABASE_COPY
    _copy_database(source, database)
    return Backup(directory=directory, database=database, library=_archive_library(cfg, directory))


def _copy_database(source: Path, destination: Path) -> None:
    reader = sqlite3.connect(source)
    writer = sqlite3.connect(destination)
    try:
        with writer:
            reader.backup(writer)
    finally:
        writer.close()
        reader.close()


def _archive_library(cfg: Config, directory: Path) -> Path | None:
    root = users_root(cfg)
    if not root.is_dir():
        return None
    archive = directory / LIBRARY_ARCHIVE
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(root, arcname=root.name)
    return archive
