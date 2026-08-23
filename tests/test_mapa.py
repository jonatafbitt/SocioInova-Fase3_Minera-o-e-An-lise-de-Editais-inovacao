"""Cenários do I/O Matrix para o Mapa-Mestre (CAP-1) + normalização AD-8."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from agente_editais.consulta import app
from agente_editais.manifest import Manifesto
from agente_editais.mapa import ErroMapa, carregar_mapa, hash_arquivo, normalizar_url

from .conftest import CONFIGS_DO_REPO, escrever_mapa, mapa_minimo


# -- mapa válido -------------------------------------------------------------


def test_mapa_shipado_e_valido_com_todas_as_categorias() -> None:
    mapa = carregar_mapa(CONFIGS_DO_REPO / "mapa-mestre.toml")

    assert len(mapa.instituicao) == 10
    portais = [portal for inst in mapa.instituicao for portal in inst.portal]
    assert len(portais) == 12
    distribuicao = Counter(portal.categoria for portal in portais)
    assert distribuicao == {
        "integra": 4,
        "nit": 2,
        "prpgi_prppg": 3,
        "agencia_inovacao": 3,
    }
    assert all(portal.seeds for portal in portais)
    seeds = {seed for _, _, seed in mapa.seeds_unicas()}
    assert len(seeds) == 12


def test_cli_mapa_validar_exit0_lista_completa_e_registra_eventos(
    cli, configs_reais_no_tmp
) -> None:
    resultado = cli.invoke(app, ["mapa", "validar"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    for sigla in ("IFBA", "IFSP", "IFMS", "IFRJ", "IFC", "IFMA", "IFPA", "IFES", "IFPR", "IFRR"):
        assert sigla in saida
    for categoria in ("integra", "nit", "prpgi_prppg", "agencia_inovacao"):
        assert f"[{categoria}]" in saida
    assert "12 portais" in saida
    assert "https://www.ifrr.edu.br/a-instituicao/agencia-de-inovacao" in saida

    with Manifesto(configs_reais_no_tmp.manifesto) as manifesto:
        assert manifesto.contar_instituicoes() == 10
        assert manifesto.contar_portais() == 12
        tipos = [linha["tipo"] for linha in manifesto.consultar("SELECT tipo FROM eventos")]
        assert "mapa_sincronizado" in tipos


def test_sincronizacao_e_idempotente_ad1(cli, configs_reais_no_tmp) -> None:
    primeira = cli.invoke(app, ["mapa", "validar"])
    segunda = cli.invoke(app, ["mapa", "validar"])

    assert primeira.exit_code == 0 and segunda.exit_code == 0
    with Manifesto(configs_reais_no_tmp.manifesto) as manifesto:
        assert manifesto.contar_portais() == 12
        eventos = manifesto.consultar(
            "SELECT detalhe FROM eventos WHERE tipo = 'mapa_sincronizado' ORDER BY id"
        )
        detalhe_segundo = json.loads(eventos[-1]["detalhe"])
        assert detalhe_segundo["portais_criados"] == []
        assert detalhe_segundo["portais"] == 12
        assert detalhe_segundo["mapa_sha256"] == hash_arquivo(
            configs_reais_no_tmp.configs / "mapa-mestre.toml"
        )


# -- categoria inválida: linha/campo nomeados, banco intocado ------------------


def test_categoria_invalida_nomes_linha_campo_e_exit_diferente_de_zero(cli, ambiente) -> None:
    texto = (
        mapa_minimo(url_portal="http://127.0.0.1:8000", seeds=["http://127.0.0.1:8000"])
        + """
  [[instituicao.portal]]
  nome = "Portal Ruim"
  categoria = "integr"
  url = "http://127.0.0.1:8000/ruim"
  seeds = ["http://127.0.0.1:8000/ruim"]
