# mangatl

Traduz capítulos de mangá/mahua de inglês para português em lote, e serve um leitor
web que funciona no PC e no celular — inclusive offline.

O processamento roda uma vez por capítulo. A leitura depois é instantânea: nenhuma
chamada de API acontece enquanto você lê.

```
imagens → [detecção de balão + OCR] → extract.json → [motor] → chapter.<motor>.json → leitor
                  local, caro            cacheado      barato
```

Os dois artefatos são separados de propósito. Trocar de motor de tradução reescreve
só o segundo — o OCR não roda de novo.

---

## Rodar

O jeito curto, em qualquer sistema:

```bash
cp .env.example .env          # preencha OWNER_EMAIL, OWNER_PASSWORD e, se tiver, a chave
docker compose --profile local up -d app worker
```

O leitor fica em `http://localhost:8000/reader/`. O `worker` é um processo separado
que tira job da fila e roda o pipeline — sem ele o upload funciona e nada traduz.

### Por que contêiner e não WSL

O WSL existia aqui por um motivo local: o Smart App Control desta máquina Windows
está em modo *enforced* e bloqueia DLLs nativas sem reputação, o que derruba todo
wheel Python com extensão em C — `numpy`, `Pillow`, `pydantic`, `opencv`,
`ctranslate2`, a base inteira do pipeline.

O contêiner resolve o mesmo problema sem ser específico desta máquina: o pipeline
passa a rodar em Linux de verdade em qualquer lugar. **O caminho por WSL continua
funcionando** e está descrito abaixo, mas é o detalhe histórico que ele é.

### Requisitos reais, medidos

Medidos num capítulo real de 155 fatias, dentro do contêiner, com o backend
`rtdetr` e o motor `free`:

| | medido |
|---|---|
| RAM em repouso (`mangatl serve`) | **40 MB** |
| RAM no pico (extração + tradução) | **575 MB** |
| Capítulo inteiro, 155 fatias | **216 s** |
| Por fatia | **~1,4 s** |
| Imagem construída | 4,6 GB |

Dois avisos que esses números carregam:

- **o wheel CUDA do torch é inútil sem GPU.** São ~2,5 GB baixados para nada. O
  `Dockerfile` instala pelo índice `cpu` de propósito, antes do resto;
- **os 575 MB são o pico do worker, não o do servidor.** Quem atende HTTP não
  carrega torch; é por isso que são dois processos.

## Onde o projeto mora

Existe **uma cópia só** do projeto, no lado Windows:

```
C:\Users\Perdido\.antigravity\tradução
```

O WSL enxerga essa mesma pasta em `/mnt/c/Users/Perdido/.antigravity/tradução`.
Não é cópia nem sincronização — é o mesmo arquivo visto por dois caminhos. Editar
de um lado aparece no outro na hora.

A divisão de trabalho segue daí:

| Tarefa | Onde rodar |
| --- | --- |
| `git`, editor, Explorer | Windows, no caminho `C:\...` |
| `python`, `pip`, `mangatl`, `tesseract` | WSL, no caminho `/mnt/c/...` |

**Sempre `wsl -d Ubuntu`.** Se a distro padrão desta máquina for a
`docker-desktop` (é o caso aqui), um `wsl` sem a flag cai nela — e ela não tem
bash, então qualquer comando morre com
`execvpe(bash) failed: No such file or directory`. Para rodar algo do pipeline a
partir do Windows:

```bash
wsl -d Ubuntu -e bash -lc 'cd "/mnt/c/Users/Perdido/.antigravity/tradução" && source ~/.venvs/mangatl/bin/activate && mangatl doctor'
```

**Não clone o repositório dentro do home do Linux.** Uma cópia em `~/traducao`
ou parecido não recebe os commits feitos do lado Windows: ela congela no estado
do dia em que foi criada e, pior, o checkout Linux grava LF onde o Windows gravou
CRLF, então `git status` acusa o arquivo inteiro como modificado sem nenhuma
mudança real de conteúdo. Uma cópia dessas chegou a existir em `~/traducao` nesta
máquina; se ainda estiver lá, apague com `rm -rf ~/traducao`. O venv em
`~/.venvs/mangatl` já aponta para o caminho `/mnt/c` em modo editável, que é o
arranjo correto.

## Setup por WSL (o caminho antigo)

