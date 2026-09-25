"""Interface de linha de comando do mangatl."""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path

import typer
from dotenv import load_dotenv

from .accounts import (
    MigrationRefused,
    ensure_owner,
    migrate_to_accounts,
    owner_config,
    password_hash_of,
    sole_owner,
)
from .config import Config, load_config
from .db import connect, migrate, transaction
from .demo import DemoError, build_demo, demo_root
from .detectors.base import (
    DetectorUnavailableError,
    UnknownDetectorError,
    available_detectors,
)
from .engines.base import TranslationError, UnknownEngineError, available_engines, create_engine
from .models import Progress, ProgressFn
from .panel import serve_panel
from .pipeline import ChapterNotFoundError, extract_chapter, translate_chapter
from .sessions import clear_every_login_attempt
from .slicing import is_tall, slice_stream
from .store import IMAGE_SUFFIXES, build_library, discover_chapters, save_library
from .worker import run_forever

app = typer.Typer(add_completion=False, help="Traduz capitulos de manga/mahua EN->PT e serve um leitor web.")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )


def _load() -> Config:
    """O projeto, sem conta nenhuma: `library/` e `output/` na raiz.

    So `migrate-to-accounts` e `doctor` usam isto. Todo o resto quer `_load_owner`,
    porque depois da migracao o acervo nao mora mais aqui.
    """
    load_dotenv()
    try:
        return load_config()
    except FileNotFoundError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=2) from error


def _load_owner() -> Config:
    """O projeto apontado para a area do dono, ou para a raiz antes da migracao.

    Um comando so, e nao uma flag `--user`: na linha de comando quem esta
    digitando e o dono da maquina. Conta de terceiro so existe pela rede.
    """
    return owner_config(_load())


def _resolve_chapter(cfg: Config, target: str) -> tuple[str, str]:
    """Aceita `library/serie/001`, `serie/001` ou um caminho absoluto.

    Tres bases e nao uma porque a biblioteca deixou de morar na raiz do projeto
    depois da migracao: `library/serie/001` e o que o proprio `mangatl slice`
    imprime e continua tendo que funcionar, e ele so casa com a base de conteudo.
    A mais especifica vem primeiro.
    """
    path = Path(target)
    if path.is_absolute():
        candidates = (path,)
    else:
        content_root = cfg.library_dir.parent
        candidates = (cfg.library_dir / path, content_root / path, cfg.root / path)

    candidate = next((option for option in candidates if option.is_dir()), None)
    if candidate is None:
        raise ChapterNotFoundError(f"capitulo nao encontrado: {target}")

    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(cfg.library_dir.resolve())
    except ValueError as error:
        raise ChapterNotFoundError(f"{resolved} esta fora de {cfg.library_dir}") from error

    if len(relative.parts) != 2:
        raise ChapterNotFoundError(f"esperava <serie>/<capitulo>, recebi {relative}")
    return relative.parts[0], relative.parts[1]


def _progress_printer() -> ProgressFn:
    """Uma linha que se reescreve, para o terminal ganhar progresso sem virar log.

    O pipeline ja logava pagina a pagina em `--verbose`, mas so la; quem roda sem
    a flag ficava minutos olhando um cursor parado.
    """
    seen: list[str] = []

    def show(update: Progress) -> None:
        if seen and seen[-1] != update.phase:
            typer.echo("")
        seen.append(update.phase)
        counted = f"{update.done}/{update.total}" if update.total else ""
        typer.echo(f"\r  {update.phase:<9} {counted:>9}  {update.detail[:46]:<46}", nl=False)

    return show


