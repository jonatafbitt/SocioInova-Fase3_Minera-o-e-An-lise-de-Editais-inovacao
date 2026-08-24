"""Manifesto SQLite — único estado compartilhado e ÚNICO escritor do banco.

Governado por:
- AD-3: escritor único (``manifest.py``), modo WAL, lock exclusivo no startup
  (padrão "admit one": transação EXCLUSIVE de curta duração, liberada em
  seguida — leitores concorrentes são atendidos por snapshot WAL);
- AD-10: guard de engine ``sqlite_version_info >= (3, 51, 3)`` (correção do
  bug WAL-reset) e custódia via tabela ``eventos`` append-only.

Story 2 adicionou a migração v2 (CAP-2): ``candidatos`` e
``secoes_visitadas``, ambas com UNIQUE (portal_id, url) para dedupe por URL
normalizada (AD-8) ser imposto pelo banco, não por memória de processo.

Story 3 adiciona a migração v3 (CAP-4): ``editais`` e ``documentos`` — as
ÚNICAS tabelas que ``coleta`` cria (AD-11). Identidade do Documento é o hash
SHA-256 dos bytes capturados (``id`` = 12 hex iniciais, AD-8); a chave é
composta com ``url_origem`` porque o MESMO conteúdo pode ter múltiplas
origens legítimas: alias intra-portal por hash duplicado (referencia_para)
e capturas cruzando portais da mesma instituição (OQ-4 — dedupe automático
vale só DENTRO do portal). Versões novas de um documento em evolução ligam-se
por ``predecessor_id``; ``flag_escaneado`` e ``metodo_datacao`` nascem NULL
e são preenchidos pelas stories seguintes (pipeline em estágios, C5).

Convenções do spine: placeholders qmark; datas como texto ISO 8601 com
timezone; tabelas no plural; nenhuma API removida/deprecada do Python 3.14.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENGINE_MINIMA = (3, 51, 3)
LOCK_TIMEOUT_MS = 1_500

SCHEMA_VERSAO_ATUAL = 3

# Cada declaração é executada isoladamente dentro da transação exclusiva de
# startup — gatilhos têm ';' no corpo e não podem ser divididos por split.
_MIGRACAO_V1: tuple[str, ...] = (
    """
    CREATE TABLE instituicoes (
        id        INTEGER PRIMARY KEY,
        sigla     TEXT NOT NULL UNIQUE,
        nome      TEXT NOT NULL,
        criado_em TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE portais (
        id                  INTEGER PRIMARY KEY,
        instituicao_id      INTEGER NOT NULL REFERENCES instituicoes(id),
        nome                TEXT NOT NULL,
        categoria           TEXT NOT NULL
            CHECK (categoria IN ('integra', 'nit', 'prpgi_prppg', 'agencia_inovacao')),
        url                 TEXT NOT NULL UNIQUE,
        dinamico            INTEGER NOT NULL DEFAULT 0 CHECK (dinamico IN (0, 1)),
        profundidade_maxima INTEGER NOT NULL DEFAULT 3 CHECK (profundidade_maxima > 0),
        criado_em           TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE eventos (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL,
        comando TEXT NOT NULL,
        tipo    TEXT NOT NULL,
        detalhe TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TRIGGER eventos_sem_update
    BEFORE UPDATE ON eventos
    BEGIN
        SELECT RAISE(ABORT, 'eventos é append-only');
    END
    """,
    """
    CREATE TRIGGER eventos_sem_delete
    BEFORE DELETE ON eventos
    BEGIN
        SELECT RAISE(ABORT, 'eventos é append-only');
    END
    """,
)

_MIGRACAO_V2: tuple[str, ...] = (
    """
    CREATE TABLE candidatos (
        id            INTEGER PRIMARY KEY,
        portal_id     INTEGER NOT NULL REFERENCES portais(id),
        url           TEXT NOT NULL,
        tipo          TEXT NOT NULL CHECK (tipo IN ('pdf', 'pagina_edital')),
        descoberto_em TEXT NOT NULL,
        UNIQUE (portal_id, url)
    )
    """,
    "CREATE INDEX idx_candidatos_portal ON candidatos(portal_id)",
    """
    CREATE TABLE secoes_visitadas (
        id            INTEGER PRIMARY KEY,
        portal_id     INTEGER NOT NULL REFERENCES portais(id),
        url           TEXT NOT NULL,
        profundidade  INTEGER NOT NULL CHECK (profundidade >= 0),
        visitado_em   TEXT NOT NULL,
        UNIQUE (portal_id, url)
    )
    """,
    "CREATE INDEX idx_secoes_visitadas_portal ON secoes_visitadas(portal_id)",
)

_MIGRACAO_V3: tuple[str, ...] = (
    """
    CREATE TABLE editais (
        id             TEXT PRIMARY KEY,
        instituicao_id INTEGER NOT NULL REFERENCES instituicoes(id),
        ano_provisorio INTEGER
            CHECK (ano_provisorio IS NULL OR ano_provisorio BETWEEN 2019 AND 2026),
        criado_em      TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_editais_instituicao ON editais(instituicao_id)",
    """
    CREATE TABLE documentos (
        id              TEXT NOT NULL,
        edital_id       TEXT NOT NULL REFERENCES editais(id),
        url_origem      TEXT NOT NULL,
        caminho         TEXT NOT NULL,
        hash_sha256     TEXT NOT NULL,
        data_captura    TEXT NOT NULL,
        ano_provisorio  INTEGER
            CHECK (ano_provisorio IS NULL OR ano_provisorio BETWEEN 2019 AND 2026),
        versao_crawler  TEXT NOT NULL,
        predecessor_id  TEXT,
        -- referencia_para fica SEM FK de propósito (assimetria deliberada
        -- face à FK composta de predecessor_id): o alias aponta para o id
        -- (hash12) do canônico, que é linha COMPOSTA (id, url_origem) já
        -- existente — um FK simples em id não teria alvo UNIQUE, e a
        -- integridade dele é garantida na camada de coleta + testes.
        referencia_para TEXT,
        flag_escaneado  INTEGER CHECK (flag_escaneado IS NULL OR flag_escaneado IN (0, 1)),
        metodo_datacao  TEXT,
        PRIMARY KEY (id, url_origem),
        FOREIGN KEY (predecessor_id, url_origem) REFERENCES documentos(id, url_origem)
    )
    """,
    "CREATE INDEX idx_documentos_edital ON documentos(edital_id)",
    "CREATE INDEX idx_documentos_url_origem ON documentos(url_origem)",
)

MIGRACOES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, _MIGRACAO_V1),
    (2, _MIGRACAO_V2),
    (3, _MIGRACAO_V3),
)


class ErroEngineIncompativel(RuntimeError):
    """SQLite engine abaixo do mínimo exigido pelo guard (AD-10)."""


class ErroManifestoOcupado(RuntimeError):
    """Outro processo detém o lock exclusivo do Manifesto (AD-3)."""


class ErroAberturaManifesto(RuntimeError):
    """Falha ao criar/abrir o arquivo do Manifesto."""


class ErroSchemaFuturo(RuntimeError):
    """Manifesto criado por versão do agente mais nova que a atual."""


def agora_iso() -> str:
    """Timestamp ISO 8601 com timezone do host (convenção §Datas & horas)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def agora_iso_utc() -> str:
    """Timestamp ISO 8601 NORMALIZADO para UTC (offset convertido).

    Usado onde o valor é CHAVE DE ORDENAÇÃO (``documentos.data_captura``):
    strings UTC ordenam lexicograficamente entre execuções feitas em fusos/
    horários de verão diferentes; offsets locais não.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Manifesto:
    """Conexão única com o Manifesto; cada comando abre uma instância."""

    def __init__(self, caminho: str | Path) -> None:
        versao_engine = sqlite3.sqlite_version_info
        if versao_engine < ENGINE_MINIMA:
            raise ErroEngineIncompativel(
                f"SQLite engine {sqlite3.sqlite_version} ({versao_engine}) é anterior ao "
                f"mínimo {'.'.join(map(str, ENGINE_MINIMA))} exigido pelo guard de "
                "integridade do motor (AD-10, correção do bug WAL-reset). Use o "
                "interpretador gerenciado pelo uv (`uv run ...`) ou atualize o Python."
            )

        self.caminho = Path(caminho)
        try:
            self.caminho.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self.caminho,
                timeout=LOCK_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
        except (OSError, sqlite3.Error) as exc:
            raise ErroAberturaManifesto(
                f"Não foi possível abrir o Manifesto em {self.caminho}: {exc}"
            ) from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute(f"PRAGMA busy_timeout = {LOCK_TIMEOUT_MS}")
            self._conn.execute("PRAGMA foreign_keys = ON")
            modo = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(modo).lower() != "wal":
                raise RuntimeError(f"Não foi possível ativar o modo WAL (journal_mode={modo!r}).")
            self._startup_exclusivo()
        except sqlite3.OperationalError as exc:
            self.fechar()
            raise ErroManifestoOcupado(
                "Outro processo já está executando um comando sobre este Manifesto "
                f"({self.caminho}). Lock exclusivo de startup recusou a segunda "
                f"instância (AD-3). Detalhe: {exc}"
            ) from exc
        except Exception:
            self.fechar()
            raise

    def _startup_exclusivo(self) -> None:
        """Padrão "admit one": transação EXCLUSIVE curta no startup.

        Se outro processo estiver dentro do próprio handshake (ou da migração),
        o ``BEGIN EXCLUSIVE`` aqui falha rápido via busy_timeout.
        """
        self._conn.execute("BEGIN EXCLUSIVE")
        try:
            self._aplicar_migracoes_pendentes()
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _aplicar_migracoes_pendentes(self) -> list[int]:
        linha = self._conn.execute("PRAGMA user_version").fetchone()
        versao_atual = int(linha[0])
        ultima_conhecida = MIGRACOES[-1][0]
        if versao_atual > ultima_conhecida:
            raise ErroSchemaFuturo(
                f"Manifesto em {self.caminho} tem schema version {versao_atual}, mas este "
                f"agente só conhece até {ultima_conhecida}: banco mais novo que o agente. "
                "Atualize o agente antes de usar este Manifesto."
            )
        aplicadas: list[int] = []
        for numero, declaracoes in MIGRACOES:
            if numero <= versao_atual:
                continue
            for declaracao in declaracoes:
                self._conn.execute(declaracao)
            self._conn.execute(f"PRAGMA user_version = {numero}")
            versao_atual = numero
            aplicadas.append(numero)
        return aplicadas

    def _garantir_aberto(self) -> sqlite3.Connection:
        if getattr(self, "_conn", None) is None:
            raise RuntimeError("Manifesto fechado: a instância não pode mais ser usada.")
        return self._conn

    # -- escrita -----------------------------------------------------------

    def registrar_evento(self, tipo: str, comando: str, detalhe: dict[str, Any] | None = None) -> int:
        """Append-only: única forma de inserir em ``eventos`` (AD-10)."""
        conn = self._garantir_aberto()
        try:
            conteudo = json.dumps(detalhe or {}, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            conteudo = "{}"
        cursor = conn.execute(
            "INSERT INTO eventos (ts, comando, tipo, detalhe) VALUES (?, ?, ?, ?)",
            (
                agora_iso(),
                comando,
                tipo,
                conteudo,
            ),
        )
        return int(cursor.lastrowid)

    @contextmanager
    def transacao(self):
        """Unidade de trabalho atômica (AD-3: toda mutação em transação)."""
        conn = self._garantir_aberto()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ErroManifestoOcupado(
                    "Outro processo está gravando neste Manifesto; a transação foi "
                    f"recusada pelo lock (AD-3). Detalhe: {exc}"
                ) from exc
            raise
        confirmada = False
        try:
            yield conn
            conn.execute("COMMIT")
            confirmada = True
        finally:
            if not confirmada:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass  # conexão já sem transação ativa — nada a desfazer

    def executar(self, sql: str, parametros: tuple = ()) -> sqlite3.Cursor:
        return self._garantir_aberto().execute(sql, parametros)

    # -- leitura -----------------------------------------------------------

    def consultar(self, sql: str, parametros: tuple = ()) -> list[sqlite3.Row]:
        return list(self._garantir_aberto().execute(sql, parametros).fetchall())

    def schema_version(self) -> int:
        return int(self.consultar("PRAGMA user_version")[0][0])

    def contar_instituicoes(self) -> int:
        return int(self.consultar("SELECT COUNT(*) FROM instituicoes")[0][0])

    def contar_portais(self) -> int:
        return int(self.consultar("SELECT COUNT(*) FROM portais")[0][0])

    def ultimos_eventos(self, limite: int = 10) -> list[sqlite3.Row]:
        return self.consultar(
            "SELECT id, ts, comando, tipo, detalhe FROM eventos ORDER BY id DESC LIMIT ?",
            (limite,),
        )

    # -- descoberta (CAP-2, migração v2) ------------------------------------

    def id_portal_por_url(self, url: str) -> int | None:
        """ID do portal cadastrado pela URL normalizada; None se ausente."""
        linhas = self.consultar("SELECT id FROM portais WHERE url = ?", (url,))
        return int(linhas[0]["id"]) if linhas else None

    def secao_visitada(self, portal_id: int, url: str) -> bool:
        """True se a seção já foi registrada — base do 'nunca revisitar'."""
        return bool(
            self.consultar(
                "SELECT 1 FROM secoes_visitadas WHERE portal_id = ? AND url = ?",
                (portal_id, url),
            )
        )

    def registrar_secao_visitada(self, portal_id: int, url: str, profundidade: int) -> bool:
        """Registra visita de seção (INSERT OR IGNORE); True se foi nova."""
        conn = self._garantir_aberto()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO secoes_visitadas (portal_id, url, profundidade, visitado_em)
            VALUES (?, ?, ?, ?)
            """,
            (portal_id, url, profundidade, agora_iso()),
        )
        return cursor.rowcount > 0

    def registrar_candidato(self, portal_id: int, url: str, tipo: str) -> bool:
        """Registra candidato a edital dedupe por UNIQUE(portal_id,url); True se novo.

        Segunda execução sobre o mesmo achado NÃO duplica linha (AD-1/AD-8):
        o banco, não memória de processo, é a fonte do "já visto".
        """
        if tipo not in ("pdf", "pagina_edital"):
            raise ValueError(f"tipo de candidato inválido: {tipo!r} (esperado pdf|pagina_edital)")
        conn = self._garantir_aberto()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO candidatos (portal_id, url, tipo, descoberto_em)
            VALUES (?, ?, ?, ?)
            """,
            (portal_id, url, tipo, agora_iso()),
        )
        return cursor.rowcount > 0

    def contar_candidatos(self, portal_id: int | None = None) -> int:
        if portal_id is None:
            return int(self.consultar("SELECT COUNT(*) FROM candidatos")[0][0])
        return int(
            self.consultar("SELECT COUNT(*) FROM candidatos WHERE portal_id = ?", (portal_id,))[0][0]
        )

    def contar_secoes_visitadas(self, portal_id: int | None = None) -> int:
        if portal_id is None:
            return int(self.consultar("SELECT COUNT(*) FROM secoes_visitadas")[0][0])
        return int(
            self.consultar(
                "SELECT COUNT(*) FROM secoes_visitadas WHERE portal_id = ?", (portal_id,)
            )[0][0]
        )

    # -- coleta (CAP-4, migração v3) -----------------------------------------

    def candidatos_pdf_do_portal(self, portal_id: int) -> list[sqlite3.Row]:
        """Candidatos tipo 'pdf' do portal na ordem de descoberta (lote estável)."""
        return self.consultar(
            """
            SELECT id, url FROM candidatos
            WHERE portal_id = ? AND tipo = 'pdf'
            ORDER BY id
            """,
            (portal_id,),
        )

    def ultimo_documento_da_url(self, url: str) -> sqlite3.Row | None:
        """Versão MAIS RECENTE registrada para a URL — base da retomada.

        Retomada (AD-1): candidato com documento cujos bytes locais batem no
        hash ⇒ trabalho já concluído; sem registro ou com bytes quebrados ⇒
        refaz. A ordenação usa ``data_captura`` NORMALIZADA em UTC na gravação
        (``agora_iso_utc``): strings UTC comparam lexicograficamente entre
        execuções feitas em fusos diferentes. ``rowid`` desempata timestamps
        idênticos na mesma execução.
        """
        linhas = self.consultar(
            """
            SELECT * FROM documentos
            WHERE url_origem = ?
            ORDER BY data_captura DESC, rowid DESC
            LIMIT 1
            """,
            (url,),
        )
        return linhas[0] if linhas else None

    def documento_mesmo_hash_no_portal(
        self, portal_id: int, hash_sha256: str, *, exceto_url: str
    ) -> sqlite3.Row | None:
        """Registro com o MESMO conteúdo já capturado NESTE portal (AD-8).

        A comparação é pelo hash SHA-256 COMPLETO (``hash_sha256 = ?``) —
        NUNCA pelo prefixo 12-hex do ``id``: colisão de prefixo não pode virar
        alias/restauração falsa. O escopo do dedupe por hash é o PORTAL —
        cruzar portais da mesma instituição é decisão humana pendente (PRD
        OQ-4), nunca automática. A associação registro→portal vai pela URL de
        origem em ``candidatos``.
        """
        linhas = self.consultar(
            """
            SELECT d.* FROM documentos d
            JOIN candidatos c ON c.url = d.url_origem
            WHERE c.portal_id = ? AND d.hash_sha256 = ? AND d.url_origem <> ?
            ORDER BY d.rowid
            LIMIT 1
            """,
            (portal_id, hash_sha256, exceto_url),
        )
        return linhas[0] if linhas else None

    def contar_editais(self) -> int:
        return int(self.consultar("SELECT COUNT(*) FROM editais")[0][0])

    def contar_documentos(self) -> int:
        return int(self.consultar("SELECT COUNT(*) FROM documentos")[0][0])

    # -- ciclo de vida -----------------------------------------------------

    def fechar(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is not None:
            conn.close()
            self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> "Manifesto":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.fechar()
