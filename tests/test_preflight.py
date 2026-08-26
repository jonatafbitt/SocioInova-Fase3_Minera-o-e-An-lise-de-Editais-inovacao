"""Cenários do I/O Matrix para o pré-voo VIA fetcher (FR-2, AD-5).

Polidez testada contra servidor HTTP local fake (convenção §Testes).
Nenhum teste acessa a rede real.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from agente_editais import fetcher
from agente_editais.consulta import app
from agente_editais.fetcher import (
    ErroConfigPolidez,
    Polidez,
    carregar_polidez,
    dentro_da_janela_off_peak,
    executar_pre_voo,
    probe_seed,
)
from agente_editais.manifest import Manifesto

from .conftest import (
    POLITENESS_VELOZ,
    escrever_mapa,
    mapa_minimo,
    url_do,
)


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
        linhas = manifesto.consultar(
            "SELECT tipo, detalhe FROM eventos "
            "WHERE tipo IN ('seed_acessivel', 'seed_inacessivel', 'preflight_concluido') "
            "ORDER BY id"
        )
        por_tipo = [linha["tipo"] for linha in linhas]
        assert por_tipo.count("seed_acessivel") == 1
        assert por_tipo.count("seed_inacessivel") == 1

        for linha in linhas[:-1]:
            detalhe = json.loads(linha["detalhe"])
            # payload completo por seed (url/metodo/janela recalculada na hora)
            assert {"url", "metodo", "dentro_janela_off_peak"} <= set(detalhe)

        conclusao = json.loads(linhas[-1]["detalhe"])
        assert conclusao["seeds"] == 2
        assert conclusao["acessiveis"] == 1 and conclusao["inacessiveis"] == 1
        assert re.fullmatch(r"[0-9a-f]{64}", conclusao["politeness_sha256"]), (
            "evento de conclusão deve referenciar o hash da politeness (AD-9)"
        )


def test_pre_voo_com_todas_mortas_segue_o_lote_exit0(cli, politeness_veloz, porta_morta) -> None:
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
    assert any(pista in erro_conexao for pista in ("refused", "10061", "recusou")), (
        f"esperava recusa de conexão, obtive: {resultados[1].erro}"
    )


def test_url_malformada_vira_resultado_negativo_sem_abortar_o_lote(servidor_fake) -> None:
    resultados = executar_pre_voo(
        [url_do(servidor_fake), "apenas-texto-sem-url"],
        _polidez(),
    )

    assert resultados[0].ok is True
    assert resultados[1].ok is False
    assert resultados[1].status_http is None
    assert "ValueError" in (resultados[1].erro or "")


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


def test_redirect_para_outro_host_aplica_cortesia_ao_host_final(
    servidor_fake, criar_servidor_fake
) -> None:
    alvo = criar_servidor_fake()
    porta_alvo = alvo.server_address[1]
    # 'localhost' ≠ '127.0.0.1' como CHAVE de hostname (ambos apontam ao loopback)
    servidor_fake.redirect_absoluto["/ponte"] = f"http://localhost:{porta_alvo}/ok"

    resultado = probe_seed(url_do(servidor_fake, "/ponte"), _polidez())

    assert resultado.ok is True
    assert "localhost" in fetcher._ultimo_pedido_por_host


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


def test_delay_e_chaveado_por_hostname_nao_por_porta(criar_servidor_fake) -> None:
    """Duas portas do MESMO hostname compartilham o balde de cortesia."""
    s1 = criar_servidor_fake()
    s2 = criar_servidor_fake()
    polidez = _polidez(delay_minimo_s=0.3)

    probe_seed(url_do(s1), polidez)
    probe_seed(url_do(s2), polidez)

    inicio_s1 = s1.registros[0]["quando"]
    inicio_s2 = s2.registros[0]["quando"]
    assert inicio_s2 - inicio_s1 >= 0.25, "hostname igual (porta diferente) deve esperar delay"


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


# -- carregar_polidez: round-trip e recusas ---------------------------------------


def test_carregar_polidez_round_trip_a_partir_de_arquivo(tmp_path) -> None:
    caminho = tmp_path / "politeness.toml"
    caminho.write_text(POLITENESS_VELOZ, encoding="utf-8")

    polidez = carregar_polidez(caminho)

    assert polidez.delay_minimo_s == 0.0
    assert polidez.off_peak == "22:00-06:00"
    assert polidez.user_agent.startswith("agente-editais-testes/")
    assert polidez.probe_timeout_s == 5.0
    assert polidez.probe_respeitar_janela_off_peak is False


def test_carregar_polidez_default_de_probe(tmp_path) -> None:
    caminho = tmp_path / "politeness.toml"
    caminho.write_text(
        'delay_minimo_s = 2\noff_peak = "22:00-06:00"\nuser_agent = "ua/1"\n',
        encoding="utf-8",
    )

    polidez = carregar_polidez(caminho)

    assert polidez.probe_timeout_s == 10.0
    assert polidez.probe_respeitar_janela_off_peak is False


@pytest.mark.parametrize(
    ("conteudo", "fragmento_esperado"),
    [
        # [probe] precisa ser tabela, não escalar
        (
            'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\nprobe = "rapida"\n',
            "tabela",
        ),
        # chave desconhecida na raiz — paridade com extra=forbid do mapa
        (
            'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\nchave_estranha = 1\n',
            "desconhecidas",
        ),
        # chave desconhecida dentro de [probe]
        (
            'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\n'
            "[probe]\ntimeout_s = 5\nestranha = true\n",
            "desconhecidas",
        ),
        # booleano estrito — nada de bool("false")
        (
            'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\n'
            '[probe]\nrespeitar_janela_off_peak = "sim"\n',
            "booleano",
        ),
        # janela vazia (início = fim)
        (
            'delay_minimo_s = 0\noff_peak = "00:00-00:00"\nuser_agent = "u"\n',
            "vazia",
        ),
        # delay não numérico (TypeError/ValueError mapeados)
        (
            'delay_minimo_s = "dois"\noff_peak = "22:00-06:00"\nuser_agent = "u"\n',
            "número",
        ),
        # chave obrigatória ausente
        ('off_peak = "22:00-06:00"\nuser_agent = "u"\n', "ausente"),
    ],
)
def test_carregar_polidez_recusa_configs_invalidas(tmp_path, conteudo, fragmento_esperado) -> None:
    caminho = tmp_path / "politeness.toml"
    caminho.write_text(conteudo, encoding="utf-8")

    with pytest.raises(ErroConfigPolidez) as excinfo:
        carregar_polidez(caminho)
    assert fragmento_esperado in str(excinfo.value)


# -- abort do lote: violação de polidez sai limpo (exit 1) com evento --------------


def test_preflight_com_polidez_exigente_fora_da_janela_aborta_com_evento(
    cli, politeness_veloz, servidor_fake, porta_morta, monkeypatch
) -> None:
    politeness_path = politeness_veloz.configs / "politeness.toml"
    politeness_path.write_text(
        POLITENESS_VELOZ.replace(
            "respeitar_janela_off_peak = false", "respeitar_janela_off_peak = true"
        ),
        encoding="utf-8",
    )
    viva = url_do(servidor_fake)
    morta = f"http://127.0.0.1:{porta_morta}/x"
    escrever_mapa(
        politeness_veloz,
        mapa_minimo(url_portal=viva, seeds=[viva])
        + "\n"
        + mapa_minimo(sigla="MOR", portal="B", categoria="nit", url_portal=morta, seeds=[morta]),
    )
    monkeypatch.setattr(fetcher, "dentro_da_janela_off_peak", lambda *_a, **_k: False)

    resultado = cli.invoke(app, ["preflight"])

    assert resultado.exit_code == 1
    saida = resultado.output + (resultado.stderr or "")
    assert "ERRO:" in saida
    assert "off-peak" in saida.lower()

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        tipos = [linha["tipo"] for linha in manifesto.consultar("SELECT tipo FROM eventos")]
        assert "preflight_abortado" in tipos
        assert "preflight_concluido" not in tipos
