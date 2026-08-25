"""Fetcher — ÚNICO pacote com I/O de rede (AD-5).

Story 1 trouxe o **probe do pré-voo** de seeds (FR-2): HEAD com fallback GET,
timeout curto e polidez (delay mínimo por host) — intocado nesta story.

Story 2 (CAP-2) estende o monopólio com:
- ``robots_para_host``: consulta/honra robots.txt por host ANTES da primeira
  requisição ao caminho (§9.1 — violar é bug crítico), com cache por execução;
  robots inacessível ⇒ PERMITE, com a decisão carregada na ``DecisaoRobots``
  para o chamador registrar evento;
- ``obter_html``: obtenção de páginas estática por padrão; Playwright no
  gatilho duplo (portal marcado ``dinamico=true`` OU conteúdo-alvo ausente no
  HTML estático) — engine importada sob demanda em ``_obter_com_playwright``
  e troca registrada no resultado para o chamador auditar por portal.

Story 3 (CAP-4) completa o monopólio com o download de documentos:
- ``baixar_stream``: GET em stream para arquivo temporário com hash SHA-256
  calculado DURANTE a leitura e cap duro de tamanho (``[crawl]
  max_mb_documento``); redirects continuam hop-a-hop com robots/delay;
- contador de HTTP 403 CONSECUTIVOS por host — ``_LIMITE_403_SUSPENSAO``
  seguidos sinalizam suspensão ao chamador (coleta), que pula o resto do
  lote do portal e segue os demais, com evento.

Toda página obtida passa pela polidez existente: delay por hostname, Session
única compartilhável pelo lote e redirects cobertos.

A janela off-peak é interpretada NO FUSO DO HOST. O probe segue checagem leve
(knob próprio); o CRAWLING obriga a janela quando
``[crawl] respeitar_janela_off_peak`` (default true) — regra que estreia na
Story 2, conforme notas da Story 1.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import re
import threading
import time
import tomllib
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as hora_toml
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from pydantic import BaseModel, Field, field_validator

from .mapa import Portal, hostname_de, normalizar_url


class ViolacaoPolidez(RuntimeError):
    """Regra de politude violada/exigida não satisfeita (AD-5, §9 PRD)."""


class ErroConfigPolidez(ValueError):
    """politeness.toml ausente ou malformado."""


class Polidez(BaseModel):
    """Parâmetros de cortesia vindos de ``configs/politeness.toml``."""

    delay_minimo_s: float = Field(ge=0, allow_inf_nan=False)
    off_peak: str
    user_agent: str = Field(min_length=1)
    probe_timeout_s: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    probe_respeitar_janela_off_peak: bool = False
    # crawling (Story 2 em diante): janela off-peak é obrigatória POR DEFAULT.
    crawl_respeitar_janela_off_peak: bool = True
    # teto de páginas por portal na descoberta — politude contra espiral de BFS.
    max_paginas_por_portal: int = Field(default=200, gt=0)
    # cap de tamanho POR DOCUMENTO baixado (MB) — excedente não é gravado (CAP-4).
    max_mb_documento: float = Field(default=50.0, gt=0, allow_inf_nan=False)
    # prazo TOTAL de cada download (s): trickle que estourar vira falha do
    # candidato — nunca um PDF pendurado segura o lote.
    download_prazo_s: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    # timeout de LEITURA do download (s), separado do timeout do probe.
    download_timeout_s: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    # 403 seguidos que caracterizam bloqueio persistente e suspendem o host.
    max_403_consecutivos: int = Field(default=3, gt=0)
    # CAP-6 (Story 4): limiar de PDF escaneado — caracteres extraíveis POR
    # PÁGINA; abaixo de limiar × nº páginas o Documento recebe
    # flag_escaneado=true. Vive em politeness.toml ([texto]) como todo knob
    # declarativo versionado (AD-9).
    texto_limiar_chars_por_pagina: int = Field(default=100, gt=0)

    @field_validator("off_peak")
    @classmethod
    def _janela_valida(cls, valor: str) -> str:
        _janela_de(valor)  # levanta ErroConfigPolidez se malformada
        return valor


_CHAVES_RAIZ = ("delay_minimo_s", "off_peak", "user_agent")
_CHAVES_PROBE = ("timeout_s", "respeitar_janela_off_peak")
_CHAVES_CRAWL = (
    "respeitar_janela_off_peak",
    "max_paginas_por_portal",
    "max_mb_documento",
    "download_prazo_s",
    "download_timeout_s",
    "max_403_consecutivos",
)
_CHAVES_TEXTO = ("limiar_chars_por_pagina",)


def _numero_positivo(caminho: Path, campo: str, valor: object) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErroConfigPolidez(
            f"{caminho}: campo '{campo}' deve ser número, recebido {type(valor).__name__}."
        )
    return float(valor)


def _texto_obrigatorio(caminho: Path, campo: str, valor: object) -> str:
    if not isinstance(valor, str):
        raise ErroConfigPolidez(
            f"{caminho}: campo '{campo}' deve ser texto, recebido {type(valor).__name__}."
        )
    return valor


def carregar_polidez(caminho: Path) -> Polidez:
    """Lê e valida politeness.toml — paridade com o extra=forbid do mapa."""
    caminho = Path(caminho)
    try:
        dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ErroConfigPolidez(f"{caminho}: não foi possível ler ({exc}).") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ErroConfigPolidez(f"{caminho}: TOML malformado — {exc}") from exc

    desconhecidas = sorted(set(dados) - set(_CHAVES_RAIZ) - {"probe", "crawl", "texto"})
    if desconhecidas:
        raise ErroConfigPolidez(
            f"{caminho}: chaves desconhecidas na raiz: {', '.join(desconhecidas)}; "
            f"aceitas: {', '.join(_CHAVES_RAIZ)}, [probe], [crawl], [texto]"
        )
    faltantes = [chave for chave in _CHAVES_RAIZ if chave not in dados]
    if faltantes:
        raise ErroConfigPolidez(f"{caminho}: chave obrigatória ausente: {', '.join(faltantes)}")

    probe_bruto = dados.get("probe", {})
    if not isinstance(probe_bruto, dict):
        raise ErroConfigPolidez(f"{caminho}: seção '[probe]' deve ser uma tabela TOML.")
    desconhecidas_probe = sorted(set(probe_bruto) - set(_CHAVES_PROBE))
    if desconhecidas_probe:
        raise ErroConfigPolidez(
            f"{caminho}: chaves desconhecidas em [probe]: {', '.join(desconhecidas_probe)}; "
            f"aceitas: {', '.join(_CHAVES_PROBE)}"
        )

    crawl_bruto = dados.get("crawl", {})
    if not isinstance(crawl_bruto, dict):
        raise ErroConfigPolidez(f"{caminho}: seção '[crawl]' deve ser uma tabela TOML.")
    desconhecidas_crawl = sorted(set(crawl_bruto) - set(_CHAVES_CRAWL))
    if desconhecidas_crawl:
        raise ErroConfigPolidez(
            f"{caminho}: chaves desconhecidas em [crawl]: {', '.join(desconhecidas_crawl)}; "
            f"aceitas: {', '.join(_CHAVES_CRAWL)}"
        )

    respeitar = probe_bruto.get("respeitar_janela_off_peak", False)
    if not isinstance(respeitar, bool):
        raise ErroConfigPolidez(
            f"{caminho}: 'probe.respeitar_janela_off_peak' exige booleano estrito "
            f"(true/false), recebido {type(respeitar).__name__}."
        )
    crawl_respeitar = crawl_bruto.get("respeitar_janela_off_peak", True)
    if not isinstance(crawl_respeitar, bool):
        raise ErroConfigPolidez(
            f"{caminho}: 'crawl.respeitar_janela_off_peak' exige booleano estrito "
            f"(true/false), recebido {type(crawl_respeitar).__name__}."
        )
    max_paginas = crawl_bruto.get("max_paginas_por_portal", 200)
    if isinstance(max_paginas, bool) or not isinstance(max_paginas, int) or max_paginas <= 0:
        raise ErroConfigPolidez(
            f"{caminho}: 'crawl.max_paginas_por_portal' exige inteiro positivo, "
            f"recebido {max_paginas!r}."
        )
    max_mb = crawl_bruto.get("max_mb_documento", 50.0)
    if (
        isinstance(max_mb, bool)
        or not isinstance(max_mb, (int, float))
        or not math.isfinite(float(max_mb))
        or float(max_mb) <= 0
    ):
        raise ErroConfigPolidez(
            f"{caminho}: 'crawl.max_mb_documento' exige número positivo finito "
            f"(inf/nan recusados), recebido {max_mb!r}."
        )
    prazo = crawl_bruto.get("download_prazo_s", 300.0)
    if (
        isinstance(prazo, bool)
        or not isinstance(prazo, (int, float))
        or not math.isfinite(float(prazo))
        or float(prazo) <= 0
    ):
        raise ErroConfigPolidez(
            f"{caminho}: 'crawl.download_prazo_s' exige número positivo finito, "
            f"recebido {prazo!r}."
        )
    timeout_leitura = crawl_bruto.get("download_timeout_s", 60.0)
    if (
        isinstance(timeout_leitura, bool)
        or not isinstance(timeout_leitura, (int, float))
        or not math.isfinite(float(timeout_leitura))
        or float(timeout_leitura) <= 0
    ):
        raise ErroConfigPolidez(
            f"{caminho}: 'crawl.download_timeout_s' exige número positivo finito, "
            f"recebido {timeout_leitura!r}."
        )
    max_403 = crawl_bruto.get("max_403_consecutivos", 3)
    if isinstance(max_403, bool) or not isinstance(max_403, int) or max_403 <= 0:
        raise ErroConfigPolidez(
            f"{caminho}: 'crawl.max_403_consecutivos' exige inteiro positivo, "
            f"recebido {max_403!r}."
        )

    texto_bruto = dados.get("texto", {})
    if not isinstance(texto_bruto, dict):
        raise ErroConfigPolidez(f"{caminho}: seção '[texto]' deve ser uma tabela TOML.")
    desconhecidas_texto = sorted(set(texto_bruto) - set(_CHAVES_TEXTO))
    if desconhecidas_texto:
        raise ErroConfigPolidez(
            f"{caminho}: chaves desconhecidas em [texto]: {', '.join(desconhecidas_texto)}; "
            f"aceitas: {', '.join(_CHAVES_TEXTO)}"
        )
    limiar = texto_bruto.get("limiar_chars_por_pagina", 100)
    if isinstance(limiar, bool) or not isinstance(limiar, int) or limiar <= 0:
        raise ErroConfigPolidez(
            f"{caminho}: 'texto.limiar_chars_por_pagina' exige inteiro positivo, "
            f"recebido {limiar!r}."
        )

    try:
        return Polidez(
            delay_minimo_s=_numero_positivo(caminho, "delay_minimo_s", dados["delay_minimo_s"]),
            off_peak=_texto_obrigatorio(caminho, "off_peak", dados["off_peak"]),
            user_agent=_texto_obrigatorio(caminho, "user_agent", dados["user_agent"]),
            probe_timeout_s=(
                _numero_positivo(caminho, "probe.timeout_s", probe_bruto.get("timeout_s", 10.0))
                if "timeout_s" in probe_bruto
                else 10.0
            ),
            probe_respeitar_janela_off_peak=respeitar,
            crawl_respeitar_janela_off_peak=crawl_respeitar,
            max_paginas_por_portal=max_paginas,
            max_mb_documento=float(max_mb),
            download_prazo_s=float(prazo),
            download_timeout_s=float(timeout_leitura),
            max_403_consecutivos=max_403,
            texto_limiar_chars_por_pagina=limiar,
        )
    except ValueError as exc:
        raise ErroConfigPolidez(f"{caminho}: valor inválido — {exc}") from exc


# -- janela off-peak ---------------------------------------------------------


def _hora_de(texto: str) -> hora_toml:
    partes = texto.split(":")
    if len(partes) != 2 or not all(p.isdigit() and len(p) == 2 for p in partes):
        raise ValueError(f"horário '{texto}' inválido — esperado HH:MM")
    horas, minutos = int(partes[0]), int(partes[1])
    if not (0 <= horas <= 23 and 0 <= minutos <= 59):
        raise ValueError(f"horário '{texto}' fora da faixa")
    return hora_toml(horas, minutos)


def _janela_de(off_peak: str) -> tuple[hora_toml, hora_toml]:
    pedacos = off_peak.split("-")
    if len(pedacos) != 2:
        raise ValueError(f"janela '{off_peak}' inválida — esperado 'HH:MM-HH:MM'")
    inicio, fim = _hora_de(pedacos[0]), _hora_de(pedacos[1])
    if inicio == fim:
        raise ValueError(f"janela '{off_peak}' é vazia (início = fim)")
    return inicio, fim


def dentro_da_janela_off_peak(off_peak: str, agora: datetime | None = None) -> bool:
    """True se ``agora`` (default: relógio local do HOST) está em [início, fim).

    Suporta janelas que cruzam a meia-noite (ex.: "22:00-06:00").
    """
    inicio, fim = _janela_de(off_peak)
    momento = (agora or datetime.now().astimezone()).time()
    if inicio <= fim:
        return inicio <= momento < fim
    return momento >= inicio or momento < fim


# -- delay mínimo por host ---------------------------------------------------

_ultimo_pedido_por_host: dict[str, float] = {}
_trava_delay = threading.Lock()


def reiniciar_cache_robots() -> None:
    """Limpa o cache de robots.txt — início de cada EXECUÇÃO de comando."""
    with _trava_robots:
        _robots_por_origem.clear()


def reiniciar_estado_polidez() -> None:
    """Limpa delay, cache de robots e contadores 403 (início de execução/testes)."""
    with _trava_delay:
        _ultimo_pedido_por_host.clear()
    with _trava_403:
        _contadores_403.clear()
    reiniciar_cache_robots()


def _aguardar_delay(host: str, delay_minimo_s: float) -> None:
    with _trava_delay:
        anterior = _ultimo_pedido_por_host.get(host)
    if anterior is not None and delay_minimo_s > 0:
        resta = delay_minimo_s - (time.monotonic() - anterior)
        if resta > 0:
            time.sleep(resta)


def _marcar_pedido(host: str) -> None:
    with _trava_delay:
        _ultimo_pedido_por_host[host] = time.monotonic()


# -- probe do pré-voo (FR-2) --------------------------------------------------


@dataclass(slots=True)
class ResultadoSeed:
    url: str
    ok: bool
    status_http: int | None
    metodo: str
    erro: str | None
    duracao_s: float


_METODOS_COM_HEAD_BLOQUEADO = frozenset({405, 501})


def nova_sessao(user_agent: str) -> requests.Session:
    """Session com UA acadêmico — UMA por lote (reuso de TCP/TLS, AD-5)."""
    sessao = requests.Session()
    sessao.headers["User-Agent"] = user_agent
    return sessao


# -- robots.txt (§9.1 — mandatório antes da primeira requisição ao caminho) ----


@dataclass(slots=True)
class DecisaoRobots:
    """Resultado da consulta a ``<origem>/robots.txt``, cacheável por execução.

    ``estado='inacessivel'`` ⇒ PERMITE tudo (contrato da story), e o chamador
    registra evento com ``detalhe`` para custódia da decisão.
    ``crawl_delay_s`` carrega o Crawl-delay FRACIONÁRIO do grupo aplicável —
    o robotparser do stdlib só aceita inteiro.
    """

    origem: str  # esquema://host[:porta] — chave do grupo de URIs do RFC 9309
    estado: str  # 'consultado' | 'inacessivel'
    detalhe: str = ""
    parser: urllib.robotparser.RobotFileParser | None = None
    crawl_delay_s: float | None = None

    @property
    def acessivel(self) -> bool:
        return self.estado == "consultado"

    def pode_acessar(self, url: str, user_agent: str) -> bool:
        if self.parser is None:
            return True
        return self.parser.can_fetch(user_agent, url)


_RE_CRAWL_DELAY = re.compile(r"^\d+(?:\.\d+)?$")


def _extrair_crawl_delay(corpo: str, user_agent: str) -> float | None:
    """Crawl-delay (float) do PRIMEIRO grupo que se aplica ao nosso token.

    Espelha a semântica de ``applies_to`` do stdlib (grupo com ``*`` ou cujo
    token apareça no nosso user agent), mas aceita valores fracionários que
    o robotparser descarta (``'0.3'.isdecimal()`` é False).
    """
    token = user_agent.split("/")[0].strip().lower()
    grupos: list[tuple[list[str], float | None]] = []
    agentes: list[str] = []
    delay: float | None = None
    viu_regra = False

    def _fechar_grupo() -> None:
        nonlocal agentes, delay, viu_regra
        if agentes:
            grupos.append((agentes, delay))
        agentes, delay, viu_regra = [], None, False

    for bruta in corpo.splitlines():
        linha = bruta.split("#", 1)[0].strip()
        if not linha:
            _fechar_grupo()
            continue
        if ":" not in linha:
            continue
        chave, valor = linha.split(":", 1)
        chave = chave.strip().lower()
        valor = valor.strip()
        if chave == "user-agent":
            if viu_regra:
                _fechar_grupo()
            agentes.append(valor.lower())
        elif not agentes:
            continue
        else:
            viu_regra = True
            if chave == "crawl-delay" and _RE_CRAWL_DELAY.fullmatch(valor):
                delay = float(valor)
    _fechar_grupo()

    for agentes_do_grupo, delay_do_grupo in grupos:
        for agente in agentes_do_grupo:
            if agente == "*" or agente in token:
                return delay_do_grupo
    return None


_trava_robots = threading.Lock()
_robots_por_origem: dict[str, DecisaoRobots] = {}

_STATUS_REDIRECT = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 10
_LIMITE_HTML_BYTES = 20 * 1024 * 1024


def _origem_de(url: str) -> str:
    """Origem normalizada (esquema+host+porta) que define o grupo de robots."""
    partes = urlsplit(normalizar_url(url))
    return urlunsplit((partes.scheme, partes.netloc, "", "", ""))


def robots_para_host(
    host: str, polidez: Polidez, sessao: requests.Session | None = None
) -> DecisaoRobots:
    """Consulta robots.txt do host (§9.1): robotparser + cache POR EXECUÇÃO.

    ``host`` aceita hostname ('ifba.edu.br'), origem ('https://ifba.edu.br') ou
    qualquer URL absoluta — esquema/porta são derivados (robots é definido por
    grupo esquema+host+porta; portas não-default, como o fake local, ficam na
    chave). A leitura vai PELA Session polida (delay/timeout), nunca pelo
    urllib interno do robotparser. Inacessível (rede, HTTP ≥ 400 ou host
    malformado) ⇒ decisão 'inacessivel' = permitir, registrável em evento —
    nunca exceção crua para o chamador.
    """
    alvo = host if "://" in host else f"https://{host}"
    try:
        origem = _origem_de(alvo)
    except ValueError as exc:
        return DecisaoRobots(
            origem=alvo.strip(),
            estado="inacessivel",
            detalhe=f"ValueError: {exc}",
            parser=None,
        )

    with _trava_robots:
        em_cache = _robots_por_origem.get(origem)
    if em_cache is not None:
        return em_cache

    propria = sessao is None
    if propria:
        sessao = nova_sessao(polidez.user_agent)
    assert sessao is not None

    balde = hostname_de(origem)
    url_robots = f"{origem}/robots.txt"
    corpo: str | None = None
    detalhe = ""
    try:
        _aguardar_delay(balde, polidez.delay_minimo_s)
        resposta = sessao.get(
            url_robots,
            timeout=(polidez.probe_timeout_s, polidez.probe_timeout_s),
            allow_redirects=True,
        )
        _marcar_pedido(balde)
        if resposta.status_code < 400:
            corpo = resposta.text
        else:
            detalhe = f"HTTP {resposta.status_code} em {url_robots}"
    except requests.RequestException as exc:
        detalhe = f"{type(exc).__name__}: {exc}"
    finally:
        if propria:
            sessao.close()

    parser: urllib.robotparser.RobotFileParser | None = None
    crawl_delay_s: float | None = None
    if corpo is not None:
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(corpo.splitlines())
        crawl_delay_s = _extrair_crawl_delay(corpo, polidez.user_agent)
    decisao = DecisaoRobots(
        origem=origem,
        estado="consultado" if corpo is not None else "inacessivel",
        detalhe=detalhe,
        parser=parser,
        crawl_delay_s=crawl_delay_s,
    )
    with _trava_robots:
        _robots_por_origem[origem] = decisao
    return decisao


def _delay_efetivo(polidez: Polidez, decisao: DecisaoRobots | None) -> float:
    """Polidez por host: ``max(delay_minimo_s, Crawl-delay do robots)``.

    O Crawl-delay é a vontade do PRÓPRIO host — honrá-la é parte de §9.1;
    o piso configurado continua valendo quando o host não declara nada.
    """
    if decisao is not None and decisao.crawl_delay_s is not None:
        return max(polidez.delay_minimo_s, float(decisao.crawl_delay_s))
    return polidez.delay_minimo_s


# -- obtenção de páginas (CAP-2): estático → gatilho → Playwright --------------


@dataclass(slots=True)
class ResultadoPagina:
    """Desfecho de ``obter_html`` — o chamador converte em eventos/estado."""

    url: str  # solicitada (normalizada)
    url_final: str  # após redirects — base correta p/ URLs relativas
    ok: bool
    engine: str  # 'estatico' | 'playwright'
    status_http: int | None = None
    gatilho_playwright: str | None = None  # 'portal_dinamico' | 'conteudo_ausente'
    html: str | None = None
    erro: str | None = None
    bloqueio_robots: bool = False
    duracao_s: float = 0.0


def _obter_com_playwright(url: str, *, user_agent: str, timeout_s: float) -> tuple[int, str]:
    """Engine Playwright importada SOB DEMANDA (Design Notes da story).

    Os testes exercitam os gatilhos SEM navegador real via monkeypatch desta
    função; smoke manual com browser real está documentado no README.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        try:
            contexto = navegador.new_context(user_agent=user_agent)
            pagina = contexto.new_page()
            resposta = pagina.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
            if resposta is None:
                # sem resposta não há como auditar status — falha explícita,
                # nunca sucesso 200 falso
                raise RuntimeError(f"goto sem resposta para {url}")
            html = pagina.content()
            return int(resposta.status), html
        finally:
            navegador.close()


