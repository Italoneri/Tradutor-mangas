/* Preenche o endereco de remocao dos termos com o que o servidor diz.

   Arquivo proprio, e nao `<script>` inline: a CSP do Caddyfile so aceita script
   de `'self'`. Sem servidor, ou sem `CONTACT_EMAIL`, o texto do HTML fica - e ele
   diz que o endereco ainda nao foi configurado, em vez de mostrar um falso. */

const target = document.getElementById("contato");

async function fillContact() {
  const response = await fetch("/api/session", { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) return;
  const { contact } = await response.json();
  if (!contact) return;

  const link = document.createElement("a");
  link.href = `mailto:${contact}`;
  link.textContent = contact;
  target.replaceChildren(link);
}

fillContact().catch((error) => console.warn("termos: contato indisponivel", error));
