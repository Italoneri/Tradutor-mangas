# Plano de execução: contas de verdade para outras pessoas

Documento de trabalho para um agente de código. Leia **tudo** antes de escrever a
primeira linha. As fases são sequenciais e cada uma termina num **PARE**: rode a
bateria inteira, relate o que falhou e só então siga.

Pré-requisito: `PLANO-CONTAS.md` executado até a Fase 9. Este plano parte do
código como ele está depois do commit `c35f1dd`.

---

## 0. Leia isto antes de tudo

**Este plano revoga a seção 1.1 do `PLANO-CONTAS.md`.** Lá a decisão foi "o dono
faz login e o testador ganha uma sessão anônima", porque os terceiros vinham só
testar. O dono respondeu em 25/09/2026 que outras pessoas vão **usar** a
ferramenta, com acervo que fica. Isso cria um terceiro papel, e a maior parte do
trabalho não é a tela de cadastro: é desfazer o atalho de que "tem conta" e "é o
dono" eram a mesma coisa.

Hoje `is_owner` responde quatro perguntas diferentes ao mesmo tempo:

| pergunta | onde | resposta certa com membros |
|---|---|---|
| pode administrar a instância? | `panel.py`, `access="owner"` | só o dono |
| tem acervo permanente? | `sessions.hours_for`, limpeza | dono e membro |
| pode mexer na própria série (capa, glossário, apagar)? | 4 rotas `owner` | dono e membro |
| está livre de cota? | `quotas.py`, 7 lugares | só o dono |

Trocar `is_owner` por `kind in ("owner", "member")` em todo lugar dá ao membro
poder de dono. Trocar só em alguns esquece uma rota. A Fase A existe para isso e
vem antes de qualquer tela nova.

O tamanho honesto: as fases A a E são **duas a três semanas**. As fases F e G são
opcionais e ficam atrás de um portão cada.

---

## 1. Decisões que este plano assume

O dono pode trocar qualquer uma. Cada uma diz o que muda se for trocada.

### 1.1 Entrada por convite, e não cadastro aberto

O dono gera um link de convite no painel e manda para quem quiser. Quem abre o
link escolhe e-mail e senha e já entra.

Por que convite primeiro:

- **Dispensa servidor de e-mail.** Cadastro aberto exige confirmar o e-mail, senão
  qualquer um cria mil contas com endereços inventados. Confirmar e-mail exige
  SMTP, domínio configurado (SPF, DKIM) e um provedor. Convite não precisa de
  nada disso: quem recebeu o link já foi escolhido pelo dono.
- **Controla a capacidade.** Há **um worker**, e ele traduz ~1,4 s por fatia.
  Um capítulo de 155 fatias ocupa a fila por ~3,6 min. Com cadastro aberto, a
  fila de todo mundo depende de quantos desconhecidos apareceram no dia.
- **Recuperação de senha sem e-mail.** O dono gera um link de redefinição no
  painel, do mesmo jeito que gera um convite.

Cadastro aberto está na Fase F, opcional, e reaproveita tudo das fases A a E.

### 1.2 Membro usa só o motor `free`

A chave da API é do dono, e a conta chega para ele. A Fase G, opcional, deixa o
dono dar a um membro específico um teto mensal em dólar para o `claude`. Ela
depende do item 8.4 do `PLANO-CONTAS.md` (o motor `claude` contra a API real),
que ainda não foi feito: sem custo medido não há teto confiável.

### 1.3 Cota por conta, com padrão e ajuste individual

| limite | padrão | por quê |
|---|---|---|
| disco (acervo + traduções) | 2 GB | ~40 capítulos longos; 10 membros cabem num disco de 60 GB |
| páginas enviadas por dia | 600 | ~4 capítulos de 155 fatias |
| jobs pendentes ao mesmo tempo | 3 | ninguém enche a fila de uma vez |
| páginas por capítulo | 400 | o mesmo teto do dono |

