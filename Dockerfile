# O pipeline inteiro num Linux de verdade, sem WSL e sem Smart App Control.
#
# Duas decisoes que valem a leitura:
#
# 1. O torch entra pelo indice `cpu` ANTES do resto. O wheel padrao arrasta ~2,5GB
#    de CUDA que nao serve num servidor sem GPU, e o `pip install -e .[rtdetr]`
#    logo abaixo aceita o torch ja instalado em vez de baixar o outro.
# 2. O modelo de deteccao e o pacote de idioma do Argos sao baixados na
#    CONSTRUCAO. Baixar no primeiro pedido faz o primeiro usuario esperar 300MB
#    de rede dentro do timeout do proxy - e pagar por ele.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/huggingface \
    XDG_DATA_HOME=/opt/argos

# tesseract-ocr-eng e separado do binario e o OCR falha sem ele.
# libglib2.0-0 e a unica biblioteca de sistema que a opencv headless ainda liga.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-eng \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.4"

WORKDIR /app

# Copia so o que o pip precisa para resolver: mexer no reader/ nao invalida a
# camada de dependencias, que e a cara.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[free,rtdetr,dev]"

# Os dois downloads que o primeiro usuario nao deve pagar.
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('ogkalu/comic-text-and-bubble-detector')"
RUN python -c "from mangatl.engines.argos import install_language_package; print(install_language_package('en', 'pt'))"

COPY config.toml ./
COPY reader ./reader
COPY public ./public

# Nao roda como root: um furo no upload vira escrita arbitraria com uid 0 senao.
# Os diretorios de cache mudam de dono junto porque um volume nomeado herda o
# dono do diretorio da imagem no momento em que e criado.
RUN useradd --create-home --uid 10001 mangatl \
 && mkdir -p /app/data \
 && chown -R mangatl:mangatl /app/data /opt/huggingface /opt/argos
USER mangatl

EXPOSE 8000
CMD ["mangatl", "serve", "--port", "8000"]
