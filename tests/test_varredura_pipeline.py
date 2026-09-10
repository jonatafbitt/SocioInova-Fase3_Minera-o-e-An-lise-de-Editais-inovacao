"""Story varredura→pipeline (Fase 5): integridade de origem lá no candidato.

Cobre o contrato das Fases 1–4:
- v9→v10 preserva candidatos e adiciona ``varredura_id`` (idempotente);
- ``registrar_candidato`` grava ``varredura_id`` só no INSERT (nunca sobrescreve);
- a varredura real (``varredura rodar``) carimba os candidatos que ela descobriu;
- no CLI, ``--varredura`` restringe cada um dos 5 estágios às URLs daquela
  varredura (o candidato de fora fica intocado);
- guardas de uso: varredura inexistente → exit 1; combinar ``--varredura`` com
  outra fonte de alvo → exit 2.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agente_editais import manifest
from agente_editais.consulta import app
from agente_editais.manifest import Manifesto

from .conftest import (
    CONFIGS_DO_REPO,
    POLITENESS_VELOZ,
    escrever_mapa,
    html_lista,
    longo,
    mapa_portal,
    pdf_com_docinfo,
    saida_cli,
    url_do,
)

RAIZ_DO_REPO = Path(__file__).resolve().parents[1]
PDF_X = "/pdfs/x.pdf"


def _linhas_varredura(caminho: Path) -> list[dict]:
    with Manifesto(caminho) as m:
        return [
            dict(linha)
            for linha in m.consultar("SELECT id, url, status FROM varreduras ORDER BY id")
        ]


def _candidatos(caminho: Path) -> list[dict]:
    with Manifesto(caminho) as m:
        return [
            dict(linha)
            for linha in m.consultar(
                "SELECT id, portal_id, url, varredura_id FROM candidatos ORDER BY id"
            )
        ]


def _documentos(caminho: Path) -> list[dict]:
    with Manifesto(caminho) as m:
        return [
            dict(linha)
            for linha in m.consultar(
                "SELECT url_origem, ano_aceito FROM documentos ORDER BY url_origem"
            )
        ]


def _classificacoes(caminho: Path) -> list[dict]:
    with Manifesto(caminho) as m:
        return [
            dict(linha)
            for linha in m.consultar(
                "SELECT url_origem, tipo_edital, metodo FROM classificacoes ORDER BY url_origem"
            )
        ]


@pytest.fixture
def configs_completas_no_tmp(politeness_veloz, monkeypatch) -> SimpleNamespace:
    """Classificação + Eixo 3 reais junto com polidez veloz e corpus isolado."""
    for nome in ("sinais_inovacao.yaml", "sinais_analiticos.yaml", "eixo3.yaml"):
        origem = CONFIGS_DO_REPO / nome
        if origem.exists():
            (politeness_veloz.configs / nome).write_bytes(origem.read_bytes())
    raiz = politeness_veloz.configs.parent / "corpus"
    monkeypatch.setenv("AGENTE_EDITAIS_CORPUS", str(raiz))
    return politeness_veloz


def _setup_varredura(
    cli: CliRunner,
    polidez: SimpleNamespace,
    servidor_fake,
    pdfs: dict[str, bytes],
    *,
    fora: str = "/pdfs/fora.pdf",
    coletar_tudo: bool = True,
) -> int:
    """Mapa validar → varredura concluída → candidato de fora → coletar.

    Retorna o id da varredura. Com ``coletar_tudo=True`` AMBOS os PDFs vão ao
    L1 (documentos) e cada estágio pode provar que ignora o candidato de fora;
    com ``False`` ninguém baixa nada (o estágio sob teste faz o download).
    """
    escrever_mapa(polidez, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    servidor_fake.paginas["/editais/lista"] = (
        200,
        "text/html; charset=utf-8",
        html_lista([(PDF_X, "Edital X")]),
    )
    for caminho, corpo in pdfs.items():
        servidor_fake.paginas[caminho] = (200, "application/pdf", corpo)
    servidor_fake.paginas[fora] = (200, "application/pdf", pdf_com_docinfo([longo("Fora")]))
    url = url_do(servidor_fake, "/editais/lista")
    assert cli.invoke(app, ["varredura", "adicionar", url]).exit_code == 0
    assert cli.invoke(app, ["varredura", "rodar"]).exit_code == 0
    (linha,) = _linhas_varredura(polidez.manifesto)
    assert linha["status"] == "concluida"

    with Manifesto(polidez.manifesto) as m:
        portal_id = m.id_portal_por_url(url_do(servidor_fake))
        assert m.registrar_candidato(portal_id, url_do(servidor_fake, fora), "pdf") is True
    if coletar_tudo:
        assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0
        assert len(_documentos(polidez.manifesto)) == 2
    return linha["id"]


def test_migracao_v10_sobre_banco_v9_preserva_candidatos_e_eh_idempotente(
    tmp_path: Path,
) -> None:
    """Banco em user_version=9 (pré-varredura) ganha varredura_id intacto."""
    caminho = tmp_path / "v9.sqlite3"
    bruto = sqlite3.connect(caminho)
    try:
        bruto.execute("PRAGMA foreign_keys = OFF")
        for nome in (
            "_MIGRACAO_V1",
            "_MIGRACAO_V2",
            "_MIGRACAO_V3",
            "_MIGRACAO_V4",
            "_MIGRACAO_V5",
            "_MIGRACAO_V6",
            "_MIGRACAO_V7",
            "_MIGRACAO_V8",
            "_MIGRACAO_V9",
        ):
            for declaracao in getattr(manifest, nome):
                bruto.execute(declaracao)
        bruto.execute("PRAGMA user_version = 9")
        bruto.commit()
    finally:
        bruto.close()

    manifesto = Manifesto(caminho)
    try:
        assert manifesto.schema_version() == 11
        colunas = [c[1] for c in manifesto.consultar("PRAGMA table_info(candidatos)")]
        assert "varredura_id" in colunas
        indices = {
            r[0]
            for r in manifesto.consultar(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%varredura%'"
            )
        }
        assert "idx_candidatos_varredura" in indices
    finally:
        manifesto.fechar()

    reaberto = Manifesto(caminho)
    try:
        assert reaberto.schema_version() == 11  # idempotente
        colunas = [c[1] for c in reaberto.consultar("PRAGMA table_info(candidatos)")]
        assert "varredura_id" in colunas
    finally:
        reaberto.fechar()


def test_registrar_candidato_grava_varredura_id_no_insert_e_nao_sobrescreve(
    cli,
    politeness_veloz,
    servidor_fake,
) -> None:
    """varredura_id entra apenas no INSERT; conflito de URL preserva a origem."""
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    with Manifesto(politeness_veloz.manifesto) as m:
        portal_id = m.id_portal_por_url(url_do(servidor_fake))
        url = "http://exemplo.org/sem-origem.pdf"
        assert m.registrar_candidato(portal_id, url, "pdf", varredura_id=None) is True
        assert (
            m.consultar("SELECT varredura_id FROM candidatos WHERE url = ?", (url,))[0][0] is None
        )

    # varredura REAL (linha existe) para satisfazer a FK de varredura_id (v10)
    assert cli.invoke(app, ["varredura", "adicionar", url_do(servidor_fake)]).exit_code == 0
    (varredura,) = _linhas_varredura(politeness_veloz.manifesto)
    vid = varredura["id"]

    with Manifesto(politeness_veloz.manifesto) as m:
        portal_id = m.id_portal_por_url(url_do(servidor_fake))
        url = "http://exemplo.org/desta-varredura.pdf"
        assert m.registrar_candidato(portal_id, url, "pdf", varredura_id=vid) is True
        assert (
            m.consultar("SELECT varredura_id FROM candidatos WHERE url = ?", (url,))[0][0] == vid
        )

        # conflito (mesma url) SEM varredura: não insere nem pisa o vid
        assert m.registrar_candidato(portal_id, url, "pdf") is False
        assert (
            m.consultar("SELECT varredura_id FROM candidatos WHERE url = ?", (url,))[0][0] == vid
        )

        resumos = m.candidatos_de_varredura(vid)
        assert [dict(c)["url"] for c in resumos] == [url]


def test_varredura_rodar_carimba_candidatos_descobertos(
    cli, politeness_veloz, servidor_fake
) -> None:
    vid = _setup_varredura(
        cli, politeness_veloz, servidor_fake, {PDF_X: pdf_com_docinfo([longo("Edital X")])}
    )
    da_varredura = [c for c in _candidatos(politeness_veloz.manifesto) if c["varredura_id"] == vid]
    assert [c["url"] for c in da_varredura] == [url_do(servidor_fake, PDF_X)]
    fora = [c for c in _candidatos(politeness_veloz.manifesto) if c["varredura_id"] is None]
    assert [c["url"] for c in fora] == [url_do(servidor_fake, "/pdfs/fora.pdf")]


def test_coletar_varredura_baixa_so_o_pdf_da_varredura(
    cli, politeness_veloz, servidor_fake
) -> None:
    vid = _setup_varredura(
        cli,
        politeness_veloz,
        servidor_fake,
        {PDF_X: pdf_com_docinfo([longo("Edital X")])},
        coletar_tudo=False,
    )

    resultado = cli.invoke(app, ["coletar", "--varredura", str(vid)])
    assert resultado.exit_code == 0, saida_cli(resultado)
    urls = _documentos(politeness_veloz.manifesto)
    assert [d["url_origem"] for d in urls] == [url_do(servidor_fake, PDF_X)]


def test_textuar_varredura_extrai_so_o_da_varredura(cli, corpus, servidor_fake) -> None:
    polidez = SimpleNamespace(
        configs=corpus.parent / "configs",
        manifesto=corpus.parent / "dados" / "manifesto.sqlite3",
    )
    vid = _setup_varredura(
        cli,
        polidez,
        servidor_fake,
        {PDF_X: pdf_com_docinfo([longo("Edital X")])},
        fora="/pdfs/fora.pdf",
    )

    resultado = cli.invoke(app, ["textuar", "--varredura", str(vid)])
    assert resultado.exit_code == 0, saida_cli(resultado)
    with Manifesto(polidez.manifesto) as m:
        caminhos = [
            dict(l)["texto_caminho"]
            for l in m.consultar("SELECT texto_caminho FROM documentos ORDER BY url_origem")
        ]
    textos = [caminho for caminho in caminhos if caminho]
    assert len(textos) == 1, "só o PDF da varredura foi texturado (o de fora ficou sem texto)"
    texto = Path(textos[0])
    assert texto.exists()
    assert "Edital X" in texto.read_text()


def test_datar_varredura_so_data_o_da_varredura(cli, corpus, servidor_fake) -> None:
    polidez = SimpleNamespace(
        configs=corpus.parent / "configs",
        manifesto=corpus.parent / "dados" / "manifesto.sqlite3",
    )
    vid = _setup_varredura(
        cli,
        polidez,
        servidor_fake,
        {PDF_X: pdf_com_docinfo([longo("Edital X")], criado_em="D:20230601000000Z")},
        fora="/pdfs/fora.pdf",
    )
    assert cli.invoke(app, ["textuar", "--todos"]).exit_code == 0

    resultado = cli.invoke(app, ["datar", "--varredura", str(vid)])
    assert resultado.exit_code == 0, saida_cli(resultado)
    datados = [d for d in _documentos(polidez.manifesto) if d["ano_aceito"] is not None]
    assert [d["url_origem"] for d in datados] == [url_do(servidor_fake, PDF_X)]
    assert datados[0]["ano_aceito"] == 2023


def test_classificar_varredura_e_eixo3_so_da_varredura(
    cli,
    configs_completas_no_tmp,
    servidor_fake,
) -> None:
    polidez = configs_completas_no_tmp
    vid = _setup_varredura(
        cli,
        polidez,
        servidor_fake,
        {PDF_X: pdf_com_docinfo([longo("Edital X")], criado_em="D:20230601000000Z")},
        fora="/pdfs/fora.pdf",
    )
    assert cli.invoke(app, ["textuar", "--varredura", str(vid)]).exit_code == 0

    saida = cli.invoke(app, ["classificar", "--varredura", str(vid)])
    assert saida.exit_code == 0, saida_cli(saida)
    classificacoes = _classificacoes(polidez.manifesto)
    assert [c["url_origem"] for c in classificacoes] == [url_do(servidor_fake, PDF_X)]
    assert classificacoes[0]["tipo_edital"] is not None

    saida_eixo = cli.invoke(app, ["classificar-eixo3", "--varredura", str(vid)])
    assert saida_eixo.exit_code == 0, saida_cli(saida_eixo)
    assert [c["url_origem"] for c in _classificacoes(polidez.manifesto)] == [
        url_do(servidor_fake, PDF_X)
    ]


@pytest.mark.parametrize(
    "comando", ["coletar", "textuar", "datar", "classificar", "classificar-eixo3"]
)
def test_varredura_inexistente_recusa_todos_os_estagios(
    cli,
    configs_completas_no_tmp,
    servidor_fake,
    comando,
) -> None:
    escrever_mapa(configs_completas_no_tmp, mapa_portal(servidor_fake))
    resultado = cli.invoke(app, [comando, "--varredura", "999"])
    assert resultado.exit_code == 1
    assert "nenhuma varredura" in saida_cli(resultado)


@pytest.mark.parametrize(
    "args",
    [
        ["coletar", "--varredura", "1", "--portal", "IFC"],
        ["textuar", "--varredura", "1", "--todos"],
        ["datar", "--varredura", "1", "--portal", "IFC"],
        ["classificar", "--varredura", "1", "--todos"],
        ["classificar-eixo3", "--varredura", "1", "--todos"],
    ],
)
def test_varredura_e_exclusivo_com_outras_fontes_de_alvo(cli, args) -> None:
    resultado = cli.invoke(app, args)
    assert resultado.exit_code == 2
    assert "exatamente um" in saida_cli(resultado)


def _carregar_dashboard() -> tuple:
    """Importa dashboard.py em processo (CWD aponta para o tmp sob teste)."""
    spec = importlib.util.spec_from_file_location(
        "dashboard_sob_teste", RAIZ_DO_REPO / "dashboard.py"
    )
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_dashboard_helpers_leem_varredura_v10(
    cli,
    servidor_fake,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Aba 6 lê candidatos/log da varredura direto do banco v10 (AD-3/AD-4)."""
    dados = tmp_path / "dados"
    dados.mkdir()
    caminho_manifesto = dados / "manifesto.sqlite3"
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "politeness.toml").write_text(POLITENESS_VELOZ, encoding="utf-8")
    monkeypatch.setenv("AGENTE_EDITAIS_CONFIGS", str(configs))
    monkeypatch.setenv("AGENTE_EDITAIS_MANIFESTO", str(caminho_manifesto))

    polidez = SimpleNamespace(configs=configs, manifesto=caminho_manifesto)
    escrever_mapa(polidez, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    assert cli.invoke(app, ["varredura", "adicionar", url_do(servidor_fake)]).exit_code == 0
    with Manifesto(caminho_manifesto) as m:
        (linha,) = m.consultar("SELECT id FROM varreduras")
        vid = linha[0]
        portal_id = m.id_portal_por_url(url_do(servidor_fake))
        assert m.registrar_candidato(
            portal_id, url_do(servidor_fake, "/pdfs/aba.pdf"), "pdf", varredura_id=vid
        )
        assert m.marcar_varredura_iniciada(vid, forcar=True)
        assert m.marcar_varredura_concluida(
            vid, {"visitas": 1, "candidatos_novos": 1, "secoes_ja_conhecidas": 0}
        )

    monkeypatch.chdir(tmp_path)
    dashboard = _carregar_dashboard()
    df = dashboard._candidatos_de_varredura(vid)
    assert len(df) == 1
    assert list(df["Baixado"]) == ["–"]
    assert list(df["Textuado"]) == ["–"]
    assert list(df["Datado"]) == ["–"]
    assert list(df["Classificado"]) == ["–"]
    assert list(df["Eixo 3"]) == ["–"]
    log = dashboard._log_varredura(vid)
    assert isinstance(log, dict)
    assert log["visitas"] == 1
    entradas = dashboard._entradas_de_varredura(log)
    assert ("visitas", "1") in entradas
    assert ("candidatos_novos", "1") in entradas
    assert dashboard._entradas_de_varredura(["corpo", {"tipo": "robo", "ok": True}]) == [
        ("evento", "corpo"),
        ("robo", '{"tipo": "robo", "ok": true}'),
    ]
