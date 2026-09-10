"""Descoberta híbrida (CAP-2): navegação por palavras-chave a partir das seeds.

Governado por:
- AD-5: TODA rede acontece VIA ``fetcher.py``; esta camada só orquestra e
  converte resultados em estado/eventos do Manifesto;
- AD-8: identidade por URL normalizada — dedupe intra-portal é imposto pelo
  banco (UNIQUE portal_id+url) e por conjunto em memória dentro da execução,
  pela URL FINAL (aliases de redirect não refetcam nem duplicam);
- §9.1: robots.txt honrado ANTES da primeira requisição ao caminho, decisão
  consultada POR ORIGEM da URL visitada (esquema/porta diferentes consultam
  o próprio robots), com Crawl-delay declarado honrado pelo fetcher.

Classificação de links é SINTÁTICA nesta story (Design Notes):
``.pdf`` ou token sugestivo ('edital(s)'/'chamada(s)') ⇒ candidato a edital;
âncora com palavra-chave de seção ⇒ seção-candidata (enfileirada para
navegação). Juízo fino de "é edital?" pertence às stories de coleta/análise.

Palavras-chave de seção casam INSENSÍVEL a caixa E acento (FR-3), por token
inteiro e sobre whitespace normalizado — "NIT" não casa dentro de "monitoria".
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .fetcher import DecisaoRobots, Polidez, nova_sessao, obter_html, robots_para_host
from .manifest import Manifesto
from .mapa import Portal, hostname_de, normalizar_url

PALAVRAS_CHAVE_SECAO: tuple[str, ...] = (
    "Inovação",
    "PRPGI",
    "PRPPG",
    "NIT",
    "Agência de Inovação",
    "Incubação",
    "Pré-incubação",
    "Aceleração",
    "Financiamento",
    "Editais",
    "Inscrições",
    "Empreendedorismo",
    "Startup",
    "Negócio",
    "Ideação",
)

_SLUGS_CANDIDATO = frozenset({"edital", "editais", "chamada", "chamadas"})
_PREFIXOS_IGNORADOS = ("mailto:", "tel:", "javascript:", "#")
_DIVISOR_TOKEN = re.compile(r"[^a-z0-9]+")


def _sem_acento(texto: str) -> str:
    """NFKD + remoção de diacríticos + casefold — base do matching FR-3."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(
        caractere for caractere in decomposto if not unicodedata.combining(caractere)
    ).casefold()


_PADROES_KEYWORD = tuple(
    re.compile(rf"\b{re.escape(_sem_acento(palavra))}\b") for palavra in PALAVRAS_CHAVE_SECAO
)


def contem_palavra_chave(texto: str) -> bool:
    """Matching insensível a caixa E acento, por token inteiro.

    Whitespace é colapsado DEPOIS da remoção de acentos: quebras de linha e
    espaços múltiplos na âncora não escondem a frase-chave.
    """
    normalizado = " ".join(_sem_acento(texto).split())
    return any(padrao.search(normalizado) for padrao in _PADROES_KEYWORD)


def classificar_link(texto_ancora: str, url: str) -> tuple[str, str] | None:
    """Classifica um link já normalizado.

    Retorna ``('candidato', 'pdf'|'pagina_edital')``, ``('secao', '')`` ou
    ``None`` (link neutro — navegação institucional sem interesse). A ordem é
    deliberada: PDF vence tudo; slug-candidato vence keyword de seção — uma
    âncora genérica ("Editais diversos") apontando para uma chamada concreta
    (``/chamada-2024.html``) é CANDIDATA, não seção navegável; a keyword cobre
    as listagens sem slug ("Editais de Inovação" → ``/inovacao`` = seção).
    Slugs casam por TOKEN do caminho com plurais ('editais', 'chamadas') —
    sem substring acidental ('contrachamada' não é chamada).
    """
    caminho = urlsplit(url).path.lower()
    if caminho.endswith(".pdf"):
        return ("candidato", "pdf")
    tokens = {token for token in _DIVISOR_TOKEN.split(caminho) if token}
    if tokens & _SLUGS_CANDIDATO:
        return ("candidato", "pagina_edital")
    if contem_palavra_chave(texto_ancora):
        return ("secao", "")
    return None


