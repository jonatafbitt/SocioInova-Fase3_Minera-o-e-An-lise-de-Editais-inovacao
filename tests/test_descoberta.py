"""Cenários do I/O Matrix para a descoberta híbrida (CAP-2).

Todos os cenários rodam contra o servidor fake local (convenção §Testes),
incluindo robots.txt servido pelo próprio fake e simulação de rede morrendo
no meio da navegação. Nenhum teste acessa a rede real nem lança navegador.
"""

from __future__ import annotations

import json

import pytest

from agente_editais.consulta import app
from agente_editais.descoberta import (
    _host_escopo,
    classificar_link,
    contem_palavra_chave,
)
from agente_editais.manifest import Manifesto

from .conftest import escrever_mapa, url_do


def _mapa_portal(
    servidor,
    *,
    sigla: str = "TST",
    seeds: list[str] | None = None,
    extra: str = "",
) -> str:
    sementes = ", ".join(f'"{seed}"' for seed in (seeds or [url_do(servidor)]))
    return (
        f'[[instituicao]]\nsigla = "{sigla}"\nnome = "Instituto de Teste {sigla}"\n\n'
        f'  [[instituicao.portal]]\n  nome = "Portal {sigla}"\n'
        f'  categoria = "integra"\n  url = "{url_do(servidor)}"\n'
        f"  seeds = [{sementes}]\n{extra}"
    )


def _tipos_eventos(manifesto_caminho) -> list[tuple[str, dict]]:
    with Manifesto(manifesto_caminho) as manifesto:
        return [
            (linha["tipo"], json.loads(linha["detalhe"]))
            for linha in manifesto.consultar("SELECT tipo, detalhe FROM eventos ORDER BY id")
        ]


# -- unidades puras: matching FR-3 e classificação sintática -----------------------


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Núcleo de INOVAÇÃO", True),  # caixa E acento insensíveis
        ("agencia de inovacao", True),
        ("Coordenação de PRPGI", True),
        ("prppg", True),
        ("Comitê local de NIT", True),
        ("AGÊNCIA DE INOVAÇÃO", True),
        ("agencia   de\ninovacao", True),  # whitespace colapsado APÓS tirar acento
        ("monitoria", False),  # 'nit' não casa DENTRO de palavra (token inteiro)
        ("inovações populares", False),  # plural não é o token da lista
        ("pesquisa básica", False),
    ],
)
def test_contem_palavra_chave_insensivel_a_caixa_e_acento(texto, esperado) -> None:
    assert contem_palavra_chave(texto) is esperado


@pytest.mark.parametrize(
    ("texto_ancora", "url", "esperado"),
    [
        ("Edital 12/2024", "http://x.org/docs/e.pdf", ("candidato", "pdf")),
        ("chamada", "http://x.org/a/b.PDF", ("candidato", "pdf")),  # extensão vence tudo
        ("Editais diversos", "http://x.org/chamada-2024.html", ("candidato", "pagina_edital")),
        ("Editais diversos", "http://x.org/chamadas/abertas.html", ("candidato", "pagina_edital")),
        ("Lista", "http://x.org/editais/", ("candidato", "pagina_edital")),  # plural por token
        # token inteiro: substring acidental não é slug
        ("Contrachamada", "http://x.org/a/contrachamada.html", None),
        ("Editais de Inovação", "http://x.org/inovacao", ("secao", "")),
        ("Pesquisa", "http://x.org/pesquisa", None),
        ("", "http://x.org/sobre/o-instituto", None),
    ],
)
def test_classificar_link_e_sintatico(texto_ancora, url, esperado) -> None:
    assert classificar_link(texto_ancora, url) == esperado


@pytest.mark.parametrize(
    ("host", "esperado"),
    [
        ("www.example.org", "example.org"),
        ("example.org", "example.org"),
        ("www2.example.org", "www2.example.org"),  # só o prefixo exato 'www.'
    ],
)
def test_escopo_de_host_normaliza_prefixo_www(host, esperado) -> None:
    assert _host_escopo(host) == esperado


