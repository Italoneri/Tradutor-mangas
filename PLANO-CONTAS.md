# Plano de execução: contas, e hospedar isto para terceiros

Documento de trabalho para um agente de código. Leia **tudo** antes de escrever a
primeira linha. As fases são sequenciais e cada uma tem um portão de parada.

Pré-requisito: `PLANO-PAINEL.md` executado — `panel.py`, `jobs.py`, `admin.html`
existem e funcionam.

---

## 0. Leia isto antes de tudo

**Este plano não acrescenta uma tela de login. Ele muda a implantação.**

Os três planos anteriores acrescentaram peças a um programa que roda na sua
máquina, para você. Este transforma o programa num serviço na internet, usado
por gente que você não conhece. Quase tudo que estava certo enquanto era local
fica errado quando é hospedado — inclusive regras que eu mesmo escrevi nos
planos anteriores e que este documento revoga explicitamente.

O tamanho honesto: **as fases 0 a 5 são semanas de trabalho, não dias.** Se o
objetivo é só mostrar a ferramenta funcionando para meia dúzia de pessoas, leia
a seção 2 antes de começar — tem um caminho muito mais curto que talvez já
resolva.

---

## 1. A decisão tomada

O dono do projeto respondeu que vai hospedar e que os terceiros são pessoas que
vêm **testar a ferramenta**, e deixou a força da conta em aberto. Duas
consequências, e a segunda não é preferência, é dedução:

### 1.1 Os dois papéis não são simétricos

O pedido original foi: *"eu terei meus capítulos criados salvos e outra pessoa
que entrar em outra conta terá a dela"*. Cruzando com *"terceiros que venham
testar"*, os dois papéis querem coisas diferentes:

| | **dono** | **testador** |
|---|---|---|
| Identidade | e-mail e senha | sessão anônima, sem cadastro |
| Biblioteca | persistente, é o acervo dele | efêmera, expira |
| Motor | `free` e `claude` | **só `free`** |
| Cota | nenhuma | páginas, capítulos e tempo |
| Painel | completo | só subir e traduzir o próprio |

Fazer N contas iguais é várias vezes mais trabalho (cadastro, verificação de
e-mail, recuperação de senha, dados pessoais para guardar) e entrega menos:
um testador não quer criar conta para experimentar uma ferramenta, e você não
quer que a chave da sua API pague a curiosidade dele.

**Por isso: o dono faz login; o testador ganha uma sessão anônima com cota e
prazo.** Quem quiser guardar o que traduziu pode "adotar" a sessão criando uma
conta depois — está na Fase 6, é pequeno, e pode nunca ser preciso.

### 1.2 "Só organizar" não é uma opção aqui

Na máquina local, separar bibliotecas sem isolar de verdade seria aceitável.
Hospedado, não é — e o motivo é o seu próprio: você disse que isto é pessoal e
não um serviço de disponibilização de capítulos.

Se o testador A sobe um capítulo e o testador B consegue baixá-lo digitando a
URL, o servidor virou exatamente esse serviço, sem ninguém ter decidido isso.
**Isolamento real é o que mantém a ferramenta sendo o que você disse que ela é.**

### 1.3 O que isso implica no conteúdo

Hospedar significa que o seu servidor passa a **guardar e entregar** material que
terceiros subiram. Três decisões de projeto que seguem disso, e que este plano já
assume:

- todo upload é privado de quem subiu, sem nenhuma tela que liste o acervo alheio;
- upload de testador expira e é apagado sozinho;
- existe um caminho para remover conteúdo a pedido, e um texto curto de termos
  dizendo que o usuário responde pelo que sobe.

Não são detalhes de conformidade a resolver depois: a primeira e a segunda são
decisões de arquitetura, e refazer isolamento depois de ter dado errado é caro.

---

## 2. O caminho curto, antes de aceitar o longo

Leia isto e descarte conscientemente, em vez de por omissão.

Se o objetivo é **mostrar a ferramenta funcionando**, existe uma versão que
entrega isso em poucos dias em vez de semanas:

> Uma página onde a pessoa sobe **uma amostra** — 5 a 10 páginas, não um capítulo
> de 155 —, escolhe `free`, vê o resultado, e aquilo some em 24h. Sessão anônima
> por cookie, sem conta nenhuma. Seu acervo pessoal continua na sua máquina, onde
> está hoje, e nunca chega ao servidor público.

Isso é a Fase 1 + Fase 2 + Fase 4 deste plano, sem login, sem banco de usuários,
sem senha, sem e-mail, e com uma superfície de ataque muito menor. O testador vê
exatamente o que ele quer ver: a qualidade da tradução.

O plano completo abaixo só se paga se você quer que **o seu acervo** viva no
servidor hospedado. Vale perguntar se quer — hoje ele vive no seu PC, e mover
para a internet é o que arrasta junto quase todo o custo e quase todo o risco
deste documento.

---

## 3. Por que o app de hoje não é hospedável

Não é pessimismo, é inventário. Cada item é um bloqueio concreto.

### 3.1 O servidor de arquivos entrega tudo para qualquer um

```python
# serving.py
SERVABLE_ROOTS = ("reader", "output", "library")
return segments[0] in SERVABLE_ROOTS
```

É uma lista de pastas permitidas, não uma decisão sobre **quem** está pedindo.
Login em cima disto não protege nada: a tela pede senha e os arquivos continuam
saindo por URL direta. Esta linha é o centro de gravidade de todo o plano.

### 3.2 `http.server` não é servidor de produção

A própria documentação da stdlib diz isso. Sem HTTPS, sem limite de tamanho de
requisição no socket, sem timeout de leitura, sem recarga graciosa. Um
`ThreadingHTTPServer` exposto na internet cai com um slowloris trivial.

**Revogo aqui a regra "stdlib apenas" que escrevi no `PLANO-PAINEL.md`.** Ela
estava certa pelo motivo certo — `scripts/serve.py` roda no python do Windows,
sem venv — e esse motivo deixa de existir quando o servidor é um contêiner Linux.

### 3.3 `scripts/serve.py` morre

Ele existe porque o celular não alcança o IP do WSL. Num servidor hospedado não
há WSL nem esse problema, e um servidor de arquivos sem autorização é
exatamente o que não pode continuar existindo. Apague-o na Fase 5 e diga no
README por quê.

### 3.4 A regra dos 127.0.0.1 era a autenticação

```python
LOCAL_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
```

Hospedado, **todo** cliente chega de fora, e atrás de um proxy reverso o
`client_address` vira o IP do proxy — ou seja, se sobrar essa regra, ou ninguém
entra, ou todo mundo entra como dono. Ela tem que ser substituída por papel de
usuário, não adaptada.

### 3.5 Jobs vivem na memória