Os padrões ficam em `config.toml`, seção nova `[members]`. O dono muda o limite
de uma conta no painel. Dono continua sem cota.

### 1.4 Nada é compartilhado entre contas

Continua valendo a seção 5 do `PLANO-CONTAS.md`: nenhuma tela lista acervo de
outra conta, nenhum link público de capítulo, nenhum "tornar público". Isso é o
que mantém a ferramenta como ferramenta pessoal de cada um, e não um serviço de
distribuição de capítulos. **Não abra exceção neste plano.**

### 1.5 Dados pessoais mínimos

Guarda-se e-mail, hash da senha e datas. Nada de nome, telefone ou IP em claro
(o limite por IP já usa hash). A pessoa consegue baixar o que é dela e apagar a
própria conta sozinha (Fase E).

---

## 2. Aviso sobre o conteúdo

Não é aconselhamento jurídico, e vale uma conversa com quem entende antes de
abrir para desconhecidos.

- **Direito autoral.** Membros vão subir capítulos de obras que não são deles. O
  que reduz o risco é o item 1.4: cada acervo é privado de quem subiu, e nada sai
  de uma conta para outra. Os termos dizem que quem sobe responde pelo que subiu,
  e existe o caminho de remoção a pedido (`CONTACT_EMAIL`).
- **LGPD.** E-mail é dado pessoal. Os termos precisam dizer o que é guardado, por
  quanto tempo, e como apagar. A Fase E entrega o "como apagar" de verdade.
- **Registro de acesso.** O Marco Civil (art. 15) obriga a guardar registros de
  acesso por seis meses quando o provedor é pessoa jurídica com fins econômicos.
  Uma instância pessoal e gratuita provavelmente não se enquadra; se isso mudar,
  este plano precisa de uma fase de logs.

---

## 3. Modelo

### 3.1 Papéis e capacidades

Um terceiro valor em `users.kind`: `member`.

Em vez de espalhar comparações de `kind`, `User` ganha propriedades com nome de
capacidade, e **todo o código passa a perguntar por elas**:

```python
@property
def is_admin(self) -> bool:        # administra a instância: convites, contas
    return self.kind == "owner"

@property
def has_account(self) -> bool:     # acervo permanente e painel da própria área
    return self.kind in ("owner", "member")

@property
def is_unmetered(self) -> bool:    # fora de toda cota
    return self.kind == "owner"
```

`is_owner` some. Um `grep` por `is_owner` e por `"owner"` no fim da Fase A tem
que voltar só `accounts.py` (a definição) e `db.py` (a migração).

### 3.2 Níveis de acesso das rotas

`Access` passa de `public | session | owner` para `public | session | account | admin`:

| nível | quem | rotas |
|---|---|---|
| `public` | qualquer um | health, session, login, logout, página de convite |
| `session` | testador, membro, dono | subir, traduzir, ler, apagar capítulo, área de espera |
| `account` | membro e dono | `series.json`, capa, glossário, apagar série, trocar senha, exportar dados, apagar conta |
| `admin` | só o dono | convites, lista de contas, desativar, limites, redefinição de senha |

As quatro rotas que hoje são `owner` e mexem só na área de quem pede viram
`account`. A área continua vindo do cookie, então o membro só alcança a própria.

### 3.3 Banco

**Migração 6, reconstruir `users`.** O `CHECK (kind IN ('owner', 'tester'))` da
migração 1 não aceita `member`, e o SQLite não altera `CHECK`. É preciso
reconstruir a tabela.

