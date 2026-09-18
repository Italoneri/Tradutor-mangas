/* Leitor estatico. Le os JSONs gerados pelo pipeline; nao chama API nenhuma.

   Tres telas, todas no mesmo documento, escolhidas pela query:

       index.html                        estante de obras
       index.html?series=X               capitulos da obra
       index.html?series=X&chapter=Y     leitura

   Navegar recarrega a pagina de proposito: o estado por tela e pequeno, e um
   router de verdade custaria mais do que resolve.
*/

import { fitFontSize, fontCqw, overlayBox } from "./overlay.js";

const ROOT = "..";
const params = new URLSearchParams(location.search);

/* Onde a vitrine publica mora. Caminho e nao rota: ela e conteudo estatico e
   imutavel, servida sem sessao e sem cookie, exatamente como esta PWA. */
const SHOWCASE_INDEX = `${ROOT}/public/demo/demo-library.json`;

/* O indice do usuario logado. Nao leva id de usuario: quem esta pedindo sai da
   sessao, e um id na URL seria convite para trocar o numero. */
const MY_LIBRARY = `${ROOT}/u/library`;

const REPOSITORY = "https://github.com/Italoneri/Tradutor-mangas";

const el = {
  main: document.getElementById("main"),
  brand: document.getElementById("brand"),
  back: document.getElementById("back"),
  backLabel: document.getElementById("back-label"),
  titleBox: document.getElementById("title-box"),
  title: document.getElementById("title"),
  subtitle: document.getElementById("subtitle"),
  search: document.getElementById("search"),
  enginePicker: document.getElementById("engine-picker"),
  engine: document.getElementById("engine"),
  toggleOverlay: document.getElementById("toggle-overlay"),
  nextChapter: document.getElementById("next-chapter"),
  progressLabel: document.getElementById("progress-label"),
  progressTrack: document.getElementById("progress-track"),
  progressFill: document.getElementById("progress-fill"),
  pager: document.getElementById("pager"),
  prev: document.getElementById("prev"),
  next: document.getElementById("next"),
  progress: document.getElementById("progress"),
};

/* ---------- estado leve em localStorage, sempre tolerante a falha ---------- */

const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* modo privado ou storage bloqueado: a leitura funciona sem memoria */
    }
  },
};

/* ---------- carregamento ---------- */

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) throw new Error(`${response.status} em ${url}`);
  return response.json();
}

/* O library.json guarda caminhos crus; a codificacao por segmento e aqui porque
   nome de obra com espaco ou acento e comum e quebraria a URL. */
function assetUrl(path) {
  return `${ROOT}/${path.split("/").map(encodeURIComponent).join("/")}`;
}

function chapterUrl(library, series, chapter, engine) {
  return assetUrl(`${library.output_base}/${series}/${chapter}/chapter.${engine}.json`);
}

function pageImageUrl(library, series, chapter, image) {
  return assetUrl(`${library.library_base}/${series}/${chapter}/${image}`);
}

function seriesHref(series) {
  return `index.html?${new URLSearchParams({ series })}`;
}

function readerHref(series, chapter, engine) {
  const query = new URLSearchParams({ series, chapter });
  if (engine) query.set("engine", engine);
  return `index.html?${query}`;
}

/* ---------- header ---------- */

/* Cada tela declara o header inteiro em vez de ligar e desligar peca por peca -
   e no "por peca" que sobra um elemento da tela anterior aceso. */
function setChrome({ back = null, title = null, subtitle = "", search = false, reader = false }) {
  el.back.hidden = back === null;
  if (back) {
    el.back.href = back.href;
    el.backLabel.textContent = back.label;
  }

  el.brand.hidden = back !== null;

  el.titleBox.hidden = title === null;
  if (title !== null) {
    el.title.textContent = title;
    el.subtitle.textContent = subtitle;
  }

  el.search.hidden = !search;
  el.progressLabel.hidden = !reader;
  el.progressTrack.hidden = !reader;
  el.toggleOverlay.hidden = !reader;
  el.nextChapter.hidden = !reader;
  el.pager.hidden = !reader;
  if (!reader) el.enginePicker.hidden = true;
}

