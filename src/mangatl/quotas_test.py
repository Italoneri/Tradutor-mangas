from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from .accounts import User, config_for_user, ensure_owner
from .config import Config
from .db import connect, migrate
from .panel import make_panel_handler
from .panel_test import OWNER_EMAIL, OWNER_PASSWORD, Client
from .quotas import (
    DISK_CEILING_ENV,
    TESTER_MAX_CHAPTERS,
    TESTER_MAX_PAGES_PER_CHAPTER,
    TESTER_MAX_UPLOAD_BYTES,
    OutOfSpace,
    QuotaExceeded,
    area_bytes,
    check_disk,
    check_engine,
    check_incoming_pages,
    check_new_chapter,
    check_upload_bytes,
    engines_for,
)
from .serving import _Server

JPEG = bytes.fromhex("ffd8ffe0") + b"0" * 32

OWNER = User(id="a" * 32, kind="owner", email="dono@example.com", created_at="", expires_at=None)
TESTER = User(id="b" * 32, kind="tester", email=None, created_at="", expires_at="2030-01-01")


@pytest.fixture
def area(tmp_path):
    """Um `Config` apontado para a area de um usuario, como o handler monta."""
    cfg = config_for_user(Config(root=tmp_path), TESTER.id)
    cfg.library_dir.mkdir(parents=True)
    return cfg


# ---------- motor ----------


def test_never_offers_the_paid_engine_to_a_tester():
    assert engines_for(TESTER) == ("free",)
    assert "claude" in engines_for(OWNER)


def test_refuses_the_paid_engine_for_a_tester():
    with pytest.raises(QuotaExceeded, match="free"):
        check_engine(TESTER, "claude")

    check_engine(TESTER, "free")
    check_engine(OWNER, "claude")


# ---------- capitulos ----------


def test_stops_the_tester_at_the_chapter_limit(area):
    for number in range(TESTER_MAX_CHAPTERS):
        (area.library_dir / "Obra" / f"{number:03d}").mkdir(parents=True)

    with pytest.raises(QuotaExceeded, match=str(TESTER_MAX_CHAPTERS)):
        check_new_chapter(area, TESTER)


def test_counts_the_staging_area_as_a_chapter(area):
    """Area de espera aberta ja e disco ocupado, e a cota existe por causa do disco."""
    for number in range(TESTER_MAX_CHAPTERS):
        (area.library_dir / "Obra" / f"{number:03d}.incoming").mkdir(parents=True)

    with pytest.raises(QuotaExceeded):
        check_new_chapter(area, TESTER)


def test_never_stops_the_owner(area):
    for number in range(TESTER_MAX_CHAPTERS + 5):
        (area.library_dir / "Obra" / f"{number:03d}").mkdir(parents=True)

    check_new_chapter(area, OWNER)


# ---------- paginas ----------


def test_stops_the_tester_at_the_page_limit(area):
    incoming = area.library_dir / "Obra" / "001.incoming"
    incoming.mkdir(parents=True)
    for number in range(TESTER_MAX_PAGES_PER_CHAPTER):
        (incoming / f"p{number:04d}.jpg").write_bytes(JPEG)

    with pytest.raises(QuotaExceeded, match=str(TESTER_MAX_PAGES_PER_CHAPTER)):
        check_incoming_pages(area, TESTER, incoming)


def test_lets_a_page_be_replaced_at_the_limit(area):
    """Sobrescrever uma pagina que ja subiu nao acrescenta pagina nenhuma."""
    incoming = area.library_dir / "Obra" / "001.incoming"
    incoming.mkdir(parents=True)
    for number in range(TESTER_MAX_PAGES_PER_CHAPTER):
        (incoming / f"p{number:04d}.jpg").write_bytes(JPEG)

    check_incoming_pages(area, TESTER, incoming, adding=0)


# ---------- bytes ----------


