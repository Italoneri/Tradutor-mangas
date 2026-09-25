"""Contratos de dados do pipeline.

Dois artefatos independentes atravessam o sistema:

    Extraction  - OCR cru, agnostico de motor de traducao, cacheado por sha da imagem
    Chapter     - saida de um motor especifico, e o unico arquivo que o leitor le

Manter os dois separados e o que permite trocar `--engine` sem rodar OCR de novo.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PIPELINE_VERSION = 4
"""Sobe quando detect/ocr/ordering mudam de forma que invalida extracoes salvas."""

BlockKind = Literal["bubble", "free"]
"""`free` e texto que nao mora em balao: narracao sem moldura, SFX, placa."""


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Progress(Frozen):
    """Onde o processamento esta, para quem quiser mostrar.

    Injetado e nao global: o pipeline ja loga cada pagina, mas raspar log para
    montar barra de progresso quebra na primeira vez que alguem mexe no texto da
    mensagem. Quem chama passa uma funcao e decide o que fazer com o numero -
    o CLI imprime, o painel atualiza o job.
    """

    phase: Literal["slice", "extract", "translate", "library"]
    done: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    detail: str = ""


ProgressFn = Callable[[Progress], None]


def report(progress: ProgressFn | None, update: Progress) -> None:
    """Avisa quem estiver ouvindo. Sem ouvinte, nao custa nada."""
    if progress is not None:
        progress(update)


class BBox(Frozen):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def center_y(self) -> float:
        return self.y + self.h / 2

    def intersection_area(self, other: BBox) -> int:
        overlap_w = min(self.right, other.right) - max(self.x, other.x)
        overlap_h = min(self.bottom, other.bottom) - max(self.y, other.y)
        if overlap_w <= 0 or overlap_h <= 0:
            return 0
        return overlap_w * overlap_h

    def iou(self, other: BBox) -> float:
        intersection = self.intersection_area(other)
        if intersection == 0:
            return 0.0
        return intersection / (self.area + other.area - intersection)

    def merged_with(self, other: BBox) -> BBox:
        x = min(self.x, other.x)
        y = min(self.y, other.y)
        return BBox(x=x, y=y, w=max(self.right, other.right) - x, h=max(self.bottom, other.bottom) - y)


class Detection(Frozen):
    """Uma regiao detectada, antes do OCR.

    `bbox` e `text_bbox` sao diferentes de proposito. O detector treinado devolve
    duas classes que descrevem o mesmo balao: `bubble` e o contorno inteiro e
    `text_bubble` e so o texto dentro dele. A caixa do texto da o melhor recorte
    para o OCR; a do balao da o melhor retangulo para escrever a traducao.
    Guardar so uma das duas obrigaria a escolher entre OCR pior e overlay pior.
    """

    bbox: BBox
    text_bbox: BBox
    kind: BlockKind = "bubble"
    score: float = Field(default=1.0, ge=0.0, le=1.0)
    """1.0 na heuristica, que nao tem confianca para reportar."""


class ExtractedBlock(Frozen):
    id: str
    bbox: BBox
    raw_text: str
    confidence: float = Field(ge=0.0, le=100.0)
    kind: BlockKind = "bubble"
    """Com default para o extract.json da versao 1 ainda validar na comparacao."""

    text_bbox: BBox | None = None
    """Regiao ocupada pelo texto ORIGINAL dentro do balao.

    Diferente de `bbox`, que e o balao inteiro. Num balao redondo a bbox e o
    quadrado circunscrito e o texto ocupa uma fracao dela - tapar a bbox apaga o
    contorno do balao sem necessidade. None quando o OCR nao devolveu caixa de
    palavra."""

    source_font_px: int | None = None
    """Corpo do letreiramento original, em pixels da pagina.

    Mediana da altura das caixas de palavra do Tesseract. E o unico sinal direto do
    tamanho em que a pagina foi letrada; sem ele o leitor so consegue estimar a
    partir do balao, e balao grande nao significa texto grande."""

    overflow_bottom: int = Field(default=0, ge=0)
    """Quantos pixels da fala passam da base desta pagina e seguem na proxima.

    Uma captura que chega ja fatiada pode ter cortado a fala no meio. Quando isso
    acontece a fala e uma so, e guarda-la como dois blocos duplicaria a traducao;
    ela fica na pagina onde comeca e este campo diz quanto sobra para baixo. Zero
    na esmagadora maioria dos blocos, que cabem na propria pagina."""


class ExtractedPage(Frozen):
    index: int = Field(ge=1)
    image: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    image_sha256: str
    blocks: tuple[ExtractedBlock, ...] = ()


class Extraction(Frozen):
    series: str
    chapter: str
    pipeline_version: int
    pages: tuple[ExtractedPage, ...] = ()

    def page_by_image(self, image: str) -> ExtractedPage | None:
        return next((page for page in self.pages if page.image == image), None)


class TranslatedBlock(Frozen):
    id: str
    bbox: BBox | None = None
    """None quando o motor achou uma fala que a deteccao local nao pegou.

    O leitor mostra o texto normalmente; a Fase 2 nao consegue sobrepor essas.
    """
    source_text: str
    text: str
    kind: BlockKind = "bubble"
    """O leitor trata os dois diferente: caixa branca so faz sentido dentro de balao."""

    text_bbox: BBox | None = None
    """Regiao ocupada pelo texto ORIGINAL dentro do balao.

    Diferente de `bbox`, que e o balao inteiro. Num balao redondo a bbox e o
    quadrado circunscrito e o texto ocupa uma fracao dela - tapar a bbox apaga o
    contorno do balao sem necessidade. None quando o OCR nao devolveu caixa de
    palavra."""

    source_font_px: int | None = None
    """Corpo do letreiramento original, em pixels da pagina.

    Mediana da altura das caixas de palavra do Tesseract. E o unico sinal direto do
    tamanho em que a pagina foi letrada; sem ele o leitor so consegue estimar a
    partir do balao, e balao grande nao significa texto grande."""

    overflow_bottom: int = Field(default=0, ge=0)
    """Quantos pixels da fala passam da base desta pagina e seguem na proxima.

    Uma captura que chega ja fatiada pode ter cortado a fala no meio. Quando isso
    acontece a fala e uma so, e guarda-la como dois blocos duplicaria a traducao;
    ela fica na pagina onde comeca e este campo diz quanto sobra para baixo. Zero
    na esmagadora maioria dos blocos, que cabem na propria pagina."""

    edited: bool = False
    """Se a fala foi corrigida a mao no leitor.

    Retraduzir o capitulo preserva o texto destes blocos: sem a marca, uma
    retraducao apagaria em silencio o que alguem corrigiu fala a fala."""


class TranslatedPage(Frozen):
    index: int = Field(ge=1)
    image: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    blocks: tuple[TranslatedBlock, ...] = ()


class Chapter(Frozen):
    series: str
    chapter: str
    engine: str
    model: str | None = None
    pipeline_version: int
    created_at: str
    pages: tuple[TranslatedPage, ...] = ()


class ChapterEntry(Frozen):
    chapter: str
    page_count: int
    engines: tuple[str, ...]
    cover: str | None = None
    """Caminho servivel da capa, relativo a raiz servida. None quando o capitulo nao tem."""


class ChapterState(Frozen):
    """O que existe no disco para um capitulo, traduzido ou nao.

    Irma de `ChapterEntry`, que descreve o que o leitor pode abrir. Sao dois
    conjuntos diferentes: todo `ChapterEntry` tem um `ChapterState`, o contrario
    nao vale, e e justamente a diferenca entre os dois que o painel precisa
    mostrar.
    """

    chapter: str

    image_count: int = Field(default=0, ge=0)
    """Imagens em library/<serie>/<cap>/ - NAO e o `page_count` do ChapterEntry.

    Aquele conta paginas do capitulo ja traduzido; este conta arquivos no disco.
    Os dois divergem de proposito e por muito: tres capturas de rolagem viram 155
    fatias depois do `slice_chapter_in_place`. Dar o mesmo nome aos dois numeros
    seria convidar o erro de exibir um achando que e o outro."""

    engines: tuple[str, ...] = ()
    """Vazio significa "enviado, ainda nao traduzido" - o estado que o painel
    precisa ver para oferecer o botao de traduzir."""

    incoming: bool = False
    """Existe `<cap>.incoming/`: upload em andamento ou interrompido."""


class SeriesState(Frozen):
    series: str
    """O slug, que e o nome da pasta - mesmo nome do campo em SeriesEntry."""

    title: str = ""
    cover: str | None = None
    chapters: tuple[ChapterState, ...] = ()


class SeriesMeta(Frozen):
    """Conteudo de `library/<slug>/series.json`, todo opcional.

    Existe para separar o titulo do nome da pasta. O nome da pasta e o slug: ele e
    a chave em toda URL e em todo caminho gravado nos JSONs, entao renomear a pasta
    para corrigir um titulo obrigaria a reprocessar o capitulo inteiro.

    Chave desconhecida e ignorada em vez de recusada, ao contrario do resto dos
    modelos: este arquivo e editavel na mao, e um typo nele nao pode derrubar a
    biblioteca inteira na hora de montar o indice.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str = ""
    """Vazio significa "use o slug"."""

    cover: str | None = None
    """Nome do arquivo de capa dentro da pasta da serie. Sem ele, vale o `cover.*`
    que estiver la."""

    status: str = ""
    """Texto livre: "em andamento", "completo", o que o dono quiser escrever."""


class SeriesEntry(Frozen):
    series: str
    """O slug, que e o nome da pasta."""

    title: str = ""
    chapters: tuple[ChapterEntry, ...]
    cover: str | None = None
    """A capa propria da serie, ou a herdada do primeiro capitulo que tiver uma."""

    @model_validator(mode="before")
    @classmethod
    def _title_defaults_to_the_slug(cls, data: object) -> object:
        """Sem titulo, o titulo e o slug.

        `library.json` gerado antes deste campo continua validando, e o leitor
        nunca precisa saber que o default existe.
        """
        if isinstance(data, dict) and not data.get("title"):
            return {**data, "title": data.get("series", "")}
        return data


class Library(Frozen):
    generated_at: str
    library_base: str
    """Prefixo das imagens originais, relativo a raiz servida - o leitor nao adivinha caminho."""
    output_base: str
    series: tuple[SeriesEntry, ...] = ()
