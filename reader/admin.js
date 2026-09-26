/* Painel: criar obra, subir capitulo e mandar traduzir, sem terminal.

   Mesmo padrao do app.js - modulo ES nativo, sem framework e sem build - e a
   mesma folha de estilo. O que muda e que aqui tudo escreve: cada acao e uma
   chamada a `/api/`, que confere a sessao antes de tocar no disco.

   Duas telas no mesmo documento: a de entrar, para quem nao tem sessao de dono, e
   o painel. Quem sobe uma amostra sem conta nenhuma nao ve a de entrar - a sessao
   anonima nasce sozinha no primeiro upload.

   Apagar capitulo e obra pede dois cliques no mesmo botao, e nao um `confirm()`:
   o dialogo do navegador bloqueia a pagina inteira, e o segundo clique no mesmo
   lugar ja e a confirmacao. Obra inteira so aparece para o dono.

   O que esta tela deliberadamente nao faz: renomear e reordenar pagina. Os dois
   reescrevem caminhos gravados nos JSONs, e nenhum fica melhor escondido atras de
   um botao pequeno.
*/

const API = "/api";

/** A ordem de leitura vem do nome do arquivo, e tem de bater com a do back.
 *
 * `numeric: true` da o mesmo resultado do `_natural_key` do `store.py` para nome
 * de pagina: 2 antes de 10. Se as duas divergirem, a tela mostra uma ordem e o
 * capitulo sai em outra - que e pior do que nao mostrar ordem nenhuma.
 */
const byName = new Intl.Collator("pt-BR", { numeric: true, sensitivity: "base" });

const ARCHIVE_SUFFIXES = [".zip", ".cbz"];

const el = {
  flash: document.getElementById("flash"),
  health: document.getElementById("health"),

  login: document.getElementById("login"),
  loginForm: document.getElementById("login-form"),
  loginEmail: document.getElementById("login-email"),
  loginPassword: document.getElementById("login-password"),
  enterTester: document.getElementById("enter-tester"),
  loginTitle: document.getElementById("login-title"),
  testerPath: document.getElementById("tester-path"),
  signOut: document.getElementById("sign-out"),
  panel: document.getElementById("panel"),
  accountCard: document.getElementById("account-card"),
  passwordForm: document.getElementById("password-form"),
  passwordCurrent: document.getElementById("password-current"),
  passwordNew: document.getElementById("password-new"),
  signOutEverywhere: document.getElementById("sign-out-everywhere"),

  seriesList: document.getElementById("series-list"),
  toggleNewSeries: document.getElementById("toggle-new-series"),
  newSeries: document.getElementById("new-series"),
  newSlug: document.getElementById("new-slug"),
  newTitle: document.getElementById("new-title"),

  seriesCard: document.getElementById("series-card"),
  seriesName: document.getElementById("series-name"),
  seriesSlug: document.getElementById("series-slug"),
  seriesMeta: document.getElementById("series-meta"),
  metaTitle: document.getElementById("meta-title"),
  metaStatus: document.getElementById("meta-status"),
  coverInput: document.getElementById("cover-input"),
  chapterList: document.getElementById("chapter-list"),
  glossaryRows: document.getElementById("glossary-rows"),
  glossaryAdd: document.getElementById("glossary-add"),
  glossarySave: document.getElementById("glossary-save"),
  seriesDanger: document.getElementById("series-danger"),
  seriesDelete: document.getElementById("series-delete"),

  uploadCard: document.getElementById("upload-card"),
  chapterNumber: document.getElementById("chapter-number"),
  drop: document.getElementById("drop"),
  fileInput: document.getElementById("file-input"),
  orderNote: document.getElementById("order-note"),
  fileList: document.getElementById("file-list"),
  uploadActions: document.getElementById("upload-actions"),
  uploadStart: document.getElementById("upload-start"),
  uploadDiscard: document.getElementById("upload-discard"),
  uploadStatus: document.getElementById("upload-status"),
  uploadTrack: document.getElementById("upload-track"),
  uploadFill: document.getElementById("upload-fill"),

  jobCard: document.getElementById("job-card"),
  jobTarget: document.getElementById("job-target"),
  jobEngine: document.getElementById("job-engine"),
  jobForce: document.getElementById("job-force"),
  jobDry: document.getElementById("job-dry"),
  jobStart: document.getElementById("job-start"),
  engineNote: document.getElementById("engine-note"),
  jobView: document.getElementById("job-view"),
  jobState: document.getElementById("job-state"),
  jobPhase: document.getElementById("job-phase"),
  jobFill: document.getElementById("job-fill"),
  jobLog: document.getElementById("job-log"),
  jobRead: document.getElementById("job-read"),
};

