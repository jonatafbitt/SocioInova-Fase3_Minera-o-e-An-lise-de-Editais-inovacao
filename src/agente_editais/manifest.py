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

Story 6 adiciona superfícies ONLY-leitura de consulta (CAP-9/UJ-3/§10):
``listar_l1`` (unidade = Documento com ano EFETIVO ``COALESCE(ano_aceito,
decidido_ano)`` e origem explícita ``ano_fonte``, excluídos fora da
listagem mas contáveis) e ``custodia_do_edital`` (cadeia captura →
datação → fila montada inteira do banco — nenhum PDF é lido, AD-4).

Story 8 adiciona a migração v7: ampliação das categorias de pró-reitoria.
O CHECK fechado de ``portais.categoria`` muda (nova Story 8) e, como o SQLite
não suporta ALTER de CHECK, a tabela ``portais`` é RECRIADA de forma segura
(table rename + create + copy + drop) dentro da transação exclusiva de
startup, preservando dados e FK via reconstrução dos índices de REFERENCIA
(``PRAGMA foreign_keys=ON`` durante a migração exige ``legacy_alter_table``
desligado pelos passos de rename/copy padrão do SQLite — a ordem "criar
nova → copiar → drop antiga → rename" mantém as FKs das tabelas filhas
apontando para o alvo físico correto). Antes de QUALQUER migração pendente,
um backup de checkpoint é produzido (PRAGMA wal_checkpoint(TRUNCATE)) —
a custódia do corpus nunca é tocada sem ponto de restauração.

Story 7 adiciona a migração v6 (CAP-7): ``lotes_l2`` — assinatura COMPLETA
do instrumento congelado por lote (FR-18/AD-6): modelo, versao_do_modelo,
prompt_versao + prompt_sha256, temperatura, seed, codebook_sha256,
versao_agente e schema_version sob UNIQUE composto (a retomada reencontra o
lote pela assinatura exata; qualquer componente diferente ⇒ NOVO lote,
delimitação explícita das linhas por lote_id); e ``catalogo_l2`` — PK
(edital_id, campo, lote_id) que dá idempotência à gravação por edital, FK
COMPOSTA para o documento citado (documento_id, url_origem), citação-evidência
(trecho literal + página) e ``verificacao`` CHECK ok|citacao_invalidada — o
antídoto a alucinação (FR-18): campo com citação inexistente no texto nasce
marcado e fora do catálogo válido, sem perder custódia.
Helpers transacionais do estágio: ``obter_ou_abrir_lote`` (mesma assinatura
continua o lote; só cria quando nenhuma casa), ``registrar_campos_l2``
(transação ÚNICA por edital, INSERT OR REPLACE pela PK), ``fechar_lote``,
``editais_codificados_no_lote`` (pulo idempotente da retomada) e
``documentos_do_portal_para_analise`` (leitura com a exclusão VIGENTE da
fila já resolvida por documento).

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

from agente_editais import __version__

ENGINE_MINIMA = (3, 51, 3)
LOCK_TIMEOUT_MS = 1_500

SCHEMA_VERSAO_ATUAL = 11

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

# Story 7 (CAP-7): lote = instrumento congelado (FR-18/AD-6) e catálogo com
# verificação de citação no caminho de gravação (FR-18). ``lotes_l2`` carrega
# a assinatura INTEIRA do instrumento sob UNIQUE — a retomada reencontra o
# lote aberto pela assinatura exata e qualquer componente diferente nasce em
# outro lote (delimitação explícita por lote_id). ``seed`` pode ser NULL
# (provedores sem seed fixa): a busca por assinatura usa IS, que casa NULL
# com NULL — o UNIQUE do banco trata NULLs como distintos, mas o lock AD-3
# garante processo único, então a idempotência vale na camada de aplicação.
# ``catalogo_l2``: valor é TEXT uniforme ('0'/'1'/'2' ordinais; rótulo
# nominal; 'N/A'); FK COMPOSTA para o documento citado; CHECK obriga
# documento citado para todo valor ≠ 'N/A' e página ≥ 1 quando presente.
_MIGRACAO_V6: tuple[str, ...] = (
    """
    CREATE TABLE lotes_l2 (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        modelo           TEXT NOT NULL,
        versao_do_modelo TEXT NOT NULL DEFAULT '',
        prompt_versao    TEXT NOT NULL,
        prompt_sha256    TEXT NOT NULL,
        temperatura      REAL NOT NULL,
        seed             INTEGER,
        codebook_sha256  TEXT NOT NULL,
        versao_agente    TEXT NOT NULL,
        schema_version   INTEGER NOT NULL,
        status           TEXT NOT NULL DEFAULT 'aberto'
                         CHECK (status IN ('aberto', 'concluido')),
        aberto_em        TEXT NOT NULL,
        concluido_em     TEXT,
        UNIQUE (modelo, versao_do_modelo, prompt_versao, prompt_sha256,
                temperatura, seed, codebook_sha256, versao_agente, schema_version)
    )
    """,
    """
    CREATE TABLE catalogo_l2 (
        edital_id      TEXT NOT NULL REFERENCES editais(id),
        campo          TEXT NOT NULL,
        lote_id        INTEGER NOT NULL REFERENCES lotes_l2(id),
        valor          TEXT NOT NULL,
        documento_id   TEXT,
        url_origem     TEXT,
        citacao_trecho TEXT,
        citacao_pagina INTEGER,
        verificacao    TEXT NOT NULL CHECK (verificacao IN ('ok', 'citacao_invalidada')),
        gravado_em     TEXT NOT NULL,
        PRIMARY KEY (edital_id, campo, lote_id),
        CHECK (documento_id IS NOT NULL OR valor = 'N/A'),
        CHECK (citacao_pagina IS NULL OR citacao_pagina >= 1),
        FOREIGN KEY (documento_id, url_origem) REFERENCES documentos(id, url_origem)
    )
    """,
    "CREATE INDEX idx_catalogo_l2_lote ON catalogo_l2(lote_id)",
    "CREATE INDEX idx_catalogo_l2_documento ON catalogo_l2(documento_id, url_origem)",
)

