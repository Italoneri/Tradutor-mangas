from __future__ import annotations

import io
import json
import threading
import zipfile
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
    TESTER_MAX_SERIES,
    TESTER_MAX_PAGES_PER_CHAPTER,
    TESTER_MAX_UPLOAD_BYTES,
    OutOfSpace,
    QuotaExceeded,
    area_bytes,
    check_disk,
    check_engine,
    check_incoming_pages,
    check_new_chapter,
    check_new_series,
    check_upload_bytes,
    engines_for,
)
from .serving import _Server
from .sessions import TESTER_SIGNUPS_PER_IP_PER_HOUR

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



def test_stops_the_tester_at_the_series_limit(area):
    """Serie vazia quase nao tem bytes, entao a cota de disco nao a segurava."""
    for number in range(TESTER_MAX_SERIES):
        (area.library_dir / f"Obra {number}").mkdir(parents=True)

    with pytest.raises(QuotaExceeded, match=str(TESTER_MAX_SERIES)):
        check_new_series(area, TESTER)


def test_never_stops_the_owner_creating_series(area):
    for number in range(TESTER_MAX_SERIES + 5):
        (area.library_dir / f"Obra {number}").mkdir(parents=True)

    check_new_series(area, OWNER)


def test_lets_the_tester_below_the_series_limit(area):
    check_new_series(area, TESTER)
    (area.library_dir / "Obra").mkdir(parents=True)
    check_new_series(area, TESTER)


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
    check_disk(base, OWNER, 400)
    with pytest.raises(OutOfSpace):
        check_disk(base, OWNER, 600)


@pytest.mark.parametrize(
    ("name", "user", "incoming", "refused"),
    [
        ("testador cabe abaixo da fatia dele", TESTER, 100, False),
        ("testador para em 80% do teto", TESTER, 300, True),
        ("dono passa dos 80%", OWNER, 300, False),
        ("dono para no teto inteiro", OWNER, 600, True),
    ],
)
def test_reserves_the_last_slice_of_the_disk_for_the_owner(
    tmp_path, monkeypatch, name: str, user: User, incoming: int, refused: bool
):
    base = Config(root=tmp_path)
    page = base.data_dir / "users" / ("c" * 32) / "library" / "p.jpg"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"x" * 1000)
    monkeypatch.setenv(DISK_CEILING_ENV, "1500")

    if refused:
        with pytest.raises(OutOfSpace):
            check_disk(base, user, incoming)
    else:
        check_disk(base, user, incoming)


# ---------- pelo servidor de verdade ----------


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    """Com a vitrine ligada: e so nela que a primeira escrita sem sessao abre um
    testador. Desligada, a mesma escrita leva 401 - e o que a Fase 7.1 corrigiu."""
    monkeypatch.setenv("PUBLIC_SHOWCASE", "1")
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


# ---------- 7.3 a cota vale para o zip aberto e para uploads em paralelo ----------


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _tester_with_chapter(server: Client) -> Client:
    tester = _tester_with_series(server)
    status, _ = tester.send(
        "POST", "/api/series/Minha/chapters", json.dumps({"chapter": "001"}).encode()
    )
    assert status == 201
    return tester


def _tester_area(tmp_path: Path) -> Path:
    users = [
        entry for entry in (tmp_path / "data" / "users").iterdir() if (entry / "library").is_dir()
    ]
    assert len(users) == 1
    return users[0]


def test_refuses_an_archive_that_grows_past_the_quota_when_opened(server: Client, tmp_path: Path):
    """Zeros comprimem quase a nada: o zip cabe folgado nos 40MB, e aberto passa."""
    tester = _tester_with_chapter(server)
    page = bytes.fromhex("ffd8ffe0") + b"\0" * (5 * 1024 * 1024)
    archive = _zip({f"p{n:02d}.jpg": page for n in range(10)})
    assert len(archive) < TESTER_MAX_UPLOAD_BYTES // 10

    status, body = tester.send("POST", "/api/series/Minha/chapters/001/archive", archive)

    assert status == 429, body
    assert not (_tester_area(tmp_path) / "library" / "Minha" / "001.incoming").exists()


def test_refuses_an_archive_with_a_disguised_page(server: Client, tmp_path: Path):
    tester = _tester_with_chapter(server)
    archive = _zip({"1.jpg": JPEG, "2.jpg": b"MZ\x90\x00" + b"\0" * 64})

    status, body = tester.send("POST", "/api/series/Minha/chapters/001/archive", archive)

    assert status == 422
    assert b"2.jpg" in body
    assert not (_tester_area(tmp_path) / "library" / "Minha" / "001.incoming").exists()


def test_counts_the_pages_already_waiting_against_the_archive(server: Client):
    tester = _tester_with_chapter(server)
    for number in range(TESTER_MAX_PAGES_PER_CHAPTER - 1):
        tester.send("PUT", f"/api/series/Minha/chapters/001/files/p{number:04d}.jpg", JPEG)

    archive = _zip({"z1.jpg": JPEG, "z2.jpg": JPEG})
    status, _ = tester.send("POST", "/api/series/Minha/chapters/001/archive", archive)

    assert status == 422


def test_keeps_the_area_inside_the_quota_under_parallel_uploads(server: Client, tmp_path: Path):
    """Dez `PUT` ao mesmo tempo passavam todos pela conferencia antes de gravar."""
    tester = _tester_with_chapter(server)
    page = bytes.fromhex("ffd8ffe0") + b"1" * (5 * 1024 * 1024)
    barrier = threading.Barrier(10)
    codes: list[int] = []

    def upload(number: int) -> None:
        client = Client(server.port)
        client.cookie = tester.cookie
        barrier.wait()
        codes.append(
            client.send("PUT", f"/api/series/Minha/chapters/001/files/p{number:02d}.jpg", page)[0]
        )

    threads = [threading.Thread(target=upload, args=(n,)) for n in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(set(codes)) == [200, 429]
    used = sum(
        path.stat().st_size for path in _tester_area(tmp_path).rglob("*") if path.is_file()
    )
    assert used <= TESTER_MAX_UPLOAD_BYTES


# ---------- 7.4 o teto global nao tranca o dono ----------


def test_lets_the_owner_upload_after_testers_fill_their_share(
    server: Client, tmp_path: Path, monkeypatch
):
    tester = _tester_with_series(server)
    assert server.login() == 200
    assert server.send("POST", "/api/series", json.dumps({"slug": "Minha"}).encode())[0] == 201

    occupied = tmp_path / "data" / "users" / ("d" * 32) / "library" / "grande.jpg"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"x" * 1300)
    monkeypatch.setenv(DISK_CEILING_ENV, "1500")

    chapter = json.dumps({"chapter": "001"}).encode()
    assert tester.send("POST", "/api/series/Minha/chapters", chapter)[0] == 507
    assert server.send("POST", "/api/series/Minha/chapters", chapter)[0] == 201


def test_limits_how_many_tester_sessions_one_address_opens(server: Client):
    """Apagar o cookie dava outra sessao, outra cota e outro lugar na fila."""
    codes = [
        Client(server.port).send("POST", "/api/series", json.dumps({"slug": "Minha"}).encode())[0]
        for _ in range(TESTER_SIGNUPS_PER_IP_PER_HOUR + 1)
    ]

    assert codes[:TESTER_SIGNUPS_PER_IP_PER_HOUR] == [201] * TESTER_SIGNUPS_PER_IP_PER_HOUR
    assert codes[-1] == 429
