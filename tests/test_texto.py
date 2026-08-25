"""Cenários do I/O Matrix para o texto (CAP-6): extrator único (AD-11),
.txt irmão, flag de escaneado pelo limiar e retomada idempotente.

Fixtures de PDF são GERADAS em memória com pypdf (Design Notes da story):
nada de binário commitado. O fluxo de teste usa a pipeline real — coletar
contra o servidor fake local — para nascerem Documentos legítimos no L1.
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PageObject, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from agente_editais.consulta import app
from agente_editais.fetcher import carregar_polidez
from agente_editais.manifest import Manifesto
from agente_editais.texto import (
    _texto_da_pagina,
    caminho_txt_do,
    extrair_documento,
    ja_extraido,
)

from .conftest import CONFIGS_DO_REPO, escrever_mapa, url_do


# -- fixtures de PDF geradas em memória -------------------------------------------


def _pagina_com_texto(escritor: PdfWriter, conteudo: str) -> PageObject:
    """Página com stream clássico 'BT/Tj/ET' — texto extraível pelo pypdf."""
    pagina = PageObject.create_blank_page(None, 612, 792)
    fluxo = DecodedStreamObject()
    fluxo.set_data(f"BT /F1 24 Tf 72 720 Td ({conteudo}) Tj ET".encode("latin-1"))
    referencia_fluxo = escritor._add_object(fluxo)
    fonte = DictionaryObject()
    fonte[NameObject("/Type")] = NameObject("/Font")
    fonte[NameObject("/Subtype")] = NameObject("/Type1")
    fonte[NameObject("/BaseFont")] = NameObject("/Helvetica")
    referencia_fonte = escritor._add_object(fonte)
    recursos = DictionaryObject()
    recursos[NameObject("/Font")] = DictionaryObject(
        {NameObject("/F1"): referencia_fonte}
    )
    pagina[NameObject("/Resources")] = recursos
    pagina[NameObject("/Contents")] = referencia_fluxo
    return pagina


def pdf_com_texto(paginas: list[str]) -> bytes:
    """PDF nativo mínimo com N páginas de texto real."""
    escritor = PdfWriter()
    for conteudo in paginas:
        escritor.add_page(_pagina_com_texto(escritor, conteudo))
    buffer = BytesIO()
    escritor.write(buffer)
    return buffer.getvalue()


def pdf_apenas_imagem(paginas: int = 2) -> bytes:
    """Simula PDF escaneado: páginas SEM stream de texto (extração ~0 chars)."""
    escritor = PdfWriter()
    for _ in range(paginas):
        escritor.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    escritor.write(buffer)
    return buffer.getvalue()


def _longo(semente: str) -> str:
    """Página 'nativa': texto ACIMA do limiar default (100 chars/página)."""
    return (
        f"{semente} - trecho de conteudo textual para extracao "
        "acima do limiar de escaneamento configurado no repositorio"
    ) * 2


# -- infraestrutura compartilhada ---------------------------------------------------


@pytest.fixture
def corpus(politeness_veloz, monkeypatch):
    raiz = politeness_veloz.configs.parent / "corpus"
    monkeypatch.setenv("AGENTE_EDITAIS_CORPUS", str(raiz))
    return raiz


def _mapa_portal(servidor, *, sigla: str = "TST") -> str:
    return (
        f'[[instituicao]]\nsigla = "{sigla}"\nnome = "Instituto de Teste {sigla}"\n\n'
        f'  [[instituicao.portal]]\n  nome = "Portal {sigla}"\n'
        f'  categoria = "integra"\n  url = "{url_do(servidor)}"\n'
        f'  seeds = ["{url_do(servidor)}"]\n'
    )


def _registrar_candidatos(servidor, caminho_manifesto, caminhos: list[str]) -> None:
    with Manifesto(caminho_manifesto) as manifesto:
        portal_id = manifesto.id_portal_por_url(url_do(servidor))
        assert portal_id is not None, "rode 'mapa validar' antes"
        for caminho in caminhos:
            assert manifesto.registrar_candidato(
                portal_id, url_do(servidor, caminho), "pdf"
            )


def _coletar(cli, politeness_veloz, servidor_fake, corpos: dict[str, bytes]) -> None:
    """Pipeline real até o L1: mapa validar → candidatos → coletar."""
    for caminho, corpo in corpos.items():
        servidor_fake.paginas[caminho] = (200, "application/pdf", corpo)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, list(corpos))
    assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0


def _documentos(caminho_manifesto) -> list[dict]:
    with Manifesto(caminho_manifesto) as manifesto:
        return [
            dict(linha)
            for linha in manifesto.consultar(
                "SELECT * FROM documentos ORDER BY url_origem"
            )
        ]


def _tipos_eventos(caminho_manifesto) -> list[tuple[str, dict]]:
    with Manifesto(caminho_manifesto) as manifesto:
        return [
            (linha["tipo"], json.loads(linha["detalhe"]))
            for linha in manifesto.consultar(
                "SELECT tipo, detalhe FROM eventos ORDER BY id"
            )
        ]


def _saida(resultado) -> str:
    return resultado.output + (resultado.stderr or "")


def _documento_em(pasta: Path, corpo_pdf: bytes) -> tuple[Manifesto, dict]:
    """Manifesto mínimo + linha de Documento apontando para um PDF real.

    O hash_sha256 é o REAL dos bytes — ``ja_extraido`` (retomada) compara-o
    contra o disco exatamente como em produção.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    pdf = pasta / "doc.pdf"
    pdf.write_bytes(corpo_pdf)
    manifesto = Manifesto(pasta / "m.sqlite3")
    manifesto.executar(
        "INSERT INTO instituicoes (sigla, nome, criado_em) "
        "VALUES ('UNT', 'Unidade', '2026-01-01T00:00:00+00:00')"
    )
    manifesto.executar(
        """
        INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
        VALUES ('unt-sem_ano-doc', 1, NULL, '2026-01-01T00:00:00+00:00')
        """
    )
    manifesto.executar(
        """
        INSERT INTO documentos (
            id, edital_id, url_origem, caminho, hash_sha256, data_captura,
            ano_provisorio, versao_crawler
        ) VALUES ('unit1234567', 'unt-sem_ano-doc', 'http://x.org/doc.pdf',
                  ?, ?, '2026-01-01T00:00:00+00:00', NULL, '0.1.0')
        """,
        (str(pdf), hashlib.sha256(corpo_pdf).hexdigest()),
    )
    return manifesto, dict(manifesto.consultar("SELECT * FROM documentos")[0])


