"""Cenários do I/O Matrix para o Manifesto (AD-3/AD-10) + custódia."""

from __future__ import annotations

import re
import sqlite3
import time

import pytest

from agente_editais.consulta import app
from agente_editais.manifest import (
    ENGINE_MINIMA,
    ErroEngineIncompativel,
    ErroManifestoOcupado,
    Manifesto,
)

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
