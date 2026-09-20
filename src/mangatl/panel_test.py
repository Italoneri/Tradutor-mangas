from __future__ import annotations

import io
import json
import threading
import zipfile
from http.client import HTTPConnection
from pathlib import Path

import pytest

from .accounts import ensure_owner, sole_owner, user_path
from .config import Config
from .db import connect, migrate
from .jobs import finish, get_any, set_progress
from .models import Progress
from .panel import (
    MAX_JSON_BYTES,
    ROUTES,
    Invalid,
    extract_archive,
    next_chapter_name,
    safe_archive_entries,
    image_suffix,
    json_body,
    make_panel_handler,
    match_route,
    request_path,
    safe_archive_members,
    safe_component,
    safe_page_name,
    validate_glossary,
)
from .serving import _Server


@pytest.mark.parametrize(
    ("name", "component", "expected"),
    [
        ("recusa subir um nivel", "..", None),
        ("recusa caminho relativo", "../x", None),
        ("recusa caminho absoluto", "/etc/passwd", None),
        ("recusa caminho do windows", r"C:\x", None),
        ("recusa barra no meio", "a/b", None),
        ("recusa barra invertida no meio", r"a\b", None),
        ("recusa o que o leitor esconde", ".env", None),
        ("recusa vazio", "", None),
        ("recusa o proprio diretorio", ".", None),
        ("recusa caractere de controle", "a\x00b", None),
        ("recusa nome absurdamente longo", "x" * 300, None),
        ("aceita capitulo", "001", "001"),
        ("aceita acento e espaco", "Eu me tornei a Neta Desprezada", "Eu me tornei a Neta Desprezada"),
        ("aceita ponto no meio", "vol.2", "vol.2"),
        ("aceita o limite exato", "x" * 120, "x" * 120),
    ],
)
def test_accepts_only_a_name_safe_as_a_folder(name: str, component: str, expected: str | None):
    assert safe_component(component) == expected, name


@pytest.mark.parametrize(
    ("name", "page", "expected"),
    [
        ("aceita jpg", "p0001.jpg", "p0001.jpg"),
        ("aceita maiuscula na extensao", "x.JPG", "x.JPG"),
        ("aceita png", "1.png", "1.png"),
        ("recusa executavel", "x.exe", None),
        ("recusa extensao dupla", "x.jpg.exe", None),
        ("recusa sem extensao", "x", None),
        ("recusa caminho", "a/x.jpg", None),
        ("recusa oculto", ".x.jpg", None),
    ],
)
def test_accepts_only_an_image_as_a_page(name: str, page: str, expected: str | None):
    assert safe_page_name(page) == expected, name


@pytest.mark.parametrize(
    ("name", "members", "expected"),
    [
        ("aceita paginas soltas", ["1.jpg", "2.jpg"], ["1.jpg", "2.jpg"]),
        ("aceita paginas dentro de uma pasta", ["cap/1.jpg", "cap/2.png"], ["1.jpg", "2.png"]),
        ("ignora a entrada de diretorio", ["cap/", "cap/1.jpg"], ["1.jpg"]),
        ("ignora o lixo do finder", ["__MACOSX/._1.jpg", "1.jpg"], ["1.jpg"]),
        ("ignora o que nao e pagina", ["leiame.txt", "1.jpg"], ["1.jpg"]),
        ("recusa fuga para fora da pasta", ["../../.env"], None),
        ("recusa fuga no meio do caminho", ["cap/../../x.jpg"], None),
        ("recusa caminho absoluto", ["/etc/passwd"], None),
        ("recusa caminho do windows", [r"C:\x.jpg"], None),
        ("uma entrada hostil condena o zip inteiro", ["1.jpg", "../../.env"], None),
        ("zip sem pagina nenhuma sai vazio", ["leiame.txt"], []),
    ],
)
def test_extracts_only_harmless_archive_entries(
    name: str, members: list[str], expected: list[str] | None
):
    assert safe_archive_members(members) == expected, name


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/health"),
        ("GET", "/api/series"),
        ("POST", "/api/series"),
        ("GET", "/api/series/obra/series.json"),
        ("PUT", "/api/series/obra/series.json"),
        ("PUT", "/api/series/obra/cover"),
        ("GET", "/api/series/obra/glossary"),
        ("PUT", "/api/series/obra/glossary"),
    ],
)
def test_routes_every_declared_path(method: str, path: str):
    match = match_route(method, path)
    assert match is not None and match.route is not None, f"{method} {path}"


