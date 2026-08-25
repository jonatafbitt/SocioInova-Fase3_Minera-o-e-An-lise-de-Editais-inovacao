"""Cenários do I/O Matrix da datação multi-fonte (CAP-3) + fila humana (FR-8).

Fluxos REAIS: mapa validar → descobrir (âncora nasce na origem, FR-6) →
coletar → datar — contra o servidor fake local. PDFs com docinfo são
GERADOS em memória com pypdf (nada de binário commitado); os builders
compartilhados vivem em ``conftest``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agente_editais.consulta import app
from agente_editais.datacao import (
    _ano_de_data_docinfo,
    _anos_da_url,
    _anos_fora_da_janela,
    _anos_in_janela,
    _avaliar,
    datar_documento,
)
from agente_editais.manifest import Manifesto
from agente_editais.texto import ler_metadados

from .conftest import (
    coletar_pdfs,
    descobrir_coletar,
    documentos_do_manifesto,
    escrever_mapa,
    fila_do_manifesto,
    html_lista,
    longo,
    mapa_portal,
    pdf_com_docinfo,
    saida_cli,
    tipos_eventos,
    url_do,
)


# -- Infraestrutura local (builders compartilhados vivem no conftest) ----------------


def _documents_unica(caminho_manifesto) -> dict:
    linhas = documentos_do_manifesto(caminho_manifesto)
    assert len(linhas) == 1
    return linhas[0]


def _evidencias(caminho_manifesto, url: str) -> dict[str, str]:
    with Manifesto(caminho_manifesto) as manifesto:
        return {
            linha["fonte"]: linha["valor_bruto"]
            for linha in manifesto.evidencias_da_url(url)
        }


# -- Unidades: extração de ano e matriz de decisão -----------------------------------


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("D:20230512093000Z", 2023),
        ("D:20190101", 2019),
        ("D:20260101000000", 2026),
        ("2023-05-12T10:00:00", 2023),
        ("D:20180512093000Z", None),  # fora da janela ⇒ nunca ano candidato
        ("sem data aqui", None),
    ],
)
def test_ano_de_data_docinfo(bruto, esperado):
    assert _ano_de_data_docinfo(bruto) == esperado


def test_anos_in_janela_ignora_fora_da_janela():
    assert _anos_in_janela("edital_2023_v2") == {2023}
    assert _anos_in_janela("portaria 2027 e revisao 2018") == set(), (
        "_RE_ANO só casa 2019–2026 — fora da janela não entra na decisão"
    )


def test_anos_fora_da_janela_para_relatorio():
    assert _anos_fora_da_janela("revisao 2018 e prorrogacao 2027") == {2018, 2027}


def test_ano_da_url_restrito_ao_caminho():
    """Fonte url = SEGMENTOS DO CAMINHO (semântica da coleta): host e query
    não são evidência de publicação — nem dentro nem fora da janela."""
    assert _anos_da_url("https://edital2023.ifba.edu.br/busca?ano=2021") == (
        set(),
        set(),
    )
    assert _anos_da_url("http://x.org/2023/a.pdf?ref=2018") == ({2023}, set())
    assert _anos_da_url("http://x.org/docs/2017/a.pdf") == (set(), {2017})
    assert _anos_da_url("http://x.org/2021-1/x.pdf") == ({2021}, set())


def test_avaliar_so_ancora_e_fonte_desconhecida():
    """Só-âncora é aceito com metodo ancora; fonte futura fora da cascata
    (ex.: time_tag) não explode — cai em baixa confiança (guarda defensiva)."""
    assert _avaliar({"url": set(), "ancora": {2023}, "pdf_meta": set()}) == (
        2023,
        "ancora",
        None,
    )
    assert _avaliar({"time_tag": {2024}}) == (None, None, "baixa_confianca_sourl")
    assert _avaliar({"pdf_meta": {2024}, "time_tag": {2024}}) == (2024, "pdf_meta", None)


@pytest.mark.parametrize(
    ("anos_por_fonte", "esperado"),
    [
        ({}, (None, None, "sem_data")),
        ({"url": {2022}}, (None, None, "baixa_confianca_sourl")),
        ({"url": {2019}}, (None, None, "baixa_confianca_sourl")),  # limite 2019
        ({"url": {2026}}, (None, None, "baixa_confianca_sourl")),  # limite 2026
        ({"url": {2023}, "ancora": {2023}}, (2023, "url", None)),
        ({"url": {2023}, "pdf_meta": {2023}}, (2023, "url", None)),
        ({"ancora": {2022}, "pdf_meta": {2022}}, (2022, "ancora", None)),
        ({"pdf_meta": {2024}}, (2024, "pdf_meta", None)),
        ({"url": {2026}, "ancora": {2026}}, (2026, "url", None)),  # limite corroborado
        (
            {"url": {2021}, "ancora": {2023}, "pdf_meta": {2021}},
            (None, None, "divergencia"),
        ),
    ],
)
def test_avaliar_matriz_de_decisao(anos_por_fonte, esperado):
    assert _avaliar(anos_por_fonte) == esperado


def test_ler_metadados_tolerante(tmp_path):
    valido = tmp_path / "bom.pdf"
    valido.write_bytes(
        pdf_com_docinfo([longo("Doc com docinfo")], criado_em="D:20230101000000Z")
    )
    meta = ler_metadados(valido)
    assert meta is not None and meta["criado_em"].startswith("D:2023")

    sem_docinfo = tmp_path / "nu.pdf"
    sem_docinfo.write_bytes(pdf_com_docinfo([longo("Sem docinfo nenhum")]))
    meta_nu = ler_metadados(sem_docinfo)
    assert meta_nu is not None, "PDF válido SEM docinfo ≠ erro"
    assert meta_nu == {"titulo": None, "criado_em": None, "modificado_em": None}

    podre = tmp_path / "podre.pdf"
    podre.write_bytes(b"%PDF-1.4 lixo sem xref nem eof")
    assert ler_metadados(podre) is None, "bytes inválidos ⇒ None (erro do chamador)"

    sumido = tmp_path / "sumiu.pdf"
    assert ler_metadados(sumido) is None


# -- Cenário "Convergência": aceite com método primeiro da cascata -------------------


def test_convergencia_aceita_com_tres_evidencias_e_metodo_url(
    cli, politeness_veloz, servidor_fake
):
    descobrir_coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        html=html_lista([("/2023/edital-a.pdf", "Edital de Inovação 2023")]),
        pdfs={
            "/2023/edital-a.pdf": pdf_com_docinfo(
                [longo("Edital convergente")],
                criado_em="D:20230601000000Z",
                titulo="Edital 2023",
            )
        },
    )

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "aceitos: 1" in saida_cli(resultado)
    assert "fila: 0" in saida_cli(resultado)

    documento = documentos_do_manifesto(politeness_veloz.manifesto)[0]
    assert documento["metodo_datacao"] == "url", "primeiro da cascata que converge"
    assert documento["ano_aceito"] == 2023

    evidencias = _evidencias(politeness_veloz.manifesto, documento["url_origem"])
    assert sorted(evidencias) == ["ancora", "pdf_meta", "url"], "uma evidência por fonte"
    assert evidencias["ancora"] == "Edital de Inovação 2023", "âncora integral"
    assert "D:2023" in evidencias["pdf_meta"]

    eventos = tipos_eventos(politeness_veloz.manifesto)
    aplicadas = [detalhe for tipo, detalhe in eventos if tipo == "datacao_aplicada"]
    assert len(aplicadas) == 1
    assert aplicadas[0]["ano_aceito"] == 2023
    assert aplicadas[0]["metodo"] == "url"
    assert aplicadas[0]["fontes_consultadas"] == ["url", "ancora", "pdf_meta"]
    assert aplicadas[0]["total_evidencias"] == 3
    tipos = [tipo for tipo, _ in eventos]
    assert "datacao_portal_concluida" in tipos
    assert "datacao_fila" not in tipos


def test_wiring_ancora_descoberta_persiste_integral_e_evento_trunca(
    cli, politeness_veloz, servidor_fake
):
    ancora_longa = "Chamada complementar de Inovacao " + "detalhe " * 40 + "2023"
    descobrir_coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        html=html_lista([("/2023/com-link.pdf", ancora_longa)]),
        pdfs={
            "/2023/com-link.pdf": pdf_com_docinfo(
                [longo("Wiring da ancora")], criado_em="D:20230101000000Z"
            )
        },
    )

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        (candidato,) = manifesto.consultar(
            "SELECT texto_ancora FROM candidatos WHERE tipo = 'pdf'"
        )
    assert candidato["texto_ancora"] == ancora_longa, "banco guarda INTEGRAL"

    (_, detalhe) = next(
        (tipo, detalhe)
        for tipo, detalhe in tipos_eventos(politeness_veloz.manifesto)
        if tipo == "candidato_encontrado"
    )
    assert detalhe["ancora"] == ancora_longa[:200], "evento trunca em 200"


def test_wiring_sem_descoberta_candidato_fica_sem_ancora(
    cli, politeness_veloz, servidor_fake
):
    """Registro direto (sem descoberta) ⇒ âncora NULL = fonte indisponível."""
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/2023/direto.pdf": pdf_com_docinfo(
                [longo("Candidato registrado a mao")], criado_em="D:20230101000000Z"
            )
        },
    )
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        (candidato,) = manifesto.consultar("SELECT texto_ancora FROM candidatos")
    assert candidato["texto_ancora"] is None


# -- Cenário "Só-URL sem corroboração": fila baixa confiança -------------------------


def test_so_url_sem_corroboracao_vai_a_fila_baixa_confianca(
    cli, politeness_veloz, servidor_fake
):
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/2022/b.pdf": pdf_com_docinfo([longo("Sem ano no docinfo")])},
    )

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert "fila: 1" in saida_cli(resultado)

    documento = documentos_do_manifesto(politeness_veloz.manifesto)[0]
    assert documento["metodo_datacao"] is None and documento["ano_aceito"] is None

    itens = fila_do_manifesto(politeness_veloz.manifesto)
    assert len(itens) == 1
    assert itens[0]["motivo"] == "baixa_confianca_sourl"
    assert itens[0]["status"] == "pendente"
    assert itens[0]["url_origem"] == documento["url_origem"]

    filas = [detalhe for tipo, detalhe in tipos_eventos(politeness_veloz.manifesto) if tipo == "datacao_fila"]
    assert len(filas) == 1
    assert filas[0]["motivo"] == "baixa_confianca_sourl"
    assert filas[0]["divergencia"] is False
    assert filas[0]["anos_encontrados"] == [2022]


# -- Cenário "Divergência": três evidências + sinal de qualidade ----------------------


def test_divergencia_vai_a_fila_com_sinal_de_qualidade(
    cli, politeness_veloz, servidor_fake
):
    descobrir_coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        html=html_lista([("/2021/c.pdf", "Edital de Inovação 2023")]),
        pdfs={
            "/2021/c.pdf": pdf_com_docinfo(
                [longo("Divergencia entre fontes")], criado_em="D:20210215000000Z"
            )
        },
    )

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert "fila: 1" in saida_cli(resultado)

    documento = documentos_do_manifesto(politeness_veloz.manifesto)[0]
    assert documento["metodo_datacao"] is None, "divergência nunca é aceita sem voto"
    evidencias = _evidencias(politeness_veloz.manifesto, documento["url_origem"])
    assert sorted(evidencias) == ["ancora", "pdf_meta", "url"]

    itens = fila_do_manifesto(politeness_veloz.manifesto)
    assert len(itens) == 1 and itens[0]["motivo"] == "divergencia"

    filas = [detalhe for tipo, detalhe in tipos_eventos(politeness_veloz.manifesto) if tipo == "datacao_fila"]
    assert filas[0]["motivo"] == "divergencia"
    assert filas[0]["divergencia"] is True, "sinal de qualidade no evento"
    assert filas[0]["anos_encontrados"] == [2021, 2023]


# -- Cenário "Ano-limite só-URL": exige corroboração interna --------------------------


def test_ano_limite_so_url_2026_vai_a_fila(cli, politeness_veloz, servidor_fake):
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/2026/d.pdf": pdf_com_docinfo([longo("Limite da janela sem corroboracao")])},
    )

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    documento = documentos_do_manifesto(politeness_veloz.manifesto)[0]
    assert documento["metodo_datacao"] is None
    itens = fila_do_manifesto(politeness_veloz.manifesto)
    assert itens[0]["motivo"] == "baixa_confianca_sourl", (
        "ano-limite 2026 só-url exige corroboração interna — vai à fila"
    )


def test_limite_corroborado_por_pdf_meta_e_aceito(cli, politeness_veloz, servidor_fake):
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/limite/f.pdf": pdf_com_docinfo(
                [longo("Limite com corroboracao interna")],
                criado_em="D:20261231000000Z",
            )
        },
    )

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    documento = _documents_unica(politeness_veloz.manifesto)
    assert documento["ano_aceito"] == 2026
    assert documento["metodo_datacao"] == "pdf_meta", (
        "a url não produz ano — a primeira da cascata que converge é pdf_meta"
    )


# -- Cenário "Ano fora da janela": evidência registrada, nunca aceito -----------------


def test_ano_fora_da_janela_no_pdf_meta_vira_evidencia_e_fila(
    cli, politeness_veloz, servidor_fake
):
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/docs/e.pdf": pdf_com_docinfo(
                [longo("Publicacao antiga fora da janela")],
                criado_em="D:20180301000000Z",
            )
        },
    )

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    documento = _documents_unica(politeness_veloz.manifesto)
    assert documento["metodo_datacao"] is None, "fora da janela NUNCA é aceito"

    evidencias = _evidencias(politeness_veloz.manifesto, documento["url_origem"])
    assert "D:2018" in evidencias["pdf_meta"], "valor bruto preservado como evidência"

    itens = fila_do_manifesto(politeness_veloz.manifesto)
    assert itens[0]["motivo"] == "sem_data", "caso único fora da janela motiva a fila"
    filas = [detalhe for tipo, detalhe in tipos_eventos(politeness_veloz.manifesto) if tipo == "datacao_fila"]
    assert filas[0]["anos_fora_da_janela"] == [2018], "sinal de qualidade"


def test_ano_fora_da_janela_na_url_nao_e_aceito(tmp_path):
    """URL com 2027: regex da janela não casa ⇒ S vazio ⇒ fila sem_data."""
    assert _anos_in_janela("/2027/h.pdf") == set()
    assert _avaliar({"url": set(), "pdf_meta": set()}) == (None, None, "sem_data")


# -- Cenário "Sem data alguma": fila, nada descartado ---------------------------------


def test_sem_data_alguma_enfileira_sem_descartar(cli, politeness_veloz, servidor_fake):
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/docs/g.pdf": pdf_com_docinfo([longo("Nenhuma fonte produz ano")])},
    )

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    documento = _documents_unica(politeness_veloz.manifesto)
    assert documento["id"], "documento segue íntegro no L1 — nada descartado"
    assert documento["metodo_datacao"] is None
    evidencias = _evidencias(politeness_veloz.manifesto, documento["url_origem"])
    assert sorted(evidencias) == ["pdf_meta", "url"], "fontes consultadas provadas"
    itens = fila_do_manifesto(politeness_veloz.manifesto)
    assert itens[0]["motivo"] == "sem_data"


# -- Cenário "Retomada idempotente": pulados no resumo, zero re-decisões --------------


def test_reexecucao_idempotente_pula_datados_e_preserva_fila(
    cli, politeness_veloz, servidor_fake
):
    descobrir_coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        html=html_lista(
            [
                ("/2023/a.pdf", "Edital de Inovação 2023"),
                ("/docs/b.pdf", "Documento sem ano nenhum"),
            ]
        ),
        pdfs={
            "/2023/a.pdf": pdf_com_docinfo(
                [longo("Convergente para aceite")], criado_em="D:20230101000000Z"
            ),
            "/docs/b.pdf": pdf_com_docinfo([longo("Vai para a fila sem data")]),
        },
    )

    primeira = cli.invoke(app, ["datar", "--portal", "TST"])
    assert primeira.exit_code == 0, primeira.output
    assert "aceitos: 1" in saida_cli(primeira) and "fila: 1" in saida_cli(primeira)
    eventos_primeira = [tipo for tipo, _ in tipos_eventos(politeness_veloz.manifesto)]
    assert eventos_primeira.count("datacao_aplicada") == 1
    assert eventos_primeira.count("datacao_fila") == 1

    segunda = cli.invoke(app, ["datar", "--todos"])

    assert segunda.exit_code == 0, segunda.output
    saida = saida_cli(segunda)
    assert "pulados: 2" in saida, "resumo mostra os pulados"
    assert "aceitos: 0" in saida and "fila: 0" in saida
    eventos_segunda = [tipo for tipo, _ in tipos_eventos(politeness_veloz.manifesto)]
    assert eventos_segunda.count("datacao_aplicada") == 1, "zero retrabalho"
    assert eventos_segunda.count("datacao_fila") == 1, "nenhuma re-decisão"
    assert len(fila_do_manifesto(politeness_veloz.manifesto)) == 1, "fila permanece intacta"


def test_decisao_humana_impede_reprocessamento_na_retomada(
    cli, politeness_veloz, servidor_fake
):
    """Item resolvido na fila também trava o reprocessamento (zero re-decisões)."""
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/docs/r.pdf": pdf_com_docinfo([longo("Fila decidida por humana")])},
    )
    assert cli.invoke(app, ["datar", "--portal", "TST"]).exit_code == 0
    (item,) = fila_do_manifesto(politeness_veloz.manifesto)

    decisao = cli.invoke(
        app,
        [
            "fila", "decidir",
            "--id", str(item["id"]),
            "--ano", "2023",
            "--justificativa", "Capa do PDF declara publicação em 2023.",
            "--autor", "Pesquisadora",
            "--evidencia", "capa página 1",
        ],
    )
    assert decisao.exit_code == 0, decisao.output

    retomada = cli.invoke(app, ["datar", "--portal", "TST"])

    assert retomada.exit_code == 0, retomada.output
    assert "pulados: 1" in saida_cli(retomada), "decisão humana não é refeta"
    assert len(fila_do_manifesto(politeness_veloz.manifesto)) == 1, "nenhum item duplicado"
    tipos = [tipo for tipo, _ in tipos_eventos(politeness_veloz.manifesto)]
    assert "datacao_fila" not in tipos[tipos.index("fila_decidida") + 1:], (
        "nenhum novo enfileiramento após a decisão"
    )


# -- Cenário "PDF sumido/corrompido pós-coleta": erro isolado, lote segue -------------


def test_pdf_corrompido_ou_sumido_vira_datacao_erro_e_lote_segue(
    cli, politeness_veloz, servidor_fake
):
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/quebrado/x.pdf": pdf_com_docinfo(
                [longo("Este sera corrompido depois")], criado_em="D:20220101000000Z"
            ),
            "/sumido/s.pdf": pdf_com_docinfo(
                [longo("Este sera apagado depois")], criado_em="D:20220101000000Z"
            ),
            "/integro/i.pdf": pdf_com_docinfo(
                [longo("Integro e convergente")], criado_em="D:20230101000000Z"
            ),
        },
    )
    documentos = documentos_do_manifesto(politeness_veloz.manifesto)
    corrompido = next(d for d in documentos if d["url_origem"].endswith("x.pdf"))
    sumido = next(d for d in documentos if d["url_origem"].endswith("s.pdf"))
    Path(corrompido["caminho"]).write_bytes(b"%PDF-1.4 lixo sem xref nem eof")
    Path(sumido["caminho"]).unlink()

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, saida_cli(resultado) + " — lote NUNCA aborta"
    saida = saida_cli(resultado)
    assert "erros: 2" in saida
    assert "aceitos: 1" in saida
    perdidas_cli = [linha for linha in saida.splitlines() if linha.strip().startswith("Perdida:")]
    assert len(perdidas_cli) == 2, "cada perda aparece no eco do portal"
    for url in (corrompido["url_origem"], sumido["url_origem"]):
        assert any(url in linha for linha in perdidas_cli), f"perdida ausente: {url}"

    eventos = tipos_eventos(politeness_veloz.manifesto)
    erros = [
        detalhe
        for tipo, detalhe in eventos
        if tipo == "datacao_erro"
    ]
    assert len(erros) == 2
    assert all(erro["fase"] == "leitura" for erro in erros)
    por_url = {erro["url"]: erro for erro in erros}
    assert por_url[corrompido["url_origem"]]["existente"] is True
    assert por_url[sumido["url_origem"]]["existente"] is False

    esperadas = sorted([corrompido["url_origem"], sumido["url_origem"]])
    portal_concluida = next(
        detalhe for tipo, detalhe in eventos if tipo == "datacao_portal_concluida"
    )
    assert sorted(portal_concluida["urls_perdidas"]) == esperadas, (
        "padrão dos irmãos: perdas agregadas no evento do portal"
    )
    datar_concluido = next(
        detalhe for tipo, detalhe in eventos if tipo == "datar_concluido"
    )
    assert sorted(datar_concluido["urls_perdidas"]) == esperadas

    (integro,) = [
        doc
        for doc in documentos_do_manifesto(politeness_veloz.manifesto)
        if doc["url_origem"].endswith("i.pdf")
    ]
    assert integro["metodo_datacao"] == "pdf_meta", "demais documentos processados"


# -- Alias intra-portal: linha própria, evidências próprias ---------------------------


def test_alias_e_datado_como_linha_propria(cli, politeness_veloz, servidor_fake):
    corpo = pdf_com_docinfo(
        [longo("Mesmos bytes em duas urls com docinfo")], criado_em="D:20230101000000Z"
    )
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/2023/um.pdf": corpo, "/2023/dois.pdf": corpo},
    )
    assert len(documentos_do_manifesto(politeness_veloz.manifesto)) == 2, "canônico + alias"

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "aceitos: 2" in saida_cli(resultado)
    linhas = documentos_do_manifesto(politeness_veloz.manifesto)
    assert all(doc["ano_aceito"] == 2023 for doc in linhas)
    for doc in linhas:
        evidencias = _evidencias(politeness_veloz.manifesto, doc["url_origem"])
        assert sorted(evidencias) == ["pdf_meta", "url"], "evidências POR linha"


# -- Fila: listar e decidir -----------------------------------------------------------


def _enfileirar_um(cli, politeness_veloz, servidor_fake) -> dict:
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/docs/q.pdf": pdf_com_docinfo([longo("Sem data para a fila")])},
    )
    assert cli.invoke(app, ["datar", "--portal", "TST"]).exit_code == 0
    return fila_do_manifesto(politeness_veloz.manifesto)[0]


def test_fila_listar_mostra_motivo_e_evidencias(cli, politeness_veloz, servidor_fake):
    item = _enfileirar_um(cli, politeness_veloz, servidor_fake)

    resultado = cli.invoke(app, ["fila", "listar"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output
    assert f"#{item['id']}" in saida
    assert item["url_origem"] in saida
    assert "motivo=sem_data" in saida
    assert "url (documentos.url_origem)" in saida
    assert "pdf_meta (docinfo)" in saida

    resolvida = cli.invoke(app, ["fila", "listar", "--status", "resolvida"])
    assert resolvida.exit_code == 0
    assert "(nada a decidir aqui)" in resolvida.output

    todas = cli.invoke(app, ["fila", "listar", "--status", "todas"])
    assert todas.exit_code == 0
    assert item["url_origem"] in todas.output, "--status todas inclui pendentes"

    invalido = cli.invoke(app, ["fila", "listar", "--status", "inventado"])
    assert invalido.exit_code == 2


def test_fila_decidir_ano_grava_decisao_completa_e_evento(
    cli, politeness_veloz, servidor_fake
):
    item = _enfileirar_um(cli, politeness_veloz, servidor_fake)

    resultado = cli.invoke(
        app,
        [
            "fila", "decidir",
            "--id", str(item["id"]),
            "--ano", "2023",
            "--justificativa", "Capa do PDF declara publicação em 2023.",
            "--autor", "Pesquisadora",
            "--evidencia", "capa página 1",
        ],
    )

    assert resultado.exit_code == 0, resultado.output
    (decidido,) = fila_do_manifesto(politeness_veloz.manifesto)
    assert decidido["status"] == "resolvida"
    assert decidido["decidido_ano"] == 2023
    assert decidido["decidido_exclusao"] == 0
    assert decidido["justificativa"] == "Capa do PDF declara publicação em 2023."
    assert decidido["autor"] == "Pesquisadora"
    assert decidido["evidencia_anexa"] == "capa página 1"
    assert decidido["decidido_em"]

    eventos = [detalhe for tipo, detalhe in tipos_eventos(politeness_veloz.manifesto) if tipo == "fila_decidida"]
    assert len(eventos) == 1
    assert eventos[0]["fila_id"] == item["id"]
    assert eventos[0]["decidido_ano"] == 2023
    assert eventos[0]["autor"] == "Pesquisadora"
    assert eventos[0]["motivo_original"] == "sem_data"
    assert eventos[0]["justificativa"] == (
        "Capa do PDF declara publicação em 2023."
    ), "cadeia append-only completa (FR-8/§10)"
    assert eventos[0]["evidencia_anexa"] == "capa página 1"

    listar = cli.invoke(app, ["fila", "listar", "--status", "resolvida"])
    assert "ANO 2023" in listar.output
    assert "Capa do PDF declara" in listar.output

    todas = cli.invoke(app, ["fila", "listar", "--status", "todas"])
    assert todas.exit_code == 0
    assert "[resolvida]" in todas.output and item["url_origem"] in todas.output


def test_fila_decidir_exclusao_grava_exclusao(cli, politeness_veloz, servidor_fake):
    item = _enfileirar_um(cli, politeness_veloz, servidor_fake)

    resultado = cli.invoke(
        app,
        [
            "fila", "decidir",
            "--id", str(item["id"]),
            "--excluir",
            "--justificativa", "Documento duplicado de outro edital.",
            "--autor", "Curadoria",
        ],
    )

    assert resultado.exit_code == 0, resultado.output
    (decidido,) = fila_do_manifesto(politeness_veloz.manifesto)
    assert decidido["status"] == "resolvida"
    assert decidido["decidido_exclusao"] == 1
    assert decidido["decidido_ano"] is None
    listar = cli.invoke(app, ["fila", "listar", "--status", "resolvida"])
    assert "EXCLUSÃO" in listar.output


def test_item_resolvido_recusa_segunda_decisao_exit_1(
    cli, politeness_veloz, servidor_fake
):
    item = _enfileirar_um(cli, politeness_veloz, servidor_fake)
    assert (
        cli.invoke(
            app,
            [
                "fila", "decidir", "--id", str(item["id"]), "--ano", "2023",
                "--justificativa", "Primeira decisão.", "--autor", "A",
            ],
        ).exit_code
        == 0
    )

    segunda = cli.invoke(
        app,
        [
            "fila", "decidir", "--id", str(item["id"]), "--ano", "2024",
            "--justificativa", "Tentativa de mudar a decisão.", "--autor", "B",
        ],
    )

    assert segunda.exit_code == 1
    (unico,) = fila_do_manifesto(politeness_veloz.manifesto)
    assert unico["decidido_ano"] == 2023, "decisão original intacta"

    inexistente = cli.invoke(
        app,
        [
            "fila", "decidir", "--id", "999", "--ano", "2023",
            "--justificativa", "Fantasma.", "--autor", "A",
        ],
    )
    assert inexistente.exit_code == 1


@pytest.mark.parametrize(
    "argumentos",
    [
        [],  # sem nada
        ["--ano", "2023"],  # sem justificativa (ausente)
        ["--ano", "2023", "--justificativa", ""],  # matriz: justificativa vazia
        ["--ano", "2023", "--justificativa", "   "],
        ["--ano", "2023", "--justificativa", "ok"],  # sem autor
        ["--ano", "2023", "--justificativa", "ok", "--autor", ""],
        ["--justificativa", "ok", "--autor", "A"],  # sem destino
        ["--ano", "2023", "--excluir", "--justificativa", "ok", "--autor", "A"],
        ["--ano", "2018", "--justificativa", "ok", "--autor", "A"],  # fora da janela
        ["--ano", "2027", "--justificativa", "ok", "--autor", "A"],
    ],
)
def test_decisao_invalida_recusada_exit_2_nada_gravado(
    cli, politeness_veloz, servidor_fake, argumentos
):
    item = _enfileirar_um(cli, politeness_veloz, servidor_fake)

    resultado = cli.invoke(app, ["fila", "decidir", "--id", str(item["id"]), *argumentos])

    assert resultado.exit_code == 2, resultado.output
    assert "ERRO" in saida_cli(resultado)
    (item_depois,) = fila_do_manifesto(politeness_veloz.manifesto)
    assert item_depois["status"] == "pendente", "nada gravado"
    assert item_depois["justificativa"] is None and item_depois["autor"] is None
    tipos = [tipo for tipo, _ in tipos_eventos(politeness_veloz.manifesto)]
    assert "fila_decidida" not in tipos


# -- Unidades de datar_documento: falha de persistência e leitura ---------------------


def _manifesto_unitario(tmp_path, *, url: str, corpo: bytes, ancora: str | None = None):
    """Manifesto mínimo com portal + documento REAL em disco (para unidades)."""
    pasta = tmp_path
    pasta.mkdir(parents=True, exist_ok=True)
    pdf = pasta / "doc.pdf"
    pdf.write_bytes(corpo)
    manifesto = Manifesto(pasta / "m.sqlite3")
    manifesto.executar(
        "INSERT INTO instituicoes (sigla, nome, criado_em) "
        "VALUES ('UNT', 'Unidade', '2026-01-01T00:00:00+00:00')"
    )
    manifesto.executar(
        """
        INSERT INTO portais (
            instituicao_id, nome, categoria, url, dinamico,
            profundidade_maxima, criado_em
        ) VALUES (1, 'Portal UNT', 'integra', 'http://x.org', 0, 3,
                  '2026-01-01T00:00:00+00:00')
        """
    )
    manifesto.executar(
        """
        INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
        VALUES ('unt-doc', 1, NULL, '2026-01-01T00:00:00+00:00')
        """
    )
    manifesto.executar(
        """
        INSERT INTO documentos (
            id, edital_id, url_origem, caminho, hash_sha256, data_captura,
            ano_provisorio, versao_crawler
        ) VALUES ('unit1234567', 'unt-doc', ?, ?, ?, '2026-01-01T00:00:00+00:00',
                  NULL, '0.1.0')
        """,
        (url, str(pdf), hashlib.sha256(corpo).hexdigest()),
    )
    if ancora is not None:
        manifesto.executar(
            """
            INSERT INTO candidatos (portal_id, url, tipo, texto_ancora, descoberto_em)
            VALUES (1, ?, 'pdf', ?, '2026-01-01T00:00:00+00:00')
            """,
            (url, ancora),
        )
    documento = manifesto.consultar("SELECT * FROM documentos")[0]
    return manifesto, documento


def test_datar_documento_metodo_ancora_quando_url_nao_produz_ano(tmp_path):
    """Cascata FR-7: url sem ano + âncora/pdf convergentes ⇒ método = ancora."""
    corpo = pdf_com_docinfo(
        [longo("Ancora decide")], criado_em="D:20230101000000Z"
    )
    manifesto, documento = _manifesto_unitario(
        tmp_path,
        url="http://x.org/pagina/doc.pdf",
        corpo=corpo,
        ancora="Chamada de Inovação 2023",
    )
    try:
        desfecho = datar_documento(documento, manifesto, portal_id=1)
        assert desfecho.desfecho == "aceito"
        assert desfecho.metodo == "ancora", "primeira da cascata que produz o consenso"
        assert desfecho.ano == 2023
        (linha,) = manifesto.consultar(
            "SELECT metodo_datacao, ano_aceito FROM documentos"
        )
        assert linha["metodo_datacao"] == "ancora" and linha["ano_aceito"] == 2023
    finally:
        manifesto.fechar()


def test_datar_documento_arquivo_ausente_vira_erro_leitura(tmp_path):
    corpo = pdf_com_docinfo([longo("Sumira do disco")], criado_em="D:20230101000000Z")
    manifesto, documento = _manifesto_unitario(
        tmp_path, url="http://x.org/2023/doc.pdf", corpo=corpo
    )
    try:
        Path(documento["caminho"]).unlink()
        desfecho = datar_documento(documento, manifesto, portal_id=1)
        assert desfecho.desfecho == "erro"
        erros = [
            detalhe
            for tipo, detalhe in tipos_eventos(manifesto.caminho)
            if tipo == "datacao_erro"
        ]
        assert erros[0]["fase"] == "leitura"
        assert erros[0]["existente"] is False
    finally:
        manifesto.fechar()


def test_datar_documento_falha_de_persistencia_vira_erro_e_lote_segue(
    tmp_path, monkeypatch
):
    corpo = pdf_com_docinfo(
        [longo("Persistencia recusada")], criado_em="D:20230101000000Z"
    )
    manifesto, documento = _manifesto_unitario(
        tmp_path, url="http://x.org/2023/doc.pdf", corpo=corpo
    )

    def _recusando(_self, *_args, **_kwargs):
        return False

    monkeypatch.setattr(Manifesto, "aplicar_datacao", _recusando)
    try:
        desfecho = datar_documento(documento, manifesto, portal_id=1)
        assert desfecho.desfecho == "erro"
        erros = [
            detalhe
            for tipo, detalhe in tipos_eventos(manifesto.caminho)
            if tipo == "datacao_erro"
        ]
        assert erros[0]["fase"] == "persistencia"
    finally:
        manifesto.fechar()


def test_datar_documento_enfileiramento_recusado_vira_erro_fila_duplicada(
    tmp_path, monkeypatch
):
    """enfileirar=False NÃO é sucesso mudo nem conta como fila: vira erro com
    fase 'fila_duplicada' e a retomada reprocessa com a fila já visível."""
    corpo = pdf_com_docinfo([longo("Sem data e sem vaga na fila")])
    manifesto, documento = _manifesto_unitario(
        tmp_path, url="http://x.org/docs/doc.pdf", corpo=corpo
    )

    def _recusando(_self, *_args, **_kwargs):
        return False

    monkeypatch.setattr(Manifesto, "enfileirar", _recusando)
    try:
        desfecho = datar_documento(documento, manifesto, portal_id=1)

        assert desfecho.desfecho == "erro", "não é desfecho 'fila'"
        tipos = [
            (tipo, detalhe)
            for tipo, detalhe in tipos_eventos(manifesto.caminho)
        ]
        erros = [detalhe for tipo, detalhe in tipos if tipo == "datacao_erro"]
        assert len(erros) == 1 and erros[0]["fase"] == "fila_duplicada"
        assert not [d for tipo, d in tipos if tipo == "datacao_fila"], (
            "nenhum evento de fila sem linha enfileirada"
        )
        pendentes = manifesto.consultar("SELECT COUNT(*) FROM fila_revisao")[0][0]
        assert pendentes == 0
    finally:
        manifesto.fechar()


# -- Superfície CLI --------------------------------------------------------------------


def test_flags_mutuamente_exclusivas_no_datar(cli, politeness_veloz) -> None:
    assert cli.invoke(app, ["datar"]).exit_code == 2
    assert cli.invoke(app, ["datar", "--portal", "TST", "--todos"]).exit_code == 2
    assert cli.invoke(app, ["datar", "--portal", ""]).exit_code == 2


def test_datar_sigla_desconhecida_sai_1(cli, politeness_veloz, servidor_fake) -> None:
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    resultado = cli.invoke(app, ["datar", "--portal", "XXX"])
    assert resultado.exit_code == 1
    saida = saida_cli(resultado)
    assert "XXX" in saida and "TST" in saida


def test_datar_recusa_portal_orfao_nao_sincronizado(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake
):
    orfao = criar_servidor_fake()  # NUNCA passa por 'mapa validar'
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    escrever_mapa(
        politeness_veloz,
        mapa_portal(servidor_fake) + "\n" + mapa_portal(orfao, sigla="ORF"),
    )

    resultado = cli.invoke(app, ["datar", "--todos"])

    assert resultado.exit_code == 1, resultado.output
    saida = saida_cli(resultado)
    assert "[ORF]" in saida and url_do(orfao) in saida
    assert "mapa validar" in saida


def test_datar_sem_documentos_sai_zero(cli, politeness_veloz, servidor_fake) -> None:
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "Documentos: 0" in saida_cli(resultado)


def test_datar_nao_exige_janela_off_peak(
    cli, politeness_veloz, servidor_fake, monkeypatch
):
    """Datação é 100% local (AD-11/AD-5: zero rede) — roda fora da janela."""
    from agente_editais import consulta

    monkeypatch.setattr(consulta, "dentro_da_janela_off_peak", lambda *_a, **_k: False)
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/2023/off.pdf": pdf_com_docinfo(
                [longo("Offline converge")], criado_em="D:20230101000000Z"
            )
        },
    )

    resultado = cli.invoke(app, ["datar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "aceitos: 1" in saida_cli(resultado)


def test_status_mostra_contagens_de_datacao(cli, politeness_veloz, servidor_fake):
    """Métrica de cobertura: automáticos + decididos em fila (com ano)."""
    item = _enfileirar_um(cli, politeness_veloz, servidor_fake)

    antes = cli.invoke(app, ["status"])
    assert antes.exit_code == 0, antes.output
    saida_antes = saida_cli(antes)
    assert "Datados (CAP-3): 0 (automáticos + decididos em fila)" in saida_antes
    assert "Fila revisão:    1 pendente(s), 0 resolvida(s)" in saida_antes

    decisao = cli.invoke(
        app,
        [
            "fila", "decidir",
            "--id", str(item["id"]),
            "--ano", "2022",
            "--justificativa", "Processo de 2022 no sistema do IF.",
            "--autor", "Pesquisadora",
        ],
    )
    assert decisao.exit_code == 0, decisao.output

    depois = cli.invoke(app, ["status"])

    assert depois.exit_code == 0, depois.output
    saida = saida_cli(depois)
    assert (
        "Datados (CAP-3): 1 (automáticos + decididos em fila)" in saida
    ), "decisão humana com ano entra na cobertura"
    assert "Fila revisão:    0 pendente(s), 1 resolvida(s)" in saida