# Story 8 (categorias): o CHECK fechado da v1 não aceita os novos valores de
# pró-reitoria. Como o SQLite não permite ALTER de CHECK, a tabela ``portais``
# é recriada: tabela nova (mesmos nomes de coluna, CHECK ampliado), cópia da
# v6 para a v7 (``prpgi_prppg`` → ``prppg_inovacao``), drop da antiga e rename.
# A recriação é feita DENTRO da transação exclusiva de startup; as FKs das
# tabelas filhas (``candidatos``/``secoes_visitadas``/``fila_revisao``) referem
# ``portais(id)`` por nome de tabela. O runner desliga FKs temporariamente
# antes de executar esta migração (o PRAGMA foreign_keys só tem efeito fora
# de transação). A cópia reatribui a categoria antiga ao novo nome canônico.
_MIGRACAO_V7: tuple[str, ...] = (
    """
    CREATE TABLE portais_v7 (
        id                  INTEGER PRIMARY KEY,
        instituicao_id      INTEGER NOT NULL REFERENCES instituicoes(id),
        nome                TEXT NOT NULL,
        categoria           TEXT NOT NULL
            CHECK (categoria IN (
                'integra', 'prppg_inovacao', 'extensao', 'ensino',
                'reitoria', 'nit', 'agencia_inovacao'
            )),
        url                 TEXT NOT NULL UNIQUE,
        dinamico            INTEGER NOT NULL DEFAULT 0 CHECK (dinamico IN (0, 1)),
        profundidade_maxima INTEGER NOT NULL DEFAULT 3 CHECK (profundidade_maxima > 0),
        criado_em           TEXT NOT NULL
    )
    """,
    """
    INSERT INTO portais_v7 (
        id, instituicao_id, nome, categoria, url, dinamico,
        profundidade_maxima, criado_em
    )
    SELECT id, instituicao_id, nome,
           CASE categoria
               WHEN 'prpgi_prppg' THEN 'prppg_inovacao'
               ELSE categoria
           END,
           url, dinamico, profundidade_maxima, criado_em
    FROM portais
    """,
    "DROP TABLE portais",
    "ALTER TABLE portais_v7 RENAME TO portais",
)

# Story 9 (varredura sob demanda + classificação): ``varreduras`` é a fila de
# URLs coladas (uma por URL — UNIQUE, AD-8; o portal de destino vem resolvido
# por hostname contra o mapa-mestre). ``classificacoes`` guarda o veredito
# ADVISORY do tipo de edital por ``url_origem`` (PK lógica) — ``metodo`` é a
# chave do "não sobrescrever" (Never da story: re-classificação só preenche
# quando o resultado é ``sem_texto``; nunca pisa ``metodo in ('texto','ancora')``).
# Assim como ``referencia_para``, não há FK simples para ``documentos`` — a
# linha do documento é COMPOSTA (id, url_origem); a integridade fica na camada
# de classificação + testes.
_MIGRACAO_V8: tuple[str, ...] = (
    """
    CREATE TABLE varreduras (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        url          TEXT NOT NULL UNIQUE,
        portal_id    INTEGER NOT NULL REFERENCES portais(id),
        status       TEXT NOT NULL DEFAULT 'pendente'
                     CHECK (status IN ('pendente', 'rodando', 'concluida', 'falhou')),
        log          TEXT NOT NULL DEFAULT '[]',
        criado_em    TEXT NOT NULL,
        iniciado_em  TEXT,
        concluido_em TEXT
    )
    """,
    "CREATE INDEX idx_varreduras_status ON varreduras(status)",
    "CREATE INDEX idx_varreduras_portal ON varreduras(portal_id)",
    """
    CREATE TABLE classificacoes (
        url_origem      TEXT PRIMARY KEY,
        tipo_edital     TEXT NOT NULL
                        CHECK (tipo_edital IN ('inovacao', 'nao_inovacao', 'sem_texto')),
        metodo          TEXT NOT NULL
                        CHECK (metodo IN ('texto', 'ancora', 'sem_texto')),
        sinais          TEXT NOT NULL DEFAULT '[]',
        dimensoes       TEXT NOT NULL DEFAULT '[]',
        fonte_ancora    TEXT,
        classificado_em TEXT NOT NULL,
        CHECK (
            (metodo = 'sem_texto' AND tipo_edital = 'sem_texto')
            OR (metodo IN ('texto', 'ancora')
                AND tipo_edital IN ('inovacao', 'nao_inovacao'))
        )
    )
    """,
    "CREATE INDEX idx_classificacoes_tipo ON classificacoes(tipo_edital)",
)

# Story 10 (Eixo 3 — Relevância e Impacto Social): adiciona colunas para
# classificação do Eixo 3 na tabela classificacoes existente.
# Colunas novas nascem com DEFAULT para não quebrar linhas existentes.
_MIGRACAO_V9: tuple[str, ...] = (
    """
    ALTER TABLE classificacoes ADD COLUMN eixo3_classificacao TEXT
        DEFAULT 'AUSENTE_SILENCIAMENTO'
        CHECK (eixo3_classificacao IN ('IMPACTO_INSTRUMENTAL', 'IMPACTO_SUBSTANTIVO', 'AUSENTE_SILENCIAMENTO'))
    """,
    """
    ALTER TABLE classificacoes ADD COLUMN eixo3_trecho_comprobatorio TEXT
        DEFAULT ''
    """,
    "CREATE INDEX idx_classificacoes_eixo3 ON classificacoes(eixo3_classificacao)",
)

# Story da varredura integrada ao pipeline (rastreabilidade varredura→candidato):
# ``varredura_id`` nasce NULL (candidatos do ``descobrir``/``coletar`` não têm
# origem em varredura) e é preenchido APENAS no INSERT de um candidato NOVO
# descoberto por ``varredura rodar`` — um candidato já conhecido do Mapa-Mestre
# (dedupe por UNIQUE(portal_id,url)) NUNCA tem a origem reatribuída (a descoberta
# original "vence"). O índice torna a consulta "candidatos de uma varredura"
# O(1) para a exibição e o recorte ``--varredura`` dos estágios.
_MIGRACAO_V10: tuple[str, ...] = (
    "ALTER TABLE candidatos ADD COLUMN varredura_id INTEGER REFERENCES varreduras(id)",
    "CREATE INDEX idx_candidatos_varredura ON candidatos(varredura_id)",
)

# Resgate por OCR dos documentos escaneados (CAP-6/AD-11, Fase 3.1): colunas
# de proveniência do resgate opcional e NÃO automático (comando ``ocrescer``).
# Nascem NULL; ``ocr_em`` preenchido é a trava de retomada (documento com OCR
# aplicado NÃO é reprocessado). O ``.txt`` irmão continua o único artefato de
# texto (AD-2); aqui só a proveniência. Índice parcial: só linhas com OCR.
_MIGRACAO_V11: tuple[str, ...] = (
    "ALTER TABLE documentos ADD COLUMN ocr_em TEXT",
    "ALTER TABLE documentos ADD COLUMN ocr_metodo TEXT",
    "ALTER TABLE documentos ADD COLUMN ocr_confianca_media REAL",
    "ALTER TABLE documentos ADD COLUMN ocr_paginas_resgatadas INTEGER",
    "ALTER TABLE documentos ADD COLUMN ocr_tentativas INTEGER",
    "CREATE INDEX idx_documentos_ocr ON documentos(ocr_em) WHERE ocr_em IS NOT NULL",
)

MIGRACOES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, _MIGRACAO_V1),
    (2, _MIGRACAO_V2),
    (3, _MIGRACAO_V3),
    (4, _MIGRACAO_V4),
    (5, _MIGRACAO_V5),
    (6, _MIGRACAO_V6),
    (7, _MIGRACAO_V7),
    (8, _MIGRACAO_V8),
    (9, _MIGRACAO_V9),
    (10, _MIGRACAO_V10),
    (11, _MIGRACAO_V11),
)


def _declaracoes_da_migracao(numero_alvo: int) -> tuple[str, ...]:
    """Declarações da migração ``numero_alvo``, localizada pelo NUMERO.

    Sempre busca pelo `numero` em ``MIGRACOES`` (nunca por posição literal):
    adicionar/reordenar migrações não desalinha a v7/v8, que rodam em fluxos
    próprios fora da transação de startup.
    """
    for numero, declaracoes in MIGRACOES:
        if numero == numero_alvo:
            return declaracoes
    raise ValueError(f"migração v{numero_alvo} não registrada em MIGRACOES")