def _tentar_playwright(
    destino: str,
    polidez: Polidez,
    gatilho: str,
    inicio: float,
    *,
    url_final_base: str | None = None,
    sessao: requests.Session | None = None,
) -> ResultadoPagina:
    """Executa o engine registrado no evento do chamador; falha vira resultado.

    O browser também cortesia: delay efetivo (incluindo Crawl-delay) no balde
    do host ANTES do goto. ``url_final_base`` propaga a URL FINAL do estático
    anterior (redirects) — é ela a base correta para links relativos.
    """
    decisao = robots_para_host(destino, polidez, sessao=sessao)
    balde = hostname_de(destino)
    try:
        _aguardar_delay(balde, _delay_efetivo(polidez, decisao))
        try:
            status, html = _obter_com_playwright(
                destino, user_agent=polidez.user_agent, timeout_s=polidez.probe_timeout_s
            )
        finally:
            _marcar_pedido(balde)  # o servidor recebeu (ou tentamos) — conta pedido
    except Exception as exc:  # noqa: BLE001 — falha do browser é erro de EVENTO, não crash
        return ResultadoPagina(
            url=destino,
            url_final=url_final_base or destino,
            ok=False,
            engine="playwright",
            gatilho_playwright=gatilho,
            erro=f"{type(exc).__name__}: {exc}",
            duracao_s=time.perf_counter() - inicio,
        )
    if not isinstance(status, int):
        return ResultadoPagina(
            url=destino,
            url_final=url_final_base or destino,
            ok=False,
            engine="playwright",
            gatilho_playwright=gatilho,
            erro=f"engine retornou status inválido ({status!r})",
            duracao_s=time.perf_counter() - inicio,
        )
    return ResultadoPagina(
        url=destino,
        url_final=url_final_base or destino,
        ok=status < 400,
        engine="playwright",
        status_http=status,
        gatilho_playwright=gatilho,
        html=html,
        erro=None if status < 400 else f"HTTP {status}",
        duracao_s=time.perf_counter() - inicio,
    )


