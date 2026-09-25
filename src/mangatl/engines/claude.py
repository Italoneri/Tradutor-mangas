"""Motor `claude`: traduz com a API da Anthropic vendo a imagem da pagina.

A imagem e o que separa este motor do `free`. O texto que chega do Tesseract vem
embaralhado com frequencia; com a pagina em maos o modelo reconstroi a fala antes
de traduzir, e ainda reporta falas que a deteccao local nao pegou (bbox nulo).

Uma requisicao por bloco de paginas, nao por pagina: o contexto do capitulo e o
que mantem nome, tratamento e tom consistentes entre baloes.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

import anthropic
import cv2
from pydantic import BaseModel, ConfigDict

from ..config import Config, ModelPricing
from ..models import ExtractedPage, Progress, ProgressFn, TranslatedBlock, TranslatedPage, report
from .base import TranslationError

log = logging.getLogger("mangatl.claude")

AVERAGE_INPUT_TOKENS_PER_PAGE = 1200
AVERAGE_OUTPUT_TOKENS_PER_PAGE = 135
"""Tokens medios por pagina, para estimar o custo antes de enfileirar.

Calibrados pela unica medicao que este projeto tem: ~US$ 0,15 por capitulo de 40
paginas com `claude-sonnet-5`, que e o numero do README. A divisao entre entrada
e saida segue o formato da chamada - a imagem da pagina pesa na entrada, e a
resposta e uma lista curta de falas. Quando o motor rodar contra a API real de
novo, o log `operation=translate_chunk` traz os tokens de verdade: troque estes
dois numeros pela media dele."""


def estimate_usd(pricing: ModelPricing, pages: int) -> float:
    """Quanto um capitulo de `pages` paginas deve custar com estes precos.

    Estimativa, e o painel diz isso: fatiar uma captura alta aumenta o numero de
    paginas depois do upload, e fala densa sobe a saida.
    """
    per_million = (
        AVERAGE_INPUT_TOKENS_PER_PAGE * pricing.input
        + AVERAGE_OUTPUT_TOKENS_PER_PAGE * pricing.output
    )
    return round(pages * per_million / 1_000_000, 4)

SYSTEM_RULES = """Voce traduz quadrinhos (mangas e mahuas) do ingles para o portugues brasileiro.

Como traduzir:
- Fala de personagem soa como gente falando, nao como legenda literal.
- Giria vira giria equivalente em portugues, nao traducao palavra a palavra.
- Grito, hesitacao, deboche e interjeicao mantem a forca no portugues.
- CAIXA ALTA no original e enfase de HQ, nao grito literal: nao replique a caixa
  alta quando o portugues ficar estranho com ela.
- Onomatopeia e SFX: traduza so quando existir equivalente natural; senao mantenha.

O texto de entrada vem de OCR e pode estar corrompido:
- Voce recebe a imagem de cada pagina. Use a imagem como fonte da verdade.
- Reconstrua a fala real antes de traduzir, e reporte a fala reconstruida em
  source_text para permitir auditoria depois.
- Se a imagem mostra uma fala que nao esta na lista extraida, inclua com
  block_id nulo, na posicao correta da ordem de leitura.
- Se um item extraido nao for texto de fala (ruido, traco de arte), devolva
  translation como string vazia.

