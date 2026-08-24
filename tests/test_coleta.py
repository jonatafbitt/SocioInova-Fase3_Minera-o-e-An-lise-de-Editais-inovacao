"""Cenários do I/O Matrix para a coleta (CAP-4): L1 na captura, dedupe por
hash, retomada, suspensão de host e cap de tamanho.

Tudo contra o servidor fake local (convenção §Testes), inclusive o
kill-mid-run — subprocesso morto com ``os._exit`` no meio do lote, relançado
depois para provar retomada sem retrabalho. Nenhum acesso à rede real.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

import pytest

from agente_editais.consulta import app
from agente_editais.coleta import ano_provisorio_da_url, pasta_do_documento, slug_de_url
from agente_editais.fetcher import carregar_polidez
from agente_editais.manifest import Manifesto

from .conftest import CONFIGS_DO_REPO, escrever_mapa, url_com_host, url_do

TIMESTAMP_ISO_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def pdf(payload: bytes) -> bytes:
    """PDF mínimo válido para o sniff de magic bytes (b'%PDF-')."""
    return b"%PDF-1.4\n%" + payload + b"\n%%EOF"


def _mapa_portal(servidor, *, sigla: str = "TST") -> str:
    return (
        f'[[instituicao]]\nsigla = "{sigla}"\nnome = "Instituto de Teste {sigla}"\n\n'
        f'  [[instituicao.portal]]\n  nome = "Portal {sigla}"\n'
        f'  categoria = "integra"\n  url = "{url_do(servidor)}"\n'
        f'  seeds = ["{url_do(servidor)}"]\n'
    )


def _mapa_url_base(url_base: str, *, sigla: str) -> str:
    """Mapa de um portal com URL-base EXPLÍTITA (hostname sob teste)."""
    return (
        f'[[instituicao]]\nsigla = "{sigla}"\nnome = "Instituto de Teste {sigla}"\n\n'
        f'  [[instituicao.portal]]\n  nome = "Portal {sigla}"\n'
        f'  categoria = "integra"\n  url = "{url_base}"\n'
        f'  seeds = ["{url_base}"]\n'
    )


def _mapa_dois_portais(servidor_a, sigla_a: str, servidor_b, sigla_b: str) -> str:
    return (
        _mapa_portal(servidor_a, sigla=sigla_a)
        + "\n"
        + _mapa_portal(servidor_b, sigla=sigla_b)
    )


@pytest.fixture
def corpus(politeness_veloz, monkeypatch):
    """Raiz de corpus isolada no tmp (AGENTE_EDITAIS_CORPUS)."""
    raiz = politeness_veloz.configs.parent / "corpus"
    monkeypatch.setenv("AGENTE_EDITAIS_CORPUS", str(raiz))
    return raiz


def _registrar_candidatos(servidor, caminho_manifesto, caminhos: list[str]) -> None:
    with Manifesto(caminho_manifesto) as manifesto:
        portal_id = manifesto.id_portal_por_url(url_do(servidor))
        assert portal_id is not None, "rode 'mapa validar' antes"
        for caminho in caminhos:
            assert manifesto.registrar_candidato(
                portal_id, url_do(servidor, caminho), "pdf"
            )


def _documentos(caminho_manifesto) -> list[dict]:
    with Manifesto(caminho_manifesto) as manifesto:
        return [
            dict(linha)
            for linha in manifesto.consultar("SELECT * FROM documentos ORDER BY url_origem")
        ]


def _tipos_eventos(caminho_manifesto) -> list[tuple[str, dict]]:
    with Manifesto(caminho_manifesto) as manifesto:
        return [
            (linha["tipo"], json.loads(linha["detalhe"]))
            for linha in manifesto.consultar("SELECT tipo, detalhe FROM eventos ORDER BY id")
        ]


def _gets(servidor, caminho: str) -> int:
    return sum(
        1
        for registro in servidor.registros
        if registro["metodo"] == "GET" and registro["caminho"] == caminho
    )


# -- unidades puras: ano provisório e slug ----------------------------------------


@pytest.mark.parametrize(
    ("url", "esperado"),
    [
        ("http://x.org/2023/edital.pdf", 2023),
        ("http://x.org/editais/2021-1/e.pdf", 2021),
        ("http://x.org/arquivo/2026/f.pdf", 2026),
        ("http://x.org/a/2019/b/2020/c.pdf", None),  # dois anos ⇒ inequívoco falha
        ("http://x.org/sem-ano/e.pdf", None),
        ("http://x.org/2018/antigo.pdf", None),  # fora da janela 2019–2026
        ("http://x.org/e-12024.pdf", None),  # '12024' não é ano (dígito antes)
    ],
)
def test_ano_provisorio_da_url(url, esperado) -> None:
    assert ano_provisorio_da_url(url) == esperado


@pytest.mark.parametrize(
    ("url", "esperado"),
    [
        ("http://x.org/pdfs/Edital Nº 12_2024!.PDF", "edital-no-12-2024"),
        ("http://x.org/docs/chamada-inovacao-junior.pdf", "chamada-inovacao-junior"),
        ("http://x.org/downloads/", "documento"),  # sem nome ⇒ fallback
        ("http://x.org/a/Açúcar_SimpleS.pdf", "acucar-simples"),
    ],
)
def test_slug_de_url(url, esperado) -> None:
    assert slug_de_url(url) == esperado


@pytest.mark.parametrize(
    ("sigla", "ano", "esperado"),
    [
        ("tst", 2023, ("TST", "2023")),  # sigla vira MAIÚSCULA na pasta
        ("IFBA", None, ("IFBA", "_sem_ano")),  # sem ano ⇒ _sem_ano
        ("ufrb", 2019, ("UFRB", "2019")),
    ],
)
def test_pasta_do_documento_monta_e_cria_a_estrutura(tmp_path, sigla, ano, esperado):
    pasta = pasta_do_documento(tmp_path, sigla, ano)

    assert pasta == tmp_path / esperado[0] / esperado[1]
    assert pasta.is_dir(), "mkdir(parents=True) acontece na hora"


def test_pasta_do_documento_e_idempotente(tmp_path):
    primeira = pasta_do_documento(tmp_path, "tst", 2024)
    segunda = pasta_do_documento(tmp_path, "tst", 2024)
    assert primeira == segunda and primeira.is_dir()


# -- Cenário "Download feliz": 3 PDFs distintos, L1 completo, exit 0 ----------------


def test_download_feliz_grava_tres_documentos_l1_completos(
    cli, politeness_veloz, servidor_fake, corpus
):
    corpos = {
        "/2023/edital-a.pdf": pdf(b"conteudo-A"),
        "/editais/chamada-b.pdf": pdf(b"conteudo-B"),
        "/docs/c.pdf": pdf(b"conteudo-C"),
    }
    for caminho, corpo in corpos.items():
        servidor_fake.paginas[caminho] = (200, "application/pdf", corpo)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, list(corpos))

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "3 documento(s)" in saida

    documentos = _documentos(politeness_veloz.manifesto)
    assert len(documentos) == 3
    por_url = {doc["url_origem"]: doc for doc in documentos}
    for caminho, corpo in corpos.items():
        doc = por_url[url_do(servidor_fake, caminho)]
        hash_hex = hashlib.sha256(corpo).hexdigest()
        assert doc["id"] == hash_hex[:12]
        assert doc["hash_sha256"] == hash_hex
        local = doc["caminho"]
        with open(local, "rb") as bruto:
            assert bruto.read() == corpo, "bytes no disco = bytes capturados"
        assert TIMESTAMP_ISO_TZ.match(doc["data_captura"])
        assert doc["versao_crawler"]
        assert doc["flag_escaneado"] is None, "Story 4 preenche depois (C5)"
        assert doc["metodo_datacao"] is None, "Story 5 preenche depois (C5)"
        assert doc["predecessor_id"] is None and doc["referencia_para"] is None
        # pasta Instituição/Ano e nome <hash12>-<slug>.pdf
        pasta_esperada = {
            "/2023/edital-a.pdf": corpus / "TST" / "2023",
            "/editais/chamada-b.pdf": corpus / "TST" / "_sem_ano",
            "/docs/c.pdf": corpus / "TST" / "_sem_ano",
        }[caminho]
        assert local.startswith(str(pasta_esperada))
        assert local.endswith(f"-{slug_de_url(url_do(servidor_fake, caminho))}.pdf")

    tipos = [tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert tipos.count("documento_baixado") == 3
    assert "coleta_portal_concluida" in tipos and "coletar_concluido" in tipos

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        editais = [dict(linha) for linha in manifesto.consultar("SELECT * FROM editais")]
    assert len(editais) == 3, "cada PDF distinto cria seu edital (1º documento agrupa)"
    for edital in editais:
        assert edital["id"].startswith("tst-")


# -- Cenário "Hash duplicado intra-portal": 1 Documento, 2 registros cruzados -------


def test_hash_duplicado_intra_portal_um_documento_dois_registros_cruzados(
    cli, politeness_veloz, servidor_fake, corpus
):
    corpo = pdf(b"bytes identicos em duas URLs")
    servidor_fake.paginas["/x/um.pdf"] = (200, "application/pdf", corpo)
    servidor_fake.paginas["/y/dois.pdf"] = (200, "application/pdf", corpo)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(
        servidor_fake, politeness_veloz.manifesto, ["/x/um.pdf", "/y/dois.pdf"]
    )

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "1 alias por hash duplicado" in saida

    documentos = _documentos(politeness_veloz.manifesto)
    assert len({doc["id"] for doc in documentos}) == 1, "um Documento só"
    assert len(documentos) == 2, "dois registros (um por candidato)"
    canonical = next(doc for doc in documentos if doc["referencia_para"] is None)
    alias = next(doc for doc in documentos if doc["referencia_para"] is not None)
    assert alias["referencia_para"] == canonical["id"], "referência cruzada"
    assert alias["caminho"] == canonical["caminho"], "bytes gravados UMA vez"
    assert alias["edital_id"] == canonical["edital_id"]

    eventos = _tipos_eventos(politeness_veloz.manifesto)
    duplicados = [detalhe for tipo, detalhe in eventos if tipo == "documento_alias_duplicado"]
    assert len(duplicados) == 1
    assert duplicados[0]["referencia_para"] == canonical["id"]

    with open(canonical["caminho"], "rb") as bruto:
        assert bruto.read() == corpo


# -- Cenário "Re-crawl idempotente": zero downloads novos, zero retrabalho ----------


def test_recrawl_idempotente_zero_downloads_e_zero_retrabalho(
    cli, politeness_veloz, servidor_fake, corpus
):
    caminhos = ["/p1.pdf", "/p2.pdf", "/p3.pdf"]
    for indice, caminho in enumerate(caminhos):
        servidor_fake.paginas[caminho] = (200, "application/pdf", pdf(bytes([65 + indice])))
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, caminhos)

    primeira = cli.invoke(app, ["coletar", "--portal", "TST"])
    assert primeira.exit_code == 0, primeira.output
    gets_antes = {_c: _gets(servidor_fake, _c) for _c in caminhos}
    assert all(contagem == 1 for contagem in gets_antes.values())

    segunda = cli.invoke(app, ["coletar", "--todos"])

    assert segunda.exit_code == 0, segunda.output
    saida = segunda.output + (segunda.stderr or "")
    assert "baixados: 0" in saida
    assert "3 já íntegros" in saida
    for caminho in caminhos:
        assert _gets(servidor_fake, caminho) == gets_antes[caminho], (
            "nenhum byte re-baixado"
        )
    assert len(_documentos(politeness_veloz.manifesto)) == 3


# -- Cenário "Arquivo sumiu/corrompido": re-baixa; hash igual restaura --------------


def test_arquivo_corrompido_rebaixa_e_restaura_o_mesmo_documento(
    cli, politeness_veloz, servidor_fake, corpus
):
    corpo = pdf(b"original integro")
    servidor_fake.paginas["/e/doc.pdf"] = (200, "application/pdf", corpo)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/e/doc.pdf"])
    assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0

    (documento,) = _documentos(politeness_veloz.manifesto)
    with open(documento["caminho"], "wb") as vitima:
        vitima.write(b"bytes corrompidos DE PROPOSITO")

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "restaurados: 1" in saida
    assert _gets(servidor_fake, "/e/doc.pdf") == 2, "re-baixa aconteceu"

    (depois,) = _documentos(politeness_veloz.manifesto)
    assert depois["id"] == documento["id"], "mesma identidade de bytes (AD-8)"
    with open(depois["caminho"], "rb") as bruto:
        assert bruto.read() == corpo, "integridade restaurada"

    tipos = [tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert "bytes_em_quarentena" in tipos, "bytes danificados ficam auditáveis"
    assert "documento_restaurado" in tipos


def test_arquivo_sumido_rebaixa_sem_quarentena(cli, politeness_veloz, servidor_fake, corpus):
    corpo = pdf(b"sumiu do disco")
    servidor_fake.paginas["/s/doc.pdf"] = (200, "application/pdf", corpo)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/s/doc.pdf"])
    assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0
    os.unlink(_documentos(politeness_veloz.manifesto)[0]["caminho"])

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    with open(_documentos(politeness_veloz.manifesto)[0]["caminho"], "rb") as bruto:
        assert bruto.read() == corpo


def test_mudanca_real_de_conteudo_cria_nova_versao_via_predecessor_id(
    cli, politeness_veloz, servidor_fake, corpus
):
    corpo_v1 = pdf(b"versao um")
    servidor_fake.paginas["/e/v.pdf"] = (200, "application/pdf", corpo_v1)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/e/v.pdf"])
    assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0
    v1 = _documentos(politeness_veloz.manifesto)[0]

    # mudança real: bytes locais quebrados E o portal passou a servir outro PDF
    with open(v1["caminho"], "wb") as vitima:
        vitima.write(b"lixo")
    corpo_v2 = pdf(b"versao dois com conteudo novo")
    servidor_fake.paginas["/e/v.pdf"] = (200, "application/pdf", corpo_v2)

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "novas versões: 1" in saida

    documentos = _documentos(politeness_veloz.manifesto)
    assert len(documentos) == 2, "v1 preservada no histórico"
    ids = {doc["id"] for doc in documentos}
    assert hashlib.sha256(corpo_v2).hexdigest()[:12] in ids
    nova = next(doc for doc in documentos if doc["id"] != v1["id"])
    assert nova["predecessor_id"] == v1["id"], "linha do predecessor"
    with open(nova["caminho"], "rb") as bruto:
        assert bruto.read() == corpo_v2

    tipos = [tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert "nova_versao_documento" in tipos


# -- Cenário "Host bloqueando": 403×3 suspende o host; outros portais seguem --------


def test_host_com_403_persistente_suspende_lote_e_outros_portais_seguem(
    cli,
    politeness_veloz,
    servidor_fake,
    servidor_localhost,
    corpus,
):
    bloqueado = servidor_fake  # hostname 127.0.0.1
    # segundo HOST de verdade SEM hardcode de 127.0.0.2: 'localhost' com
    # guard/skip (fixture pula o teste se bind/resolução falharem no ambiente)
    saudavel = servidor_localhost
    for indice in range(1, 5):
        bloqueado.paginas[f"/privado/a{indice}.pdf"] = (403, "text/plain", b"negado")
    saudavel.paginas["/aberto/b1.pdf"] = (200, "application/pdf", pdf(b"portal B ok"))

    escrever_mapa(
        politeness_veloz,
        _mapa_portal(bloqueado, sigla="AAA")
        + "\n"
        # portal B usa hostname EXPLÍTITO 'localhost' — server_address reporta
        # o IP do bind, e a suspensão distingue hosts pelo hostname da URL
        + _mapa_url_base(
            f"http://localhost:{saudavel.server_address[1]}", sigla="BBB"
        ),
    )
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        id_aaa = manifesto.id_portal_por_url(url_do(bloqueado))
        id_bbb = manifesto.id_portal_por_url(
            f"http://localhost:{saudavel.server_address[1]}"
        )
        assert id_aaa is not None and id_bbb is not None
        for indice in range(1, 5):
            assert manifesto.registrar_candidato(
                id_aaa,
                url_com_host(bloqueado, "127.0.0.1", f"/privado/a{indice}.pdf"),
                "pdf",
            )
        assert manifesto.registrar_candidato(
            id_bbb,
            url_com_host(saudavel, "localhost", "/aberto/b1.pdf"),
            "pdf",
        )

    resultado = cli.invoke(app, ["coletar", "--todos"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "Host suspenso (403×3)" in saida
    assert _gets(bloqueado, "/privado/a4.pdf") == 0, "restante do lote pulado"
    assert _gets(saudavel, "/aberto/b1.pdf") == 1, "OUTRO host coletou normalmente"

    documentos = _documentos(politeness_veloz.manifesto)
    assert [doc["url_origem"] for doc in documentos] == [
        url_com_host(saudavel, "localhost", "/aberto/b1.pdf")
    ]

    eventos = _tipos_eventos(politeness_veloz.manifesto)
    suspenso = [detalhe for tipo, detalhe in eventos if tipo == "host_suspenso"]
    assert len(suspenso) == 1
    assert suspenso[0]["host"] == "127.0.0.1"
    assert suspenso[0]["limite_403_consecutivos"] == 3
    assert suspenso[0]["retomada"] == "proxima_execucao"


def test_suspensao_de_host_e_compartilhada_entre_portais_no_mesmo_host(
    cli,
    politeness_veloz,
    servidor_fake,
    criar_servidor_fake,
    corpus,
):
    """--todos: host suspenso num portal pula o restante DO HOST nos outros.

    Dois portais em hostnames IGUAIS (127.0.0.1) e portas diferentes — a
    suspensão é por hostname e vale para a execução inteira: os candidatos do
    segundo portal são pulados SEM rede e contam no relatório.
    """
    outro = criar_servidor_fake()  # mesmo hostname, outra porta
    for indice in range(1, 4):
        servidor_fake.paginas[f"/privado/a{indice}.pdf"] = (403, "text/plain", b"negado")
    outro.paginas["/de-outro/b1.pdf"] = (200, "application/pdf", pdf(b"nunca baixado"))

    escrever_mapa(
        politeness_veloz,
        _mapa_dois_portais(servidor_fake, "AAA", outro, "BBB"),
    )
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(
        servidor_fake, politeness_veloz.manifesto, [f"/privado/a{i}.pdf" for i in range(1, 4)]
    )
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        id_bbb = manifesto.id_portal_por_url(url_do(outro))
        assert manifesto.registrar_candidato(
            id_bbb, url_do(outro, "/de-outro/b1.pdf"), "pdf"
        )

    resultado = cli.invoke(app, ["coletar", "--todos"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "Host suspenso (403×3)" in saida
    assert _gets(outro, "/de-outro/b1.pdf") == 0, (
        "candidato do segundo portal NO MESMO HOST é pulado sem rede"
    )
    assert "Puladas por suspensão de host: 1" in saida, "URL pulada conta no relatório"
    assert url_do(outro, "/de-outro/b1.pdf") in saida

    eventos = _tipos_eventos(politeness_veloz.manifesto)
    concluido = next(detalhe for tipo, detalhe in eventos if tipo == "coletar_concluido")
    assert concluido["totais"]["puladas_host_suspenso"] == 1


def test_suspensao_de_host_eh_por_execucao_host_tenta_de_novo_depois(
    cli, politeness_veloz, servidor_fake, corpus
):
    for indice in range(1, 4):
        servidor_fake.paginas[f"/r{indice}.pdf"] = (403, "text/plain", b"negado")
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(
        servidor_fake, politeness_veloz.manifesto, [f"/r{i}.pdf" for i in range(1, 4)]
    )

    primeira = cli.invoke(app, ["coletar", "--portal", "TST"])
    assert primeira.exit_code == 0, primeira.output
    gets_primeira = [_gets(servidor_fake, f"/r{i}.pdf") for i in range(1, 4)]

    segunda = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert segunda.exit_code == 0, segunda.output
    gets_segunda = [_gets(servidor_fake, f"/r{i}.pdf") for i in range(1, 4)]
    assert all(depois > antes for antes, depois in zip(gets_primeira, gets_segunda)), (
        "host suspenso NÃO é persistido: nova execução dá chance nova"
    )


# -- Cenário "Excede cap de tamanho": aborta antes de gravar; lote segue -------------


def test_excede_cap_de_tamanho_nao_grava_e_lote_segue(
    cli, politeness_veloz, servidor_fake, corpus, monkeypatch
):
    (politeness_veloz.configs / "politeness.toml").write_text(
        'delay_minimo_s = 0.0\noff_peak = "22:00-06:00"\nuser_agent = "ua-testes/1"\n'
        "[crawl]\nrespeitar_janela_off_peak = false\nmax_mb_documento = 0.0001\n"
        "[probe]\ntimeout_s = 5\nrespeitar_janela_off_peak = false\n",
        encoding="utf-8",
    )
    pequeno = pdf(b"x" * 10)  # ~26 bytes < cap (~105 bytes)
    grande = pdf(b"y" * 300)  # ~316 bytes > cap
    servidor_fake.paginas["/pequeno.pdf"] = (200, "application/pdf", pequeno)
    servidor_fake.paginas["/grande.pdf"] = (200, "application/pdf", grande)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(
        servidor_fake, politeness_veloz.manifesto, ["/pequeno.pdf", "/grande.pdf"]
    )

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "1 acima do cap" in saida
    documentos = _documentos(politeness_veloz.manifesto)
    assert [doc["id"] for doc in documentos] == [
        hashlib.sha256(pequeno).hexdigest()[:12]
    ], "excedente NÃO é gravado; lote segue"
    tipos = [tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert tipos.count("tamanho_excedido") == 1
    assert list((corpus / ".tmp").glob("captura-*")) == [], "temporário limpo"


# -- Cenário "Conteúdo não-PDF": não grava como Documento; lote segue ----------------


def test_conteudo_nao_pdf_nao_vira_documento_e_lote_segue(
    cli, politeness_veloz, servidor_fake, corpus
):
    servidor_fake.paginas["/armadilha/doc.pdf"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body>isto nao e um pdf</body></html>",
    )
    servidor_fake.paginas["/bom/doc.pdf"] = (200, "application/pdf", pdf(b"legitimo"))
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(
        servidor_fake, politeness_veloz.manifesto, ["/armadilha/doc.pdf", "/bom/doc.pdf"]
    )

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "1 não-PDF" in saida
    documentos = _documentos(politeness_veloz.manifesto)
    assert [doc["id"] for doc in documentos] == [
        hashlib.sha256(pdf(b"legitimo")).hexdigest()[:12]
    ]
    tipos = [tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert tipos.count("conteudo_inesperado") == 1


# -- Cenário "Interrupção no meio": kill-mid-run via subprocesso, retomada exata -----


# ACOPLEMENTO DELIBERADO: o script abaixo envolve o nome PRIVADO
# ``coleta._processar_candidato`` para matar o processo no meio do lote — é a
# única costura de teste ao interior do módulo. Se essa função for renomeada
# ou mudar de assinatura, este teste quebra DE PROPÓSITO (falha visível) para
# forçar a atualização do ponto exato de corte.
_SCRIPT_KILL = """\
import os
from typer.testing import CliRunner
from agente_editais import coleta
from agente_editais.consulta import app