/* ---------- tela 1: estante ---------- */

function coverHtml(cover, name) {
  if (!cover) {
    // Sem capa a carta viraria um buraco no grid; a inicial da obra segura o lugar.
    return `<div class="cover-empty" aria-hidden="true">${escapeHtml(name.trim().charAt(0) || "?")}</div>`;
  }
  return `<img src="${escapeHtml(assetUrl(cover))}" alt="Capa de ${escapeHtml(name)}" loading="lazy" decoding="async">`;
}

/** O nome que a pessoa le.
 *
 * O slug e o nome da pasta e a chave de toda URL e de todo caminho gravado nos
 * JSONs; o titulo e o que muda sem quebrar caminho nenhum. `library.json` de antes
 * deste campo nao traz titulo, e ai o slug e o que sobra.
 */
function seriesTitle(entry) {
  return (entry && (entry.title || entry.series)) || "";
}

function workHtml(series) {
  const chapters = series.chapters.length;
  const pages = series.chapters.reduce((total, chapter) => total + Number(chapter.page_count), 0);

  return `<a class="work" href="${escapeHtml(seriesHref(series.series))}">
    <div class="cover">${coverHtml(series.cover, seriesTitle(series))}</div>
    <div>
      <h2>${escapeHtml(seriesTitle(series))}</h2>
      <p>${chapters} ${chapters === 1 ? "capítulo" : "capítulos"} · ${pages} páginas</p>
    </div>
  </a>`;
}

/* O painel responde para quem tem sessao, e quem esta vendo a propria biblioteca
   tem uma. A regra antiga era o endereco de origem - so 127.0.0.1 - e ela morreu
   junto com a hospedagem: atras de um proxy todo cliente chega com o IP do proxy. */
function panelLinkHtml() {
  return `<a class="btn btn-secondary" href="admin.html">Painel</a>`;
}

/* O convite da vitrine. Fica depois das obras, e nao antes: quem chegou de um
   link quer ver a traducao funcionando primeiro, e so entao decide se sobe algo. */
function inviteHtml() {
  return `<section class="invite">
    <h2>Suba a sua amostra</h2>
    <p>
      Mande de 5 a 10 páginas em inglês e veja o resultado em português.
      Nada de cadastro: o que você subir vive 48 horas e some sozinho.
    </p>
    <div class="invite-actions">
      <a class="btn btn-primary" href="admin.html">Subir uma amostra</a>
      <a class="btn btn-secondary" href="${REPOSITORY}" rel="noopener">Ver o código</a>
      <a class="btn btn-secondary" href="termos.html">Termos</a>
    </div>
    <p class="fine">
      O motor <code>claude</code> está desligado aqui porque a chave da API seria a do dono.
      Rodando na sua máquina, com a sua chave, a tradução é bem melhor — o repositório explica como.
    </p>
  </section>`;
}

function renderShelf(library, { showcase }) {
  document.title = "Tinta";
  setChrome({ search: library.series.length > 0 });
  el.main.className = "";

  if (!library.series.length) {
    el.main.innerHTML = `<p class="empty">Nenhum capítulo traduzido ainda.<br>
      Abra o <a href="admin.html">painel</a> para subir o primeiro.</p>`;
    return;
  }

  const heading = showcase
    ? `<h1>Veja a tradução funcionando</h1>
       <p>Capítulos publicáveis, traduzidos por este pipeline. Troque o motor durante a leitura para comparar.</p>`
    : `<h1>Sua biblioteca</h1>
       <p>Ponha um <code>cover.jpg</code> na pasta da obra para dar capa a ela.</p>`;

  el.main.innerHTML = `
    <div class="head">
      <div>${heading}</div>
      <div class="head-actions">
        <span class="count">${library.series.length} ${library.series.length === 1 ? "obra" : "obras"}</span>
        ${showcase ? "" : panelLinkHtml()}
      </div>
    </div>
    <div class="shelf">${library.series.map(workHtml).join("")}</div>
    ${showcase ? inviteHtml() : ""}`;

  setupSearch();
}