Formato: devolva as falas de cada pagina na ordem de leitura, esquerda para
direita e cima para baixo."""


class TranslatedLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_index: int
    block_id: str | None
    source_text: str
    translation: str


class ChunkTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[TranslatedLine]


def _encode_page(path: Path, max_side: int) -> str:
    """JPEG em base64, reduzido ao lado maximo.

    Acima de ~1568px o modelo nao le melhor e o custo em tokens sobe junto com a
    area da imagem, entao reduzir aqui e economia direta.
    """
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise TranslationError(f"nao consegui ler a imagem {path}")

    height, width = image.shape[:2]
    longest = max(height, width)
    if longest > max_side:
        scale = max_side / longest
        resized = (round(width * scale), round(height * scale))
        image = cv2.resize(image, resized, interpolation=cv2.INTER_AREA)

    encoded, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not encoded:
        raise TranslationError(f"nao consegui codificar {path} em JPEG")
    return base64.standard_b64encode(buffer.tobytes()).decode("ascii")


def _page_content(page: ExtractedPage, chapter_dir: Path, max_side: int) -> list[dict]:
    extracted = [
        {
            "block_id": block.id,
            "ocr_text": block.raw_text,
            "ocr_confidence": round(block.confidence),
        }
        for block in page.blocks
    ]
    return [
        {"type": "text", "text": f"=== PAGINA {page.index} ({page.image}) ==="},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _encode_page(chapter_dir / page.image, max_side),
            },
        },
        {
            "type": "text",
            "text": (
                "Blocos extraidos por OCR, em ordem de leitura:\n"
                + json.dumps(extracted, ensure_ascii=False, indent=1)
            ),
        },
    ]


def _system_blocks(glossary: Mapping[str, str]) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": SYSTEM_RULES}]
    if glossary:
        terms = "\n".join(f"- {source} -> {target}" for source, target in sorted(glossary.items()))
        blocks.append(
            {"type": "text", "text": f"Glossario fixo da serie (respeite exatamente):\n{terms}"}
        )
    # Estavel entre chunks e entre capitulos. Prompt curto pode nao atingir o
    # minimo cacheavel do modelo; nesse caso a API ignora sem erro.
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _chunks(pages: Sequence[ExtractedPage], size: int) -> list[Sequence[ExtractedPage]]:
    return [pages[start : start + size] for start in range(0, len(pages), size)]


class ClaudeEngine:
    name = "claude"
    sees_images = True

    def __init__(self, cfg: Config, client: object | None = None) -> None:
        self._cfg = cfg
        self.model = cfg.translation.model
        self._client = client or anthropic.Anthropic(
            timeout=cfg.translation.timeout_seconds,
            max_retries=cfg.translation.max_retries,
        )

    def translate_chapter(
        self,
        pages: Sequence[ExtractedPage],
        glossary: Mapping[str, str],
        chapter_dir: Path,
        progress: ProgressFn | None = None,
    ) -> list[TranslatedPage]:
        if not pages:
            return []

        system = _system_blocks(glossary)
        lines_by_page: dict[int, list[TranslatedLine]] = {page.index: [] for page in pages}

        # Por bloco e nao por pagina: uma chamada traduz `chunk_pages` paginas de
        # uma vez, e nada dentro dela conclui antes da resposta chegar.
        done = 0
        for chunk in _chunks(pages, self._cfg.translation.chunk_pages):
            for line in self._translate_chunk(chunk, system, Path(chapter_dir)):
                lines_by_page.setdefault(line.page_index, []).append(line)
            done += len(chunk)
            report(
                progress,
                Progress(phase="translate", done=done, total=len(pages), detail=chunk[-1].image),
            )

        return [self._assemble(page, lines_by_page.get(page.index, [])) for page in pages]

    def _translate_chunk(
        self,
        chunk: Sequence[ExtractedPage],
        system: list[dict],
        chapter_dir: Path,
    ) -> list[TranslatedLine]:
        content: list[dict] = []
        for page in chunk:
            content.extend(_page_content(page, chapter_dir, self._cfg.translation.max_image_side))

        response = self._request(content, system)

        if response.stop_reason == "refusal":
            raise TranslationError(f"pedido recusado pelo modelo: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise TranslationError("resposta truncada em max_tokens; reduza chunk_pages no config.toml")

        self._log_usage(response, page_count=len(chunk))

        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise TranslationError("resposta sem bloco de texto")
        try:
            return ChunkTranslation.model_validate_json(text).lines
        except ValueError as error:
            raise TranslationError(f"resposta fora do schema esperado: {error}") from error

    def _request(self, content: list[dict], system: list[dict]):
        try:
            return self._client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": content}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self._cfg.translation.effort,
                    "format": {
                        "type": "json_schema",
                        "schema": ChunkTranslation.model_json_schema(),
                    },
                },
            )
        except anthropic.RateLimitError as error:
            raise TranslationError(
                f"limite de taxa apos {self._cfg.translation.max_retries} tentativas"
            ) from error
        except anthropic.APIStatusError as error:
            raise TranslationError(f"API respondeu {error.status_code}: {error.message}") from error
        except anthropic.APIConnectionError as error:
            raise TranslationError("nao consegui falar com a API da Anthropic") from error

    def _log_usage(self, response, *, page_count: int) -> None:
        usage = response.usage
        pricing = self._cfg.pricing_for(self.model)
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = None
        if pricing is not None:
            cost = round(
                (
                    usage.input_tokens * pricing.input
                    + usage.output_tokens * pricing.output
                    + cached * pricing.cache_read
                )
                / 1_000_000,
                4,
            )
        log.info(
            "operation=translate_chunk pages=%d input_tokens=%d output_tokens=%d "
            "cache_read_tokens=%d estimated_usd=%s",
            page_count,
            usage.input_tokens,
            usage.output_tokens,
            cached,
            cost,
        )

    def _assemble(self, page: ExtractedPage, lines: Sequence[TranslatedLine]) -> TranslatedPage:
        detected = {block.id: block for block in page.blocks}
        blocks = [
            TranslatedBlock(
                id=line.block_id or f"p{page.index:03d}-new{position:02d}",
                bbox=detected[line.block_id].bbox if line.block_id in detected else None,
                source_text=line.source_text,
                text=line.translation,
                # Fala que o detector perdeu chega sem bbox e sem classe; `bubble`
                # e o palpite seguro, porque e o unico caso em que o leitor pode
                # pintar caixa - e sem bbox ele nao pinta nada mesmo.
                kind=detected[line.block_id].kind if line.block_id in detected else "bubble",
                text_bbox=detected[line.block_id].text_bbox if line.block_id in detected else None,
                source_font_px=(
                    detected[line.block_id].source_font_px if line.block_id in detected else None
                ),
                overflow_bottom=(
                    detected[line.block_id].overflow_bottom if line.block_id in detected else 0
                ),
            )
            for position, line in enumerate(lines, start=1)
            if line.translation.strip()
        ]
        return TranslatedPage(
            index=page.index,
            image=page.image,
            width=page.width,
            height=page.height,
            blocks=tuple(blocks),
        )