`jobs.py` guarda o registro em memória e roda um job por vez. Reiniciar o
processo perde tudo, e "um por vez" com cinco testadores é uma fila de uma hora.

### 3.6 O pipeline é pesado

RT-DETR (torch) + Tesseract + OpenCV + Argos residente. Estime **4 GB de RAM e 2
vCPU como piso**, e meça antes de escolher plano — um servidor de 1 GB não sobe
o torch. Tradução de um capítulo de 155 fatias é minutos de CPU, por usuário.

### 3.7 A chave da API é sua

`config.toml` tem `engine = "claude"` como padrão e você mediu ~$0,15 por
capítulo de 40 páginas. Hospedado sem trava, cada testador gasta o seu dinheiro,
e um laço acidental gasta muito. **Testador nunca usa o motor `claude`** — é a
trava de custo mais importante deste plano.

### 3.8 O cache do navegador não sabe de contas

O service worker (`mangatl-v4`) guarda por origem. Duas contas no mesmo navegador
compartilham cache: sair e entrar com outra conta pode servir página da anterior.

---

## 4. Arquitetura alvo

```
Caddy (HTTPS, limites de corpo, timeouts)
  └── gunicorn  →  app WSGI (rotas, sessão, autorizacao)
                     ├── SQLite (WAL): users, sessions, jobs, quotas
                     ├── fila de jobs (worker separado, mesmo contêiner)
                     └── X-Accel-Redirect → Caddy entrega os bytes
  └── disco: data/users/<user_id>/{library,output}/...
```

> **Decisão tomada na implementação (revisão de 24/09):** no lugar de `gunicorn` +
> WSGI ficou o `ThreadingHTTPServer` da stdlib atrás do Caddy, e o worker é um
> contêiner próprio, não um processo no mesmo contêiner. A troca não é etapa
> faltando: o Caddy segura o que o `http.server` não segura sozinho (corpo máximo,
> timeouts de leitura, slowloris), a entrega dos bytes já sai do Python pelo
> `X-Accel-Redirect`, e o que sobra no app é autorizar e enfileirar — trabalho de
> milissegundos, que uma thread por conexão atende. Se um dia o app precisar de
> vários processos, a fila e a sessão já moram no banco e nada precisa mudar além
> do servidor HTTP.

Três pontos que decidem o resto:

**O app autoriza, o proxy entrega.** Um capítulo são 155 JPEGs. Se cada imagem
ocupar um worker Python durante o download, quatro leitores derrubam o servidor.
O handler valida a sessão, confirma que o arquivo é daquele usuário, e responde
`X-Accel-Redirect` com um caminho interno — o Caddy lê do disco e transmite. O
Python sai do caminho dos bytes.

**O id do usuário nunca vem do usuário.** É um UUID gerado no servidor. Nome de
série e de capítulo continuam passando por `safe_component`, que já existe e já
está testado; o id não passa por ali porque nunca é digitado.

**Um caminho, uma função.** Todo acesso a disco passa por:

```python
def user_path(user_id: str, *parts: str) -> Path | None:
    """Caminho dentro da area do usuario, ou None se escapar dela.

    Resolve e confere que o resultado esta sob a raiz do usuario. `safe_component`
    ja recusa `..`, mas isto e a segunda tranca: symlink, normalizacao de unicode
    e junção de caminho absoluto passam por ela e morrem aqui. Vazamento entre
    contas e o unico risco que nao tem conserto depois de acontecer."""
```

---

## FASE 0 — Contêiner (mata a dependência do WSL)

Sem isto nada é hospedável, e de quebra resolve o Smart App Control: o pipeline
passa a rodar em Linux de verdade, em qualquer máquina.

- `Dockerfile` a partir de `python:3.12-slim`; `tesseract-ocr` e `tesseract-ocr-eng`
  via apt; `opencv-python-headless` (não a `opencv-python`, que arrasta GTK).
- torch **CPU**: `pip install torch --index-url https://download.pytorch.org/whl/cpu`
  antes do resto, como no `PLANO-DETECCAO.md`. O wheel CUDA são ~2,5 GB inúteis
  num servidor sem GPU.
- Baixe o RT-DETR **na construção da imagem**, não no primeiro pedido: sem isso o
  primeiro usuário espera o download e paga o timeout.
- `docker-compose.yml` com volume para `data/` e para o cache do HuggingFace.
- `mangatl doctor` roda dentro do contêiner e passa.

**PARE.** Construa, suba, rode um capítulo pelo CLI dentro do contêiner. Meça e
anote: RAM em repouso, RAM no pico, segundos por fatia. Esses três números
decidem o plano de hospedagem. Relate.

---

## FASE 1 — Banco, identidade e caminhos por usuário

Ainda sem login e sem tela. Só a fundação.

### 1.1 SQLite

`sqlite3` da stdlib, `PRAGMA journal_mode=WAL`, arquivo em `data/mangatl.db`.
Migrações como lista de scripts numerados aplicados em ordem, com a versão numa
tabela — não um ORM.

```sql
users(id TEXT PK, kind TEXT, email TEXT UNIQUE NULL, password_hash TEXT NULL,
      created_at TEXT, expires_at TEXT NULL)
sessions(token_hash TEXT PK, user_id TEXT, created_at TEXT, expires_at TEXT,
         last_seen TEXT)
jobs(id TEXT PK, user_id TEXT, series TEXT, chapter TEXT, engine TEXT,
     state TEXT, progress_json TEXT, error TEXT, created_at, finished_at)
usage(user_id TEXT, day TEXT, pages INTEGER, chapters INTEGER, bytes INTEGER,
      PRIMARY KEY (user_id, day))
```

`kind` é `owner` ou `tester`. `expires_at` em `users` só é preenchido para
testador.

**Guarde hash do token de sessão, não o token.** Um vazamento do banco não pode
entregar sessão ativa de ninguém.

### 1.2 Raiz por usuário

De `library/<serie>/<cap>/` para `data/users/<user_id>/library/<serie>/<cap>/`,
e o mesmo para `output/`.

`Config` ganha uma forma de produzir um `Config` derivado com `library_dir` e
`output_dir` apontando para a área de um usuário. Todo o resto do pipeline
continua recebendo um `Config` e não precisa saber que usuários existem — é o
que essa indireção compra.

Escreva `user_path` como na seção 4, com teste cobrindo: `..`, caminho absoluto,
symlink apontando para fora, `%2e%2e`, id inventado, e o caso normal.

### 1.3 Migrar o acervo que já existe

Um comando `mangatl migrate-to-accounts` que cria o usuário dono e move
`library/` e `output/` para dentro da área dele. Idempotente, e que recusa rodar
se `data/users/` já tiver conteúdo.

**PARE.** `pytest`. Rode a migração numa cópia e confirme que `mangatl process` e
`mangatl serve` continuam funcionando contra a área do dono. Relate.