def _ler_corpo_limitado(resposta: requests.Response) -> tuple[bytes, bool]:
    """Lê o corpo em chunks com teto duro; ``(b"", True)`` se estourou."""
    pedacos: list[bytes] = []
    total = 0
    for pedaco in resposta.iter_content(chunk_size=64 * 1024):
        total += len(pedaco)
        if total > _LIMITE_HTML_BYTES:
            return b"", True
        pedacos.append(pedaco)
    return b"".join(pedacos), False


def _obter_estatico(
    destino: str,
    polidez: Polidez,
    sessao: requests.Session,
    conteudo_presente: Callable[[str], bool] | None,
    inicio: float,
) -> ResultadoPagina:
    """GET com redirects MANUAIS: robots + delay efetivo ANTES de cada hop.

    Cada hop reconsulta robots da PRÓPRIA origem (cache por execução) — um
    redirect que cruza origem só segue se o destino permitir (§9.1). Corpos
    não-HTML não são baixados; HTML lido sob teto de bytes.
    """
    tempo = (polidez.probe_timeout_s, polidez.probe_timeout_s)
    url_atual = destino
    resposta: requests.Response | None = None

    for _salto in range(_MAX_REDIRECTS + 1):
        decisao_hop = robots_para_host(url_atual, polidez, sessao=sessao)
        if not decisao_hop.pode_acessar(url_atual, polidez.user_agent):
            return ResultadoPagina(
                url=destino,
                url_final=url_atual,
                ok=False,
                engine="estatico",
                erro=f"bloqueado por {decisao_hop.origem}/robots.txt",
                bloqueio_robots=True,
                duracao_s=time.perf_counter() - inicio,
            )

        balde = hostname_de(url_atual)
        try:
            _aguardar_delay(balde, _delay_efetivo(polidez, decisao_hop))
            resposta = sessao.get(url_atual, timeout=tempo, allow_redirects=False, stream=True)
            _marcar_pedido(balde)
        except requests.RequestException as exc:
            return ResultadoPagina(
                url=destino,
                url_final=url_atual,
                ok=False,
                engine="estatico",
                erro=f"{type(exc).__name__}: {exc}",
                duracao_s=time.perf_counter() - inicio,
            )

        if resposta.status_code not in _STATUS_REDIRECT:
            break

        location = resposta.headers.get("Location")
        resposta.close()  # redirect não tem corpo útil — libera a conexão
        resposta = None
        if not location:
            return ResultadoPagina(
                url=destino,
                url_final=url_atual,
                ok=False,
                engine="estatico",
                erro=f"redirect de {url_atual} sem cabeçalho Location",
                duracao_s=time.perf_counter() - inicio,
            )
        try:
            url_atual = normalizar_url(urljoin(url_atual, location))
        except ValueError as exc:
            return ResultadoPagina(
                url=destino,
                url_final=url_atual,
                ok=False,
                engine="estatico",
                erro=f"redirect para URL inválida ({location!r}): {exc}",
                duracao_s=time.perf_counter() - inicio,
            )
    else:
        return ResultadoPagina(
            url=destino,
            url_final=url_atual,
            ok=False,
            engine="estatico",
            erro=f"excesso de redirects (>{_MAX_REDIRECTS}) a partir de {destino}",
            duracao_s=time.perf_counter() - inicio,
        )

    assert resposta is not None  # o loop só sai por break COM resposta viva
    status = resposta.status_code
    url_final = url_atual
    tipo_conteudo = (resposta.headers.get("Content-Type") or "").strip().lower()

    try:
        if status >= 400:
            return ResultadoPagina(
                url=destino,
                url_final=url_final,
                ok=False,
                engine="estatico",
                status_http=status,
                erro=f"HTTP {status}",
                duracao_s=time.perf_counter() - inicio,
            )
        if "html" not in tipo_conteudo:
            # patch: corpo NÃO é baixado — evita arrastar PDF/zip por engano
            return ResultadoPagina(
                url=destino,
                url_final=url_final,
                ok=False,
                engine="estatico",
                status_http=status,
                erro=f"conteudo nao-HTML ({tipo_conteudo or 'sem content-type'})",
                duracao_s=time.perf_counter() - inicio,
            )
        corpo, estourou = _ler_corpo_limitado(resposta)
    finally:
        resposta.close()

    if estourou:
        return ResultadoPagina(
            url=destino,
            url_final=url_final,
            ok=False,
            engine="estatico",
            status_http=status,
            erro=f"conteudo excede {_LIMITE_HTML_BYTES} bytes",
            duracao_s=time.perf_counter() - inicio,
        )

    encoding = resposta.encoding or "utf-8"
    try:
        html = corpo.decode(encoding, errors="replace")
    except LookupError:
        html = corpo.decode("utf-8", errors="replace")

    if conteudo_presente is not None and not conteudo_presente(html):
        alternativa = _tentar_playwright(
            destino,
            polidez,
            "conteudo_ausente",
            inicio,
            url_final_base=url_final,
            sessao=sessao,
        )
        return alternativa

    return ResultadoPagina(
        url=destino,
        url_final=url_final,
        ok=True,
        engine="estatico",
        status_http=status,
        html=html,
        duracao_s=time.perf_counter() - inicio,
    )


