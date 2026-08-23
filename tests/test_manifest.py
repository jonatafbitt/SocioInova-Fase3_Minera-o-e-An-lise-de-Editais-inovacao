"""Cenários do I/O Matrix para o Manifesto (AD-3/AD-10) + custódia."""

from __future__ import annotations

import re
import sqlite3
import time

import pytest

from agente_editais.consulta import app
from agente_editais.manifest import (
    ENGINE_MINIMA,
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
        assert manifesto.schema_version() == 1
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
        assert primeira.schema_version() == 1
        objetos = primeira.consultar(
            "SELECT name FROM sqlite_master WHERE type IN ('table','trigger') ORDER BY name"
        )
    finally:
        primeira.fechar()

    segunda = Manifesto(caminho)
    try:
        assert segunda.schema_version() == 1
        assert segunda.consultar(
            "SELECT name FROM sqlite_master WHERE type IN ('table','trigger') ORDER BY name"
        ) == objetos
    finally:
        segunda.fechar()


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
    assert "Schema version:  1" in saida
    assert re.search(r"Instituições:\s+\d+", saida)
    assert re.search(r"Portais:\s+\d+", saida)


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