# -- AC1: fluxo completo com seção + PDF relativo + dedupe na segunda execução ------


def test_descobrir_persiste_secoes_e_candidato_pdf_e_dedupe_na_segunda_execucao(
    cli, politeness_veloz, servidor_fake
) -> None:
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body>"
        "<a href='/inovacao'>Núcleo de INOVAÇÃO</a>"
        "<a href='pdfs/edital-relativo.pdf'>Documento</a>"
        "</body></html>",
    )
    servidor_fake.paginas["/inovacao"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body>"
        "<a href='../pdfs/agente-inovacao-junior.pdf'>Agente de Inovação Júnior</a>"
        "<a href='../pdfs/agente-inovacao-junior.pdf'>Mesmo arquivo repetido</a>"
        "<a href='/contato'>Contato</a>"
        "</body></html>",
    )
    servidor_fake.paginas["/pdfs/edital-relativo.pdf"] = (200, "application/pdf", b"%PDF-fake")
    servidor_fake.paginas["/pdfs/agente-inovacao-junior.pdf"] = (200, "application/pdf", b"%PDF-fake")
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    primeira = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert primeira.exit_code == 0, primeira.output
    saida = primeira.output + (primeira.stderr or "")
    assert "[TST] Portal TST" in saida
    assert "2 visitadas" in saida  # seed (profundidade 0) + /inovacao (profundidade 1)
    assert "2 novos" in saida  # os dois PDFs distintos; o repetido foi ignorado

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        secoes = {
            linha["url"]: linha["profundidade"]
            for linha in manifesto.consultar(
                "SELECT url, profundidade FROM secoes_visitadas ORDER BY url"
            )
        }
        assert secoes == {url_do(servidor_fake): 0, url_do(servidor_fake, "/inovacao"): 1}
        candidatos = {
            linha["url"]: linha["tipo"]
            for linha in manifesto.consultar("SELECT url, tipo FROM candidatos")
        }
        assert candidatos == {
            url_do(servidor_fake, "/pdfs/agente-inovacao-junior.pdf"): "pdf",
            url_do(servidor_fake, "/pdfs/edital-relativo.pdf"): "pdf",
        }

    tipos = [tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert "secao_visitada" in tipos
    assert "candidato_encontrado" in tipos
    assert "descoberta_portal_concluida" in tipos
    assert "descobrir_concluido" in tipos

    pedidos_pagina_antes = [
        registro["caminho"]
        for registro in servidor_fake.registros
        if registro["metodo"] == "GET"
    ]

    segunda = cli.invoke(app, ["descobrir", "--todos"])

    assert segunda.exit_code == 0, segunda.output
    saida2 = segunda.output + (segunda.stderr or "")
    assert "0 visitadas" in saida2
    assert "1 já conhecidas" in saida2  # a seed; /inovacao só seria tocada via seed
    assert "0 novos" in saida2

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        assert manifesto.contar_secoes_visitadas() == 2, "nunca revisitar"
        assert manifesto.contar_candidatos() == 2, "dedupe por URL normalizada"

    pedidos_pagina_depois = [
        registro["caminho"]
        for registro in servidor_fake.registros
        if registro["metodo"] == "GET"
    ]
    assert pedidos_pagina_depois[: len(pedidos_pagina_antes)] == pedidos_pagina_antes


# -- AC2: portal dinamico=true vai direto ao Playwright ------------------------------


def test_portal_dinamico_usa_playwright_sem_tentativa_estatica(
    cli, politeness_veloz, servidor_fake, monkeypatch
) -> None:
    chamadas: list[str] = []

    def _engine(url, *, user_agent, timeout_s):
        chamadas.append(url)
        return (
            200,
            "<html><body><a href='/renderizado/edital-x.pdf'>edital renderizado</a></body></html>",
        )

    monkeypatch.setattr("agente_editais.fetcher._obter_com_playwright", _engine)
    escrever_mapa(
        politeness_veloz,
        _mapa_portal(servidor_fake, extra="  dinamico = true\n"),
    )
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "Playwright: 1 uso(s)" in saida
    assert "portal_dinamico=1" in saida
    assert "/ok" not in [
        registro["caminho"] for registro in servidor_fake.registros
    ], "nenhuma tentativa estática"
    assert chamadas == [url_do(servidor_fake)]

    eventos = _tipos_eventos(politeness_veloz.manifesto)
    usos = [detalhe for tipo, detalhe in eventos if tipo == "playwright_uso"]
    assert len(usos) == 1
    assert usos[0]["gatilho"] == "portal_dinamico"

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        assert manifesto.contar_candidatos() == 1, "candidato veio do HTML renderizado"


# -- AC3: estático sem conteúdo-alvo ⇒ exatamente UMA troca ---------------------------


def test_estatico_sem_conteudo_alvo_troca_para_playwright_uma_unica_vez(
    cli, politeness_veloz, servidor_fake, monkeypatch
) -> None:
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><p>Instituto Federal de Teste</p></body></html>",
    )
    chamadas: list[str] = []

    def _engine(url, *, user_agent, timeout_s):
        chamadas.append(url)
        return (
            200,
            "<html><body><a href='/js/edital-y.pdf'>edital pós-render</a>"
            "<span>Inovação</span></body></html>",
        )

    monkeypatch.setattr("agente_editais.fetcher._obter_com_playwright", _engine)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "conteudo_ausente=1" in saida
    assert len(chamadas) == 1, "exatamente UMA troca para Playwright"
    eventos = _tipos_eventos(politeness_veloz.manifesto)
    usos = [detalhe for tipo, detalhe in eventos if tipo == "playwright_uso"]
    assert len(usos) == 1 and usos[0]["gatilho"] == "conteudo_ausente"


