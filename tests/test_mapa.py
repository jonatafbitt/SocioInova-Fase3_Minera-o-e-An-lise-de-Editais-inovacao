"""Cenários do I/O Matrix para o Mapa-Mestre (CAP-1) + normalização AD-8."""

from __future__ import annotations

import json
import re

import pytest

from agente_editais.consulta import app
from agente_editais.manifest import Manifesto
from agente_editais.mapa import ErroMapa, carregar_mapa, hash_arquivo, normalizar_url

from .conftest import CONFIGS_DO_REPO, escrever_mapa, mapa_minimo


# -- mapa válido -------------------------------------------------------------


def test_mapa_shipado_e_valido_cobrindo_as_quatro_categorias() -> None:
    mapa = carregar_mapa(CONFIGS_DO_REPO / "mapa-mestre.toml")

    # mínimos, não exatos: curadoria pode adicionar IFs sem editar este teste
    assert len(mapa.instituicao) >= 3
    portais = [portal for inst in mapa.instituicao for portal in inst.portal]
    assert len(portais) >= len(mapa.instituicao)
    assert all(portal.seeds for portal in portais)

    categorias_presentes = {portal.categoria for portal in portais}
    assert categorias_presentes == {"integra", "nit", "prpgi_prppg", "agencia_inovacao"}

    seeds = {seed for _, _, seed in mapa.seeds_unicas()}
    assert len(seeds) == sum(len(portal.seeds) for portal in portais)


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
    assert re.search(r"\d+ instituições, \d+ portais, \d+ seeds", saida)
    assert "https://www.ifrr.edu.br/a-instituicao/agencia-de-inovacao" in saida

    with Manifesto(configs_reais_no_tmp.manifesto) as manifesto:
        assert manifesto.contar_instituicoes() >= 3
        assert manifesto.contar_portais() >= 4
        tipos = [linha["tipo"] for linha in manifesto.consultar("SELECT tipo FROM eventos")]
        assert "mapa_sincronizado" in tipos


def test_sincronizacao_e_idempotente_ad1(cli, configs_reais_no_tmp) -> None:
    primeira = cli.invoke(app, ["mapa", "validar"])
    segunda = cli.invoke(app, ["mapa", "validar"])

    assert primeira.exit_code == 0 and segunda.exit_code == 0
    with Manifesto(configs_reais_no_tmp.manifesto) as manifesto:
        portais_depois = manifesto.contar_portais()
        portais_primeira = int(
            json.loads(
                manifesto.consultar(
                    "SELECT detalhe FROM eventos WHERE tipo='mapa_sincronizado' ORDER BY id"
                )[0]["detalhe"]
            )["portais"]
        )
        assert portais_depois == portais_primeira
        eventos = manifesto.consultar(
            "SELECT detalhe FROM eventos WHERE tipo = 'mapa_sincronizado' ORDER BY id"
        )
        detalhe_segundo = json.loads(eventos[-1]["detalhe"])
        assert detalhe_segundo["portais_criados"] == []
        assert detalhe_segundo["portais"] == portais_primeira
        assert detalhe_segundo["mapa_sha256"] == hash_arquivo(
            configs_reais_no_tmp.configs / "mapa-mestre.toml"
        )


def test_sync_update_branch_grava_novo_valor_e_lista_atualizados(cli, configs_reais_no_tmp) -> None:
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    caminho_mapa = configs_reais_no_tmp.configs / "mapa-mestre.toml"
    alterado = caminho_mapa.read_text(encoding="utf-8").replace(
        'nome = "Integra IFBA"', 'nome = "Integra IFBA (Portal de Editais)"'
    )
    caminho_mapa.write_text(alterado, encoding="utf-8")

    segunda = cli.invoke(app, ["mapa", "validar"])
    assert segunda.exit_code == 0
    assert "1 atualizados" in segunda.output

    with Manifesto(configs_reais_no_tmp.manifesto) as manifesto:
        linha = manifesto.consultar(
            "SELECT nome FROM portais WHERE url = 'https://integra.ifba.edu.br'"
        )[0]
        assert linha["nome"] == "Integra IFBA (Portal de Editais)"
        detalhe_segundo = json.loads(
            manifesto.consultar(
                "SELECT detalhe FROM eventos WHERE tipo='mapa_sincronizado' ORDER BY id DESC"
            )[0]["detalhe"]
        )
        assert "https://integra.ifba.edu.br" in detalhe_segundo["portais_atualizados"]


