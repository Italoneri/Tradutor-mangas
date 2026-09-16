"""Carga e validacao do config.toml.

Parse na borda, confia depois: todo o resto do pipeline recebe um `Config`
validado e nunca toca em dicionario cru nem em variavel de ambiente.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONFIG_FILENAME = "config.toml"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PathsConfig(Frozen):
    library: str = "library"
    output: str = "output"
    data: str = "data"
    """Banco, areas dos usuarios e tudo que so o servidor escreve.

    Separado de `library`/`output` porque e o unico diretorio que precisa de
    backup depois que o acervo se muda para dentro dele - e o unico que um
    contêiner precisa montar como volume."""


class TranslationConfig(Frozen):
    engine: str = "claude"
    model: str = "claude-sonnet-5"
    source_lang: str = "en"
    target_lang: str = "pt"
    chunk_pages: int = Field(default=12, ge=1, le=40)
    max_image_side: int = Field(default=1568, ge=256)
    timeout_seconds: float = Field(default=600.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    effort: str = "medium"


class ModelPricing(Frozen):
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(default=0.0, ge=0)


class OcrConfig(Frozen):
    lang: str = "eng"
    psm: int = Field(default=6, ge=0, le=13)
    upscale: int = Field(default=3, ge=1, le=8)
    padding: int = Field(default=4, ge=0)
    min_confidence: float = Field(default=40.0, ge=0, le=100)
    min_letters: int = Field(default=2, ge=1)


class RtdetrConfig(Frozen):
    model_id: str = "ogkalu/comic-text-and-bubble-detector"
    confidence: float = Field(default=0.35, gt=0, lt=1)
    artwork_confidence: float = Field(default=0.60, gt=0, lt=1)
    """Maior que `confidence` de proposito: ver a justificativa em detectors/rtdetr.py."""
    device: Literal["auto", "cpu", "cuda"] = "auto"
    max_strip_height: int = Field(default=1600, ge=256)
    strip_overlap: int = Field(default=120, ge=0)


class DetectConfig(Frozen):
    backend: Literal["heuristic", "rtdetr"] = "heuristic"
    """Default no backend antigo: quem nao editou o config.toml nao muda de comportamento."""
    rtdetr: RtdetrConfig = RtdetrConfig()
    min_area_ratio: float = Field(default=0.002, gt=0, lt=1)
    max_area_ratio: float = Field(default=0.35, gt=0, le=1)
    min_aspect: float = Field(default=0.15, gt=0)
    max_aspect: float = Field(default=8.0, gt=0)
    min_fill_ratio: float = Field(default=0.55, ge=0, le=1)
    min_interior_brightness: float = Field(default=200.0, ge=0, le=255)
    min_ink_ratio: float = Field(default=0.02, ge=0, le=1)
    max_ink_ratio: float = Field(default=0.40, ge=0, le=1)
    merge_iou: float = Field(default=0.30, ge=0, le=1)

    seam_band: float = Field(default=0.40, gt=0, le=0.5)
    """Fracao de cada pagina que entra na faixa que atravessa a emenda.

    Vale para os dois backends: quem fatia nao e o detector, e a origem.

    Medido nas 6 primeiras emendas candidatas de manhwa/001, 0.40 contra 0.25: le
    melhor em 3 e nunca pior. `'I keri'` volta a ser `'I kept'`, `'CALL ME DY MY
    NAME TOO.'` volta a ser `'BY'` com a confianca subindo de 91.8 para 96.0, e a
    frase que a faixa curta entregava sem o `'AND WHEN I STEPPED'` sai inteira.
    Faixa maior da mais contexto ao detector e ao Tesseract.

    Acima de 0.5 as faixas de duas emendas vizinhas passariam a se sobrepor, e a
    mesma fala seria achada duas vezes."""


class SlicingConfig(Frozen):
    """Fatiamento de captura de rolagem. `max_height` casa com `max_image_side`
    de proposito: assim a fatia chega a API sem reducao e o texto continua legivel."""

    max_height: int = Field(default=1568, ge=256)
    min_height: int = Field(default=400, ge=64)
    tall_ratio: float = Field(default=3.0, gt=1.0)
    seam_window: float = Field(default=0.25, gt=0, le=0.5)
    format: Literal["jpeg", "png"] = "jpeg"
    quality: int = Field(default=92, ge=1, le=100)


class ReadingOrderConfig(Frozen):
    rtl: bool = False
    band_overlap: float = Field(default=0.40, gt=0, le=1)


class Config(Frozen):
    root: Path
    paths: PathsConfig = PathsConfig()
    translation: TranslationConfig = TranslationConfig()
    pricing: dict[str, ModelPricing] = {}
    ocr: OcrConfig = OcrConfig()
    detect: DetectConfig = DetectConfig()
    slicing: SlicingConfig = SlicingConfig()
    reading_order: ReadingOrderConfig = ReadingOrderConfig()

    content_root: Path | None = None
    """Onde `library/` e `output/` moram, quando nao e na raiz do projeto.

    E a unica coisa que separa a area de um usuario da de outro, e o resto do
    pipeline nao sabe que ela existe: continua recebendo um `Config` e lendo
    `library_dir`. Quem deriva o `Config` por usuario e `accounts.config_for_user`.

    None significa "a raiz do projeto", que e o modo local de sempre - o
    `mangatl process` na sua maquina nao precisa de conta nenhuma."""

    @property
    def data_dir(self) -> Path:
        """Sempre relativo a raiz do projeto, nunca a area de um usuario.

        O banco e as areas de todos moram aqui; derivar isto por usuario daria a
        cada um o proprio banco."""
        return self.root / self.paths.data

    @property
    def library_dir(self) -> Path:
        return (self.content_root or self.root) / self.paths.library

    @property
    def output_dir(self) -> Path:
        return (self.content_root or self.root) / self.paths.output

    def pricing_for(self, model: str) -> ModelPricing | None:
        return self.pricing.get(model)


def find_project_root(start: Path | None = None) -> Path:
    """Sobe a partir de `start` ate achar o diretorio que contem config.toml."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    raise FileNotFoundError(
        f"{CONFIG_FILENAME} nao encontrado a partir de {current}. "
        "Rode o mangatl de dentro do projeto."
    )


def load_config(start: Path | None = None) -> Config:
    root = find_project_root(start)
    with (root / CONFIG_FILENAME).open("rb") as handle:
        raw = tomllib.load(handle)
    return Config(root=root, **raw)
