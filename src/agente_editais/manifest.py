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

Story 4 adiciona a migração v4 (CAP-6): colunas de proveniência do texto em
``documentos`` — ``texto_caminho``/``texto_chars``/``texto_paginas``/
``extraido_em``. ``texto.py`` é o ÚNICO escritor destas colunas e da
``flag_escaneado`` (nascida NULL na v3) via UPDATE (AD-11: demais estágios
nunca INSERT em ``documentos``).

Story 5 adiciona a migração v5 (CAP-3): ``evidencias_datacao`` — prova bruta
POR FONTE consultada (FR-6 sem short-circuit), FK composta para
``documentos(id, url_origem)`` e PK tripla que dá idempotência (uma linha por
(documento, fonte)); ``fila_revisao`` — entidade própria da Fila de Revisão
Manual (FR-8) com UNIQUE PARCIAL por url onde pendente imposta pelo banco e a
decisão humana completa na própria linha (ano atribuído OU exclusão +
justificativa obrigatória + autoria + data); ``documentos.ano_aceito`` nasce
NULL com CHECK de janela e só é preenchido VIA ``aplicar_datacao`` (UPDATE,
AD-11); ``candidatos.texto_ancora`` preserva a âncora desde a descoberta
(FR-6 nasce na origem) — corpus antigo fica NULL = fonte indisponível.

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

SCHEMA_VERSAO_ATUAL = 5

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

# Story 4 (CAP-6): proveniência do texto — colunas que ``texto.py`` preenche
# via UPDATE quando extrai um Documento. ``flag_escaneado`` NÃO entra aqui:
# ela nasceu na v3 (NULL) e é apenas PREENCHIDA pelo extrator único.
_MIGRACAO_V4: tuple[str, ...] = (
    "ALTER TABLE documentos ADD COLUMN texto_caminho TEXT",
    "ALTER TABLE documentos ADD COLUMN texto_chars INTEGER",
    "ALTER TABLE documentos ADD COLUMN texto_paginas INTEGER",
    "ALTER TABLE documentos ADD COLUMN extraido_em TEXT",
)

# Story 5 (CAP-3): evidência bruta por fonte + fila humana fundamentada.
# ``evidencias_datacao`` tem FK COMPOSTA para a linha do Documento e PK
# tripla — regravar a mesma fonte na retomada substitui a própria linha sem
# duplicar. ``fila_revisao`` impõe "um item ATIVO por URL" no BANCO via
# índice parcial; motivo fica TEXT livre (fontes futuras podem motivar novos
# motivos sem migração). ``ano_aceito`` carrega CHECK de janela — ano fora
# de 2019–2026 é recusado pelo banco, nunca só pela camada.
_MIGRACAO_V5: tuple[str, ...] = (
    """
    CREATE TABLE evidencias_datacao (
        documento_id TEXT NOT NULL,
        url_origem   TEXT NOT NULL,
        fonte        TEXT NOT NULL CHECK (fonte IN ('url', 'ancora', 'pdf_meta')),
        valor_bruto  TEXT NOT NULL,
        localizacao  TEXT NOT NULL,
        criado_em    TEXT NOT NULL,
        PRIMARY KEY (documento_id, url_origem, fonte),
        FOREIGN KEY (documento_id, url_origem) REFERENCES documentos(id, url_origem)
    )
    """,
    "CREATE INDEX idx_evidencias_datacao_url ON evidencias_datacao(url_origem)",
    """
    CREATE TABLE fila_revisao (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        url_origem        TEXT NOT NULL,
        portal_id         INTEGER NOT NULL REFERENCES portais(id),
        motivo            TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'pendente'
                          CHECK (status IN ('pendente', 'resolvida')),
        criado_em         TEXT NOT NULL,
        decidido_ano      INTEGER
                          CHECK (decidido_ano IS NULL OR decidido_ano BETWEEN 2019 AND 2026),
        decidido_exclusao INTEGER NOT NULL DEFAULT 0 CHECK (decidido_exclusao IN (0, 1)),
        justificativa     TEXT,
        evidencia_anexa   TEXT,
        autor             TEXT,
        decidido_em       TEXT
    )
    """,
    "CREATE UNIQUE INDEX idx_fila_revisao_pendente_por_url "
    "ON fila_revisao(url_origem) WHERE status = 'pendente'",
    "CREATE INDEX idx_fila_revisao_url ON fila_revisao(url_origem)",
    "ALTER TABLE documentos ADD COLUMN ano_aceito "
    "INTEGER CHECK (ano_aceito IS NULL OR ano_aceito BETWEEN 2019 AND 2026)",
    "ALTER TABLE candidatos ADD COLUMN texto_ancora TEXT",
)