> **Armadilha, leia antes de escrever a migração.** `connect()` liga
> `PRAGMA foreign_keys = ON`, e `sessions`, `jobs` e `usage` apontam para `users`
> com `ON DELETE CASCADE`. Um `DROP TABLE users` com as chaves ligadas faz um
> `DELETE` implícito, e a cascata **apaga todas as sessões, todos os jobs e todo
> o uso** sem erro nenhum. Siga o procedimento de 12 passos da documentação do
> SQLite ("Making Other Kinds Of Table Schema Changes"): `foreign_keys = OFF`
> fora da transação, criar `users_new`, copiar, `DROP`, renomear, recriar os
> índices, `PRAGMA foreign_key_check`, e só então religar.
>
> Hoje `migrate()` roda cada script num `executescript` com `BEGIN` dentro, e o
> pragma não muda dentro de transação. A migração precisa de um jeito de rodar
> com as chaves desligadas: uma marca na entrada de `MIGRATIONS`, e não um
> `if version == 6`.

Colunas novas de `users`:

| coluna | tipo | para quê |
|---|---|---|
| `kind` | `CHECK (kind IN ('owner','member','tester'))` | o papel novo |
| `status` | `active` ou `disabled`, padrão `active` | desativar sem apagar |
| `last_login_at` | texto | o dono ver quem ainda usa |
| `disk_limit_bytes`, `pages_per_day`, `max_pending_jobs` | inteiro, nulo = padrão | ajuste por conta |

**Migração 7, tabelas novas.**

```sql
CREATE TABLE invites (
    token_hash  TEXT PRIMARY KEY,         -- sha256, como sessions
    created_by  TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    note        TEXT,                     -- "para o João", só o dono vê
    used_by     TEXT REFERENCES users (id) ON DELETE SET NULL,
    used_at     TEXT
);

CREATE TABLE password_resets (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE TABLE audit_log (
    at        TEXT NOT NULL,
    actor_id  TEXT,                       -- nulo quando foi a linha de comando
    action    TEXT NOT NULL,              -- invite.create, user.disable, ...
    target_id TEXT,
    detail    TEXT
);
```

Token de convite e de redefinição segue a regra das sessões: o banco guarda o
hash, e o valor só existe no link.

---

## FASE A — Capacidades no lugar de `is_owner` (sem mudar comportamento)

Refatoração pura. No fim dela ainda não existe membro nenhum, e **nenhum teste
existente muda de resultado**.

1. `User.is_admin`, `has_account`, `is_unmetered`, como na seção 3.1.
2. Cada uso de `is_owner` troca pela pergunta que ele fazia de fato. Guia:
   - `quotas.py`: `is_unmetered`;
   - `engines_for`: `is_admin` (depois a Fase G muda isso);
   - `hours_for`: `has_account`;
   - `_health`, prioridade na fila, `access`: veja a seção 3.2;
   - `admin.js`: `state.session.kind` vira campos que a API manda
     (`can_admin`, `has_account`). A interface não deduz papel de string.
3. `Access` com os quatro níveis. As quatro rotas de série viram `account`; nada
   vira `admin` ainda.
4. `find_owner` vira `find_account(email)`, que procura `owner` e `member`. O
   login passa a aceitar os dois, e o `_decoy_hash` continua valendo para e-mail
   que não existe.

**Teste:** uma matriz rota × papel em `panel_test.py`, gerada a partir de
`ROUTES`, com um testador, um membro (criado direto no banco para o teste) e o
dono. Cada combinação confere o status esperado. Uma rota nova que esquecer o
nível cai na matriz sem ninguém escrever teste para ela.

**PARE.** Bateria inteira, e o `grep` da seção 3.1.

> **Feito em 27/10/2026.** 631 testes passando. Um desvio: `POST /api/account/password`
> era `owner` e virou `admin`, e não `account`. Ela chama `change_owner_password` e o
> card de conta só aparece para o dono; abrir para o membro é a Fase E. O membro de
> teste nasce com `PRAGMA ignore_check_constraints`, porque o `CHECK` de `users` só
> aceita `member` depois da migração 6.

---

## FASE B — Convites e criação de conta