def obter_html(
    url: str,
    portal: Portal,
    polidez: Polidez,
    *,
    sessao: requests.Session | None = None,
    conteudo_presente: Callable[[str], bool] | None = None,
) -> ResultadoPagina:
    """Obtém uma página: HTTP leve por padrão; Playwright NO GATILHO (AD-5).

    Gatilho duplo (FR-4): portal marcado ``dinamico=true`` (sem tentativa
    estática) OU HTML estático obtido sem o conteúdo-alvo — troca acontece
    UMA única vez e fica sinalizada em ``gatilho_playwright`` para evento.
    robots.txt é consultado/honrado ANTES da requisição a CADA hop (§9.1),
    inclusive na origem final após redirects; delay efetivo inclui Crawl-delay.
    Falhas (rede, browser, bloqueio, não-HTML) viram ``ResultadoPagina(ok=False)``
    — quem decide continuar o lote é o chamador.
    """
    inicio = time.perf_counter()
    try:
        destino = normalizar_url(url)
    except ValueError as exc:
        return ResultadoPagina(
            url=url.strip(),
            url_final=url.strip(),
            ok=False,
            engine="-",
            erro=f"ValueError: {exc}",
            duracao_s=time.perf_counter() - inicio,
        )

    decisao = robots_para_host(destino, polidez, sessao=sessao)
    if not decisao.pode_acessar(destino, polidez.user_agent):
        return ResultadoPagina(
            url=destino,
            url_final=destino,
            ok=False,
            engine="-",
            erro=f"bloqueado por {decisao.origem}/robots.txt",
            bloqueio_robots=True,
            duracao_s=time.perf_counter() - inicio,
        )

    if portal.dinamico:
        return _tentar_playwright(
            destino, polidez, "portal_dinamico", inicio,
            url_final_base=destino, sessao=sessao,
        )

    propria = sessao is None
    if propria:
        sessao = nova_sessao(polidez.user_agent)
    assert sessao is not None
    try:
        return _obter_estatico(destino, polidez, sessao, conteudo_presente, inicio)
    finally:
        if propria:
            sessao.close()


