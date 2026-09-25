"""Orquestracao: imagens -> extract.json -> chapter.<engine>.json.

As duas metades sao deliberadamente separaveis. A extracao e cara e agnostica de
motor; a traducao e barata e descartavel. Rodar `--engine free` depois de
`--engine claude` reaproveita o extract.json inteiro.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from .config import Config
from .detect import draw_boxes
from .detectors.base import Detector, create_detector
from .engines.base import TranslationEngine
from .models import (
    PIPELINE_VERSION,
    BBox,
    Chapter,
    Detection,
    ExtractedBlock,
    ExtractedPage,
    Extraction,
    Progress,
    ProgressFn,
    TranslatedPage,
    report,
)
from .ocr import BlockReading, is_usable, read_block
from .ordering import reading_order
from .seams import (
    band_bbox_to_page,
    band_heights,
    covered_by_seam,
    improves_on,
    touches_bottom,
    touches_top,
)
from .slicing import slice_chapter_in_place
from .store import (
    chapter_output_dir,
    image_sha256,
    is_page_current,
    list_page_images,
    load_chapter,
    load_extraction,
    load_glossary,
    save_chapter,
    save_extraction,
)

log = logging.getLogger("mangatl.pipeline")


class ChapterNotFoundError(FileNotFoundError):
    pass


@dataclass(frozen=True)
class ExtractionReport:
    extraction: Extraction
    reused_pages: int
    extracted_pages: int

    @property
    def block_count(self) -> int:
        return sum(len(page.blocks) for page in self.extraction.pages)


def chapter_input_dir(cfg: Config, series: str, chapter: str) -> Path:
    directory = cfg.library_dir / series / chapter
    if not directory.is_dir():
        raise ChapterNotFoundError(f"capitulo nao encontrado: {directory}")
    return directory


def _read_image(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"nao consegui decodificar a imagem {path}")
    return image


def _drop_repeated_readings(
    readings: list[tuple[Detection, BlockReading]],
) -> tuple[list[tuple[Detection, BlockReading]], list[BBox]]:
    """Remove o mesmo texto lido duas vezes na mesma pagina.

    Um balao dentro de um painel branco produz duas caixas: a do balao e a do
    painel, que o contem parcialmente. Medido numa pagina real, elas tinham IoU
    0.26 e sobreposicao de 0.52 da menor - abaixo dos dois criterios de fusao -
    e as duas liam a mesma fala, que aparecia duplicada no leitor.

    Exigir sobreposicao evita apagar repeticao legitima: duas falas iguais em
    baloes distintos ("...", "NAO!") nao se cruzam. Entre as duas, fica a caixa
    menor, que e a do balao e nao a do painel - e a que a Fase 2 precisa.
    """
    kept: list[tuple[Detection, BlockReading]] = []
    removed: list[BBox] = []

    for detection, reading in readings:
        twin = next(
            (
                index
                for index, (other, other_reading) in enumerate(kept)
                if other_reading.text == reading.text
                and other.bbox.intersection_area(detection.bbox) > 0
            ),
            None,
        )
        if twin is None:
            kept.append((detection, reading))
            continue
        if detection.bbox.area < kept[twin][0].bbox.area:
            removed.append(kept[twin][0].bbox)
            kept[twin] = (detection, reading)
        else:
            removed.append(detection.bbox)

    return kept, removed


def _extract_page(
    cfg: Config, detector: Detector, path: Path, index: int, *, debug_dir: Path | None
) -> ExtractedPage:
    image = _read_image(path)
    height, width = image.shape[:2]

    detections = detector.detect(image)
    order = reading_order(
        [detection.bbox for detection in detections],
        band_overlap=cfg.reading_order.band_overlap,
        rtl=cfg.reading_order.rtl,
    )
    ordered = [detections[position] for position in order]

    readings: list[tuple[Detection, BlockReading]] = []
    dropped: list[BBox] = []
    for detection in ordered:
        # O recorte do texto, nao o do balao: o contorno que sobra em volta faz o
        # Tesseract ler a borda como glifo.
        reading = read_block(image, detection.text_bbox, cfg.ocr)
        if not is_usable(reading.text, reading.confidence, cfg.ocr):
            dropped.append(detection.bbox)
            continue
        readings.append((detection, reading))

    readings, duplicates = _drop_repeated_readings(readings)
    dropped.extend(duplicates)

    kept = [detection for detection, _ in readings]
    blocks = [
        ExtractedBlock(
            id=f"p{index:03d}-b{position:02d}",
            bbox=detection.bbox,
            raw_text=reading.text,
            confidence=reading.confidence,
            kind=detection.kind,
            text_bbox=reading.text_bbox,
            source_font_px=reading.source_font_px,
        )
        for position, (detection, reading) in enumerate(readings, start=1)
    ]

    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(debug_dir / f"{path.stem}.png"), draw_boxes(image, kept, dropped))

    return ExtractedPage(
        index=index,
        image=path.name,
        width=width,
        height=height,
        image_sha256=image_sha256(path),
        blocks=tuple(blocks),
    )


def _stitch_seam(
    cfg: Config,
    detector: Detector,
    upper: ExtractedPage,
    lower: ExtractedPage,
    *,
    upper_image: np.ndarray,
    lower_image: np.ndarray,
) -> tuple[ExtractedPage, ExtractedPage]:
    """Reune numa fala so o que a emenda entre duas paginas cortou em duas.

    A deteccao roda numa faixa montada com o pe de uma pagina e a cabeca da outra,
    entao o detector ve a fala inteira. Os filtros de area do backend `heuristic`
    sao proporcionais a area da imagem e a faixa e menor que uma pagina, o que
    empurra as razoes para cima - na pratica na direcao segura, porque o que estava
    perto do minimo passa a caber com folga.
    """
    upper_take, lower_take = band_heights(upper.height, lower.height, cfg.detect.seam_band)
    band = np.vstack([upper_image[upper.height - upper_take :], lower_image[:lower_take]])

    def covered(bbox: BBox, overflow: int) -> list[ExtractedBlock]:
        return [
            block
            for blocks, on_lower in ((upper.blocks, False), (lower.blocks, True))
            for block in blocks
            if covered_by_seam(
                block.bbox, bbox, overflow=overflow, page_height=upper.height, on_lower=on_lower
            )
        ]

    stitched: list[ExtractedBlock] = []
    replaced: set[str] = set()
    for detection in detector.detect(band):
        mapped = band_bbox_to_page(detection.bbox, upper_take=upper_take, upper_height=upper.height)
        if mapped is None:
            continue

        reading = read_block(band, detection.text_bbox, cfg.ocr)
        if not is_usable(reading.text, reading.confidence, cfg.ocr):
            continue

        bbox, overflow = mapped
        halves = covered(bbox, overflow)
        if not improves_on(reading.text, [block.raw_text for block in halves]):
            log.info(
                "operation=stitch_seam upper=%d lower=%d descartada=%r",
                upper.index,
                lower.index,
                reading.text[:40],
            )
            continue

        stitched.append(
            ExtractedBlock(
                id=f"p{upper.index:03d}-s{len(stitched) + 1:02d}",
                bbox=bbox,
                raw_text=reading.text,
                confidence=reading.confidence,
                kind=detection.kind,
                # `text_bbox` fica de fora de proposito: ela sairia em coordenada
                # da faixa, e mapea-la pediria um segundo campo de transbordo so
                # para ela. Sem ela o leitor pinta o balao inteiro, que numa fala
                # cortada e o que cobre as duas metades.
                source_font_px=reading.source_font_px,
                overflow_bottom=overflow,
            )
        )
        replaced.update(block.id for block in halves)

    if not stitched:
        return upper, lower

    log.info(
        "operation=stitch_seam upper=%d lower=%d falas=%d substituidas=%d",
        upper.index,
        lower.index,
        len(stitched),
        len(replaced),
    )

    kept_upper = [block for block in upper.blocks if block.id not in replaced]
    kept_upper.extend(stitched)
    order = reading_order(
        [block.bbox for block in kept_upper],
        band_overlap=cfg.reading_order.band_overlap,
        rtl=cfg.reading_order.rtl,
    )

    return (
        upper.model_copy(update={"blocks": tuple(kept_upper[position] for position in order)}),
        lower.model_copy(
            update={"blocks": tuple(b for b in lower.blocks if b.id not in replaced)}
        ),
    )


def _seam_candidates(pages: Sequence[ExtractedPage], fresh: set[int]) -> list[int]:
    """Indices das emendas que valem uma deteccao a mais.

    O sinal de corte e bloco encostado na borda compartilhada, de um lado ou do
    outro. Medido em manhwa/001, 26 das 154 emendas - nas outras 128 a faixa nao
    acharia nada e a deteccao seria paga de graca.

    Emenda entre duas paginas reaproveitadas do cache tambem e pulada: o bloco de
    emenda ficou salvo na pagina de cima e veio junto com ela.
    """
    candidates = []
    for index, (upper, lower) in enumerate(zip(pages, pages[1:], strict=False)):
        if upper.index not in fresh and lower.index not in fresh:
            continue
        cut = any(touches_bottom(block.bbox, upper.height) for block in upper.blocks) or any(
            touches_top(block.bbox) for block in lower.blocks
        )
        if cut:
            candidates.append(index)
    return candidates


def extract_chapter(
    cfg: Config,
    series: str,
    chapter: str,
    *,
    force: bool = False,
    debug_boxes: bool = False,
    detector_name: str | None = None,
    progress: ProgressFn | None = None,
    force_pages: frozenset[str] = frozenset(),
) -> ExtractionReport:
    """Detecta e OCRa as paginas do capitulo, pulando as que nao mudaram.

    `force_pages` refaz so as paginas com esses nomes: retraduzir uma pagina e
    tambem reler ela, porque o erro que motivou o pedido quase sempre veio do OCR.
    """
    chapter_dir = chapter_input_dir(cfg, series, chapter)
    images = list_page_images(chapter_dir)
    if not images:
        raise ChapterNotFoundError(f"nenhuma imagem em {chapter_dir}")

    # Captura de rolagem vira paginas normais antes de qualquer outra coisa, para
    # que o resto do pipeline nunca precise saber que ela existiu.
    report(progress, Progress(phase="slice", total=len(images), detail="procurando a costura"))
    if slice_chapter_in_place(chapter_dir, images, cfg.slicing):
        images = list_page_images(chapter_dir)
    report(
        progress,
        Progress(phase="slice", done=len(images), total=len(images), detail=f"{len(images)} paginas"),
    )

    previous = None if force else load_extraction(cfg, series, chapter)
    debug_dir = chapter_output_dir(cfg, series, chapter) / "debug" if debug_boxes else None

    # Um detector por capitulo, nao por pagina: o backend treinado leva segundos
    # para carregar o modelo, e sao 155 paginas.
    detector = create_detector(detector_name or cfg.detect.backend, cfg)
    log.info("operation=extract_chapter detector=%s pages=%d", detector.name, len(images))

    pages: list[ExtractedPage] = []
    fresh: set[int] = set()
    reused = 0
    for index, path in enumerate(images, start=1):
        cached = previous.page_by_image(path.name) if previous else None
        current = cached is not None and is_page_current(previous, path, PIPELINE_VERSION)
        if current and not debug_boxes and path.name not in force_pages:
            pages.append(cached.model_copy(update={"index": index}))
            reused += 1
            continue
        log.info("operation=extract_page page=%d image=%s", index, path.name)
        pages.append(_extract_page(cfg, detector, path, index, debug_dir=debug_dir))
        fresh.add(index)
        report(
            progress,
            Progress(phase="extract", done=index, total=len(images), detail=path.name),
        )

    # Depois das paginas, e nao junto: a emenda precisa das duas ja detectadas
    # para saber se ha sinal de corte antes de pagar uma deteccao a mais.
    candidates = _seam_candidates(pages, fresh)
    if candidates:
        report(
            progress,
            Progress(
                phase="extract",
                done=len(images),
                total=len(images),
                detail=f"{len(candidates)} emendas entre paginas",
            ),
        )
    for position in candidates:
        pages[position], pages[position + 1] = _stitch_seam(
            cfg,
            detector,
            pages[position],
            pages[position + 1],
            upper_image=_read_image(images[position]),
            lower_image=_read_image(images[position + 1]),
        )

    extraction = Extraction(
        series=series,
        chapter=chapter,
        pipeline_version=PIPELINE_VERSION,
        pages=tuple(pages),
    )
    save_extraction(cfg, extraction)

    return ExtractionReport(
        extraction=extraction,
        reused_pages=reused,
        extracted_pages=len(pages) - reused,
    )


def keep_manual_edits(
    previous: Chapter | None, pages: Sequence[TranslatedPage]
) -> list[TranslatedPage]:
    """As paginas novas, com as falas corrigidas a mao por cima da traducao nova.

    Casa por imagem e id de bloco. Um bloco editado que a nova extracao nao trouxe
    volta como estava: a correcao e trabalho de alguem, e sumir com ela porque o
    OCR mudou de ideia seria perde-la sem ninguem ter pedido.
    """
    if previous is None:
        return list(pages)
    edited = {
        page.image: {block.id: block for block in page.blocks if block.edited}
        for page in previous.pages
    }

    merged: list[TranslatedPage] = []
    for page in pages:
        kept = edited.get(page.image, {})
        if not kept:
            merged.append(page)
            continue
        blocks = [kept.get(block.id, block) for block in page.blocks]
        present = {block.id for block in page.blocks}
        blocks.extend(block for block_id, block in kept.items() if block_id not in present)
        merged.append(page.model_copy(update={"blocks": tuple(blocks)}))
    return merged


def _splice(
    extraction: Extraction, previous: Chapter, fresh: Sequence[TranslatedPage]
) -> list[TranslatedPage]:
    """O capitulo anterior com as paginas retraduzidas no lugar, na ordem da extracao."""
    by_image = {page.image: page for page in previous.pages}
    by_image.update({page.image: page for page in fresh})
    return [
        by_image[page.image].model_copy(update={"index": page.index})
        for page in extraction.pages
        if page.image in by_image
    ]


def translate_chapter(
    cfg: Config,
    extraction: Extraction,
    engine: TranslationEngine,
    progress: ProgressFn | None = None,
    only_pages: frozenset[str] = frozenset(),
) -> Chapter:
    """Traduz o capitulo, ou so `only_pages` dele, preservando as correcoes a mao.

    So as paginas pedidas quando ja existe traducao deste motor para costurar o
    resto; sem ela, uma pagina sozinha viraria um capitulo de uma pagina.
    """
    chapter_dir = chapter_input_dir(cfg, extraction.series, extraction.chapter)
    glossary = load_glossary(cfg, extraction.series)
    previous = load_chapter(cfg, extraction.series, extraction.chapter, engine.name)

    partial = bool(only_pages) and previous is not None
    targets = [page for page in extraction.pages if page.image in only_pages] if partial else extraction.pages

    fresh = keep_manual_edits(
        previous, engine.translate_chapter(targets, glossary, chapter_dir, progress)
    )
    pages = _splice(extraction, previous, fresh) if partial and previous is not None else fresh

    chapter = Chapter(
        series=extraction.series,
        chapter=extraction.chapter,
        engine=engine.name,
        model=engine.model,
        pipeline_version=PIPELINE_VERSION,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        pages=tuple(pages),
    )
    save_chapter(cfg, chapter)
    return chapter


def translated_line_count(pages: Sequence[object]) -> int:
    return sum(len(page.blocks) for page in pages)
