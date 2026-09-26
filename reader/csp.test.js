/* O leitor tem que funcionar debaixo da CSP do Caddyfile.

   Sem o proxy nao ha CSP nenhuma, entao um atributo `style="..."` escrito no HTML
   funciona no desenvolvimento e some em producao. Foi assim que todo balao passou
   a cair no canto de baixo da fatia na instancia hospedada, com a bateria inteira
   passando. Estilo que depende de dado entra pelo CSSOM (`el.style`), que a CSP
   nao bloqueia. */

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { describe, it } from "node:test";

const here = new URL("./", import.meta.url);
const read = (name) => readFileSync(new URL(name, here), "utf8");
const sources = readdirSync(here).filter(
  (name) => /\.(js|html)$/.test(name) && !name.endsWith(".test.js"),
);

function cspDirective(name) {
  const caddyfile = readFileSync(new URL("../Caddyfile", here), "utf8");
  const policy = caddyfile.match(/Content-Security-Policy "([^"]+)"/);
  assert.ok(policy, "o Caddyfile nao declara Content-Security-Policy");
  const directive = policy[1].split(";").map((part) => part.trim()).find((part) => part.startsWith(`${name} `));
  return directive ? directive.split(/\s+/).slice(1) : [];
}

/** Tira comentarios de JS, para a explicacao de uma regra nao contar como violacao dela. */
function withoutComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

describe("o leitor debaixo da CSP do Caddyfile", () => {
  it("a CSP continua sem 'unsafe-inline' para estilo", () => {
    assert.ok(!cspDirective("style-src").includes("'unsafe-inline'"));
  });

  it("nenhum arquivo escreve atributo style no HTML", () => {
    const offenders = sources.filter((name) => /\bstyle\s*=\s*["'`]/.test(withoutComments(read(name))));
    assert.deepEqual(offenders, [], "use el.style / setProperty; a CSP recusa style inline");
  });

  it("nenhum arquivo usa <style> embutido", () => {
    const offenders = sources.filter((name) => /<style[\s>]/i.test(read(name)));
    assert.deepEqual(offenders, []);
  });

  it("a previa por blob: do painel esta liberada em img-src", () => {
    if (!sources.some((name) => /createObjectURL/.test(read(name)))) return;
    assert.ok(cspDirective("img-src").includes("blob:"));
  });
});
