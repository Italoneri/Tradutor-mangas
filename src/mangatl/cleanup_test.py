from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from .cleanup import (
    INCOMING_MAX_AGE_DAYS,
    UPLOAD_MAX_AGE_SECONDS,
    sweep_incoming,
    sweep_spool,
)
from .config import Config

NOW = time.time()


def _aged(path: Path, seconds: float) -> Path:
    os.utime(path, (NOW - seconds, NOW - seconds))
    return path


@pytest.mark.parametrize(
    ("name", "age", "survives"),
    [
        ("upload recente fica", 60, True),
        ("upload de processo morto some", UPLOAD_MAX_AGE_SECONDS + 60, False),
    ],
)
def test_sweeps_only_the_old_orphan_uploads(tmp_path: Path, name: str, age: float, survives: bool):
    cfg = Config(root=tmp_path)
    spool = cfg.data_dir / "uploads"
    spool.mkdir(parents=True)
    upload = spool / "tmp123.upload"
    upload.write_bytes(b"meio zip")
    _aged(upload, age)

    sweep_spool(cfg, moment=NOW)

    assert upload.exists() == survives, name


def test_leaves_other_files_in_the_spool_alone(tmp_path: Path):
    cfg = Config(root=tmp_path)
    spool = cfg.data_dir / "uploads"
    spool.mkdir(parents=True)
    other = spool / "leiame.txt"
    other.write_text("x")
    _aged(other, UPLOAD_MAX_AGE_SECONDS * 10)

    sweep_spool(cfg, moment=NOW)

    assert other.exists()


@pytest.mark.parametrize(
    ("name", "age_days", "survives"),
    [
        ("area de espera em uso fica", 1, True),
        ("area de espera abandonada some", INCOMING_MAX_AGE_DAYS + 1, False),
    ],
)
def test_sweeps_only_the_abandoned_incoming_areas(
    tmp_path: Path, name: str, age_days: int, survives: bool
):
    cfg = Config(root=tmp_path)
    series = cfg.data_dir / "users" / ("a" * 32) / "library" / "Obra"
    incoming = series / "002.incoming"
    incoming.mkdir(parents=True)
    (incoming / "p1.jpg").write_bytes(b"x")
    chapter = series / "001"
    chapter.mkdir()
    _aged(incoming, age_days * 24 * 60 * 60)
    _aged(chapter, (INCOMING_MAX_AGE_DAYS + 30) * 24 * 60 * 60)

    sweep_incoming(cfg, moment=NOW)

    assert incoming.exists() == survives, name
    assert chapter.exists(), "capitulo promovido nunca e lixo, por mais velho que seja"