```bash
# 1. No PowerShell do Windows, uma vez:
wsl --install -d Ubuntu

# 2. Dentro do Ubuntu, dependências de sistema:
sudo apt update
sudo apt install -y tesseract-ocr tesseract-ocr-eng python3-venv python3-pip

# 3. O resto é o script:
cd "/mnt/c/Users/Perdido/.antigravity/tradução"
bash scripts/setup-wsl.sh            # acrescente --free para incluir o Argos
source ~/.venvs/mangatl/bin/activate

# 4. Chave da API (só para o motor `claude`):
cp .env.example .env && nano .env

# 5. Conferir:
mangatl doctor
```

**O venv fica em `~/.venvs/mangatl`, no filesystem Linux — não em `/mnt/c`.** O
DrvFs não suporta as operações de permissão que o pip faz ao instalar, e um venv
criado ali falha com `OSError: [Errno 1] Operation not permitted`. O código-fonte
continua em `/mnt/c` sem problema: as imagens seguem visíveis no Explorer.

### Motor gratuito (opcional)

```bash
bash scripts/setup-wsl.sh --free
mangatl setup-free          # baixa o modelo Argos en->pt, ~100MB, uma vez
```

---

## Onde colocar os capítulos

```
library/
└─ nome-da-serie/
   ├─ glossary.json      # opcional
   ├─ 001/
   │  ├─ 001.jpg
   │  └─ 002.jpg
   └─ 002/
```

O `glossary.json` fixa nomes e termos entre capítulos — é o que impede o mesmo
personagem de mudar de nome no capítulo seguinte:

```json
{
  "Sect Master": "Mestre da Seita",
  "Lin Feng": "Lin Feng",
  "Qi": "Qi"
}
```

### Captura de rolagem (webtoon)

Se as páginas vêm de um macro que rola a tela e costura tudo numa imagem alta, use
`slice` para importar — ele corta em páginas antes do OCR:

```bash
mangatl slice "/mnt/c/Users/<voce>/.../CapturaRolagem/<data>" minha-serie 001
```

Ele fatia só as capturas altas. Numa pasta onde convivem a costura final e os prints
brutos que a geraram, os prints são ignorados — eles se sobrepõem entre si e
duplicariam as falas.

**A pasta inteira é tratada como uma tira só.** Um macro de rolagem corta a captura
num teto fixo de altura, e esse corte é cego: medido numa captura real, um balão
terminava com o arco no fim de um arquivo e o texto no começo do seguinte, virando
duas metades que o OCR lê como ruído. O que sobra de um arquivo é carregado para o
início do próximo antes de procurar a próxima costura, então a fronteira do macro
nunca vira fronteira de página. As fatias são numeradas em sequência contínua
(`p0001.jpg`, `p0002.jpg`, …) porque uma fatia pode atravessar dois arquivos.

Isso pressupõe que uma pasta é um capítulo. Se você capturar dois capítulos na mesma
sessão, eles serão emendados — capture cada capítulo separado.

Fatiar não é opcional para esse formato. Uma captura de 1004x29799 quebra o pipeline
em três pontos: a imagem enviada à API é reduzida ao lado maior, e 29799px viram 53px
de largura (o texto deixa de existir para o modelo); os filtros de área em `[detect]`
são proporcionais à área da página, então o balão mínimo aceito fica 20x maior; e cada
página passa de 100MB descomprimida.

O corte procura a linha com menos tinta perto da altura alvo, para não partir balão ao
meio. Ele desconta as colunas de moldura da captura antes de medir — uma borda de
poucos pixels põe tinta em toda linha da página e apagaria as calhas entre painéis.

Se você já tem as imagens dentro de `library/`, o `process` fatia sozinho e guarda os
originais em `library/<serie>/<cap>/_source/`. Rodar de novo não refatia nada.

### Quando a origem já cortou a fala

Nada disso vale se as páginas chegam já fatiadas do site — aí quem escolheu onde
cortar não foi este projeto, e a escolha pode ter caído no meio de uma fala. O
detector roda por página e nunca vê as duas metades juntas: medido na emenda entre
`p0001` e `p0002` de um capítulo real, ele não acha nada acima do corte e lê
`"my Collen Eyes activated..."` abaixo dele — metade da frase, com a outra metade
visível na arte, em inglês, do lado de fora da caixa branca.