# -- Cenário "PDF nativo válido": .txt irmão, flag false, eventos, exit 0 ----------


def test_tres_nativos_geram_txt_irmaos_flags_false_e_eventos(
    cli, politeness_veloz, servidor_fake, corpus
):
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/2023/edital-a.pdf": pdf_com_texto([_longo("Edital de Inovacao numero um")]),
            "/editais/b.pdf": pdf_com_texto(
                [_longo("Conteudo nativo da pagina"), _longo("Segunda pagina com texto")]
            ),
            "/docs/c.pdf": pdf_com_texto([
                _longo("Terceiro edital com texto suficiente")
            ]),
        },
    )

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "extraídos: 3" in _saida(resultado)
    assert "erros: 0" in _saida(resultado)

    documentos = _documentos(politeness_veloz.manifesto)
    assert len(documentos) == 3
    for documento in documentos:
        pdf = Path(documento["caminho"])
        txt = documento["texto_caminho"]
        assert txt is not None and txt.endswith(".txt"), ".txt é irmão do PDF"
        assert Path(txt) == pdf.with_suffix(".txt"), (
            "mesmo nome/caminho do PDF, só muda a extensão"
        )
        assert documento["flag_escaneado"] == 0, "nativo ⇒ flag false"
        assert documento["extraido_em"], "proveniência gravada"
        conteudo = Path(txt).read_text(encoding="utf-8")
        nome_pdf = pdf.name
        esperado_paginas = next(
            paginas
            for sufixo, paginas in (
                ("-edital-a.pdf", 1),
                ("-b.pdf", 2),
                ("-c.pdf", 1),
            )
            if nome_pdf.endswith(sufixo)
        )
        assert documento["texto_paginas"] == esperado_paginas
        # chars EXTRAÍDOS: conteúdo do txt menos os separadores entre páginas
        assert documento["texto_chars"] == len(conteudo) - (esperado_paginas - 1), (
            "contagem sem separadores confere com o gravado"
        )
        assert documento["texto_chars"] >= 20, "texto real foi extraído"

    eventos = _tipos_eventos(politeness_veloz.manifesto)
    tipos = [tipo for tipo, _ in eventos]
    assert tipos.count("texto_extraido") == 3
    assert "texto_escaneado" not in tipos
    assert "texto_portal_concluida" in tipos

    concluido = next(detalhe for tipo, detalhe in eventos if tipo == "textuar_concluido")
    assert concluido["portais"] == 1
    assert concluido["alvo"] == "TST"
    assert concluido["limiar_chars_por_pagina"] == 100
    assert len(concluido["mapa_sha256"]) == 64
    assert len(concluido["politeness_sha256"]) == 64
    assert concluido["totais"] == {
        "documentos": 3,
        "extraidos": 3,
        "escaneados": 0,
        "erros": 0,
        "pulados": 0,
    }
    assert concluido["urls_perdidas"] == []