# -- consulta L1 (CAP-9/UJ-3/§10): definição ÚNICA compartilhada pelas leituras
#
# Ano EFETIVO = aceite automático OU decisão humana da fila (deferred-work D1
# parcialmente endereçado na LEITURA — a fila segue sendo a fonte de verdade
# das decisões humanas; nenhum dado é migrado). Membresia documento→portal é
# VIA ``candidatos`` (mesma costura da captura, tipo='pdf'); o GROUP BY por
# url garante UMA linha por documento mesmo se a mesma URL já foi registrada
# em dois portais — contagens coerentes nunca fan-out.
#
# Precedência ÚNICA por URL na fila (_FILA_VIGENTE, patch 3): uma única linha
# de ``fila_revisao`` entra no jogo — RESOLVIDA vence PENDENTE; entre múltiplas
# resolvidas, a de MAIOR id (decisão mais recente); sem resolvida, a pendente
# mais recente (ano segue vazio de qualquer forma). A subquery é COMPARTILHADA
# por ``listar_l1``, ``contar_l1_excluidos`` e ``custodia_do_edital`` — listar
# e custódia nunca divergem sobre qual decisão vale. A exclusão passa a ser
# decidida pela linha VIGENTE (não por EXISTS-any), mantendo listagem e
# resumo consistentes mesmo em patologias de duas resoluções na mesma URL.
_FILA_VIGENTE = """
    SELECT url_origem,
           CASE WHEN MAX(CASE WHEN status = 'resolvida' THEN id END) IS NOT NULL
                THEN MAX(CASE WHEN status = 'resolvida' THEN id END)
                ELSE MAX(id)
           END AS id_fila_vigente
    FROM fila_revisao
    GROUP BY url_origem
"""

_L1_ANO_EFETIVO = "COALESCE(d.ano_aceito, f.decidido_ano)"

_L1_FONTE_ANO = """
CASE
    WHEN d.ano_aceito IS NOT NULL THEN 'automatica'
    WHEN f.decidido_ano IS NOT NULL THEN 'fila_humana'
    ELSE 'vazio'
END"""

_L1_JOINS = f"""
FROM documentos d
JOIN editais e ON e.id = d.edital_id
JOIN instituicoes i ON i.id = e.instituicao_id
LEFT JOIN (
    SELECT url, MIN(portal_id) AS portal_id
    FROM candidatos
    WHERE tipo = 'pdf'
    GROUP BY url
) c ON c.url = d.url_origem
LEFT JOIN portais p ON p.id = c.portal_id
LEFT JOIN ({_FILA_VIGENTE}) fv ON fv.url_origem = d.url_origem
LEFT JOIN fila_revisao f ON f.id = fv.id_fila_vigente
"""

# Exclusão decidida SOMENTE pela linha vigente do join (≤ 1 linha por URL):
# pendentes/ausentes ⇒ 0; decisão vigente de exclusão ⇒ 1.
_L1_EXCLUIDO_VIGENTE = "COALESCE(f.decidido_exclusao, 0) = 1"
_L1_NAO_EXCLUIDO_VIGENTE = "COALESCE(f.decidido_exclusao, 0) = 0"

# Ordenação com anos vazios POR ÚLTIMO (patch 9): NULLs primeiro fariam os
# pendentes dominarem o topo da listagem.
_L1_ORDEM = (
    f"ORDER BY CASE WHEN {_L1_ANO_EFETIVO} IS NULL THEN 1 ELSE 0 END, "
    "i.sigla, ano, d.edital_id, d.id, d.url_origem"
)

_L1_COLUNAS = f"""
    d.id AS documento_id,
    d.edital_id,
    i.sigla AS instituicao,
    p.categoria,
    {_L1_ANO_EFETIVO} AS ano,
    {_L1_FONTE_ANO} AS ano_fonte,
    d.metodo_datacao,
    d.url_origem,
    d.data_captura,
    d.hash_sha256,
    d.caminho
"""