# -- download de documentos (CAP-4): stream + hash em voo + cap + 403 ----------


_CHUNK_DOWNLOAD = 8 * 1024  # 1º chunk ~8 KB: sniff de %PDF- antes de puxar o resto
_MAGICO_PDF = b"%PDF-"

_contadores_403: dict[str, int] = {}
_trava_403 = threading.Lock()


def contagem_403_consecutivos(host: str) -> int:
    """403 seguidos atuais do host (diagnóstico/testes)."""
    with _trava_403:
        return _contadores_403.get(host, 0)


def _registrar_status_download(host: str, status_http: int) -> int:
    """Avança/zera o contador de 403 consecutivos; retorna o valor atual.

    O limite que suspende o host é configurável
    (``[crawl] max_403_consecutivos``, default 3) e vale para o CHAMADOR —
    aqui só se mantém a contagem por host.
    """
    with _trava_403:
        if status_http == 403:
            _contadores_403[host] = _contadores_403.get(host, 0) + 1
        else:
            _contadores_403[host] = 0
        return _contadores_403[host]


@dataclass(slots=True)
class ResultadoDownload:
    """Desfecho de ``baixar_stream`` — o chamador converte em eventos/estado.

    ``hash_sha256`` vem pronto do stream (AD-2: calculado DURANTE a leitura);
    ``excedeu_cap=True`` garante que NADA foi persistido no destino temporário;
    ``conteudo_inesperado`` aborta ANTES de baixar o corpo inteiro (primeiro
    chunk sem ``%PDF-`` e content-type não-PDF).
    """

    url: str
    url_final: str
    ok: bool
    status_http: int | None = None
    hash_sha256: str | None = None
    bytes_baixados: int = 0
    excedeu_cap: bool = False
    conteudo_inesperado: bool = False
    content_type: str | None = None
    estourou_prazo: bool = False
    bloqueio_robots: bool = False
    contador_403: int = 0
    erro: str | None = None
    duracao_s: float = 0.0