def test_sync_reencontra_instituicao_por_sigla_nocase(cli, configs_reais_no_tmp) -> None:
    """Sigla com caixa diferente deve atualizar a MESMA linha, não duplicar."""
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    caminho_mapa = configs_reais_no_tmp.configs / "mapa-mestre.toml"
    original = caminho_mapa.read_text(encoding="utf-8")
    caminho_mapa.write_text(original.replace('sigla = "IFRR"', 'sigla = "ifrr"'), encoding="utf-8")

    segunda = cli.invoke(app, ["mapa", "validar"])
    assert segunda.exit_code == 0

    with Manifesto(configs_reais_no_tmp.manifesto) as manifesto:
        linhas = manifesto.consultar("SELECT sigla FROM instituicoes")
        correspondencias = [linha["sigla"] for linha in linhas if linha["sigla"].upper() == "IFRR"]
        assert len(correspondencias) == 1, "mesma sigla em outra caixa não deve duplicar linha"
        detalhe_segundo = json.loads(
            manifesto.consultar(
                "SELECT detalhe FROM eventos WHERE tipo='mapa_sincronizado' ORDER BY id DESC"
            )[0]["detalhe"]
        )
        assert detalhe_segundo["instituicoes_criadas"] == [], (
            "sync com caixa diferente deve reencontrar a instituição existente"
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


@pytest.mark.parametrize(
    ("bruta", "esperada"),
    [
        ("http://a.org:80/x", "http://a.org/x"),
        ("https://a.org:443/x", "https://a.org/x"),
        ("http://a.org:8080/x", "http://a.org:8080/x"),
        ("https://A.ORG:443", "https://a.org"),
        # parse_qsl decodifica; urlencode recodifica — sem % crus indevidos
        ("https://a.org/b?titulo=Edital%20n%C2%BA%201&x=%2B", "https://a.org/b?titulo=Edital+n%C2%BA+1&x=%2B"),
    ],
)
def test_normalizar_url_portas_default_e_query_recodificada(bruta: str, esperada: str) -> None:
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


def test_seed_em_multiplos_portais_e_recusada_nomeando_os_dois(ambiente) -> None:
    seed_comum = "http://comum.org/editais"
    escrever_mapa(
        ambiente,
        mapa_minimo(sigla="AAA", portal="P1", url_portal="http://aaa.org", seeds=[seed_comum])
        + "\n"
        + mapa_minimo(
            sigla="BBB",
            portal="P2",
            categoria="nit",
            url_portal="http://bbb.org",
            seeds=[seed_comum],
        ),
    )
    with pytest.raises(ErroMapa) as excinfo:
        carregar_mapa(ambiente.configs / "mapa-mestre.toml")
    problema = next(p for p in excinfo.value.problemas if "múltiplos portais" in p)
    assert seed_comum in problema
    assert "AAA" in problema and "BBB" in problema


def test_leitura_de_arquivo_ilegivel_vira_erro_mapa(ambiente) -> None:
    caminho = ambiente.configs / "mapa-mestre.toml"
    caminho.write_bytes(b"\xff\xfe\x00nao-e-utf8")
    with pytest.raises(ErroMapa):
        carregar_mapa(caminho)


def test_hash_de_arquivo_ausente_vira_erro_mapa(tmp_path) -> None:
    with pytest.raises(ErroMapa):
        hash_arquivo(tmp_path / "inexistente.toml")
