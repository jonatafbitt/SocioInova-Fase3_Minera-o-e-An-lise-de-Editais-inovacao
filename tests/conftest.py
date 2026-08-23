"""Infra de testes: servidor HTTP local fake (polidez testada contra ele,
convenção §Testes do spine) e fixtures de ambiente isolado."""

from __future__ import annotations

import http.server
import shutil
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parents[1]
CONFIGS_DO_REPO = REPO / "configs"

POLITENESS_VELOZ = """\
# politeness de teste: delay zero para suite veloz
delay_minimo_s = 0.0
off_peak = "22:00-06:00"
user_agent = "agente-editais-testes/0.1 (+suite pytest; finalidade academica)"

[probe]
timeout_s = 5
respeitar_janela_off_peak = false
"""


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


@pytest.fixture
def servidor_fake():
    """HTTP local em porta efêmera; ``registros`` captura método/UA/momento."""
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Manipulador)
    servidor.registros = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    try:
        yield servidor
    finally:
        servidor.shutdown()
        servidor.server_close()


def url_do(servidor: http.server.ThreadingHTTPServer, caminho: str = "/ok") -> str:
    porta = servidor.server_address[1]
    return f"http://127.0.0.1:{porta}{caminho}"


@pytest.fixture
def porta_morta() -> int:
    """Porta TCP livre SEM servidor — conexões falham na hora."""
    com_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    com_socket.bind(("127.0.0.1", 0))
    porta = com_socket.getsockname()[1]
    com_socket.close()
    return porta


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
