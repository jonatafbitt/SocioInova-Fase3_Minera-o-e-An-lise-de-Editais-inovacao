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

Toda página obtida passa pela polidez existente: delay por hostname, Session
única compartilhável pelo lote e redirects cobertos.

A janela off-peak é interpretada NO FUSO DO HOST. O probe segue checagem leve
(knob próprio); o CRAWLING obriga a janela quando
``[crawl] respeitar_janela_off_peak`` (default true) — regra que estreia na
Story 2, conforme notas da Story 1.
"""

from __future__ import annotations

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

    delay_minimo_s: float = Field(ge=0)
    off_peak: str
    user_agent: str = Field(min_length=1)
    probe_timeout_s: float = Field(default=10.0, gt=0)
    probe_respeitar_janela_off_peak: bool = False
    # crawling (Story 2 em diante): janela off-peak é obrigatória POR DEFAULT.
    crawl_respeitar_janela_off_peak: bool = True
    # teto de páginas por portal na descoberta — politude contra espiral de BFS.
    max_paginas_por_portal: int = Field(default=200, gt=0)

    @field_validator("off_peak")
    @classmethod
    def _janela_valida(cls, valor: str) -> str:
        _janela_de(valor)  # levanta ErroConfigPolidez se malformada
        return valor


_CHAVES_RAIZ = ("delay_minimo_s", "off_peak", "user_agent")
_CHAVES_PROBE = ("timeout_s", "respeitar_janela_off_peak")
_CHAVES_CRAWL = ("respeitar_janela_off_peak", "max_paginas_por_portal")


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

    desconhecidas = sorted(set(dados) - set(_CHAVES_RAIZ) - {"probe", "crawl"})
    if desconhecidas:
        raise ErroConfigPolidez(
            f"{caminho}: chaves desconhecidas na raiz: {', '.join(desconhecidas)}; "
            f"aceitas: {', '.join(_CHAVES_RAIZ)}, [probe], [crawl]"
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
    """Limpa registro de último pedido e cache de robots.txt (uso em testes)."""
    with _trava_delay:
        _ultimo_pedido_por_host.clear()
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