1. **Migrações 6 e 7.** Teste obrigatório: um banco com dono, sessões, jobs e
   uso criado pelas migrações 1 a 5 passa pela 6 e mantém **todas** as linhas.
   É o teste que pega a armadilha da seção 3.3.
2. **Criar convite.** `POST /api/invites` (`admin`), com `note` opcional. Devolve
   o link `https://<domínio>/reader/convite.html#<token>`. Vale 7 dias e uma vez.
   O token vai no fragmento (`#`), e não na query: fragmento não chega ao
   servidor no pedido da página nem aparece no log do Caddy.
   `GET /api/invites` lista os abertos e os usados; `DELETE` revoga.
3. **Aceitar convite.** `POST /api/invites/accept` (`public`), com token, e-mail
   e senha (mínimo 12, como a do dono). Numa transação: confere o token (existe,
   não venceu, não foi usado), cria o membro, marca o convite, abre a sessão.
   - Resposta única para token inválido, vencido ou usado, pelo mesmo motivo do
     login: a diferença diria quais tokens existem.
   - E-mail que já tem conta leva 409. Isso revela que a conta existe, mas só
     para quem tem um convite válido na mão.
   - Limite de tentativas por IP, reaproveitando `login_attempts` com a chave
     `invite-ip:`.
4. **Testador que aceita convite fica com o que já subiu.** Se o pedido chega com
   sessão de testador, a conta nova **é o mesmo usuário**: `UPDATE users SET
   kind='member', email=?, password_hash=?, expires_at=NULL`. O id não muda, então
   a área em disco não precisa de `mv`. É a Fase 6 do plano anterior, de graça.
5. **Tela.** `reader/convite.html` + `convite.js`, no estilo de `termos.html`:
   lê o token do fragmento, pede e-mail e senha duas vezes, e manda para o
   painel. Sem `style=` no HTML (`csp.test.js` confere).
6. **Linha de comando.** `mangatl invite [--note TEXTO]` imprime o link. É o
   caminho do dono antes de o painel existir no servidor.

**Testes:** convite aceito uma vez, a segunda leva a resposta única; convite
vencido idem; testador que aceita mantém capítulos e jobs; membro criado entra
pelo login normal; membro não alcança `POST /api/invites`.

**PARE.**

> **Feito em 27/10/2026.** 654 testes passando, e o fluxo rodado de ponta a ponta
> atrás do Caddy: `mangatl invite` → `convite.html` no Chromium → painel do membro
> vazio, só com `free` → o mesmo link de novo leva a resposta única.
>
> A subida real achou um furo que já existia: `app` e `worker` rodam `migrate()`
> juntos, e o segundo tentava a mesma migração e morria (`UNIQUE constraint
> failed: schema_migrations.version`; com as migrações antigas seria `duplicate
> column`). O rollback protegeu o banco, e o Docker reiniciou o app. Agora cada
> migração pega `BEGIN IMMEDIATE` e confere a versão de novo antes de aplicar.
>
> Decisões que o plano não fixava:
>
> - o link usa `SITE_ADDRESS`, o mesmo endereço que o Caddy atende. Sem domínio,
>   aponta para `http://localhost:8000`;
> - conta logada que abre um convite leva 409, porque aceitar trocaria a pessoa
>   sem ela pedir. Só a sessão de teste vira a conta nova;
> - aceitar derruba a sessão de teste de 48 h e abre uma com o prazo de conta;
> - só convite não usado é revogável. O usado fica como registro de quem entrou;
> - a auditoria de criar e revogar convite ficou para a Fase D, junto com o card.
> - Antes de migrar a base real, uma cópia foi para
>   `data/backups/mangatl-antes-da-migracao-6.db`.

---

## FASE C — Cota por conta e fila justa

1. **Limites.** `quotas.limits_for(connection, user)` devolve os números que
   valem: o da coluna da conta, ou o padrão de `[members]`. O testador continua
   com as constantes de hoje. Dono sem limite.