function setupSearch() {
  el.search.value = "";
  el.search.oninput = () => {
    const query = el.search.value.trim().toLowerCase();
    for (const card of el.main.querySelectorAll(".work")) {
      card.hidden = query !== "" && !card.textContent.toLowerCase().includes(query);
    }
  };
}

/* ---------- tela 2: capitulos da obra ---------- */

function chapterRowHtml(series, chapter) {
  return `<a class="chapter" href="${escapeHtml(readerHref(series, chapter.chapter))}">
    <strong>${escapeHtml(chapter.chapter)}</strong>
    <div class="tags">${chapter.engines.map((name) => `<span class="tag">${escapeHtml(name)}</span>`).join("")}</div>
    <span class="pages">${Number(chapter.page_count)} páginas</span>
  </a>`;
}

function renderChapters(series) {
  const count = series.chapters.length;
  const pages = series.chapters.reduce((total, chapter) => total + Number(chapter.page_count), 0);

  document.title = `${seriesTitle(series)} · Tinta`;
  setChrome({ back: { href: "index.html", label: "Biblioteca" } });
  el.main.className = "";

  el.main.innerHTML = `
    <div class="head">
      <div>
        <h1>${escapeHtml(seriesTitle(series))}</h1>
        <p>${pages} páginas traduzidas</p>
      </div>
      <span class="count">${count} ${count === 1 ? "capítulo" : "capítulos"}</span>
    </div>
    <div class="chapters">${series.chapters.map((chapter) => chapterRowHtml(series.series, chapter)).join("")}</div>`;
}

/* ---------- tela 3: leitor ---------- */

function flatten(library) {
  return library.series.flatMap((series) =>
    series.chapters.map((chapter) => ({ series: series.series, ...chapter })),
  );
}

function pickEngine(entry, requested) {
  const preferred = requested || store.get("mangatl.engine", null);
  if (preferred && entry.engines.includes(preferred)) return preferred;
  return entry.engines.includes("claude") ? "claude" : entry.engines[0];
}

function renderChapter(library, chapterData, entry, engine) {
  const { series, chapter } = entry;
  const neighbours = neighboursOf(library, entry);
  const nextHref = neighbours.following
    ? readerHref(neighbours.following.series, neighbours.following.chapter, engine)
    : null;

  const title = seriesTitle(library.series.find((item) => item.series === series));

  document.title = `${title} ${chapter} · Tinta`;
  setChrome({
    back: { href: seriesHref(series), label: title },
    title,
    subtitle: `Capítulo ${chapter} · ${engine}${chapterData.model ? ` · ${chapterData.model}` : ""}`,
    reader: true,
  });

  el.enginePicker.hidden = entry.engines.length < 2;
  el.engine.innerHTML = entry.engines
    .map((name) => `<option value="${escapeHtml(name)}"${name === engine ? " selected" : ""}>${escapeHtml(name)}</option>`)
    .join("");

  const slices = chapterData.pages.map((page) => sliceHtml(library, series, chapter, page)).join("");
  el.main.className = "reading";
  el.main.innerHTML =
    `<div class="strip">${slices}</div>` +
    orphansHtml(chapterData.pages) +
    chapterEndHtml(chapter, seriesHref(series), nextHref);

  fitOnScroll();
  setupPager(neighbours, engine, nextHref);
  trackProgress(chapterData.pages.length);
  restoreScroll(series, chapter);
}

/* ---------- tira continua ---------- */

/** Quatro decimais bastam num style inline; o resto e lixo de ponto flutuante. */
function round(value) {
  return Math.round(value * 1e4) / 1e4;
}