def _host_escopo(host: str) -> str:
    """Host para escopo de coleta: prefixo 'www.' é equivalência, não fronteira."""
    return host[4:] if host.startswith("www.") else host


@dataclass(slots=True)
class ContextoPortal:
    """Portal resolvido contra o Manifesto (id) para uma execução de descoberta."""

    instituicao_sigla: str
    portal: Portal
    portal_id: int


@dataclass(slots=True)
class ResumoPortal:
    """Contagens da navegação de UM portal — vira saída CLI e evento."""

    instituicao_sigla: str
    portal_nome: str
    secoes_visitadas: int = 0
    secoes_ja_conhecidas: int = 0
    secoes_falha: int = 0
    secoes_excedidas: int = 0
    candidatos_novos: int = 0
    candidatos_duplicados: int = 0
    usos_playwright: int = 0
    gatilhos_playwright: dict[str, int] = field(
        default_factory=lambda: {"portal_dinamico": 0, "conteudo_ausente": 0}
    )
    bloqueios_robots: int = 0
    robots_inacessivel: bool = False
    urls_invalidas: int = 0
    links_repetidos: int = 0
    links_fora_do_portal: int = 0
    paginas_baixadas: int = 0
    corte_por_teto: bool = False
    urls_perdidas: list[str] = field(default_factory=list)

    def totalizar(self) -> dict:
        return {
            "secoes_visitadas": self.secoes_visitadas,
            "secoes_ja_conhecidas": self.secoes_ja_conhecidas,
            "secoes_falha": self.secoes_falha,
            "secoes_excedidas": self.secoes_excedidas,
            "candidatos_novos": self.candidatos_novos,
            "candidatos_duplicados": self.candidatos_duplicados,
            "usos_playwright": self.usos_playwright,
            "gatilhos_playwright": dict(self.gatilhos_playwright),
            "bloqueios_robots": self.bloqueios_robots,
            "robots_inacessivel": self.robots_inacessivel,
            "urls_invalidas": self.urls_invalidas,
            "links_repetidos": self.links_repetidos,
            "links_fora_do_portal": self.links_fora_do_portal,
            "paginas_baixadas": self.paginas_baixadas,
            "corte_por_teto": self.corte_por_teto,
            "urls_perdidas": list(self.urls_perdidas),
        }


def navegar_portal(
    contexto: ContextoPortal,
    manifesto: Manifesto,
    polidez: Polidez,
    *,
    comando: str = "descobrir",
    sessao: requests.Session | None = None,
    varredura_id: int | None = None,
) -> ResumoPortal:
    """BFS limitado por ``profundidade_maxima`` e ``max_paginas_por_portal``.

    Idempotente (AD-1): seções já presentes em ``secoes_visitadas`` NÃO são
    revisitadas (regra "nunca revisitar" — inclusive aliases de redirect,
    persistidos pela URL FINAL); candidatos repetidos não duplicam linha.
    Falhas de rede/engine viram eventos e o lote SEGUE — nunca aborta.
    """
    portal = contexto.portal
    rotulo = {"instituicao": contexto.instituicao_sigla, "portal": portal.nome}

    resumo = ResumoPortal(contexto.instituicao_sigla, portal.nome)

    propria = sessao is None
    if propria:
        sessao = nova_sessao(polidez.user_agent)
    assert sessao is not None

    # decisao de robots POR ORIGEM da URL consultada (cache do fetcher evita
    # reconsultas na rede); origens inacessíveis viram um evento cada
    decisoes: dict[str, DecisaoRobots] = {}
    origens_avisadas: set[str] = set()

    def _decisao(url: str) -> DecisaoRobots:
        decisao_url = robots_para_host(url, polidez, sessao=sessao)
        decisoes[decisao_url.origem] = decisao_url
        if not decisao_url.acessivel and decisao_url.origem not in origens_avisadas:
            origens_avisadas.add(decisao_url.origem)
            resumo.robots_inacessivel = True
            manifesto.registrar_evento(
                tipo="robots_inacessivel",
                comando=comando,
                detalhe={
                    **rotulo,
                    "host": hostname_de(url),
                    "origem": decisao_url.origem,
                    "detalhe": decisao_url.detalhe,
                    "decisao": "permitir",
                },
            )
        return decisao_url

    _decisao(portal.url)  # aquece a origem do portal antes do BFS

    try:
        _navegar(
            contexto,
            manifesto,
            polidez,
            resumo,
            _decisao,
            comando,
            sessao,
            varredura_id,
        )
    finally:
        if propria:
            sessao.close()

    manifesto.registrar_evento(
        tipo="descoberta_portal_concluida",
        comando=comando,
        detalhe={**rotulo, **resumo.totalizar()},
    )
    return resumo