# -- Cenário "PDF escaneado": flag true + evento texto_escaneado --------------------


def test_pdf_apenas_imagem_recebe_flag_escaneado_e_evento(
    cli, politeness_veloz, servidor_fake, corpus
):
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/scan/antigo.pdf": pdf_apenas_imagem(paginas=2),
            "/nativo/bom.pdf": pdf_com_texto([_longo("Documento nativo com bastante texto")]),
        },
    )

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "escaneados: 1" in _saida(resultado)
    assert "extraídos: 1" in _saida(resultado)

    por_nome = {
        Path(doc["caminho"]).name.rsplit("-", 1)[-1]: doc
        for doc in _documentos(politeness_veloz.manifesto)
    }
    escaneado = next(
        doc
        for doc in _documentos(politeness_veloz.manifesto)
        if Path(doc["caminho"]).name.endswith("-antigo.pdf")
    )
    nativo = next(
        doc
        for doc in _documentos(politeness_veloz.manifesto)
        if Path(doc["caminho"]).name.endswith("-bom.pdf")
    )
    assert escaneado["flag_escaneado"] == 1
    assert escaneado["texto_chars"] < escaneado["texto_paginas"] * 100, (
        "abaixo do limiar default (100 × nº páginas)"
    )
    assert (
        Path(escaneado["texto_caminho"]).read_text(encoding="utf-8").strip() == ""
    ), ".txt vazio/quase-vazio para o escaneado"
    assert nativo["flag_escaneado"] == 0

    escaneados = [
        detalhe
        for tipo, detalhe in _tipos_eventos(politeness_veloz.manifesto)
        if tipo == "texto_escaneado"
    ]
    assert len(escaneados) == 1
    assert escaneados[0]["documento_id"] == escaneado["id"]
    assert escaneados[0]["falha_parsing_total"] is False, (
        "páginas parseiam; simplesmente não há texto"
    )


# -- Cenário "PDF corrompido/truncado": erro isolado, lote segue, exit 0 ------------


def test_bytes_corrompidos_isolam_erro_e_lote_segue(
    cli, politeness_veloz, servidor_fake, corpus
):
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/quebrado/x.pdf": pdf_com_texto([_longo("Este sera corrompido depois")]),
            "/integro/y.pdf": pdf_com_texto([_longo("Documento integro permanece")]),
        },
    )
    vitima = next(
        doc
        for doc in _documentos(politeness_veloz.manifesto)
        if doc["caminho"].endswith("x.pdf")
    )
    with open(vitima["caminho"], "wb") as estrago:
        estrago.write(b"%PDF-1.4 lixo sem xref nem eof")

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, _saida(resultado) + " — lote NUNCA aborta por um documento"
    assert "erros: 1" in _saida(resultado)
    assert "extraídos: 1" in _saida(resultado)

    quebrado = next(
        doc
        for doc in _documentos(politeness_veloz.manifesto)
        if doc["id"] == vitima["id"]
    )
    assert quebrado["extraido_em"] is None, "corrompido fica SEM proveniência"
    assert quebrado["flag_escaneado"] is None and quebrado["texto_caminho"] is None
    assert not Path(quebrado["caminho"]).with_suffix(".txt").exists(), "sem .txt"

    eventos = _tipos_eventos(politeness_veloz.manifesto)
    erros = [detalhe for tipo, detalhe in eventos if tipo == "texto_erro"]
    assert len(erros) == 1
    assert erros[0]["fase"] == "abertura_pdf"
    assert erros[0]["documento_id"] == vitima["id"]
    tipos = [tipo for tipo, _ in eventos]
    assert tipos.count("texto_extraido") == 1, "demais documentos processados"

    concluido = next(detalhe for tipo, detalhe in eventos if tipo == "textuar_concluido")
    assert concluido["totais"] == {
        "documentos": 2,
        "extraidos": 1,
        "escaneados": 0,
        "erros": 1,
        "pulados": 0,
    }
    assert concluido["urls_perdidas"] == [vitima["url_origem"]], "perda agregada no evento"


# -- Cenário "Corpus ausente no disco": arquivo_ausente, erro marcado, exit 0 -------


def test_arquivo_ausente_marca_erro_e_lote_segue(
    cli, politeness_veloz, servidor_fake, corpus
):
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/sumiu/a.pdf": pdf_com_texto([_longo("Este arquivo vai sumir")]),
            "/ficou/b.pdf": pdf_com_texto([_longo("Documento presente no disco")]),
        },
    )
    vitima = next(
        doc
        for doc in _documentos(politeness_veloz.manifesto)
        if doc["caminho"].endswith("a.pdf")
    )
    Path(vitima["caminho"]).unlink()

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "erros: 1" in _saida(resultado)
    assert "extraídos: 1" in _saida(resultado)

    ausentes = [
        detalhe
        for tipo, detalhe in _tipos_eventos(politeness_veloz.manifesto)
        if tipo == "arquivo_ausente"
    ]
    assert len(ausentes) == 1
    assert ausentes[0]["documento_id"] == vitima["id"]
    assert ausentes[0]["caminho"] == vitima["caminho"]