def test_declares_no_duplicate_route():
    keys = [(route.method, route.pattern.pattern) for route in ROUTES]
    assert len(keys) == len(set(keys))


def test_captures_the_slug_from_the_path():
    match = match_route("GET", "/api/series/Eu%20me%20tornei/glossary")

    assert match is not None
    assert match.groups == ("Eu me tornei",)


def test_answers_405_and_not_404_for_the_wrong_method():
    match = match_route("DELETE", "/api/health")

    assert match is not None
    assert match.route is None
    assert match.allowed == ("GET",)


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        ("glossario comum", {"Zhuge": "Zhuge", "Sect Master": "Mestre da Seita"}, None),
        ("vazio passa", {}, None),
        ("recusa lista", ["a"], "objeto JSON"),
        ("recusa texto solto", "Zhuge", "objeto JSON"),
        ("recusa termo vazio", {"": "x"}, "nao pode ser vazio"),
        ("recusa termo so de espaco", {"  ": "x"}, "nao pode ser vazio"),
        ("recusa traducao que nao e texto", {"Zhuge": 3}, "precisa ser texto"),
        ("recusa dicionario inteiro", {str(n): "x" for n in range(501)}, "o teto e 500"),
    ],
)
def test_validates_the_glossary_at_the_edge(name: str, payload: object, expected: str | None):
    if expected is None:
        assert validate_glossary(payload) == payload, name
        return
    with pytest.raises(Invalid, match=expected):
        validate_glossary(payload)


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("jpeg", b"\xff\xd8\xff\xe0" + b"0" * 20, ".jpg"),
        ("png", b"\x89PNG\r\n\x1a\n" + b"0" * 20, ".png"),
        ("bmp", b"BM" + b"0" * 20, ".bmp"),
        ("webp", b"RIFF" + b"0000" + b"WEBP" + b"0" * 20, ".webp"),
        ("executavel disfarcado", b"MZ" + b"0" * 20, None),
        ("texto", b"nao sou imagem", None),
        ("vazio", b"", None),
    ],
)
def test_reads_the_format_from_the_bytes(name: str, data: bytes, expected: str | None):
    # Do conteudo e nao do Content-Type: o cabecalho e do cliente.
    assert image_suffix(data) == expected, name


@pytest.mark.parametrize(
    ("name", "body", "expected"),
    [
        ("objeto", b'{"a": 1}', {"a": 1}),
        ("corpo vazio vira nulo", b"", None),
    ],
)
def test_parses_the_json_body(name: str, body: bytes, expected: object):
    assert json_body(body) == expected, name


def test_refuses_a_body_that_is_not_json():
    with pytest.raises(Invalid, match="nao e JSON"):
        json_body(b"{nao")


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("caminho que nao existe", "/api/nao-existe"),
        ("prefixo parecido", "/api/healthz"),
        ("arquivo comum", "/reader/app.js"),
    ],
)
def test_reports_no_route_for_an_unknown_path(name: str, path: str):
    assert match_route("GET", path) is None, name


def test_keeps_an_encoded_slash_inside_one_segment():
    # `%2F` decodificado cedo viraria separador e `/api/series/a%2Fb` casaria como
    # se fossem duas pastas. O caminho e casado cru; quem le o grupo decodifica e
    # passa por `safe_component`, que recusa a barra.
    assert request_path("/api/series/a%2Fb?x=1") == "/api/series/a%2Fb"
    assert safe_component("a/b") is None


@pytest.mark.parametrize(
    ("name", "target", "expected"),
    [
        ("tira a query", "/api/health?x=1", "/api/health"),
        ("tira o fragmento", "/api/health#topo", "/api/health"),
        ("deixa o caminho limpo em paz", "/api/health", "/api/health"),
    ],
)
def test_strips_query_and_fragment(name: str, target: str, expected: str):
    assert request_path(target) == expected, name


OWNER_EMAIL = "dono@example.com"
OWNER_PASSWORD = "senha-do-dono-bem-comprida"