# -- AC4: robots.txt proíbe o caminho ⇒ requisição NEM acontece ------------------------


def test_robots_bloqueio_pula_o_caminho_antes_da_requisicao(
    cli, politeness_veloz, servidor_fake
) -> None:
    servidor_fake.robots_txt = "User-agent: *\nDisallow: /privado/\n"
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body>"
        "<a href='/privado/secao'>Área de Inovação restrita</a>"
        "<a href='/livre.pdf'>documento livre</a>"
        "</body></html>",
    )
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "robots.txt: 1 bloqueio(s)" in saida
    caminhos = [registro["caminho"] for registro in servidor_fake.registros]
    assert "/privado/secao" not in caminhos, "requisição NEM acontece"

    eventos = _tipos_eventos(politeness_veloz.manifesto)
    bloqueios = [detalhe for tipo, detalhe in eventos if tipo == "robots_bloqueio"]
    assert len(bloqueios) == 1
    assert bloqueios[0]["url"].endswith("/privado/secao")
    assert any(tipo == "candidato_encontrado" for tipo, _ in eventos), "lote segue após bloqueio"


def test_robots_por_origem_da_url_consultada(cli, politeness_veloz, servidor_fake, criar_servidor_fake):
    """Mesmo hostname, porta diferente = origem DIFERENTE — consulta o próprio robots."""
    outro = criar_servidor_fake()
    outro.robots_txt = "User-agent: *\nDisallow: /\n"
    porta_outro = outro.server_address[1]

    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body>"
        f"<a href='http://127.0.0.1:{porta_outro}/secao-x'>Núcleo de Inovação externo</a>"
        "</body></html>",
    )
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    caminhos_outro = [registro["caminho"] for registro in outro.registros]
    assert "/secao-x" not in caminhos_outro, "origem proibida: requisição NEM acontece"
    assert "/robots.txt" in caminhos_outro, "robots da OUTRA origem foi consultado"
    eventos = _tipos_eventos(politeness_veloz.manifesto)
    bloqueios = [detalhe for tipo, detalhe in eventos if tipo == "robots_bloqueio"]
    assert len(bloqueios) == 1
    assert bloqueios[0]["url"].endswith("/secao-x")
    assert f":{porta_outro}" in bloqueios[0]["origem"], "decisão é da origem da URL"


# -- AC5: rede morre no meio da navegação ⇒ perdas viram eventos, exit 0 ----------------