# -- Cenário "Re-execução idempotente": zero retrabalho, pulados no resumo ----------


def test_reexecucao_idempotente_zero_reextracoes(
    cli, politeness_veloz, servidor_fake, corpus
):
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/i/um.pdf": pdf_com_texto([_longo("Primeira pagina"), _longo("Segunda pagina")]),
            "/i/dois.pdf": pdf_com_texto([_longo("Outro documento nativo")]),
            "/i/tres.pdf": pdf_apenas_imagem(paginas=1),
        },
    )
    primeira = cli.invoke(app, ["textuar", "--portal", "TST"])
    assert primeira.exit_code == 0, primeira.output
    tipos_primeira = [
        tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)
    ]
    assert tipos_primeira.count("texto_extraido") == 2
    assert tipos_primeira.count("texto_escaneado") == 1

    segunda = cli.invoke(app, ["textuar", "--todos"])

    assert segunda.exit_code == 0, segunda.output
    assert "pulados: 3" in _saida(segunda), "resumo mostra os pulados"
    assert "extraídos: 0" in _saida(segunda) and "escaneados: 0" in _saida(segunda)
    tipos_segunda = [
        tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)
    ]
    assert tipos_segunda.count("texto_extraido") == 2, "nenhum re-parse na retomada"
    assert tipos_segunda.count("texto_escaneado") == 1


# -- Cenário "Documento novo pós-extração": só ele é processado ---------------------


def test_documento_novo_pos_extracao_processa_apenas_ele(
    cli, politeness_veloz, servidor_fake, corpus
):
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/velho/a.pdf": pdf_com_texto([_longo("Documento original ja textuado")])},
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0

    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/novo/b.pdf"])
    servidor_fake.paginas["/novo/b.pdf"] = (
        200,
        "application/pdf",
        pdf_com_texto([_longo("Novidade capturada depois da primeira textuacao")]),
    )
    assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "extraídos: 1" in _saida(resultado), "apenas o novo é extraído"
    assert "pulados: 1" in _saida(resultado), "o antigo já tem hash vigente"
    novos = [
        detalhe
        for tipo, detalhe in _tipos_eventos(politeness_veloz.manifesto)
        if tipo == "texto_extraido" and detalhe["url"].endswith("/novo/b.pdf")
    ]
    assert len(novos) == 1


def test_nova_versao_do_documento_ganha_txt_proprio(
    cli, politeness_veloz, servidor_fake, corpus
):
    """Bytes alterados ⇒ nova versão ⇒ re-extração do NOVO caminho (novo txt)."""
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/v/doc.pdf": pdf_com_texto([_longo("Versao um do edital")])},
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0
    v1 = _documentos(politeness_veloz.manifesto)[0]

    with open(v1["caminho"], "wb") as estrago:
        estrago.write(b"lixo")  # quebra a vigência ⇒ coleta re-captura como v2
    servidor_fake.paginas["/v/doc.pdf"] = (
        200,
        "application/pdf",
        pdf_com_texto([_longo("Versao dois com conteudo novo e mais longo")]),
    )
    assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    versoes = _documentos(politeness_veloz.manifesto)
    assert len(versoes) == 2
    v2 = next(doc for doc in versoes if doc["id"] != v1["id"])
    assert v2["predecessor_id"] == v1["id"]
    assert v2["texto_caminho"] and Path(v2["texto_caminho"]) != Path(v1["texto_caminho"]), (
        "nova versão ⇒ novo .txt"
    )
    assert all(doc["flag_escaneado"] == 0 for doc in versoes)


# -- Alias intra-portal: linhas próprias, .txt irmão único --------------------------


def test_alias_de_hash_duplicado_ganha_proveniencia_e_compartilha_o_txt(
    cli, politeness_veloz, servidor_fake, corpus
):
    corpo = pdf_com_texto([_longo("Mesmos bytes servidos em duas urls")])
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/x/um.pdf": corpo, "/y/dois.pdf": corpo},
    )
    assert len(_documentos(politeness_veloz.manifesto)) == 2, "canônico + alias"

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "extraídos: 2" in _saida(resultado)
    linhas = _documentos(politeness_veloz.manifesto)
    assert all(doc["flag_escaneado"] == 0 for doc in linhas)
    caminhos_txt = {doc["texto_caminho"] for doc in linhas}
    assert len(caminhos_txt) == 1, ".txt irmão é UM só (mesmo caminho de bytes)"