---

## FASE 1.5 — A vitrine pública

Dois capítulos que qualquer visitante — alguém que clicou num link do LinkedIn —
lê sem criar nada, para ver a ferramenta funcionando, com um convite para subir
a amostra dele.

**Esta fase não depende das outras e pode ir ao ar sozinha.** Uma vitrine
estática mais um README explicando como rodar na própria máquina já é a peça de
portfólio completa. Considere publicá-la antes de escrever a primeira linha de
sessão: entrega o valor que você quer hoje e adia semanas de risco.

### 1.5.1 A vitrine é conteúdo estático, não uma conta compartilhada

A tentação é criar um usuário `demo` e deixar todo mundo entrar nele. **Não
faça.** Uma conta que todos compartilham volta a ter todos os problemas que as
Fases 2 e 3 existem para resolver — sessão, autorização, cota, alguém
sobrescrevendo o que o outro vê — para servir arquivos que nunca mudam.

A vitrine é uma terceira categoria de conteúdo, ao lado de `reader/` (código) e
da área do usuário (privada):

```
public/
└─ demo/
   ├─ <serie>/
   │  ├─ series.json
   │  ├─ cover.jpg
   │  ├─ 001/ p0001.jpg …
   │  └─ 002/ …
   └─ demo-library.json
```

Regras que fazem isso ser seguro e barato:

- servida **sem sessão e sem cookie**, por caminho, como `reader/` já é;
- **imutável**: gerada na sua máquina pelo pipeline normal e copiada para dentro
  da imagem. O pipeline hospedado nunca escreve em `public/`;
- por ser imutável e igual para todos, é a única parte do conteúdo que o service
  worker **pode** guardar em cache — ao contrário de tudo sob `/u/`;
- `SERVABLE_ROOTS` ganha `public` e perde `library` e `output` na Fase 3. A
  lista de pastas permitidas continua sendo a ferramenta certa **aqui**, porque
  aqui a resposta para "quem pode ver isto?" é de fato "qualquer um".

### 1.5.2 O que vai nesses dois capítulos

Decisão de conteúdo, e vale decidir com os olhos abertos: esses capítulos ficam
**publicados na internet aberta**, traduzidos, sem login. Isso é diferente das
duas outras coisas que este projeto faz — você traduzindo o que você lê na sua
máquina, e um testador subindo uma amostra privada que expira. É a única parte do
sistema que publica quadrinho para o mundo, e é sua a responsabilidade pelo que
estiver ali.

A boa notícia é que a escolha que resolve isso também faz uma demonstração
melhor. Opções, da mais forte para a mais fraca:

- **Domínio público.** Tirinha de jornal antiga e revista em quadrinho do início
  do século XX já caíram em domínio público em boa parte do mundo, e são
  **em inglês** — que é exatamente a entrada do seu pipeline. Arte real,
  letreiramento real, dificuldade real de OCR, e publicável sem pedir licença.
  Confira a situação de cada obra em vez de supor pela idade.
- **Licença livre.** Muita webcomic é publicada em Creative Commons; crédito e
  link costumam bastar. Leia a licença específica, não a família dela.
- **Arte sua.** Dez painéis desenhados ou montados por você. Dá trabalho, e
  compra a melhor demonstração de todas: você escolhe os casos difíceis que quer
  exibir — balão colorido, narração sem moldura, SFX estilizado, fala que estoura
  em português — em vez de torcer para eles aparecerem.

Para quem vem do LinkedIn, isso também é sinal melhor. "Construí um pipeline de
detecção, OCR e tradução, demonstrado em material que posso publicar" lê muito
diferente de uma página hospedando scanlation. O que você quer que avaliem é a
engenharia, e a engenharia aparece igual — ou melhor — em domínio público.

Se mesmo assim a vitrine for de material licenciado, então mantenha-a fora do ar
público e mostre-a por vídeo ou captura de tela no README. A ferramenta
demonstra-se igual, e não há servidor seu distribuindo obra de terceiro.

### 1.5.3 Como gerar

Um comando `mangatl build-demo <serie> <cap>...` que roda na sua máquina, pega
capítulos já processados da sua área e copia imagens e `chapter.*.json` para
`public/demo/`, gerando o `demo-library.json`.

Inclua **os dois motores** quando existirem: o seletor de motor do leitor já
funciona, e deixar o visitante comparar `free` com `claude` lado a lado mostra em
dois cliques por que a escolha do motor importa. É a melhor demonstração que o
projeto tem e ela já está construída.

### 1.5.4 O leitor

- `/` sem sessão → vitrine: as séries de `public/demo/`, com um cartão claro de
  "suba a sua amostra" e um link para o repositório;
- `/` com sessão → a biblioteca do usuário, como hoje;
- ler um capítulo da vitrine usa o mesmo leitor, o mesmo overlay e o mesmo
  seletor de motor. Nada de caminho separado de renderização: se a vitrine
  precisar de código próprio para exibir, ela deixou de demonstrar o produto.

A vitrine nunca mostra nada da área de ninguém, e nenhuma tela lista acervo
alheio. Essa parte continua valendo.

**PARE.** Publique só a vitrine e abra de uma rede que não é a sua, sem cookie.
Confirme: lê os dois capítulos, troca de motor, **nenhum cookie foi criado**, e
`/library/...` e `/output/...` não respondem. Relate.

---

## FASE 2 — Sessões

### 2.1 Testador: sessão anônima, criada tarde

Usuário `tester` com `expires_at` em 48h e uma sessão. Sem cadastro, sem e-mail,
sem senha.

**A sessão nasce no primeiro upload, não no primeiro acesso.** Com a vitrine
pública da Fase 1.5, a home é uma página aberta que qualquer um alcança: robô de
busca, prévia de link do LinkedIn, monitor de uptime. Criar usuário a cada `GET`
enche a tabela de visitante que nunca vai subir nada, e dá a cada robô uma área
em disco.

Enquanto ninguém escreveu nada, não existe sessão e não existe cookie — o que
também deixa a vitrine rápida e sem aviso de cookie.

Cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, nome sem prefixo
identificável do produto. Valor = token aleatório de 32 bytes
(`secrets.token_urlsafe`); no banco só o `sha256` dele.

### 2.2 Dono: e-mail e senha

`hashlib.scrypt` da stdlib com sal por usuário — evita dependência e é adequado.
Se preferir `argon2-cffi`, também serve; o que não serve é sha256 puro.

Uma variável de ambiente `OWNER_EMAIL` cria o dono na primeira subida se ele não
existir, com senha vinda de `OWNER_PASSWORD`, que é lida uma vez e **não** fica
no banco em claro nem no log.

Limite de tentativas por IP e por e-mail: 5 em 15 minutos, resposta 429. Sem isso
a senha é questão de tempo.

### 2.3 CSRF