2. **Disco.** Mesma regra do testador: o que vale é o que está no disco
   (`area_bytes`), e não a tabela `usage`. Estourou, 429 com o número.
3. **Páginas por dia.** Aqui a tabela `usage` é a fonte certa, porque a pergunta
   é de ritmo e não de espaço. Conta por dia UTC.
4. **Jobs pendentes.** `enqueue` recusa com 429 quando a conta já tem
   `max_pending_jobs` em `pending`.
5. **Teto global de disco.** Hoje o testador para em 80% e o dono vai a 100%.
   Membro para em 95%: a folga final continua sendo do dono.
6. **Fila justa.** `claim_next` já impede dois jobs da mesma conta rodando ao
   mesmo tempo. Falta a vez: quem enfileirou dez capítulos às 9h passa na frente
   de quem enfileirou um às 9h05, dez vezes seguidas. Troque o desempate
   `created_at` por "a conta cujo último job começou há mais tempo", e só então
   `created_at`. Prioridade: dono 0, membro 1, testador 2. O valor de
   `TESTER_PRIORITY` muda de 1 para 2; os jobs antigos no banco já terminaram,
   mas confira antes de subir.
7. **Painel.** O card de upload mostra "usado X de Y" e "páginas hoje: N de 600".

**Testes:** membro no limite de disco leva 429 e o dono não; 601ª página do dia
leva 429; quarto job pendente leva 429; dois membros com 3 jobs cada rodam
alternados, e não 3 de um e depois 3 do outro.

**PARE.**

> **Feito em 27/10/2026.** 670 testes passando, e um membro convidado rodado contra
> o worker real atrás do Caddy: a 4ª página do dia (teto baixado para 3) levou 429
> com a mensagem do teto, o capítulo traduziu com prioridade 1, e o card de upload
> mostra "usado 54 B de 2.0 GB · páginas hoje: 0 de 600".
>
> O que saiu diferente do texto acima:
>
> - **O quarto job leva 409, e não 429.** É a mesma recusa de sempre do testador
>   (`Busy`), agora com o teto da conta na mensagem. Duas respostas para "fila
>   cheia" seriam uma a mais para o painel tratar.
> - **O teto conta fila e execução juntas** (`pending` + `running`), como a regra do
>   testador já contava. Na config, a chave é `active_jobs`; a coluna da conta
>   continua `max_pending_jobs`.
> - **O dono perdeu o teto de um job por vez**, porque o dono não tem cota. O que
>   ficou para todo papel, dono incluso, é recusar o *mesmo capítulo* duas vezes na
>   fila: o segundo job refaria o primeiro.
> - Membro sem teto de capítulos e de obras: quem segura é o disco. Páginas por
>   capítulo, as 400 do dono.
> - A posição na fila (`queue_position`) agora segue a mesma regra do
>   `claim_next`. Contar só quem é mais antigo mentiria com a vez alternando.
> - O uso sai em `GET /api/account/usage`, e não em `/api/session`: somar o disco
>   varre a área, e a sessão é lida a cada página do leitor.
> - Antes de subir, nenhum job pendente na base real com a prioridade antiga: os 5
>   que existem são do dono e estão `done`.

---

## FASE D — O dono administra as contas

1. **Rotas `admin`:**
   - `GET /api/users`: e-mail, papel, criado em, último login, disco usado,
     páginas hoje, jobs pendentes, status. Só testadores e membros, nunca o
     acervo de ninguém: é contabilidade, não conteúdo.
   - `PATCH /api/users/<id>`: `status` e os três limites.
   - `POST /api/users/<id>/password-reset`: gera o link de redefinição (24 h,
     uma vez) para o dono mandar por onde quiser.
   - `DELETE /api/users/<id>`: `delete_user` já existe. Exige a senha do dono no
     corpo, como a troca de senha.

   O id aparece nessas URLs, e isso é diferente das rotas `/u/`: aqui quem pede
   é o administrador, e o id identifica a conta administrada, não a área lida.