# -- Unidades: limiar configurável, tolerância por página e retomada ---------------


def test_limiar_configuravel_decide_a_flag(tmp_path):
    """O MESMO PDF troca de flag conforme [texto] limiar_chars_por_pagina."""
    corpo = pdf_com_texto(["conteudo curto", "outra pagina curta"])  # ~34 chars / 2 págs

    manifesto, documento = _documento_em(tmp_path / "apertado", corpo)
    try:
        desfecho = extrair_documento(documento, manifesto, 100)
        assert desfecho.desfecho == "escaneado", "34 < 100×2 ⇒ flag true"
        (linha,) = manifesto.consultar("SELECT flag_escaneado FROM documentos")
        assert linha["flag_escaneado"] == 1
    finally:
        manifesto.fechar()

    manifesto2, documento2 = _documento_em(tmp_path / "frouxo", corpo)
    try:
        desfecho2 = extrair_documento(documento2, manifesto2, 5)
        assert desfecho2.desfecho == "extraido", "34 >= 5×2 ⇒ flag false"
        (linha2,) = manifesto2.consultar("SELECT flag_escaneado FROM documentos")
        assert linha2["flag_escaneado"] == 0
    finally:
        manifesto2.fechar()


def test_todas_as_paginas_falhando_vira_escaneado_com_detalhe(tmp_path, monkeypatch):
    """Design Notes: parsing falha em TODAS ⇒ flag true + falha_parsing_total."""
    corpo = pdf_com_texto(["pagina a", "pagina b"])
    manifesto, documento = _documento_em(tmp_path, corpo)

    def _explodindo(_pagina):
        raise RuntimeError("xref podre")

    monkeypatch.setattr("agente_editais.texto._texto_da_pagina", _explodindo)
    try:
        desfecho = extrair_documento(documento, manifesto, 100)
        assert desfecho.desfecho == "escaneado"
        (linha,) = manifesto.consultar("SELECT flag_escaneado FROM documentos")
        assert linha["flag_escaneado"] == 1
        escaneados = [
            detalhe
            for tipo, detalhe in _tipos_eventos(manifesto.caminho)
            if tipo == "texto_escaneado"
        ]
        assert len(escaneados) == 1
        assert escaneados[0]["falha_parsing_total"] is True
        assert escaneados[0]["paginas_com_falha_de_parsing"] == 2
    finally:
        manifesto.fechar()


def test_falha_parcial_de_parsing_nao_forca_flag(tmp_path, monkeypatch):
    """Página ruim rende vazio sem matar o documento; o limiar decide normal."""
    texto_bom = "pagina integra com texto suficiente para o lote seguir bem"  # 58 chars
    corpo = pdf_com_texto([texto_bom] * 4)
    manifesto, documento = _documento_em(tmp_path, corpo)

    chamadas = {"n": 0}

    def _metade_explode(_pagina):
        chamadas["n"] += 1
        if chamadas["n"] % 2 == 0:
            raise RuntimeError("meia-página podre")
        return texto_bom

    monkeypatch.setattr("agente_editais.texto._texto_da_pagina", _metade_explode)
    try:
        desfecho = extrair_documento(documento, manifesto, 25)
        assert desfecho.desfecho == "extraido", "117 chars (2 boas) ≥ 25×4 páginas"
        (linha,) = manifesto.consultar("SELECT flag_escaneado FROM documentos")
        assert linha["flag_escaneado"] == 0, "falha PARCIAL não força flag"
        extraidos = [
            detalhe
            for tipo, detalhe in _tipos_eventos(manifesto.caminho)
            if tipo == "texto_extraido"
        ]
        assert len(extraidos) == 1
        assert extraidos[0]["paginas_com_falha_de_parsing"] == 2, "perda auditável"
    finally:
        manifesto.fechar()


def test_retomada_ja_extraido_exige_hash_vigente(tmp_path):
    """``ja_extraido``: proveniência + txt presente E bytes batendo no L1."""
    corpo = pdf_com_texto(["texto para vigencia"])
    manifesto, documento = _documento_em(tmp_path, corpo)

    try:
        assert ja_extraido(manifesto.consultar("SELECT * FROM documentos")[0]) is False, (
            "sem extração ainda"
        )
        extrair_documento(documento, manifesto, 100)
        linha = manifesto.consultar("SELECT * FROM documentos")[0]
        assert linha["extraido_em"] is not None
        assert ja_extraido(linha) is True, "hash vigente ⇒ pula na retomada"

        # bytes alterados no disco derrubam a vigência (re-extração na próxima)
        Path(documento["caminho"]).write_bytes(pdf_com_texto(["outra coisa"]))
        assert ja_extraido(linha) is False

        # txt sumido também derruba
        Path(documento["caminho"]).write_bytes(corpo)
        Path(linha["texto_caminho"]).unlink()
        assert ja_extraido(linha) is False
    finally:
        manifesto.fechar()