O `process` faz uma segunda passada nas emendas: monta uma faixa com o pé de uma
página e a cabeça da seguinte, e detecta ali. Na mesma emenda a leitura passa a ser
`"Just in case, I kept my Golden Eyes activated..."`, e o bloco fica registrado na
página onde começa, com `overflow_bottom` dizendo quanto dele segue na próxima — o
leitor estica a caixa branca através da emenda.

A passada não é gratuita, então ela só roda onde há sinal de corte: algum bloco
encostado na borda compartilhada. Medido no mesmo capítulo, 26 das 154 emendas.

A faixa nem sempre lê melhor, e por isso a costura só é aceita quando o texto dela
não é mais curto que o das metades que ela substituiria. Sem essa trava, uma emenda
medida trocava a frase inteira que a página já tinha lido por um trecho dela.
`seam_band` em `[detect]` controla a fração de cada página que entra na faixa.

---

## Uso

```bash
mangatl doctor                                   # o que falta instalar
mangatl slice <pasta> <serie> <capitulo>         # importa captura de rolagem, já fatiada
mangatl process library/serie/001                # um capítulo, motor padrão
mangatl process library/serie/001 --engine free  # sem custo de API
mangatl process-all serie                        # a série toda; inalterados são no-op
mangatl process-all                              # a biblioteca inteira
mangatl serve                                    # leitor em http://localhost:8000/reader/
```

Opções úteis em `process` / `process-all`:

| Opção | Efeito |
|---|---|
| `--debug-boxes` | desenha as caixas detectadas em `output/<serie>/<cap>/debug/` |
| `--dry-run` | só extrai; não chama motor de tradução |
| `--force` | refaz o OCR mesmo em páginas que não mudaram |
| `--model` | sobrescreve o modelo do `config.toml` nesta execução |

### Adicionar capítulos depois

`mangatl process-all` é idempotente: cada página guarda o sha256 da imagem, e páginas
inalteradas são puladas. Rode de novo à vontade — só o que é novo custa tempo e API.

---

## Os dois motores não são equivalentes

| | `claude` (padrão) | `free` (Argos) |
|---|---|---|
| Vê a imagem da página | sim | **não** |
| Corrige OCR embaralhado | sim | **não** |
| Tom de HQ, gíria | sim | **não**, traduz literal |
| Respeita glossário | por instrução | por proteção de marcador |
| Acha falas que a detecção perdeu | sim, com `bbox` nulo | não |
| Custo | ~$0.15 / capítulo de 40 páginas | zero |
| Rede | necessária | nenhuma, 100% local |

O `free` existe para quando você não quer gastar nada. Ele não substitui o `claude`:
Argos é um modelo MT pequeno, e como não enxerga a página, texto ruim de OCR entra
e sai ruim traduzido.

Quando os dois existem para o mesmo capítulo, o leitor mostra um seletor de motor.

---

## Calibrar a detecção de balão

Este é o passo que decide a qualidade de tudo depois. Antes de gastar API:

```bash
mangatl process library/serie/001 --dry-run --debug-boxes
```

Abra os PNGs em `output/serie/001/debug/`. Três cores, e a diferença entre elas é
que diz qual botão girar:

- **verde numerado** — virou fala dentro de balão, na ordem de leitura mostrada
- **azul numerado** — virou fala sobre a arte (SFX, narração sem moldura, placa)
- **vermelho** — o detector achou, e o filtro de OCR descartou por não parecer texto

O número traz a confiança do detector ao lado. Quando a caixa do texto difere a do
balão, ela aparece fina por dentro — é assim que se vê se o pareamento das duas
está certo.

Vermelho não é erro: a detecção é deliberadamente solta, e o OCR é quem decide.
Muito vermelho só incomoda se estiver custando tempo. Balão *sem caixa nenhuma* é o
sintoma que importa.

### Dois backends

`[detect] backend` escolhe entre eles, e `--detector` sobrescreve sem editar arquivo:

| Backend | O que é | Quando usar |
|---|---|---|
| `rtdetr` | RT-DETR-v2 treinado em HQ (`ogkalu/comic-text-and-bubble-detector`) | Padrão. Vê balão colorido, balão sem borda e texto sobre arte |
| `heuristic` | Visão clássica: região clara, fechada, convexa, com tinta moderada | Sem `torch` instalado, ou para comparar |

```bash
mangatl process library/serie/001 --dry-run --debug-boxes --detector heuristic
python scripts/compare_extractions.py <extracao-antiga>.json output/serie/001/extract.json
```

