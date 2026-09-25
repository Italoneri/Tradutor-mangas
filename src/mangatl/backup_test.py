from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from .accounts import create_user
from .backup import backup
from .config import Config
from .db import connect, migrate


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(root=tmp_path)
    migrate(cfg)
    return cfg


def test_copies_the_database_with_every_row(cfg: Config):
    with connect(cfg) as connection:
        for _ in range(3):
            create_user(connection, kind="tester")

    result = backup(cfg)

    copy = sqlite3.connect(result.database)
    try:
        assert copy.execute("SELECT count(*) FROM users").fetchone()[0] == 3
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()


def test_keeps_the_rows_written_after_the_last_checkpoint(cfg: Config):
    """O motivo de nao copiar o arquivo: com o WAL aberto, o recente mora no `-wal`."""
    with connect(cfg) as keeper:
        keeper.execute("PRAGMA wal_autocheckpoint = 0")
        create_user(keeper, kind="tester")

        result = backup(cfg)

    copy = sqlite3.connect(result.database)
    try:
        assert copy.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    finally:
        copy.close()


def test_bundles_every_account_area(cfg: Config):
    page = cfg.data_dir / "users" / ("a" * 32) / "library" / "Obra" / "001" / "p1.jpg"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"pagina")

    result = backup(cfg)

    assert result.library is not None
    with tarfile.open(result.library) as bundle:
        assert f"users/{'a' * 32}/library/Obra/001/p1.jpg" in bundle.getnames()


def test_refuses_to_overwrite_an_existing_backup(cfg: Config, tmp_path: Path):
    target = tmp_path / "copia"
    backup(cfg, target)

    with pytest.raises(FileExistsError):
        backup(cfg, target)