Toda rota que escreve exige cabeçalho `X-Requested-With` **e** origem conferida
(`Origin`/`Referer` batendo com o host). Cookie `SameSite=Lax` sozinho não cobre
formulário em `POST` de outro site.

**PARE.** `pytest`. Teste: sessão sem cookie cria testador; cookie forjado é
rejeitado; sessão expirada é rejeitada; login errado 5x dá 429. Relate.

---

## FASE 3 — Autorizar cada byte

**A fase que define se isto funciona.** Enquanto ela não terminar, não exponha
nada na internet.

### 3.1 O filtro de caminho deixa de ser lista de pastas

`is_servable` some do caminho de conteúdo. No lugar:

- `reader/` (a PWA) continua público e estático — é código, não conteúdo;
- `library/` e `output/` **deixam de ser servíveis por caminho**. Passam a ser
  alcançados por rotas autorizadas:

```
GET /u/pages/<serie>/<cap>/<arquivo>       imagem de pagina
GET /u/chapters/<serie>/<cap>/<motor>      chapter.<motor>.json
GET /u/library                             o indice do usuario logado
```

Nenhuma delas recebe id de usuário: o usuário vem da sessão. **Se um id de
usuário aparecer em qualquer URL, o desenho está errado** — é convite para
trocar o número e ler o acervo do vizinho.

### 3.2 Entrega pelo proxy

Depois de autorizar, responda com cabeçalho de redirecionamento interno
(`X-Accel-Redirect`) apontando para um prefixo que o proxy serve e que **não** é
alcançável de fora. O worker Python devolve uma resposta vazia e volta a atender.

Se preferir não depender disso no começo, transmita em pedaços pelo Python e
anote como dívida — mas meça: 155 imagens por capítulo, 4 leitores simultâneos.

#### 3.2.1 A armadilha do prefixo interno — leia antes de escrever o Caddyfile

**Encontrada em auditoria, na primeira versão deste arquivo. Não é hipótese.**

`internal` é diretiva do **nginx** (`location /interno/ { internal; }`), que marca
uma rota como alcançável só por redirecionamento interno. **O Caddy não tem
equivalente.** Escrever um bloco de primeiro nível confiando que existe produz
isto, que parece certo e está aberto para a internet:

```
# ERRADO. Este bloco responde de fora.
handle_path /_internal/* {
	root * /app
	file_server
}
```

`handle_path` é uma rota exclusiva de primeiro nível no bloco do site: um pedido a
`/_internal/qualquer/coisa` é atendido ali e **nunca chega ao `reverse_proxy`**.
Nada restringe quem pede. Com `./data:/app/data` montado no contêiner do proxy, a
sequência de exploração é curta:

1. `GET /_internal/data/mangatl.db` — o banco inteiro: e-mail do dono, hash da
   senha, todos os `user_id`, hashes de sessão;
2. com os UUIDs, `GET /_internal/data/users/<uuid>/library/<serie>/001/p0001.jpg`
   — qualquer arquivo de qualquer conta.

Toda esta Fase 3 vira decoração, porque existe um caminho que não passa por ela. E
como o repositório vai a público, `data/mangatl.db` deixa de ser adivinhação e
passa a ser documentação.

**O bloco de primeiro nível não é só perigoso: é desnecessário.** No Caddy o
`handle_response` já roda dentro do `reverse_proxy` e não reentra nas rotas do
site. A entrega mora ali dentro, e o prefixo nunca existe como rota pública:

```
@accel header X-Accel-Redirect *
handle_response @accel {
	route {
		rewrite * {rp.header.X-Accel-Redirect}
		uri strip_prefix /_internal
		root * /app
		file_server
	}
}
```

O `route` está ali para fixar a ordem: dentro de um sub-roteador o Caddy ordena
diretiva pela ordem padrão dele, não pela ordem escrita, e `rewrite` precisa
acontecer antes do `strip_prefix`.

**A verificação não é reler a configuração, é o `curl` de fora** — ordenação de
diretiva em `handle_response` é exatamente o tipo de coisa que se testa em vez de
se deduzir. Está na bateria abaixo, casos 8 e 9.

Vale a mesma desconfiança para qualquer comentário que afirme uma proteção: o
buraco original vinha acompanhado de duas frases dizendo que ele não existia, uma
no `Caddyfile` e outra no `panel.py`. **Comentário não é controle.** Se um
comentário afirma que algo não é alcançável, ou existe um teste que prova, ou a
frase sai.

### 3.3 O leitor muda de endereço

`app.js` monta URL hoje assim:

```js
`${ROOT}/${library.library_base}/${serie}/${cap}/${image}`
```

Passa a chamar `/u/pages/...`. `library.json` deixa de ser arquivo estático e
vira resposta de `/u/library`.

### 3.4 Service worker

- `VERSION` sobe para `mangatl-v5`;
- nada sob `/u/` e `/api/` entra em cache — é conteúdo autorizado, e cache
  cruzando sessão é vazamento;
- ao sair da conta, `caches.delete` de tudo, no logout **e** na primeira carga em
  que o id da sessão mudar.

Isto custa o modo offline do leitor hospedado. É o preço do isolamento; o leitor
local, que você continua usando, mantém o cache.

### 3.5 O papel substitui o `127.0.0.1`

`is_local_client` sai. As rotas do painel passam a exigir sessão, e as
destrutivas (apagar, editar série, glossário) exigem `kind == "owner"`.

Atrás de proxy, IP de cliente só vale se vier de `X-Forwarded-For` **e** você
confiar no proxy explicitamente. Use IP só para limite de tentativas, nunca para
autorizar.

**PARE. Teste de invasão caseiro, obrigatório antes de qualquer deploy:**

1. Logado como testador A, peça `/u/pages/<serie de B>/001/p0001.jpg` → 404
   (não 403: 403 confirma que existe).
2. Peça `/library/...` e `/output/...` direto → 404.
3. `/u/pages/../../../.env`, com `..` codificado de três formas → 404.
4. Sem cookie, todas as rotas `/u/` → 401.
5. Com cookie de sessão expirada → 401.
6. Testador tentando `POST /api/jobs` com `engine=claude` → 403.
7. Testador tentando rota de dono → 403.

Os sete acima batem no app Python e podem rodar contra o handler direto, como o
`isolation_test.py` faz. **Os dois seguintes não:** o buraco da seção 3.2.1 mora
no proxy, e teste que não cruza a camada que falha não protege nada. Eles só
valem contra o servidor real, **com o Caddy na frente**:

8. `GET /_internal/data/mangatl.db` → 404.
9. `GET /_internal/data/users/` e `/_internal/` → 404.

E um de regressão, porque a correção mexe justamente no caminho de entrega:

10. Logado, abrir um capítulo inteiro e confirmar que todas as imagens chegam,
    com `X-Accel-Redirect` saindo do app e o Caddy transmitindo — não o Python.

Relate cada um com a resposta observada. **Nenhum pode falhar.**

Repita os casos 8, 9 e 10 na Fase 5, agora contra o domínio público e de outra
rede. Em ambiente local o Caddy costuma nem estar de pé — o perfil `public` não
sobe por padrão —, e um teste que não rodou passa com a mesma cara de um que
passou.

---

## FASE 4 — Cotas, fila e limpeza

### 4.1 Cotas do testador

Constantes num lugar só, com o número justificado:

```python
TESTER_MAX_PAGES_PER_CHAPTER = 12
TESTER_MAX_CHAPTERS = 2
TESTER_MAX_UPLOAD_BYTES = 40 * 1024 * 1024
TESTER_SESSION_HOURS = 48
TESTER_ENGINES = ("free",)
"""Sem `claude`: a chave da API e do dono, e a conta chega para ele."""
```

Doze páginas mostram a qualidade da tradução tão bem quanto 155 e custam 1/13 do
CPU. Cota estourada devolve 429 com mensagem legível, não erro genérico.

### 4.2 Fila que sobrevive a reinício

Jobs saem da memória e vão para a tabela. Um worker (processo separado, mesmo
contêiner) pega o próximo pendente. Concorrência configurável, começando em 1.

Job de testador entra atrás de job de dono. Um job por usuário ao mesmo tempo.

Ao subir, todo job em `running` volta para `pending` — ninguém ficou rodando
durante o reinício.

### 4.3 Limpeza

Tarefa periódica: apaga usuário `tester` vencido, a área dele em disco, e as
sessões. Loga quanto liberou. Um teto global de disco que, ao ser atingido,
recusa upload novo com 507 em vez de encher o volume.

A limpeza roda no laço do worker (`cleanup.sweep`, a cada 15 min), e desde a Fase
7.6 também apaga `.upload` órfão com mais de uma hora e área de espera parada há
mais de sete dias. Com o worker parado, ou no meio de um capítulo longo, ela
também para; é aceito, porque os prazos são de horas e dias. A alternativa, se um
dia pesar, é uma thread própria no `app`.

**PARE.** Suba dois testadores, estoure a cota de um, confirme que o outro não é
afetado. Reinicie o contêiner no meio de um job e confirme que ele volta. Relate.

---

## FASE 5 — Publicar

- Caddy na frente: HTTPS automático, `request_body max_size` compatível com o
  upload, timeouts de leitura e escrita, cabeçalhos de segurança
  (`Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy`).
- **Reveja a seção 3.2.1 com o `Caddyfile` aberto do lado.** É o único lugar do
  sistema onde um erro de configuração desfaz a Fase 3 inteira sem nenhum código
  errado, e ele não aparece em teste local — o perfil `public` não sobe por
  padrão, então o proxy costuma nem estar de pé quando o `pytest` passa.
- O contêiner do proxy monta `data/` e `public/` **somente leitura**: ele entrega
  bytes e nunca escreve no acervo. Monte só o que ele precisa servir; tudo que
  for montado ali é alcançável se alguma rota vazar.
- Segredos por variável de ambiente. **O `.env` do projeto não vai para a
  imagem** — confira com `docker history` que a chave não ficou numa camada.
- Log estruturado sem dado pessoal e sem nome de arquivo do usuário.
- Backup do `data/` — o acervo do dono agora mora lá.
- Apague `scripts/serve.py` e explique no README que o servidor de arquivos sem
  autorização não sobrevive à hospedagem.
- Página curta de termos: o usuário responde pelo que sobe, uploads de teste
  expiram em 48h, e um endereço para pedir remoção.

### 5.1 O README é parte da entrega, não sobra

A instância pública nunca gasta a chave do dono, e por isso ela demonstra só
metade da ferramenta: o visitante vê o motor `free`, que traduz literal e não
enxerga a página. Quem quiser o resultado bom roda o projeto na própria máquina
com a própria chave — e o README é o que torna isso possível. Ele é a peça que
converte "vi uma demo" em "instalei e usei".

Escreva, em seções curtas:

- **Rodar local com chave própria.** Onde obter a chave, `cp .env.example .env`,
  o que `mangatl doctor` confere, e a primeira tradução. O caminho por contêiner
  vem antes do caminho por WSL: a partir da Fase 0 o `docker compose` funciona em
  qualquer sistema, e o WSL passa a ser o detalhe histórico que ele é.
- **Quanto custa.** O seu número medido: ~$0,15 por capítulo de 40 páginas com
  `claude`, e zero com `free`. Diga também o que a diferença compra — o `claude`
  vê a imagem e reconstrói fala que o OCR embaralhou; o `free` não. A tabela
  comparativa que já existe no README atual cobre isso e deve continuar lá.
- **Por que a instância pública é limitada.** Uma frase: o motor `claude` está
  desligado lá porque a chave seria a do dono. Sem isso o visitante conclui que a
  ferramenta é fraca, quando o que ele viu foi a metade gratuita dela.
- **Requisitos reais.** Os números medidos na Fase 0: RAM de pico, segundos por
  fatia, e que o wheel CUDA do torch é desnecessário sem GPU.
- **O que o projeto não é.** Uma linha dizendo que é ferramenta de tradução para
  uso próprio, não acervo nem serviço de distribuição, e que o material da
  vitrine é publicável. É a mesma frase que orienta todo este plano; escrita no
  README, ela também responde a pergunta antes que alguém precise fazê-la.

**PARE.** Rode a bateria da Fase 3.7 **contra o domínio público**, de outra
máquina e de outra rede. Relate.

---

## FASE 6 — Opcional, só se o teste mostrar que precisa

- **Adotar a sessão:** testador que quer guardar cria conta e o conteúdo da
  sessão passa para ela. Uma transação, um `UPDATE` de `user_id` e um `mv`.
- Recuperação de senha por e-mail (arrasta servidor de e-mail; adie ao máximo).
- Mais de um dono.
- Cota de custo em dólar por usuário, se algum dia um testador puder usar
  `claude`.

---

## FASE 7 — Furos achados na revisão de 24/09/2026

Revisão do código feita com as fases 0 a 5 prontas localmente e antes do deploy
público. **Nenhum destes furos aparece no `pytest`**: cada um mora na combinação
de duas peças que, separadas, funcionam e estão testadas. Esse é o mesmo padrão
do `handle_path /_internal/*` da seção 3.2.1, e por isso cada item abaixo vem
com o teste que teria pegado o furo.

A ordem é a prioridade. De 7.1 a 7.5, faça antes de apontar o domínio.

### 7.1 `PUBLIC_SHOWCASE=0` precisa valer na API, e não só na tela