def _process_one(
    cfg: Config,
    series: str,
    chapter: str,
    *,
    engine_name: str,
    detector_name: str | None,
    force: bool,
    debug_boxes: bool,
    dry_run: bool,
) -> None:
    progress = _progress_printer()
    report = extract_chapter(
        cfg,
        series,
        chapter,
        force=force,
        debug_boxes=debug_boxes,
        detector_name=detector_name,
        progress=progress,
    )
    typer.echo("")
    typer.secho(
        f"{series}/{chapter}: {len(report.extraction.pages)} paginas "
        f"({report.extracted_pages} extraidas, {report.reused_pages} reaproveitadas), "
        f"{report.block_count} baloes",
        fg=typer.colors.CYAN,
    )
    if debug_boxes:
        typer.echo(f"  caixas desenhadas em {cfg.output_dir / series / chapter / 'debug'}")

    if dry_run:
        typer.secho("  --dry-run: parei antes de traduzir", fg=typer.colors.YELLOW)
        return

    engine = create_engine(engine_name, cfg)
    result = translate_chapter(cfg, report.extraction, engine, progress)
    typer.echo("")
    lines = sum(len(page.blocks) for page in result.pages)
    typer.secho(f"  traduzido com '{engine.name}': {lines} falas", fg=typer.colors.GREEN)