def baixar_stream(
    url: str,
    destino_tmp: str | Path,
    polidez: Polidez,
    *,
    max_bytes: int,
    sessao: requests.Session | None = None,
) -> ResultadoDownload:
    """Baixa um documento em stream para ``destino_tmp`` sob polidez integral.

    Mesma disciplina de ``obter_html``: redirects MANUAIS hop-a-hop, robots.txt
    consultado/honrado ANTES de cada requisição (§9.1), delay efetivo por host
    (incluindo Crawl-delay) e Session compartilhável pelo lote. O corpo é lido
    em chunks direto para o arquivo temporário com SHA-256 calculado em voo;
    ultrapassar ``max_bytes`` ou o prazo total (``download_prazo_s``) aborta
    ANTES de completar a gravação (o temporário parcial é removido). O PRIMEIRO
    chunk é inspecionado antes de baixar o resto: sem ``%PDF-`` no início E sem
    content-type PDF, o download é interrompido ali mesmo
    (``conteudo_inesperado=True``). Falhas viram ``ResultadoDownload(ok=False)``
    — quem decide suspender host/continuar o lote é o chamador.
    """
    inicio = time.perf_counter()
    try:
        destino = normalizar_url(url)
    except ValueError as exc:
        return ResultadoDownload(
            url=url.strip(),
            url_final=url.strip(),
            ok=False,
            erro=f"ValueError: {exc}",
            duracao_s=time.perf_counter() - inicio,
        )

    propria = sessao is None
    if propria:
        sessao = nova_sessao(polidez.user_agent)
    assert sessao is not None

    # connect segue o timeout curto do probe; a LEITURA do corpo tem teto próprio
    tempo = (polidez.probe_timeout_s, polidez.download_timeout_s)
    url_atual = destino
    resposta: requests.Response | None = None

    def _falha(url_referencia: str, **campos: object) -> ResultadoDownload:
        return ResultadoDownload(
            url=destino,
            url_final=url_referencia,
            ok=False,
            duracao_s=time.perf_counter() - inicio,
            **campos,  # type: ignore[arg-type]
        )

    try:
        for _salto in range(_MAX_REDIRECTS + 1):
            decisao_hop = robots_para_host(url_atual, polidez, sessao=sessao)
            if not decisao_hop.pode_acessar(url_atual, polidez.user_agent):
                return _falha(
                    url_atual,
                    bloqueio_robots=True,
                    erro=f"bloqueado por {decisao_hop.origem}/robots.txt",
                )

            balde = hostname_de(url_atual)
            try:
                _aguardar_delay(balde, _delay_efetivo(polidez, decisao_hop))
                resposta = sessao.get(
                    url_atual, timeout=tempo, allow_redirects=False, stream=True
                )
                _marcar_pedido(balde)
            except requests.RequestException as exc:
                return _falha(url_atual, erro=f"{type(exc).__name__}: {exc}")

            if resposta.status_code not in _STATUS_REDIRECT:
                # contador avança em TODO hop terminal — inclusive 403 no meio
                # de uma cadeia que até aqui só redirecionou; 3xx é neutro
                # (não incrementa nem zera o streak do host)
                consecutivos = _registrar_status_download(
                    balde, resposta.status_code
                )
                break

            location = resposta.headers.get("Location")
            resposta.close()
            resposta = None
            if not location:
                return _falha(
                    url_atual, erro=f"redirect de {url_atual} sem cabeçalho Location"
                )
            try:
                url_atual = normalizar_url(urljoin(url_atual, location))
            except ValueError as exc:
                return _falha(
                    url_atual,
                    erro=f"redirect para URL inválida ({location!r}): {exc}",
                )
        else:
            return _falha(
                url_atual, erro=f"excesso de redirects (>{_MAX_REDIRECTS}) a partir de {destino}"
            )

        assert resposta is not None  # o loop só sai por break COM resposta viva
        status = resposta.status_code

        if status >= 400:
            resposta.close()
            return _falha(
                url_atual,
                status_http=status,
                contador_403=consecutivos if status == 403 else 0,
                erro=f"HTTP {status}",
            )

        tipo_conteudo = (resposta.headers.get("Content-Type") or "").strip().lower()

        # sniff ANTES de puxar o corpo inteiro (~1º chunk de 8 KB): não-PDF não
        # vira Documento e nem gasta banda/bytes de gravação (I/O matrix); o
        # cap de tamanho continua valendo para o restante da leitura
        hasher = hashlib.sha256()
        total = 0
        estourou = False
        estourou_prazo = False
        caminho_tmp = Path(destino_tmp)
        caminho_tmp.parent.mkdir(parents=True, exist_ok=True)
        prazo = time.monotonic() + polidez.download_prazo_s
        try:
            iterador = resposta.iter_content(chunk_size=_CHUNK_DOWNLOAD)
            primeiro = next(iterador, b"")
            if primeiro and not (
                primeiro.startswith(_MAGICO_PDF) or "pdf" in tipo_conteudo
            ):
                resposta.close()
                return _falha(
                    url_atual,
                    status_http=status,
                    bytes_baixados=len(primeiro),
                    conteudo_inesperado=True,
                    content_type=tipo_conteudo or None,
                    erro=(
                        "conteudo inicial nao-PDF "
                        f"({tipo_conteudo or 'sem content-type'})"
                    ),
                )

            with caminho_tmp.open("wb") as saida:
                for pedaco in itertools.chain((primeiro,), iterador):
                    if not pedaco:
                        continue
                    total += len(pedaco)
                    if total > max_bytes:
                        estourou = True
                        break
                    if time.monotonic() > prazo:
                        estourou_prazo = True
                        break
                    hasher.update(pedaco)
                    saida.write(pedaco)
                if not estourou and not estourou_prazo:
                    saida.flush()
                    os.fsync(saida.fileno())
        except requests.RequestException as exc:
            return _falha(url_atual, status_http=status, erro=f"{type(exc).__name__}: {exc}")
        finally:
            resposta.close()

        if estourou:
            caminho_tmp.unlink(missing_ok=True)
            return _falha(
                url_atual,
                status_http=status,
                bytes_baixados=total,
                excedeu_cap=True,
                content_type=tipo_conteudo or None,
                erro=f"conteudo excede max_bytes ({max_bytes})",
            )
        if estourou_prazo:
            caminho_tmp.unlink(missing_ok=True)
            return _falha(
                url_atual,
                status_http=status,
                bytes_baixados=total,
                estourou_prazo=True,
                content_type=tipo_conteudo or None,
                erro=(
                    f"prazo de download excedido ({polidez.download_prazo_s:g}s "
                    "de download_prazo_s)"
                ),
            )

        return ResultadoDownload(
            url=destino,
            url_final=url_atual,
            ok=True,
            status_http=status,
            hash_sha256=hasher.hexdigest(),
            bytes_baixados=total,
            content_type=tipo_conteudo or None,
            duracao_s=time.perf_counter() - inicio,
        )
    finally:
        if propria:
            sessao.close()