class Client:
    """Um cliente HTTP com sessao, porque agora toda rota pergunta quem esta pedindo.

    Guarda o cookie entre chamadas como um navegador guardaria, e manda os
    cabecalhos de CSRF por padrao - que e o que a tela real manda. Os testes que
    querem provar a recusa passam `csrf=False` ou `cookie=""` de proposito.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie: str | None = None

    def raw(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        *,
        declare_length: bool = True,
        csrf: bool = True,
        cookie: str | None = None,
        extra_length: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        """Pedido cru, para poder omitir o Content-Length ou o cookie de proposito."""
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.putrequest(method, path)
        sent = self.cookie if cookie is None else cookie
        if sent:
            connection.putheader("Cookie", sent)
        if csrf:
            connection.putheader("X-Requested-With", "fetch")
            connection.putheader("Origin", f"http://127.0.0.1:{self.port}")
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if extra_length is not None:
            # Declara mais do que se vai mandar, para provar que o teto e conferido
            # no cabecalho e nao depois de ler.
            connection.putheader("Content-Length", str(extra_length))
        elif body is not None and declare_length:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        if body is not None and declare_length:
            connection.send(body)

        response = connection.getresponse()
        payload = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()

        issued = headers.get("set-cookie")
        if issued and cookie is None:
            self.cookie = issued.split(";", 1)[0]
        return response.status, payload, headers

    def get(self, path: str, method: str = "GET", **kwargs) -> tuple[int, bytes]:
        status, body, _ = self.raw(method, path, **kwargs)
        return status, body

    def send(
        self, method: str, path: str, body: bytes | None = None, **kwargs
    ) -> tuple[int, bytes]:
        status, payload, _ = self.raw(method, path, body, **kwargs)
        return status, payload

    def login(self, email: str = OWNER_EMAIL, password: str = OWNER_PASSWORD) -> int:
        status, _, _ = self.raw(
            "POST",
            "/api/login",
            json.dumps({"email": email, "password": password}).encode("utf-8"),
        )
        return status

    def forget(self) -> None:
        """Esquece a sessao, como um navegador sem cookie."""
        self.cookie = None


def owner_area(tmp_path: Path, *parts: str) -> Path:
    """Onde o acervo do dono realmente mora depois que ele tem conta.

    Os testes do painel apontavam para `tmp_path/library`, que e a raiz do projeto
    - o lugar de antes das contas. Continuar olhando para la escondia justamente o
    caso que importa: numa instalacao nova o dono nasce com area propria.
    """
    cfg = Config(root=tmp_path)
    with connect(cfg) as connection:
        owner = sole_owner(connection)
    assert owner is not None
    resolved = user_path(cfg, owner.id, *parts)
    assert resolved is not None
    return resolved


@pytest.fixture
def panel_server(tmp_path: Path):
    """Sobe o handler real numa porta efemera, para provar a ligacao e nao so as funcoes."""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-secreta\n", encoding="utf-8")
    (tmp_path / "reader").mkdir()
    (tmp_path / "reader" / "app.js").write_text("export const ok = 1;\n", encoding="utf-8")
    cfg = Config(root=tmp_path)
    migrate(cfg)
    with connect(cfg) as connection:
        ensure_owner(connection, OWNER_EMAIL, OWNER_PASSWORD)

    with _Server(("127.0.0.1", 0), make_panel_handler(cfg)) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        client = Client(httpd.server_address[1])
        # O dono entra uma vez. Os testes que precisam de outro papel trocam de
        # sessao explicitamente, para a troca aparecer no proprio teste.
        assert client.login() == 200
        try:
            yield client
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def test_reports_what_the_machine_can_do(panel_server: Client):
    status, body = panel_server.get("/api/health")
    payload = json.loads(body)

    assert status == 200
    assert payload["ok"] is True
    assert "free" in payload["engines"]
    assert isinstance(payload["has_api_key"], bool)


def test_never_answers_with_the_api_key(panel_server: Client):
    # A chave esta no ambiente do processo de teste ou nao; o que nao pode e o
    # valor sair numa resposta.
    _, body = panel_server.get("/api/health")
    assert b"sk-" not in body


def test_lists_the_disk_and_not_the_reader_index(panel_server: Client, tmp_path: Path):
    # Uma serie com capitulo enviado e nao traduzido: o painel precisa ve-la para
    # oferecer o botao que a traduz, e o leitor nao pode lista-la porque nao ha o
    # que abrir.
    chapter = owner_area(tmp_path, "library") / "Obra" / "001"
    chapter.mkdir(parents=True)
    (chapter / "1.jpg").write_bytes(b"0")

    status, body = panel_server.get("/api/series")
    payload = json.loads(body)

    assert status == 200
    assert [s["series"] for s in payload["series"]] == ["Obra"]
    assert payload["series"][0]["chapters"][0]["image_count"] == 1
    assert payload["series"][0]["chapters"][0]["engines"] == []


def test_lists_an_empty_library_as_empty(panel_server: Client):
    status, body = panel_server.get("/api/series")

    assert status == 200
    assert json.loads(body)["series"] == []


def test_shows_a_series_created_by_the_panel_right_away(panel_server: Client):
    # Sem isso a tela criaria a serie e ela sumiria ate ter capitulo traduzido.
    panel_server.send("POST", "/api/series", json.dumps({"slug": "Recem-criada"}).encode("utf-8"))

    _, body = panel_server.get("/api/series")

    assert [s["series"] for s in json.loads(body)["series"]] == ["Recem-criada"]


def test_keeps_serving_the_reader_next_to_the_panel(panel_server: Client):
    assert panel_server.get("/reader/app.js")[0] == 200
    assert panel_server.get("/.env")[0] == 404


def test_refuses_the_wrong_method_on_a_real_route(panel_server: Client):
    status, body = panel_server.get("/api/health", method="POST")

    assert status == 405
    assert b"GET" in body


def test_refuses_a_write_method_outside_the_panel(panel_server: Client):
    # Sem rota e sem arquivo para servir: PUT em caminho de leitor nao tem para
    # onde cair.
    assert panel_server.get("/reader/app.js", method="PUT")[0] == 404


# ---------- rotas de escrita, pelo servidor de verdade ----------

PNG = bytes.fromhex("89504e470d0a1a0a") + b"0" * 32
JPEG = bytes.fromhex("ffd8ffe0") + b"0" * 32


def _put_json(client: Client, path: str, payload: object) -> tuple[int, bytes]:
    return client.send("PUT", path, json.dumps(payload).encode("utf-8"))


def test_creates_a_series_with_slug_and_title(panel_server: Client, tmp_path: Path):
    status, body = panel_server.send(
        "POST",
        "/api/series",
        json.dumps({"slug": "Obra Nova", "title": "Obra Nova, o Titulo"}).encode("utf-8"),
    )

    assert status == 201
    assert json.loads(body)["slug"] == "Obra Nova"
    assert (owner_area(tmp_path, "library") / "Obra Nova" / "series.json").is_file()


def test_refuses_to_create_a_series_twice(panel_server: Client):
    payload = json.dumps({"slug": "Repetida"}).encode("utf-8")
    assert panel_server.send("POST", "/api/series", payload)[0] == 201
    assert panel_server.send("POST", "/api/series", payload)[0] == 409


@pytest.mark.parametrize(
    ("name", "slug"),
    [("fuga", "../fora"), ("oculta", ".git"), ("vazia", "")],
)
def test_refuses_a_hostile_series_name(panel_server: Client, name: str, slug: str):
    status, body = panel_server.send(
        "POST", "/api/series", json.dumps({"slug": slug}).encode("utf-8")
    )

    assert status == 422, name
    assert b"inaceitavel" in body, name


def test_keeps_the_glossary_across_a_write_and_a_read(panel_server: Client):
    panel_server.send("POST", "/api/series", json.dumps({"slug": "Obra"}).encode("utf-8"))
    terms = {"Zhuge": "Zhuge", "Sect Master": "Mestre da Seita"}

    assert _put_json(panel_server, "/api/series/Obra/glossary", terms)[0] == 200

    status, body = panel_server.send("GET", "/api/series/Obra/glossary")
    assert status == 200
    assert json.loads(body) == terms


def test_answers_422_and_not_500_for_a_broken_glossary(panel_server: Client):
    panel_server.send("POST", "/api/series", json.dumps({"slug": "Obra"}).encode("utf-8"))

    status, body = _put_json(panel_server, "/api/series/Obra/glossary", ["nao", "e", "objeto"])

    assert status == 422
    assert b"objeto JSON" in body


def test_answers_422_for_a_series_that_does_not_exist(panel_server: Client):
    status, body = panel_server.send("GET", "/api/series/nao-existe/glossary")

    assert status == 422
    assert b"nao existe" in body


def test_writes_the_cover_with_the_suffix_the_bytes_ask_for(panel_server: Client, tmp_path: Path):
    panel_server.send("POST", "/api/series", json.dumps({"slug": "Obra"}).encode("utf-8"))

    assert panel_server.send("PUT", "/api/series/Obra/cover", PNG)[0] == 200
    assert (owner_area(tmp_path, "library") / "Obra" / "cover.png").is_file()


def test_replaces_the_old_cover_instead_of_stacking_one(panel_server: Client, tmp_path: Path):
    panel_server.send("POST", "/api/series", json.dumps({"slug": "Obra"}).encode("utf-8"))
    panel_server.send("PUT", "/api/series/Obra/cover", PNG)
    panel_server.send("PUT", "/api/series/Obra/cover", JPEG)

    covers = sorted(p.name for p in (owner_area(tmp_path, "library") / "Obra").glob("cover.*"))
    assert covers == ["cover.jpg"]


def test_refuses_a_cover_that_is_not_an_image(panel_server: Client):
    panel_server.send("POST", "/api/series", json.dumps({"slug": "Obra"}).encode("utf-8"))

    status, body = panel_server.send("PUT", "/api/series/Obra/cover", b"MZ" + b"0" * 40)

    assert status == 422
    assert b"nao e jpeg" in body


def test_demands_a_declared_length_on_a_write(panel_server: Client):
    # `http.server` nao decodifica chunked e o painel nao adivinha tamanho.
    status, _ = panel_server.send("PUT", "/api/series/Obra/glossary", b"{}", declare_length=False)
    assert status == 411


def test_refuses_a_body_over_the_route_ceiling_before_reading_it(panel_server: Client):
    # O corpo nunca e enviado: o teto e conferido pelo `Content-Length`, e recusar
    # depois de receber o megabyte nao protegeria de nada.
    status, _ = panel_server.send(
        "PUT", "/api/series/Obra/glossary", b"x" * 8, declare_length=False, extra_length=MAX_JSON_BYTES + 1
    )

    assert status == 413


def test_shows_the_title_from_series_json(panel_server: Client, tmp_path: Path):
    panel_server.send(
        "POST",
        "/api/series",
        json.dumps({"slug": "obra-slug", "title": "O Titulo Bonito"}).encode("utf-8"),
    )

    status, body = panel_server.send("GET", "/api/series/obra-slug/series.json")

    assert status == 200
    assert json.loads(body)["title"] == "O Titulo Bonito"


# ---------- area de espera e upload ----------


@pytest.mark.parametrize(
    ("name", "existing", "expected"),
    [
        ("serie sem capitulo nenhum", [], "001"),
        ("segue a largura que a serie usa", ["001"], "002"),
        ("passa a dezena sem perder a largura", ["009"], "010"),
        ("passa a centena e cresce", ["999"], "1000"),
        ("pega o maior e nao o ultimo", ["003", "001", "002"], "004"),
        ("ordena por numero e nao por texto", ["2", "10"], "11"),
        ("ignora capitulo com nome solto", ["extra", "001"], "002"),
        ("so nomes soltos voltam ao inicio", ["extra", "especial"], "001"),
    ],
)
def test_suggests_the_next_chapter_number(name: str, existing: list[str], expected: str):
    assert next_chapter_name(existing) == expected, name


def test_pairs_each_archive_entry_with_its_position():
    # Quem extrai precisa voltar ao ZipInfo: o nome-base sozinho nao diz de qual
    # entrada ele veio.
    entries = safe_archive_entries(["leiame.txt", "cap/2.jpg", "cap/1.jpg"])

    assert entries == [(1, "2.jpg"), (2, "1.jpg")]


def zip_of(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_extracts_pages_by_their_base_name(tmp_path: Path):
    target = tmp_path / "espera"
    target.mkdir()

    names = extract_archive(zip_of({"cap/10.jpg": JPEG, "cap/2.jpg": JPEG}), target)

    assert names == ["2.jpg", "10.jpg"], "a ordem devolvida ja e a de leitura"
    assert sorted(p.name for p in target.iterdir()) == ["10.jpg", "2.jpg"]


def test_extracts_nothing_from_an_archive_with_an_escape_path(tmp_path: Path):
    target = tmp_path / "espera"
    target.mkdir()

    with pytest.raises(Invalid, match="caminho de fuga"):
        extract_archive(zip_of({"1.jpg": JPEG, "../../.env": b"CHAVE=1"}), target)

    assert list(target.iterdir()) == [], "o veto vem antes de qualquer byte sair"
    assert not (tmp_path.parent / ".env").exists()


def test_refuses_an_archive_with_two_pages_of_the_same_name(tmp_path: Path):
    # Gravar pelo nome-base faria uma apagar a outra, em silencio.
    target = tmp_path / "espera"
    target.mkdir()

    with pytest.raises(Invalid, match="mesmo nome de pagina"):
        extract_archive(zip_of({"a/1.jpg": JPEG, "b/1.jpg": PNG}), target)


def test_refuses_something_that_is_not_an_archive(tmp_path: Path):
    target = tmp_path / "espera"
    target.mkdir()

    with pytest.raises(Invalid, match="nao e um zip"):
        extract_archive(b"nao sou zip", target)


def test_refuses_an_archive_without_a_single_image(tmp_path: Path):
    target = tmp_path / "espera"
    target.mkdir()

    with pytest.raises(Invalid, match="nenhuma imagem"):
        extract_archive(zip_of({"leiame.txt": b"oi"}), target)


def series_with(client: Client, slug: str = "Obra") -> str:
    client.send("POST", "/api/series", json.dumps({"slug": slug}).encode("utf-8"))
    return slug


def test_suggests_the_chapter_number_when_the_body_is_empty(panel_server: Client, tmp_path: Path):
    slug = series_with(panel_server)
    (owner_area(tmp_path, "library") / slug / "001").mkdir()

    status, body = panel_server.send("POST", f"/api/series/{slug}/chapters", b"")

    assert status == 201
    assert json.loads(body)["chapter"] == "002"


def test_opens_the_staging_area_and_not_the_chapter(panel_server: Client, tmp_path: Path):
    slug = series_with(panel_server)

    panel_server.send("POST", f"/api/series/{slug}/chapters", json.dumps({"chapter": "007"}).encode())

    assert (owner_area(tmp_path, "library") / slug / "007.incoming").is_dir()
    assert not (owner_area(tmp_path, "library") / slug / "007").exists()


def test_refuses_to_stage_over_a_chapter_that_exists(panel_server: Client, tmp_path: Path):
    slug = series_with(panel_server)
    (owner_area(tmp_path, "library") / slug / "001").mkdir()

    status, _ = panel_server.send(
        "POST", f"/api/series/{slug}/chapters", json.dumps({"chapter": "001"}).encode()
    )

    assert status == 409


def stage(client: Client, slug: str, chapter: str) -> None:
    client.send("POST", f"/api/series/{slug}/chapters", json.dumps({"chapter": chapter}).encode())


def test_writes_a_page_into_the_staging_area(panel_server: Client, tmp_path: Path):
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")

    status, body = panel_server.send("PUT", f"/api/series/{slug}/chapters/001/files/1.jpg", JPEG)

    assert status == 200
    assert json.loads(body)["file"] == "1.jpg"
    assert (owner_area(tmp_path, "library") / slug / "001.incoming" / "1.jpg").read_bytes() == JPEG


def test_refuses_a_page_that_is_not_an_image(panel_server: Client):
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")

    status, body = panel_server.send(
        "PUT", f"/api/series/{slug}/chapters/001/files/1.jpg", b"MZ" + b"0" * 40
    )

    assert status == 422
    assert b"nao e jpeg" in body


@pytest.mark.parametrize("filename", ["..%2F..%2F.env", "1.exe", ".oculta.jpg"])
def test_refuses_a_hostile_page_name(panel_server: Client, filename: str):
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")

    status, _ = panel_server.send(
        "PUT", f"/api/series/{slug}/chapters/001/files/{filename}", JPEG
    )

    assert status == 422


def test_reports_the_staging_area_in_reading_order(panel_server: Client):
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")
    for name in ("10.jpg", "2.jpg", "1.jpg"):
        panel_server.send("PUT", f"/api/series/{slug}/chapters/001/files/{name}", JPEG)

    status, body = panel_server.send("GET", f"/api/series/{slug}/chapters/001/incoming")
    payload = json.loads(body)

    assert status == 200
    assert payload["files"] == ["1.jpg", "2.jpg", "10.jpg"]
    assert payload["bytes"] == 3 * len(JPEG)


def test_extracts_an_archive_into_the_staging_area(panel_server: Client, tmp_path: Path):
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")

    status, body = panel_server.send(
        "POST",
        f"/api/series/{slug}/chapters/001/archive",
        zip_of({"cap/1.jpg": JPEG, "cap/2.png": PNG}),
    )

    assert status == 200
    assert json.loads(body)["files"] == ["1.jpg", "2.png"]
    assert (owner_area(tmp_path, "library") / slug / "001.incoming" / "1.jpg").is_file()


def test_throws_away_the_staging_area_when_an_archive_fails(panel_server: Client, tmp_path: Path):
    # Meio zip extraido e pior que zip nenhum: parece capitulo e o commit aceitaria.
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")

    status, body = panel_server.send(
        "POST",
        f"/api/series/{slug}/chapters/001/archive",
        zip_of({"1.jpg": JPEG, "../../.env": b"CHAVE=1"}),
    )

    assert status == 422
    assert b"caminho de fuga" in body
    assert not (owner_area(tmp_path, "library") / slug / "001.incoming").exists()


def test_promotes_the_staging_area_to_a_chapter(panel_server: Client, tmp_path: Path):
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")
    panel_server.send("PUT", f"/api/series/{slug}/chapters/001/files/1.jpg", JPEG)

    status, body = panel_server.send("POST", f"/api/series/{slug}/chapters/001/commit")

    assert status == 200
    assert json.loads(body)["files"] == ["1.jpg"]
    assert (owner_area(tmp_path, "library") / slug / "001" / "1.jpg").is_file()
    assert not (owner_area(tmp_path, "library") / slug / "001.incoming").exists()


def test_refuses_to_promote_an_empty_staging_area(panel_server: Client):
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")

    status, body = panel_server.send("POST", f"/api/series/{slug}/chapters/001/commit")

    assert status == 422
    assert b"vazia" in body


def test_refuses_to_promote_over_a_chapter_that_exists(panel_server: Client, tmp_path: Path):
    slug = series_with(panel_server)
    incoming = owner_area(tmp_path, "library") / slug / "001.incoming"
    incoming.mkdir(parents=True)
    (incoming / "1.jpg").write_bytes(JPEG)
    (owner_area(tmp_path, "library") / slug / "001").mkdir()

    status, _ = panel_server.send("POST", f"/api/series/{slug}/chapters/001/commit")

    assert status == 409


def test_discards_the_staging_area_on_request(panel_server: Client, tmp_path: Path):
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")
    panel_server.send("PUT", f"/api/series/{slug}/chapters/001/files/1.jpg", JPEG)

    status, body = panel_server.send("DELETE", f"/api/series/{slug}/chapters/001/incoming")

    assert status == 200
    assert json.loads(body)["removed"] == 1
    assert not (owner_area(tmp_path, "library") / slug / "001.incoming").exists()


def test_keeps_the_staging_area_out_of_the_reader_index(panel_server: Client, tmp_path: Path):
    # E o bug que a area de espera existe para evitar: upload interrompido virando
    # capitulo para o process-all.
    slug = series_with(panel_server)
    stage(panel_server, slug, "001")
    panel_server.send("PUT", f"/api/series/{slug}/chapters/001/files/1.jpg", JPEG)

    _, body = panel_server.get("/api/series")
    chapters = json.loads(body)["series"][0]["chapters"]

    assert [c["chapter"] for c in chapters] == ["001"]
    assert chapters[0]["incoming"] is True
    assert chapters[0]["image_count"] == 0, "o que esta na area de espera ainda nao e pagina"


# ---------- jobs ----------
#
# A fila deixou de rodar neste processo: quem processa e o `mangatl worker`. O que
# estas rotas precisam provar mudou junto - que enfileiram, recusam e leem estado,
# e nao que o pipeline roda. Quem prova o pipeline e o `worker_test.py`.


@pytest.fixture
def panel_with_jobs(tmp_path: Path):
    (tmp_path / "reader").mkdir()

    cfg = Config(root=tmp_path)
    migrate(cfg)
    with connect(cfg) as connection:
        ensure_owner(connection, OWNER_EMAIL, OWNER_PASSWORD)

    # Depois de `ensure_owner`, e nao antes: a area do dono e nomeada pelo id que
    # essa chamada cria.
    (owner_area(tmp_path, "library") / "Obra" / "001").mkdir(parents=True)

    with _Server(("127.0.0.1", 0), make_panel_handler(cfg)) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        client = Client(httpd.server_address[1])
        assert client.login() == 200
        try:
            yield client, cfg
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def post_job(client: Client, **fields: object) -> tuple[int, bytes]:
    payload = {"series": "Obra", "chapter": "001", "engine": "free", **fields}
    return client.send("POST", "/api/jobs", json.dumps(payload).encode("utf-8"))


def test_accepts_the_job_and_answers_before_it_runs(panel_with_jobs):
    # 202 e nao 200: o que volta e um recibo, nao o resultado.
    client, cfg = panel_with_jobs

    status, body = post_job(client)
    payload = json.loads(body)

    assert status == 202
    assert payload["job"]["state"] == "pending"
    with connect(cfg) as connection:
        assert get_any(connection, payload["job_id"]) is not None


def test_tells_the_waiting_person_their_place_in_the_queue(panel_with_jobs):
    """"na fila" sem numero e indistinguivel de travado."""
    client, _ = panel_with_jobs

    payload = json.loads(post_job(client)[1])

    assert payload["job"]["queue_position"] == 0


def test_refuses_a_second_job_with_a_readable_conflict(panel_with_jobs):
    client, _ = panel_with_jobs
    post_job(client)

    status, body = post_job(client)

    assert status == 409
    assert b"ja tem um processamento" in body


def test_refuses_an_engine_that_does_not_exist(panel_with_jobs):
    client, _ = panel_with_jobs

    status, body = post_job(client, engine="tradutor-magico")

    assert status == 422
    assert b"nao existe" in body


def test_refuses_a_chapter_that_was_never_committed(panel_with_jobs):
    # Area de espera nao e capitulo: processar meio upload e o que ela evita.
    client, _ = panel_with_jobs

    status, body = post_job(client, chapter="999")

    assert status == 422
    assert b"nao existe" in body


def test_reports_the_progress_the_worker_wrote(panel_with_jobs):
    client, cfg = panel_with_jobs
    job_id = json.loads(post_job(client)[1])["job_id"]

    with connect(cfg) as connection:
        set_progress(
            connection,
            job_id,
            Progress(phase="extract", done=1, total=2, detail="p0001.jpg"),
            ("INFO linha que o job coletou",),
        )

    payload = json.loads(client.send("GET", f"/api/jobs/{job_id}")[1])

    assert payload["progress"] == {
        "phase": "extract",
        "done": 1,
        "total": 2,
        "detail": "p0001.jpg",
    }
    assert any("linha que o job coletou" in line for line in payload["log"])


def test_reports_the_job_as_done_once_the_worker_finishes_it(panel_with_jobs):
    client, cfg = panel_with_jobs
    job_id = json.loads(post_job(client)[1])["job_id"]

    with connect(cfg) as connection:
        finish(connection, job_id, state="done")

    payload = json.loads(client.send("GET", f"/api/jobs/{job_id}")[1])

    assert payload["state"] == "done"
    assert payload["finished_at"] is not None


def test_lists_the_jobs_newest_first(panel_with_jobs):
    client, cfg = panel_with_jobs
    first = json.loads(post_job(client)[1])["job_id"]
    with connect(cfg) as connection:
        finish(connection, first, state="done")
    second = json.loads(post_job(client)[1])["job_id"]

    payload = json.loads(client.send("GET", "/api/jobs")[1])

    assert [job["id"] for job in payload["jobs"]] == [second, first]


def test_reports_no_job_for_an_id_that_does_not_exist(panel_with_jobs):
    client, _ = panel_with_jobs

    status, body = client.send("GET", "/api/jobs/naoexiste")

    assert status == 404