**Onde:** `panel.py`, em `_dispatch`. `showcase_is_public()` só é lida em
`_session_payload`.

**O furo:** a flag decide o que a tela mostra, mas o `_dispatch` abre sessão
anônima de testador em qualquer escrita sem cookie, esteja a flag ligada ou
desligada. A instalação "de uma pessoa só" esconde o fluxo de testador na
interface e continua aceitando esse fluxo pela API: cria usuário, grava 40MB e
enfileira job.

**Fazer:** só chamar `_open_tester_session` quando `showcase_is_public()` for
verdadeiro. Sem vitrine, escrita sem sessão responde 401, igual à leitura.

**Teste:** com `PUBLIC_SHOWCASE=0`, um `POST /api/series` sem cookie (com CSRF
válido) leva 401 e a tabela `users` não ganha linha nenhuma. Com `1`, o
comportamento atual continua igual.

### 7.2 O IP do limite de login é sempre o do proxy

**Onde:** `Context.client_ip = self.client_address[0]`, em `_dispatch`.

**O furo:** atrás do Caddy, todo pedido chega com o IP do Caddy. A chave `ip:`
do `login_is_throttled` vira uma só para o mundo inteiro: cinco senhas erradas
de qualquer pessoa trancam o login de todos, **dono incluso**, por quinze
minutos. A docstring de `client_ip` já registra a ressalva, mas a consequência
dela não foi tirada.

**Fazer:**

- No `Caddyfile`, dentro do `reverse_proxy`, usar `header_up X-Real-IP {remote_host}`.
  Isso sobrescreve o valor que o cliente tiver mandado.
- No app, ler `X-Real-IP` **só** quando `client_address` for o proxy (a rede
  do compose, ou uma variável `TRUSTED_PROXY`). Fora disso, o cabeçalho é
  ignorado, porque sem proxy quem o escreve é o cliente.
- Criar o comando `mangatl reset-login`, que limpa `login_attempts` pela máquina.
  É a porta dos fundos do dono quando alguém tranca a conta dele pela chave
  `email:`, como a seção 2.2 já previa que pode acontecer.

**Teste:** com dois IPs simulados, cinco erros de um não trancam o outro. Um
`X-Real-IP` que chega por uma conexão que não vem do proxy é ignorado. Pôr um
caso no `caddy_test.py` que confira o cabeçalho com o proxy de pé.

### 7.3 A cota do testador vaza por quatro lados

A seção 4.1 pede "cotas aplicadas antes de gravar", e isso vale para página
avulsa. O zip e a concorrência escapam dessa regra:

- **Zip que cresce ao extrair.** `check_upload_bytes` confere o tamanho
  *compactado*, e a extração só tem o teto global de 2GB
  (`MAX_ARCHIVE_EXPANDED_BYTES`). Um zip que cabe nos 40MB pode ocupar muito
  mais depois de extraído. Passar `upload_headroom` do usuário como teto do
  descompactado em `_extract_from`, e recusar a entrada cujo `file_size` passe
  de `MAX_PAGE_BYTES`.
- **Página de zip que não passa pelos bytes mágicos.** `_put_page` confere
  `image_suffix`; a extração só confere a extensão do nome. Ler o começo de cada
  entrada antes de gravar. Uma entrada que não é imagem condena o arquivo
  inteiro, pelo mesmo raciocínio do caminho de fuga.
- **Teto de páginas por zip, e não por capítulo.** `limit` conta as entradas
  do zip e ignora o que já está na área de espera. Somar as duas coisas.
- **Uploads em paralelo.** O servidor é `ThreadingHTTPServer`, e "conferir a
  cota e gravar" não é atômico: N `PUT` simultâneos passam todos pela conferência
  antes de qualquer um gravar. Pôr um `threading.Lock` por `user_id` em volta de
  conferir e gravar em `_put_page`, `_put_archive` e `_create_chapter`.

**Teste:** um zip de poucos MB que se expande acima da folga leva 429, e a área
de espera some. Um zip com um arquivo que não é imagem renomeado para `.jpg`
leva 422. Dez `PUT` paralelos que, somados, passam da cota deixam a área final
dentro da cota.

### 7.4 O teto global de disco também tranca o dono

**Onde:** `check_disk(ctx.base)` vale para toda sessão.

**O furo:** testadores que enchem o teto global fazem o upload do dono levar
507. É o mesmo desenho do 7.2: um limite pensado para proteger o dono acaba
sendo usado contra ele.

**Fazer:** reservar uma folga para o dono. Por exemplo, testador para em 80% do
teto e dono vai até 100%. Somar a isso um limite de testadores novos por IP por
hora, que depende do 7.2 estar feito.

**Teste:** com um teto baixo, encher o disco com testadores. O dono continua
subindo.

### 7.5 Job que derruba o worker entra em laço

**Onde:** `jobs.requeue_running`, chamado toda vez que o worker sobe.

**O furo:** um capítulo que estoura o `mem_limit: 6g` mata o worker por OOM. O
Docker reinicia o worker, o job volta para `pending`, o worker pega o job de
novo e morre de novo. A fila inteira fica parada atrás desse capítulo, sem erro
nenhum na tela.

**Fazer:** uma migração com a coluna `attempts`. O `claim_next` incrementa o
contador. O `requeue_running` marca o job como `failed`, com uma mensagem legível
("o worker caiu 3 vezes neste capítulo"), quando `attempts >= 3`.

**Teste:** três subidas seguidas com o mesmo job em `running` deixam esse job em
`failed`, e o próximo job da fila roda.

### 7.6 A limpeza não varre tudo que pode sobrar

A seção 4.3 cobre testador vencido e sessão vencida. Ficaram de fora:

- `data/uploads/*.upload` órfãos, que sobram quando o processo morre no meio
  de `_spool_body`. O `finally` do `_run` só roda se o processo continuar vivo.
  Apagar os que tiverem `mtime` com mais de uma hora.
- Pastas `*.incoming` abandonadas, inclusive as do dono. Apagar depois de N
  dias, ou pelo menos mostrar no painel que estão lá.
- A limpeza roda no laço do worker. Com o worker parado, ou no meio de um
  capítulo de quatro minutos, a limpeza também para. Isso é aceitável, mas fica
  anotado aqui; a alternativa é uma thread própria no `app`.

**Teste:** no `sweep`, um `.upload` velho some e um recente fica.

### 7.7 Remoção que falta

A mensagem de cota do testador diz "Apague um para subir outro", e não existe
rota que apague. Além disso, `DELETE .../incoming` é só do dono, então um
testador com upload quebrado fica preso até a sessão expirar.

**Fazer:**

- `DELETE /api/series/<s>/chapters/<c>`: apaga o capítulo em `library/` e em
  `output/` na área de quem pede, com CSRF. Recusa quando há job `pending` ou
  `running` para esse capítulo.