@app.command()
def process(
    target: str = typer.Argument(..., help="Pasta do capitulo, ex: library/minha-serie/001"),
    engine: str = typer.Option(None, "--engine", "-e", help=f"Motor de traducao: {', '.join(available_engines())}"),
    model: str = typer.Option(None, "--model", "-m", help="Sobrescreve o modelo do config.toml"),
    detector: str = typer.Option(
        None, "--detector", "-d", help=f"Detector de baloes: {', '.join(available_detectors())}"
    ),
    force: bool = typer.Option(False, "--force", help="Refaz o OCR mesmo em paginas inalteradas"),
    debug_boxes: bool = typer.Option(False, "--debug-boxes", help="Desenha as caixas detectadas em output/.../debug"),
    dry_run: bool = typer.Option(False, "--dry-run", help="So extrai; nao chama motor de traducao"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Processa um capitulo: deteccao, OCR e traducao."""
    _configure_logging(verbose)
    cfg = _load_owner()
    if model:
        cfg = cfg.model_copy(update={"translation": cfg.translation.model_copy(update={"model": model})})

    try:
        series, chapter = _resolve_chapter(cfg, target)
        _process_one(
            cfg, series, chapter,
            engine_name=engine or cfg.translation.engine,
            detector_name=detector,
            force=force, debug_boxes=debug_boxes, dry_run=dry_run,
        )
    except (ChapterNotFoundError, UnknownEngineError, UnknownDetectorError,
            DetectorUnavailableError, TranslationError) as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    save_library(cfg, build_library(cfg))


@app.command(name="process-all")
def process_all(
    series: str = typer.Argument(None, help="Nome da serie; vazio processa a biblioteca inteira"),
    engine: str = typer.Option(None, "--engine", "-e"),
    detector: str = typer.Option(None, "--detector", "-d"),
    force: bool = typer.Option(False, "--force"),
    debug_boxes: bool = typer.Option(False, "--debug-boxes"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Processa todos os capitulos; os ja processados e inalterados sao no-op."""
    _configure_logging(verbose)
    cfg = _load_owner()
    engine_name = engine or cfg.translation.engine

    pairs = [pair for pair in discover_chapters(cfg) if series is None or pair[0] == series]
    if not pairs:
        typer.secho(f"nenhum capitulo encontrado em {cfg.library_dir}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    failures = 0
    for chapter_series, chapter in pairs:
        try:
            _process_one(
                cfg, chapter_series, chapter,
                engine_name=engine_name, detector_name=detector,
                force=force, debug_boxes=debug_boxes, dry_run=dry_run,
            )
        except (ChapterNotFoundError, UnknownEngineError, UnknownDetectorError,
            DetectorUnavailableError, TranslationError) as error:
            failures += 1
            typer.secho(f"{chapter_series}/{chapter}: {error}", fg=typer.colors.RED)

    save_library(cfg, build_library(cfg))
    if failures:
        typer.secho(f"{failures} capitulo(s) falharam", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command(name="slice")
def slice_command(
    source: str = typer.Argument(..., help="Pasta com as capturas costuradas"),
    series: str = typer.Argument(..., help="Nome da serie de destino"),
    chapter: str = typer.Argument(..., help="Nome do capitulo de destino"),
    pattern: str = typer.Option("*", "--pattern", help="Glob dos arquivos a importar"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Importa capturas de rolagem de uma pasta externa, ja fatiadas em paginas."""
    _configure_logging(verbose)
    cfg = _load_owner()

    source_dir = Path(source).expanduser()
    if not source_dir.is_dir():
        typer.secho(f"pasta nao encontrada: {source_dir}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    candidates = sorted(
        path for path in source_dir.glob(pattern)
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    if not candidates:
        typer.secho(f"nenhuma imagem casou com '{pattern}' em {source_dir}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    import cv2

    tall, short = [], []
    for path in candidates:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            typer.secho(f"pulei {path.name}: nao consegui decodificar", fg=typer.colors.YELLOW)
            continue
        height, width = image.shape[:2]
        (tall if is_tall(width, height, cfg.slicing) else short).append(path)

    # Numa pasta de captura convivem a costura final e os prints brutos que a
    # geraram. Os prints se sobrepoem entre si, entao importa-los junto duplicaria
    # falas; quando ha costura, ela e a unica fonte correta.
    chosen = tall or short
    if tall and short:
        typer.secho(f"ignorei {len(short)} imagem(ns) curta(s) - a costura ja as contem", fg=typer.colors.YELLOW)
    if not tall:
        typer.secho("nenhuma captura alta; importando as imagens como paginas", fg=typer.colors.YELLOW)

    destination = cfg.library_dir / series / chapter
    if tall:
        # Uma chamada so para todas: o macro corta a captura num teto fixo de altura,
        # e esse corte parte baloes ao meio. Fatiar em fluxo continuo remonta o que
        # ficou dividido entre dois arquivos.
        written = len(slice_stream(tall, destination, cfg.slicing))
    else:
        destination.mkdir(parents=True, exist_ok=True)
        for path in chosen:
            shutil.copy2(path, destination / path.name)
        written = len(chosen)

    typer.secho(f"{len(chosen)} origem(ns) -> {written} pagina(s) em {destination}", fg=typer.colors.GREEN)
    typer.echo(f"agora: mangatl process library/{series}/{chapter} --dry-run --debug-boxes")


@app.command(name="build-library")
def build_library_command() -> None:
    """Regenera output/library.json a partir do que ja existe em disco."""
    cfg = _load_owner()
    path = save_library(cfg, build_library(cfg))
    typer.secho(f"escrito {path}", fg=typer.colors.GREEN)


@app.command(name="build-demo")
def build_demo_command(
    series: str = typer.Argument(..., help="Serie de onde os capitulos saem"),
    chapters: list[str] = typer.Argument(..., help="Um ou mais capitulos ja processados"),
) -> None:
    """Publica capitulos seus na vitrine `public/demo/`, que qualquer um le sem conta.

    Roda na sua maquina. O servidor hospedado nunca escreve em `public/` - a
    vitrine viaja dentro da imagem, e por isso e imutavel para quem a visita.

    O material publicado aqui fica na internet aberta, sem login. Publique so o
    que voce pode publicar: dominio publico, licenca livre ou arte sua.
    """
    cfg = _load_owner()
    try:
        library, copied = build_demo(cfg, [(series, chapter) for chapter in chapters])
    except DemoError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    published = sum(len(entry.chapters) for entry in library.series)
    typer.secho(f"{copied} arquivo(s) copiado(s) para {demo_root(cfg)}", fg=typer.colors.GREEN)
    typer.echo(f"vitrine: {published} capitulo(s) em {len(library.series)} serie(s)")
    for entry in library.series:
        motors = sorted({engine for chapter in entry.chapters for engine in chapter.engines})
        typer.echo(f"  {entry.series}: {len(entry.chapters)} cap., motores {', '.join(motors)}")


@app.command(name="migrate-to-accounts")
def migrate_to_accounts_command() -> None:
    """Cria o dono e move library/ e output/ para a area dele. Roda uma vez."""
    cfg = _load()
    try:
        result = migrate_to_accounts(cfg)
    except MigrationRefused as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    owner = owner_config(cfg)
    if not result.moved:
        typer.secho(f"nada a mover; o acervo do dono ja esta em {owner.library_dir}", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"movido: {', '.join(result.moved)} -> {owner.library_dir.parent}", fg=typer.colors.GREEN)

    # O `library.json` guarda caminhos; deixa-lo apontando para a raiz antiga
    # daria um leitor que lista capitulo e nao acha imagem nenhuma.
    save_library(owner, build_library(owner))
    typer.echo(f"dono: {result.owner_id}")


@app.command(name="setup-free")
def setup_free() -> None:
    """Baixa o pacote de idioma do motor `free` (roda uma vez)."""
    cfg = _load()
    from .engines.argos import install_language_package

    source, target = cfg.translation.source_lang, cfg.translation.target_lang
    typer.echo(f"baixando pacote Argos {source}->{target} (~100MB na primeira vez)...")
    try:
        installed = install_language_package(source, target)
    except (ImportError, TranslationError) as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error
    typer.secho(f"instalado: {installed}", fg=typer.colors.GREEN)


def _lan_addresses() -> list[str]:
    addresses = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        addresses.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        addresses.update(
            info[4][0]
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        )
    except OSError:
        pass
    return sorted(address for address in addresses if not address.startswith("127."))


def _prepare_server(cfg: Config) -> None:
    """Esquema do banco e dono, antes de abrir a porta.

    Migrar em toda subida em vez de por comando separado: conferir uma tabela de
    quatro linhas nao custa nada, e a alternativa e alguem subir com o esquema
    velho e descobrir na primeira escrita.

    `OWNER_EMAIL` e `OWNER_PASSWORD` sao lidos aqui e so aqui. A senha vira hash
    na hora; nem o banco nem o log veem o valor.
    """
    migrate(cfg)
    email, password = os.environ.get("OWNER_EMAIL"), os.environ.get("OWNER_PASSWORD")
    with connect(cfg) as connection, transaction(connection):
        try:
            owner = ensure_owner(connection, email, password)
        except ValueError as error:
            typer.secho(str(error), fg=typer.colors.RED)
            raise typer.Exit(code=2) from error
        existing = owner or sole_owner(connection)
        has_password = existing is not None and password_hash_of(connection, existing.id)

    if not has_password:
        typer.secho(
            "sem dono com senha: defina OWNER_EMAIL e OWNER_PASSWORD para entrar no painel.\n"
            "      A vitrine publica continua funcionando sem isso.",
            fg=typer.colors.YELLOW,
        )


@app.command()
def serve(port: int = typer.Option(8000, "--port", "-p")) -> None:
    """Sobe o leitor web: a vitrine para qualquer um, o acervo para quem tem sessao."""
    cfg = _load()
    _prepare_server(cfg)

    owner = owner_config(cfg)
    if owner.library_dir.is_dir():
        save_library(owner, build_library(owner))

    typer.secho(f"leitor:   http://localhost:{port}/reader/", fg=typer.colors.GREEN)
    for address in _lan_addresses():
        typer.echo(f"celular:  http://{address}:{port}/reader/")
    typer.echo("Ctrl+C para parar")

    try:
        serve_panel(cfg, port)
    except KeyboardInterrupt:
        typer.echo("\nparado")


def _check(label: str, ok: bool, hint: str = "") -> bool:
    mark = typer.style("OK  ", fg=typer.colors.GREEN) if ok else typer.style("FALTA", fg=typer.colors.RED)
    typer.echo(f"{mark} {label}")
    if not ok and hint:
        typer.echo(f"      {hint}")
    return ok


def _installed(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


def _report_detector(cfg: Config) -> None:
    """Estado do backend de deteccao configurado.

    Fora da lista de checks obrigatorios: quem usa o backend `heuristic` nao
    precisa de torch, e reprovar o doctor por isso seria mentira.
    """
    typer.echo(f"detector: {cfg.detect.backend}")
    if cfg.detect.backend != "rtdetr":
        return

    # Lista, nao gerador: o doctor existe para listar tudo que falta de uma vez,
    # e o short-circuit do all() esconderia o segundo pacote ausente.
    ready = [
        _check(f"pacote {module}", _installed(module), "pip install -e '.[rtdetr]'")
        for module in ("torch", "transformers")
    ]
    if not all(ready):
        return

    from huggingface_hub import try_to_load_from_cache

    from .detectors.rtdetr import _resolve_device

    typer.echo(f"      device: {_resolve_device(cfg.detect.rtdetr.device)}")
    _check(
        f"modelo {cfg.detect.rtdetr.model_id} em cache",
        isinstance(try_to_load_from_cache(cfg.detect.rtdetr.model_id, "config.json"), str),
        "baixa sozinho na primeira extracao (~200MB)",
    )


@app.command(name="reset-login")
def reset_login() -> None:
    """Destranca o login: apaga todas as tentativas de senha registradas.

    Para quando alguem trancou a conta do dono errando a senha dele de proposito.
    """
    cfg = _load()
    migrate(cfg)
    with connect(cfg) as connection:
        removed = clear_every_login_attempt(connection)
    typer.secho(f"{removed} tentativa(s) apagada(s); o login esta destrancado", fg=typer.colors.GREEN)


@app.command()
def worker(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Roda a fila de processamento. Processo separado do que atende HTTP.

    O pipeline segura um nucleo por minutos; no mesmo processo do servidor, isso e
    a tela de progresso congelando junto com o trabalho que ela mostra.
    """
    _configure_logging(verbose)
    cfg = _load()
    migrate(cfg)
    typer.secho("worker de pe; Ctrl+C para parar", fg=typer.colors.GREEN)
    run_forever(cfg)
    typer.echo("parado")


@app.command()
def doctor() -> None:
    """Verifica tudo que o pipeline precisa e diz o que falta."""
    import os

    cfg = _load_owner()
    typer.echo(f"projeto: {cfg.root}\n")

    checks = [
        _check("config.toml", True),
        _check(
            "binario tesseract",
            shutil.which("tesseract") is not None,
            "sudo apt install tesseract-ocr",
        ),
    ]

    languages: list[str] = []
    if shutil.which("tesseract"):
        try:
            result = subprocess.run(
                ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30, check=False
            )
            languages = result.stdout.split()
        except (OSError, subprocess.SubprocessError):
            languages = []
    checks.append(
        _check(
            f"idioma tesseract '{cfg.ocr.lang}'",
            cfg.ocr.lang in languages,
            f"sudo apt install tesseract-ocr-{cfg.ocr.lang}",
        )
    )

    for module, hint in (("cv2", "pip install -e ."), ("pytesseract", "pip install -e ."), ("anthropic", "pip install -e .")):
        try:
            __import__(module)
            present = True
        except ImportError:
            present = False
        checks.append(_check(f"pacote {module}", present, hint))

    _check(
        "ANTHROPIC_API_KEY (motor claude)",
        bool(os.environ.get("ANTHROPIC_API_KEY")),
        "copie .env.example para .env e preencha a chave",
    )

    typer.echo("")
    _report_detector(cfg)

    try:
        from .engines.argos import language_package_installed

        free_ready = language_package_installed(cfg.translation.source_lang, cfg.translation.target_lang)
    except ImportError:
        free_ready = False
    _check("motor free (Argos en->pt)", free_ready, "pip install -e '.[free]' && mangatl setup-free")

    typer.echo("")
    _check(f"biblioteca em {cfg.library_dir}", cfg.library_dir.is_dir(), f"mkdir -p {cfg.library_dir}")
    chapters = list(discover_chapters(cfg))
    typer.echo(f"      {len(chapters)} capitulo(s) encontrado(s)")

    if not all(checks):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