def test_rede_morrendo_no_meio_registra_perda_e_termina_exit0(
    cli, politeness_veloz, servidor_fake
) -> None:
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><a href='/secundaria'>Programa de Inovação</a></body></html>",
    )
    servidor_fake.aborts_conexao.add("/secundaria")  # conexão morre SEM resposta
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "1 falhas" in saida
    assert "lote não abortado" in saida
    eventos = _tipos_eventos(politeness_veloz.manifesto)
    falhas = [detalhe for tipo, detalhe in eventos if tipo == "pagina_falha"]
    assert len(falhas) == 1
    assert falhas[0]["url"].endswith("/secundaria")
    assert falhas[0]["profundidade"] == 1
    assert falhas[0]["erro"], "erro deve ser registrado para custódia"

    saida = resultado.output + (resultado.stderr or "")
    assert "Perdida:" in saida and "/secundaria" in saida, "urls_perdidas sai na CLI"
    conclusoes = [detalhe for tipo, detalhe in eventos if tipo == "descobrir_concluido"]
    assert any("/secundaria" in u for u in conclusoes[-1]["urls_perdidas"])


# -- HTTP ≥400 no meio da navegação: perda registrada, NÃO persiste como visita --------


def test_http_404_no_meio_da_navegacao_vira_falha_sem_persistir_visita(
    cli, politeness_veloz, servidor_fake
) -> None:
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><a href='/quebrada'>Programa de Inovação</a></body></html>",
    )
    servidor_fake.paginas["/quebrada"] = (404, "text/html; charset=utf-8", "<html><body>sumiu</body></html>")
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "1 falhas" in saida
    eventos = _tipos_eventos(politeness_veloz.manifesto)
    falhas = [detalhe for tipo, detalhe in eventos if tipo == "pagina_falha"]
    assert len(falhas) == 1
    assert falhas[0]["status_http"] == 404
    assert falhas[0]["erro"] == "HTTP 404"
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        urls_visitadas = [
            linha["url"] for linha in manifesto.consultar("SELECT url FROM secoes_visitadas")
        ]
    assert not any(u.endswith("/quebrada") for u in urls_visitadas), (
        "página com HTTP ≥400 NÃO persiste em secoes_visitadas"
    )


# -- URLs relativas resolvidas contra a base correta; inválidas viram evento ------------


def test_urls_relativas_resolvidas_e_invalidas_ignoradas_com_evento(
    cli, politeness_veloz, servidor_fake
) -> None:
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><span>Coordenação de Inovação</span>"
        "<a href='docs/relativo.pdf'>relativo</a>"
        "<a href='http://if.teste:porta-ruim/x'>quebrado</a>"
        "<a href='mailto:pessoa@if.teste'>email</a>"
        "</body></html>",
    )
    servidor_fake.paginas["/docs/relativo.pdf"] = (200, "application/pdf", b"%PDF-fake")
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        urls = [linha["url"] for linha in manifesto.consultar("SELECT url FROM candidatos")]
        assert urls == [url_do(servidor_fake, "/docs/relativo.pdf")], (
            "URL relativa resolvida contra a base correta e normalizada"
        )
    eventos = _tipos_eventos(politeness_veloz.manifesto)
    invalidas = [detalhe for tipo, detalhe in eventos if tipo == "url_invalida"]
    assert len(invalidas) == 1
    assert invalidas[0]["href"] == "http://if.teste:porta-ruim/x"
    # paridade com os eventos irmãos: rótulo de custódia presente
    assert invalidas[0]["instituicao"] == "TST"
    assert invalidas[0]["portal"] == "Portal TST"


# -- limite de profundidade --------------------------------------------------------------