Calibrar o `rtdetr` são dois números em `[detect.rtdetr]`, e nada mais:

| Sintoma | Ajuste |
|---|---|
| Perdeu balões | baixe `confidence` |
| Ruído de arte virando fala | suba `confidence` |
| Caixa desenhada em cima do desenho | suba `artwork_confidence` |
| SFX ou narração sem moldura perdidos | baixe `artwork_confidence` |

`artwork_confidence` é maior que `confidence` de propósito: falso positivo sobre a
arte desenha caixa em cima do desenho, e isso é pior que perder um balão — balão
perdido o motor `claude` ainda recupera a partir da imagem.

**`min_confidence` em `[ocr]` acompanha a qualidade da detecção.** Ele era 45,
calibrado para a heurística solta. Medido no capítulo `manhwa/001` com o `rtdetr`:
em 45 o filtro descartava três falas corretas — uma delas `"YOU CAN USE INFORMAL
SPEECH."`, lida inteira, com confiança 40, dentro de um balão que o detector deu
0.96; em 30 entravam três lixos, inclusive a marca d'água do site. **40** é o ponto
onde as três voltam sem nenhum ruído junto. Se trocar de detector, meça de novo.

### Calibrar o backend `heuristic`

A tabela abaixo e os thresholds de `[detect]` valem **só** para este backend. O
`min_interior_brightness = 200` é o que exige papel branco, e é por isso que balão
colorido e balão sem borda são invisíveis para ele por construção.

| Sintoma | Ajuste |
|---|---|
| Perdeu balões | baixe `min_fill_ratio` ou `min_interior_brightness` |
| Perdeu balão de contorno claro | suba `INK_THRESHOLD` em `detect.py` |
| Fala boa descartada (aparece vermelha) | baixe `min_confidence` em `[ocr]` |
| Muito ruído de arte virando fala | suba `min_confidence` ou `min_letters` |

`min_letters` conta **letras seguidas**, não letras somadas: `"I I"` tem duas letras e
nenhuma palavra. Toda fala real tem ao menos uma palavra, então subir esse valor
rejeita ruído sem poder descartar diálogo.
| Balão partido em vários | suba `merge_iou` |
| Texto estilizado ou SFX perdido | baixe `min_interior_brightness` |
| Ordem errada entre balões lado a lado | ajuste `band_overlap` em `[reading_order]` |

Balões sem borda e SFX estilizado escapam da heurística. Com o motor `claude` isso é
recuperável: ele vê a página e devolve a fala com `bbox` nulo — sem coordenada não há
onde sobrepor, então o leitor a lista no fim do capítulo em vez de escondê-la.

### Um erro que não dá para corrigir localmente

O Tesseract confunde letra com dígito em fonte estilizada: `SO` vira `50` ou `90`.
Medido nesta captura, blacklistar dígitos acerta a palavra em 2 de 3 casos — mas a
confiança não diz qual está certo (num deles a leitura errada pontua *mais* alto), e
blacklistar sempre corromperia números legítimos como `50 YEARS`.

Não há correção local segura. O motor `claude` resolve porque vê a página e
reconstrói a fala antes de traduzir; o `free` não tem como perceber.

### Beco sem saída medido: trocar o Tesseract pelo EasyOCR

Parecia o próximo passo óbvio — o EasyOCR é neural, detecta e lê na mesma passada, e
seria o caminho para os balões que sobram. **Não é.** Medido nas 116 regiões que o
`rtdetr` acha no capítulo `manhwa/001`, com a saída do EasyOCR normalizada em caixa
alta (letreiro de HQ é caixa alta; o modelo de inglês dele é treinado em cena natural
e alterna maiúscula com minúscula, o que sozinho já estragaria a comparação):

| | Tesseract | EasyOCR |
|---|---|---|
| letras lidas | **2927** | 2823 |
| confiança média | **66.3** | 46.4 |
| regiões que só ele leu | 6, todas ruído de arte | **0** |
| tempo no capítulo | **28s** | 50s |

**Zero.** O EasyOCR não leu uma única região que o Tesseract tivesse perdido, e custa
`torchvision` mais onze pacotes.

