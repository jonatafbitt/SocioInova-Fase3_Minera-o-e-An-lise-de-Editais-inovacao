"""Eixo 3 — relevância e impacto social (heurística sobre sinais de eixo3.yaml).

Cobre a carga/validação ``carregar_config`` (contra a config REAL do repo) e
a função pura ``classificar_eixo3``: precedência SUBSTANTIVO > INSTRUMENTAL >
AUSENTE_SILENCIAMENTO, normalização (acento/caixa/whitespace), trecho
probatório dentro da janela e rede de NUNCA (config malformada recusa).
"""

from __future__ import annotations

import pytest

from agente_editais.eixo3 import (
    ErroConfigEixo3,
    _extrair_trecho,
    _normalizar,
    carregar_config,
    classificar_eixo3,
)

from .conftest import CONFIGS_DO_REPO, longo


@pytest.fixture(scope="module")
def config_repo():
    return carregar_config(CONFIGS_DO_REPO / "eixo3.yaml")


def test_normalizar_colapsa_acento_caixa_e_whitespace() -> None:
    assert _normalizar("  Pontencial de  Mercado\t\nViabilidade  ") == (
        "pontencial de mercado viabilidade"
    )
    assert _normalizar("COMUNIDADES VULNERÁVEIS") == "comunidades vulneraveis"


def test_carregar_config_real_compila_regras(config_repo) -> None:
    assert config_repo.janela_comprobatoria == 120
    verbal = classificar_eixo3("", config_repo)
    assert verbal.codigo == "AUSENTE_SILENCIAMENTO"
    assert "potencial de mercado" in config_repo.instrumental.sinais_obrigatorios
    assert "comunidade vulneravel" in config_repo.substantivo.sinais_alvo


def test_carregar_config_arquivo_ausente_recusa() -> None:
    with pytest.raises(ErroConfigEixo3, match="não encontrado"):
        carregar_config(CONFIGS_DO_REPO / "eixo3-inexistente.yaml")


def test_carregar_config_schema_errado_recusa(tmp_path) -> None:
    arquivo = tmp_path / "eixo3.yaml"
    arquivo.write_text("schema_version: 2\nregras: {}\n", encoding="utf-8")
    with pytest.raises(ErroConfigEixo3, match="schema_version"):
        carregar_config(arquivo)


def test_carregar_config_yaml_invalido_recusa(tmp_path) -> None:
    arquivo = tmp_path / "eixo3.yaml"
    arquivo.write_text("schema_version: [", encoding="utf-8")
    with pytest.raises(ErroConfigEixo3, match="YAML inválido"):
        carregar_config(arquivo)


# ---------------------------------------------------------------------------
# classificar_eixo3 — funções puras
# ---------------------------------------------------------------------------


def test_substantivo_ganha_de_instrumental(config_repo) -> None:
    texto = (
        "O edital reserva de vagas para comunidades vulneraveis no programa, "
        "avaliando tambem o potencial de mercado e a viabilidade financeira."
    )
    desfecho = classificar_eixo3(texto, config_repo)
    assert desfecho.codigo == "IMPACTO_SUBSTANTIVO"
    assert desfecho.regra_disparada == "substantivo"


def test_substantivo_respeita_acento_e_caixa(config_repo) -> None:
    sem_acento = classificar_eixo3(
        "Pontuação adicional para ECONOMIA SOLIDÁRIA neste certame.",
        config_repo,
    )
    assert sem_acento.codigo == "IMPACTO_SUBSTANTIVO"
    com_acento_na_regra = classificar_eixo3(
        "Criterio de desempate para grupos vulneraveis.",
        config_repo,
    )
    assert com_acento_na_regra.codigo == "IMPACTO_SUBSTANTIVO"


def test_substantivo_exige_alvo_ja_com_gatilho(config_repo) -> None:
    so_gatilho = classificar_eixo3("Reserva de vagas para alunos.", config_repo)
    assert so_gatilho.codigo == "AUSENTE_SILENCIAMENTO"
    so_alvo = classificar_eixo3("Fomento as tecnologias sociais.", config_repo)
    assert so_alvo.codigo == "AUSENTE_SILENCIAMENTO"


def test_instrumental_exige_todos_e_nenhum_vedado(config_repo) -> None:
    completo = classificar_eixo3(
        "A avaliacao pontua o potencial de mercado e a viabilidade financeira.",
        config_repo,
    )
    assert completo.codigo == "IMPACTO_INSTRUMENTAL"
    assert completo.regra_disparada == "instrumental"
    assert completo.sinais_encontrados == [
        "potencial de mercado",
        "viabilidade financeira",
    ]

    so_um = classificar_eixo3("So o potencial de mercado interessa.", config_repo)
    assert so_um.codigo == "AUSENTE_SILENCIAMENTO"

    vedado = classificar_eixo3(
        "Potencial de mercado e viabilidade financeira, com sustentabilidade.",
        config_repo,
    )
    assert vedado.codigo == "AUSENTE_SILENCIAMENTO", "vedado bloqueia EXCLUSIVO"


def test_silencio_padrao_e_vazio(config_repo) -> None:
    assert classificar_eixo3("", config_repo).codigo == "AUSENTE_SILENCIAMENTO"
    assert classificar_eixo3("   \n\t ", config_repo).codigo == "AUSENTE_SILENCIAMENTO"
    assert classificar_eixo3(
        "Edital de fomento a pesquisas em energias renovaveis.",
        config_repo,
    ).codigo == "AUSENTE_SILENCIAMENTO"


def test_trecho_probatorio_dentro_da_janela(config_repo) -> None:
    texto = (
        ("texto de contexto " * 25)
        + "potencial de mercado e viabilidade financeira"
        + (" mais contexto " * 30)
    )
    desfecho = classificar_eixo3(texto, config_repo)
    assert desfecho.codigo == "IMPACTO_INSTRUMENTAL"
    assert desfecho.trecho_comprobatorio
    assert "potencial de mercado" in desfecho.trecho_comprobatorio
    assert len(desfecho.trecho_comprobatorio) <= 2 * 120 + len("potencial de mercado")


def test_extrair_trecho_limita_nas_bordas() -> None:
    curto = _extrair_trecho("abc", 1, 120)
    assert curto == "abc"
    janela_zero = _extrair_trecho("substancia", 2, 0)
    assert janela_zero == "", "janela 0 ⇒ fatia vazia (não estoura)"


def test_classificar_texto_longo_sem_match_silencio(config_repo) -> None:
    assert classificar_eixo3(longo("fomento geral sem sinais"), config_repo).codigo == (
        "AUSENTE_SILENCIAMENTO"
    )