def test_profundidade_maxima_limita_a_navegacao(cli, politeness_veloz, servidor_fake) -> None:
    def _pagina(proximo: str | None) -> tuple[int, str, str]:
        link = f"<a href='{proximo}'>Inovação nível seguinte</a>" if proximo else ""
        return (200, "text/html; charset=utf-8", f"<html><body>{link}</body></html>")

    servidor_fake.paginas["/ok"] = _pagina("/nivel1")
    servidor_fake.paginas["/nivel1"] = _pagina("/nivel2")
    servidor_fake.paginas["/nivel2"] = _pagina("/nivel3")
    servidor_fake.paginas["/nivel3"] = _pagina(None)
    escrever_mapa(
        politeness_veloz,
        _mapa_portal(servidor_fake, extra="  profundidade_maxima = 2\n"),
    )
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    caminhos = {registro["caminho"] for registro in servidor_fake.registros}
    assert "/nivel1" in caminhos and "/nivel2" in caminhos
    assert "/nivel3" not in caminhos, "profundidade 3 > máxima 2: não visita"
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        profundidades = {
            linha["url"]: linha["profundidade"]
            for linha in manifesto.consultar("SELECT url, profundidade FROM secoes_visitadas")
        }
        assert profundidades[url_do(servidor_fake, "/nivel2")] == 2


# -- robots.txt inacessível durante a descoberta ⇒ decisão registrada ---------------------


def test_robots_inacessivel_registra_decisao_e_segue(cli, politeness_veloz, servidor_fake) -> None:
    servidor_fake.paginas["/robots.txt"] = (500, "text/plain", "boom")
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><p>Portal da Agência de Inovação</p></body></html>",
    )
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "inacessível — permitido com evento" in saida
    eventos = _tipos_eventos(politeness_veloz.manifesto)
    decisoes = [detalhe for tipo, detalhe in eventos if tipo == "robots_inacessivel"]
    assert len(decisoes) == 1
    assert decisoes[0]["decisao"] == "permitir"
    assert "HTTP 500" in decisoes[0]["detalhe"]
    assert any(tipo == "secao_visitada" for tipo, _ in eventos)


# -- redirect 301: candidato resolve contra a URL FINAL e alias não refetcha -----------


def test_redirect_301_resolve_links_contra_url_final_e_alias_persiste(
    cli, politeness_veloz, servidor_fake
) -> None:
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><p>Agência de Inovação</p>"
        "<a href='docs/x.pdf'>documento</a></body></html>",
    )
    servidor_fake.paginas["/docs/x.pdf"] = (200, "application/pdf", b"%PDF-fake")
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake, seeds=[url_do(servidor_fake, "/movido")]))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    primeira = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert primeira.exit_code == 0, primeira.output
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        candidatos = [
            linha["url"] for linha in manifesto.consultar("SELECT url FROM candidatos")
        ]
        secoes = {
            linha["url"]: linha["profundidade"]
            for linha in manifesto.consultar("SELECT url, profundidade FROM secoes_visitadas")
        }
    assert candidatos == [url_do(servidor_fake, "/docs/x.pdf")], (
        "href relativo resolvido contra a URL FINAL do redirect"
    )
    assert secoes.get(url_do(servidor_fake, "/movido")) == 0
    assert secoes.get(url_do(servidor_fake, "/ok")) == 0, "URL final também persistida"

    gets_movido_antes = sum(
        1
        for registro in servidor_fake.registros
        if registro["metodo"] == "GET" and registro["caminho"] == "/movido"
    )
    segunda = cli.invoke(app, ["descobrir", "--todos"])
    gets_movido_depois = sum(
        1
        for registro in servidor_fake.registros
        if registro["metodo"] == "GET" and registro["caminho"] == "/movido"
    )

    assert segunda.exit_code == 0, segunda.output
    assert gets_movido_depois == gets_movido_antes == 1, "alias 301 não refetcha"


# -- âncora via atributo alt de imagem --------------------------------------------------


def test_ancora_considera_alt_de_imagem_dentro_do_link(cli, politeness_veloz, servidor_fake):
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><p>Portal de Inovação</p>"
        "<a href='/setor'><img src='logo.png' alt='Agência de Inovação'></a>"
        "</body></html>",
    )
    servidor_fake.paginas["/setor"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><p>Setor de Inovação</p></body></html>",
    )
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    caminhos = [registro["caminho"] for registro in servidor_fake.registros]
    assert "/setor" in caminhos, "alt da imagem conta como texto da âncora"


