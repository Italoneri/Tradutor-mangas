"""O capitulo traduzido como arquivo: CBZ ou PDF, com a fala escrita na imagem.

O leitor pinta a traducao por cima da pagina em HTML, e isso so existe dentro
dele. Exportar e o caminho para ler em outro programa, e ai o texto precisa estar
nos pixels.

A geometria segue a do `reader/overlay.js`, em forma simples: largura do balao,
faixa vertical do texto original, corpo tirado do letreiramento medido pelo OCR e
encolhido ate caber. Nao ha medicao de layout aqui como no navegador - a quebra
de linha e feita com a largura real de cada palavra na fonte escolhida, o que
basta para o texto nao sair do balao.

Pagina a pagina, e nunca o capitulo inteiro na memoria: 155 fatias de 1000x2000
decodificadas sao ~900MB, e o `app` tem 1GB.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import BBox, Chapter, TranslatedBlock, TranslatedPage

SOURCE_FONT_RATIO = 1.39
"""Corpo tipografico como multiplo da altura da caixa de palavra do Tesseract.
O mesmo numero do `overlay.js`, medido la."""

FONT_FLOOR = 0.028
FONT_MAX = 0.08
"""Piso e teto do corpo, como fracao da largura da pagina - os `cqw` do leitor."""

FALLBACK_FONT = 0.045
"""Corpo quando o bloco nao traz `source_font_px`: o meio da faixa medida no leitor."""

SHRINK = 0.88
LINE_HEIGHT = 1.15
PADDING = 0.5
"""Folga do branco em volta do texto, em fracoes do corpo, como o `padding` do CSS."""

JPEG_QUALITY = 90

BOLD_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
"""Uma fonte com peso de letreiro, se a maquina tiver. Sem nenhuma, cai na que
vem dentro do Pillow - fina, mas com os acentos do portugues."""

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


def _font(size: int) -> Font:
    for candidate in BOLD_FONTS:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Font, width: float) -> list[str]:
    """Linhas que cabem em `width`, quebrando entre palavras.

    Palavra maior que a linha fica sozinha nela: partir a palavra e trabalho de
    hifenizacao, e o encolhimento da fonte resolve quase todos os casos antes.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _start_size(block: TranslatedBlock, page: TranslatedPage) -> float:
    wanted = (
        block.source_font_px * SOURCE_FONT_RATIO
        if block.source_font_px
        else page.width * FALLBACK_FONT
    )
    return min(max(wanted, page.width * FONT_FLOOR), page.width * FONT_MAX)


def _fit(
    draw: ImageDraw.ImageDraw, text: str, block: TranslatedBlock, bubble: BBox, page: TranslatedPage
) -> tuple[Font, list[str], int]:
    """A maior fonte da serie decrescente em que a fala cabe na altura do balao."""
    limit = bubble.h + block.overflow_bottom
    size = _start_size(block, page)
    floor = page.width * FONT_FLOOR
    while True:
        pixels = max(int(size), 1)
        font = _font(pixels)
        lines = _wrap(draw, text, font, bubble.w * 0.92)
        if len(lines) * pixels * LINE_HEIGHT <= limit or size <= floor:
            return font, lines, pixels
        size = max(size * SHRINK, floor)


