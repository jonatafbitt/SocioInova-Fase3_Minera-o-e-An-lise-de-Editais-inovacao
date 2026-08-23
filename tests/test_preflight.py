"""Cenários do I/O Matrix para o pré-voo VIA fetcher (FR-2, AD-5).

Polidez testada contra servidor HTTP local fake (convenção §Testes).
Nenhum teste acessa a rede real.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from agente_editais import fetcher
from agente_editais.consulta import app
from agente_editais.fetcher import (
    Polidez,
    dentro_da_janela_off_peak,
    executar_pre_voo,
    probe_seed,
)
from agente_editais.manifest import Manifesto

from .conftest import escrever_mapa, mapa_minimo, url_do


def _polidez(**overrides) -> Polidez:
    base = dict(
        delay_minimo_s=0.0,
        off_peak="22:00-06:00",
        user_agent="agente-editais-testes/0.1 (+pytest)",
        probe_timeout_s=5.0,
    )
    base.update(overrides)
    return Polidez.model_validate(base)


# -- cenário central do matrix: seeds mistas ------------------------------------


def test_seeds_mistas_separam_acessiveis_inacessiveis_e_registram_eventos(
    cli, politeness_veloz, servidor_fake, porta_morta
) -> None:
    viva = url_do(servidor_fake)
    morta = f"http://127.0.0.1:{porta_morta}/x"
    escrever_mapa(
        politeness_veloz,
        mapa_minimo(url_portal=viva, seeds=[viva])
        + "\n"
        + mapa_minimo(sigla="MOR", portal="B", categoria="nit", url_portal=morta, seeds=[morta]),
    )

    resultado = cli.invoke(app, ["preflight"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "Acessíveis:   1" in saida
    assert "Inacessíveis: 1" in saida
    assert "[FALHA]" in saida and "[OK   ]" in saida
    assert "não abortado" in saida

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        tipos = [linha["tipo"] for linha in manifesto.consultar("SELECT tipo FROM eventos")]
        assert tipos.count("seed_acessivel") == 1
        assert tipos.count("seed_inacessivel") == 1
        assert "preflight_concluido" in tipos


def test_pre_voo_com_todas_mortas_segue_o_lote_exit0(
    cli, politeness_veloz, porta_morta
) -> None:
    morta = f"http://127.0.0.1:{porta_morta}/x"
    escrever_mapa(politeness_veloz, mapa_minimo(url_portal=morta, seeds=[morta]))
    resultado = cli.invoke(app, ["preflight"])
    assert resultado.exit_code == 0
    assert "Inacessíveis: 1" in (resultado.output + (resultado.stderr or ""))


def test_executar_pre_voo_nunca_aborta_o_lote(servidor_fake, porta_morta) -> None:
    resultados = executar_pre_voo(
        [
            url_do(servidor_fake),
            f"http://127.0.0.1:{porta_morta}/x",
            url_do(servidor_fake, "/erro-servidor"),
            url_do(servidor_fake, "/movido"),
        ],
        _polidez(),
    )

    assert [r.ok for r in resultados] == [True, False, False, True]
    assert resultados[2].erro == "HTTP 500"
    assert resultados[1].status_http is None
    erro_conexao = (resultados[1].erro or "").lower()
    assert any(
        pista in erro_conexao for pista in ("refused", "10061", "recusou")
    ), f"esperava recusa de conexão, obtive: {resultados[1].erro}"


# -- polidez: UA, método e delay --------------------------------------------------


def test_probe_envia_user_agent_configurado(servidor_fake) -> None:
    polidez = _polidez(user_agent="agente-editais-pesquisa/1.0 (+ufba)")
    probe_seed(url_do(servidor_fake), polidez)
    assert servidor_fake.registros[0]["user_agent"] == "agente-editais-pesquisa/1.0 (+ufba)"


def test_head_bloqueado_cai_para_get(servidor_fake) -> None:
    resultado = probe_seed(url_do(servidor_fake, "/so-get"), _polidez())

    assert resultado.ok is True
    assert resultado.metodo == "GET"
    metodos = [registro["metodo"] for registro in servidor_fake.registros]
    assert metodos == ["HEAD", "GET"]


def test_redirect_e_seguido(servidor_fake) -> None:
    resultado = probe_seed(url_do(servidor_fake, "/movido"), _polidez())
    assert resultado.ok is True
    assert resultado.status_http == 200


def test_delay_minimo_entre_requisicoes_do_mesmo_host(servidor_fake) -> None:
    polidez = _polidez(delay_minimo_s=0.3)
    destino = url_do(servidor_fake)

    probe_seed(destino, polidez)
    probe_seed(destino, polidez)

    tempos = [registro["quando"] for registro in servidor_fake.registros]
    assert len(tempos) >= 2
    assert tempos[1] - tempos[0] >= 0.25, "segunda requisição deve esperar o delay mínimo"


def test_delay_aplicado_no_fallback_get_tambem(servidor_fake) -> None:
    polidez = _polidez(delay_minimo_s=0.3)
    probe_seed(url_do(servidor_fake, "/so-get"), polidez)

    tempos = [registro["quando"] for registro in servidor_fake.registros]
    assert len(tempos) == 2
    assert tempos[1] - tempos[0] >= 0.25


# -- janela off-peak (fuso do HOST) -----------------------------------------------


@pytest.mark.parametrize(
    ("janela", "momento", "esperado"),
    [
        ("22:00-06:00", datetime(2026, 8, 21, 23, 30), True),
        ("22:00-06:00", datetime(2026, 8, 21, 5, 59), True),
        ("22:00-06:00", datetime(2026, 8, 21, 6, 0), False),
        ("22:00-06:00", datetime(2026, 8, 21, 21, 59), False),
        ("22:00-06:00", datetime(2026, 8, 21, 12, 0), False),
        ("08:00-18:00", datetime(2026, 8, 21, 12, 0), True),
        ("08:00-18:00", datetime(2026, 8, 21, 8, 0), True),
        ("08:00-18:00", datetime(2026, 8, 21, 17, 59), True),
        ("08:00-18:00", datetime(2026, 8, 21, 18, 0), False),
    ],
)
def test_dentro_da_janela_off_peak(janela, momento, esperado) -> None:
    assert dentro_da_janela_off_peak(janela, agora=momento) is esperado


def test_probe_fora_da_janela_recusado_quando_exigido(monkeypatch, servidor_fake) -> None:
    monkeypatch.setattr(fetcher, "dentro_da_janela_off_peak", lambda *_args, **_kw: False)
    polidez = _polidez(probe_respeitar_janela_off_peak=True)
    with pytest.raises(fetcher.ViolacaoPolidez):
        probe_seed(url_do(servidor_fake), polidez)


def test_probe_na_janela_prossegue_quando_exigido(monkeypatch, servidor_fake) -> None:
    monkeypatch.setattr(fetcher, "dentro_da_janela_off_peak", lambda *_args, **_kw: True)
    polidez = _polidez(probe_respeitar_janela_off_peak=True)
    resultado = probe_seed(url_do(servidor_fake), polidez)
    assert resultado.ok is True