# -- gatilho de conteúdo avalia TEXTO visível, não HTML cru -----------------------------


def test_gatilho_de_conteudo_usa_texto_visivel_nao_html_cru(
    cli, politeness_veloz, servidor_fake, monkeypatch
) -> None:
    # href contém 'agente-inovacao.pdf' mas NÃO há keyword no texto visível:
    # o HTML cru conteria 'inovacao' — o gatilho correto é disparar mesmo assim
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><a href='docs/agente-inovacao.pdf'>link</a></body></html>",
    )
    chamadas: list[str] = []

    def _engine(url, *, user_agent, timeout_s):
        chamadas.append(url)
        return 200, "<html><body>conteudo renderizado</body></html>"

    monkeypatch.setattr("agente_editais.fetcher._obter_com_playwright", _engine)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert len(chamadas) == 1, "keyword só em href não é conteúdo: troca para Playwright"
    eventos = _tipos_eventos(politeness_veloz.manifesto)
    usos = [detalhe for tipo, detalhe in eventos if tipo == "playwright_uso"]
    assert len(usos) == 1 and usos[0]["gatilho"] == "conteudo_ausente"


# -- teto de páginas por portal ([crawl] max_paginas_por_portal) -------------------------


def test_teto_de_paginas_para_o_bfs_e_registra_evento(cli, politeness_veloz, servidor_fake):
    politeness_path = politeness_veloz.configs / "politeness.toml"
    politeness_path.write_text(
        'delay_minimo_s = 0.0\noff_peak = "22:00-06:00"\nuser_agent = "ua/1"\n'
        "[crawl]\nrespeitar_janela_off_peak = false\nmax_paginas_por_portal = 1\n"
        "[probe]\ntimeout_s = 5\nrespeitar_janela_off_peak = false\n",
        encoding="utf-8",
    )
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><a href='/nivel1'>Inovação nível 1</a></body></html>",
    )
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "[TETO ATINGIDO]" in saida
    caminhos = [registro["caminho"] for registro in servidor_fake.registros]
    assert "/nivel1" not in caminhos, "BFS para ao atingir o teto"
    eventos = _tipos_eventos(politeness_veloz.manifesto)
    tetos = [detalhe for tipo, detalhe in eventos if tipo == "teto_paginas_atingido"]
    assert len(tetos) == 1
    assert tetos[0]["teto"] == 1
    assert any("/nivel1" in pendente for pendente in [tetos[0]["url_proxima"]])


# -- seção além da profundidade máxima vira evento dedicado ------------------------------


def test_secao_alem_da_profundidade_maxima_vira_evento(cli, politeness_veloz, servidor_fake):
    def _pagina(proximo: str | None) -> tuple[int, str, str]:
        link = f"<a href='{proximo}'>Inovação seguinte</a>" if proximo else ""
        return (200, "text/html; charset=utf-8", f"<html><body>{link}</body></html>")

    servidor_fake.paginas["/ok"] = _pagina("/nivel1")
    servidor_fake.paginas["/nivel1"] = _pagina("/nivel2")
    escrever_mapa(
        politeness_veloz,
        _mapa_portal(servidor_fake, extra="  profundidade_maxima = 1\n"),
    )
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "1 além da profundidade" in saida
    caminhos = {registro["caminho"] for registro in servidor_fake.registros}
    assert "/nivel2" not in caminhos
    eventos = _tipos_eventos(politeness_veloz.manifesto)
    excedidas = [detalhe for tipo, detalhe in eventos if tipo == "secao_profundidade_excedida"]
    assert len(excedidas) == 1
    assert excedidas[0]["profundidade_recusada"] == 2
    assert excedidas[0]["profundidade_maxima"] == 1


# -- superfície CLI: flags, siglas, sincronização e janela off-peak ------------------------


def test_flags_mutuamente_exclusivas_e_obrigatorias(cli, politeness_veloz) -> None:
    assert cli.invoke(app, ["descobrir"]).exit_code == 2
    assert cli.invoke(app, ["descobrir", "--portal", "TST", "--todos"]).exit_code == 2