def _navegar(
    contexto: ContextoPortal,
    manifesto: Manifesto,
    polidez: Polidez,
    resumo: ResumoPortal,
    _decisao_robots,
    comando: str,
    sessao: requests.Session,
    varredura_id: int | None = None,
) -> None:
    portal = contexto.portal
    host_portal = _host_escopo(hostname_de(portal.url))
    teto_paginas = polidez.max_paginas_por_portal

    def _conteudo_visivel_ok(html: str) -> bool:
        # gatilho sobre TEXTO visível — href contendo keyword não é conteúdo
        return contem_palavra_chave(BeautifulSoup(html, "lxml").get_text(" "))

    fila: deque[tuple[str, int]] = deque((seed, 0) for seed in portal.seeds)
    planejadas: set[str] = set(portal.seeds)

    while fila:
        url, profundidade = fila.popleft()

        # teto de páginas POR PORTAL (politude contra espiral de BFS)
        if resumo.paginas_baixadas >= teto_paginas:
            resumo.corte_por_teto = True
            manifesto.registrar_evento(
                tipo="teto_paginas_atingido",
                comando=comando,
                detalhe={
                    "url_proxima": url,
                    "teto": teto_paginas,
                    "paginas_baixadas": resumo.paginas_baixadas,
                    "pendentes_na_fila": len(fila),
                    "instituicao": resumo.instituicao_sigla,
                    "portal": resumo.portal_nome,
                },
            )
            break

        # regra "nunca revisitar": visita persistida de execuções anteriores
        if manifesto.secao_visitada(contexto.portal_id, url):
            resumo.secoes_ja_conhecidas += 1
            continue

        # robots DA ORIGEM desta URL, ANTES da requisição ao caminho (§9.1)
        decisao_origem = _decisao_robots(url)
        if not decisao_origem.pode_acessar(url, polidez.user_agent):
            resumo.bloqueios_robots += 1
            manifesto.registrar_evento(
                tipo="robots_bloqueio",
                comando=comando,
                detalhe={
                    "url": url,
                    "profundidade": profundidade,
                    "origem": decisao_origem.origem,
                    "instituicao": resumo.instituicao_sigla,
                    "portal": resumo.portal_nome,
                },
            )
            continue

        resumo.paginas_baixadas += 1
        resultado = obter_html(
            url,
            portal,
            polidez,
            sessao=sessao,
            conteudo_presente=_conteudo_visivel_ok,
        )

        if resultado.engine == "playwright":
            resumo.usos_playwright += 1
            gatilho = resultado.gatilho_playwright or "desconhecido"
            resumo.gatilhos_playwright[gatilho] = resumo.gatilhos_playwright.get(gatilho, 0) + 1
            manifesto.registrar_evento(
                tipo="playwright_uso",
                comando=comando,
                detalhe={
                    "url": url,
                    "gatilho": gatilho,
                    "instituicao": resumo.instituicao_sigla,
                    "portal": resumo.portal_nome,
                },
            )

        if resultado.bloqueio_robots:
            # bloqueio aplicado DENTRO do engine (ex.: redirect cruzou origem)
            resumo.bloqueios_robots += 1

        if not resultado.ok:
            resumo.secoes_falha += 1
            resumo.urls_perdidas.append(resultado.url_final or url)
            manifesto.registrar_evento(
                tipo="pagina_falha",
                comando=comando,
                detalhe={
                    "url": url,
                    "url_final": resultado.url_final,
                    "profundidade": profundidade,
                    "engine": resultado.engine,
                    "status_http": resultado.status_http,
                    "erro": resultado.erro,
                    "bloqueio_robots": resultado.bloqueio_robots,
                    "instituicao": resumo.instituicao_sigla,
                    "portal": resumo.portal_nome,
                },
            )
            continue  # lote segue — seção perdida fica registrada p/ retomada

        # persiste pela URL FINAL (e pelo alias pedido): redirects 301 não
        # refetcam na próxima execução; dedupe/consulta ficam estáveis
        manifesto.registrar_secao_visitada(contexto.portal_id, url, profundidade)
        url_final = normalizar_segura(resultado.url_final, url)
        if url_final != url:
            manifesto.registrar_secao_visitada(contexto.portal_id, url_final, profundidade)
            planejadas.add(url_final)
        resumo.secoes_visitadas += 1
        manifesto.registrar_evento(
            tipo="secao_visitada",
            comando=comando,
            detalhe={
                "url": url,
                "url_final": url_final,
                "profundidade": profundidade,
                "engine": resultado.engine,
                "status_http": resultado.status_http,
                "instituicao": resumo.instituicao_sigla,
                "portal": resumo.portal_nome,
            },
        )

        soup = BeautifulSoup(resultado.html or "", "lxml")
        for ancora in soup.find_all("a", href=True):
            bruto = (ancora.get("href") or "").strip()
            if not bruto or bruto.lower().startswith(_PREFIXOS_IGNORADOS):
                continue
            texto_ancora = ancora.get_text(" ", strip=True)
            imagem = ancora.find("img")
            if imagem is not None:
                texto_ancora = f"{texto_ancora} {imagem.get('alt') or ''}".strip()
            absoluta = urljoin(url_final, bruto)  # base correta (URL final)
            try:
                destino = normalizar_url(absoluta)
            except ValueError as exc:
                resumo.urls_invalidas += 1
                manifesto.registrar_evento(
                    tipo="url_invalida",
                    comando=comando,
                    detalhe={
                        "href": bruto,
                        "base": url_final,
                        "erro": str(exc),
                        "instituicao": resumo.instituicao_sigla,
                        "portal": resumo.portal_nome,
                    },
                )
                continue
            if _host_escopo(hostname_de(destino)) != host_portal:
                resumo.links_fora_do_portal += 1  # coleta restrita ao Mapa-Mestre
                continue

            classe = classificar_link(texto_ancora, destino)
            if classe is None:
                continue
            if destino in planejadas:
                resumo.links_repetidos += 1  # matriz: duplicado processado UMA vez
                continue
            planejadas.add(destino)

            if classe[0] == "secao":
                if profundidade + 1 <= portal.profundidade_maxima:
                    fila.append((destino, profundidade + 1))
                else:
                    resumo.secoes_excedidas += 1
                    manifesto.registrar_evento(
                        tipo="secao_profundidade_excedida",
                        comando=comando,
                        detalhe={
                            "url": destino,
                            "profundidade_recusada": profundidade + 1,
                            "profundidade_maxima": portal.profundidade_maxima,
                            "instituicao": resumo.instituicao_sigla,
                            "portal": resumo.portal_nome,
                        },
                    )
                continue

            inserido = manifesto.registrar_candidato(
                contexto.portal_id, destino, classe[1],
                texto_ancora=texto_ancora, varredura_id=varredura_id,
            )
            if inserido:
                resumo.candidatos_novos += 1
                manifesto.registrar_evento(
                    tipo="candidato_encontrado",
                    comando=comando,
                    detalhe={
                        "url": destino,
                        "tipo": classe[1],
                        "origem": url,
                        "profundidade": profundidade,
                        # âncora TRUNCADA só no detalhe do evento (≤200) — o
                        # valor INTEGRAL vive em candidatos.texto_ancora (FR-6)
                        "ancora": texto_ancora[:200],
                        "instituicao": resumo.instituicao_sigla,
                        "portal": resumo.portal_nome,
                    },
                )
            else:
                resumo.candidatos_duplicados += 1


def normalizar_segura(url: str, alternativa: str) -> str:
    """Normaliza caindo para ``alternativa`` se a URL vier estranha."""
    try:
        return normalizar_url(url)
    except ValueError:
        return alternativa