def test_stops_the_tester_at_the_byte_limit(area):
    page = area.library_dir / "Obra" / "001" / "p0001.jpg"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"x" * (TESTER_MAX_UPLOAD_BYTES - 10))

    assert area_bytes(area) == TESTER_MAX_UPLOAD_BYTES - 10
    check_upload_bytes(area, TESTER, 10)
    with pytest.raises(QuotaExceeded, match="MB"):
        check_upload_bytes(area, TESTER, 11)


# ---------- teto de disco ----------


def test_refuses_a_new_upload_once_the_disk_is_full(tmp_path, monkeypatch):
    base = Config(root=tmp_path)
    page = base.data_dir / "users" / ("c" * 32) / "library" / "p.jpg"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"x" * 1000)

    monkeypatch.setenv(DISK_CEILING_ENV, "1500")
    check_disk(base, 400)
    with pytest.raises(OutOfSpace):
        check_disk(base, 600)


# ---------- pelo servidor de verdade ----------


@pytest.fixture
def server(tmp_path: Path):
    (tmp_path / "reader").mkdir()
    cfg = Config(root=tmp_path)
    migrate(cfg)
    with connect(cfg) as connection:
        ensure_owner(connection, OWNER_EMAIL, OWNER_PASSWORD)

    with _Server(("127.0.0.1", 0), make_panel_handler(cfg)) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield Client(httpd.server_address[1])
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def _tester_with_series(server: Client) -> Client:
    tester = Client(server.port)
    assert tester.send("POST", "/api/series", json.dumps({"slug": "Minha"}).encode())[0] == 201
    return tester


def test_answers_429_with_a_readable_reason(server: Client):
    """Cota estourada devolve o teto, e nao erro generico: quem bateu nela precisa
    saber qual e para decidir o que fazer."""
    tester = _tester_with_series(server)
    tester.send("POST", "/api/series/Minha/chapters", json.dumps({"chapter": "001"}).encode())
    for number in range(TESTER_MAX_PAGES_PER_CHAPTER):
        tester.send("PUT", f"/api/series/Minha/chapters/001/files/p{number:04d}.jpg", JPEG)

    status, body = tester.send("PUT", "/api/series/Minha/chapters/001/files/extra.jpg", JPEG)

    assert status == 429
    assert str(TESTER_MAX_PAGES_PER_CHAPTER).encode() in body


def test_answers_507_when_the_server_has_no_room(server: Client, tmp_path: Path, monkeypatch):
    tester = _tester_with_series(server)

    # O volume ja esta acima do teto quando o proximo upload chega.
    occupied = tmp_path / "data" / "users" / ("d" * 32) / "library" / "grande.jpg"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"x" * 4096)
    monkeypatch.setenv(DISK_CEILING_ENV, "1024")

    status, body = tester.send(
        "POST", "/api/series/Minha/chapters", json.dumps({"chapter": "001"}).encode()
    )

    # 507 e nao 429: o limite nao e desta pessoa, e tentar de novo nao adianta.
    assert status == 507
    assert b"espaco" in body


def test_never_limits_the_owner_the_way_it_limits_a_tester(server: Client):
    assert server.login() == 200
    assert server.send("POST", "/api/series", json.dumps({"slug": "Minha"}).encode())[0] == 201

    for number in range(TESTER_MAX_CHAPTERS + 1):
        status, _ = server.send(
            "POST", "/api/series/Minha/chapters", json.dumps({"chapter": f"{number:03d}"}).encode()
        )
        assert status == 201, number


def test_keeps_one_tester_out_of_another_testers_quota(server: Client):
    """Cota e por sessao. Sem isso, o primeiro visitante do dia gastaria a de todos."""
    first = _tester_with_series(server)
    for number in range(TESTER_MAX_CHAPTERS):
        first.send("POST", "/api/series/Minha/chapters", json.dumps({"chapter": f"{number:03d}"}).encode())

    second = _tester_with_series(server)
    status, _ = second.send(
        "POST", "/api/series/Minha/chapters", json.dumps({"chapter": "001"}).encode()
    )

    assert status == 201
