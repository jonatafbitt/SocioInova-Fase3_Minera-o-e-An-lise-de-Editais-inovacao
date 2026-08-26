"""Cenários do I/O Matrix para obter_html/robots (CAP-2, AD-5).

Polidez testada contra servidor HTTP local fake (convenção §Testes).
Nenhum teste acessa a rede real nem lança navegador: o engine Playwright é
monkeypatched (Design Notes da story — download de browsers é pesado/frágil
em CI; smoke manual com browser real é verificação opcional no README).
"""

from __future__ import annotations

import pytest

from agente_editais import fetcher
from agente_editais.fetcher import (
    DecisaoRobots,
    ErroConfigPolidez,
    Polidez,
    carregar_polidez,
    nova_sessao,
    obter_html,
    robots_para_host,
)
from agente_editais.mapa import Portal

from .conftest import url_do


def _polidez(**overrides) -> Polidez:
    base = dict(
        delay_minimo_s=0.0,
        off_peak="22:00-06:00",
        user_agent="agente-editais-testes/0.1 (+pytest)",
        probe_timeout_s=5.0,
    )
    base.update(overrides)
    return Polidez.model_validate(base)


def _portal(url_base: str, **overrides) -> Portal:
    dados = dict(
        nome="Portal Teste",
        categoria="integra",
        url=url_base,
        seeds=[url_base],
    )
    dados.update(overrides)
    return Portal.model_validate(dados)


def _caminhos(servidor) -> list[str]:
    return [registro["caminho"].split("?")[0] for registro in servidor.registros]


# -- obtenção estática -------------------------------------------------------------


def test_obter_html_estatico_retorna_pagina(servidor_fake) -> None:
    servidor_fake.paginas["/p"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><a href='/e.pdf'>edital</a></body></html>",
    )

    resultado = obter_html(url_do(servidor_fake, "/p"), _portal(url_do(servidor_fake)), _polidez())

    assert resultado.ok is True
    assert resultado.engine == "estatico"
    assert resultado.status_http == 200
    assert resultado.gatilho_playwright is None
    assert resultado.html is not None and "/e.pdf" in resultado.html
    assert resultado.url_final == resultado.url


def test_obter_html_segue_redirect_e_reporta_url_final(servidor_fake) -> None:
    servidor_fake.redirect_absoluto["/ponte"] = (
        f"http://127.0.0.1:{servidor_fake.server_address[1]}/ok"
    )

    resultado = obter_html(
        url_do(servidor_fake, "/ponte"), _portal(url_do(servidor_fake)), _polidez()
    )

    assert resultado.ok is True
    assert resultado.url_final.endswith("/ok")


def test_obter_html_rede_morta_vira_resultado_negativo_sem_crash(porta_morta) -> None:
    destino = f"http://127.0.0.1:{porta_morta}/x"

    resultado = obter_html(destino, _portal(f"http://127.0.0.1:{porta_morta}"), _polidez())

    assert resultado.ok is False
    assert resultado.engine == "estatico"
    assert resultado.status_http is None
    assert any(
        pista in (resultado.erro or "").lower() for pista in ("refused", "10061", "recusou")
    )


# -- gatilho Playwright (FR-4): portal dinâmico OU conteúdo ausente ------------------


@pytest.fixture
def playwright_falso(monkeypatch):
    """Substitui o engine real; devolve recorder com as chamadas recebidas."""
    chamadas: list[dict] = []

    def _engine(url: str, *, user_agent: str, timeout_s: float) -> tuple[int, str]:
        chamadas.append({"url": url, "user_agent": user_agent})
        return 200, "<html><body><a href='/renderizado.pdf'>edital</a></body></html>"

    monkeypatch.setattr(fetcher, "_obter_com_playwright", _engine)
    return chamadas


def test_portal_dinamico_vai_direto_ao_playwright_sem_tentativa_estatica(
    servidor_fake, playwright_falso
) -> None:
    portal = _portal(url_do(servidor_fake), dinamico=True)

    resultado = obter_html(url_do(servidor_fake), portal, _polidez())

    assert resultado.ok is True
    assert resultado.engine == "playwright"
    assert resultado.gatilho_playwright == "portal_dinamico"
    assert len(playwright_falso) == 1
    # nenhuma tentativa estática: o GET da página nunca chegou ao fake
    assert "/ok" not in _caminhos(servidor_fake)