function bubbleHtml(block, page) {
  const rect = overlayBox(block, page);
  const size = fontCqw({
    bbox: block.bbox,
    page,
    text: block.text,
    sourceFontPx: block.source_font_px,
  });

  const box =
    `left:${round(rect.left)}%;top:${round(rect.top)}%;width:${round(rect.width)}%` +
    `;--h:${round(rect.height)}%;--limit:${round(rect.limit)}%;--min-w:${round(rect.minWidth)}%`;

  // `data-size` guarda o tamanho pedido e `--size` o que foi aplicado. Sem separar
  // os dois, refazer o ajuste partiria do valor ja encolhido e so encolheria mais.
  return `<span class="bubble" style="${box};--size:${round(size)}" data-size="${round(size)}" title="${escapeHtml(block.source_text)}"
      ><span class="t">${escapeHtml(block.text)}</span></span>`;
}

/* Fatia sem margem nem moldura. A fronteira entre fatias e detalhe do
   processamento: o capitulo e uma tira so, e e assim que ele deve aparecer. */
function sliceHtml(library, series, chapter, page) {
  const bubbles = page.blocks
    .filter((block) => block.bbox)
    .map((block) => bubbleHtml(block, page))
    .join("");

  return `<figure class="slice" id="fatia-${Number(page.index)}">
    <img src="${escapeHtml(pageImageUrl(library, series, chapter, page.image))}"
         width="${Number(page.width)}" height="${Number(page.height)}"
         alt="Trecho ${Number(page.index)} do capitulo" loading="lazy" decoding="async">
    ${bubbles}
  </figure>`;
}

function chapterEndHtml(chapter, seriesUrl, nextHref) {
  const next = nextHref
    ? `<a class="btn btn-primary" href="${escapeHtml(nextHref)}">Ler o próximo capítulo</a>`
    : "";

  return `<div class="chapter-end">
    <div class="rule"></div>
    <p>Fim do capítulo ${escapeHtml(chapter)}</p>
    <div class="actions">
      <a class="btn btn-secondary" href="${escapeHtml(seriesUrl)}">Voltar aos capítulos</a>
      ${next}
    </div>
  </div>`;
}

/* Fala que o motor achou e a deteccao local nao: sem bbox nao ha onde sobrepor.
   Vai para o fim do capitulo em vez de desaparecer. */
function orphansHtml(pages) {
  const orphans = pages.flatMap((page) =>
    page.blocks.filter((block) => !block.bbox).map((block) => ({ page: page.index, ...block })),
  );
  if (!orphans.length) return "";

  return `<details class="orphans">
    <summary>${orphans.length} fala(s) sem posicao na pagina</summary>
    <ol>${orphans
      .map(
        (block) => `<li>
          <span class="n">${Number(block.page)}</span>
          <div>
            <p class="pt">${escapeHtml(block.text)}</p>
            <p class="en">${escapeHtml(block.source_text)}</p>
          </div>
        </li>`,
      )
      .join("")}</ol>
  </details>`;
}

/* O ajuste de fonte le o layout ja pintado, entao acontece por fatia, quando ela
   chega perto da tela. Medir as 155 na abertura travaria o capitulo. */
let fitObserver = null;

/** Reajusta a fonte de cada fatia quando ela chega perto da tela.
 *
 * Chamavel de novo: `fitSlice` parte sempre do `data-size`, entao rodar duas vezes
 * da o mesmo resultado. E o que permite refazer a conta quando o overlay volta a
 * aparecer - escondido, a caixa mede zero, nada transborda, e a fala ficaria no
 * tamanho pedido mesmo sem caber.
 */
function fitOnScroll() {
  fitObserver?.disconnect();
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        observer.unobserve(entry.target);
        fitSlice(entry.target);
      }
    },
    { rootMargin: "400px 0px" },
  );

  fitObserver = observer;
  for (const slice of document.querySelectorAll(".slice")) observer.observe(slice);
}