MIGRACOES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, _MIGRACAO_V1),
    (2, _MIGRACAO_V2),
    (3, _MIGRACAO_V3),
    (4, _MIGRACAO_V4),
    (5, _MIGRACAO_V5),
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

    def registrar_candidato(
        self, portal_id: int, url: str, tipo: str, *, texto_ancora: str | None = None
    ) -> bool:
        """Registra candidato a edital dedupe por UNIQUE(portal_id,url); True se novo.

        Segunda execução sobre o mesmo achado NÃO duplica linha (AD-1/AD-8):
        o banco, não memória de processo, é a fonte do "já visto" — o retorno
        continua significando "inserido AGORA". ``texto_ancora`` preserva a
        âncora integral (TEXT livre) desde a descoberta (FR-6); numa
        RE-DESCOBERTA o upsert preenche a âncora só quando ela ainda é NULL
        — corpus antigo sem âncora é retroalimentado e uma âncora já gravada
        NUNCA é sobrescrita.
        """
        if tipo not in ("pdf", "pagina_edital"):
            raise ValueError(f"tipo de candidato inválido: {tipo!r} (esperado pdf|pagina_edital)")
        conn = self._garantir_aberto()
        existente = conn.execute(
            "SELECT 1 FROM candidatos WHERE portal_id = ? AND url = ?",
            (portal_id, url),
        ).fetchone()
        cursor = conn.execute(
            """
            INSERT INTO candidatos (portal_id, url, tipo, texto_ancora, descoberto_em)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(portal_id, url) DO UPDATE
            SET texto_ancora = COALESCE(texto_ancora, excluded.texto_ancora)
            """,
            (portal_id, url, tipo, texto_ancora, agora_iso()),
        )
        return existente is None and cursor.rowcount > 0

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

    def limpar_secoes_do_portal(self, portal_id: int) -> int:
        """Apaga o registro de seções visitadas de UM portal (operação --revisitar).

        Usada quando a curadoria muda o comportamento esperado da navegação
        (ex.: portal marcado dinamico=true após 1ª rodada estática). O DELETE
        é registrado como evento pelo chamador — nunca silencioso (AD-10).
        """
        with self.transacao() as conn:
            cursor = conn.execute(
                "DELETE FROM secoes_visitadas WHERE portal_id = ?", (portal_id,)
            )
            return int(cursor.rowcount)

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

    # -- texto (CAP-6, migração v4) ------------------------------------------

    def documentos_do_portal(self, portal_id: int) -> list[sqlite3.Row]:
        """Documentos ligados ao portal VIA ``candidatos`` tipo 'pdf' (lote estável).

        Mesma costura coleta→portal da captura e MESMA regra de membria de
        ``candidatos_pdf_do_portal`` (tipo = 'pdf'); a subquery DISTINCT protege
        contra fan-out caso a mesma URL apareça mais de uma vez nos candidatos
        do portal. O estágio de texto não cria Documento (AD-11), apenas os
        consome na ordem em que nasceram (``rowid``).
        """
        return self.consultar(
            """
            SELECT d.* FROM documentos d
            JOIN (
                SELECT DISTINCT url FROM candidatos
                WHERE portal_id = ? AND tipo = 'pdf'
            ) c ON c.url = d.url_origem
            ORDER BY d.rowid
            """,
            (portal_id,),
        )

    def registrar_texto_extraido(
        self,
        documento_id: str,
        url_origem: str,
        *,
        texto_caminho: str,
        texto_chars: int,
        texto_paginas: int,
        flag_escaneado: bool,
    ) -> bool:
        """UPDATE de proveniência do estágio texto (AD-11: nunca INSERT aqui).

        Preenche as colunas da v4 e a ``flag_escaneado`` nascida NULL na v3;
        ``extraido_em`` vai NORMALIZADO em UTC (mesma convenção de
        ``data_captura`` — carimbo comparável entre fusos). Retorna True só se
        alguma linha casou — UPDATE de 0 linhas NÃO é sucesso silencioso: o
        chamador trata como erro de persistência.
        """
        with self.transacao() as conn:
            cursor = conn.execute(
                """
                UPDATE documentos
                SET texto_caminho = ?, texto_chars = ?, texto_paginas = ?,
                    flag_escaneado = ?, extraido_em = ?
                WHERE id = ? AND url_origem = ?
                """,
                (
                    texto_caminho,
                    texto_chars,
                    texto_paginas,
                    1 if flag_escaneado else 0,
                    agora_iso_utc(),
                    documento_id,
                    url_origem,
                ),
            )
            return cursor.rowcount > 0

    # -- datacao (CAP-3, migração v5) -----------------------------------------
    # UPDATE-only sobre ``documentos`` (AD-11): a datação NUNCA INSERTa em
    # editais/documentos nem parseia PDF — grava evidências, aplica o aceite
    # e roteia à fila; a decisão humana vive inteira em ``fila_revisao``.

    def registrar_evidencias(
        self,
        documento_id: str,
        url_origem: str,
        evidencias: list[tuple[str, str, str]],
    ) -> int:
        """Grava a evidência bruta de CADA fonte consultada (FR-6/CAP-3).

        ``evidencias`` são triplas (fonte, valor_bruto, localizacao). O
        INSERT OR REPLACE sobre a PK (documento_id, url_origem, fonte) torna
        a operação idempotente: reprocessar o documento substitui a própria
        linha em vez de duplicar. Devolve quantas linhas foram gravadas.
        """
        for fonte, _valor, _local in evidencias:
            if fonte not in ("url", "ancora", "pdf_meta"):
                raise ValueError(f"fonte de evidência inválida: {fonte!r}")
        with self.transacao() as conn:
            agora = agora_iso()
            for fonte, valor_bruto, localizacao in evidencias:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO evidencias_datacao
                        (documento_id, url_origem, fonte, valor_bruto, localizacao, criado_em)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (documento_id, url_origem, fonte, valor_bruto, localizacao, agora),
                )
            return len(evidencias)

    def aplicar_datacao(
        self, documento_id: str, url_origem: str, *, metodo: str, ano: int
    ) -> bool:
        """UPDATE do aceite de datação no Documento (AD-11: nunca INSERT).

        Preenche ``metodo_datacao`` (∈ url|ancora|pdf_meta — cascata FR-7) e
        ``ano_aceito`` (CHECK 2019–2026 no banco). Retorna True só se alguma
        linha casou — UPDATE de 0 linhas NÃO é sucesso silencioso.
        """
        if metodo not in ("url", "ancora", "pdf_meta"):
            raise ValueError(f"método de datação inválido: {metodo!r} (cascata FR-7)")
        with self.transacao() as conn:
            cursor = conn.execute(
                """
                UPDATE documentos
                SET metodo_datacao = ?, ano_aceito = ?
                WHERE id = ? AND url_origem = ?
                """,
                (metodo, ano, documento_id, url_origem),
            )
            return cursor.rowcount > 0

    def enfileirar(self, url_origem: str, portal_id: int, motivo: str) -> bool:
        """Roteia uma URL à Fila de Revisão Manual (FR-8); True se inserido.

        O UNIQUE parcial por url onde pendente impõe "um item ativo por URL"
        NO BANCO. Só o conflito com ESSE índice é tratado como "já
        enfileirado" (False, idempotente); qualquer OUTRA IntegrityError
        (FK inexistente etc.) PROPAGA — engoli-la esconderia bug real como
        sucesso mudo. Evidências coletadas ficam em ``evidencias_datacao``
        ligadas pela mesma url — nada é descartado ao enfileirar.
        """
        try:
            with self.transacao() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO fila_revisao (url_origem, portal_id, motivo, status, criado_em)
                    VALUES (?, ?, ?, 'pendente', ?)
                    """,
                    (url_origem, portal_id, motivo, agora_iso()),
                )
                return cursor.rowcount > 0
        except sqlite3.IntegrityError as exc:
            # O conflito com o índice parcial ÚNICO chega como violação de
            # coluna ("UNIQUE constraint failed: fila_revisao.url_origem") ou
            # citando o índice, conforme a engine — só esse caso é "já
            # enfileirado"; qualquer outra violação (FK, CHECK, NOT NULL)
            # PROPAGA em vez de virar sucesso mudo.
            mensagem = str(exc)
            duplicado_pendente = (
                "idx_fila_revisao_pendente_por_url" in mensagem
                or (
                    "UNIQUE constraint failed" in mensagem
                    and "fila_revisao.url_origem" in mensagem
                )
            )
            if duplicado_pendente:
                return False
            raise

    def tem_item_de_fila(self, url_origem: str) -> bool:
        """True se a url JÁ TEM item na fila — pendente OU resolvida.

        Base do pulo idempotente da datação (AC da story: execução repetida
        ⇒ zero re-decisões e fila intacta): itens resolvidos também travam o
        reprocessamento, senão toda execução re-enfileiraria o que a decisão
        humana já resolveu ("retomada não refaz nem duplica").
        """
        return bool(
            self.consultar(
                "SELECT 1 FROM fila_revisao WHERE url_origem = ? LIMIT 1",
                (url_origem,),
            )
        )

    def consultar_fila(self, status: str | None = "pendente") -> list[sqlite3.Row]:
        """Itens da fila na ordem de criação; ``None`` lista todos os status."""
        if status is None:
            return self.consultar("SELECT * FROM fila_revisao ORDER BY id")
        return self.consultar(
            "SELECT * FROM fila_revisao WHERE status = ? ORDER BY id", (status,)
        )

    def registrar_decisao_fila(
        self,
        fila_id: int,
        *,
        decidido_ano: int | None,
        decidido_exclusao: bool,
        justificativa: str,
        autor: str,
        evidencia_anexa: str | None = None,
    ) -> bool:
        """Grava a decisão humana SOBRE um item pendente (FR-8) — transacional.

        Exige EXATAMENTE um destino (--ano AAAA XOR --excluir), justificativa
        textual e autoria, e ano dentro da janela 2019–2026 — recusa decidir
        fora do contrato com ``ValueError`` (contrato uniforme para o
        chamador de biblioteca; o CHECK do banco segue como última defesa).
        Só afeta linhas PENDENTES: re-decidir item resolvido retorna False.
        """
        if decidido_exclusao == (decidido_ano is not None):
            raise ValueError(
                "a decisão deve ter exatamente UM destino: ano atribuído (2019–2026) "
                "OU exclusão — nunca ambos nem nenhum."
            )
        if decidido_ano is not None and not (2019 <= decidido_ano <= 2026):
            raise ValueError(
                f"ano atribuído {decidido_ano} está fora da janela fixa 2019–2026 "
                "(Never da story: ano fora da janela nunca é aceito)."
            )
        if not justificativa or not justificativa.strip():
            raise ValueError("justificativa é obrigatória para decidir (FR-8).")
        if not autor or not autor.strip():
            raise ValueError("autoria é obrigatória para decidir (FR-8).")
        with self.transacao() as conn:
            cursor = conn.execute(
                """
                UPDATE fila_revisao
                SET status = 'resolvida',
                    decidido_ano = ?,
                    decidido_exclusao = ?,
                    justificativa = ?,
                    evidencia_anexa = ?,
                    autor = ?,
                    decidido_em = ?
                WHERE id = ? AND status = 'pendente'
                """,
                (
                    decidido_ano,
                    1 if decidido_exclusao else 0,
                    justificativa.strip(),
                    evidencia_anexa,
                    autor.strip(),
                    agora_iso(),
                    fila_id,
                ),
            )
            return cursor.rowcount > 0

    def contar_documentos_datados(self) -> int:
        """Documentos COM data de publicação definida (métrica do status).

        Soma, sem duplicar por url (UNION), os dois destinos que fixam ano:
        aceites automáticos (``metodo_datacao`` preenchido) e decisões
        humanas com ano atribuído (fila RESOLVIDA com ``decidido_ano``) —
        itens resolvidos com EXCLUSÃO não contam como datados.
        """
        return int(
            self.consultar(
                """
                SELECT COUNT(*) FROM (
                    SELECT url_origem FROM documentos
                    WHERE metodo_datacao IS NOT NULL
                    UNION
                    SELECT url_origem FROM fila_revisao
                    WHERE status = 'resolvida' AND decidido_ano IS NOT NULL
                )
                """
            )[0][0]
        )

    def contar_fila(self, status: str | None = None) -> int:
        if status is None:
            return int(self.consultar("SELECT COUNT(*) FROM fila_revisao")[0][0])
        return int(
            self.consultar(
                "SELECT COUNT(*) FROM fila_revisao WHERE status = ?", (status,)
            )[0][0]
        )

    def evidencias_da_url(self, url_origem: str) -> list[sqlite3.Row]:
        """Evidências brutas coletadas para a URL — exibição na fila."""
        return self.consultar(
            """
            SELECT fonte, valor_bruto, localizacao, criado_em
            FROM evidencias_datacao
            WHERE url_origem = ?
            ORDER BY rowid
            """,
            (url_origem,),
        )

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