_original = coleta._processar_candidato
_baixados = {"n": 0}

def _envolto(*args, **kwargs):
    desfecho = _original(*args, **kwargs)
    if desfecho.desfecho == "baixado":
        _baixados["n"] += 1
        if _baixados["n"] >= 2:
            os._exit(9)  # morte abrupta SEM cleanup: simula kill durante o lote
    return desfecho

coleta._processar_candidato = _envolto
resultado = CliRunner().invoke(app, ["coletar", "--todos"])
raise SystemExit(resultado.exit_code)
"""


def test_processo_morto_no_meio_do_lote_retoma_sem_retrabalho(
    cli, politeness_veloz, servidor_fake, corpus
):
    caminhos = ["/k1.pdf", "/k2.pdf", "/k3.pdf"]
    for indice, caminho in enumerate(caminhos):
        servidor_fake.paginas[caminho] = (200, "application/pdf", pdf(bytes([90 + indice])))
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, caminhos)

    ambiente_filho = {
        **os.environ,
        "AGENTE_EDITAIS_CONFIGS": str(politeness_veloz.configs),
        "AGENTE_EDITAIS_MANIFESTO": str(politeness_veloz.manifesto),
        "AGENTE_EDITAIS_CORPUS": str(corpus),
    }
    morto = subprocess.run(
        [sys.executable, "-c", _SCRIPT_KILL],
        env=ambiente_filho,
        capture_output=True,
        timeout=180,
    )
    assert morto.returncode == 9, (
        f"subprocesso devia morrer no meio do lote; stderr: {morto.stderr!r}"
    )
    assert [_gets(servidor_fake, c) for c in caminhos] == [1, 1, 0], (
        "matou após o 2º documento concluído; o 3º não começou"
    )
    assert len(_documentos(politeness_veloz.manifesto)) == 2

    retomada = cli.invoke(app, ["coletar", "--todos"])

    assert retomada.exit_code == 0, retomada.output
    saida = retomada.output + (retomada.stderr or "")
    assert "baixados: 1" in saida, "só o pendente é baixado na retomada"
    assert [_gets(servidor_fake, c) for c in caminhos] == [1, 1, 1], (
        "nenhum documento concluído é re-baixado"
    )
    documentos = _documentos(politeness_veloz.manifesto)
    assert len(documentos) == 3
    for doc in documentos:
        with open(doc["caminho"], "rb") as bruto:
            assert bruto.read().startswith(b"%PDF-")


# -- superfície CLI: flags, janela off-peak, robots ----------------------------------


def test_flags_mutuamente_exclusivas_e_obrigatorias_no_coletar(cli, politeness_veloz) -> None:
    assert cli.invoke(app, ["coletar"]).exit_code == 2
    assert cli.invoke(app, ["coletar", "--portal", "TST", "--todos"]).exit_code == 2
    assert cli.invoke(app, ["coletar", "--portal", ""]).exit_code == 2


def test_coletar_sigla_desconhecida_sai_1(cli, politeness_veloz, servidor_fake) -> None:
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    resultado = cli.invoke(app, ["coletar", "--portal", "XXX"])
    assert resultado.exit_code == 1
    saida = resultado.output + (resultado.stderr or "")
    assert "XXX" in saida and "TST" in saida


def test_coletar_fora_da_janela_off_peak_recusa_sem_requisicoes(
    cli, politeness_veloz, servidor_fake, monkeypatch
):
    from agente_editais import consulta

    politeness_path = politeness_veloz.configs / "politeness.toml"
    politeness_path.write_text(
        'delay_minimo_s = 0.0\noff_peak = "22:00-06:00"\nuser_agent = "ua/1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(consulta, "dentro_da_janela_off_peak", lambda *_a, **_k: False)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 1
    saida = resultado.output + (resultado.stderr or "")
    assert "off-peak" in saida.lower()
    assert servidor_fake.registros == [], "nada é requisitado fora da janela"


def test_robots_bloqueia_caminho_do_candidato_e_lote_segue(
    cli, politeness_veloz, servidor_fake, corpus
):
    servidor_fake.robots_txt = "User-agent: *\nDisallow: /proibido/\n"
    servidor_fake.paginas["/proibido/x.pdf"] = (200, "application/pdf", pdf(b"negado"))
    servidor_fake.paginas["/livre/y.pdf"] = (200, "application/pdf", pdf(b"permitido"))
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(
        servidor_fake, politeness_veloz.manifesto, ["/proibido/x.pdf", "/livre/y.pdf"]
    )

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    assert _gets(servidor_fake, "/proibido/x.pdf") == 0, "§9.1: requisição NEM acontece"
    assert len(_documentos(politeness_veloz.manifesto)) == 1
    tipos = [tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert "robots_bloqueio" in tipos


# -- Revisão: redirect hop-a-hop no download ----------------------------------------


def test_download_com_redirect_hop_a_hop_registra_url_origem_original(
    cli, politeness_veloz, servidor_fake, corpus
):
    """/velho/a.pdf →301→ /novo/a.pdf: Documento com a URL ORIGINAL.

    Redirects são seguidos hop-a-hop VIA fetcher: robots consultado e delay
    aplicado ANTES de cada hop (prova pelos GETs registrados); o L1 guarda
    ``url_origem`` do candidato (URL solicitada), nunca a URL do alvo.
    """
    corpo_alvo = pdf(b"bytes vivem no alvo do redirect")
    servidor_fake.paginas["/novo/a.pdf"] = (200, "application/pdf", corpo_alvo)
    servidor_fake.redirect_absoluto["/velho/a.pdf"] = url_do(servidor_fake, "/novo/a.pdf")
    # delay pequeno mas MENSURÁVEL: prova que a cortesia vale entre hops
    (politeness_veloz.configs / "politeness.toml").write_text(
        'delay_minimo_s = 0.05\noff_peak = "22:00-06:00"\nuser_agent = "ua-testes/1"\n'
        "[crawl]\nrespeitar_janela_off_peak = false\n"
        "[probe]\ntimeout_s = 5\nrespeitar_janela_off_peak = false\n",
        encoding="utf-8",
    )
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/velho/a.pdf"])

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    (documento,) = _documentos(politeness_veloz.manifesto)
    url_velha = url_do(servidor_fake, "/velho/a.pdf")
    url_nova = url_do(servidor_fake, "/novo/a.pdf")
    assert documento["url_origem"] == url_velha, "L1 mantém a URL ORIGINAL"
    assert documento["hash_sha256"] == hashlib.sha256(corpo_alvo).hexdigest()
    with open(documento["caminho"], "rb") as bruto:
        assert bruto.read() == corpo_alvo, "bytes gravados = bytes DO ALVO"

    gets = [r for r in servidor_fake.registros if r["metodo"] == "GET"]
    assert "/robots.txt" in [g["caminho"] for g in gets], "robots é consultado antes da rede"
    assert _gets(servidor_fake, "/velho/a.pdf") == 1, "1º hop requisitado"
    assert _gets(servidor_fake, "/novo/a.pdf") == 1, "2º hop requisitado após o 301"
    # delay aplicado ENTRE os hops: cada GET espera o intervalo mínimo por host
    quando_velho = next(g["quando"] for g in gets if g["caminho"] == "/velho/a.pdf")
    quando_novo = next(g["quando"] for g in gets if g["caminho"] == "/novo/a.pdf")
    assert quando_novo - quando_velho >= 0.045, (
        "delay_minimo_s (0.05) é honrado entre o 1º hop e o alvo do redirect"
    )


# -- Revisão: portal no TOML mas não sincronizado ------------------------------------


def test_coletar_todos_recusa_portal_orfao_nao_sincronizado(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """Portal presente no TOML mas ausente do Manifesto ⇒ exit 1 nomeando-o."""
    orfao = criar_servidor_fake()  # NUNCA passa por 'mapa validar'
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    # mapa ganha um SEGUNDO portal depois da sincronização — fica órfão
    escrever_mapa(politeness_veloz, _mapa_dois_portais(servidor_fake, "TST", orfao, "ORF"))

    resultado = cli.invoke(app, ["coletar", "--todos"])

    assert resultado.exit_code == 1, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "[ORF]" in saida and url_do(orfao) in saida, "órfão é nomeado na saída"
    assert "mapa validar" in saida, "mensagem diz como resolver"
    assert _gets(orfao, "/ok") == 0, "nada é coletado com órfão no lote"


# -- Revisão: OQ-4 — mesmos bytes em portais DIFERENTES não viram alias --------------


def test_bytes_identicos_em_dois_portais_sao_documentos_independentes(
    cli, politeness_veloz, servidor_fake, criar_servidor_fake, corpus
):
    """Pin OQ-4 (negativo): dedupe automático NÃO cruza portais."""
    outro = criar_servidor_fake()
    corpo = pdf(b"mesmos bytes, portais diferentes")
    servidor_fake.paginas["/p/doc.pdf"] = (200, "application/pdf", corpo)
    outro.paginas["/q/doc.pdf"] = (200, "application/pdf", corpo)

    escrever_mapa(politeness_veloz, _mapa_dois_portais(servidor_fake, "AAA", outro, "BBB"))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/p/doc.pdf"])
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        id_bbb = manifesto.id_portal_por_url(url_do(outro))
        assert manifesto.registrar_candidato(id_bbb, url_do(outro, "/q/doc.pdf"), "pdf")

    resultado = cli.invoke(app, ["coletar", "--todos"])

    assert resultado.exit_code == 0, resultado.output
    documentos = _documentos(politeness_veloz.manifesto)
    assert len(documentos) == 2, "um registro POR PORTAL — sem alias cruzado"
    hash_hex = hashlib.sha256(corpo).hexdigest()
    assert {doc["id"] for doc in documentos} == {hash_hex[:12]}
    assert all(doc["referencia_para"] is None for doc in documentos), (
        "OQ-4: captura cruzando portais NUNCA cria referencia_para automático"
    )
    assert {doc["url_origem"] for doc in documentos} == {
        url_do(servidor_fake, "/p/doc.pdf"),
        url_do(outro, "/q/doc.pdf"),
    }
    saida = resultado.output + (resultado.stderr or "")
    assert "0 alias por hash duplicado" in saida

    tipos = [tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert tipos.count("documento_baixado") == 2
    assert "documento_alias_duplicado" not in tipos


# -- Revisão: config versionada do repo + superfície CLI ------------------------------


def test_politeness_do_repo_tem_cap_e_chaves_novas():
    """configs/politeness.toml versionado carrega com defaults esperados."""
    polidez = carregar_polidez(CONFIGS_DO_REPO / "politeness.toml")
    assert polidez.max_mb_documento == 50
    assert polidez.download_prazo_s == 300.0
    assert polidez.download_timeout_s == 60.0
    assert polidez.max_403_consecutivos == 3


def test_status_apos_coleta_real_lista_editais_e_documentos(
    cli, politeness_veloz, servidor_fake, corpus
):
    corpo = pdf(b"status depois da coleta")
    servidor_fake.paginas["/st/doc.pdf"] = (200, "application/pdf", corpo)
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/st/doc.pdf"])
    assert cli.invoke(app, ["coletar", "--portal", "TST"]).exit_code == 0

    resultado = cli.invoke(app, ["status"])

    assert resultado.exit_code == 0, resultado.output
    editais = re.search(r"Editais \(L1\):\s*(\d+)", resultado.output)
    documentos = re.search(r"Documentos:\s*(\d+)", resultado.output)
    assert editais and int(editais.group(1)) >= 1, "linha de Editais com contagem real"
    assert documentos and int(documentos.group(1)) == 1, "linha de Documentos confere"


def test_falha_de_verificacao_pos_mover_vira_evento_e_lote_segue(
    cli, politeness_veloz, servidor_fake, corpus, monkeypatch
):
    import agente_editais.coleta as modulo_coleta

    for indice in range(2):
        servidor_fake.paginas[f"/v{indice}.pdf"] = (
            200,
            "application/pdf",
            pdf(bytes([70 + indice])),
        )
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(
        servidor_fake, politeness_veloz.manifesto, ["/v0.pdf", "/v1.pdf"]
    )

    # hash pós-mover SEMPRE diverge: move acontece, verificação reprova
    monkeypatch.setattr(modulo_coleta, "hash_arquivo_local", lambda _caminho: "0" * 64)

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "2 falhas de download" in saida
    assert _documentos(politeness_veloz.manifesto) == [], "nada vira Documento"
    tipos = [tipo for tipo, _ in _tipos_eventos(politeness_veloz.manifesto)]
    assert tipos.count("captura_falha_verificacao") == 2
    assert list((corpus / ".tmp").glob("captura-*")) == [], "temporário limpo"
    gravados = [c for c in (corpus / "TST").rglob("*") if c.is_file()]
    assert gravados == [], "bytes reprovados não permanecem no corpus"


def test_candidatos_pagina_edital_sao_ignorados_na_coleta(
    cli, politeness_veloz, servidor_fake, corpus
):
    servidor_fake.paginas["/docs/bom.pdf"] = (200, "application/pdf", pdf(b"so o pdf"))
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/docs/bom.pdf"])
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        portal_id = manifesto.id_portal_por_url(url_do(servidor_fake))
        manifesto.registrar_candidato(
            portal_id, url_do(servidor_fake, "/secao/pagina-edital"), "pagina_edital"
        )

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "Candidatos PDF: 1" in saida, "lote só considera tipo='pdf'"
    assert _gets(servidor_fake, "/secao/pagina-edital") == 0
    assert len(_documentos(politeness_veloz.manifesto)) == 1


def test_coletar_aceita_sigla_em_minusculas(cli, politeness_veloz, servidor_fake, corpus):
    servidor_fake.paginas["/m/doc.pdf"] = (200, "application/pdf", pdf(b"minusculas"))
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/m/doc.pdf"])

    resultado = cli.invoke(app, ["coletar", "--portal", "tst"])

    assert resultado.exit_code == 0, resultado.output
    assert len(_documentos(politeness_veloz.manifesto)) == 1


def test_ano_fora_da_janela_vai_para_sem_ano_na_fase_atual(
    cli, politeness_veloz, servidor_fake, corpus
):
    servidor_fake.paginas["/2027/futuro.pdf"] = (
        200,
        "application/pdf",
        pdf(b"ano fora da janela"),
    )
    escrever_mapa(politeness_veloz, _mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    _registrar_candidatos(servidor_fake, politeness_veloz.manifesto, ["/2027/futuro.pdf"])

    resultado = cli.invoke(app, ["coletar", "--portal", "TST"])

    assert resultado.exit_code == 0, resultado.output
    (documento,) = _documentos(politeness_veloz.manifesto)
    assert documento["ano_provisorio"] is None
    assert str(corpus / "TST" / "_sem_ano") in documento["caminho"], (
        "2027 está FORA da janela 2019–2026 ⇒ _sem_ano nesta fase; Story 5 decide"
    )
