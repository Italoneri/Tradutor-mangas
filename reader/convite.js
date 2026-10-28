/* Aceita o convite: le o token do fragmento, cria a conta e manda para o painel.

   O token vem de `location.hash`, e nao da query, porque o fragmento nao chega
   ao servidor no pedido desta pagina nem fica no log do Caddy. Ele so sai daqui
   no corpo do POST. */

const el = {
  flash: document.getElementById("flash"),
  card: document.getElementById("invite-card"),
  noToken: document.getElementById("no-token"),
  form: document.getElementById("invite-form"),
  email: document.getElementById("invite-email"),
  password: document.getElementById("invite-password"),
  confirm: document.getElementById("invite-confirm"),
};

const token = decodeURIComponent(location.hash.slice(1));

function flash(message) {
  el.flash.textContent = message;
  el.flash.className = "flash flash-erro";
  el.flash.hidden = false;
}

async function accept(event) {
  event.preventDefault();
  if (el.password.value !== el.confirm.value) {
    flash("As duas senhas não são iguais.");
    return;
  }

  const response = await fetch("/api/invites/accept", {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: { "X-Requested-With": "fetch", "Content-Type": "application/json" },
    body: JSON.stringify({ token, email: el.email.value, password: el.password.value }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    flash(payload.error || `O servidor respondeu ${response.status}.`);
    return;
  }

  // O convite ja foi usado: tirar o token da barra e do historico antes de sair.
  history.replaceState(null, "", location.pathname);
  location.assign("admin.html");
}

if (token) {
  el.form.addEventListener("submit", (event) => {
    accept(event).catch((error) => flash(`Sem resposta do servidor: ${error.message}`));
  });
} else {
  el.card.hidden = true;
  el.noToken.hidden = false;
}