- Liberar `DELETE .../incoming` para a sessão. A marca `owner` existia para
  proteger o acervo do dono, mas a área vem do cookie e não da URL: o testador
  só alcança a própria área. Mantenha como `owner` só o que muda o acervo
  *inteiro* (glossário, `series.json`, capa).
- `DELETE /api/series/<s>`, só para o dono, com confirmação na interface.

**Teste (isolamento):** um testador que apaga um capítulo com o mesmo nome de
um capítulo do dono apaga só o seu, e o do dono continua lá. Apagar um capítulo
que não existe na própria área leva 404.

### 7.8 Menores

- `/api/health` é público e responde a versão do Python e `has_api_key`. Na
  instância pública, responder só `ok`, e deixar o resto para a sessão do dono.
- `Strict-Transport-Security ... includeSubDomains` vale para todos os
  subdomínios do domínio escolhido. Confirmar antes de ligar.
- A seção 4 descreve `gunicorn` + WSGI, e o que foi implementado é
  `ThreadingHTTPServer` atrás do Caddy. Anotar a decisão na seção 4, para o
  próximo leitor não achar que falta uma etapa.
- Há uma pasta vazia `src;C` na raiz, resto de um comando com caminho do
  Windows. Apagar. `.claude/` está fora do `.gitignore`.
- README: a tabela de calibração da heurística foi cortada pelo parágrafo do
  `min_letters`, e as três últimas linhas ficaram soltas. `--profile local` não
  existe no `docker-compose.yml`; funciona só porque `app` e `worker` não têm
  perfil.
- O checklist marcava "termos com endereço para remoção" como feito, mas
  `reader/termos.html` ainda diz `contato@exemplo.com`. O item foi desmarcado
  abaixo.

**PARE.** Rode a bateria inteira, e rode o `caddy_test.py` com os casos novos e
o proxy de pé. Relate o que falhou antes de corrigir.

---

## FASE 8 — O que mais falta no uso

Não são furos. É o que a revisão achou que mais pesa para quem usa a ferramenta
todo dia, em ordem de valor.

1. **Corrigir uma fala à mão, no leitor.** Hoje, um erro de OCR ou de tradução
   só se corrige retraduzindo o capítulo inteiro. A ideia: clicar no balão,
   editar e gravar em `chapter.<motor>.json` pela área da sessão, validando pelo
   modelo. Marcar a edição (`edited: true`) para que uma retradução não apague o
   que foi corrigido à mão.
2. **Retraduzir uma página só.** Um job com `pages=[...]`. O `extract.json` já é
   por página, com sha256, então a peça que falta é a rota e o botão.
3. **Estimar o custo antes de um job `claude`.** Os preços já estão em
   `[pricing]`: páginas × tokens médios medidos, mostrado no painel antes de
   confirmar.
4. **Exercitar o motor `claude` contra a API real.** O README admite que isso
   nunca foi feito. Rodar um capítulo curto e conferir schema, `effort`,
   `stop_reason` e o custo medido contra `[pricing]`. Registrar o número no
   README.
5. **`mangatl backup`.** A Fase 5 pede backup de `data/` e nada o implementa.
   Usar `sqlite3.Connection.backup`, porque copiar o arquivo com o WAL aberto
   não é backup, mais um tar de `data/users/`.
6. **Trocar a senha do dono sem reiniciar, e "sair de todas as sessões"**
   (`DELETE FROM sessions WHERE user_id = ?`). Hoje a senha só muda via `.env`
   e reinício.
7. **Exportar o capítulo traduzido** como CBZ ou PDF, com o texto renderizado
   na imagem. O Pillow já está no worker.
8. **CI.** GitHub Actions rodando o `pytest`, com os testes de `rtdetr` e
   `argos` atrás de marcador, e o `node --test reader/overlay.test.js`.
9. **Inpainting simples em balão colorido**, o primeiro limite conhecido do
   README. É opcional: meça com `scripts/report_overlay.py` antes de decidir.

---

## FASE 9 — Segunda revisão, 25/09/2026

Mesmo padrão da Fase 7: nada disto aparecia no `pytest`. Os dois primeiros só
existem com a instância montada de verdade — um depende da CSP que o Caddy põe, o
outro do `mem_limit` do compose.

### 9.1 A CSP do Caddyfile apagava a posição de todo balão

`bubbleHtml` escrevia a geometria num `style="..."` dentro do HTML, e a CSP tem
`style-src 'self'`, que recusa atributo de estilo inline. Atrás do Caddy todo
balão caía no canto de baixo da fatia, e `fitSlice` lia `--limit` vazio, então a
fonte também nunca encolhia. Sem o proxy não há CSP, e por isso tudo funcionava
no desenvolvimento. Conferido no Chromium, com e sem o cabeçalho.

**Feito:** a geometria vai em `data-*` e `applyBubbleGeometry` aplica pelo CSSOM,
que a CSP não bloqueia. `img-src` ganhou `blob:` para a prévia das páginas no
painel, que tinha o mesmo problema. `reader/csp.test.js` falha se voltar a
aparecer `style=` no HTML do leitor, e o CI agora roda todo `reader/*.test.js`.
SW em v8.

### 9.2 Exportar PDF derrubava o `app`

`Image.save(save_all=True, append_images=gerador)` junta o gerador inteiro numa
lista antes de escrever a primeira página. Medido com fatias de 800x2400: 80
páginas, 635MB; 155 páginas, 1,18GB — acima do `mem_limit: 1g`, e quem cai é o
servidor HTTP inteiro, não só o pedido.

**Feito:** `write_pdf` escreve o PDF à mão, uma página por vez, com o JPEG cru
(`DCTDecode`). Pico medido: 61MB para 155 e para 400 páginas. Teste confere que a
página anterior já está no arquivo quando a próxima é pintada.

### 9.3 O tempo do login entregava o e-mail do dono

A mensagem era a mesma para e-mail errado e senha errada, mas o scrypt só rodava
quando o e-mail existia: ~98ms contra ~0ms. **Feito:** sem dono, a senha é
conferida contra um hash de mesmo custo.

### 9.4 Testador criava séries sem limite

`POST /api/series` não passava por cota nenhuma. Pasta vazia quase não tem
bytes, então nem a cota de 40MB nem o teto de disco a seguravam. **Feito:**
`check_new_series`, com o mesmo teto dos capítulos (2).

### 9.5 Anotados na revisão, feitos depois

- O comentário do `Caddyfile` dizia que `read_timeout`/`write_timeout` fecham
  conexão lenta. Esses dois valem para a conexão do Caddy com o `app`, e não
  para a do cliente com o Caddy. **Feito:** `timeouts` de `servers` nas opções
  globais — `read_header 10s` (é ele que fecha slowloris), `read_body 30m`
  (500MB a 2 Mbps leva ~33min; mais curto cortaria upload de quem ia terminar)
  e `idle 2m`. Comentário do `transport` corrigido. `caddy validate` passa.