def probe_seed(url: str, polidez: Polidez, sessao: requests.Session | None = None) -> ResultadoSeed:
    """Verifica alcançabilidade de uma seed: HEAD, caindo para GET em 405/501.

    ``sessao`` permite reaproveitar TCP/TLS ao longo de um lote (pré-voo);
    sem ela, uma sessão dedicada é criada e descartada.
    """
    destino = normalizar_url(url)
    host_origem = hostname_de(destino)

    if polidez.probe_respeitar_janela_off_peak and not dentro_da_janela_off_peak(polidez.off_peak):
        raise ViolacaoPolidez(
            f"Fora da janela off-peak ({polidez.off_peak}) no fuso do host; "
            "coleta recusada pela polidez centralizada (AD-5)."
        )

    inicio = time.perf_counter()

    tempo = (polidez.probe_timeout_s, polidez.probe_timeout_s)
    metodo = "HEAD"
    status: int | None = None
    erro: str | None = None
    url_final = destino
    propria = sessao is None
    if propria:
        sessao = nova_sessao(polidez.user_agent)
    assert sessao is not None
    try:
        # delay vale para o hostname SEM porta; redirects caem no mesmo balde
        # quando o host final coincide com o de origem.
        _aguardar_delay(host_origem, polidez.delay_minimo_s)
        resposta = sessao.head(destino, timeout=tempo, allow_redirects=True)
        _marcar_pedido(host_origem)
        if resposta.status_code in _METODOS_COM_HEAD_BLOQUEADO:
            metodo = "GET"
            # o fallback é uma nova requisição ao mesmo host: delay de novo
            _aguardar_delay(host_origem, polidez.delay_minimo_s)
            resposta = sessao.get(destino, timeout=tempo, stream=True)
            _marcar_pedido(host_origem)
            resposta.close()
        status = resposta.status_code
        url_final = str(resposta.url)
        host_final = hostname_de(url_final)
        if host_final and host_final != host_origem:
            # o redirect atingiu OUTRO host: cortesia registrada/aplicada a ele
            _aguardar_delay(host_final, polidez.delay_minimo_s)
            _marcar_pedido(host_final)
    except requests.RequestException as exc:
        erro = f"{type(exc).__name__}: {exc}"
    finally:
        if propria:
            sessao.close()

    duracao = time.perf_counter() - inicio
    ok = status is not None and status < 400
    if not ok and erro is None:
        erro = f"HTTP {status}"
    return ResultadoSeed(
        url=destino,
        ok=ok,
        status_http=status,
        metodo=metodo,
        erro=erro,
        duracao_s=duracao,
    )


