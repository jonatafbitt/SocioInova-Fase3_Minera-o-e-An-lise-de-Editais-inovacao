"""Cenários do I/O Matrix para o Manifesto (AD-3/AD-10) + custódia."""

from __future__ import annotations

import re
import sqlite3
import time

import pytest

from agente_editais.consulta import app
from agente_editais.manifest import (
    ENGINE_MINIMA,
    _MIGRACAO_V1,
    _MIGRACAO_V2,
    _MIGRACAO_V3,
    _MIGRACAO_V4,
    ErroAberturaManifesto,
    ErroEngineIncompativel,
    ErroManifestoOcupado,
    ErroSchemaFuturo,
    Manifesto,
)

from .conftest import escrever_mapa

TIMESTAMP_ISO_TZ = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$"
)


# -- engine velha (guard AD-10) -----------------------------------------------


def test_guard_recusa_engine_abaixo_do_minimo(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 50, 4))
    with pytest.raises(ErroEngineIncompativel) as excinfo:
        Manifesto(tmp_path / "manifesto.sqlite3")
    mensagem = str(excinfo.value)
    assert ".".join(map(str, ENGINE_MINIMA)) in mensagem
    assert "AD-10" in mensagem


def test_guard_via_cli_exit3_cita_o_guard(monkeypatch, tmp_path, cli) -> None:
    caminho = tmp_path / "m.sqlite3"
    caminho.touch()  # existe para o 'status' chegar até o guard do Manifesto
    monkeypatch.setenv("AGENTE_EDITAIS_MANIFESTO", str(caminho))
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 50, 4))
    resultado = cli.invoke(app, ["status"])
    assert resultado.exit_code == 3
    saida = resultado.output + (resultado.stderr or "")
    assert "3.51.3" in saida