O engano que levou até aqui vale registrar: um balão de fundo hachurado na `p0058`
parecia prova de que o Tesseract não dava conta de fundo padronizado. Ele lia
`"YOU CAN USE INFORMAL SPEECH."` inteiro e sem erro — quem descartava era o
`min_confidence = 45`. Baixar o limiar para 40 resolveu o caso e removeu o motivo da
troca junto.

Das 116 regiões, 11 os dois OCRs leem como vazias: são balões sem texto, e o descarte
está certo.

---

## O leitor não mostra as fatias

O fatiamento é etapa interna. O leitor monta o capítulo como uma tira contínua: as
fatias entram coladas, sem moldura, margem ou borda, e a emenda cai justamente na
linha de menos tinta que o corte escolheu — invisível no pixel. Cada fatia é
sobreposta em 1px sobre a anterior, senão o arredondamento da altura em escala abre
uma linha de fundo entre elas.

A fala traduzida é escrita **dentro do balão**, numa caixa branca do tamanho do
texto original — não do balão. O OCR guarda duas medidas por fala: `text_bbox`, a
união das caixas de palavra que o Tesseract leu, e `source_font_px`, a mediana da
altura delas. A primeira diz onde o branco precisa cobrir; a segunda, em que corpo
a página foi letrada. Sem elas o leitor só conseguia estimar a partir do balão, e
balão grande não significa texto grande.

Tudo em unidade relativa: a posição em porcentagem da fatia, o tamanho da fonte em
`cqw` (fração da largura da tira). Por isso o overlay acompanha qualquer largura de
tela sem recalcular nada — 998px de origem viram 430px no celular e as coordenadas
continuam certas.

O botão **tradução** liga e desliga o overlay, e o estado fica guardado. Desligado,
a arte aparece intacta.

### Três limites conhecidos

**Não há inpainting.** A caixa é branca e retangular. Ela cobre só a região do
texto original, então o contorno do balão sobrevive — medido neste capítulo, a bbox
do balão tem 4,3x a área do texto na mediana, e era tudo isso que a caixa pintava
antes. Funciona porque a detecção só aceita balão de interior claro
(`min_interior_brightness`), então o branco encosta na cor que já estava lá — mas
num balão colorido ou em SFX a caixa continua visível.

**O português é mais longo que o inglês.** Quando a fala não cabe, a fonte encolhe
até o piso de legibilidade (`FONT_FLOOR_CQW`, ~28px na resolução de origem) e a
partir dali **a caixa cresce** em vez de cortar o texto. Medido neste capítulo: das
88 falas, 13 encolhem, 3 chegam ao piso, 2 fazem a caixa crescer e o pior caso
passa 1,37x da altura do balão. Nenhuma foi cortada. Perder um pedaço de arte é
melhor que perder metade da fala; se preferir o contrário, baixe o piso em
`reader/overlay.js`.

**A bbox às vezes é do painel, não do balão.** Quando isso acontece a caixa branca
tapa arte. O botão de tradução é a saída.

A geometria e o dimensionamento são puros e testados:

```bash
node --test reader/overlay.test.js
```

---

## Ler no celular

O contêiner escuta em `0.0.0.0`, então o celular alcança o PC direto:

```bash
docker compose --profile local up -d app worker
```

Abra `http://<ip-do-pc>:8000/reader/` no celular (`ipconfig` mostra o IP) e use
"Adicionar à tela de início". O service worker guarda as páginas da vitrine, que é
imutável; o acervo de conta não entra em cache, e isso é deliberado — cache é por
origem e não por conta, e uma página guardada numa sessão sairia servida em outra.

**O `scripts/serve.py` foi apagado, e o motivo importa.** Ele era um servidor de
arquivos sem autorização: uma lista de pastas permitidas decidia o que saía na
rede, sem nunca perguntar *quem* estava pedindo. Isso era aceitável enquanto o
programa rodava na sua máquina, para você — e deixa de ser no instante em que
alguém além de você tem uma conta aqui, porque uma tela de login na frente de um
servidor de arquivos aberto não protege nada: a senha é pedida na tela e os
arquivos continuam saindo por URL direta. O acervo agora sai pelas rotas `/u/`,
que conferem a sessão antes de mandar um byte.

Ele também existia por um problema que o contêiner resolveu: o IP que o
`mangatl serve` imprimia dentro do WSL era o endereço interno (`172.x.x.x`), que o
celular não enxerga. Num contêiner isso não acontece.

---

## Hospedar para outras pessoas