"""
    )
    caminho = escrever_mapa(ambiente, texto)
    linha_esperada = next(n for n, l in enumerate(caminho.read_text(encoding="utf-8").splitlines(), 1) if '"integr"' in l)

    resultado = cli.invoke(app, ["mapa", "validar"])

    assert resultado.exit_code != 0
    saida = resultado.output + (resultado.stderr or "")
    assert str(linha_esperada) in saida
    assert "categoria" in saida
    assert "'integr'" in saida
    assert "instituicao[0].portal[1].categoria" in saida
    assert not ambiente.manifesto.exists(), "banco não pode ser tocado com mapa inválido"


def test_categoria_invalida_via_api_levanta_erro_mapa(ambiente) -> None:
    escrever_mapa(
        ambiente,
        mapa_minimo(categoria="nit-de-verdade", url_portal="http://x.org", seeds=["http://x.org"]),
    )
    with pytest.raises(ErroMapa) as excinfo:
        carregar_mapa(ambiente.configs / "mapa-mestre.toml")
    assert any("nit-de-verdade" in problema for problema in excinfo.value.problemas)


def test_toml_malformado_nomeia_linha(cli, ambiente) -> None:
    escrever_mapa(
        ambiente,
        'schema_version = 1\n\n[[instituicao]]\nsigla = "TST"\nnome = "sem fechar string\n',
    )
    resultado = cli.invoke(app, ["mapa", "validar"])
    assert resultado.exit_code != 0
    saida = resultado.output + (resultado.stderr or "")
    assert "TOML malformado" in saida
    assert "linha" in saida.lower()
    assert not ambiente.manifesto.exists()


def test_campo_extra_e_recusado(ambiente) -> None:
    escrever_mapa(
        ambiente,
        'schema_version = 1\nchave_estranha = true\n\n[[instituicao]]\nsigla = "TST"\n'
        'nome = "Teste"\n\n  [[instituicao.portal]]\n  nome = "P"\n  categoria = "integra"\n'
        '  url = "http://x.org"\n  seeds = ["http://x.org"]\n',
    )
    with pytest.raises(ErroMapa) as excinfo:
        carregar_mapa(ambiente.configs / "mapa-mestre.toml")
    assert any("chave_estranha" in problema for problema in excinfo.value.problemas)


def test_sigla_duplicada_e_recusada(ambiente) -> None:
    um = mapa_minimo(sigla="ABC", url_portal="http://um.org", seeds=["http://um.org"])
    dois = mapa_minimo(
        sigla="ABC",
        portal="B",
        url_portal="http://dois.org",
        seeds=["http://dois.org"],
    )
    escrever_mapa(ambiente, um + "\n" + dois)
    with pytest.raises(ErroMapa) as excinfo:
        carregar_mapa(ambiente.configs / "mapa-mestre.toml")
    assert any("duplicada" in problema for problema in excinfo.value.problemas)


def test_url_de_portal_duplicada_e_recusada(ambiente) -> None:
    um = mapa_minimo(sigla="AAA", url_portal="http://mesmo.org/p", seeds=["http://mesmo.org/p"])
    dois = mapa_minimo(sigla="BBB", url_portal="HTTP://MESMO.ORG/p", seeds=["http://mesmo.org/q"])
    escrever_mapa(ambiente, um + "\n" + dois)
    with pytest.raises(ErroMapa) as excinfo:
        carregar_mapa(ambiente.configs / "mapa-mestre.toml")
    assert any("devem ser únicas" in problema for problema in excinfo.value.problemas)


# -- normalização de URL (AD-8) -----------------------------------------------


@pytest.mark.parametrize(
    ("bruta", "esperada"),
    [
        (
            "HTTPS://Integra.IFBA.edu.br/Editais/2024/#secao",
            "https://integra.ifba.edu.br/Editais/2024/",
        ),
        (
            "https://Portal.IFSP.edu.br/x?utm_source=boletim&utm_medium=email&pagina=2",
            "https://portal.ifsp.edu.br/x?pagina=2",
        ),
        ("http://IFRJ.edu.br", "http://ifrj.edu.br"),
        (
            "https://a.org/caminho?b=2&a=1&utm_campaign=z",
            "https://a.org/caminho?b=2&a=1",
        ),
    ],
)
def test_normalizar_url_minusculas_fragment_utm(bruta: str, esperada: str) -> None:
    assert normalizar_url(bruta) == esperada


@pytest.mark.parametrize("ruim", ["ftp://a.org/x", "//sem-esquema.org", "apenas-texto"])
def test_normalizar_url_recusa_esquema_invalido(ruim: str) -> None:
    with pytest.raises(ValueError):
        normalizar_url(ruim)


def test_seeds_duplicadas_apos_normalizacao_sao_recusadas(ambiente) -> None:
    escrever_mapa(
        ambiente,
        mapa_minimo(
            url_portal="http://dupla.org",
            seeds=["http://dupla.org/a#topo", "http://DUPLA.ORG/a"],
        ),
    )
    with pytest.raises(ErroMapa) as excinfo:
        carregar_mapa(ambiente.configs / "mapa-mestre.toml")
    assert any("repetida" in problema for problema in excinfo.value.problemas)
