/* Cache do leitor: o celular precisa reabrir um capitulo ja visitado sem rede.

   Duas politicas, porque os arquivos tem naturezas diferentes:
   - imagem de pagina nao muda de conteudo sob o mesmo nome -> cache primeiro
   - todo o resto muda quando o capitulo e reprocessado -> rede primeiro, cache
     como rede de seguranca

   O `chapter.*.json` estava do lado errado dessa divisao. A premissa era que uma
   traducao nao muda depois de gerada, e ela e falsa: `--force`, troca de motor e
   PIPELINE_VERSION novo reescrevem o arquivo sob o mesmo nome, e o leitor que ja
   tinha aberto o capitulo continuava mostrando a traducao antiga para sempre.
   Rede primeiro custa uma revalidacao de ~50KB por abertura e, offline, cai no
   cache exatamente como antes - o ganho de ler sem rede esta nas imagens, que
   sao o volume.

   Uma terceira regra entrou quando o leitor passou a ser hospedado: nada sob
   `/u/` ou `/api/` passa por aqui. Aquilo e conteudo de uma conta, o cache e da
   origem, e guardar um sai servindo para o outro.
*/

const VERSION = "mangatl-v6";
const SHELL = ["./", "./index.html", "./app.js", "./overlay.js", "./style.css", "./manifest.webmanifest", "./icon.svg", "./termos.html", "./termos.js"];
/* `admin.html` e `admin.js` ficam de fora: o painel so funciona com o servidor de
   pe, e guarda-lo offline criaria uma tela que abre e nao faz nada. */

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => name !== VERSION).map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  );
});

/* Conteudo que so existe depois de uma sessao ser conferida.
 *
 * Nada disto entra em cache, e nem passa pelo service worker. Cache e por
 * origem, nao por conta: duas pessoas no mesmo navegador dividem o mesmo
 * armazenamento, e uma pagina guardada na sessao de uma sairia servida na da
 * outra. Isso custa o modo offline do leitor hospedado, e e o preco do
 * isolamento - o leitor local, que serve `library/` por caminho, mantem o cache.
 */
function isPrivate(url) {
  return url.pathname.startsWith("/u/") || url.pathname.startsWith("/api/");
}

function isImmutable(url) {
  // Trocar a arte de uma pagina sem trocar o nome do arquivo e o unico jeito de
  // furar isto; nesse caso subir o VERSION acima e a saida.
  return /\.(jpe?g|png|webp|bmp)$/i.test(url.pathname);
}

/* Apaga tudo. Chamado pelo leitor no logout e na primeira carga em que o id da
   sessao muda - trocar de conta no mesmo navegador nao pode servir pagina da
   conta anterior. */
async function purge() {
  const names = await caches.keys();
  await Promise.all(names.map((name) => caches.delete(name)));
}

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "purge") {
    event.waitUntil(purge());
  }
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;

  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(VERSION);
    cache.put(request, response.clone());
  }
  return response;
}

async function networkFirst(request) {
  try {
    // no-cache revalida com o servidor em vez de confiar no cache HTTP do browser.
    // Sem isso o shell atualizado fica preso atras de uma copia velha e so aparece
    // depois de uma recarga forcada - o cache do service worker nao e o culpado,
    // o do browser e.
    const response = await fetch(new Request(request, { cache: "no-cache" }));
    if (response.ok) {
      const cache = await caches.open(VERSION);
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw error;
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (isPrivate(url)) return;

  event.respondWith(isImmutable(url) ? cacheFirst(request) : networkFirst(request));
});