def test_engine_no_minimo_passa(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", ENGINE_MINIMA)
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    try:
        assert manifesto.schema_version() == 5  # v1..v5 (CAP-6 + CAP-3)
    finally:
        manifesto.fechar()


# -- lock exclusivo de startup (AD-3) ------------------------------------------


def test_segundo_processo_falha_rapido_sem_corromper_banco(tmp_path) -> None:
    caminho = tmp_path / "manifesto.sqlite3"

    primeiro = Manifesto(caminho)
    primeiro.registrar_evento(tipo="boot", comando="teste")
    primeiro.fechar()

    bloqueador = sqlite3.connect(caminho, timeout=5)
    bloqueador.execute("BEGIN EXCLUSIVE")  # simula outro processo segurando o lock

    inicio = time.perf_counter()
    with pytest.raises(ErroManifestoOcupado) as excinfo:
        Manifesto(caminho)
    decorrido = time.perf_counter() - inicio

    assert decorrido < 5.0, "o segundo processo precisa falhar rápido"
    assert "outro processo" in str(excinfo.value).lower()

    bloqueador.rollback()
    bloqueador.close()

    reaberto = Manifesto(caminho)  # banco íntegro após liberar o lock
    try:
        assert reaberto.consultar("PRAGMA integrity_check")[0][0] == "ok"
        tipos = [linha["tipo"] for linha in reaberto.consultar("SELECT tipo FROM eventos")]
        assert tipos == ["boot"]
    finally:
        reaberto.fechar()


# -- WAL e migrações -----------------------------------------------------------


def test_wal_ativo_e_migracao_versionada_idempotente(tmp_path) -> None:
    caminho = tmp_path / "manifesto.sqlite3"
    primeira = Manifesto(caminho)
    try:
        assert primeira.consultar("PRAGMA journal_mode")[0][0] == "wal"
        assert primeira.schema_version() == 5
        objetos = primeira.consultar(
            "SELECT name FROM sqlite_master WHERE type IN ('table','trigger') ORDER BY name"
        )
    finally:
        primeira.fechar()

    segunda = Manifesto(caminho)
    try:
        assert segunda.schema_version() == 5
        assert segunda.consultar(
            "SELECT name FROM sqlite_master WHERE type IN ('table','trigger') ORDER BY name"
        ) == objetos
    finally:
        segunda.fechar()


def test_migracao_v1_para_atual_preserva_dados_e_eh_idempotente(tmp_path) -> None:
    """Banco criado manualmente em user_version=1 migra intacto até a versão atual."""
    caminho = tmp_path / "antigo.sqlite3"
    bruto = sqlite3.connect(caminho)
    try:
        for declaracao in _MIGRACAO_V1:
            bruto.execute(declaracao)
        bruto.execute(
            "INSERT INTO instituicoes (sigla, nome, criado_em) "
            "VALUES ('VEL', 'Instituição Antiga', '2026-01-01T00:00:00+00:00')"
        )
        bruto.execute(
            """
            INSERT INTO portais (
                instituicao_id, nome, categoria, url, dinamico,
                profundidade_maxima, criado_em
            ) VALUES (1, 'Portal Velho', 'integra', 'http://velho.org', 0, 3,
                      '2026-01-01T00:00:00+00:00')
            """
        )
        bruto.execute("PRAGMA user_version = 1")
        bruto.commit()
    finally:
        bruto.close()

    manifesto = Manifesto(caminho)
    try:
        assert manifesto.schema_version() == 5
        assert manifesto.contar_instituicoes() == 1, "dados v1 preservados"
        assert manifesto.contar_portais() == 1
        # tabelas da v2/v3 utilizáveis imediatamente após a migração
        assert (
            manifesto.registrar_candidato(1, "http://velho.org/e.pdf", "pdf") is True
        )
        assert (
            manifesto.registrar_secao_visitada(1, "http://velho.org", 0) is True
        )
    finally:
        manifesto.fechar()

    objetos_antes: list | None = None
    reaberto = Manifesto(caminho)
    try:
        assert reaberto.schema_version() == 5  # idempotente
        objetos_antes = reaberto.consultar(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        assert reaberto.contar_candidatos() == 1
        assert reaberto.contar_secoes_visitadas() == 1
    finally:
        reaberto.fechar()

    terceira = Manifesto(caminho)
    try:
        assert terceira.consultar(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) == objetos_antes
    finally:
        terceira.fechar()


def test_migracao_v2_para_atual_preserva_dados_e_eh_idempotente(tmp_path) -> None:
    """Banco da Story 2 (v2) abre na versão atual com candidatos intactos."""
    caminho = tmp_path / "story2.sqlite3"
    bruto = sqlite3.connect(caminho)
    try:
        for declaracao in (*_MIGRACAO_V1, *_MIGRACAO_V2):
            bruto.execute(declaracao)
        bruto.execute(
            "INSERT INTO instituicoes (sigla, nome, criado_em) "
            "VALUES ('S2', 'Instituição Story 2', '2026-01-01T00:00:00+00:00')"
        )
        bruto.execute(
            """
            INSERT INTO portais (
                instituicao_id, nome, categoria, url, dinamico,
                profundidade_maxima, criado_em
            ) VALUES (1, 'Portal Antigo', 'integra', 'http://s2.org', 0, 3,
                      '2026-01-01T00:00:00+00:00')
            """
        )
        bruto.execute(
            "INSERT INTO candidatos (portal_id, url, tipo, descoberto_em) "
            "VALUES (1, 'http://s2.org/e.pdf', 'pdf', '2026-02-02T00:00:00+00:00')"
        )
        bruto.execute("PRAGMA user_version = 2")
        bruto.commit()
    finally:
        bruto.close()

    manifesto = Manifesto(caminho)
    try:
        assert manifesto.schema_version() == 5
        assert manifesto.contar_candidatos() == 1, "candidatos v2 preservados"
        # v3 utilizável: retomada enxerga o candidato herdado
        pendentes = manifesto.candidatos_pdf_do_portal(1)
        assert [linha["url"] for linha in pendentes] == ["http://s2.org/e.pdf"]
        assert manifesto.ultimo_documento_da_url("http://s2.org/e.pdf") is None
        assert manifesto.contar_editais() == 0 and manifesto.contar_documentos() == 0
    finally:
        manifesto.fechar()

    objetos_v3: list | None = None
    reaberto = Manifesto(caminho)
    try:
        assert reaberto.schema_version() == 5  # idempotente
        objetos_v3 = reaberto.consultar(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        assert reaberto.contar_candidatos() == 1
    finally:
        reaberto.fechar()

    terceira = Manifesto(caminho)
    try:
        assert terceira.consultar(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) == objetos_v3
    finally:
        terceira.fechar()


def test_migracao_v3_para_v4_preserva_documentos_e_habilita_proveniencia(tmp_path) -> None:
    """Banco da Story 3 (v3) abre na v4 com documentos intactos e colunas de texto.

    As quatro colunas da v4 nascem NULL; ``registrar_texto_extraido`` (UPDATE
    do estágio texto, AD-11) preenche proveniência + ``flag_escaneado`` da v3.
    """
    caminho = tmp_path / "story3.sqlite3"
    bruto = sqlite3.connect(caminho)
    try:
        for declaracao in (*_MIGRACAO_V1, *_MIGRACAO_V2, *_MIGRACAO_V3):
            bruto.execute(declaracao)
        bruto.execute(
            "INSERT INTO instituicoes (sigla, nome, criado_em) "
            "VALUES ('S3', 'Instituição Story 3', '2026-01-01T00:00:00+00:00')"
        )
        bruto.execute(
            """
            INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
            VALUES ('s3-2023-edital-x', 1, 2023, '2026-03-03T00:00:00+00:00')
            """
        )
        bruto.execute(
            """
            INSERT INTO documentos (
                id, edital_id, url_origem, caminho, hash_sha256, data_captura,
                ano_provisorio, versao_crawler
            ) VALUES ('abc123456789', 's3-2023-edital-x', 'http://s3.org/x.pdf',
                      'corpus/S3/2023/abc123456789-edital-x.pdf',
                      'a' * 64, '2026-03-03T01:00:00+00:00', 2023, '0.1.0')
            """
        )
        bruto.execute("PRAGMA user_version = 3")
        bruto.commit()
    finally:
        bruto.close()

    manifesto = Manifesto(caminho)
    try:
        assert manifesto.schema_version() == 5  # migra até a versão atual
        colunas = {
            linha["name"]
            for linha in manifesto.consultar("PRAGMA table_info(documentos)")
        }
        assert {
            "texto_caminho",
            "texto_chars",
            "texto_paginas",
            "extraido_em",
        } <= colunas, "as 4 colunas de texto da v4 existem"
        (documento,) = manifesto.consultar("SELECT * FROM documentos")
        assert documento["flag_escaneado"] is None, "v4 não toca a flag da v3 na migração"
        assert documento["texto_caminho"] is None and documento["extraido_em"] is None

        # v4 utilizável imediatamente: UPDATE de proveniência do estágio texto
        manifesto.registrar_texto_extraido(
            "abc123456789",
            "http://s3.org/x.pdf",
            texto_caminho="corpus/S3/2023/abc123456789-edital-x.txt",
            texto_chars=1200,
            texto_paginas=12,
            flag_escaneado=True,
        )
        (texto,) = manifesto.consultar("SELECT * FROM documentos")
        assert texto["texto_chars"] == 1200
        assert texto["texto_paginas"] == 12
        assert texto["flag_escaneado"] == 1, "coluna nascida NULL na v3 é preenchida"
        assert texto["extraido_em"] is not None
    finally:
        manifesto.fechar()

    objetos_v4: list | None = None
    reaberto = Manifesto(caminho)
    try:
        assert reaberto.schema_version() == 5  # idempotente
        objetos_v4 = reaberto.consultar(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        (documento,) = reaberto.consultar("SELECT * FROM documentos")
        assert documento["texto_caminho"].endswith(".txt"), "proveniência persiste"
    finally:
        reaberto.fechar()

    quarta = Manifesto(caminho)
    try:
        assert quarta.consultar(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) == objetos_v4
    finally:
        quarta.fechar()


def _banco_v4_com_documento(caminho) -> None:
    """Banco da Story 4 (v4) com portal, candidato, edital e documento."""
    bruto = sqlite3.connect(caminho)
    try:
        for declaracao in (*_MIGRACAO_V1, *_MIGRACAO_V2, *_MIGRACAO_V3, *_MIGRACAO_V4):
            bruto.execute(declaracao)
        bruto.execute(
            "INSERT INTO instituicoes (sigla, nome, criado_em) "
            "VALUES ('S4', 'Instituição Story 4', '2026-01-01T00:00:00+00:00')"
        )
        bruto.execute(
            """
            INSERT INTO portais (
                id, instituicao_id, nome, categoria, url, dinamico,
                profundidade_maxima, criado_em
            ) VALUES (1, 1, 'Portal S4', 'integra', 'http://s4.org', 0, 3,
                      '2026-01-01T00:00:00+00:00')
            """
        )
        bruto.execute(
            """
            INSERT INTO candidatos (portal_id, url, tipo, descoberto_em)
            VALUES (1, 'http://s4.org/x.pdf', 'pdf', '2026-04-04T00:00:00+00:00')
            """
        )
        bruto.execute(
            """
            INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
            VALUES ('s4-2023-edital-x', 1, 2023, '2026-04-04T00:00:00+00:00')
            """
        )
        bruto.execute(
            """
            INSERT INTO documentos (
                id, edital_id, url_origem, caminho, hash_sha256, data_captura,
                ano_provisorio, versao_crawler
            ) VALUES ('def234567890', 's4-2023-edital-x', 'http://s4.org/x.pdf',
                      'corpus/S4/2023/def234567890-edital-x.pdf',
                      ?, '2026-04-04T01:00:00+00:00', 2023, '0.1.0')
            """,
            ("b" * 64,),
        )
        bruto.execute("PRAGMA user_version = 4")
        bruto.commit()
    finally:
        bruto.close()


def test_migracao_v4_para_v5_preserva_documentos_e_habilita_fila(tmp_path) -> None:
    """Banco da Story 4 (v4) abre na v5 com dados intactos e a datação operante.

    ``ano_aceito``/``texto_ancora`` nascem NULL; as tabelas de evidência e de
    fila ficam usáveis imediatamente; helpers UPDATE-only exercitados ponta a
    ponta com idempotência.
    """
    caminho = tmp_path / "story4.sqlite3"
    _banco_v4_com_documento(caminho)

    manifesto = Manifesto(caminho)
    try:
        assert manifesto.schema_version() == 5
        colunas_documentos = {
            linha["name"]
            for linha in manifesto.consultar("PRAGMA table_info(documentos)")
        }
        colunas_candidatos = {
            linha["name"]
            for linha in manifesto.consultar("PRAGMA table_info(candidatos)")
        }
        assert "ano_aceito" in colunas_documentos, "coluna do aceite existe"
        assert "texto_ancora" in colunas_candidatos, "âncora persistível desde a descoberta"
        tabelas = {
            linha["name"]
            for linha in manifesto.consultar(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"evidencias_datacao", "fila_revisao"} <= tabelas

        (documento,) = manifesto.consultar("SELECT * FROM documentos")
        assert documento["metodo_datacao"] is None and documento["ano_aceito"] is None
        assert documento["hash_sha256"] == "b" * 64, "documento v4 preservado"

        # -- evidências: gravação e substituição idempotente pela PK tripla --
        total = manifesto.registrar_evidencias(
            "def234567890",
            "http://s4.org/x.pdf",
            [
                ("url", "http://s4.org/x.pdf", "documentos.url_origem"),
                ("ancora", "Edital 2023", "candidatos.texto_ancora"),
                ("pdf_meta", '{"criado_em": "D:20230101"}', "docinfo"),
            ],
        )
        assert total == 3
        manifesto.registrar_evidencias(
            "def234567890",
            "http://s4.org/x.pdf",
            [("url", "http://s4.org/x.pdf", "documentos.url_origem")],
        )
        linhas_fonte = [
            linha["fonte"]
            for linha in manifesto.consultar(
                "SELECT fonte FROM evidencias_datacao"
            )
        ]
        assert sorted(linhas_fonte) == ["ancora", "pdf_meta", "url"], (
            "regravar a mesma fonte substitui a própria linha — nunca duplica"
        )

        # -- aceite UPDATE-only com rowcount verificado -----------------------
        assert manifesto.aplicar_datacao(
            "def234567890", "http://s4.org/x.pdf", metodo="url", ano=2023
        ) is True
        (datado,) = manifesto.consultar(
            "SELECT metodo_datacao, ano_aceito FROM documentos"
        )
        assert datado["metodo_datacao"] == "url" and datado["ano_aceito"] == 2023
        assert (
            manifesto.aplicar_datacao(
                "zzz999999999", "http://s4.org/x.pdf", metodo="url", ano=2023
            )
            is False
        ), "UPDATE sem casamento é False — nunca sucesso silencioso"

        # -- fila: enfileirar é idempotente pelo UNIQUE parcial ---------------
        assert manifesto.enfileirar("http://s4.org/y.pdf", 1, "sem_data") is True
        assert manifesto.enfileirar("http://s4.org/y.pdf", 1, "divergencia") is False, (
            "segundo item pendente para a MESMA url é recusado pelo banco"
        )
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                """
                INSERT INTO fila_revisao (url_origem, portal_id, motivo, status, criado_em)
                VALUES ('http://s4.org/y.pdf', 1, 'sem_data', 'pendente',
                        '2026-05-05T00:00:00+00:00')
                """
            )
        # FK inválida PROPAGA: só o conflito com o índice parcial é "já na fila"
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.enfileirar("http://s4.org/fantasma.pdf", 999, "sem_data")

        # -- decisão humana: valida justificativa/autoria e destino único -----
        (item,) = manifesto.consultar_fila("pendente")
        assert item["motivo"] == "sem_data" and item["status"] == "pendente"
        with pytest.raises(ValueError):
            manifesto.registrar_decisao_fila(
                item["id"],
                decidido_ano=2023,
                decidido_exclusao=False,
                justificativa="   ",
                autor="Pesquisadora",
            )
        with pytest.raises(ValueError):
            manifesto.registrar_decisao_fila(
                item["id"],
                decidido_ano=None,
                decidido_exclusao=False,
                justificativa="ok",
                autor="Pesquisadora",
            )
        with pytest.raises(ValueError):
            manifesto.registrar_decisao_fila(
                item["id"],
                decidido_ano=2023,
                decidido_exclusao=True,
                justificativa="ok",
                autor="Pesquisadora",
            )
        with pytest.raises(ValueError):
            manifesto.registrar_decisao_fila(
                item["id"],
                decidido_ano=2030,
                decidido_exclusao=False,
                justificativa="ok",
                autor="Pesquisadora",
            )
        assert (
            manifesto.registrar_decisao_fila(
                item["id"],
                decidido_ano=2022,
                decidido_exclusao=False,
                justificativa="Capa declara 2022.",
                autor="Pesquisadora",
                evidencia_anexa="capa página 1",
            )
            is True
        )
        (decidido,) = manifesto.consultar("SELECT * FROM fila_revisao")
        assert decidido["status"] == "resolvida" and decidido["decidido_ano"] == 2022
        assert decidido["decidido_exclusao"] == 0 and decidido["autor"] == "Pesquisadora"
        assert decidido["justificativa"] == "Capa declara 2022."
        assert decidido["decidido_em"] is not None
        # item resolvido não aceita segunda decisão (retomada não refaz)
        assert (
            manifesto.registrar_decisao_fila(
                item["id"],
                decidido_ano=None,
                decidido_exclusao=True,
                justificativa="de novo",
                autor="Outra",
            )
            is False
        )
        assert manifesto.contar_fila("pendente") == 0
        assert manifesto.contar_fila("resolvida") == 1
    finally:
        manifesto.fechar()

    objetos_v5: list | None = None
    reaberto = Manifesto(caminho)
    try:
        assert reaberto.schema_version() == 5  # idempotente
        objetos_v5 = reaberto.consultar(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        (documento,) = reaberto.consultar("SELECT * FROM documentos")
        assert documento["ano_aceito"] == 2023, "aceite persiste na retomada"
    finally:
        reaberto.fechar()

    quinta = Manifesto(caminho)
    try:
        assert quinta.consultar(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) == objetos_v5
    finally:
        quinta.fechar()


def test_registrar_candidato_preenche_ancora_sem_sobrescrever(tmp_path) -> None:
    """Upsert da âncora (FR-6 na origem): re-descoberta preenche NULL e
    NUNCA sobrescreve âncora existente; retorno continua "inserido agora"."""
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    try:
        manifesto.executar(
            "INSERT INTO instituicoes (sigla, nome, criado_em) "
            "VALUES ('ANC', 'Instituto Âncora', '2026-01-01T00:00:00+00:00')"
        )
        manifesto.executar(
            """
            INSERT INTO portais (
                instituicao_id, nome, categoria, url, dinamico,
                profundidade_maxima, criado_em
            ) VALUES (1, 'Portal ANC', 'integra', 'http://a.org', 0, 3,
                      '2026-01-01T00:00:00+00:00')
            """
        )
        # novo candidato nasce com âncora
        assert (
            manifesto.registrar_candidato(
                1, "http://a.org/x.pdf", "pdf", texto_ancora="Edital 2023"
            )
            is True
        )
        (linha,) = manifesto.consultar("SELECT texto_ancora FROM candidatos")
        assert linha["texto_ancora"] == "Edital 2023"

        # re-registro SEM âncora: não é novo E preserva a âncora gravada
        assert manifesto.registrar_candidato(1, "http://a.org/x.pdf", "pdf") is False
        (linha,) = manifesto.consultar("SELECT texto_ancora FROM candidatos")
        assert linha["texto_ancora"] == "Edital 2023", "nunca sobrescreve"

        # candidato pré-existente SEM âncora: re-registro COM âncora preenche
        assert manifesto.registrar_candidato(1, "http://a.org/y.pdf", "pdf") is True
        assert (
            manifesto.registrar_candidato(
                1, "http://a.org/y.pdf", "pdf", texto_ancora="Chamada 2024"
            )
            is False
        ), "preencher âncora de existente não é 'novo'"
        (y,) = manifesto.consultar(
            "SELECT texto_ancora FROM candidatos WHERE url = 'http://a.org/y.pdf'"
        )
        assert y["texto_ancora"] == "Chamada 2024", "corpus antigo retroalimentado"
        total = manifesto.contar_candidatos()
        assert total == 2, "upsert nunca duplica linha"
    finally:
        manifesto.fechar()


def test_schema_v5_guarda_integridade_de_datacao(tmp_path) -> None:
    """CHECKs/FKs da v5: janela no aceite, fontes fechadas, FK composta."""
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    try:
        manifesto.executar(
            "INSERT INTO instituicoes (sigla, nome, criado_em) "
            "VALUES ('V5', 'Instituto V5', '2026-01-01T00:00:00+00:00')"
        )
        manifesto.executar(
            """
            INSERT INTO portais (
                instituicao_id, nome, categoria, url, dinamico,
                profundidade_maxima, criado_em
            ) VALUES (1, 'Portal V5', 'integra', 'http://v5.org', 0, 3,
                      '2026-01-01T00:00:00+00:00')
            """
        )
        manifesto.executar(
            """
            INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
            VALUES ('v5-2023-e', 1, 2023, '2026-01-01T00:00:00+00:00')
            """
        )
        manifesto.executar(
            """
            INSERT INTO documentos (
                id, edital_id, url_origem, caminho, hash_sha256, data_captura,
                ano_provisorio, versao_crawler
            ) VALUES ('ghi345678901', 'v5-2023-e', 'http://v5.org/a.pdf',
                      'corpus/a.pdf', ?, '2026-01-01T00:00:00+00:00',
                      2023, '0.1.0')
            """,
            ("c" * 64,),
        )
        # ano_aceito fora da janela é recusado PELO BANCO
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                "UPDATE documentos SET ano_aceito = 2018 WHERE id = 'ghi345678901'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                "UPDATE documentos SET ano_aceito = 2027 WHERE id = 'ghi345678901'"
            )
        # fonte fora do conjunto fechado recusada
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                """
                INSERT INTO evidencias_datacao (
                    documento_id, url_origem, fonte, valor_bruto, localizacao, criado_em
                ) VALUES ('ghi345678901', 'http://v5.org/a.pdf', 'time_tag', 'x',
                          'y', '2026-01-01T00:00:00+00:00')
                """
            )
        # FK composta: evidência para documento inexistente recusada
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                """
                INSERT INTO evidencias_datacao (
                    documento_id, url_origem, fonte, valor_bruto, localizacao, criado_em
                ) VALUES ('zzz999999999', 'http://v5.org/a.pdf', 'url', 'x',
                          'y', '2026-01-01T00:00:00+00:00')
                """
            )
        # status fechado + decisão com ano fora da janela recusados
        manifesto.executar(
            """
            INSERT INTO fila_revisao (url_origem, portal_id, motivo, status, criado_em)
            VALUES ('http://v5.org/a.pdf', 1, 'sem_data', 'pendente',
                    '2026-01-01T00:00:00+00:00')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                "UPDATE fila_revisao SET status = 'arquivada' WHERE url_origem = "
                "'http://v5.org/a.pdf'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                "UPDATE fila_revisao SET decidido_ano = 2030 WHERE url_origem = "
                "'http://v5.org/a.pdf'"
            )
    finally:
        manifesto.fechar()


def test_schema_v3_guarda_integridade_de_documentos(tmp_path) -> None:
    """FK/checks da v3: edital precisa de instituição; ano fora da janela recusa."""
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                """
                INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
                VALUES ('x-2023-e', 999, 2023, '2026-01-01T00:00:00+00:00')
                """
            )
        manifesto.executar(
            "INSERT INTO instituicoes (sigla, nome, criado_em) "
            "VALUES ('TST', 'Teste', '2026-01-01T00:00:00+00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                """
                INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
                VALUES ('tst-2018-fora', 1, 2018, '2026-01-01T00:00:00+00:00')
                """
            )
        manifesto.executar(
            """
            INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
            VALUES ('tst-2023-e', 1, 2023, '2026-01-01T00:00:00+00:00')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                """
                INSERT INTO documentos (
                    id, edital_id, url_origem, caminho, hash_sha256, data_captura,
                    ano_provisorio, versao_crawler
                ) VALUES ('abc123456789', 'fantasma', 'http://x.org/a.pdf',
                          'corpus/x.pdf', 'h', '2026-01-01T00:00:00+00:00',
                          2023, '0.1.0')
                """
            )
    finally:
        manifesto.fechar()


# -- eventos append-only (custódia AD-10) ---------------------------------------


def test_eventos_sao_append_only(tmp_path) -> None:
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    try:
        identificador = manifesto.registrar_evento(
            tipo="evento_teste",
            comando="pytest",
            detalhe={"chave": "valor"},
        )
        linha = manifesto.consultar(
            "SELECT ts, comando, tipo, detalhe FROM eventos WHERE id = ?",
            (identificador,),
        )[0]
        assert TIMESTAMP_ISO_TZ.match(linha["ts"]), "timestamp deve ser ISO 8601 com timezone"
        import json

        assert json.loads(linha["detalhe"]) == {"chave": "valor"}

        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar("UPDATE eventos SET tipo = 'adulterado' WHERE id = ?", (identificador,))
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar("DELETE FROM eventos WHERE id = ?", (identificador,))
    finally:
        manifesto.fechar()


def test_chave_estrangeira_de_portal_exige_instituicao(tmp_path) -> None:
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                """
                INSERT INTO portais (
                    instituicao_id, nome, categoria, url, dinamico,
                    profundidade_maxima, criado_em
                ) VALUES (999, 'Fantasma', 'integra', 'http://x.org', 0, 3, '2026-01-01T00:00:00+00:00')
                """
            )
    finally:
        manifesto.fechar()


def test_categoria_no_banco_tem_check_fechado(tmp_path) -> None:
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    try:
        manifesto.executar(
            "INSERT INTO instituicoes (sigla, nome, criado_em) VALUES ('TST', 'Teste', '2026-01-01T00:00:00+00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            manifesto.executar(
                """
                INSERT INTO portais (
                    instituicao_id, nome, categoria, url, dinamico,
                    profundidade_maxima, criado_em
                ) VALUES (
                    (SELECT id FROM instituicoes WHERE sigla='TST'),
                    'Ruim', 'categoria_inventada', 'http://y.org', 0, 3, '2026-01-01T00:00:00+00:00'
                )
                """
            )
    finally:
        manifesto.fechar()


# -- robustez de erros e integridade -------------------------------------------


def test_metodos_apos_fechar_levantan_runtime_error(tmp_path) -> None:
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    manifesto.fechar()
    with pytest.raises(RuntimeError, match="Manifesto fechado"):
        manifesto.consultar("SELECT 1")
    with pytest.raises(RuntimeError, match="Manifesto fechado"):
        manifesto.registrar_evento(tipo="x", comando="y")
    with pytest.raises(RuntimeError, match="Manifesto fechado"):
        with manifesto.transacao():
            pass


def test_detalhe_nao_serializavel_nao_aborta_o_chamador(tmp_path) -> None:
    from pathlib import Path

    manifesto = Manifesto(tmp_path / "m.sqlite3")
    try:
        identificador = manifesto.registrar_evento(
            tipo="com_objeto",
            comando="pytest",
            detalhe={"caminho": Path("algum/arquivo.toml"), "quando": object()},
        )
        import json

        detalhe = json.loads(
            manifesto.consultar("SELECT detalhe FROM eventos WHERE id = ?", (identificador,))[0][
                "detalhe"
            ]
        )
        assert "caminho" in detalhe  # default=str preservou o conteúdo como texto
    finally:
        manifesto.fechar()


def test_banco_mais_novo_que_o_agente_eh_recusado(tmp_path) -> None:
    caminho = tmp_path / "m.sqlite3"
    manifesto = Manifesto(caminho)
    manifesto.fechar()

    bruto = sqlite3.connect(caminho)
    bruto.execute(f"PRAGMA user_version = {ENGINE_MINIMA[0] * 100}")
    bruto.close()

    with pytest.raises(ErroSchemaFuturo, match="mais novo que o agente"):
        Manifesto(caminho)


def test_falha_de_conexao_vira_erro_mapeado(monkeypatch, tmp_path) -> None:
    def _conectar_explodindo(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", _conectar_explodindo)
    with pytest.raises(ErroAberturaManifesto):
        Manifesto(tmp_path / "m.sqlite3")


def test_transacao_faz_rollback_quando_corpo_explode(tmp_path) -> None:
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    try:
        manifesto.executar(
            "INSERT INTO instituicoes (sigla, nome, criado_em) VALUES ('AAA', 'Antes', '2026-01-01T00:00:00+00:00')"
        )
        with pytest.raises(RuntimeError, match="boom"):
            with manifesto.transacao() as conn:
                conn.execute(
                    "INSERT INTO instituicoes (sigla, nome, criado_em) VALUES ('BBB', 'Dentro', '2026-01-01T00:00:00+00:00')"
                )
                raise RuntimeError("boom")
        siglas = [linha["sigla"] for linha in manifesto.consultar("SELECT sigla FROM instituicoes")]
        assert siglas == ["AAA"], "insert da transação abortada não pode persistir"
    finally:
        manifesto.fechar()


# -- CLI: status e mapeamento de lock para exit 4 --------------------------------


def test_status_happy_path_exit0_com_contagens(cli, configs_reais_no_tmp) -> None:
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["status"])
    assert resultado.exit_code == 0, resultado.output
    saida = resultado.output + (resultado.stderr or "")
    assert "SQLite engine:" in saida
    assert "Schema version:  5" in saida
    assert re.search(r"Instituições:\s+\d+", saida)
    assert re.search(r"Portais:\s+\d+", saida)
    # contagens da descoberta visíveis no status
    assert re.search(r"Candidatos:\s+\d+", saida)
    assert re.search(r"Seções visitadas:\s*\d+", saida)


def test_cli_mapa_validar_com_lock_segurado_sai_4(cli, configs_reais_no_tmp) -> None:
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0  # cria o banco

    bloqueador = sqlite3.connect(configs_reais_no_tmp.manifesto, timeout=5)
    bloqueador.execute("BEGIN EXCLUSIVE")  # simula segundo processo segurando o lock
    try:
        resultado = cli.invoke(app, ["mapa", "validar"])
        assert resultado.exit_code == 4
        saida = resultado.output + (resultado.stderr or "")
        assert "outro processo" in saida.lower()
    finally:
        bloqueador.rollback()
        bloqueador.close()


def test_cli_mapa_validar_aceita_mapa_minimo_sem_instituicao_extra(cli, ambiente, tmp_path) -> None:
    """Sanidade do helper escrever_mapa usado nos demais testes."""
    escrever_mapa(
        ambiente,
        '[[instituicao]]\nsigla = "ZZ"\nnome = "Zed"\n\n  [[instituicao.portal]]\n'
        '  nome = "P"\n  categoria = "integra"\n  url = "http://zz.org"\n'
        '  seeds = ["http://zz.org"]\n',
    )
    resultado = cli.invoke(app, ["mapa", "validar"])
    assert resultado.exit_code == 0