def test_portal_vazio_e_flag_malformada_exit_2(cli, politeness_veloz) -> None:
    resultado = cli.invoke(app, ["descobrir", "--portal", ""])
    assert resultado.exit_code == 2
    saida = resultado.output + (resultado.stderr or "")
    assert "não vazia" in saida


def test_sigla_e_case_insensitive(cli, politeness_veloz, servidor_fake) -> None:
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--portal", "tst"])

    assert resultado.exit_code == 0, resultado.output
    assert "[TST] Portal TST" in resultado.output


def test_sigla_desconhecida_sai_1_listando_siglas(cli, politeness_veloz, servidor_fake) -> None:
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    resultado = cli.invoke(app, ["descobrir", "--portal", "XXX"])
    assert resultado.exit_code == 1
    saida = resultado.output + (resultado.stderr or "")
    assert "XXX" in saida and "TST" in saida


def test_portal_fora_do_manifesto_pedemapa_validar(
    cli, politeness_veloz, servidor_fake
) -> None:
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    # sem 'mapa validar': Manifesto recém-criado não conhece o portal
    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])
    assert resultado.exit_code == 1
    saida = resultado.output + (resultado.stderr or "")
    assert "mapa validar" in saida


def test_fora_da_janela_off_peak_recusa_crawling_sem_requisicoes(
    cli, politeness_veloz, servidor_fake, monkeypatch
) -> None:
    from agente_editais import consulta

    # politeness sem [crawl] ⇒ default TRUE: crawling obriga a janela
    politeness_path = politeness_veloz.configs / "politeness.toml"
    politeness_path.write_text(
        'delay_minimo_s = 0.0\noff_peak = "22:00-06:00"\nuser_agent = "ua/1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(consulta, "dentro_da_janela_off_peak", lambda *_a, **_k: False)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))

    resultado = cli.invoke(app, ["descobrir", "--portal", "TST"])

    assert resultado.exit_code == 1
    saida = resultado.output + (resultado.stderr or "")
    assert "off-peak" in saida.lower()
    assert servidor_fake.registros == [], "nada é requisitado fora da janela"


def test_polidez_configurada_livre_roda_fora_da_janela(
    cli, politeness_veloz, servidor_fake, monkeypatch
) -> None:
    from agente_editais import consulta

    monkeypatch.setattr(consulta, "dentro_da_janela_off_peak", lambda *_a, **_k: False)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["descobrir", "--todos"])

    assert resultado.exit_code == 0, resultado.output


def test_revisitar_esquece_secoes_e_navega_de_novo_com_evento(
    cli, politeness_veloz, servidor_fake
) -> None:
    """--revisitar limpa secoes_visitadas do portal (via Manifesto, com evento)."""
    servidor_fake.paginas["/ok"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><a href='/inovacao'>Núcleo de INOVAÇÃO</a></body></html>",
    )
    servidor_fake.paginas["/inovacao"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><a href='pdfs/e.pdf'>Edital</a></body></html>",
    )
    servidor_fake.paginas["/pdfs/e.pdf"] = (200, "application/pdf", b"%PDF-fake")
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    primeira = cli.invoke(app, ["descobrir", "--portal", "TST"])
    assert primeira.exit_code == 0, primeira.output
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        assert manifesto.contar_secoes_visitadas() == 2

    gets_antes = sum(1 for r in servidor_fake.registros if r["metodo"] == "GET")

    revisita = cli.invoke(app, ["descobrir", "--portal", "TST", "--revisitar"])

    assert revisita.exit_code == 0, revisita.output
    assert "esquecidas" in revisita.output
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        # navegação recomeçou: as mesmas 2 seções voltaram ao registro
        assert manifesto.contar_secoes_visitadas() == 2
    tipos = [t for t, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert "secoes_reiniciadas" in tipos

    gets_depois = sum(1 for r in servidor_fake.registros if r["metodo"] == "GET")
    assert gets_depois > gets_antes  # houve re-navegação real
