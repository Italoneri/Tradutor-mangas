"""O que sai na rede sem ninguem perguntar quem esta pedindo.

Duas pastas, e as duas sao publicas por natureza: `reader/`, que e a PWA, e
`public/`, que e a vitrine que o dono escolheu publicar. Para elas, uma lista de
pastas permitidas e a ferramenta certa, porque a resposta para "quem pode ver
isto?" e de fato "qualquer um".

`library/` e `output/` ja estiveram nesta lista e sairam quando o servidor passou
a ter contas. A pergunta deixou de ser "esta pasta pode sair na rede" e virou
"esta pasta pode sair para VOCE", e lista de pasta nao sabe responder isso. Quem
responde sao as rotas `/u/` do `panel.py`, que conferem a sessao antes de mandar
um byte.

Poe-se uma tela de login na frente disto e nada fica protegido: a senha e pedida
na tela e os arquivos continuam saindo por URL direta. Era esse o centro de
gravidade do problema todo.

Sem dependencia fora da stdlib de proposito: este modulo e o unico que o servidor
de arquivos precisa, e um import pesado aqui derrubaria quem so quer servir a PWA.
"""

from __future__ import annotations

import http.server
from http import HTTPStatus
from pathlib import Path
from urllib.parse import unquote

SERVABLE_ROOTS = ("reader", "public")
"""A PWA e a vitrine. Nada mais sai por caminho."""


def _segments(request_path: str) -> list[str]:
    """Segmentos do caminho pedido, ja sem query e sem percent-encoding.

    Decodifica antes de olhar: `/%2Eenv` e `/.env` sao o mesmo arquivo, e so o
    primeiro passa por uma comparacao literal. A barra invertida conta como
    separador porque o NTFS a trata como tal, embora o http.server nao trate.
    """
    path = request_path.split("?", 1)[0].split("#", 1)[0]
    return [segment for segment in unquote(path).replace("\\", "/").split("/") if segment]


def is_servable(request_path: str) -> bool:
    """Se os bytes desse caminho podem sair na rede para qualquer um.

    A raiz responde False por nao ter nada para servir: ela lista o .env. Quem
    chama redireciona para /reader/ antes de perguntar.
    """
    segments = _segments(request_path)
    if not segments:
        return False
    # Cobre .env, .git e tambem `..`, que e o caminho de fuga para fora da raiz.
    if any(segment.startswith(".") for segment in segments):
        return False
    return segments[0] in SERVABLE_ROOTS


def is_showcase_path(request_path: str) -> bool:
    """Se o caminho cai em `public/demo/`, a vitrine que so sai com a flag ligada.

    `casefold` porque o NTFS acha `Demo` e `demo` iguais, e a flag nao pode
    depender de o servidor rodar em Linux.
    """
    return [segment.casefold() for segment in _segments(request_path)[:2]] == ["public", "demo"]


class ReaderHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler com `is_servable` na frente.

    O filtro entra em `send_head` porque e por onde GET e HEAD passam os dois,
    e antes do `translate_path` - que resolveria o `..` e apagaria a evidencia.
    """

    def send_head(self):  # noqa: ANN201 - assinatura herdada da stdlib
        if not _segments(self.path):
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", "/reader/")
            self.end_headers()
            return None
        if not is_servable(self.path):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        return super().send_head()


def make_handler(root: Path) -> type[ReaderHandler]:
    class RootedReaderHandler(ReaderHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

    return RootedReaderHandler


class _Server(http.server.ThreadingHTTPServer):
    """Uma thread por conexao.

    O servidor de uma conexao por vez basta para servir arquivo, mas nao para o
    painel: um job de traducao leva minutos e, com o servidor bloqueado, o
    navegador nao consegue nem buscar o progresso nem carregar a pagina - a tela
    congela e parece que travou.
    """

    allow_reuse_address = True
    daemon_threads = True


def serve_handler(handler: type[http.server.BaseHTTPRequestHandler], port: int) -> None:
    """Bloqueia servindo com `handler` na porta, ate KeyboardInterrupt."""
    with _Server(("0.0.0.0", port), handler) as httpd:
        httpd.serve_forever()
