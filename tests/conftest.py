"""Infra de testes: servidor HTTP local fake (polidez testada contra ele,
convenção §Testes do spine) e fixtures de ambiente isolado.

A partir da Story 6, os builders compartilhados entre suítes vivem AQUI com
nomes públicos — ``test_texto``/``test_datacao``/``test_consulta`` deixam de
se importar mutuamente.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import logging
import shutil
import socket
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PageObject, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from typer.testing import CliRunner

from agente_editais import fetcher
from agente_editais.consulta import app
from agente_editais.manifest import Manifesto

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


# -- builders compartilhados entre suítes (nomes públicos) ---------------------------
#
# Fixtures de PDF são GERADAS em memória com pypdf (Design Notes das stories):
# nada de binário commitado. Os fluxos usam a pipeline real contra o servidor
# fake — Documentos legítimos nascem no L1 como em produção.


def pagina_com_texto(escritor: PdfWriter, conteudo: str) -> PageObject:
    """Página com stream clássico 'BT/Tj/ET' — texto extraível pelo pypdf."""
    pagina = PageObject.create_blank_page(None, 612, 792)
    fluxo = DecodedStreamObject()
    fluxo.set_data(f"BT /F1 24 Tf 72 720 Td ({conteudo}) Tj ET".encode("latin-1"))
    referencia_fluxo = escritor._add_object(fluxo)
    fonte = DictionaryObject()
    fonte[NameObject("/Type")] = NameObject("/Font")
    fonte[NameObject("/Subtype")] = NameObject("/Type1")
    fonte[NameObject("/BaseFont")] = NameObject("/Helvetica")
    referencia_fonte = escritor._add_object(fonte)
    recursos = DictionaryObject()
    recursos[NameObject("/Font")] = DictionaryObject(
        {NameObject("/F1"): referencia_fonte}
    )
    pagina[NameObject("/Resources")] = recursos
    pagina[NameObject("/Contents")] = referencia_fluxo
    return pagina


def pdf_com_texto(paginas: list[str]) -> bytes:
    """PDF nativo mínimo com N páginas de texto real."""
    escritor = PdfWriter()
    for conteudo in paginas:
        escritor.add_page(pagina_com_texto(escritor, conteudo))
    buffer = BytesIO()
    escritor.write(buffer)
    return buffer.getvalue()


def pdf_apenas_imagem(paginas: int = 2) -> bytes:
    """Simula PDF escaneado: páginas SEM stream de texto (extração ~0 chars)."""
    escritor = PdfWriter()
    for _ in range(paginas):
        escritor.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    escritor.write(buffer)
    return buffer.getvalue()


def pdf_com_docinfo(
    paginas: list[str],
    *,
    criado_em: str | None = None,
    modificado_em: str | None = None,
    titulo: str | None = None,
) -> bytes:
    """PDF nativo mínimo com docinfo controlado pela história de teste."""
    escritor = PdfWriter()
    for conteudo in paginas:
        escritor.add_page(pagina_com_texto(escritor, conteudo))
    metadados = {}
    if criado_em:
        metadados["/CreationDate"] = criado_em
    if modificado_em:
        metadados["/ModDate"] = modificado_em
    if titulo:
        metadados["/Title"] = titulo
    if metadados:
        escritor.add_metadata(metadados)
    buffer = BytesIO()
    escritor.write(buffer)
    return buffer.getvalue()


def longo(semente: str) -> str:
    """Página 'nativa': texto ACIMA do limiar default (100 chars/página)."""
    return (
        f"{semente} - trecho de conteudo textual para extracao "
        "acima do limiar de escaneamento configurado no repositorio"
    ) * 2


def html_lista(links: list[tuple[str, str]]) -> str:
    """Página de listagem com âncoras (href, texto) — gatilho da descoberta."""
    corpo = "".join(f'<p><a href="{href}">{texto}</a></p>' for href, texto in links)
    return f"<html><body><h1>Editais</h1>{corpo}</body></html>"


def mapa_portal(servidor: http.server.ThreadingHTTPServer, *, sigla: str = "TST") -> str:
    """Bloco TOML de UM portal apontando para o servidor fake."""
    return (
        f'[[instituicao]]\nsigla = "{sigla}"\nnome = "Instituto de Teste {sigla}"\n\n'
        f'  [[instituicao.portal]]\n  nome = "Portal {sigla}"\n'
        f'  categoria = "integra"\n  url = "{url_do(servidor)}"\n'
        f'  seeds = ["{url_do(servidor)}"]\n'
    )


def registrar_candidatos(
    servidor: http.server.ThreadingHTTPServer,
    caminho_manifesto: Path,
    caminhos: list[str],
) -> None:
    """Registra candidatos tipo 'pdf' direto no Manifesto (sem descoberta)."""
    with Manifesto(caminho_manifesto) as manifesto:
        portal_id = manifesto.id_portal_por_url(url_do(servidor))
        assert portal_id is not None, "rode 'mapa validar' antes"
        for caminho in caminhos:
            assert manifesto.registrar_candidato(
                portal_id, url_do(servidor, caminho), "pdf"
            )


def coletar_pdfs(
    cli,
    politeness_veloz: SimpleNamespace,
    servidor_fake: http.server.ThreadingHTTPServer,
    corpos: dict[str, bytes],
) -> None:
    """Pipeline real até o L1: mapa validar → candidatos → coletar."""
    for caminho, corpo in corpos.items():
        servidor_fake.paginas[caminho] = (200, "application/pdf", corpo)
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    registrar_candidatos(servidor_fake, politeness_veloz.manifesto, list(corpos))
    assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0


def descobrir_coletar(
    cli,
    politeness_veloz: SimpleNamespace,
    servidor_fake: http.server.ThreadingHTTPServer,
    *,
    html: str,
    pdfs: dict[str, bytes],
) -> None:
    """Pipeline real COM descoberta: âncora persistida desde a origem (FR-6).

    O HTML declara ``charset=utf-8`` — sem charset o requests assume
    ISO-8859-1 e a âncora chegaria duplo-codificada ao banco.
    """
    servidor_fake.paginas["/ok"] = (200, "text/html; charset=utf-8", html)
    for caminho, corpo in pdfs.items():
        servidor_fake.paginas[caminho] = (200, "application/pdf", corpo)
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    assert cli.invoke(app, ["descobrir", "--portal", "TST"]).exit_code == 0
    assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0


def documentos_do_manifesto(caminho_manifesto: Path) -> list[dict]:
    """Linhas de ``documentos`` em ordem estável por URL de origem."""
    with Manifesto(caminho_manifesto) as manifesto:
        return [
            dict(linha)
            for linha in manifesto.consultar(
                "SELECT * FROM documentos ORDER BY url_origem"
            )
        ]


def fila_do_manifesto(caminho_manifesto: Path) -> list[dict]:
    """Itens da fila de revisão em TODOS os status, na ordem de criação."""
    with Manifesto(caminho_manifesto) as manifesto:
        return [dict(linha) for linha in manifesto.consultar_fila(None)]


def tipos_eventos(caminho_manifesto: Path) -> list[tuple[str, dict]]:
    """Eventos append-only como ``(tipo, detalhe)`` na ordem de gravação."""
    with Manifesto(caminho_manifesto) as manifesto:
        return [
            (linha["tipo"], json.loads(linha["detalhe"]))
            for linha in manifesto.consultar(
                "SELECT tipo, detalhe FROM eventos ORDER BY id"
            )
        ]


def saida_cli(resultado) -> str:
    """stdout + stderr do CliRunner (qualquer versão do typer/click)."""
    return resultado.output + (resultado.stderr or "")


@pytest.fixture
def corpus(politeness_veloz, monkeypatch):
    """Raiz do corpus isolada no tmp (coletar/textuar nunca tocam o repo)."""
    raiz = politeness_veloz.configs.parent / "corpus"
    monkeypatch.setenv("AGENTE_EDITAIS_CORPUS", str(raiz))
    return raiz