def test_conteudo_ausente_dispara_exatamente_uma_troca(servidor_fake, playwright_falso) -> None:
    servidor_fake.paginas["/sem-alvo"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><p>Instituto Federal</p></body></html>",
    )
    portal = _portal(url_do(servidor_fake))

    resultado = obter_html(
        url_do(servidor_fake, "/sem-alvo"),
        portal,
        _polidez(),
        conteudo_presente=lambda html: False,  # conteúdo-alvo ausente no HTML estático
    )

    assert resultado.ok is True
    assert resultado.engine == "playwright"
    assert resultado.gatilho_playwright == "conteudo_ausente"
    assert len(playwright_falso) == 1, "a troca acontece UMA única vez"


def test_conteudo_presente_nao_troca_de_engine(servidor_fake, playwright_falso) -> None:
    resultado = obter_html(
        url_do(servidor_fake),
        _portal(url_do(servidor_fake)),
        _polidez(),
        conteudo_presente=lambda html: True,
    )

    assert resultado.ok is True
    assert resultado.engine == "estatico"
    assert playwright_falso == []


def test_falha_do_browser_vira_erro_de_evento_nao_crash(monkeypatch, servidor_fake) -> None:
    def _engine_explodindo(url, *, user_agent, timeout_s):
        raise RuntimeError("chromium ausente")

    monkeypatch.setattr(fetcher, "_obter_com_playwright", _engine_explodindo)

    resultado = obter_html(
        url_do(servidor_fake), _portal(url_do(servidor_fake), dinamico=True), _polidez()
    )

    assert resultado.ok is False
    assert resultado.engine == "playwright"
    assert "RuntimeError" in (resultado.erro or "")
    assert "chromium" in (resultado.erro or "")


# -- robots.txt (§9.1) ----------------------------------------------------------------


def test_robots_bloqueia_caminho_antes_da_requisicao(servidor_fake) -> None:
    servidor_fake.robots_txt = "User-agent: *\nDisallow: /privado/\n"

    resultado = obter_html(
        url_do(servidor_fake, "/privado/secao"),
        _portal(url_do(servidor_fake)),
        _polidez(),
    )

    assert resultado.ok is False
    assert resultado.bloqueio_robots is True
    assert "/privado/secao" not in _caminhos(servidor_fake), "requisição NEM acontece"
    assert "/robots.txt" in _caminhos(servidor_fake)


def test_robots_permite_caminho_livre(servidor_fake) -> None:
    servidor_fake.robots_txt = "User-agent: *\nDisallow: /privado/\n"

    resultado = obter_html(
        url_do(servidor_fake, "/publico/x"), _portal(url_do(servidor_fake)), _polidez()
    )

    assert resultado.ok is True
    assert resultado.bloqueio_robots is False


def test_robots_e_consultado_uma_unica_vez_por_execucao(servidor_fake) -> None:
    servidor_fake.robots_txt = "User-agent: *\nDisallow:\n"
    polidez = _polidez()

    primeira = obter_html(url_do(servidor_fake), _portal(url_do(servidor_fake)), polidez)
    segunda = obter_html(url_do(servidor_fake, "/outra"), _portal(url_do(servidor_fake)), polidez)

    assert primeira.ok and segunda.ok
    assert _caminhos(servidor_fake).count("/robots.txt") == 1


def test_robots_inacessivel_http500_permite_com_decisao(servidor_fake) -> None:
    servidor_fake.paginas["/robots.txt"] = (500, "text/plain", "boom")

    decisao = robots_para_host(url_do(servidor_fake), _polidez())

    assert decisao.acessivel is False
    assert decisao.pode_acessar(url_do(servidor_fake, "/qualquer"), "ua") is True
    assert "HTTP 500" in decisao.detalhe


def test_robots_ausente_404_permite(servidor_fake) -> None:
    servidor_fake.paginas["/robots.txt"] = (404, "text/plain", "não há")

    decisao = robots_para_host(url_do(servidor_fake), _polidez())

    assert decisao.acessivel is False
    assert decisao.pode_acessar(url_do(servidor_fake, "/x"), "ua") is True


def test_robots_inatingivel_rede_morta_permite(porta_morta) -> None:
    decisao = robots_para_host(f"http://127.0.0.1:{porta_morta}", _polidez())

    assert decisao.acessivel is False
    assert "ConnectionError" in decisao.detalhe or "refused" in decisao.detalhe.lower()