2. **Desativar.** `status='disabled'` apaga as sessões da conta, `resolve()` passa
   a recusar sessão de conta desativada, o login responde igual a senha errada,
   e os jobs pendentes dela viram `failed` com "conta desativada". O acervo fica.
3. **Redefinir senha.** `reader/redefinir.html#<token>` pede a senha nova duas
   vezes; ao gravar, derruba todas as sessões da conta.
4. **Auditoria.** Toda ação desta fase, e criar ou revogar convite, grava uma
   linha em `audit_log`. Um card "Últimas ações" no painel mostra as 50 mais
   recentes.
5. **Tela.** Card "Contas" no `admin.html`, visível só com `can_admin`:
   convites abertos com botão de copiar o link, a lista de contas, e as ações por
   linha. Apagar pede confirmação com o e-mail digitado.
6. **Linha de comando.** `mangatl users list`, `users disable EMAIL`,
   `users reset-link EMAIL` e `users delete EMAIL`. É a porta dos fundos se o
   painel do dono estiver inacessível.

**Testes:** membro desativado não entra e a sessão aberta dele para de valer no
próximo pedido; o link de redefinição vale uma vez; `GET /api/users` não devolve
nenhum nome de série ou capítulo; toda ação gera linha de auditoria.

**PARE.**

---

## FASE E — A conta é da pessoa

1. **Trocar a senha.** A rota do dono vira `account`. Mesma regra: pede a atual,
   derruba as outras sessões.
2. **Sair de todos os aparelhos.** Já existe, e já é `session`.
3. **Baixar meus dados.** `GET /api/account/export` (`account`): um `tar` da área
   da pessoa (`library/` e `output/`) mais um `conta.json` com e-mail, datas e
   limites. Transmitido em pedaços, nunca montado na memória: a lição do PDF da
   Fase 9.2 vale igual aqui. Um por vez por conta (`user_locked`).
4. **Apagar minha conta.** `DELETE /api/account` (`account`, menos o dono), com a
   senha no corpo. Chama `delete_user` e responde com o cookie limpo. Sem
   "período de arrependimento": apagar é apagar, e a tela diz isso antes.
5. **Termos.** `termos.html` ganha uma seção de privacidade: o que é guardado
   (e-mail, hash da senha, datas, uso diário), por quanto tempo (até a pessoa
   apagar), quem vê (só o dono da instância, e só a contabilidade), e onde fica
   o botão de apagar. A tela de convite tem um link para os termos e uma caixa
   de "li e concordo" que o servidor confere.
6. **Backup.** Agora o backup guarda dados de outras pessoas, e perdê-los não é
   mais só problema seu. O item 7 do README ("Backup agendado") vira
   obrigatório, e o README diz para testar a restauração uma vez antes de
   mandar o primeiro convite.

**Testes:** o export de uma conta não contém nada de outra, mesmo com série de
mesmo nome; conta apagada some do banco e do disco; o dono não consegue apagar a
própria conta pela API.

**PARE.** Depois desta fase dá para mandar convites. Atualize o README
("Hospedar para outras pessoas") com o fluxo do convite antes de mandar o
primeiro.

---

## FASE F — Opcional: cadastro aberto

**Portão:** só comece se convite não estiver bastando: gente demais pedindo link,
ou o dono querendo divulgar a instância. Releia a seção 1.1 antes.

