"""Infra de testes: servidor HTTP local fake (polidez testada contra ele,
convenção §Testes do spine) e fixtures de ambiente isolado."""

from __future__ import annotations

import http.server
import logging
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agente_editais import fetcher

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[1]
CONFIGS_DO_REPO = REPO / "configs"

POLITENESS_VELOZ = """\
# politeness de teste: delay zero para suite veloz
delay_minimo_s = 0.0
off_peak = "22:00-06:00"
user_agent = "agente-editais-testes/0.1 (+suite pytest; finalidade academica)"

[crawl]
# suite roda a qualquer hora — janela do crawling desligada NOS TESTES
respeitar_janela_off_peak = false

[probe]
timeout_s = 5
respeitar_janela_off_peak = false
"""


class ServidorFalso(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer com knobs de conteúdo para os cenários da story.

    Atributos configuráveis por teste:
    - ``paginas``: caminho → (status, content_type, corpo) — páginas de teste;
    - ``robots_txt``: corpo de /robots.txt (None ⇒ cai no fluxo padrão);
    - ``aborts_conexao``: caminhos que derrubam a conexão SEM resposta
      (simula rede morrendo no meio da navegação);
    - ``registros``: cada requisição recebida (método/UA/momento/caminho) —
      prova de que um caminho NÃO foi requisitado (bloqueio robots) ou de
      quantas vezes foi (cache, dedupe).
    """

    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.registros: list[dict] = []
        self.redirect_absoluto: dict[str, str] = {}
        self.paginas: dict[str, tuple[int, str, object]] = {}
        self.robots_txt: str | None = None
        self.aborts_conexao: set[str] = set()

    def handle_error(self, request, client_address) -> None:
        # conexões abortadas DE PROPÓSITO não passam por aqui (fechamos sem
        # escrever); o que chegar é bug de teste/fixture — logue, não engula
        logger.warning(
            "ServidorFalso: exceção não tratada atendendo %s: %s",
            client_address,
            sys.exc_info()[1],
        )


class _Manipulador(http.server.BaseHTTPRequestHandler):
    server_version = "FakePortal/1.0"

    def log_message(self, *args: object) -> None:  # silencia o log padrão
        pass

    def _responder(self, metodo: str) -> None:
        self.server.registros.append(  # type: ignore[attr-defined]
            {
                "metodo": metodo,
                "user_agent": self.headers.get("User-Agent"),
                "quando": time.monotonic(),
                "caminho": self.path,
            }
        )
        caminho = self.path.split("?")[0]
        if caminho in self.server.aborts_conexao:  # type: ignore[attr-defined]
            # rede "morre": sem resposta HTTP — o cliente vê ConnectionError
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        pagina = self.server.paginas.get(caminho)  # type: ignore[attr-defined]
        if pagina is not None:
            status, content_type, corpo = pagina
            if isinstance(corpo, str):
                corpo = corpo.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            if metodo != "HEAD":
                self.wfile.write(corpo)
            return
        if caminho == "/robots.txt":
            conteudo = getattr(self.server, "robots_txt", None)
            if conteudo is not None:
                corpo = conteudo.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                if metodo != "HEAD":
                    self.wfile.write(corpo)
                return
        absoluto = getattr(self.server, "redirect_absoluto", {}).get(caminho)
        if absoluto:
            self.send_response(301)
            self.send_header("Location", absoluto)
            self.end_headers()
            return
        if caminho == "/movido":
            self.send_response(301)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        if caminho == "/so-get" and metodo == "HEAD":
            self.send_response(405)
            self.end_headers()
            return
        if caminho == "/erro-servidor":
            self.send_response(500)
            self.end_headers()
            return
        corpo = b"<html><body><a href='/editais'>Editais</a></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_HEAD(self) -> None:  # noqa: NIP221 — API do http.server
        self._responder("HEAD")

    def do_GET(self) -> None:
        self._responder("GET")


def _novo_servidor(host: str = "127.0.0.1") -> ServidorFalso:
    servidor = ServidorFalso((host, 0), _Manipulador)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor


@pytest.fixture
def servidor_fake():
    """HTTP local em porta efêmera; ``registros`` captura método/UA/momento.

    ``servidor.redirect_absoluto`` mapeia caminho → URL absoluta para simular
    redirect entre hosts (ex.: 127.0.0.1 → localhost). Veja ``ServidorFalso``
    para ``paginas``/``robots_txt``/``aborts_conexao``.
    """
    servidor = _novo_servidor()
    try:
        yield servidor
    finally:
        servidor.shutdown()
        servidor.server_close()


def url_do(servidor: http.server.ThreadingHTTPServer, caminho: str = "/ok") -> str:
    host, porta = servidor.server_address[:2]
    return f"http://{host}:{porta}{caminho}"


def url_com_host(
    servidor: http.server.ThreadingHTTPServer, host: str, caminho: str = "/ok"
) -> str:
    """URL com hostname EXPLÍTITO (porta do servidor).

    Necessário quando o bind resolve para outro endereço (ex.: fixture
    ``servidor_localhost`` liga em 'localhost' mas o SO reporta 127.0.0.1):
    a URL do portal precisa carregar o HOSTNAME sob teste, não o IP do bind.
    """
    porta = servidor.server_address[1]
    return f"http://{host}:{porta}{caminho}"


@pytest.fixture
def criar_servidor_fake():
    """Fábrica de servidores fake para testes que precisam de VÁRIOS hosts.

    ``criar_servidor_fake()`` usa 127.0.0.1; ``criar_servidor_fake("localhost")``
    dá um SEGUNDO hostname de verdade — necessário quando a política sob teste
    distingue hosts (ex.: suspensão de host por 403 na coleta). Sem hardcode
    de 127.0.0.2: nem todo ambiente aceita bind/rotas em loopback "extra".
    Se o bind em ``localhost`` falhar (OSError) OU o hostname não resolver/
    servir de verdade, o teste deve usar o guard ``servidor_localhost`` abaixo
    e pular (pytest.skip) — nunca fingir um segundo host que não existe.
    """
    criados: list[ServidorFalso] = []

    def _criar(host: str = "127.0.0.1") -> ServidorFalso:
        servidor = _novo_servidor(host)
        criados.append(servidor)
        return servidor

    yield _criar
    for servidor in criados:
        servidor.shutdown()
        servidor.server_close()


@pytest.fixture
def servidor_localhost(criar_servidor_fake):
    """Segundo servidor fake no hostname 'localhost', COM guard de ambiente.

    Devolve o servidor só se ele for alcançável de verdade via
    ``http://localhost:<porta>`` (resolução IPv4/IPv6 simétrica); caso
    contrário o teste é pulado — ambientes exóticos não viram falso-positivo.
    """
    import urllib.request

    try:
        servidor = criar_servidor_fake("localhost")
    except OSError as exc:
        pytest.skip(f"bind em 'localhost' indisponível neste ambiente: {exc}")
    porta = servidor.server_address[1]
    try:
        with urllib.request.urlopen(f"http://localhost:{porta}/ok", timeout=5):
            pass
    except OSError:
        pytest.skip("'localhost' não alcança este servidor neste ambiente")
    return servidor


@pytest.fixture
def porta_morta():
    """Porta TCP ocupada SEM serviço escutando — conexões são recusadas na hora.

    O socket permanece ABERTO (bind sem listen) durante todo o teste: isso
    garante que a porta não seja reutilizada pelo SO no meio do teste
    (corrida de porta efêmera) e, sem listener, o connect recebe RST —
    recusa imediata, não timeout.
    """
    com_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    com_socket.bind(("127.0.0.1", 0))
    com_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    porta = com_socket.getsockname()[1]
    try:
        yield porta
    finally:
        com_socket.close()


@pytest.fixture(autouse=True)
def estado_polidez_limpo():
    """Isola o estado global de delay entre testes (_ultimo_pedido_por_host)."""
    fetcher.reiniciar_estado_polidez()
    yield
    fetcher.reiniciar_estado_polidez()


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


@pytest.fixture
def ambiente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Isola configs e Manifesto em tmp_path via variáveis de ambiente."""
    configs = tmp_path / "configs"
    configs.mkdir()
    manifesto = tmp_path / "dados" / "manifesto.sqlite3"
    monkeypatch.setenv("AGENTE_EDITAIS_CONFIGS", str(configs))
    monkeypatch.setenv("AGENTE_EDITAIS_MANIFESTO", str(manifesto))
    return SimpleNamespace(configs=configs, manifesto=manifesto)


@pytest.fixture
def politeness_veloz(ambiente: SimpleNamespace) -> SimpleNamespace:
    (ambiente.configs / "politeness.toml").write_text(POLITENESS_VELOZ, encoding="utf-8")
    return ambiente


@pytest.fixture
def configs_reais_no_tmp(ambiente: SimpleNamespace) -> SimpleNamespace:
    """Copia os configs versionados do repo para o tmp (o repo não é tocado)."""
    shutil.copytree(CONFIGS_DO_REPO, ambiente.configs, dirs_exist_ok=True)
    return ambiente


MAPA_MINIMO = """\
[[instituicao]]
sigla = "{sigla}"
nome = "Instituição de Teste {sigla}"

  [[instituicao.portal]]
  nome = "Portal {portal}"
  categoria = "{categoria}"
  url = "{url_portal}"
  seeds = [{seeds}]
"""


def escrever_mapa(ambiente: SimpleNamespace, texto: str) -> Path:
    """Grava o mapa com o cabeçalho de schema garantido uma única vez."""
    corpo_sem_cabecalho = "\n".join(
        linha
        for linha in texto.splitlines()
        if not linha.strip().startswith("schema_version")
    )
    conteudo = f"schema_version = 1\n{corpo_sem_cabecalho}\n"
    caminho = ambiente.configs / "mapa-mestre.toml"
    caminho.write_text(conteudo, encoding="utf-8")
    return caminho


def mapa_minimo(
    *,
    sigla: str = "TST",
    portal: str = "A",
    categoria: str = "integra",
    url_portal: str,
    seeds: list[str],
) -> str:
    sementes = ", ".join(f'"{seed}"' for seed in seeds)
    return MAPA_MINIMO.format(
        sigla=sigla,
        portal=portal,
        categoria=categoria,
        url_portal=url_portal,
        seeds=sementes,
    )