def test_txt_irmao_e_utf8(tmp_path):
    """Encoding obrigatório (§Convenções): UTF-8 no I/O do texto extraído."""
    corpo = pdf_com_texto(["Texto com acentuacao basica para codificacao"])
    manifesto, documento = _documento_em(tmp_path, corpo)
    try:
        extrair_documento(documento, manifesto, 5)
        linha = manifesto.consultar("SELECT texto_caminho, texto_chars FROM documentos")[0]
        bruto = Path(linha["texto_caminho"]).read_bytes()
        bruto.decode("utf-8")  # levanta se não for UTF-8 válido
        assert len(bruto.decode("utf-8")) == linha["texto_chars"]
    finally:
        manifesto.fechar()


# -- Fronteira da flag, surrogates e persistência -----------------------------------


@pytest.mark.parametrize(
    ("tamanhos", "desfecho_esperado"),
    [
        ([100, 100], "extraido"),  # 200 == 100×2: IGUAL ao limiar NÃO é escaneado (<)
        ([99, 100], "escaneado"),  # 199 extraídos < 200 — o join somaria 200: conta SEM separadores
        ([99, 99], "escaneado"),   # 198 < 200
    ],
)
def test_fronteira_da_flag_conta_sem_separadores(
    tmp_path, monkeypatch, tamanhos, desfecho_esperado
):
    corpo = pdf_com_texto(["p", "p"])  # documento real com 2 páginas
    manifesto, documento = _documento_em(tmp_path, corpo)
    fila = iter(tamanhos)

    def _paginas_da_fila(_pagina):
        return "a" * next(fila)

    monkeypatch.setattr("agente_editais.texto._texto_da_pagina", _paginas_da_fila)
    try:
        desfecho = extrair_documento(documento, manifesto, 100)
        assert desfecho.desfecho == desfecho_esperado
        (linha,) = manifesto.consultar("SELECT flag_escaneado FROM documentos")
        assert linha["flag_escaneado"] == (0 if desfecho_esperado == "extraido" else 1)
    finally:
        manifesto.fechar()


def test_surrogate_nao_pareado_vira_marcador_sem_abortar(tmp_path, monkeypatch):
    """Surrogate do pypdf NÃO explode a gravação (UnicodeEncodeError abortaria
    o lote inteiro): o encode-sanitizado troca por marcador e a contagem
    reflete exatamente os bytes gravados."""
    corpo = pdf_com_texto(["pagina com byte podre"])
    manifesto, documento = _documento_em(tmp_path, corpo)

    def _com_surrogate(_pagina):
        return "edital \ud800 truncado " + "conteudo suficiente acima do limiar configurado"

    monkeypatch.setattr("agente_editais.texto._texto_da_pagina", _com_surrogate)
    try:
        desfecho = extrair_documento(documento, manifesto, 20)

        assert desfecho.desfecho == "extraido"
        (linha,) = manifesto.consultar(
            "SELECT texto_caminho, flag_escaneado, texto_chars FROM documentos"
        )
        conteudo = Path(linha["texto_caminho"]).read_bytes().decode("utf-8")  # UTF-8 válido
        assert "\ud800" not in conteudo, "surrogate não sobrevive à sanitização"
        assert "?" in conteudo, "surrogate virou o marcador de substituição do encode"
        assert linha["flag_escaneado"] == 0
        assert len(conteudo) == linha["texto_chars"]
    finally:
        manifesto.fechar()


def test_registrar_texto_extraido_sem_linha_retorna_false(tmp_path):
    """UPDATE que não casa nenhuma linha é False — nunca sucesso silencioso."""
    manifesto, _documento = _documento_em(tmp_path, pdf_com_texto(["doc qualquer"]))
    try:
        persistiu = manifesto.registrar_texto_extraido(
            "zzz999999999",
            "http://x.org/inexistente.pdf",
            texto_caminho="t.txt",
            texto_chars=10,
            texto_paginas=1,
            flag_escaneado=False,
        )
        assert persistiu is False
        (linha,) = manifesto.consultar("SELECT extraido_em FROM documentos")
        assert linha["extraido_em"] is None, "nada gravado sem casamento"
    finally:
        manifesto.fechar()