function fitSlice(slice) {
  for (const bubble of slice.querySelectorAll(".bubble")) {
    const inner = bubble.firstElementChild;
    const start = Number(bubble.dataset.size);
    // `--limit` e a altura do balao, e nao a da caixa branca: a fala pode ocupar o
    // balao inteiro antes de passar dele. Vem da fatia e nao de `getComputedStyle`,
    // que devolveria a porcentagem ainda em `%`.
    const limit = (parseFloat(bubble.style.getPropertyValue("--limit")) / 100) * slice.clientHeight;

    // Mede o filho, e nao `scrollHeight` da caixa: o texto e centrado, e
    // transbordo centrado sai pelos dois lados - scrollHeight so ve um.
    const fitted = fitFontSize({
      start,
      overflows: (size) => {
        bubble.style.setProperty("--size", String(size));
        return inner.offsetHeight > limit;
      },
    });

    bubble.style.setProperty("--size", String(round(fitted)));
  }
}

/* A ordem e a da biblioteca inteira, nao a da obra: no ultimo capitulo de uma
   obra o "proximo" cai na seguinte, que e o que a leitura em sequencia espera. */
function neighboursOf(library, entry) {
  const all = flatten(library);
  const position = all.findIndex((item) => item.series === entry.series && item.chapter === entry.chapter);

  return {
    position,
    total: all.length,
    previous: position > 0 ? all[position - 1] : null,
    following: position >= 0 && position < all.length - 1 ? all[position + 1] : null,
  };
}

function setupPager({ position, total, previous, following }, engine, nextHref) {
  el.progress.textContent = `${position + 1} / ${total}`;

  const go = (target) => {
    if (target) location.href = readerHref(target.series, target.chapter, engine);
  };

  el.prev.disabled = !previous;
  el.next.disabled = !following;
  el.prev.onclick = () => go(previous);
  el.next.onclick = () => go(following);

  el.nextChapter.disabled = !nextHref;
  el.nextChapter.onclick = () => go(following);

  document.addEventListener("keydown", (event) => {
    if (event.target.tagName === "SELECT" || event.target.tagName === "INPUT") return;
    if (event.key === "ArrowLeft") go(previous);
    if (event.key === "ArrowRight") go(following);
  });

  enableSwipe(() => go(following), () => go(previous));
}

/* Swipe horizontal so dispara quando o gesto e claramente horizontal,
   para nao roubar o scroll vertical que e o modo normal de leitura. */
function enableSwipe(onLeft, onRight) {
  const MIN_DISTANCE = 80;
  let startX = 0;
  let startY = 0;

  document.addEventListener(
    "touchstart",
    (event) => {
      startX = event.changedTouches[0].clientX;
      startY = event.changedTouches[0].clientY;
    },
    { passive: true },
  );

  document.addEventListener(
    "touchend",
    (event) => {
      const deltaX = event.changedTouches[0].clientX - startX;
      const deltaY = event.changedTouches[0].clientY - startY;
      if (Math.abs(deltaX) < MIN_DISTANCE || Math.abs(deltaX) < Math.abs(deltaY) * 1.5) return;
      if (deltaX < 0) onLeft();
      else onRight();
    },
    { passive: true },
  );
}

/* O progresso e o do scroll, nao o de virada de pagina: a tira e continua e nao
   tem pagina para virar. A contagem em paginas so traduz a fracao para uma
   unidade que o leitor reconhece. */
function trackProgress(pageCount) {
  const paint = () => {
    const doc = document.scrollingElement || document.documentElement;
    const scrollable = doc.scrollHeight - doc.clientHeight;
    const ratio = scrollable > 0 ? Math.min(1, Math.max(0, doc.scrollTop / scrollable)) : 0;
    const page = Math.min(pageCount, Math.max(1, Math.ceil(ratio * pageCount) || 1));

    el.progressFill.style.width = `${round(ratio * 100)}%`;
    el.progressLabel.textContent = `${page} / ${pageCount}`;
  };

  window.addEventListener("scroll", paint, { passive: true });
  paint();
}

function scrollKey(series, chapter) {
  return `mangatl.scroll.${series}/${chapter}`;
}