def _filtros_l1(
    instituicao: str | None,
    ano: int | None,
    categoria: str | None,
    *,
    excluidos: bool,
    ignorar_ano: bool = False,
    edital: str | None = None,
) -> tuple[str, tuple]:
    """WHERE compartilhado da consulta L1 — MESMA definição de linhas sempre.

    ``excluidos=False`` lista os publicáveis (exclusão NÃO decidida na linha
    vigente); ``True`` devolve só os excluídos — população do resumo, nunca
    silenciada. Filtros combinam por E (AND); ``None`` = filtro ausente.
    ``ignorar_ano=True`` (usado pela contagem de excluídos, patch 4) aplica
    instituição/categoria mas IGNORA o filtro de ano: a população excluída
    não participa da janela de listagem — com ``--ano``, o resumo continua
    honesto em vez de reportar "excluídos: 0" enganoso. ``edital`` filtra
    por subtrecho do id do edital (LIKE %edital%, case-insensitive).
    """
    predicado_exclusao = _L1_EXCLUIDO_VIGENTE if excluidos else _L1_NAO_EXCLUIDO_VIGENTE
    clausulas = [predicado_exclusao]
    parametros: list[Any] = []
    if instituicao is not None:
        clausulas.append("UPPER(i.sigla) = UPPER(?)")
        parametros.append(instituicao)
    if ano is not None and not ignorar_ano:
        clausulas.append(f"{_L1_ANO_EFETIVO} = ?")
        parametros.append(ano)
    if categoria is not None:
        clausulas.append("p.categoria = ?")
        parametros.append(categoria)
    if edital is not None:
        clausulas.append("UPPER(d.edital_id) LIKE UPPER(?)")
        parametros.append(f"%{edital}%")
    return " AND ".join(clausulas), tuple(parametros)


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
            # Backup de checkpoint ANTES de qualquer migração PENDENTE: força o
            # descarregamento do WAL para o arquivo principal, produzindo um ponto
            # de restauração físico antes de tocar o schema (a custódia nunca é
            # migrada sem backup). Só roda quando há migração a aplicar — um
            # Manifesto já no schema atual abre SEM checkpoint, permitindo leituras
            # concorrentes (ex.: o dashboard, AD-3/AD-4). Executa FORA da transação
            # EXCLUSIVE — wal_checkpoint não roda dentro de transação.
            self._backup_pre_migracao()
            self._startup_exclusivo()
        except ErroManifestoOcupado:
            self.fechar()
            raise
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

        Migrações v1..v6 e v8 rodam dentro da transação. Migração v7 (recria
        portais com novo CHECK) desliga as FKs fora de transação (o PRAGMA
        foreign_keys só tem efeito fora dela) e então roda na própria BEGIN
        EXCLUSIVE — a recriação é atômica e retomável.
        """
        # 1) Migrações v1..v6 dentro da transação exclusiva
        self._conn.execute("BEGIN EXCLUSIVE")
        try:
            self._aplicar_migracoes_ate_v6()
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

        # 2) Migração v7 (se pendente) fora da transação com FKs desligadas
        self._aplicar_migracao_v7_se_pendente()

        # 3) Migração v8 (se pendente) dentro de transação exclusiva própria
        # (DDL puro — varreduras/classificacoes; não precisa de PRAGMA off)
        self._aplicar_migracao_v8_se_pendente()

        # 4) Migração v9 (se pendente) — colunas Eixo 3
        self._aplicar_migracao_v9_se_pendente()

        # 5) Migração v10 (se pendente) — varredura_id em candidatos
        self._aplicar_migracao_v10_se_pendente()

        # 6) Migração v11 (se pendente) — proveniência OCR em documentos
        self._aplicar_migracao_v11_se_pendente()

    def _aplicar_migracoes_ate_v6(self) -> list[int]:
        """Aplica migrações 1..6 dentro da transação exclusiva de startup."""
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
            if numero >= 7:
                break  # v7+ rodam em fluxos próprios fora desta transação
            for declaracao in declaracoes:
                self._conn.execute(declaracao)
            self._conn.execute(f"PRAGMA user_version = {numero}")
            versao_atual = numero
            aplicadas.append(numero)
        return aplicadas

    def _aplicar_migracao_v7_se_pendente(self) -> None:
        """Aplica migração v7 (recria ``portais``) de forma ATÔMICA e retomável.

        As FKs são desligadas fora de transação (o PRAGMA foreign_keys só tem
        efeito fora dela — é o que permite ``DROP TABLE portais`` com filhas);
        depois os quatro passos da v7 rodam numa BEGIN EXCLUSIVE própria, junto
        com o bump de ``user_version``. Um processo morto no meio não deixa
        schema parcial: ou tudo (re)cria, ou nada — a próxima abertura retoma
        do zero sem "already exists".
        """
        linha = self._conn.execute("PRAGMA user_version").fetchone()
        versao_atual = int(linha[0])
        if versao_atual >= 7:
            return
        # Desliga FKs para permitir DROP TABLE portais com tabelas filhas
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.execute("BEGIN EXCLUSIVE")
            try:
                for declaracao in _declaracoes_da_migracao(7):
                    self._conn.execute(declaracao)
                self._conn.execute("PRAGMA user_version = 7")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")

    def _aplicar_migracao_v8_se_pendente(self) -> None:
        """Aplica migração v8 (varreduras/classificacoes) numa transação própria.

        A v7 precisa de FKs desligadas (recria ``portais``), então a v8 — um
        DDL puro de tabelas novas — roda DEPOIS dela num ``BEGIN EXCLUSIVE``
        próprio, sempre dentro do fluxo exclusivo de startup (Design Notes).
        """
        linha = self._conn.execute("PRAGMA user_version").fetchone()
        if int(linha[0]) >= 8:
            return
        self._conn.execute("BEGIN EXCLUSIVE")
        try:
            for declaracao in _declaracoes_da_migracao(8):
                self._conn.execute(declaracao)
            self._conn.execute("PRAGMA user_version = 8")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _aplicar_migracao_v9_se_pendente(self) -> None:
        """Aplica migração v9 (colunas Eixo 3 em classificacoes).

        DDL puro (ALTER TABLE ADD COLUMN) — roda depois da v8 numa transação
        EXCLUSIVE própria, dentro do fluxo de startup.
        """
        linha = self._conn.execute("PRAGMA user_version").fetchone()
        if int(linha[0]) >= 9:
            return
        self._conn.execute("BEGIN EXCLUSIVE")
        try:
            for declaracao in _declaracoes_da_migracao(9):
                self._conn.execute(declaracao)
            self._conn.execute("PRAGMA user_version = 9")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _aplicar_migracao_v10_se_pendente(self) -> None:
        """Aplica migração v10 (varredura_id em candidatos).

        DDL puro (ALTER TABLE ADD COLUMN + índice) — roda depois da v9 numa
        transação EXCLUSIVE própria, dentro do fluxo de startup. Candidatos
        existentes nascem com ``varredura_id`` NULL (origem em varredura não é
        retro-inferida — AD-1: re-execuções não reatribuem origem).
        """
        linha = self._conn.execute("PRAGMA user_version").fetchone()
        if int(linha[0]) >= 10:
            return
        self._conn.execute("BEGIN EXCLUSIVE")
        try:
            for declaracao in _declaracoes_da_migracao(10):
                self._conn.execute(declaracao)
            self._conn.execute("PRAGMA user_version = 10")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _aplicar_migracao_v11_se_pendente(self) -> None:
        """Aplica migração v11 (proveniência OCR em documentos).

        DDL puro (ALTER TABLE ADD COLUMN + índice parcial) — roda depois da
        v10 numa transação EXCLUSIVE própria, dentro do fluxo de startup.
        Colunas nascem NULL; nenhum dado existente é tocado.
        """
        linha = self._conn.execute("PRAGMA user_version").fetchone()
        if int(linha[0]) >= 11:
            return
        self._conn.execute("BEGIN EXCLUSIVE")
        try:
            for declaracao in _declaracoes_da_migracao(11):
                self._conn.execute(declaracao)
            self._conn.execute("PRAGMA user_version = 11")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _backup_pre_migracao(self) -> None:
        """Materializa o WAL no arquivo principal antes de migrar.

        ``PRAGMA wal_checkpoint(TRUNCATE)`` descarrega as páginas pendentes do
        WAL para o arquivo ``manifesto.sqlite3`` e trunca o WAL — assim o estado
        pré-migração fica "fisicamente" no arquivo principal (o checkpoint NÃO é
        uma cópia de segurança externa; a frota deve fazer o próprio .bak). Só é
        chamado quando há migração pendente (``user_version`` abaixo do schema
        conhecido): um Manifesto já no schema atual abre SEM checkpoint, então
        leitores concorrentes (ex.: o dashboard) coexistem com o CLI no dia a
        dia. Se o checkpoint reportar erro (busy de outro leitor), a migração
        não prossegue — melhor falhar do que migrar a partir de um estado
        não-materializado.
        """
        linha = self._conn.execute("PRAGMA user_version").fetchone()
        versao_atual = int(linha[0])
        if versao_atual >= MIGRACOES[-1][0]:
            return
        resultado = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # retorno: (busy, log, checkpointed) — busy ≠ 0 significa não concluído
        if not resultado or int(resultado[0]) != 0:
            raise ErroManifestoOcupado(
                "Outro processo está executando um comando sobre este Manifesto "
                f"({self.caminho}). wal_checkpoint(TRUNCATE) não concluiu antes da "
                f"migração (busy={resultado}): o Manifesto está em uso por um leitor."
            )

    def _garantir_aberto(self) -> sqlite3.Connection:
        if getattr(self, "_conn", None) is None:
            raise RuntimeError("Manifesto fechado: a instância não pode mais ser usada.")
        return self._conn

    # -- escrita -----------------------------------------------------------

    def registrar_evento(
        self, tipo: str, comando: str, detalhe: dict[str, Any] | None = None
    ) -> int:
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

    # -- varredura sob demanda (CAP-9/story varredura) — migração v8 ---------

    def varredura_adicionar(self, url: str, portal_id: int) -> bool:
        """Registra uma URL colada para varredura; True se inserida.

        ``UNIQUE(url)`` impõe "uma varredura por URL" NO BANCO (AD-8):
        re-adicionar a mesma URL retorna False e o chamador avisa, sem
        duplicata (matriz I/O: URL duplicada → INSERT ignorado + aviso).
        """
        self._garantir_aberto()
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO varreduras (url, portal_id, status, log, criado_em)
            VALUES (?, ?, 'pendente', '[]', ?)
            """,
            (url, portal_id, agora_iso_utc()),
        )
        return cursor.rowcount > 0

    def varredura_por_id(self, id: int) -> sqlite3.Row | None:
        """Linha de varredura com o contexto do portal ligado.

        O Portal sintético da execução nasce daqui (Design Notes): url/nome/
        categoria/dinamico/profundidade_maxima herdados do portal ligado, com a
        URL colada como url/seeds — o banco NÃO ganha linha nova de portal.
        """
        linhas = self.consultar(
            """
            SELECT v.*, p.url AS portal_url, p.nome AS portal_nome,
                   p.categoria AS portal_categoria,
                   p.dinamico AS portal_dinamico,
                   p.profundidade_maxima AS portal_profundidade_maxima,
                   i.sigla AS instituicao_sigla
            FROM varreduras v
            JOIN portais p ON p.id = v.portal_id
            JOIN instituicoes i ON i.id = p.instituicao_id
            WHERE v.id = ?
            """,
            (id,),
        )
        return linhas[0] if linhas else None

    def varreduras_por_status(self, status: str | None = None) -> list[sqlite3.Row]:
        """Linhas de varredura com contexto do portal, na ordem de criação.

        ``status=None`` lista todos; senão só o status pedido. Cada linha traz
        também o contexto do portal ligado (categoria/dinamico/profundidade —
        o Portal sintético da execução nasce daqui) e a sigla da instituição.
        Leitura ONLY (AD-3).
        """
        sql = """
            SELECT v.*, p.url AS portal_url, p.nome AS portal_nome,
                   p.categoria AS portal_categoria,
                   p.dinamico AS portal_dinamico,
                   p.profundidade_maxima AS portal_profundidade_maxima,
                   i.sigla AS instituicao_sigla
            FROM varreduras v
            JOIN portais p ON p.id = v.portal_id
            JOIN instituicoes i ON i.id = p.instituicao_id
        """
        if status is None:
            return self.consultar(f"{sql} ORDER BY v.id")
        return self.consultar(f"{sql} WHERE v.status = ? ORDER BY v.id", (status,))

    def marcar_varredura_iniciada(self, id: int, *, forcar: bool = False) -> bool:
        """Leva a varredura a 'rodando' (CAS, AD-3) e diz se ela foi reclamada.

        Sem ``forcar`` só reclama de ``'pendente'``: se outro processo já levou
        a linha a ``'rodando'``, retorna False e o comando pula — nada de rede
        tocada em duplicidade. Com ``forcar`` (``rodar --id``) reclama também
        ``'rodando'``/``'concluida'``/``'falhou'``: é a re-execução explícita e
        a recuperação de varreduras travadas por processo morto (o lock
        EXCLUSIVE do startup não cobre a execução, então só o ``--id``
        re-executa). ``iniciado_em`` é renovado para refletir a rodada atual e
        re-executar segue idempotente (AD-1).
        """
        condicao = "" if forcar else " AND status = 'pendente'"
        with self.transacao() as conn:
            cursor = conn.execute(
                f"UPDATE varreduras SET status = 'rodando', iniciado_em = ? "
                f"WHERE id = ?{condicao}",
                (agora_iso_utc(), id),
            )
            return cursor.rowcount > 0

    def marcar_varredura_concluida(self, id: int, resumo: dict | None = None) -> bool:
        """Registra o resumo da rodada (JSON em ``log``) e encerra em 'concluida'."""
        with self.transacao() as conn:
            cursor = conn.execute(
                """
                UPDATE varreduras
                SET status = 'concluida', log = ?, concluido_em = ?
                WHERE id = ? AND status = 'rodando'
                """,
                (
                    json.dumps(resumo or {}, ensure_ascii=False, default=str),
                    agora_iso_utc(),
                    id,
                ),
            )
            return cursor.rowcount > 0

    def marcar_varredura_falhou(self, id: int, erro: str) -> bool:
        with self.transacao() as conn:
            cursor = conn.execute(
                """
                UPDATE varreduras SET status = 'falhou', log = ?, concluido_em = ?
                WHERE id = ? AND status = 'rodando'
                """,
                (json.dumps({"erro": erro}, ensure_ascii=False), agora_iso_utc(), id),
            )
            return cursor.rowcount > 0

    # -- classificação do tipo de edital (story classificação) — migração v8 ---

    def classificacao_obter(self, url_origem: str) -> sqlite3.Row | None:
        linhas = self.consultar(
            "SELECT * FROM classificacoes WHERE url_origem = ?", (url_origem,)
        )
        return linhas[0] if linhas else None

    def classificacao_registrar(
        self,
        *,
        url_origem: str,
        tipo_edital: str,
        metodo: str,
        sinais: list[str] | None = None,
        dimensoes: list[str] | None = None,
        fonte_ancora: str | None = None,
        eixo3_classificacao: str | None = None,
        eixo3_trecho_comprobatorio: str | None = None,
    ) -> bool:
        """Grava o veredito ADVISORY de uma URL (INSERT OR REPLACE).

        A política de sobrescrita vive na camada de classificação ("já
        classificado com método textual/âncora nunca é pisado; 'sem_texto'
        pode ser preenchido depois"); aqui o banco só impõe o CHECK lógico
        tipo_edital × metodo.
        """
        if tipo_edital not in ("inovacao", "nao_inovacao", "sem_texto"):
            raise ValueError(f"tipo_edital inválido: {tipo_edital!r}")
        if metodo not in ("texto", "ancora", "sem_texto"):
            raise ValueError(f"metodo inválido: {metodo!r}")
        if eixo3_classificacao is not None and eixo3_classificacao not in (
            "IMPACTO_INSTRUMENTAL", "IMPACTO_SUBSTANTIVO", "AUSENTE_SILENCIAMENTO"
        ):
            raise ValueError(f"eixo3_classificacao inválida: {eixo3_classificacao!r}")
        with self.transacao() as conn:
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO classificacoes (
                    url_origem, tipo_edital, metodo, sinais, dimensoes,
                    fonte_ancora, classificado_em,
                    eixo3_classificacao, eixo3_trecho_comprobatorio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url_origem,
                    tipo_edital,
                    metodo,
                    json.dumps(list(sinais or []), ensure_ascii=False),
                    json.dumps(list(dimensoes or []), ensure_ascii=False),
                    fonte_ancora,
                    agora_iso_utc(),
                    eixo3_classificacao or "AUSENTE_SILENCIAMENTO",
                    eixo3_trecho_comprobatorio or "",
                ),
            )
            return cursor.rowcount > 0

    def documentos_para_classificar(
        self, portal_id: int | None = None, *, url_origem: str | None = None
    ) -> list[sqlite3.Row]:
        """Documentos com ``texto_ancora`` (da FR-6) e exclusão vigente.

        - entrada via candidatos tipo 'pdf' do portal, com ``MAX(texto_ancora)``
          agrupado por URL (mata fan-out de candidatos repetidos);
        - ``excluido_vigente`` vem da MESMA linha vigente da fila de revisão
          (resolução vence pendência; id maior entre resolvidas — padrão da
          story 4): a classificação NUNCA vê documento que a curadoria já
          excluiu ('não publicar') — e o veredito segue ADVISORY, nunca exclui;
        - ``portal_id=None, url_origem=...`` resolve um documento individual
          (comando ``classificar --documento URL``).
        """
        from_sub = """
                SELECT url, MAX(texto_ancora) AS texto_ancora,
                       MIN(portal_id) AS portal_id
                FROM candidatos
                WHERE tipo = 'pdf'
        """
        params: list[object] = []
        if portal_id is not None:
            from_sub += " AND portal_id = ?"
            params.append(portal_id)
        from_sub += "\n                GROUP BY url"

        sql = f"""
            SELECT d.*, c.texto_ancora AS texto_ancora,
                   COALESCE(f.decidido_exclusao, 0) AS excluido_vigente,
                   ins.sigla AS instituicao_sigla, po.nome AS portal_nome
            FROM documentos d
            JOIN ({from_sub}) c ON c.url = d.url_origem
            LEFT JOIN ({_FILA_VIGENTE}) fv ON fv.url_origem = d.url_origem
            LEFT JOIN fila_revisao f ON f.id = fv.id_fila_vigente
            LEFT JOIN portais po ON po.id = c.portal_id
            LEFT JOIN instituicoes ins ON ins.id = po.instituicao_id
        """
        if url_origem is not None:
            sql += " WHERE d.url_origem = ?"
            params.append(url_origem)
        sql += " ORDER BY d.rowid"
        return self.consultar(sql, tuple(params))

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
        self,
        portal_id: int,
        url: str,
        tipo: str,
        *,
        texto_ancora: str | None = None,
        varredura_id: int | None = None,
    ) -> bool:
        """Registra candidato a edital dedupe por UNIQUE(portal_id,url); True se novo.

        Segunda execução sobre o mesmo achado NÃO duplica linha (AD-1/AD-8):
        o banco, não memória de processo, é a fonte do "já visto" — o retorno
        continua significando "inserido AGORA". ``texto_ancora`` preserva a
        âncora integral (TEXT livre) desde a descoberta (FR-6); numa
        RE-DESCOBERTA o upsert preenche a âncora só quando ela ainda é NULL
        — corpus antigo sem âncora é retroalimentado e uma âncora já gravada
        NUNCA é sobrescrita. ``varredura_id`` (v10) é gravado APENAS no INSERT
        de um candidato NOVO descoberto por ``varredura rodar``: um candidato
        já existente (do ``descobrir``/``coletar``) não é reatribuído, pois a
        varredura não foi quem o injetou no pipeline.
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
            INSERT INTO candidatos (portal_id, url, tipo, texto_ancora, varredura_id, descoberto_em)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(portal_id, url) DO UPDATE
            SET texto_ancora = COALESCE(texto_ancora, excluded.texto_ancora)
            """,
            (portal_id, url, tipo, texto_ancora, varredura_id, agora_iso()),
        )
        return existente is None and cursor.rowcount > 0

    def candidatos_de_varredura(self, varredura_id: int) -> list[sqlite3.Row]:
        """Candidatos descobertos por uma varredura (v10), na ordem de descoberta.

        Leitura ONLY (AD-3). Alimenta a exibição da aba de varredura e o
        recorte ``--varredura`` dos estágios do pipeline (coletar/textuar/
        datar/classificar/eixo3).
        """
        return self.consultar(
            """
            SELECT c.id, c.url, c.tipo, c.texto_ancora, c.descoberto_em
            FROM candidatos c
            WHERE c.varredura_id = ?
            ORDER BY c.id
            """,
            (varredura_id,),
        )

    def contar_candidatos(self, portal_id: int | None = None) -> int:
        if portal_id is None:
            return int(self.consultar("SELECT COUNT(*) FROM candidatos")[0][0])
        return int(
            self.consultar("SELECT COUNT(*) FROM candidatos WHERE portal_id = ?", (portal_id,))[0][
                0
            ]
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
            cursor = conn.execute("DELETE FROM secoes_visitadas WHERE portal_id = ?", (portal_id,))
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

    def registrar_texto_ocr_proveniencia(
        self,
        documento_id: str,
        url_origem: str,
        *,
        metodo: str,
        confianca_media: float | None,
        paginas_resgatadas: int,
        tentativas: int,
    ) -> bool:
        """UPDATE das colunas de proveniência do resgate por OCR (v11).

        Chamada DEPOIS de ``registrar_texto_extraido`` (v4 preenche o ``.txt``
        e zera a ``flag_escaneado``): as colunas ``ocr_*`` carimbam o método,
        a confiança média das páginas aceitas e a retomada (``ocr_em`` virou a
        trava do ciclo). Retorna True só se a linha casou — 0 linhas NÃO é
        sucesso silencioso.
        """
        with self.transacao() as conn:
            cursor = conn.execute(
                """
                UPDATE documentos
                SET ocr_em = ?, ocr_metodo = ?, ocr_confianca_media = ?,
                    ocr_paginas_resgatadas = ?, ocr_tentativas = ?
                WHERE id = ? AND url_origem = ?
                """,
                (
                    agora_iso_utc(),
                    metodo,
                    confianca_media,
                    paginas_resgatadas,
                    tentativas,
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
            duplicado_pendente = "idx_fila_revisao_pendente_por_url" in mensagem or (
                "UNIQUE constraint failed" in mensagem and "fila_revisao.url_origem" in mensagem
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

    def consultar_fila(
        self, status: str | None = "pendente", *, limite: int | None = None
    ) -> list[sqlite3.Row]:
        """Itens da fila na ordem de criação; ``None`` lista todos os status.

        ``limite`` restringe o número de linhas retornadas (paginacao).
        Retorna sigla da instituição via JOIN com portais/instituições.
        """
        sql_base = """
            SELECT f.*, p.categoria AS portal_categoria,
                   i.sigla AS instituicao_sigla
            FROM fila_revisao f
            JOIN portais p ON p.id = f.portal_id
            JOIN instituicoes i ON i.id = p.instituicao_id
        """
        if status is None:
            sql = f"{sql_base} ORDER BY f.id"
        else:
            sql = f"{sql_base} WHERE f.status = ? ORDER BY f.id"
            params: tuple[Any, ...] = (status,)
            if limite is not None:
                sql += f" LIMIT {int(limite)}"
                return self.consultar(sql, params)
            return self.consultar(sql, params)
        if limite is not None:
            sql += f" LIMIT {int(limite)}"
        return self.consultar(sql)

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

    def contar_texto_estagio(self) -> dict[str, int]:
        """Contagem de documentos por estágio de extração de texto (métrica do status).

        Devolve ``extraidos`` (flag_escaneado=0 E texto extraído),
        ``escaneados`` (flag_escaneado=1) e ``pendentes`` (flag_escaneado
        IS NULL — nunca processados pelo ``textuar``).
        """
        linhas = self.consultar(
            """
            SELECT
                CASE
                    WHEN flag_escaneado = 0 AND extraido_em IS NOT NULL THEN 'extraidos'
                    WHEN flag_escaneado = 1 THEN 'escaneados'
                    ELSE 'pendentes'
                END AS estagio,
                COUNT(*) AS qtd
            FROM documentos
            GROUP BY estagio
            """
        )
        resultado = {"extraidos": 0, "escaneados": 0, "pendentes": 0}
        for linha in linhas:
            resultado[str(linha["estagio"])] = int(linha["qtd"])
        return resultado

    def contar_fila(self, status: str | None = None) -> int:
        if status is None:
            return int(self.consultar("SELECT COUNT(*) FROM fila_revisao")[0][0])
        return int(
            self.consultar("SELECT COUNT(*) FROM fila_revisao WHERE status = ?", (status,))[0][0]
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

    # -- análise L2 (CAP-7, migração v6) --------------------------------------
    # ``analise`` grava SOMENTE aqui (AD-11): lote e catálogo são as únicas
    # tabelas novas do estágio; editais/documentos seguem intocados.

    def documentos_do_portal_para_analise(self, portal_id: int) -> list[sqlite3.Row]:
        """Documentos do portal COM a exclusão VIGENTE resolvida por documento.

        Mesma membria de ``documentos_do_portal`` (candidatos tipo 'pdf',
        DISTINCT anti-fan-out); a coluna extra ``excluido_vigente`` carrega a
        decisão vigente da fila por URL (1 = excluído). Ordenação
        ``edital_id, data_captura, rowid`` — precedência cronológica de
        captura DENTRO do edital (FR-17: captura posterior prevalece na
        consolidação). Leitura pura: nenhum INSERT/UPDATE.
        """
        return self.consultar(
            f"""
            SELECT d.*, COALESCE(f.decidido_exclusao, 0) AS excluido_vigente
            FROM documentos d
            JOIN (
                SELECT DISTINCT url FROM candidatos
                WHERE portal_id = ? AND tipo = 'pdf'
            ) c ON c.url = d.url_origem
            LEFT JOIN ({_FILA_VIGENTE}) fv ON fv.url_origem = d.url_origem
            LEFT JOIN fila_revisao f ON f.id = fv.id_fila_vigente
            ORDER BY d.edital_id, d.data_captura, d.rowid
            """,
            (portal_id,),
        )

    _COLUNAS_LOTE = (
        "modelo",
        "versao_do_modelo",
        "prompt_versao",
        "prompt_sha256",
        "temperatura",
        "seed",
        "codebook_sha256",
        "versao_agente",
        "schema_version",
    )

    def obter_ou_abrir_lote(self, assinatura: dict[str, Any]) -> tuple[sqlite3.Row, bool]:
        """Lote da assinatura EXATA do instrumento; cria só quando não existe.

        A comparação usa ``IS`` coluna a coluna (seed NULL casa com NULL).
        Preferência por lotes ABERTOS (retomada FR-18); um lote CONCLUÍDO com
        a mesma assinatura é REUSADO DE FORMA HONESTA: é REABERTO aqui
        (``status='aberto'``, ``concluido_em=NULL``) e só volta a 'concluido'
        no próximo ``fechar_lote`` — a delimitação nunca mente sobre
        conclusão enquanto novas linhas entram. Devolve ``(linha_do_lote,
        criado_agora)``.
        """
        colunas = self._COLUNAS_LOTE
        ausentes = [c for c in colunas if c not in assinatura]
        if ausentes:
            raise ValueError(f"assinatura incompleta: faltam {', '.join(ausentes)}")
        desconhecidas = [k for k in assinatura if k not in colunas]
        if desconhecidas:
            raise ValueError(f"assinatura com chaves desconhecidas: {', '.join(desconhecidas)}")
        valores = tuple(assinatura[c] for c in colunas)
        where = " AND ".join(f"{c} IS ?" for c in colunas)
        selecao = (
            f"SELECT * FROM lotes_l2 WHERE {where} "
            "ORDER BY CASE WHEN status = 'aberto' THEN 0 ELSE 1 END, id DESC LIMIT 1"
        )
        with self.transacao() as conn:
            linha = conn.execute(selecao, valores).fetchone()
            if linha is None:
                colunas_sql = ", ".join([*colunas, "aberto_em"])
                placeholders = ", ".join("?" for _ in range(len(colunas) + 1))
                conn.execute(
                    f"INSERT INTO lotes_l2 ({colunas_sql}) VALUES ({placeholders})",
                    (*valores, agora_iso_utc()),
                )
                criado = conn.execute(selecao, valores).fetchone()
                assert criado is not None  # acabou de ser inserido nesta transação
                return criado, True
            if str(linha["status"]) == "concluido":
                conn.execute(
                    """
                    UPDATE lotes_l2 SET status = 'aberto', concluido_em = NULL
                    WHERE id = ?
                    """,
                    (int(linha["id"]),),
                )
                linha = conn.execute(selecao, valores).fetchone()
                assert linha is not None  # mesma transação, mesma linha
            return linha, False

    def registrar_campos_l2(
        self, lote_id: int, edital_id: str, campos: list[dict[str, Any]]
    ) -> int:
        """Grava TODOS os campos do edital numa ÚNICA transação (AD-3).

        ``campos``: dicts com campo, valor (TEXT), documento_id, url_origem,
        citacao_trecho, citacao_pagina e verificacao (∈ ok|citacao_invalidada
        — CHECK do banco é a última defesa). INSERT OR REPLACE pela PK
        (edital_id, campo, lote_id): reprocessar o edital no MESMO lote
        substitui as próprias linhas, nunca duplica. Devolve quantas linhas
        foram gravadas.
        """
        for registro in campos:
            verificacao = registro.get("verificacao")
            if verificacao not in ("ok", "citacao_invalidada"):
                raise ValueError(
                    f"verificacao inválida para {registro.get('campo')!r}: "
                    f"{verificacao!r} (esperado ok|citacao_invalidada)"
                )
            for obrigatoria in ("campo", "valor"):
                if not registro.get(obrigatoria):
                    raise ValueError(f"campo '{obrigatoria}' é obrigatório em cada registro")
        with self.transacao() as conn:
            for registro in campos:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO catalogo_l2 (
                        edital_id, campo, lote_id, valor, documento_id,
                        url_origem, citacao_trecho, citacao_pagina,
                        verificacao, gravado_em
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        edital_id,
                        str(registro["campo"]),
                        lote_id,
                        str(registro["valor"]),
                        registro.get("documento_id"),
                        registro.get("url_origem"),
                        registro.get("citacao_trecho"),
                        registro.get("citacao_pagina"),
                        registro["verificacao"],
                        agora_iso_utc(),
                    ),
                )
            return len(campos)

    def fechar_lote(self, lote_id: int) -> bool:
        """Marca o lote como concluído; False se já estava (nunca reabre)."""
        with self.transacao() as conn:
            cursor = conn.execute(
                """
                UPDATE lotes_l2
                SET status = 'concluido', concluido_em = ?
                WHERE id = ? AND status = 'aberto'
                """,
                (agora_iso_utc(), lote_id),
            )
            return cursor.rowcount > 0

    def editais_codificados_no_lote(self, lote_id: int) -> set[str]:
        """Editais que já têm catálogo gravado NESTE lote (pulo da retomada)."""
        return {
            str(linha["edital_id"])
            for linha in self.consultar(
                "SELECT DISTINCT edital_id FROM catalogo_l2 WHERE lote_id = ?",
                (lote_id,),
            )
        }

    def contar_catalogo_l2(self, lote_id: int | None = None) -> int:
        """Campos VÁLIDOS do catálogo L2 (verificacao='ok'), total ou de UM lote.

        Métrica do ``status``: linhas com ``citacao_invalidada`` ficam FORA da
        contagem — custódia preservada na tabela, mas fora do catálogo válido.
        """
        if lote_id is None:
            return int(
                self.consultar("SELECT COUNT(*) FROM catalogo_l2 WHERE verificacao = 'ok'")[0][0]
            )
        return int(
            self.consultar(
                "SELECT COUNT(*) FROM catalogo_l2 WHERE lote_id = ? AND verificacao = 'ok'",
                (lote_id,),
            )[0][0]
        )

    # -- consulta (CAP-9/UJ-3/§10): leituras ONLY (AD-3/AD-4: fonte única) ----

    def listar_l1(
        self,
        *,
        instituicao: str | None = None,
        ano: int | None = None,
        categoria: str | None = None,
        edital: str | None = None,
        limite: int | None = None,
    ) -> list[sqlite3.Row]:
        """Documentos NÃO excluídos com ano EFETIVO e sua origem (FR-20).

        Unidade = DOCUMENTO (``edital_id`` é coluna; agregar por Edital fica
        para decisão futura). ``ano`` = COALESCE(ano_aceito, decidido_ano) e
        ``ano_fonte`` ∈ {automatica, fila_humana, vazio} — pendentes na fila
        aparecem com ano vazio (e POR ÚLTIMO na ordenação); excluídos ficam
        FORA da listagem (conte-os com ``contar_l1_excluidos``, mesma
        definição de linhas). A fila entra pela linha VIGENTE por URL (uma
        única decisão: resolvida vence pendente, maior id entre resolvidas)
        — nunca há duplicação nem divergência com a custódia. ``edital``
        filtra por subtrecho do id (case-insensitive LIKE). ``limite``
        restringe o número de linhas retornadas (paginacao). Leitura pura:
        nenhum parseio de PDF, nenhum acesso ao corpus (AD-4).
        """
        clausulas, parametros = _filtros_l1(
            instituicao, ano, categoria, excluidos=False, edital=edital
        )
        sql = f"SELECT {_L1_COLUNAS} {_L1_JOINS} WHERE {clausulas} {_L1_ORDEM}"
        if limite is not None:
            sql += f" LIMIT {int(limite)}"
        return self.consultar(sql, parametros)

    def contar_l1_excluidos(
        self,
        *,
        instituicao: str | None = None,
        ano: int | None = None,
        categoria: str | None = None,
        edital: str | None = None,
    ) -> int:
        """Excluídos sob os filtros informados, IGNORANDO o filtro de ano.

        Mesma definição de linhas de ``listar_l1`` com o predicado invertido.
        O ``--ano`` NÃO se aplica aqui (patch 4): a população excluída não
        participa da janela de listagem (decisão de exclusão não tem ano) —
        com ``--ano 2024`` o resumo continua mostrando quantos foram
        excluídos na instituição/categoria pedida, em vez de um "excluídos:
        0" enganoso.
        """
        clausulas, parametros = _filtros_l1(
            instituicao, ano, categoria, excluidos=True, ignorar_ano=True, edital=edital
        )
        return int(
            self.consultar(f"SELECT COUNT(*) {_L1_JOINS} WHERE {clausulas}", parametros)[0][0]
        )

    def custodia_do_edital(self, edital_id: str) -> dict[str, Any] | None:
        """Cadeia de custódia §10 do Edital: captura → datação → fila.

        Montado INTEGRAMENTE a partir do Manifesto (AD-3/AD-4): nenhum PDF
        nem arquivo do corpus é lido. Devolve ``None`` quando o edital não
        existe (edital existente SEM documentos devolve ``documentos: []``).
        O bloco ``fila`` usa a MESMA linha vigente por URL da listagem
        (resolvida vence pendente; maior id entre resolvidas) — listar e
        custódia nunca divergem.
        """
        linhas_edital = self.consultar(
            """
            SELECT e.id AS edital_id, e.criado_em, i.sigla AS instituicao
            FROM editais e
            JOIN instituicoes i ON i.id = e.instituicao_id
            WHERE e.id = ?
            """,
            (edital_id,),
        )
        if not linhas_edital:
            return None
        dados: dict[str, Any] = dict(linhas_edital[0])
        # Metadados autodescritivos (§10 "quem capturou"): quando foi gerado,
        # com qual versão do agente e sobre qual schema do Manifesto.
        dados["gerado_em"] = agora_iso_utc()
        dados["versao_agente"] = __version__
        dados["schema_version"] = self.schema_version()
        documentos = self.consultar(
            "SELECT * FROM documentos WHERE edital_id = ? ORDER BY data_captura, id, url_origem",
            (edital_id,),
        )
        documentos_saida: list[dict[str, Any]] = []
        for documento in documentos:
            url = documento["url_origem"]
            evidencias = [
                {
                    "fonte": evidencia["fonte"],
                    "valor_bruto": evidencia["valor_bruto"],
                    "localizacao": evidencia["localizacao"],
                }
                for evidencia in self.evidencias_da_url(url)
            ]
            itens_fila = self.consultar(
                f"""
                SELECT f.* FROM fila_revisao f
                JOIN ({_FILA_VIGENTE}) fv ON fv.id_fila_vigente = f.id
                WHERE f.url_origem = ?
                """,
                (url,),
            )
            fila: dict[str, Any] | None = None
            if itens_fila:
                # a subquery vigente devolve NO MÁXIMO uma linha por URL
                item = itens_fila[0]
                fila = {
                    "status": item["status"],
                    "motivo": item["motivo"],
                    "decidido_ano": item["decidido_ano"],
                    "decidido_exclusao": bool(item["decidido_exclusao"]),
                    "justificativa": item["justificativa"],
                    "autor": item["autor"],
                    "decidido_em": item["decidido_em"],
                }
            documentos_saida.append(
                {
                    "documento_id": documento["id"],
                    "captura": {
                        "data_captura": documento["data_captura"],
                        "hash_sha256": documento["hash_sha256"],
                        "url_origem": url,
                        "versao_crawler": documento["versao_crawler"],
                        "caminho": documento["caminho"],
                        "predecessor_id": documento["predecessor_id"],
                    },
                    "datacao": {
                        "metodo_datacao": documento["metodo_datacao"],
                        "ano_aceito": documento["ano_aceito"],
                        "evidencias": evidencias,
                    },
                    "fila": fila,
                }
            )
        dados["documentos"] = documentos_saida
        return dados

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
