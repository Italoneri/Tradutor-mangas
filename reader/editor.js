/* Corrigir uma fala e retraduzir uma pagina, sem sair da leitura.

   So no acervo de quem tem sessao: a vitrine e estatica e imutavel, e la nao ha
   o que gravar. O leitor chama `enableEditing` depois de pintar o capitulo, e
   este modulo e a unica parte dele que escreve - todo o resto so le.

   Clicar no texto de um balao o torna editavel. Enter grava, Esc desiste, e sair
   do balao sem Enter tambem desiste: gravar no blur transformaria um clique
   perdido numa correcao que ninguem quis fazer.
*/

const API = "../api";
const POLL_MS = 1500;
const LIVE_STATES = ["pending", "running"];

let toast = null;

function say(message, kind = "ok") {
  if (!toast) {
    toast = document.createElement("div");
    toast.className = "toast";
    toast.setAttribute("role", "status");
    document.body.append(toast);
  }
  toast.textContent = message;
  toast.dataset.kind = kind;
  toast.hidden = false;
}

/** Chamada a API com o cabecalho que o CSRF exige e a mensagem do servidor preservada. */
async function api(path, { method = "GET", body = null } = {}) {
  const headers = { "X-Requested-With": "fetch" };
  const options = { method, cache: "no-store", credentials: "same-origin", headers };
  if (body !== null) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`${API}${path}`, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${response.status} em ${path}`);
  return payload;
}

function chapterPath({ series, chapter }) {
  return `/series/${encodeURIComponent(series)}/chapters/${encodeURIComponent(chapter)}`;
}

/* ---------- corrigir uma fala ---------- */

function startEdit(text, target, onRefit) {
  if (text.isContentEditable) return;
  const bubble = text.closest(".bubble");
  const original = text.textContent;
  let saving = false;

  const finish = (value) => {
    text.contentEditable = "false";
    text.textContent = value;
    text.removeEventListener("keydown", onKey);
    text.removeEventListener("blur", onBlur);
    onRefit(bubble.closest(".slice"));
  };

  const save = async () => {
    const value = text.textContent.trim();
    if (!value || value === original) {
      finish(original);
      return;
    }
    saving = true;
    try {
      await api(`${chapterPath(target)}/translations/${encodeURIComponent(target.engine)}/blocks`, {
        method: "PUT",
        body: { page: Number(bubble.dataset.page), block: bubble.dataset.block, text: value },
      });
      bubble.dataset.edited = "true";
      finish(value);
      say("Fala corrigida. Uma retradução do capítulo mantém esta correção.");
    } catch (error) {
      finish(original);
      say(error.message, "erro");
    }
  };

  function onKey(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      save();
    } else if (event.key === "Escape") {
      finish(original);
    }
  }

  function onBlur() {
    if (!saving) finish(original);
  }

  text.contentEditable = "plaintext-only";
  text.addEventListener("keydown", onKey);
  text.addEventListener("blur", onBlur);
  text.focus();
  document.getSelection()?.selectAllChildren(text);
}

/* ---------- retraduzir uma pagina ---------- */

async function retranslate(button, target) {
  button.disabled = true;
  try {
    const created = await api("/jobs", {
      method: "POST",
      body: { series: target.series, chapter: target.chapter, engine: target.engine, pages: [button.dataset.retranslate] },
    });
    say("Página na fila para retraduzir.");
    await follow(created.job_id);
  } catch (error) {
    say(error.message, "erro");
    button.disabled = false;
  }
}

/** Acompanha o job ate o fim e recarrega: a traducao nova ja esta no disco. */
async function follow(jobId) {
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    const job = await api(`/jobs/${encodeURIComponent(jobId)}`);
    if (LIVE_STATES.includes(job.state)) {
      const where = job.state === "pending" ? "na fila" : job.progress.phase;
      say(`Retraduzindo a página: ${where}…`);
      continue;
    }
    if (job.state !== "done") throw new Error(job.error || "a retradução falhou");
    location.reload();
    return;
  }
}

/** Liga os dois gestos na tira ja pintada. `onRefit` reajusta a fonte da fatia. */
export function enableEditing(strip, target, onRefit) {
  strip.classList.add("editable");
  strip.addEventListener("click", (event) => {
    const tool = event.target.closest("[data-retranslate]");
    if (tool) {
      retranslate(tool, target);
      return;
    }
    const text = event.target.closest(".bubble .t");
    if (text) startEdit(text, target, onRefit);
  });
}