def executar_pre_voo(
    seeds: list[str],
    polidez: Polidez,
    *,
    informar_progresso=None,
) -> list[ResultadoSeed]:
    """Sonda cada seed em sequência; falhas NUNCA abortam o lote (FR-2).

    Uma única Session serve todo o lote (reuso de TCP/TLS). Exceção por-seed
    (ex.: URL malformada) vira ``ResultadoSeed(ok=False)`` — só ``ViolacaoPolidez``
    interrompe o lote inteiro, pois é condição global de politude.
    """
    reiniciar_estado_polidez()
    resultados: list[ResultadoSeed] = []
    with nova_sessao(polidez.user_agent) as sessao:
        for indice, seed in enumerate(seeds, start=1):
            try:
                resultado = probe_seed(seed, polidez, sessao=sessao)
            except ViolacaoPolidez:
                raise
            except Exception as exc:  # noqa: BLE001 — seed ruim não derruba o lote
                try:
                    destino = normalizar_url(seed)
                except Exception:  # noqa: BLE001 — nem normalizar dá
                    destino = seed.strip()
                resultado = ResultadoSeed(
                    url=destino,
                    ok=False,
                    status_http=None,
                    metodo="-",
                    erro=f"{type(exc).__name__}: {exc}",
                    duracao_s=0.0,
                )
            resultados.append(resultado)
            if informar_progresso is not None:
                informar_progresso(indice, len(seeds), resultado)
    return resultados
