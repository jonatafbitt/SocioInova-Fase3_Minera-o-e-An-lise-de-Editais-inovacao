"""Varredura sob demanda de páginas de editais a partir de URLs coladas.

Story de varredura: o usuário cola a URL de uma página de editais e o agente
navega EXATAMENTE aquela página (BFS dentro do host do portal ligado).

Reusa a descoberta (CAP-2) INTEGRAL via ``navegar_portal(... comando='varredura')``
— robots.txt mandatório (FR-2 skew §9.1), polidez off-peak, dedupe por URL e
registro de candidatos/seções são OS MESMOS. O que muda:

- a URL colada vira um Portal SINTÉTICO em memória (url/seeds = URL colada;
  os demais campos são herdados do portal ligado no Mapa-Mestre) — o banco
  NÃO ganha linha nova de instituição/portal (Ask-First da story: varredura
  nunca auto-registra portal novo);
- o portal de destino é RESOLVIDO por HOSTNAME da URL contra a colada —
  funciona para URLs profundas sob o host do portal;
- re-execução é IDEMPOTENTE (AD-1): seções já visitadas viram
  ``secoes_ja_conhecidas`` e a varredura chega a ``concluida`` mesmo sem
  novos achados;
- a régua da janela off-peak é do CLI: fora da janela a varredura segue
  ``pendente`` e nenhuma rede é tocada (Ask-First).
"""

from __future__ import annotations

import sqlite3

import requests

from .descoberta import ContextoPortal, ResumoPortal, _host_escopo, navegar_portal
from .fetcher import Polidez
from .manifest import Manifesto
from .mapa import Instituicao, MapaMestre, Portal, hostname_de


def host_escopo_de(url: str) -> str:
    """Host de escopo (www. é equivalência — padrão da descoberta, FR-3)."""
    return _host_escopo(hostname_de(url))


class ErroVarredura(ValueError):
    """URL colada irresolvível ou AMBÍGUA no Mapa-Mestre (Ask-First)."""


def resolver_portal_por_url(
    mapa: MapaMestre, url_normalizada: str
) -> tuple[Instituicao, Portal] | None:
    """Resolve o portal do Mapa-Mestre dono do host da URL colada.

    Compara por host de escopo contra ``url``/``seeds`` do portal — permite
    colar páginas profundas (URLs sob o mesmo host) e trata ``www.`` como
    equivalência. Retorna ``None`` se nenhum portal hospeda o host (Ask-First:
    NÃO se auto-registra; o CLI recusa listando o que existe no mapa). Se MAIS
    de um portal reivindica o MESMO host de escopo, a colagem é ambígua — levanta
    ``ErroVarredura`` com os candidatos, em vez de "o primeiro vence" silencioso.
    """
    alvo = host_escopo_de(url_normalizada)
    candidatos: list[tuple[Instituicao, Portal]] = []
    for instituicao in mapa.instituicao:
        for portal in instituicao.portal:
            hosts = {host_escopo_de(portal.url)}
            hosts.update(host_escopo_de(seed) for seed in portal.seeds)
            if alvo in hosts:
                candidatos.append((instituicao, portal))
    if len(candidatos) > 1:
        nomes = "; ".join(
            f"[{instituicao.sigla}] {portal.nome} ({portal.url})"
            for instituicao, portal in candidatos
        )
        raise ErroVarredura(
            f"host '{alvo}' é reivindicado por mais de um portal no "
            f"Mapa-Mestre — colagem ambígua ({nomes}). Resolva o mapa antes."
        )
    return candidatos[0] if candidatos else None


def portal_sintetico(linha_varredura: sqlite3.Row, url_normalizada: str) -> Portal:
    """Portal em MEMÓRIA da varredura: URL colada como url/seeds, demais
    campos herdados do portal ligado (linha de ``varredura_por_id``).

    Nunca persiste: o contexto dela é o ``portal_id`` do portal ligado; nada
    de novo vai para ``instituicoes``/``portais`` (Ask-First/AD-11).
    """
    return Portal(
        nome=linha_varredura["portal_nome"],
        categoria=linha_varredura["portal_categoria"],
        url=url_normalizada,
        seeds=[url_normalizada],
        dinamico=bool(linha_varredura["portal_dinamico"]),
        profundidade_maxima=int(linha_varredura["portal_profundidade_maxima"]),
    )


def rodar_varredura(
    linha_varredura: sqlite3.Row,
    manifesto: Manifesto,
    polidez: Polidez,
    *,
    comando: str = "varredura",
    sessao: requests.Session | None = None,
) -> ResumoPortal:
    """Executa a navegação da URL colada (candidatos/seções como na CAP-2).

    Retorna ``ResumoPortal`` — ``secoes_ja_conhecidas`` sinaliza a execução
    idempotente de uma página já visitada. Robôs/janela/dedupe são do fetcher.
    """
    url = linha_varredura["url"]
    portal = portal_sintetico(linha_varredura, url)
    contexto = ContextoPortal(
        linha_varredura["instituicao_sigla"], portal, linha_varredura["portal_id"]
    )
    return navegar_portal(
        contexto,
        manifesto,
        polidez,
        comando=comando,
        sessao=sessao,
        varredura_id=int(linha_varredura["id"]),
    )