def test_falha_de_persistencia_vira_erro_e_lote_segue(tmp_path, monkeypatch):
    """``registrar_texto_extraido`` False ⇒ evento texto_erro fase
    'persistencia' por documento — e o lote segue até o fim."""
    manifesto, _primeiro = _documento_em(
        tmp_path / "lote", pdf_com_texto([_longo("Primeiro documento do lote")])
    )
    segundo = tmp_path / "lote" / "segundo.pdf"
    segundo.write_bytes(pdf_com_texto([_longo("Segundo documento do lote")]))
    manifesto.executar(
        """
        INSERT INTO documentos (
            id, edital_id, url_origem, caminho, hash_sha256,
            data_captura, ano_provisorio, versao_crawler
        ) VALUES ('unit7654321', 'unt-sem_ano-doc', 'http://x.org/segundo.pdf',
                  ?, ?, '2026-01-01T00:00:00+00:00', NULL, '0.1.0')
        """,
        (str(segundo), hashlib.sha256(segundo.read_bytes()).hexdigest()),
    )

    def _recusando(_self, *_args, **_kwargs):
        return False

    monkeypatch.setattr(Manifesto, "registrar_texto_extraido", _recusando)
    try:
        desfechos = [
            extrair_documento(doc, manifesto, 100)
            for doc in manifesto.consultar("SELECT * FROM documentos ORDER BY rowid")
        ]
        assert [d.desfecho for d in desfechos] == ["erro", "erro"], "lote segue após falha"
        erros = [
            detalhe
            for tipo, detalhe in _tipos_eventos(manifesto.caminho)
            if tipo == "texto_erro"
        ]
        assert len(erros) == 2
        assert all(erro["fase"] == "persistencia" for erro in erros)
    finally:
        manifesto.fechar()


# -- Configuração: limiar versionado no repo e validação de config ------------------


def test_politeness_do_repo_tem_limiar_de_texto():
    polidez = carregar_polidez(CONFIGS_DO_REPO / "politeness.toml")
    assert polidez.texto_limiar_chars_por_pagina == 100


def test_polidez_le_limiar_de_texto(tmp_path) -> None:
    """Wiring config→campo (espelho do teto de páginas): o valor sai do TOML."""
    caminho = tmp_path / "politeness.toml"
    caminho.write_text(
        'delay_minimo_s = 0\noff_peak = "22:00-06:00"\nuser_agent = "u"\n'
        "[texto]\nlimiar_chars_por_pagina = 250\n",
        encoding="utf-8",
    )

    polidez = carregar_polidez(caminho)
    assert polidez.texto_limiar_chars_por_pagina == 250
    assert polidez.max_paginas_por_portal == 200  # default de outra seção preservado


@pytest.mark.parametrize(
    "secao_texto",
    [
        "[texto]\nlimiar_chars_por_pagina = 0\n",
        "[texto]\nlimiar_chars_por_pagina = -3\n",
        '[texto]\nlimiar_chars_por_pagina = "cem"\n',
        "[texto]\nlimiar_chars_por_pagina = true\n",
        "[texto]\nchave_inventada = 1\n",
    ],
)
def test_config_texto_malformada_recusada_exit_2(
    cli, politeness_veloz, servidor_fake, secao_texto
):
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    (politeness_veloz.configs / "politeness.toml").write_text(
        'delay_minimo_s = 0.0\noff_peak = "22:00-06:00"\nuser_agent = "ua-testes/1"\n'
        + secao_texto,
        encoding="utf-8",
    )
    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])
    assert resultado.exit_code == 2
    assert "ERRO" in _saida(resultado)


# -- Superfície CLI ------------------------------------------------------------------


def test_flags_mutuamente_exclusivas_no_textuar(cli, politeness_veloz) -> None:
    assert cli.invoke(app, ["textuar"]).exit_code == 2
    assert cli.invoke(app, ["textuar", "--portal", "TST", "--todos"]).exit_code == 2
    assert cli.invoke(app, ["textuar", "--portal", ""]).exit_code == 2


def test_textuar_sigla_desconhecida_sai_1(cli, politeness_veloz, servidor_fake) -> None:
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    resultado = cli.invoke(app, ["textuar", "--portal", "XXX"])
    assert resultado.exit_code == 1
    saida = _saida(resultado)
    assert "XXX" in saida and "TST" in saida


def test_sigla_minuscula_case_insensitive(cli, politeness_veloz, servidor_fake) -> None:
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["textuar", "--portal", "tst"])

    assert resultado.exit_code == 0, resultado.output
    assert "[TST] Portal TST" in resultado.output