1. **E-mail de saída.** Variáveis `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
   `SMTP_PASSWORD`, `MAIL_FROM`. Um provedor transacional, com SPF e DKIM no
   domínio, senão o e-mail cai no spam e ninguém confirma nada. `smtplib` da
   stdlib basta; o envio sai pelo worker, não pelo pedido HTTP.
2. **Cadastro.** `POST /api/signup` cria a conta com `status='pending'` e manda o
   link de confirmação (24 h). Conta pendente entra, mas não sobe nada.
3. **Recuperar senha por e-mail.** O mesmo `password_resets` da Fase D, com o
   link indo por e-mail em vez de pelo dono. A resposta de "esqueci a senha" é
   sempre "se o e-mail existir, mandamos o link".
4. **Contas em massa.** Cadastros por IP por hora (como `tester_signups`), teto
   global de cadastros por dia configurável, e o dono recebendo um aviso quando o
   teto for atingido.
5. **Interruptor.** `OPEN_SIGNUP=0` por padrão. Desligado, a rota responde 404
   e a tela nem mostra o botão, como a vitrine com `PUBLIC_SHOWCASE=0`.

---

## FASE G — Opcional: `claude` para membros, com teto em dólar

**Portão:** o item 8.4 do `PLANO-CONTAS.md` feito, com custo por página medido
contra a API real e anotado no README.

1. Coluna `claude_usd_month` por conta, nula por padrão (sem acesso).
2. `engines_for` inclui `claude` quando a conta tem teto.
3. **Antes de enfileirar:** a estimativa do job (`_estimate` já existe) somada ao
   gasto do mês não pode passar do teto. A estimativa fica **reservada** no job.
4. **Depois de rodar:** o custo real, que `_log_usage` já calcula, é gravado no
   job, e a reserva é trocada por ele. O gasto do mês é a soma do real mais as
   reservas dos pendentes.
5. O painel do membro mostra "US$ X de Y neste mês"; o do dono mostra o total
   por conta.

---

## 4. Fora de escopo (anote, não implemente)

- Compartilhar capítulo entre contas, link público, perfil público, "seguir".
  É a linha da seção 1.4.
- Pagamento e planos.
- Login com Google e afins: arrasta dependência e dado pessoal de terceiro para
  resolver algo que o convite já resolve.
- Mais de um administrador.
- Mais de um worker em máquinas diferentes. Dois workers na mesma máquina já
  funcionam: `claim_next` roda em `BEGIN IMMEDIATE`. Mas cada um pede ~600 MB no
  pico; meça antes de subir o segundo.

---

## Checklist

- [x] A: `is_admin`, `has_account`, `is_unmetered`; `grep is_owner` limpo
- [x] A: quatro níveis de acesso; matriz rota × papel gerada de `ROUTES` — o esperado vem de uma tabela escrita à mão (`ROUTE_ACCESS`), não de `route.access`: derivado, o teste concordava com qualquer erro (conferido rebaixando o glossário para `session`)
- [x] A: login aceita dono e membro, com o hash de fachada para e-mail inexistente
- [x] B: migração 6 reconstrói `users` sem perder sessão, job nem uso — testado, e o teste falha se o `foreign_keys = OFF` sair (conferido)
- [x] B: convite de uso único, 7 dias, token só como hash, link com `#`
- [x] B: testador que aceita convite vira membro com o mesmo id e o mesmo acervo
- [x] B: `mangatl invite`
- [x] C: limites por conta com padrão em `[members]`
- [x] C: disco pelo disco, páginas por dia pela `usage`, pendentes pela fila
- [x] C: fila alterna entre contas; prioridade dono 0, membro 1, testador 2 — conferido: sem o desempate novo, o teste de alternância cai
- [ ] D: lista de contas sem nenhum nome de série; desativar; redefinir por link
- [ ] D: auditoria de toda ação de administração
- [ ] D: `mangatl users list | disable | reset-link | delete`
- [ ] E: trocar senha, exportar dados em fluxo, apagar a própria conta
- [ ] E: termos com privacidade e aceite conferido no servidor
- [ ] E: restauração de backup testada uma vez; README com o fluxo do convite
- [ ] F (opcional): cadastro aberto atrás de `OPEN_SIGNUP`, com confirmação por e-mail
- [ ] G (opcional): `claude` por conta com teto mensal, reserva e custo real