A instância pública tem dois papéis, e eles não são simétricos:

| | **dono** | **testador** |
|---|---|---|
| Identidade | e-mail e senha | sessão anônima, sem cadastro |
| Biblioteca | persistente | expira em 48 h |
| Motor | `free` e `claude` | **só `free`** |
| Cota | nenhuma | 12 páginas, 2 capítulos, 40 MB |
| Painel | completo | só subir e traduzir o próprio |

```bash
cp .env.example .env               # OWNER_EMAIL, OWNER_PASSWORD, ACME_EMAIL
$EDITOR Caddyfile                  # troque tinta.example.com pelo seu domínio
docker compose --profile public up -d
```

**Por que a instância pública é limitada.** O motor `claude` está desligado lá
porque a chave da API seria a do dono — cada tradução de um visitante sairia da
conta dele. Quem vê só o `free` está vendo metade da ferramenta: o `free` traduz
literal e não enxerga a página. Para o resultado bom, rode na sua máquina com a
sua chave; é o que a seção **Rodar** acima descreve, e custa ~$0,15 por capítulo
de 40 páginas.

**O que o projeto não é.** É uma ferramenta de tradução para uso próprio. Não é um
acervo e não é um serviço de distribuição de capítulos: nenhuma tela lista o que
outra pessoa subiu, upload de testador expira e some sozinho, e o material da
vitrine pública é escolhido e copiado à mão pelo dono — conteúdo de usuário nunca
vira vitrine, nem por botão.

### A vitrine

Dois capítulos que qualquer visitante lê sem criar nada:

```bash
mangatl build-demo "Nome da Obra" 001 002
```

Roda na sua máquina, pega capítulos já processados da sua área e copia para
`public/demo/`, que viaja dentro da imagem. O servidor hospedado nunca escreve ali.

O que vai nesses capítulos é uma decisão de conteúdo, não de código: eles ficam
publicados na internet aberta, sem login. Domínio público, licença livre ou arte
sua — e a escolha fica registrada aqui. Se o material for licenciado, mantenha a
vitrine fora do ar e mostre a ferramenta por vídeo; a engenharia aparece igual.

> **Pendente:** os dois capítulos da vitrine ainda não foram escolhidos, e o
> endereço de contato para pedidos de remoção em `reader/termos.html` ainda é um
> `contato@exemplo.com` de exemplo.

---

## Estado atual

478 testes passando. O pipeline está completo ponta a ponta: detecção → ordem de
leitura → OCR → `extract.json` → motor → `chapter.<motor>.json` → leitor.

A detecção é o `rtdetr` por padrão. Medido no capítulo `manhwa/001` contra a
heurística: letras de diálogo 2427 → 2774, páginas com diálogo 61 → 71, e nenhuma
regressão real. Os blocos caem de 123 para 88 porque a heurística picava um balão
por linha de texto — 19.7 letras por bloco viraram 31.5.

O motor `claude` tem o formato de request e o parsing cobertos por testes com
cliente dublê, mas ainda não foi exercitado contra a API real — falta a chave.

### O que a hospedagem acrescentou

- contêiner: o pipeline roda em Linux de verdade, em qualquer máquina;
- contas: `data/users/<id>/`, com duas trancas independentes contra vazamento
  entre áreas — nome validado na borda, caminho conferido depois de resolvido;
- `library/` e `output/` deixaram de sair por caminho; o acervo sai por `/u/`,
  depois de conferida a sessão;
- fila no banco, worker em processo separado, `running` volta a `pending` na
  subida;
- cota de testador e teto global de disco;
- vitrine pública, estática e imutável, servida sem cookie.

A bateria de isolamento (`src/mangatl/isolation_test.py`) roda as sete checagens
que decidem se o servidor guarda o acervo de cada um: acervo alheio dá 404, caminho
direto dá 404, fuga de caminho dá 404 nas cinco formas, sem cookie dá 401, sessão
vencida dá 401, testador pedindo `claude` dá 429 e rota de dono dá 403.

### Ainda não feito

- **rodar a bateria contra o domínio público, de outra rede.** Ela passa contra o
  servidor local; o plano pede a mesma bateria contra o endereço real, e essa
  execução só é possível depois do deploy;
- escolher o conteúdo da vitrine e preencher o contato nos termos;
- medir a entrega por `X-Accel-Redirect` com quatro leitores simultâneos.