def test_robots_respeita_regras_por_user_agent(servidor_fake) -> None:
    servidor_fake.robots_txt = (
        "User-agent: agente-editais\nDisallow: /pesquisa/\nUser-agent: *\nAllow: /\n"
    )
    decisao = robots_para_host(url_do(servidor_fake), _polidez())

    assert decisao.acessivel is True
    assert decisao.pode_acessar(url_do(servidor_fake, "/livre"), "agente-editais/0.1 (+x)") is True
    assert (
        decisao.pode_acessar(url_do(servidor_fake, "/pesquisa/x"), "agente-editais/0.1 (+x)")
        is False
    )


def test_delay_aplicado_entre_robots_e_pagina(servidor_fake) -> None:
    servidor_fake.robots_txt = "User-agent: *\nDisallow:\n"
    polidez = _polidez(delay_minimo_s=0.3)

    robots_para_host(url_do(servidor_fake), polidez)
    obter_html(url_do(servidor_fake), _portal(url_do(servidor_fake)), polidez)

    tempos = [registro["quando"] for registro in servidor_fake.registros]
    assert len(tempos) >= 2
    # limiar folgado (>=0.1 para delay 0.3): relógio de parede em CI é ruidoso
    assert tempos[1] - tempos[0] >= 0.1, "robots e página dividem o balde de cortesia do host"


def test_playwright_tambem_divide_o_balde_de_cortesia(monkeypatch, servidor_fake) -> None:
    monkeypatch.setattr(
        fetcher,
        "_obter_com_playwright",
        lambda url, *, user_agent, timeout_s: (200, "<html></html>"),
    )
    polidez = _polidez(delay_minimo_s=0.3)

    primeira = obter_html(url_do(servidor_fake), _portal(url_do(servidor_fake)), polidez)
    segunda = obter_html(
        url_do(servidor_fake), _portal(url_do(servidor_fake), dinamico=True), polidez
    )

    assert primeira.engine == "estatico" and segunda.engine == "playwright"
    tempos = [registro["quando"] for registro in servidor_fake.registros]
    assert tempos[1] - tempos[0] >= 0.1


def test_crawl_delay_do_host_e_honrado(servidor_fake) -> None:
    """Delay efetivo = max(delay_minimo_s, Crawl-delay declarado pelo host)."""
    servidor_fake.robots_txt = "User-agent: *\nCrawl-delay: 0.3\nDisallow:\n"
    polidez = _polidez(delay_minimo_s=0.0)

    primeira = obter_html(url_do(servidor_fake), _portal(url_do(servidor_fake)), polidez)
    segunda = obter_html(url_do(servidor_fake, "/outra"), _portal(url_do(servidor_fake)), polidez)

    assert primeira.ok and segunda.ok
    tempos = [registro["quando"] for registro in servidor_fake.registros]
    assert tempos[-1] - tempos[-2] >= 0.1, "Crawl-delay do robots deve ser honrado"


# -- robustez do obter_html ---------------------------------------------------------


def test_conteudo_nao_html_e_recusado_sem_baixar_corpo(servidor_fake) -> None:
    servidor_fake.paginas["/doc"] = (200, "application/pdf", b"%PDF-1.4 corpo-grande")

    resultado = obter_html(
        url_do(servidor_fake, "/doc"), _portal(url_do(servidor_fake)), _polidez()
    )

    assert resultado.ok is False
    assert "nao-HTML" in (resultado.erro or "")
    assert "application/pdf" in (resultado.erro or "")


def test_engine_retornando_status_invalido_vira_falha(monkeypatch, servidor_fake) -> None:
    monkeypatch.setattr(
        fetcher,
        "_obter_com_playwright",
        lambda url, *, user_agent, timeout_s: (None, "<html></html>"),
    )

    resultado = obter_html(
        url_do(servidor_fake), _portal(url_do(servidor_fake), dinamico=True), _polidez()
    )

    assert resultado.ok is False
    assert "status inválido" in (resultado.erro or "")


def test_host_malformado_em_robots_nao_levanta_excecao_crua() -> None:
    decisao = robots_para_host("http://[host-malformado", _polidez())

    assert decisao.acessivel is False
    assert decisao.parser is None
    assert "ValueError" in decisao.detalhe