def _paint_block(
    draw: ImageDraw.ImageDraw, block: TranslatedBlock, bubble: BBox, page: TranslatedPage
) -> None:
    text = block.text.upper()
    font, lines, pixels = _fit(draw, text, block, bubble, page)
    line_height = pixels * LINE_HEIGHT
    widths = [draw.textlength(line, font=font) for line in lines]
    # Onde o texto original estava: a faixa medida, ou o balao inteiro sem ela.
    band = block.text_bbox or bubble

    pad = pixels * PADDING
    content_w = max(max(widths, default=0), band.w if block.text_bbox else 0)
    content_h = len(lines) * line_height
    center_x = bubble.x + bubble.w / 2
    center_y = band.y + band.h / 2
    top = center_y - content_h / 2

    if block.kind == "bubble":
        # Caixa branca so dentro de balao, como no leitor: fora dele ela taparia arte.
        draw.rounded_rectangle(
            (center_x - content_w / 2 - pad, top - pad / 2, center_x + content_w / 2 + pad, top + content_h + pad / 2),
            radius=pixels * 0.7,
            fill="white",
        )
    stroke = 0 if block.kind == "bubble" else max(1, pixels // 8)
    for index, (line, width) in enumerate(zip(lines, widths)):
        draw.text(
            (center_x - width / 2, top + index * line_height),
            line,
            font=font,
            fill="#111111",
            stroke_width=stroke,
            stroke_fill="white",
        )


def render_page(image_path: Path, page: TranslatedPage) -> Image.Image:
    """A pagina com as falas traduzidas escritas nela."""
    with Image.open(image_path) as source:
        canvas = source.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    for block in page.blocks:
        if block.bbox is not None and block.text.strip():
            _paint_block(draw, block, block.bbox, page)
    return canvas


def _rendered(chapter: Chapter, pages_dir: Path) -> Iterator[Image.Image]:
    for page in chapter.pages:
        yield render_page(pages_dir / page.image, page)


def write_cbz(chapter: Chapter, pages_dir: Path, target: Path) -> Path:
    """CBZ e zip de imagens em ordem de nome. Sem compressao: JPEG ja vem comprimido."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
        for page, image in zip(chapter.pages, _rendered(chapter, pages_dir)):
            with archive.open(f"{page.index:04d}.jpg", "w") as sink:
                image.save(sink, "JPEG", quality=JPEG_QUALITY)
            image.close()
    return target


PDF_DPI = 150
"""Resolucao declarada da pagina: define o tamanho em pontos, e nao os pixels."""


def write_pdf(chapter: Chapter, pages_dir: Path, target: Path) -> Path:
    """Um PDF de uma pagina por fatia, gravado enquanto as paginas sao pintadas.

    Nao usa o `save_all` do Pillow: ele junta todas as `append_images` numa lista
    antes de escrever a primeira, e o gerador daqui virava o capitulo inteiro
    decodificado na memoria - medido, 155 fatias de 800x2400 passavam de 1,1GB,
    acima do `mem_limit` de 1GB do `app`, e o servidor inteiro caia por OOM.

    Cada pagina entra como JPEG cru (`DCTDecode`), que o PDF aceita sem
    reencodar. Os objetos saem na ordem em que ficam prontos; so a arvore de
    paginas e o catalogo esperam o fim, porque precisam da lista inteira.
    """
    offsets: list[int] = []
    page_ids: list[int] = []
    # 1 e o catalogo e 2 a arvore de paginas, escritos no fim; o resto vem daqui.
    next_id = 3

    with target.open("wb") as sink:
        sink.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

        def put(object_id: int, body: bytes) -> None:
            while len(offsets) < object_id:
                offsets.append(0)
            offsets[object_id - 1] = sink.tell()
            sink.write(b"%d 0 obj\n" % object_id + body + b"\nendobj\n")

        for image in _rendered(chapter, pages_dir):
            with image:
                width, height = image.size
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=JPEG_QUALITY)
            jpeg = buffer.getvalue()
            points_w, points_h = width * 72 / PDF_DPI, height * 72 / PDF_DPI

            image_id, content_id, page_id = next_id, next_id + 1, next_id + 2
            next_id += 3
            put(
                image_id,
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d"
                b" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode"
                b" /Length %d >>\nstream\n" % (width, height, len(jpeg))
                + jpeg
                + b"\nendstream",
            )
            drawing = b"q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (points_w, points_h)
            put(content_id, b"<< /Length %d >>\nstream\n" % len(drawing) + drawing + b"\nendstream")
            put(
                page_id,
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f]"
                b" /Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                % (points_w, points_h, image_id, content_id),
            )
            page_ids.append(page_id)

        if not page_ids:
            raise ValueError("capitulo sem pagina nenhuma para exportar")

        kids = b" ".join(b"%d 0 R" % page_id for page_id in page_ids)
        put(2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(page_ids))
        put(1, b"<< /Type /Catalog /Pages 2 0 R >>")

        xref = sink.tell()
        sink.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(offsets) + 1))
        for offset in offsets:
            sink.write(b"%010d 00000 n \n" % offset)
        sink.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(offsets) + 1, xref))
    return target


WRITERS = {"cbz": write_cbz, "pdf": write_pdf}
CONTENT_TYPES = {"cbz": "application/vnd.comicbook+zip", "pdf": "application/pdf"}