def test_limiar_custom_do_toml_decide_flags_via_cli(
    cli, politeness_veloz, servidor_fake, corpus
):
    """Wiring TOML→flag ponta a ponta: com limiar 250, um nativo de ~120
    chars/página vira ESCANEADO (hardcode de 100 no chamador daria extraído)."""
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/custom/a.pdf": pdf_com_texto(["a" * 120]),
            "/custom/b.pdf": pdf_com_texto(["b" * 300]),
        },
    )
    (politeness_veloz.configs / "politeness.toml").write_text(
        'delay_minimo_s = 0.0\noff_peak = "22:00-06:00"\nuser_agent = "ua-testes/1"\n'
        "[texto]\nlimiar_chars_por_pagina = 250\n",
        encoding="utf-8",
    )

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    flags = {
        Path(doc["caminho"]).name.rsplit("-", 1)[-1]: doc["flag_escaneado"]
        for doc in _documentos(politeness_veloz.manifesto)
    }
    assert flags["a.pdf"] == 1, "120 < 250×1 ⇒ flag true VINDA DO TOML"
    assert flags["b.pdf"] == 0, "300 ≥ 250×1 ⇒ flag false"


def test_textuar_recusa_portal_orfao_nao_sincronizado(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake
):
    orfao = criar_servidor_fake()  # NUNCA passa por 'mapa validar'
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    escrever_mapa(
        politeness_veloz,
        _mapa_portal(servidor_fake) + "\n" + _mapa_portal(orfao, sigla="ORF"),
    )

    resultado = cli.invoke(app, ["textuar", "--todos"])

    assert resultado.exit_code == 1, resultado.output
    saida = _saida(resultado)
    assert "[ORF]" in saida and url_do(orfao) in saida
    assert "mapa validar" in saida


def test_textuar_nao_exige_janela_off_peak(
    cli, politeness_veloz, servidor_fake, corpus, monkeypatch
):
    """Textuação é 100% local (AD-5: zero rede) — roda fora da janela também."""
    from agente_editais import consulta

    monkeypatch.setattr(consulta, "dentro_da_janela_off_peak", lambda *_a, **_k: False)
    _coletar(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/fora-janela/doc.pdf": pdf_com_texto([_longo("Extracao local dispensa janela")])},
    )

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "extraídos: 1" in _saida(resultado)


def test_textuar_sem_documentos_sai_zero(cli, politeness_veloz, servidor_fake) -> None:
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["textuar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert "Documentos: 0" in _saida(resultado)


# -- Membria e dedupe de documentos_do_portal ----------------------------------------


def test_documentos_do_portal_membria_pdf_sem_fan_out(tmp_path):
    """Membria = candidatos tipo 'pdf' (mesma regra da captura); cada Documento
    aparece exatamente UMA vez — 'pagina_edital' fica fora, JOIN não duplifica."""
    with Manifesto(tmp_path / "m.sqlite3") as manifesto:
        manifesto.executar(
            "INSERT INTO instituicoes (sigla, nome, criado_em) "
            "VALUES ('MEM', 'Instituto Membria', '2026-01-01T00:00:00+00:00')"
        )
        manifesto.executar(
            """
            INSERT INTO portais (
                id, instituicao_id, nome, categoria, url, dinamico,
                profundidade_maxima, criado_em
            ) VALUES (1, 1, 'Portal MEM', 'integra', 'http://p.org', 0, 3,
                      '2026-01-01T00:00:00+00:00')
            """
        )
        manifesto.executar(
            "INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em) "
            "VALUES ('mem-2023-doc', 1, 2023, '2026-01-01T00:00:00+00:00')"
        )
        membria = (
            ("pdf", "http://p.org/a.pdf"),
            ("pdf", "http://p.org/b.pdf"),
            ("pagina_edital", "http://p.org/pag-edital"),
        )
        for tipo, url in membria:
            manifesto.executar(
                "INSERT INTO candidatos (portal_id, url, tipo, descoberto_em) "
                "VALUES (1, ?, ?, '2026-01-01T00:00:00+00:00')",
                (url, tipo),
            )
        # Documento até para a URL só-'pagina_edital': se o filtro de membria
        # falhasse, ele vazaria no resultado
        for indice, (_, url) in enumerate(membria):
            manifesto.executar(
                """
                INSERT INTO documentos (
                    id, edital_id, url_origem, caminho, hash_sha256,
                    data_captura, ano_provisorio, versao_crawler
                ) VALUES (?, 'mem-2023-doc', ?, ?, ?, '2026-01-01T00:00:00+00:00',
                          NULL, '0.1.0')
                """,
                (
                    f"doc{indice:09d}",
                    url,
                    str(tmp_path / f"{indice}.pdf"),
                    "0" * 64,
                ),
            )

        ids = [linha["id"] for linha in manifesto.documentos_do_portal(1)]

        assert sorted(ids) == ["doc000000000", "doc000000001"], (
            "'pagina_edital' fica fora da membria"
        )
        assert len(ids) == len(set(ids)), "nenhum fan-out: cada Documento uma única vez"