def test_redirect_para_origem_que_proibe_bloqueia_sem_pedir_alvo(
    servidor_fake, criar_servidor_fake
) -> None:
    destino = criar_servidor_fake()
    destino.robots_txt = "User-agent: *\nDisallow: /\n"
    porta_destino = destino.server_address[1]
    servidor_fake.redirect_absoluto["/ponte"] = f"http://127.0.0.1:{porta_destino}/alvo"

    resultado = obter_html(
        url_do(servidor_fake, "/ponte"), _portal(url_do(servidor_fake)), _polidez()
    )

    assert resultado.ok is False
    assert resultado.bloqueio_robots is True
    caminhos_destino = [registro["caminho"] for registro in destino.registros]
    assert "/alvo" not in caminhos_destino, "hop final bloqueado ANTES da requisição"
    assert "/robots.txt" in caminhos_destino, "robots DO HOST FINAL foi consultado"


# -- sessão compartilhável ------------------------------------------------------------


def test_nova_sessao_carrega_user_agent() -> None:
    with nova_sessao("ua-de-teste/1.2") as sessao:
        assert sessao.headers["User-Agent"] == "ua-de-teste/1.2"


# -- carregar_polidez: seção [crawl] ---------------------------------------------------


def test_polidez_default_obriga_janela_no_crawl(tmp_path) -> None:
    caminho = tmp_path / "politeness.toml"
    caminho.write_text(
        'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\n',
        encoding="utf-8",
    )

    assert carregar_polidez(caminho).crawl_respeitar_janela_off_peak is True


def test_polidez_le_secao_crawl(tmp_path) -> None:
    caminho = tmp_path / "politeness.toml"
    caminho.write_text(
        'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\n'
        "[crawl]\nrespeitar_janela_off_peak = false\n",
        encoding="utf-8",
    )

    assert carregar_polidez(caminho).crawl_respeitar_janela_off_peak is False


def test_polidez_le_teto_de_paginas(tmp_path) -> None:
    caminho = tmp_path / "politeness.toml"
    caminho.write_text(
        'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\n'
        "[crawl]\nmax_paginas_por_portal = 50\n",
        encoding="utf-8",
    )

    polidez = carregar_polidez(caminho)
    assert polidez.max_paginas_por_portal == 50
    assert polidez.crawl_respeitar_janela_off_peak is True  # default preservado


@pytest.mark.parametrize(
    ("linha", "fragmento"),
    [
        ("[crawl]\nmax_paginas_por_portal = 0\n", "inteiro positivo"),
        ("[crawl]\nmax_paginas_por_portal = -3\n", "inteiro positivo"),
        ("[crawl]\nmax_paginas_por_portal = true\n", "inteiro positivo"),
        ('[crawl]\nmax_paginas_por_portal = "muitas"\n', "inteiro positivo"),
    ],
)
def test_polidez_recusa_teto_de_paginas_invalido(tmp_path, linha, fragmento) -> None:
    caminho = tmp_path / "politeness.toml"
    caminho.write_text(
        'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\n' + linha,
        encoding="utf-8",
    )

    with pytest.raises(ErroConfigPolidez) as excinfo:
        carregar_polidez(caminho)
    assert fragmento in str(excinfo.value)


@pytest.mark.parametrize(
    "conteudo",
    [
        # [crawl] precisa ser tabela
        'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\ncrawl = true\n',
        # chave desconhecida dentro de [crawl]
        'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\n[crawl]\nestranha = 1\n',
        # booleano estrito em [crawl]
        'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\n'
        '[crawl]\nrespeitar_janela_off_peak = "nao"\n',
    ],
)
def test_polidez_recusa_configs_invalidas_na_secao_crawl(tmp_path, conteudo) -> None:
    caminho = tmp_path / "politeness.toml"
    caminho.write_text(conteudo, encoding="utf-8")

    with pytest.raises(ErroConfigPolidez):
        carregar_polidez(caminho)


# -- DecisaoRobots: superfície pura ----------------------------------------------------


def test_decisao_sem_parser_permite_tudo() -> None:
    decisao = DecisaoRobots(origem="http://x.org", estado="inacessivel")

    assert decisao.acessivel is False
    assert decisao.pode_acessar("http://x.org/qualquer", "ua") is True