function restoreScroll(series, chapter) {
  const saved = store.get(scrollKey(series, chapter), 0);
  if (saved > 0) requestAnimationFrame(() => window.scrollTo(0, saved));

  let pending = null;
  window.addEventListener(
    "scroll",
    () => {
      if (pending) return;
      pending = setTimeout(() => {
        pending = null;
        store.set(scrollKey(series, chapter), Math.round(window.scrollY));
      }, 400);
    },
    { passive: true },
  );
}

/* ---------- utilitarios ---------- */

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function fail(message) {
  setChrome({ back: { href: "index.html", label: "Biblioteca" } });
  el.main.className = "";
  el.main.innerHTML = `<p class="empty">${escapeHtml(message)}</p>`;
}

/* ---------- inicializacao ---------- */

function setupOverlayToggle() {
  const visible = store.get("mangatl.overlay", true);
  document.body.classList.toggle("hide-overlay", !visible);
  el.toggleOverlay.setAttribute("aria-pressed", String(visible));

  el.toggleOverlay.onclick = () => {
    const next = document.body.classList.contains("hide-overlay");
    document.body.classList.toggle("hide-overlay", !next);
    el.toggleOverlay.setAttribute("aria-pressed", String(next));
    store.set("mangatl.overlay", next);
    // Escondida, a caixa mede zero e o ajuste de fonte nao teve o que medir.
    // Quem le com a traducao desligada e liga no meio veria tudo sem caber.
    if (next) fitOnScroll();
  };
}

/* Qual biblioteca esta pessoa ve.
 *
 * A do usuario primeiro, a vitrine como resposta para quem nao tem sessao.
 *
 * `/u/library` responde 401 sem cookie e NAO cria sessao: a sessao anonima nasce
 * no primeiro upload, e nao no primeiro `GET`. E o que mantem a home aberta para
 * robo de busca e previa de link sem dar area em disco para cada um deles.
 *
 * Nao ha mais fallback para `output/library.json`. Aquele arquivo era servido por
 * caminho, e caminho deixou de responder por acervo - e o mesmo motivo pelo qual
 * uma tela de login sozinha nao protegeria nada.
 */
async function loadLibrary() {
  const mine = await fetchJson(MY_LIBRARY).catch(() => null);
  if (mine) return { library: mine, showcase: false };

  const demo = await fetchJson(SHOWCASE_INDEX).catch(() => null);
  return demo ? { library: demo, showcase: true } : null;
}

async function main() {
  setupOverlayToggle();

  const loaded = await loadLibrary();
  if (!loaded) {
    fail("Nao achei biblioteca nenhuma. Rode `mangatl build-library` e recarregue.");
    return;
  }
  const { library, showcase } = loaded;

  const seriesName = params.get("series");
  const chapterName = params.get("chapter");

  if (!seriesName) {
    renderShelf(library, { showcase });
    return;
  }

  const series = library.series.find((item) => item.series === seriesName);
  if (!series) {
    fail(`A obra ${seriesName} nao esta na biblioteca.`);
    return;
  }

  if (!chapterName) {
    renderChapters(series);
    return;
  }

  const entry = flatten(library).find((item) => item.series === seriesName && item.chapter === chapterName);
  if (!entry) {
    fail(`Capitulo ${seriesName}/${chapterName} nao esta na biblioteca.`);
    return;
  }

  const engine = pickEngine(entry, params.get("engine"));
  el.engine.onchange = () => {
    store.set("mangatl.engine", el.engine.value);
    location.href = readerHref(seriesName, chapterName, el.engine.value);
  };

  try {
    const chapterData = await fetchJson(chapterUrl(library, seriesName, chapterName, engine));
    renderChapter(library, chapterData, entry, engine);
  } catch {
    fail(`Nao consegui carregar a traducao '${engine}' de ${seriesName}/${chapterName}.`);
  }
}

main();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch(() => {
      /* sem service worker o leitor funciona, so nao guarda offline */
    });
  });
}
