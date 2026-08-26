"""Cenários do I/O Matrix da consulta essencial (CAP-9/UJ-3/§10).

Fluxos REAIS: mapa validar → descobrir → coletar → textuar → datar →
fila decidir — contra o servidor fake local. Toda leitura passa pelos
métodos ONLY-leitura do Manifesto (AD-3/AD-4): a custódia é verificada
com o corpus APAGADO do disco, provando que nenhum PDF é lido.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

import agente_editais
from agente_editais.consulta import _COLUNAS_CSV, app
from agente_editais.manifest import Manifesto

from .conftest import (
    descobrir_coletar,
    documentos_do_manifesto,
    escrever_mapa,
    fila_do_manifesto,
    html_lista,
    longo,
    mapa_minimo,
    mapa_portal,
    pdf_com_docinfo,
    registrar_candidatos,
    saida_cli,
    tipos_eventos,
    url_do,
)

_EDITAL_GEMEOS = "tst-2023-gemeo"
_RE_RESUMO = re.compile(r"Resumo: (\d+) documento\(s\) listado\(s\), (\d+) excluído\(s\)")

# Ordem determinística da listagem completa (patch 9): flag de ano vazio em
# PRIMEIRO (pendentes por último), MANTENDO as chaves seguintes do ORDER BY
# original (instituição → ano → edital). Fixada documento a documento (ids
# únicos) no teste do CSV.
_ORDEM_ESPERADA_SUFIXOS = (
    "/2024/f.pdf",  # AGI antes de TST (chave seguinte: instituição)
    "/docs/c.pdf",  # TST · 2022 (fila_humana)
    "/2023/a.pdf",  # tst-2023-a < tst-2023-gemeo
    "/2023/gemeo.pdf",
    "/2024/b.pdf",
    "/outro/2023/gemeo.pdf",  # 2025 (fila_humana)
    "/docs/p.pdf",  # ano vazio POR ÚLTIMO
)


def _caminho_de(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path


def _urls_na_ordem(estado: dict) -> list[str]:
    """URLs do corpus na ordem esperada — sufixos resolvidos SEM ambiguidade
    ('/2023/gemeo.pdf' também casa em '/outro/2023/gemeo.pdf')."""
    docs = estado["docs"]

    def unico(sufixo: str) -> str:
        encontradas = [u for u in docs if _caminho_de(u).endswith(sufixo)]
        assert len(encontradas) == 1, f"sufixo ambíguo no fixture: {sufixo}"
        return encontradas[0]

    canonico = next(
        u for u in docs if _caminho_de(u).endswith("/2023/gemeo.pdf") and "/outro/" not in u
    )
    return [
        unico("/2024/f.pdf"),
        unico("/docs/c.pdf"),
        unico("/2023/a.pdf"),
        canonico,
        unico("/2024/b.pdf"),
        estado["gemeo_fila"],
        estado["pendente"],
    ]


# -- infraestrutura compartilhada ---------------------------------------------------


def _decidir(cli, caminho_manifesto, url_suffix: str, *argumentos: str) -> None:
    """Decide o item de fila cuja url_origem termina com ``url_suffix``."""
    (item,) = [
        i for i in fila_do_manifesto(caminho_manifesto) if i["url_origem"].endswith(url_suffix)
    ]
    resultado = cli.invoke(app, ["fila", "decidir", "--id", str(item["id"]), *argumentos])
    assert resultado.exit_code == 0, resultado.output


def _montar_corpus(
    cli,
    politeness_veloz,
    servidor_fake,
    criar_servidor_fake,
) -> dict:
    """Pipeline real completo até as decisões humanas — estado do I/O Matrix.

    TST (categoria integra):
      - /2023/a.pdf e /2024/b.pdf com docinfo ⇒ ano_fonte=automatica;
      - /docs/c.pdf (sem data nenhuma) decidido como ano 2022 na fila;
      - /2023/gemeo.pdf (docinfo 2023) E /outro/2023/gemeo.pdf (sem docinfo,
        mesmo slug+ano ⇒ MESMO edital tst-2023-gemeo): o primeiro aceito na
        automática, o segundo só pela fila (baixa_confianca_sourl) decidido
        como ano 2025;
      - /docs/p.pdf fica PENDENTE na fila (ano vazio);
      - /docs/e.pdf é decidido como EXCLUSÃO.
    AGI (categoria agencia_inovacao): /2024/f.pdf automático (filtro combinado).
    """
    descobrir_coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        html=html_lista(
            [
                ("/2023/a.pdf", "Edital de Inovação um"),
                ("/2024/b.pdf", "Edital de Inovação dois"),
                ("/2023/gemeo.pdf", "Edital gemeo automatico"),
                ("/outro/2023/gemeo.pdf", "Edital gemeo da fila"),
                ("/docs/c.pdf", "Documento decidido na fila"),
                ("/docs/p.pdf", "Documento pendente"),
                ("/docs/e.pdf", "Documento duplicado"),
            ]
        ),
        pdfs={
            "/2023/a.pdf": pdf_com_docinfo([longo("Automatico A")], criado_em="D:20230601000000Z"),
            "/2024/b.pdf": pdf_com_docinfo([longo("Automatico B")], criado_em="D:20240201000000Z"),
            "/2023/gemeo.pdf": pdf_com_docinfo(
                [longo("Gemeo automatico")], criado_em="D:20230501000000Z"
            ),
            "/outro/2023/gemeo.pdf": pdf_com_docinfo([longo("Gemeo apenas pela fila")]),
            "/docs/c.pdf": pdf_com_docinfo([longo("Decidido somente na fila")]),
            "/docs/p.pdf": pdf_com_docinfo([longo("Pendente para sempre")]),
            "/docs/e.pdf": pdf_com_docinfo([longo("Excluido pela curadoria")]),
        },
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0
    assert cli.invoke(app, ["datar", "--portal", "TST"]).exit_code == 0

    servidor_agi = criar_servidor_fake()
    servidor_agi.paginas["/2024/f.pdf"] = (
        200,
        "application/pdf",
        pdf_com_docinfo([longo("Agencia automatica")], criado_em="D:20240301000000Z"),
    )
    escrever_mapa(
        politeness_veloz,
        mapa_portal(servidor_fake)
        + "\n"
        + mapa_minimo(
            sigla="AGI",
            categoria="agencia_inovacao",
            url_portal=url_do(servidor_agi),
            seeds=[url_do(servidor_agi)],
        ),
    )
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    registrar_candidatos(servidor_agi, politeness_veloz.manifesto, ["/2024/f.pdf"])
    assert cli.invoke(app, ["coletar", "--todos"]).exit_code == 0
    assert cli.invoke(app, ["textuar", "--todos"]).exit_code == 0
    assert cli.invoke(app, ["datar", "--todos"]).exit_code == 0

    _decidir(
        cli,
        politeness_veloz.manifesto,
        "/docs/c.pdf",
        "--ano",
        "2022",
        "--justificativa",
        "Processo de 2022 no sistema do IF.",
        "--autor",
        "Pesquisadora",
    )
    _decidir(
        cli,
        politeness_veloz.manifesto,
        "/outro/2023/gemeo.pdf",
        "--ano",
        "2025",
        "--justificativa",
        "Capa declara publicação em 2025.",
        "--autor",
        "Pesquisadora",
    )
    _decidir(
        cli,
        politeness_veloz.manifesto,
        "/docs/e.pdf",
        "--excluir",
        "--justificativa",
        "Duplicado de outro edital.",
        "--autor",
        "Curadoria",
    )

    docs = {d["url_origem"]: d for d in documentos_do_manifesto(politeness_veloz.manifesto)}
    return {
        "docs": docs,
        "mais_antigo": next(u for u in docs if u.endswith("/docs/c.pdf")),
        "gemeo_fila": next(u for u in docs if u.endswith("/outro/2023/gemeo.pdf")),
        "pendente": next(u for u in docs if u.endswith("/docs/p.pdf")),
        "excluido": next(u for u in docs if u.endswith("/docs/e.pdf")),
    }


def _totais_da_saida(saida: str) -> tuple[int, int]:
    captura = _RE_RESUMO.search(saida)
    assert captura, f"resumo ausente na saída:\n{saida}"
    return int(captura.group(1)), int(captura.group(2))


def _contagem_sql_independente(manifesto: Manifesto, *, excluidos: bool) -> int:
    """COUNT SQL independente sobre a MESMA definição de linhas (AC da story).

    Espelha a regra vigente da fila (uma decisão por URL: resolvida vence
    pendente; entre resolvidas, a de maior id) — escrito À MÃO aqui para que
    a igualdade prove a fonte única em vez de testar o código contra ele mesmo.
    """
    alvo = "= 1" if excluidos else "= 0"
    return int(
        manifesto.consultar(
            f"""
            SELECT COUNT(*) FROM documentos d
            JOIN editais e ON e.id = d.edital_id
            JOIN instituicoes i ON i.id = e.instituicao_id
            LEFT JOIN (
                SELECT url_origem,
                       CASE WHEN MAX(CASE WHEN status = 'resolvida' THEN id END) IS NOT NULL
                            THEN MAX(CASE WHEN status = 'resolvida' THEN id END)
                            ELSE MAX(id)
                       END AS id_vigente
                FROM fila_revisao
                GROUP BY url_origem
            ) fv ON fv.url_origem = d.url_origem
            LEFT JOIN fila_revisao f ON f.id = fv.id_vigente
            WHERE COALESCE(f.decidido_exclusao, 0) {alvo}
            """
        )[0][0]
    )


def _ultimo_evento(caminho_manifesto, tipo: str) -> dict:
    eventos = [detalhe for t, detalhe in tipos_eventos(caminho_manifesto) if t == tipo]
    assert eventos, f"evento {tipo} ausente"
    return eventos[-1]


# -- Cenário "Consulta feliz" + AC: total impresso = COUNT SQL independente ---------


def test_consulta_sem_filtros_bate_com_sql_independente(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)

    resultado = cli.invoke(app, ["consultar"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    listados, excluidos = _totais_da_saida(saida)
    assert "Resumo: 7 documento(s) listado(s), 1 excluído(s)" in saida
    assert "Por ano_fonte: automatica=4, fila_humana=2, vazio=1" in saida, (
        "pendentes aparecem como vazio — nada silenciado"
    )

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        total_sql = _contagem_sql_independente(manifesto, excluidos=False)
        excluidos_sql = _contagem_sql_independente(manifesto, excluidos=True)
    assert listados == total_sql == 7, "fonte única verificada (FR-20)"
    assert excluidos == excluidos_sql == 1


def test_consulta_filtro_instituicao_insensivel_a_caixa(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)

    tst = cli.invoke(app, ["consultar", "--instituicao", "tst"])

    assert tst.exit_code == 0, saida_cli(tst)
    saida_tst = saida_cli(tst)
    listados, excluidos = _totais_da_saida(saida_tst)
    assert (listados, excluidos) == (6, 1), "AGI fica fora; exclusão é da TST"
    assert "agi-2024-f" not in saida_tst

    agi = cli.invoke(app, ["consultar", "--instituicao", "AGI"])

    assert agi.exit_code == 0, saida_cli(agi)
    saida_agi = saida_cli(agi)
    listados_agi, excluidos_agi = _totais_da_saida(saida_agi)
    assert (listados_agi, excluidos_agi) == (1, 0)
    assert "agi-2024-f" in saida_agi and "tst-2023-a" not in saida_agi


# -- Cenário "Filtros combinados": interseção AND ------------------------------------


def test_consulta_filtros_combinados_em_and(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)

    combinado = cli.invoke(app, ["consultar", "--ano", "2024", "--categoria", "agencia_inovacao"])

    assert combinado.exit_code == 0, saida_cli(combinado)
    saida = saida_cli(combinado)
    listados, _ = _totais_da_saida(saida)
    assert listados == 1 and "agi-2024-f" in saida
    assert "tst-2024-b" not in saida, "interseção AND, nunca união"

    so_ano = cli.invoke(app, ["consultar", "--ano", "2024"])
    assert _totais_da_saida(saida_cli(so_ano))[0] == 2, "b (TST) + f (AGI)"

    so_categoria = cli.invoke(app, ["consultar", "--categoria", "integra"])
    assert _totais_da_saida(saida_cli(so_categoria))[0] == 6


# -- Patch 8a: --ano casa o ano EFETIVO (fio COALESCE → filtro) ----------------------


def test_ano_casa_decisao_humana_que_so_existe_na_fila(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """--ano 2025 retorna EXATAMENTE o documento cujo ano só existe via
    ``decidido_ano`` — quebrar o filtro para ``d.ano_aceito = ?`` zera isto."""
    estado = _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    destino = Path(politeness_veloz.configs).parent / "so-fila.csv"

    resultado = cli.invoke(app, ["consultar", "--ano", "2025", "--saida", str(destino)])

    assert resultado.exit_code == 0, saida_cli(resultado)
    linhas = list(csv.reader(destino.read_text(encoding="utf-8").splitlines()))
    indice = {coluna: i for i, coluna in enumerate(_COLUNAS_CSV)}
    urls = [linha[indice["url_origem"]] for linha in linhas[1:]]
    assert urls == [estado["gemeo_fila"]], "exatamente o documento só-da-fila"
    listados, excluidos = _totais_da_saida(saida_cli(resultado))
    assert (listados, excluidos) == (1, 1), (
        "excluído continua contado no resumo — população excluída ignora --ano"
    )


def test_ano_sem_resultados_ainda_conta_os_excluidos(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """Patch 4: com --ano, a população excluída (sem ano efetivo, em geral)
    NÃO some do resumo — 'excluídos: 0' enganoso é bug, não feature."""
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)

    resultado = cli.invoke(app, ["consultar", "--ano", "2019"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert "Resumo: 0 documento(s) listado(s), 1 excluído(s)" in saida_cli(resultado)


# -- Cenário "Zero resultados": exit 0, cabeçalho, contagens zeradas ------------------


def test_zero_resultados_exit_zero_com_contagens_zeradas(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)

    resultado = cli.invoke(app, ["consultar", "--instituicao", "XXX"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "ANO" in saida and "FONTE" in saida, "cabeçalho presente mesmo vazio"
    assert "(nenhum documento corresponde aos filtros)" in saida
    assert "Resumo: 0 documento(s) listado(s), 0 excluído(s)" in saida
    assert "automatica=0, fila_humana=0, vazio=0" in saida


def test_csv_zero_resultados_tem_cabecalho_completo_e_zero_linhas(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """Patch 8b+8d: hipótese vazia COMBINANDO filtros e exportando — arquivo
    existe, cabeçalho completo, ZERO linhas de dados (não é erro)."""
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    destino = Path(politeness_veloz.configs).parent / "vazio.csv"

    resultado = cli.invoke(
        app,
        ["consultar", "--ano", "2019", "--categoria", "integra", "--saida", str(destino)],
    )

    assert resultado.exit_code == 0, saida_cli(resultado)
    linhas = list(csv.reader(destino.read_text(encoding="utf-8").splitlines()))
    assert linhas[0] == list(_COLUNAS_CSV), "cabeçalho completo mesmo sem dados"
    assert len(linhas) == 1, "zero linhas de dados"
    assert "Resumo: 0 documento(s) listado(s)" in saida_cli(resultado)


# -- Cenário "Export CSV": header completo, ordem fixa, ano só-da-fila ----------------


def test_csv_carrega_ano_da_fila_ordem_deterministica_e_iguala_contagem(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """Patches 8b+8c+9: linhas = contagem impressa, ORDEM assertada documento a
    documento (anos vazios por último) e ano da fila presente no export."""
    estado = _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    destino = Path(politeness_veloz.configs).parent / "catalogo.csv"

    resultado = cli.invoke(app, ["consultar", "--saida", str(destino)])

    assert resultado.exit_code == 0, saida_cli(resultado)
    conteudo = destino.read_text(encoding="utf-8")
    assert not conteudo.startswith("\ufeff"), "UTF-8 SEM BOM"
    linhas = list(csv.reader(conteudo.splitlines()))
    assert linhas[0] == list(_COLUNAS_CSV), "cabeçalho completo"
    dados = linhas[1:]
    listados, _ = _totais_da_saida(saida_cli(resultado))
    assert len(dados) == listados == 7, "linhas de dados = contagem impressa"

    indice = {coluna: i for i, coluna in enumerate(_COLUNAS_CSV)}
    ids = [linha[indice["documento_id"]] for linha in dados]
    urls_na_ordem = _urls_na_ordem(estado)
    assert ids == [estado["docs"][u]["id"] for u in urls_na_ordem], (
        "ordem determinística: anos vazios por último, depois inst/ano/edital"
    )

    por_url = {linha[indice["url_origem"]]: linha for linha in dados}
    gemeo = por_url[estado["gemeo_fila"]]
    assert gemeo[indice["ano"]] == "2025", "ano vem da decisão humana"
    assert gemeo[indice["ano_fonte"]] == "fila_humana"
    antigo = por_url[estado["mais_antigo"]]
    assert antigo[indice["ano"]] == "2022" and antigo[indice["ano_fonte"]] == "fila_humana"
    pendente = por_url[estado["pendente"]]
    assert pendente[indice["ano"]] == "" and pendente[indice["ano_fonte"]] == "vazio"
    assert estado["excluido"] not in por_url, "excluído fora do CSV padrão"


# -- Cenário "Excluído" e "Pendente na fila" -----------------------------------------


def test_excluido_ausente_da_listagem_mas_contado_no_resumo(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    estado = _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)

    resultado = cli.invoke(app, ["consultar"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "Resumo: 7 documento(s) listado(s), 1 excluído(s)" in saida
    assert estado["excluido"] not in saida, "excluído fora da listagem"


def test_pendente_aparece_com_ano_vazio_e_por_ultimo(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    estado = _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)

    resultado = cli.invoke(app, ["consultar"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    id_pendente = estado["docs"][estado["pendente"]]["id"]
    assert id_pendente in saida, "nenhum desaparecimento silencioso"
    listados, _ = _totais_da_saida(saida)
    assert listados == 7, "pendente conta na listagem com ano vazio"


# -- Patch 3: precedência ÚNICA por URL na fila (listar ↔ custódia) -------------------


def test_dupla_resolucao_na_mesma_url_entra_uma_vez_com_a_decisao_recente(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """Duas resolvidas na mesma URL (patologia inserida direto no banco): o
    documento aparece exatamente UMA vez com a decisão MAIS RECENTE (maior
    id) — nem duplicação na listagem, nem dado velho na custódia."""
    estado = _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    url = estado["gemeo_fila"]

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        (primeira,) = manifesto.consultar(
            "SELECT * FROM fila_revisao WHERE url_origem = ?", (url,)
        )
        manifesto.executar(
            """
            INSERT INTO fila_revisao (
                url_origem, portal_id, motivo, status, criado_em,
                decidido_ano, decidido_exclusao, justificativa, autor, decidido_em
            ) VALUES (?, ?, 'baixa_confianca_sourl', 'resolvida',
                      '2026-01-01T00:00:00+00:00', 2024, 0,
                      'Revisão posterior corrigiu o ano.', 'Curadoria Revisada',
                      '2026-02-01T00:00:00+00:00')
            """,
            (url, primeira["portal_id"]),
        )

    resultado = cli.invoke(app, ["consultar"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    listados, excluidos = _totais_da_saida(saida)
    assert (listados, excluidos) == (7, 1), "documento aparece exatamente UMA vez"

    so_2025 = cli.invoke(app, ["consultar", "--ano", "2025"])
    assert _totais_da_saida(saida_cli(so_2025))[0] == 0, (
        "decisão antiga (2025) foi SUPERADA pela mais recente"
    )

    so_2024 = cli.invoke(app, ["consultar", "--ano", "2024"])
    saida_2024 = saida_cli(so_2024)
    assert _totais_da_saida(saida_2024)[0] == 3, "b + f + gêmeo re-decidido"
    id_gemeo = estado["docs"][url]["id"]
    assert id_gemeo in saida_2024

    destino = Path(politeness_veloz.configs).parent / "custodia-dupla.json"
    custodia = cli.invoke(app, ["custodia", "--edital", _EDITAL_GEMEOS, "--saida", str(destino)])
    assert custodia.exit_code == 0, saida_cli(custodia)
    dados = json.loads(destino.read_text(encoding="utf-8"))
    por_url = {d["captura"]["url_origem"]: d for d in dados["documentos"]}
    bloco = por_url[url]["fila"]
    assert bloco["decidido_ano"] == 2024, "custódia reflete a MESMA linha vigente"
    assert bloco["autor"] == "Curadoria Revisada"
    assert bloco["justificativa"] == "Revisão posterior corrigiu o ano."
    assert bloco["decidido_em"] == "2026-02-01T00:00:00+00:00"


# -- Cenário "Flag malformada": recusa ANTES de abrir o banco (exit 2) ---------------


@pytest.mark.parametrize(
    "argumentos",
    [
        ["--categoria", "foo"],
        ["--categoria", ""],
        ["--ano", "2030"],
        ["--ano", "2018"],
        ["--instituicao", ""],
    ],
)
def test_flags_malformadas_recusadas_antes_do_banco(cli, politeness_veloz, argumentos):
    caminho_manifesto = politeness_veloz.manifesto
    assert not Path(caminho_manifesto).exists()

    resultado = cli.invoke(app, ["consultar", *argumentos])

    assert resultado.exit_code == 2, saida_cli(resultado)
    assert "ERRO" in saida_cli(resultado)
    assert not Path(caminho_manifesto).exists(), "banco nem pode ser criado"


def test_custodia_edital_vazio_ou_ausente_exit_2_antes_do_banco(cli, politeness_veloz):
    assert not Path(politeness_veloz.manifesto).exists()

    vazio = cli.invoke(app, ["custodia", "--edital", ""])
    ausente = cli.invoke(app, ["custodia"])

    assert vazio.exit_code == 2
    assert ausente.exit_code == 2
    assert not Path(politeness_veloz.manifesto).exists()


def test_saida_apontando_para_o_manifesto_recusada_antes_do_banco(cli, politeness_veloz):
    """Patch 5: export nunca pode truncar o próprio banco (guarda catastrófica)."""
    caminho_manifesto = politeness_veloz.manifesto
    assert not Path(caminho_manifesto).exists()

    recusado = cli.invoke(app, ["consultar", "--saida", str(caminho_manifesto)])
    custodia_recusada = cli.invoke(
        app, ["custodia", "--edital", "x", "--saida", str(caminho_manifesto)]
    )

    assert recusado.exit_code == 2, saida_cli(recusado)
    assert "Manifesto" in saida_cli(recusado)
    assert custodia_recusada.exit_code == 2
    assert not Path(caminho_manifesto).exists(), "banco nem é criado na recusa"


def test_consulta_e_custodia_sem_manifesto_exit_1_sem_criar_banco(cli, politeness_veloz):
    consultar = cli.invoke(app, ["consultar", "--instituicao", "IFES"])
    custodia = cli.invoke(app, ["custodia", "--edital", "algo"])

    assert consultar.exit_code == 1
    assert "Manifesto não encontrado" in saida_cli(consultar)
    assert custodia.exit_code == 1
    assert not Path(politeness_veloz.manifesto).exists(), "abertura não cria banco"


# -- Cenário "Custódia completa": JSON reconstrói captura → datação → decisão --------


def test_custodia_reconstroi_cadeia_sem_ler_nenhum_pdf(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    estado = _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    for doc in estado["docs"].values():
        Path(doc["caminho"]).unlink()  # corpus APAGADO: custodia não pode ler PDF
    destino = Path(politeness_veloz.configs).parent / "custodia.json"

    resultado = cli.invoke(app, ["custodia", "--edital", _EDITAL_GEMEOS, "--saida", str(destino)])

    assert resultado.exit_code == 0, saida_cli(resultado)
    dados = json.loads(destino.read_text(encoding="utf-8"))
    assert dados["edital_id"] == _EDITAL_GEMEOS
    assert dados["instituicao"] == "TST"
    assert len(dados["documentos"]) == 2, "canônico + irmão no mesmo edital"

    # Patch 2: metadados autodescritivos no nível raiz (§10 "quem capturou")
    assert dados["versao_agente"] == agente_editais.__version__
    assert isinstance(dados["schema_version"], int) and dados["schema_version"] >= 5
    gerado_em = datetime.fromisoformat(dados["gerado_em"])
    assert gerado_em.tzinfo is not None, "gerado_em é ISO 8601 COM timezone"

    por_url = {d["captura"]["url_origem"]: d for d in dados["documentos"]}
    url_automatico = next(
        u for u in por_url if u.endswith("/2023/gemeo.pdf") and "/outro/" not in u
    )
    automatico = por_url[url_automatico]
    captura = automatico["captura"]
    assert captura["hash_sha256"] and captura["data_captura"]
    assert captura["url_origem"] and captura["versao_crawler"]
    assert captura["caminho"] and captura["predecessor_id"] is None
    datacao = automatico["datacao"]
    assert datacao["metodo_datacao"] == "url" and datacao["ano_aceito"] == 2023
    assert {ev["fonte"] for ev in datacao["evidencias"]} == {
        "url",
        "ancora",
        "pdf_meta",
    }, "uma evidência por fonte consultada (FR-6)"
    assert all(ev["valor_bruto"] and ev["localizacao"] for ev in datacao["evidencias"])
    assert automatico["fila"] is None

    decidido = por_url[estado["gemeo_fila"]]
    assert decidido["datacao"]["metodo_datacao"] is None, "decisão humana não toca o documento"
    bloco_fila = decidido["fila"]
    assert bloco_fila["status"] == "resolvida"
    assert bloco_fila["motivo"] == "baixa_confianca_sourl"
    assert bloco_fila["decidido_ano"] == 2025
    assert bloco_fila["decidido_exclusao"] is False
    assert "publicação" in bloco_fila["justificativa"], "UTF-8 real (ensure_ascii=False)"
    assert bloco_fila["autor"] == "Pesquisadora" and bloco_fila["decidido_em"]


def test_custodia_stdout_com_pendente_e_inexistente_exit_1(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    estado = _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    edital_pendente = "tst-_sem_ano-p"

    resultado = cli.invoke(app, ["custodia", "--edital", edital_pendente])

    assert resultado.exit_code == 0, saida_cli(resultado)
    dados = json.loads(saida_cli(resultado))
    assert dados["edital_id"] == edital_pendente
    (documento,) = dados["documentos"]
    assert documento["captura"]["url_origem"] == estado["pendente"]
    assert documento["datacao"]["metodo_datacao"] is None
    assert documento["fila"]["status"] == "pendente", "bloco fila existe sem decisão"
    assert documento["fila"]["decidido_ano"] is None
    assert documento["fila"]["autor"] is None and documento["fila"]["justificativa"] is None

    fantasma = cli.invoke(app, ["custodia", "--edital", "nao-existe"])

    assert fantasma.exit_code == 1
    assert "nao-existe" in saida_cli(fantasma)


# -- Falha de escrita: exit 1, evento honesto e resultado visível ---------------------


def test_consulta_falha_de_escrita_do_csv_exit_1_mas_mostra_o_resultado(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """Patches 1+6+7: a listagem sai ANTES da tentativa de escrita; a falha
    (qualquer OSError/Unicode/csv.Error) vira exit 1 limpo e o evento registra
    ``escrita: falha`` — o log nunca afirma export concluída."""
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    ruim = Path(politeness_veloz.configs).parent / "nao-existe" / "catalogo.csv"

    resultado = cli.invoke(app, ["consultar", "--instituicao", "TST", "--saida", str(ruim)])

    assert resultado.exit_code == 1
    saida = saida_cli(resultado)
    assert "falha" in saida.lower(), "mensagem clara, sem traceback"
    assert "Resumo: 6 documento(s) listado(s)" in saida, (
        "resultado da consulta não é escondido pela falha de export"
    )
    assert not ruim.exists()
    assert _ultimo_evento(politeness_veloz.manifesto, "consultar_concluido")["escrita"] == (
        "falha"
    )


def test_custodia_falha_de_escrita_do_json_exit_1_e_evento_honesto(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """Custódia SEM fallback silencioso: falha de arquivo ⇒ exit 1 + evento
    ``escrita: falha`` (o JSON não vaza parcial no stdout)."""
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    ruim = Path(politeness_veloz.configs).parent / "nao-existe" / "custodia.json"

    resultado = cli.invoke(app, ["custodia", "--edital", _EDITAL_GEMEOS, "--saida", str(ruim)])

    assert resultado.exit_code == 1
    saida = saida_cli(resultado)
    assert "falha" in saida.lower()
    assert '"documentos"' not in saida, "JSON não vaza no stdout em falha de arquivo"
    assert not ruim.exists()
    assert _ultimo_evento(politeness_veloz.manifesto, "custodia_concluida")["escrita"] == ("falha")


# -- Custódia operacional: eventos finais e regressões da story ------------------------


def test_eventos_finais_registrados_apos_a_escrita_com_resultado_ok(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """Patch 1: evento registrado DEPOIS da tentativa de escrita, com o
    resultado honesto no detalhe."""
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    destino_csv = Path(politeness_veloz.configs).parent / "catalogo.csv"
    destino_json = Path(politeness_veloz.configs).parent / "custodia.json"

    assert cli.invoke(app, ["consultar", "--saida", str(destino_csv)]).exit_code == 0
    assert (
        cli.invoke(
            app, ["custodia", "--edital", _EDITAL_GEMEOS, "--saida", str(destino_json)]
        ).exit_code
        == 0
    )

    assert _ultimo_evento(politeness_veloz.manifesto, "consultar_concluido")["escrita"] == "ok"
    assert _ultimo_evento(politeness_veloz.manifesto, "custodia_concluida")["escrita"] == "ok"


def test_fila_decidir_sem_justificativa_continua_exit_2(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    _montar_corpus(cli, politeness_veloz, servidor_fake, criar_servidor_fake)
    (item,) = [
        i for i in fila_do_manifesto(politeness_veloz.manifesto) if i["status"] == "pendente"
    ]

    resultado = cli.invoke(app, ["fila", "decidir", "--id", str(item["id"]), "--ano", "2023"])

    assert resultado.exit_code == 2
    assert "ERRO" in saida_cli(resultado)