- `mangatl backup` grava em `data/backups/`, no mesmo disco que ele copia, e fora
  da conta do teto de disco. Serve contra erro humano, não contra perder o disco.
  **Feito:** o README diz isso e aponta `--out` para outro volume; a CLI já
  avisava no fim da cópia.
- `.impeccable/hook.cache.json` estava versionado, com caminhos da máquina
  Windows, e gerava commits sem conteúdo real. **Feito:** no `.gitignore` e fora
  do índice.

---

## 5. Fora de escopo (anote, não implemente)

- Qualquer tela que liste o acervo de outro usuário. A vitrine da Fase 1.5 é a
  única exceção, e ela não lista acervo de ninguém: é conteúdo estático que você
  escolheu publicar. Acervo de usuário nunca vira vitrine automaticamente, nem
  com botão de "tornar público" — esse botão é a linha entre ferramenta e serviço
  de distribuição, e quem o aperta é você, copiando arquivo para `public/`.
- Pagamento, planos, convites.
- OAuth de terceiros.
- Escalar para mais de um servidor (SQLite e disco local supõem um só).

---

## Checklist

- [x] Fase 0: imagem construída, modelo embutido, RAM e segundos por fatia medidos e anotados
- [x] SQLite em WAL, migrações numeradas, hash do token de sessão (nunca o token)
- [x] `user_path` com a segunda tranca, testada contra symlink e caminho absoluto
- [x] Id de usuário é UUID do servidor e **não aparece em nenhuma URL**
- [x] `mangatl migrate-to-accounts` idempotente, acervo do dono preservado
- [x] Vitrine é `public/` estático, gerada na máquina do dono; o servidor hospedado nunca escreve lá
- [ ] Conteúdo da vitrine é publicável, e a escolha está registrada no README
- [x] Vitrine abre sem cookie; sessão só nasce no primeiro upload
- [x] Vitrine usa o leitor e o seletor de motor de sempre, sem caminho de renderização próprio
- [x] Sessão anônima para testador; `scrypt` para o dono; 429 após 5 tentativas
- [x] CSRF por origem conferida, não só `SameSite`
- [x] `library/` e `output/` **não** são mais alcançáveis por caminho
- [x] Entrega por redirecionamento interno, ou dívida anotada e medida
- [x] **Nenhum `handle_path /_internal/*` de primeiro nível no `Caddyfile`** — a entrega mora dentro do `handle_response` (seção 3.2.1)
- [x] Proxy monta só `data/` e `public/`, somente leitura
- [x] Nenhum comentário afirma proteção que não tenha teste provando
- [x] `is_local_client` removido; papel `owner` nas rotas destrutivas
- [x] SW em v5, `/u/` e `/api/` fora do cache — conferido no navegador: depois de bater nas cinco rotas privadas, o cache segue com os 8 arquivos do `reader/` e nada mais
- [x] Cache limpo ao sair da conta (`postMessage` de purge no logout). O gatilho por troca de id de sessão do plano não foi escrito: nada de `/u/` ou `/api/` entra no cache, então não há conteúdo de sessão para limpar — o purge do logout já é folga
- [x] Testador não consegue selecionar `claude` — nem pela interface, nem pela API
- [x] Cotas aplicadas antes de gravar; 429 legível
- [x] Fila no banco; `running` volta a `pending` ao subir
- [x] Limpeza de testador vencido, e teto de disco com 507
- [x] Chave da API fora da imagem, confirmado com `docker history` — 27 camadas, nenhuma ocorrência, e `.env` não está na imagem
- [x] `scripts/serve.py` apagado e o motivo escrito no README
- [x] README com: rodar local com chave própria, custo medido, por que a instância pública é limitada, requisitos reais, e o que o projeto não é — mais `PUBLIC_SHOWCASE` e a bateria do proxy
- [x] Termos publicados, com prazo de expiração e endereço para remoção — o endereço vem de `CONTACT_EMAIL`; sem ele a página diz que ainda não foi configurado, em vez de mostrar um falso
- [x] Os 7 primeiros testes da Fase 3.7 passando contra o app
- [x] Os casos 8, 9 e 10 executados com o Caddy na frente, localmente — `caddy_test.py`, 14 testes
- [ ] **Os casos 8, 9 e 10 repetidos contra o domínio público, de outra rede**
- [x] 7.1 `PUBLIC_SHOWCASE=0` recusa escrita sem sessão na API (401, nenhum usuário criado)
- [x] 7.2 IP real vindo do Caddy, aceito só do proxy; `mangatl reset-login`
- [x] 7.3 Cota do testador vale para o zip descompactado, para bytes mágicos por entrada e para uploads em paralelo
- [x] 7.4 Folga de disco reservada para o dono
- [x] 7.5 `attempts` na fila; job que derruba o worker 3 vezes vira `failed`
- [x] 7.6 Limpeza de `.upload` órfão e de `.incoming` abandonado
- [x] 7.7 Apagar capítulo pela sessão, e série pelo dono
- [x] 7.8 Menores: health enxuto, `src;C` apagada, README corrigido, seção 4 atualizada
- [x] Backup de `data/` implementado (`mangatl backup`), e não só pedido na Fase 5
- [x] 8.1 Fala corrigida à mão no leitor, marcada `edited`, sobrevive a retradução — conferido contra o worker real: retraduzir a página e o capítulo inteiro devolvem a correção
- [x] 8.2 Retraduzir uma página (`pages` no job; relê e retraduz só ela)
- [x] 8.3 Estimativa de custo antes do job `claude` — calibrada pelo número do README, não por tokens medidos (depende do 8.4)
- [ ] 8.4 Motor `claude` contra a API real — fora desta execução, gasta a chave
- [x] 8.6 Trocar a senha do dono sem reiniciar, `mangatl set-password`, e sair de todas as sessões; o `.env` não desfaz a troca
- [x] 8.7 Exportar CBZ e PDF com a fala escrita na página
- [x] 8.8 CI no GitHub Actions. Os testes de `rtdetr` e `argos` não precisaram de marcador: cobrem funções puras e rodam sem torch nem Argos instalados
- [ ] 8.9 Inpainting em balão colorido — fora desta execução; medir com `scripts/report_overlay.py` antes
- [x] 9.1 Balões posicionados pelo CSSOM; CSP do Caddyfile conferida no navegador e por `csp.test.js`
- [x] 9.2 PDF escrito página a página; pico de 61MB medido
- [x] 9.3 Login com o mesmo custo para e-mail inexistente
- [x] 9.4 Teto de séries para o testador
- [x] 9.5 Timeout de cliente no Caddy, backup fora do disco, hook cache no `.gitignore`
