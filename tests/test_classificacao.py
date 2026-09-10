"""Story de classificação: sinais de inovação decidem o tipo_edital (ADVISORY).

Cobre a função pura ``avaliar``, a carga/validação ``carregar_sinais`` e o
pipeline CLI real (descobrir → coletar → textuar → classificar) sobre o
servidor fake local — incluindo os NUNCA: não sobrescrever veredito
definitivo e nunca classificar documento excluído pela curadoria.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agente_editais.classificacao import (
    ErroConfigSinais,
    SinaisInovacao,
    _normalizar,
    avaliar,
    carregar_sinais,
)
from agente_editais.consulta import app
from agente_editais.manifest import Manifesto

from .conftest import (
    CONFIGS_DO_REPO,
    POLITENESS_VELOZ,
    descobrir_coletar,
    escrever_mapa,
    html_lista,
    longo,
    mapa_portal,
    pdf_apenas_imagem,
    pdf_com_texto,
    saida_cli,
    url_do,
)


@pytest.fixture
def configs_com_sinais(ambiente):
    """Configs com sinais versionados + polidez rápida (janela fora do jogo)."""
    (ambiente.configs / "politeness.toml").write_text(POLITENESS_VELOZ, encoding="utf-8")
    shutil.copy2(
        CONFIGS_DO_REPO / "sinais_inovacao.yaml", ambiente.configs / "sinais_inovacao.yaml"
    )
    shutil.copy2(
        CONFIGS_DO_REPO / "sinais_analiticos.yaml", ambiente.configs / "sinais_analiticos.yaml"
    )
    return ambiente


@pytest.fixture
def corpus_com_sinais(configs_com_sinais, monkeypatch):
    raiz = configs_com_sinais.configs.parent / "corpus"
    monkeypatch.setenv("AGENTE_EDITAIS_CORPUS", str(raiz))
    return raiz


@pytest.fixture(scope="module")
def sinais_repo() -> SinaisInovacao:
    return carregar_sinais(
        CONFIGS_DO_REPO / "sinais_inovacao.yaml",
        CONFIGS_DO_REPO / "sinais_analiticos.yaml",
    )


@pytest.fixture
def corpus_pipeline(cli, configs_com_sinais, corpus_com_sinais, servidor_fake):
    """4 documentos reais: inovação(f), nao_inovação, escaneado e âncora."""
    links = [
        ("/docs/fomento-tecnologico.pdf", "Chamada Pública 2024 — Fomento"),
        ("/docs/assistencia-estudantil.pdf", "Documento Oficial 2024 - Anexo I"),
        ("/docs/escaneado-2024.pdf", "Edital Escaneado 2024"),
        ("/docs/incubadora-2024.pdf", "EDITAL DE INCUBAÇÃO DE STARTUPS"),
    ]
    pdfs = {
        # texto ASCII: o helper de PDF em memória (content-stream latin-1)
        # corrompe acentos na extração — matching acentuado fica nas funções
        # puras e na âncora acentuada da incubadora abaixo.
        "/docs/fomento-tecnologico.pdf": pdf_com_texto(
            [longo("EDITAL DE TRANSFERENCIA DE TECNOLOGIA E INOVACAO PARA "
                   "PROSPECCAO TECNOLOGICA")]
        ),
        "/docs/assistencia-estudantil.pdf": pdf_com_texto(
            [longo("EDITAL DE APOIO A ASSISTENCIA ESTUDANTIL DO CAMPUS")]
        ),
        "/docs/escaneado-2024.pdf": pdf_apenas_imagem(),
        "/docs/incubadora-2024.pdf": pdf_apenas_imagem(),
    }
    descobrir_coletar(cli, configs_com_sinais, servidor_fake, html=html_lista(links), pdfs=pdfs)
    textuar = cli.invoke(app, ["textuar", "--portal", "TST"])
    assert textuar.exit_code == 0, saida_cli(textuar)
    return configs_com_sinais, servidor_fake


def _classificacoes(caminho_manifesto) -> dict[str, dict]:
    with Manifesto(caminho_manifesto) as manifesto:
        return {
            str(linha["url_origem"]): dict(linha)
            for linha in manifesto.consultar(
                "SELECT * FROM classificacoes ORDER BY url_origem"
            )
        }


# ---------------------------------------------------------------------------
# funções puras
# ---------------------------------------------------------------------------


def test_carregar_sinais_da_repo_e_casar_com_acento_e_caixa(sinais_repo) -> None:
    casados, dimensoes = sinais_repo.casar(
        _normalizar("edital de TRANSFERÊNCIA de tecnologia vira inovacao")
    )
    assert "transferencia de tecnologia" in casados
    assert "inovacao" in casados
    assert "fundamentos_epistemologicos" in dimensoes


def test_carregar_sinais_recusa_duplicata_pos_normalizacao(ambiente) -> None:
    sinais = ambiente.configs / "sinais_inovacao.yaml"
    sinais.write_text(
        "schema_version: 1\n"
        "sinais:\n"
        '  - "Inovação"\n'
        '  - "INOVAÇÃO"  # colapsa no mesmo token\n',
        encoding="utf-8",
    )
    dimensoes = ambiente.configs / "sinais_analiticos.yaml"
    dimensoes.write_text("schema_version: 1\ndimensoes: []\n", encoding="utf-8")

    with pytest.raises(ErroConfigSinais):
        carregar_sinais(sinais, dimensoes)


def test_carregar_sinais_recusa_arquivo_ausente(ambiente) -> None:
    with pytest.raises(ErroConfigSinais):
        carregar_sinais(
            ambiente.configs / "sumido.yaml", ambiente.configs / "tambem-sumido.yaml"
        )


def test_avaliar_texto_com_sinal_metodo_texto(sinais_repo) -> None:
    desfecho = avaliar(
        "EDITAL DE TRANSFERÊNCIA DE TECNOLOGIA, INOVAÇÃO E PROSPECÇÃO",
        "Nota oficial",
        sinais_repo,
    )
    assert desfecho.tipo_edital == "inovacao"
    assert desfecho.metodo == "texto"
    assert desfecho.fonte_ancora is None
    assert "transferencia de tecnologia" in desfecho.sinais


def test_avaliar_texto_sem_sinal_ancora_com_sinal_metodo_ancora(sinais_repo) -> None:
    desfecho = avaliar(
        "EDITAL DE APOIO À ASSISTÊNCIA ESTUDANTIL",
        "EDITAL DE INOVAÇÃO TECNOLÓGICA",
        sinais_repo,
    )
    assert desfecho.tipo_edital == "inovacao"
    assert desfecho.metodo == "ancora"
    assert desfecho.fonte_ancora and "INOVAÇÃO" in desfecho.fonte_ancora
    assert "inovacao" in desfecho.sinais


def test_avaliar_texto_sem_sinal_em_nenhum_lugar_e_nao_inovacao(sinais_repo) -> None:
    desfecho = avaliar(
        "EDITAL DE APOIO À ASSISTÊNCIA ESTUDANTIL DO CAMPUS",
        "Documento oficial",
        sinais_repo,
    )
    assert desfecho.tipo_edital == "nao_inovacao"
    assert desfecho.metodo == "texto"
    assert desfecho.sinais == []


def test_avaliar_sem_texto_ancora_com_sinal_metodo_ancora(sinais_repo) -> None:
    desfecho = avaliar(None, "EDITAL DE INCUBAÇÃO DE STARTUPS", sinais_repo)
    assert desfecho.tipo_edital == "inovacao"
    assert desfecho.metodo == "ancora"
    assert desfecho.fonte_ancora
    assert "incubacao" in desfecho.sinais


def test_avaliar_sem_texto_e_ancora_neutra_resulta_sem_texto(sinais_repo) -> None:
    desfecho = avaliar(None, "Documento escaneado", sinais_repo)
    assert desfecho.tipo_edital == "sem_texto"
    assert desfecho.metodo == "sem_texto"
    assert desfecho.sinais == []


# ---------------------------------------------------------------------------
# CLI — pipeline completo
# ---------------------------------------------------------------------------


def test_classificar_todos_decide_matriz_completa(
    cli, corpus_pipeline, configs_com_sinais, servidor_fake
) -> None:
    resultado = cli.invoke(app, ["classificar", "--todos"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "[TST] Portal TST" in saida
    assert "inovacao=2" in saida
    assert "nao_inovacao=1" in saida
    assert "sem_texto=1" in saida
    assert "Classificados: 4" in saida

    classificacoes = _classificacoes(configs_com_sinais.manifesto)
    fomento_url = url_do(servidor_fake, "/docs/fomento-tecnologico.pdf")
    assistencia_url = url_do(servidor_fake, "/docs/assistencia-estudantil.pdf")
    escaneado_url = url_do(servidor_fake, "/docs/escaneado-2024.pdf")
    incubadora_url = url_do(servidor_fake, "/docs/incubadora-2024.pdf")
    assert classificacoes[fomento_url]["tipo_edital"] == "inovacao"
    assert classificacoes[fomento_url]["metodo"] == "texto"
    assert classificacoes[assistencia_url]["tipo_edital"] == "nao_inovacao"
    assert classificacoes[escaneado_url]["tipo_edital"] == "sem_texto"
    assert classificacoes[escaneado_url]["metodo"] == "sem_texto"
    assert classificacoes[incubadora_url]["tipo_edital"] == "inovacao"
    assert classificacoes[incubadora_url]["metodo"] == "ancora"

    fomento = classificacoes[fomento_url]
    sinais = json.loads(fomento["sinais"])
    assert "transferencia de tecnologia" in sinais
    dimensoes = json.loads(fomento["dimensoes"])
    assert "fundamentos_epistemologicos" in dimensoes
    incubadora = classificacoes[incubadora_url]
    assert incubadora["fonte_ancora"] and "INCUBAÇÃO" in incubadora["fonte_ancora"]


def test_classificar_documento_sobre_veredito_definitivo_nao_sobrescreve(
    cli, corpus_pipeline, configs_com_sinais, servidor_fake
) -> None:
    url = url_do(servidor_fake, "/docs/fomento-tecnologico.pdf")
    assert cli.invoke(app, ["classificar", "--todos"]).exit_code == 0

    resultado = cli.invoke(app, ["classificar", "--documento", url])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "Já definitivos: 1" in saida
    assert "Classificados: 0" in saida
    with Manifesto(configs_com_sinais.manifesto) as manifesto:
        total = manifesto.consultar("SELECT COUNT(*) FROM classificacoes")[0][0]
    assert total == 4, "nenhum INSERT de re-classificação (never sobrescrever)"


def test_classificar_documento_preenche_sem_texto_quando_ganha_texto(
    cli, corpus_pipeline, configs_com_sinais, servidor_fake
) -> None:
    url = url_do(servidor_fake, "/docs/escaneado-2024.pdf")
    assert cli.invoke(app, ["classificar", "--todos"]).exit_code == 0

    with Manifesto(configs_com_sinais.manifesto) as manifesto:
        (documento,) = manifesto.consultar(
            "SELECT * FROM documentos WHERE url_origem = ?", (url,)
        )
        Path(documento["texto_caminho"]).write_text(
            "EDITAL DE TRANSFERÊNCIA DE TECNOLOGIA E INOVAÇÃO", encoding="utf-8"
        )

    resultado = cli.invoke(app, ["classificar", "--documento", url])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert "inovacao=1" in saida_cli(resultado)
    assert "sem_texto=0" in saida_cli(resultado)

    classificacao = _classificacoes(configs_com_sinais.manifesto)[url]
    assert classificacao["tipo_edital"] == "inovacao"
    assert classificacao["metodo"] == "texto"
    assert "transferencia de tecnologia" in json.loads(classificacao["sinais"])


def test_classificar_documento_inexistente_exit1(
    cli, corpus_pipeline, configs_com_sinais, servidor_fake
) -> None:
    resultado = cli.invoke(
        app, ["classificar", "--documento", url_do(servidor_fake, "/docs/nunca.pdf")]
    )
    assert resultado.exit_code == 1
    assert "nenhum documento" in saida_cli(resultado)


def test_classificar_documento_excluido_pela_curadoria_exit1(
    cli, corpus_pipeline, configs_com_sinais, servidor_fake
) -> None:
    url = url_do(servidor_fake, "/docs/incubadora-2024.pdf")
    with Manifesto(configs_com_sinais.manifesto) as manifesto:
        portal_id = manifesto.id_portal_por_url(url_do(servidor_fake))
        assert portal_id is not None
        manifesto.executar(
            """
            INSERT INTO fila_revisao (
                url_origem, portal_id, motivo, status, criado_em,
                decidido_exclusao, justificativa, autor, decidido_em
            ) VALUES (?, ?, 'exclusao_da_curadoria', 'resolvida', '2026-01-01T00:00:00+00:00',
                      1, 'Fora do escopo da Rede Federal.', 'Curadoria', '2026-01-02T00:00:00+00:00')
            """,
            (url, portal_id),
        )

    resultado = cli.invoke(app, ["classificar", "--documento", url])

    assert resultado.exit_code == 1
    assert "excluído da curadoria" in saida_cli(resultado)
    assert _classificacoes(configs_com_sinais.manifesto).get(url) is None


def test_classificar_excluido_vigente_sai_da_populacao_do_portal(
    cli, corpus_pipeline, configs_com_sinais, servidor_fake
) -> None:
    assert cli.invoke(app, ["classificar", "--todos"]).exit_code == 0
    url = url_do(servidor_fake, "/docs/escaneado-2024.pdf")
    with Manifesto(configs_com_sinais.manifesto) as manifesto:
        portal_id = manifesto.id_portal_por_url(url_do(servidor_fake))
        manifesto.executar(
            """
            INSERT INTO fila_revisao (
                url_origem, portal_id, motivo, status, criado_em,
                decidido_exclusao, justificativa, autor, decidido_em
            ) VALUES (?, ?, 'exclusao_da_curadoria', 'resolvida', '2026-01-01T00:00:00+00:00',
                      1, 'Duplicado do ano anterior.', 'Curadoria', '2026-01-02T00:00:00+00:00')
            """,
            (url, portal_id),
        )

    resultado = cli.invoke(app, ["classificar", "--todos"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "Classificados: 0" in saida
    assert "Já definitivos: 3" in saida
    assert "excluídos pela curadoria: 1" in saida
    with Manifesto(configs_com_sinais.manifesto) as manifesto:
        (atual,) = manifesto.consultar(
            "SELECT * FROM classificacoes WHERE url_origem = ?", (url,)
        )
    assert atual["tipo_edital"] == "sem_texto", "excluído não é re-classificado"


def test_classificar_sem_flag_exatamente_um_exit2(
    cli, corpus_pipeline, configs_com_sinais
) -> None:
    sem_destino = cli.invoke(app, ["classificar"])
    assert sem_destino.exit_code == 2
    assert "exatamente um de" in saida_cli(sem_destino)

    duplo = cli.invoke(app, ["classificar", "--todos", "--portal", "TST"])
    assert duplo.exit_code == 2


def test_classificar_sem_sinais_config_recusa_exit2(
    cli, politeness_veloz, servidor_fake
) -> None:
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["classificar", "--todos"])

    assert resultado.exit_code == 2
    assert "sinais_inovacao.yaml" in saida_cli(resultado)


def test_classificar_por_portal_sigla_case_insensivel(
    cli, corpus_pipeline, configs_com_sinais
) -> None:
    resultado = cli.invoke(app, ["classificar", "--portal", "tst"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "[TST] Portal TST" in saida
    assert "Classificados: 4" in saida
    assert "inovacao=2" in saida


def test_classificar_recusa_portal_nao_sincronizado(
    cli, configs_com_sinais, servidor_fake
) -> None:
    """classificar --todos recusa portal presente no mapa mas NÃO sincronizado.

    O mapa tem TST (sincronizado via 'mapa validar') + BBB (adicionado no
    arquivo depois, sem revalidar). A recusa acontece ANTES de qualquer
    classificação (linha 1797-1804 consulta.py), sem tocar a rede.
    """
    escrever_mapa(configs_com_sinais, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    # Adiciona BBB ao mapa sem revalidar — portal existe no arquivo mas não no manifesto
    # Adiciona ao MESMO [[instituicao]] do TST (TOML não permite [[instituicao]] duplicado)
    caminho_mapa = configs_com_sinais.configs / "mapa-mestre.toml"
    original = caminho_mapa.read_text(encoding="utf-8")
    bbb = """

  [[instituicao.portal]]
  nome = "BBB"
  categoria = "integra"
  url = "https://bbb.ifyy.edu.br"
  seeds = ["https://bbb.ifyy.edu.br/editais"]
""".strip()
    caminho_mapa.write_text(original + "\n" + bbb, encoding="utf-8")

    resultado = cli.invoke(app, ["classificar", "--todos"])

    assert resultado.exit_code == 1, saida_cli(resultado)
    assert "não sincronizados" in saida_cli(resultado)