const state = {
  health: null,
  /** Quem esta usando o painel. `{authenticated, kind, engines}`, como o back manda. */
  session: { authenticated: false, kind: null, engines: [] },
  series: [],
  selected: null,
  /** Arquivos escolhidos e ainda nao enviados, com o resultado de cada um. */
  staged: [],
  chapter: null,
  polling: null,
};

/* ---------- conversa com a API ---------- */

/** Chamada ao painel, com a mensagem do servidor preservada.
 *
 * O back devolve `{"error": "..."}` em toda recusa justamente para a tela poder
 * mostrar o motivo; engolir isso e trocar "capitulo 001 ja existe" por "falhou".
 */
async function api(path, { method = "GET", body = null, raw = false } = {}) {
  /* `X-Requested-With` em toda chamada, e nao so nas que escrevem: o servidor
     exige o cabecalho nas rotas de escrita, e um formulario HTML de outro site
     nao consegue mandar cabecalho nenhum. Junto com a conferencia de origem no
     back, e o que fecha o CSRF que `SameSite=Lax` sozinho deixa passar. */
  const headers = { "X-Requested-With": "fetch" };
  const options = { method, cache: "no-store", credentials: "same-origin", headers };
  if (body !== null) {
    options.body = raw ? body : JSON.stringify(body);
    if (!raw) headers["Content-Type"] = "application/json";
  }

  const response = await fetch(`${API}${path}`, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${response.status} em ${path}`);
  return payload;
}

function seriesPath(slug, suffix = "") {
  return `/series/${encodeURIComponent(slug)}${suffix}`;
}

function flash(message, kind = "erro") {
  el.flash.textContent = message;
  el.flash.className = `flash flash-${kind}`;
  el.flash.hidden = false;
}

function clearFlash() {
  el.flash.hidden = true;
}

/** Roda a acao e transforma falha em recado, em vez de erro silencioso no console. */
async function guard(action) {
  try {
    clearFlash();
    await action();
  } catch (error) {
    flash(error.message);
  }
}

/* ---------- obras ---------- */

function coverHtml(entry) {
  if (!entry.cover) {
    const initial = (entry.title || entry.series).trim().charAt(0) || "?";
    return `<div class="cover-empty" aria-hidden="true">${escapeHtml(initial)}</div>`;
  }
  const url = entry.cover.split("/").map(encodeURIComponent).join("/");
  return `<img src="../${url}" alt="" loading="lazy" decoding="async">`;
}

function renderSeries() {
  if (!state.series.length) {
    el.seriesList.innerHTML = `<p class="empty">Nenhuma obra ainda. Crie a primeira acima.</p>`;
    return;
  }

  el.seriesList.innerHTML = state.series
    .map((entry) => {
      const pending = entry.chapters.filter((chapter) => !chapter.engines.length).length;
      const marks = [
        `${entry.chapters.length} ${entry.chapters.length === 1 ? "capítulo" : "capítulos"}`,
        pending ? `${pending} sem tradução` : "",
      ].filter(Boolean);

      return `<button class="work${entry.series === state.selected ? " chosen" : ""}" data-slug="${escapeHtml(entry.series)}">
        <div class="cover">${coverHtml(entry)}</div>
        <div>
          <h2>${escapeHtml(entry.title || entry.series)}</h2>
          <p>${marks.join(" · ")}</p>
        </div>
      </button>`;
    })
    .join("");
}

function selected() {
  return state.series.find((entry) => entry.series === state.selected) || null;
}

function renderChapters() {
  const entry = selected();
  if (!entry) return;

  if (!entry.chapters.length) {
    el.chapterList.innerHTML = `<p class="empty">Nenhum capítulo. Suba o primeiro abaixo.</p>`;
    return;
  }

  el.chapterList.innerHTML = entry.chapters
    .map((chapter) => {
      const tags = chapter.engines.map((name) => `<span class="tag">${escapeHtml(name)}</span>`).join("");
      const pending = chapter.incoming
        ? `<span class="tag tag-warn">upload aberto</span>`
        : chapter.engines.length
          ? ""
          : `<span class="tag tag-warn">sem tradução</span>`;

      return `<div class="chapter">
        <strong>${escapeHtml(chapter.chapter)}</strong>
        <div class="tags">${tags}${pending}</div>
        <span class="pages">${chapter.image_count} ${chapter.image_count === 1 ? "imagem" : "imagens"}</span>
        <button class="btn btn-secondary" data-translate="${escapeHtml(chapter.chapter)}">Traduzir</button>
        <button class="btn btn-ghost" data-delete="${escapeHtml(chapter.chapter)}">Apagar</button>
      </div>`;
    })
    .join("");
}

const ARM_MS = 4000;

/** Primeiro clique arma o botao, o segundo executa. Devolve se ja estava armado.
 *
 * Sem `confirm()`: o dialogo do navegador trava a pagina inteira, e o segundo
 * clique no mesmo botao, com o texto trocado, ja pergunta "tem certeza?".
 */
function armed(button, question) {
  if (button.dataset.armed) return true;
  const label = button.textContent;
  button.dataset.armed = "1";
  button.textContent = question;
  setTimeout(() => {
    delete button.dataset.armed;
    button.textContent = label;
  }, ARM_MS);
  return false;
}

async function deleteChapter(button) {
  if (!armed(button, "Apagar mesmo?")) return;
  const chapter = button.dataset.delete;
  await api(`${seriesPath(state.selected)}/chapters/${encodeURIComponent(chapter)}`, {
    method: "DELETE",
  });
  await loadSeries();
  flash(`Capítulo ${chapter} apagado.`, "ok");
}

async function deleteSeries(button) {
  if (!armed(button, "Apagar a obra e todos os capítulos?")) return;
  const slug = state.selected;
  await api(seriesPath(slug), { method: "DELETE" });
  state.selected = null;
  el.seriesCard.hidden = true;
  el.uploadCard.hidden = true;
  el.jobCard.hidden = true;
  await loadSeries(false);
  flash(`Obra ${slug} apagada.`, "ok");
}

function glossaryRowHtml(term = "", translation = "") {
  return `<tr>
    <td><input class="term" value="${escapeHtml(term)}" autocomplete="off"></td>
    <td><input class="translation" value="${escapeHtml(translation)}" autocomplete="off"></td>
    <td><button class="btn btn-ghost" data-drop-row>remover</button></td>
  </tr>`;
}

function readGlossary() {
  const terms = {};
  for (const row of el.glossaryRows.querySelectorAll("tr")) {
    const term = row.querySelector(".term").value.trim();
    if (term) terms[term] = row.querySelector(".translation").value;
  }
  return terms;
}

async function selectSeries(slug) {
  state.selected = slug;
  state.staged = [];
  state.chapter = null;
  renderSeries();
  renderChapters();

  const entry = selected();
  el.seriesName.textContent = entry.title || entry.series;
  el.seriesSlug.textContent = `pasta: ${entry.series}`;
  el.seriesCard.hidden = false;
  el.seriesDanger.hidden = state.session.kind !== "owner";
  el.uploadCard.hidden = false;
  el.jobCard.hidden = true;
  renderStaged();

  const [meta, glossary] = await Promise.all([
    api(seriesPath(slug, "/series.json")),
    api(seriesPath(slug, "/glossary")),
  ]);
  el.metaTitle.value = meta.title || "";
  el.metaStatus.value = meta.status || "";

  const rows = Object.entries(glossary);
  el.glossaryRows.innerHTML = (rows.length ? rows : [["", ""]])
    .map(([term, translation]) => glossaryRowHtml(term, translation))
    .join("");
}

async function loadSeries(keepSelection = true) {
  const payload = await api("/series");
  state.series = payload.series;
  renderSeries();
  if (keepSelection && state.selected && selected()) {
    renderChapters();
  } else if (!selected()) {
    state.selected = null;
    el.seriesCard.hidden = true;
    el.uploadCard.hidden = true;
  }
}

/* ---------- capitulo novo ---------- */

function isArchive(file) {
  return ARCHIVE_SUFFIXES.some((suffix) => file.name.toLowerCase().endsWith(suffix));
}

function humanBytes(total) {
  if (total < 1024) return `${total} B`;
  if (total < 1024 * 1024) return `${Math.round(total / 1024)} KB`;
  return `${(total / (1024 * 1024)).toFixed(1)} MB`;
}

/* Uma URL por arquivo, criada na primeira vez. `renderStaged` roda a cada pagina
   enviada, e criar uma URL nova em cada chamada deixava centenas delas vivas num
   capitulo longo. A CSP do Caddyfile libera `blob:` em `img-src` por causa disto. */
function previewUrl(item) {
  item.preview ??= URL.createObjectURL(item.file);
  return item.preview;
}

function stageFiles(files) {
  for (const item of state.staged) if (item.preview) URL.revokeObjectURL(item.preview);
  // Ordenado aqui e nao na chegada: o navegador nao garante a ordem em que
  // entrega os arquivos arrastados, e a ordem que vale e a do nome.
  state.staged = [...files]
    .sort((a, b) => byName.compare(a.name, b.name))
    .map((file) => ({ file, state: "pendente", error: null }));
  renderStaged();
}

function renderStaged() {
  const items = state.staged;
  el.orderNote.hidden = !items.length;
  el.uploadActions.hidden = !items.length;
  el.fileList.innerHTML = items
    .map((item) => {
      const thumb = item.file.type.startsWith("image/")
        ? `<img src="${previewUrl(item)}" alt="" loading="lazy">`
        : `<span class="cover-empty" aria-hidden="true">zip</span>`;
      const note = item.error ? `<span class="tag tag-warn">${escapeHtml(item.error)}</span>` : "";

      return `<li class="file file-${item.state}">
        <span class="thumb">${thumb}</span>
        <span class="name">${escapeHtml(item.file.name)}</span>
        <span class="pages">${humanBytes(item.file.size)}</span>
        ${note}
      </li>`;
    })
    .join("");

  const done = items.filter((item) => item.state === "enviado").length;
  el.uploadStatus.textContent = items.length ? `${done} de ${items.length}` : "";
}

function setBar(fill, track, done, total) {
  track.hidden = !total;
  fill.style.width = total ? `${Math.round((done / total) * 100)}%` : "0";
}

async function sendOne(slug, chapter, item) {
  const path = `${seriesPath(slug)}/chapters/${encodeURIComponent(chapter)}`;
  if (isArchive(item.file)) {
    await api(`${path}/archive`, { method: "POST", body: item.file, raw: true });
  } else {
    await api(`${path}/files/${encodeURIComponent(item.file.name)}`, {
      method: "PUT",
      body: item.file,
      raw: true,
    });
  }
}

async function uploadStaged() {
  const slug = state.selected;
  const asked = el.chapterNumber.value.trim();

  // Corpo vazio pede a sugestao ao servidor, que e quem sabe quais capitulos
  // existem. O nome escolhido volta na resposta.
  const created = await api(`${seriesPath(slug)}/chapters`, {
    method: "POST",
    body: asked ? { chapter: asked } : {},
  });
  state.chapter = created.chapter;
  el.chapterNumber.value = created.chapter;

  const pending = state.staged.filter((item) => item.state !== "enviado");
  let done = state.staged.length - pending.length;
  setBar(el.uploadFill, el.uploadTrack, done, state.staged.length);

  for (const item of pending) {
    try {
      await sendOne(slug, state.chapter, item);
      item.state = "enviado";
      item.error = null;
    } catch (error) {
      // Para no primeiro erro em vez de seguir: a area de espera sobrevive, e
      // continuar mandando por cima de uma falha esconde qual arquivo quebrou.
      item.state = "falhou";
      item.error = error.message;
      renderStaged();
      throw new Error(`${item.file.name}: ${error.message} — corrija e clique em enviar de novo`);
    }
    done += 1;
    setBar(el.uploadFill, el.uploadTrack, done, state.staged.length);
    renderStaged();
  }

  const committed = await api(`${seriesPath(slug)}/chapters/${encodeURIComponent(state.chapter)}/commit`, {
    method: "POST",
  });
  state.staged = [];
  renderStaged();
  setBar(el.uploadFill, el.uploadTrack, 0, 0);
  flash(`Capítulo ${committed.chapter} pronto com ${committed.files.length} páginas.`, "ok");

  await loadSeries();
  openJob(committed.chapter);
}

/* ---------- traduzir ---------- */

function openJob(chapter) {
  state.chapter = chapter;
  disarmCost();
  el.jobCard.hidden = false;
  el.jobTarget.textContent = `${state.selected} · capítulo ${chapter}`;
  el.jobView.hidden = true;
  el.jobRead.hidden = true;
  el.jobCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* Estados que ainda vao mudar sozinhos. A fila e compartilhada agora: um capítulo
   pode ficar minutos em `pending` esperando o que está na frente terminar. */
const LIVE_STATES = ["pending", "running"];

function queueLabel(job) {
  if (job.state !== "pending") return job.state;
  /* "na fila" sem número é indistinguível de travado. */
  const ahead = job.queue_position;
  if (!ahead) return "na fila, é o próximo";
  return `na fila, ${ahead} na frente`;
}

function renderJob(job) {
  el.jobView.hidden = false;
  el.jobState.textContent = queueLabel(job);
  el.jobPhase.textContent = job.progress.total
    ? `${job.progress.phase} ${job.progress.done}/${job.progress.total} · ${job.progress.detail}`
    : `${job.progress.phase} · ${job.progress.detail}`;
  setBar(el.jobFill, el.jobFill.parentElement, job.progress.done, job.progress.total || 1);

  el.jobLog.textContent = job.log.join("\n");
  el.jobLog.scrollTop = el.jobLog.scrollHeight;

  el.jobStart.disabled = LIVE_STATES.includes(job.state);
  if (job.state === "failed") flash(job.error || "o processamento falhou");
  if (job.state === "done") {
    const href = `index.html?series=${encodeURIComponent(job.series)}&chapter=${encodeURIComponent(job.chapter)}`;
    el.jobRead.href = href;
    el.jobRead.hidden = false;
  }
}

/** Uma consulta por segundo. Sem SSE nem WebSocket: a pagina fica aberta no mesmo
 *  PC que serve, o custo do polling e nulo e o codigo extra nao e. */
function pollJob(id) {
  clearInterval(state.polling);
  state.polling = setInterval(async () => {
    const job = await api(`/jobs/${id}`).catch(() => null);
    if (!job) return;
    renderJob(job);
    if (!LIVE_STATES.includes(job.state)) {
      clearInterval(state.polling);
      state.polling = null;
      loadSeries().catch(() => {});
    }
  }, 1000);
}

/** Reencontra o que ficou na fila enquanto a aba estava fechada.
 *
 * O job vive no banco e quem o roda e outro processo: fechar a aba nao para nada,
 * e reabrir sem procurar deixaria a tela fingindo que nao ha nada acontecendo -
 * com o botao de traduzir liberado para disparar um segundo que so tomaria 409.
 */
async function restoreJob() {
  const payload = await api("/jobs").catch(() => null);
  const job = payload && payload.jobs[0];
  if (!job) return;

  if (job.series !== state.selected && state.series.some((entry) => entry.series === job.series)) {
    await selectSeries(job.series);
  }
  openJob(job.chapter);
  renderJob(job);
  if (LIVE_STATES.includes(job.state)) pollJob(job.id);
}

/** O custo estimado de um job `claude`, pedido antes do clique que enfileira.
 *
 * O primeiro clique em "Traduzir" com `claude` so mostra o numero e troca o
 * texto do botao; o segundo confirma. Trocar de motor ou de capitulo desarma.
 */
async function confirmCost() {
  if (el.jobEngine.value !== "claude") return true;
  const key = `${state.selected}/${state.chapter}`;
  if (el.jobStart.dataset.confirmed === key) return true;

  const path = `${seriesPath(state.selected)}/chapters/${encodeURIComponent(state.chapter)}/estimate/claude`;
  const estimate = await api(path);
  const cost = estimate.usd === null ? "custo desconhecido (modelo sem preço no config.toml)" : `~US$ ${estimate.usd.toFixed(2)}`;
  el.engineNote.hidden = false;
  el.engineNote.textContent = `Estimativa: ${estimate.pages} páginas com ${estimate.model}, ${cost}. É estimativa: captura alta vira mais páginas ao fatiar.`;
  el.jobStart.dataset.confirmed = key;
  el.jobStart.textContent = `Confirmar (${cost})`;
  return false;
}

function disarmCost() {
  delete el.jobStart.dataset.confirmed;
  el.jobStart.textContent = "Traduzir";
}

async function startJob() {
  if (!(await confirmCost())) return;
  disarmCost();
  const job = await api("/jobs", {
    method: "POST",
    body: {
      series: state.selected,
      chapter: state.chapter,
      engine: el.jobEngine.value,
      force: el.jobForce.checked,
      dry_run: el.jobDry.checked,
    },
  });
  renderJob(job.job);
  pollJob(job.job_id);
}

/* ---------- ligacao ---------- */

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );
}

function wire() {
  el.toggleNewSeries.onclick = () => {
    el.newSeries.hidden = !el.newSeries.hidden;
    if (!el.newSeries.hidden) el.newSlug.focus();
  };

  el.newSeries.onsubmit = (event) => {
    event.preventDefault();
    guard(async () => {
      const created = await api("/series", {
        method: "POST",
        body: { slug: el.newSlug.value.trim(), title: el.newTitle.value.trim() },
      });
      el.newSlug.value = el.newTitle.value = "";
      el.newSeries.hidden = true;
      await loadSeries(false);
      await selectSeries(created.slug);
    });
  };

  el.seriesList.onclick = (event) => {
    const card = event.target.closest("[data-slug]");
    if (card) guard(() => selectSeries(card.dataset.slug));
  };

  el.chapterList.onclick = (event) => {
    const button = event.target.closest("[data-translate]");
    if (button) openJob(button.dataset.translate);
    const eraser = event.target.closest("[data-delete]");
    if (eraser) guard(() => deleteChapter(eraser));
  };

  el.seriesDelete.onclick = (event) => {
    event.preventDefault();
    guard(() => deleteSeries(el.seriesDelete));
  };

  el.seriesMeta.onsubmit = (event) => {
    event.preventDefault();
    guard(async () => {
      await api(seriesPath(state.selected, "/series.json"), {
        method: "PUT",
        body: { title: el.metaTitle.value.trim(), status: el.metaStatus.value.trim() },
      });
      await loadSeries();
      flash("Título salvo.", "ok");
    });
  };

  el.coverInput.onchange = () => {
    const file = el.coverInput.files[0];
    if (!file) return;
    guard(async () => {
      await api(seriesPath(state.selected, "/cover"), { method: "PUT", body: file, raw: true });
      el.coverInput.value = "";
      await loadSeries();
      flash("Capa trocada.", "ok");
    });
  };

  el.glossaryAdd.onclick = (event) => {
    event.preventDefault();
    el.glossaryRows.insertAdjacentHTML("beforeend", glossaryRowHtml());
  };

  el.glossaryRows.onclick = (event) => {
    const button = event.target.closest("[data-drop-row]");
    if (!button) return;
    event.preventDefault();
    button.closest("tr").remove();
  };

  el.glossarySave.onclick = (event) => {
    event.preventDefault();
    guard(async () => {
      const terms = await api(seriesPath(state.selected, "/glossary"), {
        method: "PUT",
        body: readGlossary(),
      });
      flash(`Glossário salvo com ${Object.keys(terms).length} termos.`, "ok");
    });
  };

  el.fileInput.onchange = () => stageFiles(el.fileInput.files);

  for (const name of ["dragenter", "dragover"]) {
    el.drop.addEventListener(name, (event) => {
      event.preventDefault();
      el.drop.classList.add("over");
    });
  }
  for (const name of ["dragleave", "drop"]) {
    el.drop.addEventListener(name, () => el.drop.classList.remove("over"));
  }
  el.drop.addEventListener("drop", (event) => {
    event.preventDefault();
    stageFiles(event.dataTransfer.files);
  });

  el.uploadStart.onclick = (event) => {
    event.preventDefault();
    el.uploadStart.disabled = true;
    guard(uploadStaged).finally(() => {
      el.uploadStart.disabled = false;
    });
  };

  el.uploadDiscard.onclick = (event) => {
    event.preventDefault();
    guard(async () => {
      if (state.chapter) {
        await api(
          `${seriesPath(state.selected)}/chapters/${encodeURIComponent(state.chapter)}/incoming`,
          { method: "DELETE" },
        ).catch(() => {});
      }
      state.staged = [];
      state.chapter = null;
      renderStaged();
      setBar(el.uploadFill, el.uploadTrack, 0, 0);
      await loadSeries();
    });
  };

  el.jobStart.onclick = (event) => {
    event.preventDefault();
    guard(startJob);
  };
  el.jobEngine.onchange = disarmCost;

  el.loginForm.onsubmit = signIn;
  el.passwordForm.onsubmit = changePassword;
  el.signOutEverywhere.onclick = () => guard(signOutEverywhere);
  el.signOut.onclick = () => guard(signOut);

  /* Entrar como testador nao chama rota nenhuma: a sessao nasce no servidor na
     primeira escrita, e criar uma aqui daria area em disco para quem so clicou. */
  el.enterTester.onclick = () => {
    state.session = { authenticated: false, kind: "tester", engines: ["free"] };
    guard(enterPanel);
  };
}

/* Quais motores esta conta pode escolher.
 *
 * A lista vem do servidor, e nao do front: para o testador ela nunca traz
 * `claude`, porque a chave da API e do dono e a conta chegaria para ele. Esconder
 * no front e cortesia; quem recusa de verdade e a rota - as duas coisas juntas.
 */
function renderEngines() {
  const engines = state.session.engines || [];
  el.jobEngine.innerHTML = engines
    .map((name) => {
      const blocked = name === "claude" && !state.health.has_api_key;
      return `<option value="${escapeHtml(name)}"${blocked ? " disabled" : ""}>${escapeHtml(name)}</option>`;
    })
    .join("");

  const owner = state.session.kind === "owner";
  if (owner && !state.health.has_api_key) {
    el.engineNote.hidden = false;
    el.engineNote.textContent =
      "O motor claude está desligado porque não há ANTHROPIC_API_KEY no ambiente do servidor. Copie .env.example para .env, preencha a chave e reinicie.";
  } else if (!owner) {
    el.engineNote.hidden = false;
    el.engineNote.textContent =
      "Esta é uma sessão de teste: só o motor free, até 12 páginas por capítulo, e o que você subir expira em 48 horas.";
  }
}

async function signIn(event) {
  event.preventDefault();
  clearFlash();
  try {
    state.session = await api("/login", {
      method: "POST",
      body: { email: el.loginEmail.value.trim(), password: el.loginPassword.value },
    });
  } catch (error) {
    flash(error.message);
    return;
  }
  el.loginPassword.value = "";
  await enterPanel();
}

async function changePassword(event) {
  event.preventDefault();
  await guard(async () => {
    const result = await api("/account/password", {
      method: "POST",
      body: { current: el.passwordCurrent.value, new: el.passwordNew.value },
    });
    el.passwordCurrent.value = el.passwordNew.value = "";
    const closed = result.other_sessions_closed;
    flash(`Senha trocada. ${closed} ${closed === 1 ? "sessão encerrada" : "sessões encerradas"} em outros aparelhos.`, "ok");
  });
}

async function signOutEverywhere() {
  if (!armed(el.signOutEverywhere, "Sair de todos, inclusive daqui?")) return;
  await api("/account/sessions/revoke", { method: "POST" });
  navigator.serviceWorker?.controller?.postMessage({ type: "purge" });
  location.reload();
}

async function signOut() {
  await api("/logout", { method: "POST" }).catch(() => null);
  /* O cache e por origem, nao por conta: sem esta limpeza a proxima pessoa a
     usar este navegador poderia receber uma pagina guardada na sessao anterior. */
  navigator.serviceWorker?.controller?.postMessage({ type: "purge" });
  location.reload();
}

async function enterPanel() {
  /* De novo, e agora com sessao: sem ela o `/health` so diz que o servidor esta
     de pe, e o que falta aqui - detector, chave da API - e do dono. */
  state.health = (await api("/health").catch(() => null)) || state.health;
  el.health.textContent = state.health.detector ? `detector ${state.health.detector}` : "";
  el.login.hidden = true;
  el.panel.hidden = false;
  el.accountCard.hidden = state.session.kind !== "owner";
  el.signOut.hidden = false;
  renderEngines();
  await guard(() => loadSeries(false));
  await guard(restoreJob);
}

async function main() {
  wire();

  state.health = await api("/health").catch(() => null);
  if (!state.health) {
    el.health.textContent = "servidor fora do ar";
    flash("Não consegui falar com o servidor. Suba o `mangatl serve` e recarregue.");
    return;
  }
  state.session = await api("/session").catch(() => ({ authenticated: false }));
  if (state.session.authenticated) {
    await enterPanel();
    return;
  }

  /* Sem sessao a tela fica na entrada. Quem lidera depende da instancia: numa
     privada o dono e o unico usuario e o formulario dele e a tela inteira; numa
     vitrine o caminho anonimo aparece embaixo, porque o visitante nao quer criar
     conta para experimentar. */
  const showcase = state.session.showcase === true;
  el.testerPath.hidden = !showcase;
  el.loginTitle.textContent = showcase ? "Entrar como dono" : "Entrar";
  el.login.hidden = false;
  el.panel.hidden = true;
  if (!showcase) el.loginEmail.focus();
}

main();
