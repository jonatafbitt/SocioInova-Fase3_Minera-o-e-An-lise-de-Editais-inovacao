"""Dashboard interativo — corpus de editais da Rede Federal EPCT.

Uso:  uv run streamlit run dashboard.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
import umap
import yaml
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from wordcloud import WordCloud

from agente_editais.texto_analise import (
    STOPWORDS_PT,
    info_versao,
    limpar_texto,
)
from agente_editais.texto_analise import (
    TOKEN_PATTERN as _TOKEN_PATTERN,
)

DB_PATH = Path("dados/manifesto.sqlite3")
CORPUS_TXT = Path("corpus")

# Token exigindo início por LETRA: exclui números puros ("2025", "2024")
# da nuvem, dos top-termos e do TF-IDF.
TOKEN_PATTERN = _TOKEN_PATTERN

st.set_page_config(
    page_title="Editais Rede Federal EPCT",
    page_icon=" ",
    layout="wide",
)


# ── loaders ──────────────────────────────────────────────────────────────────


@st.cache_data
def carregar_dados() -> pd.DataFrame:
    """Todos os documentos com texto e ano EFETIVO, incluindo excluídos.

    Mesma semântica de linhas do ``listar_l1`` do Manifesto:
    ``ano = COALESCE(ano_aceito, decidido_ano)``; fila entra pela linha
    vigente por URL (resolvida vence pendente); excluídos entram marcados
    na coluna ``excluido``, não como linhas sumidas.
    """
    if not DB_PATH.exists():
        raise RuntimeError(
            "Manifesto ausente — rode uma vez `uv run agente-editais status` "
            "(ou `mapa validar`) para criar/migrar dados/manifesto.sqlite3."
        )
    try:
        conn = sqlite3.connect(str(DB_PATH))
        df = pd.read_sql_query(
            """
            SELECT
                d.id                             AS doc_id,
                d.edital_id                      AS edital_id,
                d.texto_caminho                  AS txt_path,
                d.texto_chars                    AS chars,
                COALESCE(d.ano_aceito, f.decidido_ano) AS ano,
                CASE
                    WHEN d.ano_aceito IS NOT NULL THEN 'automatica'
                    WHEN f.decidido_ano IS NOT NULL THEN 'fila_humana'
                    ELSE 'vazio'
                END                              AS ano_fonte,
                COALESCE(f.decidido_exclusao, 0) AS excluido,
                COALESCE(d.flag_escaneado, 0)    AS escaneado,
                i.sigla                          AS instituicao,
                p.nome                           AS portal,
                p.categoria                      AS categoria,
                cl.tipo_edital                   AS tipo_edital,
                cl.eixo3_classificacao           AS eixo3_classificacao,
                cl.eixo3_trecho_comprobatorio    AS eixo3_trecho_comprobatorio
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
            LEFT JOIN (
                SELECT url_origem,
                       CASE WHEN MAX(CASE WHEN status = 'resolvida' THEN id END)
                                 IS NOT NULL
                            THEN MAX(CASE WHEN status = 'resolvida' THEN id END)
                            ELSE MAX(id)
                       END AS id_fila_vigente
                FROM fila_revisao
                GROUP BY url_origem
            ) fv ON fv.url_origem = d.url_origem
            LEFT JOIN fila_revisao f ON f.id = fv.id_fila_vigente
            LEFT JOIN classificacoes cl ON cl.url_origem = d.url_origem
            WHERE d.texto_caminho IS NOT NULL
            """,
            conn,
        )
        conn.close()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "Manifesto vazio ou com schema desatualizado (sem a tabela de "
            "classificação). Rode uma vez `uv run agente-editais status` (ou "
            f"`mapa validar`) para migrar o banco. Detalhe: {exc}"
        ) from exc
    df["ano"] = df["ano"].fillna(0).astype(int)
    df["excluido"] = df["excluido"].astype(int)
    df["escaneado"] = df["escaneado"].astype(int)
    df["tipo_edital"] = df["tipo_edital"].fillna("não classificado")
    return df


def _roda_cli(*args: str) -> tuple[int, str]:
    """Executa um comando do CLI em subprocess (AD-5: sem rede no dashboard).

    Usa ``python -m agente_editais`` para não depender do script instalado;
    herda o ambiente (configs/manifesto/corpus) e força codificação UTF-8.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agente_editais", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            cwd=str(Path.cwd()),
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return 1, "Tempo limite excedido para o comando do CLI."
    saida = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, saida


def _varreduras() -> pd.DataFrame:
    """Tabela de status das varreduras — leitura DIRETA, sem o lock EXCLUSIVE
    do startup do Manifesto (AD-3/AD-4): o dashboard nunca compete com o CLI
    pela escrita. Mesma query de ``varreduras_por_status``."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        df = pd.read_sql_query(
            """
            SELECT v.id AS ID, v.url AS URL,
                   i.sigla AS "Instituição", p.nome AS Portal,
                   v.status AS Status,
                   v.criado_em AS "Criado em", v.concluido_em AS "Terminado em"
            FROM varreduras v
            JOIN portais p ON p.id = v.portal_id
            JOIN instituicoes i ON i.id = p.instituicao_id
            ORDER BY v.id
            """,
            conn,
        )
        conn.close()
    except sqlite3.Error as exc:
        st.warning(f"Aviso: não foi possível ler o histórico de varreduras. {exc}")
        return pd.DataFrame()
    return df[["ID", "URL", "Instituição", "Portal", "Status", "Criado em", "Terminado em"]]


def _candidatos_de_varredura(varredura_id: int) -> pd.DataFrame:
    """Status do pipeline POR candidato descoberto pela varredura (v10).

    Leitura DIRETA (mesmo padrão de ``_varreduras`` — AD-3/AD-4): CASEs
    refletem a presença de evidência em cada estágio (baixado → textuado →
    datado → classificado → eixo 3). ``AUSENTE_SILENCIAMENTO`` conta como
    "silêncio" no Eixo 3, não como classificação pendente.
    """
    if not DB_PATH.exists():
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        df = pd.read_sql_query(
            """
            SELECT
                c.url                  AS URL,
                c.tipo                 AS Tipo,
                c.descoberto_em        AS "Descoberto em",
                CASE WHEN d.id IS NOT NULL THEN '✔' ELSE '–' END AS Baixado,
                CASE WHEN d.texto_caminho IS NOT NULL THEN '✔' ELSE '–' END AS Textuado,
                CASE WHEN d.ano_aceito IS NOT NULL THEN '✔' ELSE '–' END AS Datado,
                CASE WHEN cl.tipo_edital IS NOT NULL THEN '✔' ELSE '–' END AS "Classificado",
                CASE
                    WHEN cl.eixo3_classificacao IS NULL THEN '–'
                    WHEN cl.eixo3_classificacao = 'AUSENTE_SILENCIAMENTO' THEN 'silêncio'
                    ELSE cl.eixo3_classificacao
                END AS "Eixo 3"
            FROM candidatos c
            LEFT JOIN documentos d ON d.url_origem = c.url
            LEFT JOIN classificacoes cl ON cl.url_origem = c.url
            WHERE c.varredura_id = ?
            ORDER BY c.id
            """,
            conn,
            params=(varredura_id,),
        )
        conn.close()
    except sqlite3.Error as exc:
        st.warning(f"Aviso: não foi possível ler os candidatos da varredura. {exc}")
        return pd.DataFrame()
    return df


def _log_varredura(varredura_id: int) -> list:
    """Log JSON da varredura (leitura direta)."""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        bruto = conn.execute(
            "SELECT log FROM varreduras WHERE id = ?", (varredura_id,)
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        st.warning(f"Aviso: não foi possível ler o log da varredura. {exc}")
        return []
    if bruto is None:
        return []
    try:
        return json.loads(bruto[0])
    except json.JSONDecodeError:
        return []


def _entradas_de_varredura(log) -> list[tuple[str, str]]:
    """Normaliza o log da varredura para exibição (resumo ou lista de eventos).

    ``marcar_varredura_concluida`` grava o log como RESUMO em dict (contadores
    do ``totalizar()``); a exibição antiga iterava o dict e caía em chaves
    str — normaliza ambos: dict → (chave, valor); lista → (tipo, item).
    """
    if isinstance(log, dict):
        return [(chave, str(valor)) for chave, valor in log.items()]
    if isinstance(log, str):
        return [("resumo", log)]
    entradas: list[tuple[str, str]] = []
    for entrada in log:
        if isinstance(entrada, dict):
            entradas.append(
                (
                    str(entrada.get("tipo", "evento")),
                    json.dumps(entrada, ensure_ascii=False, default=str),
                )
            )
        else:
            entradas.append(("evento", str(entrada)))
    return entradas


@st.cache_data(show_spinner=False)
def _carregar_todos_textos() -> dict[str, str]:
    """Lê todos os .txt do corpus uma vez; chave = doc_id (string hex 12)."""
    texts: dict[str, str] = {}
    for txt_file in CORPUS_TXT.rglob("*.txt"):
        try:
            doc_id = txt_file.stem.split("-")[0]
            if len(doc_id) != 12 or any(ch not in "0123456789abcdef" for ch in doc_id):
                continue
            texts[doc_id] = txt_file.read_text(
                encoding="utf-8", errors="replace"
            )[:8000]
        except Exception:
            continue
    return texts


ALL_TEXTS: dict[str, str] = _carregar_todos_textos()


def carregar_textos(_ids: tuple[str, ...]) -> dict[str, str]:
    return {did: ALL_TEXTS.get(did, "") for did in _ids}


def _contagem_termos(
    id_textos: dict[str, str],
    ngram: tuple[int, int],
    min_freq: int = 2,
) -> pd.DataFrame:
    """Frequência de termos (palavras ou n-gramas) com stopwords PT e limpeza de conectivos multiword."""
    docs_limpos = [limpar_texto(t) for t in id_textos.values() if t.strip()]
    if not docs_limpos:
        return pd.DataFrame(columns=["termo", "freq"])
    vec = CountVectorizer(
        ngram_range=ngram,
        stop_words=list(STOPWORDS_PT),
        token_pattern=TOKEN_PATTERN,
        lowercase=True,
    )
    X = vec.fit_transform(docs_limpos)
    freq = X.sum(axis=0).A1
    termos = vec.get_feature_names_out()
    df = pd.DataFrame({"termo": termos, "freq": freq})
    df = df[~df["termo"].str.contains(r"\d", regex=True)]
    return (
        df[df["freq"] >= min_freq]
        .sort_values("freq", ascending=False)
        .reset_index(drop=True)
    )


# ── Análise Textual helpers ─────────────────────────────────────────────────────


@st.cache_data
def _carregar_sinais_config() -> dict:
    """Carrega configuração de sinais analíticos do YAML."""
    caminho = Path("configs/sinais_analiticos.yaml")
    if caminho.exists():
        with open(caminho, encoding="utf-8") as f:
            return yaml.safe_load(f)
    st.warning(
        "Aviso: 'configs/sinais_analiticos.yaml' ausente — quadro de sinais "
        "rodando com regras padrão (e a abas de sinais do 'agente-editais' "
        "não refletem a configuração corrente)."
    )
    return {"dimensoes": [], "mineracao": {"janela_contexto": 50, "normalizar": True, "incluir_bigramas": True}}


def _buscar_kwic(
    id_textos: dict[str, str],
    termo: str,
    contexto: int = 50,
    limite: int = 100,
) -> pd.DataFrame:
    """Busca KWIC (Key Word In Context) — concordância do termo no corpus."""
    termo_norm = termo.lower().strip()
    if not termo_norm:
        return pd.DataFrame(columns=["doc_id", "posicao", "trecho"])
    resultados = []
    for doc_id, texto in id_textos.items():
        if not texto.strip():
            continue
        texto_norm = " ".join(texto.lower().split())
        idx = 0
        while True:
            pos = texto_norm.find(termo_norm, idx)
            if pos == -1:
                break
            inicio = max(0, pos - contexto)
            fim = min(len(texto_norm), pos + len(termo_norm) + contexto)
            trecho = texto_norm[inicio:fim]
            # Marcar o termo no trecho com ** para negrito no markdown
            trecho_marcado = (
                trecho[: pos - inicio]
                + f"**{texto_norm[pos : pos + len(termo_norm)]}**"
                + trecho[pos + len(termo_norm) - inicio :]
            )
            resultados.append({"doc_id": doc_id, "posicao": pos, "trecho": trecho_marcado})
            idx = pos + 1
            if len(resultados) >= limite:
                break
        if len(resultados) >= limite:
            break
    return pd.DataFrame(resultados)


def _coocorrencias_janela(
    id_textos: dict[str, str],
    top_n: int = 30,
    janela: int = 10,
    min_freq: int = 2,
) -> pd.DataFrame:
    """Matriz de co-ocorrência por janela deslizante de palavras."""

    # Primeiro, obter top-N termos por frequência
    df_freq = _contagem_termos(id_textos, ngram=(1, 1), min_freq=min_freq)
    if df_freq.empty:
        return pd.DataFrame()
    top_termos = set(df_freq.head(top_n)["termo"].tolist())

    # Contar co-ocorrências dentro da janela
    cooc_counts: dict[tuple[str, str], int] = {}
    for texto in id_textos.values():
        if not texto.strip():
            continue
        tokens = [t for t in texto.lower().split() if t in top_termos]
        for i, t1 in enumerate(tokens):
            for j in range(i + 1, min(i + janela + 1, len(tokens))):
                t2 = tokens[j]
                if t1 != t2:
                    par = tuple(sorted((t1, t2)))
                    cooc_counts[par] = cooc_counts.get(par, 0) + 1

    if not cooc_counts:
        return pd.DataFrame()

    # Converter para matriz
    termos_lista = sorted(top_termos)
    matriz = pd.DataFrame(0, index=termos_lista, columns=termos_lista, dtype=int)
    for (t1, t2), count in cooc_counts.items():
        if count >= min_freq:
            matriz.loc[t1, t2] = count
            matriz.loc[t2, t1] = count
    return matriz


def _normalizar_para_mineracao(texto_ou_sinal: str) -> str:
    """Case-insensitive + acentos NFD descartados + espaços colapsados.

    Espelha a normalização do motor de classificação ('_normalizar' em
    classificacao.py) para o quadro de sinais contar o MESMO que o
    'classificar': token-inteiro (ex.: 'parque' não dispara 'parquet').
    """
    sem_acentos = "".join(
        ch
        for ch in unicodedata.normalize("NFD", texto_ou_sinal.lower())
        if not unicodedata.combining(ch)
    )
    return " ".join(sem_acentos.split())


def _minerar_sinais_quadro16(
    id_textos: dict[str, str],
    config: dict,
) -> pd.DataFrame:
    """Mineração dos sinais das 5 dimensões do Quadro 1.6 no corpus."""
    dimensoes = config.get("dimensoes", [])
    mineracao_cfg = config.get("mineracao", {})
    normalizar = mineracao_cfg.get("normalizar", True)
    incluir_bigramas = mineracao_cfg.get("incluir_bigramas", True)

    resultados = []
    for dimensao in dimensoes:
        dim_id = dimensao.get("id", "")
        dim_nome = dimensao.get("nome", "")
        sinais = dimensao.get("sinais", [])

        for doc_id, texto in id_textos.items():
            if not texto.strip():
                continue
            texto_proc = (
                _normalizar_para_mineracao(texto)
                if normalizar
                else " ".join(texto.lower().split())
            )
            sinais_encontrados = []
            for sinal in sinais:
                sinal_proc = (
                    _normalizar_para_mineracao(sinal)
                    if normalizar
                    else sinal.lower()
                )
                palavras = sinal_proc.split()
                if not palavras:
                    continue
                expressao = r"\s+".join(re.escape(p) for p in palavras)
                if re.search(rf"\b{expressao}\b", texto_proc):
                    sinais_encontrados.append(sinal)
                elif incluir_bigramas and len(palavras) > 1:
                    for i in range(len(palavras) - 1):
                        if re.search(
                            rf"\b{re.escape(palavras[i])}\s+"
                            rf"{re.escape(palavras[i + 1])}\b",
                            texto_proc,
                        ):
                            sinais_encontrados.append(sinal)
                            break
            resultados.append(
                {
                    "dimensao_id": dim_id,
                    "dimensao_nome": dim_nome,
                    "doc_id": doc_id,
                    "sinais_encontrados": len(sinais_encontrados),
                    "total_sinais": len(sinais),
                    "sinais_lista": ", ".join(sinais_encontrados),
                }
            )
    return pd.DataFrame(resultados)


# ── Cobertura do corpus (Fase 2.4) ─────────────────────────────────────────────


@st.cache_data
def _catalogo_l2() -> dict:
    """Resumo do catálogo analítico L2 (CAP-7) — tolerante a lote vazio."""
    if not DB_PATH.exists():
        return {"editais_l2": 0, "lotes_l2": 0, "verificados_ok": 0}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            editais_l2 = conn.execute(
                "SELECT COUNT(DISTINCT edital_id) FROM catalogo_l2"
            ).fetchone()[0]
            lotes = conn.execute("SELECT COUNT(*) FROM lotes_l2").fetchone()[0]
            verificados = conn.execute(
                "SELECT COUNT(*) FROM catalogo_l2 WHERE verificacao = 'ok'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            return {"editais_l2": 0, "lotes_l2": 0, "verificados_ok": 0}
        conn.close()
        return {"editais_l2": int(editais_l2), "lotes_l2": int(lotes), "verificados_ok": int(verificados)}
    except sqlite3.Error:
        return {"editais_l2": 0, "lotes_l2": 0, "verificados_ok": 0}


def _cobertura_corpus() -> pd.DataFrame:
    """Cobertura do corpus por (instituição, ano) — leitura DIRETA do Manifesto.

    Conta TODOS os documentos (não só os com texto, ao contrário de
    ``carregar_dados``) e a taxa de avanço de cada estágio do pipeline:
    baixado → textuado → com texto útil → datado → classificado L1 → Eixo 3,
    com a contagem de resgatados por OCR (Fase 3.1, colunas ``ocr_*``).
    Ano efetivo = ano_aceito → decidido_ano → ano_provisorio (idem L1).
    """
    if not DB_PATH.exists():
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        df = pd.read_sql_query(
            """
            SELECT
                i.sigla AS instituicao,
                COALESCE(COALESCE(d.ano_aceito, f.decidido_ano),
                         CAST(d.ano_provisorio AS INTEGER), 0) AS ano,
                CAST(COUNT(*) AS INTEGER) AS n_docs,
                CAST(SUM(CASE WHEN d.texto_caminho IS NOT NULL THEN 1 ELSE 0 END) AS INTEGER) AS textuados,
                CAST(SUM(CASE WHEN d.texto_caminho IS NOT NULL AND COALESCE(d.texto_chars, 0) > 0
                          THEN 1 ELSE 0 END) AS INTEGER) AS com_texto_util,
                CAST(SUM(CASE WHEN COALESCE(d.flag_escaneado, 0) = 1 THEN 1 ELSE 0 END) AS INTEGER) AS escaneados,
                CAST(SUM(CASE WHEN d.ocr_em IS NOT NULL THEN 1 ELSE 0 END) AS INTEGER) AS resgatados_ocr,
                CAST(SUM(CASE WHEN (d.ano_aceito IS NOT NULL OR f.decidido_ano IS NOT NULL)
                          THEN 1 ELSE 0 END) AS INTEGER) AS datados,
                CAST(SUM(CASE WHEN cl.tipo_edital IS NOT NULL THEN 1 ELSE 0 END) AS INTEGER) AS classificados_l1,
                CAST(SUM(CASE WHEN cl.eixo3_classificacao IS NOT NULL THEN 1 ELSE 0 END) AS INTEGER) AS eixo3,
                CAST(SUM(CASE WHEN COALESCE(f.decidido_exclusao, 0) = 1 THEN 1 ELSE 0 END) AS INTEGER) AS excluidos
            FROM documentos d
            JOIN editais e ON e.id = d.edital_id
            JOIN instituicoes i ON i.id = e.instituicao_id
            LEFT JOIN (
                SELECT url_origem,
                       CASE WHEN MAX(CASE WHEN status = 'resolvida' THEN id END)
                                  IS NOT NULL
                            THEN MAX(CASE WHEN status = 'resolvida' THEN id END)
                            ELSE MAX(id)
                       END AS id_fila_vigente
                FROM fila_revisao
                GROUP BY url_origem
            ) fv ON fv.url_origem = d.url_origem
            LEFT JOIN fila_revisao f ON f.id = fv.id_fila_vigente
            LEFT JOIN classificacoes cl ON cl.url_origem = d.url_origem
            GROUP BY i.sigla, 2
            ORDER BY i.sigla, ano
            """,
            conn,
        )
        conn.close()
    except sqlite3.Error as exc:
        st.warning(f"Aviso: não foi possível ler a cobertura do corpus. {exc}")
        return pd.DataFrame()
    df["ano"] = df["ano"].fillna(0).astype(int)
    return df


# ── sidebar filtros ──────────────────────────────────────────────────────────

st.sidebar.title("Filtros")
todos = carregar_dados()

if todos.empty:
    st.warning("Corpus vazio — rode o pipeline completo antes.")
    st.stop()

excluidos = todos[todos["excluido"] == 1]
dados = todos[todos["excluido"] == 0].copy()

# filtros
insts = sorted(dados["instituicao"].unique())
anos = sorted(dados["ano"].unique())
cats = sorted(dados["categoria"].unique())
tipos = sorted(dados["tipo_edital"].unique())

cat_descricoes = {
    "integra": "Portal Integra (editais gerais)",
    "prppg_inovacao": "Pró-Reitoria de Pesquisa, Pós-Graduação e Inovação",
    "extensao": "Pró-Reitoria de Extensão",
    "ensino": "Pró-Reitoria de Ensino",
    "reitoria": "Reitoria",
    "nit": "Núcleo de Inovação Tecnológica",
    "agencia_inovacao": "Agência de Inovação",
}

sel_inst = st.sidebar.multiselect("Instituição", insts, default=insts[:5])
sel_cat = st.sidebar.multiselect(
    "Categoria", cats, default=cats,
    help=" | ".join(f"{k}: {v}" for k, v in cat_descricoes.items() if k in cats),
)
sel_tipo = st.sidebar.multiselect(
    "Tipo de edital",
    tipos,
    default=tipos,
    help="Classificação automática ('classificar') por sinais de inovação.",
)

# Filtro Eixo 3 — Relevância e Impacto Social
eixo3_opcoes = ["IMPACTO_INSTRUMENTAL", "IMPACTO_SUBSTANTIVO", "AUSENTE_SILENCIAMENTO"]
sel_eixo3 = st.sidebar.multiselect(
    "Eixo 3 (Impacto)",
    eixo3_opcoes,
    default=eixo3_opcoes,
    help="Classificação do Eixo 3: Instrumental (mercado/viabilidade), Substantivo (cotas/bolsas social), Silêncio (ausente).",
)

anos_positivos = [a for a in anos if a > 0]
ano_min = int(min(anos_positivos)) if anos_positivos else 2019
ano_max = int(max(anos)) if anos_positivos else 2026
sel_ano = st.sidebar.slider("Ano", ano_min, ano_max, (ano_min, ano_max))
limite = st.sidebar.slider("Limite de docs para vetorização", 100, 2000, 500, step=100)

flt = dados[
    (dados["instituicao"].isin(sel_inst))
    & (dados["ano"] >= sel_ano[0])
    & (dados["ano"] <= sel_ano[1])
    & (dados["categoria"].isin(sel_cat))
    & (dados["tipo_edital"].isin(sel_tipo))
    & (dados["eixo3_classificacao"].isin(sel_eixo3) if "eixo3_classificacao" in dados.columns else True)
].copy()

# Excluídos sob os MESMOS filtros de inst/categoria, ignorando ano (FR/consultar)
excl_flt = excluidos[
    (excluidos["instituicao"].isin(sel_inst))
    & (excluidos["categoria"].isin(sel_cat))
]

n_pendentes = len(flt[flt["ano"] == 0])
n_escaneados = int(flt["escaneado"].sum())

st.sidebar.markdown(f"**{len(flt)}** documentos selecionados")
if n_pendentes:
    st.sidebar.info(f"{n_pendentes} ainda sem data (fila de revisão).")

# ── ficha técnica: dicionário de stopwords / conectivos (directive 404) ──────
with st.sidebar.expander("Ficha técnica — Análise textual", expanded=False):
    vinfo = info_versao()
    st.caption(
        f"**Stopwords v{vinfo['stopwords_versao']}** — {vinfo['n_stopwords']} termos\n\n"
        f"**Conectivos multiword v{vinfo['conectivos_versao']}** — {vinfo['n_conectivos_multiword']} expressões\n\n"
        f"**Token pattern:** `{TOKEN_PATTERN}`"
    )

# ── métricas gerais ─────────────────────────────────────────────────────────

st.title("  Editais da Rede Federal EPCT")
st.caption("Corpus completo — visualização interativa com análise textual vetorizada")

# Heurística honesta: edital com mais de um documento (várias versões do
# mesmo edital no corpus) — NÃO afirma "resultado" que não conseguimos saber.
editais_ids = flt["edital_id"].unique()
editais_com_multiplos_docs = flt.groupby("edital_id").size()
editais_com_resultado = (editais_com_multiplos_docs > 1).sum()
pct_resultado = (editais_com_resultado / len(editais_ids) * 100) if len(editais_ids) > 0 else 0

c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
c1.metric("Documentos", f"{len(flt):,}")
c2.metric("Instituições", flt["instituicao"].nunique())
c3.metric(
    "Anos",
    f"{int(flt[flt['ano']>0]['ano'].min()) if (flt['ano']>0).any() else 0}–"
    f"{int(flt['ano'].max()) if len(flt) else 0}",
)
c4.metric("Total chars", f"{flt['chars'].sum():,.0f}")
c5.metric("Escaneados", f"{n_escaneados}")
c6.metric("Excluídos", f"{len(excl_flt):,}")
c7.metric("Editais c/ várias versões", f"{pct_resultado:.0f}%")
# Top dimensão analítica
try:
    sinais_cfg = _carregar_sinais_config()
    dimensoes = sinais_cfg.get("dimensoes", [])
    if dimensoes:
        top_dim = dimensoes[0]["nome"]  # fallback
        c8.metric("Dimensão principal", top_dim[:15] + "…" if len(top_dim) > 15 else top_dim)
    else:
        c8.metric("Dimensão principal", "—")
except Exception:
    c8.metric("Dimensão principal", "—")

st.divider()


# ── abas ────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
    [
        "Distribuição",
        "Mapa vetorial",
        "Nuvem de palavras",
        "Explorar",
        "Análise Textual",
        "Varredura Editais",
        "Cobertura do Corpus",
    ]
)

# ── aba 1: visão geral ──────────────────────────────────────────────────────

with tab1:
    col_a, col_b = st.columns(2)

    with col_a:
        fig_bar = px.histogram(
            flt, x="ano", color="instituicao", barmode="stack",
            title="Documentos por ano",
            labels={"ano": "Ano", "count": "Qtd"},
        )
        fig_bar.update_layout(height=400)
        st.plotly_chart(fig_bar, width="stretch")

    with col_b:
        fig_cat = px.pie(
            flt, names="categoria", hole=0.4,
            title="Por categoria",
        )
        fig_cat.update_layout(height=400)
        st.plotly_chart(fig_cat, width="stretch")

    if not flt.empty and flt["ano"].nunique() > 0:
        fig_fonte = px.pie(
            flt[flt["ano"] > 0], names="ano_fonte", hole=0.4,
            title="Origem da data",
            color_discrete_map={
                "automatica": "#2e9e6b",
                "fila_humana": "#e0a029",
                "vazio": "#9aa3ad",
            },
        )
        fig_fonte.update_layout(height=400)
        st.plotly_chart(fig_fonte, width="stretch")

    # heatmap instituição × ano
    if not flt.empty:
        pivot = flt.pivot_table(index="instituicao", columns="ano", aggfunc="size", fill_value=0)
        fig_heat = px.imshow(
            pivot.values,
            x=[str(c) for c in pivot.columns],
            y=pivot.index,
            color_continuous_scale="Blues",
            title="Heatmap: Instituição × Ano",
            aspect="auto",
        )
        fig_heat.update_layout(height=max(400, len(pivot) * 25))
        st.plotly_chart(fig_heat, width="stretch")


# ── aba 2: mapa vetorial (TF-IDF + UMAP) ───────────────────────────────────

with tab2:
    st.subheader("Mapa vetorial — TF-IDF + UMAP")

    amostra = flt.sample(min(limite, len(flt)), random_state=42) if len(flt) > limite else flt

    if len(amostra) < 10:
        st.info("Poucos documentos para vetorização. Ajuste os filtros.")
    else:
        with st.spinner("Carregando textos..."):
            ids_tuple = tuple(amostra["doc_id"].tolist())
            textos_map = carregar_textos(ids_tuple)

        textos_disponiveis = {k: v for k, v in textos_map.items() if v.strip()}
        amostra_filtrada = amostra[amostra["doc_id"].isin(textos_disponiveis)].copy()

        if len(amostra_filtrada) < 10:
            st.warning("Poucos textos disponíveis para vetorização.")
        else:
            with st.spinner("Vetorizando com TF-IDF..."):
                corpus_lista = [limpar_texto(textos_disponiveis[did]) for did in amostra_filtrada["doc_id"]]
                tfidf = TfidfVectorizer(
                    max_features=1500,
                    stop_words=list(STOPWORDS_PT),
                    token_pattern=TOKEN_PATTERN,
                    ngram_range=(1, 1),
                    min_df=2,
                    max_df=0.9,
                )
                X = tfidf.fit_transform(corpus_lista)

            with st.spinner("Projetando com UMAP..."):
                reducer = umap.UMAP(
                    n_neighbors=min(10, len(amostra_filtrada) - 1),
                    n_components=2,
                    metric="cosine",
                    random_state=42,
                )
                coords = reducer.fit_transform(X.toarray())

            amostra_filtrada = amostra_filtrada.copy()
            amostra_filtrada["x"] = coords[:, 0]
            amostra_filtrada["y"] = coords[:, 1]

            color_by = st.radio(
                "Colorir por:", ["instituicao", "ano", "categoria", "ano_fonte"],
                horizontal=True,
            )

            fig_scatter = px.scatter(
                amostra_filtrada,
                x="x", y="y",
                color=color_by,
                hover_data=["instituicao", "ano", "ano_fonte", "portal", "chars"],
                title=f"UMAP — {len(amostra_filtrada)} documentos (TF-IDF, cosine)",
                opacity=0.7,
            )
            fig_scatter.update_traces(marker=dict(size=6))
            fig_scatter.update_layout(height=600)
            st.plotly_chart(fig_scatter, width="stretch")

            # Top termos por frequência (com stopwords PT)
            st.markdown("**Termos mais frequentes:**")
            n_top = st.slider(
                "Nº de termos no topo", 10, 60, 30, step=5, key="top_termos"
            )
            df_termos = _contagem_termos(textos_disponiveis, ngram=(1, 1), min_freq=2)
            if df_termos.empty:
                st.info("Sem termos suficientes nesta seleção.")
            else:
                top = df_termos.head(n_top).iloc[::-1]
                fig_terms = px.bar(
                    top, x="freq", y="termo", orientation="h", height=400,
                    labels={"freq": "ocorrências", "termo": ""},
                )
                st.plotly_chart(fig_terms, width="stretch")


# ── aba 3: nuvem de palavras ────────────────────────────────────────────────

with tab3:
    st.subheader("Nuvem de palavras")

    wc_tipo = st.radio(
        "Unidade:",
        ["Palavras simples", "Bigramas"],
        horizontal=True,
    )
    ngram_nuvem = (1, 2) if wc_tipo == "Bigramas" else (1, 1)
    top_wc = st.slider("Máx. palavras na nuvem", 30, 300, 120, step=10, key="wc_top")
    min_freq_wc = st.slider("Frequência mínima", 1, 20, 2, key="wc_minfreq")
    cor_nuvem = st.selectbox(
        "Colormap",
        ["viridis", "plasma", "magma", "Blues", "Reds", "Greens", "Oranges", "cividis"],
        key="wc_cor",
    )

    sel_inst_wc = st.selectbox("Instituição", ["Todas", *insts], key="wc_inst")

    if sel_inst_wc == "Todas":
        WC_IDS = tuple(flt["doc_id"].tolist())
    else:
        WC_IDS = tuple(flt[flt["instituicao"] == sel_inst_wc]["doc_id"].tolist())

    with st.spinner("Carregando textos..."):
        wc_textos = carregar_textos(WC_IDS)
    wc_textos = {k: v for k, v in wc_textos.items() if v.strip()}

    if not wc_textos:
        st.info("Sem texto disponível para esta seleção.")
    else:
        with st.spinner("Contando frequências..."):
            df_wc = _contagem_termos(wc_textos, ngram=ngram_nuvem, min_freq=min_freq_wc)

        if df_wc.empty:
            st.info("Nenhum termo acima da frequência mínima.")
        else:
            freqs = dict(zip(df_wc["termo"].head(top_wc), df_wc["freq"].head(top_wc)))
            wc = WordCloud(
                width=1400, height=600,
                background_color="white",
                max_words=top_wc,
                max_font_size=120,
                colormap=cor_nuvem,
                normalize_plurals=False,
                collocations=False,
            )
            wc.generate_from_frequencies(freqs)

            fig_wc = px.imshow(wc.to_array())
            fig_wc.update_layout(
                xaxis=dict(showticklabels=False, showgrid=False),
                yaxis=dict(showticklabels=False, showgrid=False),
                height=600,
            )
            st.plotly_chart(fig_wc, width="stretch")

            st.caption(
                f"{len(df_wc)} termos distintos acima da frequência mínima; "
                f"mostrando os {min(top_wc, len(df_wc))} mais frequentes."
            )


# ── aba 4: explorar documentos ─────────────────────────────────────────────

with tab4:
    st.subheader("Explorar documentos")

    col_x, col_y = st.columns([1, 2])
    with col_x:
        portal_opcoes = ["Todos", *sorted(flt["portal"].dropna().unique())]
        sel_portal_exp = st.selectbox("Portal", portal_opcoes, key="exp_portal")
        sel_ano_fonte_exp = st.multiselect(
            "Origem da data",
            ["automatica", "fila_humana", "vazio"],
            default=["automatica", "fila_humana", "vazio"],
            key="exp_fonte",
        )
        exp_limite = st.slider("Máx. resultados", 10, 200, 50, step=10, key="exp_lim")
    with col_y:
        busca = st.text_input(
            "Buscar termo no texto (opcional; insensível a caixa):",
            key="exp_busca",
        )

    base_exp = flt[flt["ano_fonte"].isin(sel_ano_fonte_exp)]
    if sel_portal_exp != "Todos":
        base_exp = base_exp[base_exp["portal"] == sel_portal_exp]

    if busca.strip():
        termo = busca.strip().lower()
        with st.spinner("Buscando no corpus..."):
            ids_busca = tuple(base_exp["doc_id"].tolist())
            textos_busca = carregar_textos(ids_busca)
            hits = {
                did for did, t in textos_busca.items() if termo in t.lower()
            }
            base_exp = base_exp[base_exp["doc_id"].isin(hits)]
        st.caption(f"{len(base_exp)} documento(s) com o termo “{busca.strip()}”.")

    base_exp = base_exp.head(exp_limite)

    if base_exp.empty:
        st.info("Nenhum documento corresponde aos filtros.")
    else:
        vis = base_exp[
            ["doc_id", "instituicao", "portal", "ano", "ano_fonte", "tipo_edital", "chars", "escaneado"]
        ].copy()
        vis = vis.rename(
            columns={
                "doc_id": "Doc",
                "instituicao": "Instituição",
                "portal": "Portal",
                "ano": "Ano",
                "ano_fonte": "Origem da data",
                "tipo_edital": "Tipo de edital",
                "chars": "Chars",
                "escaneado": "Escaneado",
            }
        )
        st.dataframe(vis, width="stretch", hide_index=True)

        ids_mostrar = tuple(base_exp["doc_id"].tolist())
        textos_mostrar = carregar_textos(ids_mostrar)
        for _, row in base_exp.head(20).iterrows():
            did = row["doc_id"]
            trecho = textos_mostrar.get(did, "")
            if not trecho.strip():
                trecho = "*(sem texto extraível — documento escaneado)*"
            else:
                trecho = trecho[:1200]
            with st.expander(
                f"{row['instituicao']} · {row['portal']} · {row['ano'] or 's/ano'} · {did}"
            ):
                st.markdown(trecho)


# ── aba 5: análise textual ─────────────────────────────────────────────────────

with tab5:
    st.subheader("Análise Textual — KWIC, Frequência, Co-ocorrências e Quadro 1.6")

    # Carregar textos da seleção filtrada
    ids_sel = tuple(flt["doc_id"].tolist())
    with st.spinner("Carregando textos da seleção..."):
        textos_sel = carregar_textos(ids_sel)
    textos_sel = {k: v for k, v in textos_sel.items() if v.strip()}

    if not textos_sel:
        st.info("Sem texto disponível para a seleção atual.")
    else:
        sinais_config = _carregar_sinais_config()

        sub_tab1, sub_tab2, sub_tab3, sub_tab4, sub_tab5 = st.tabs(
            ["Concordância (KWIC)", "Frequência de Termos", "Co-ocorrências", "Mineração Quadro 1.6", "Classificação Eixo 3"]
        )

        # Sub-aba 1: KWIC
        with sub_tab1:
            st.markdown("**Concordância (KWIC) — Key Word In Context**")
            col_k1, col_k2 = st.columns([3, 1])
            with col_k1:
                kwic_termo = st.text_input(
                    "Termo para busca:",
                    placeholder="ex: inovação, extensão, território...",
                    key="kwic_termo",
                )
            with col_k2:
                kwic_contexto = st.slider("Contexto (caracteres)", 20, 200, 50, step=10, key="kwic_ctx")
                kwic_limite = st.slider("Máx. resultados", 20, 500, 100, step=20, key="kwic_lim")

            if kwic_termo.strip():
                with st.spinner("Buscando concordâncias..."):
                    df_kwic = _buscar_kwic(textos_sel, kwic_termo, contexto=kwic_contexto, limite=kwic_limite)
                if df_kwic.empty:
                    st.info(f"Nenhuma ocorrência de “{kwic_termo}” encontrada.")
                else:
                    # Enriquecer com metadados
                    meta = flt[flt["doc_id"].isin(df_kwic["doc_id"].unique())][
                        ["doc_id", "instituicao", "ano", "portal", "categoria"]
                    ].drop_duplicates("doc_id")
                    df_kwic = df_kwic.merge(meta, on="doc_id", how="left")
                    st.dataframe(
                        df_kwic[["doc_id", "instituicao", "ano", "portal", "categoria", "posicao", "trecho"]],
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "trecho": st.column_config.TextColumn("Trecho (contexto)", width="large"),
                        },
                    )
                    st.caption(f"{len(df_kwic)} ocorrência(s) em {df_kwic['doc_id'].nunique()} documento(s).")
            else:
                st.info("Digite um termo para buscar a concordância.")

        # Sub-aba 2: Frequência de Termos
        with sub_tab2:
            st.markdown("**Frequência de Termos — Top-N, Busca Específica, Evolução Temporal, Comparação**")

            # --- Busca de termo específico (unigrama, bigrama, trigram) ---
            col_busca1, col_busca2 = st.columns([3, 1])
            with col_busca1:
                termo_busca = st.text_input(
                    "Buscar termo específico (unigrama, bigrama ou trigram — ex.: 'inovação', 'parque tecnológico', 'transferência de tecnologia'):",
                    placeholder="Digite o termo e pressione Enter",
                    key="freq_busca_termo",
                )
            with col_busca2:
                ngram_busca = st.selectbox(
                    "N-grama:",
                    ["Unigrama (1)", "Bigrama (2)", "Trigrama (3)"],
                    index=0,
                    key="freq_ngram_busca",
                )

            # Configuração do n-grama para a busca
            ngram_busca_val = int(ngram_busca.split("(")[1][0])

            col_f1, col_f2, col_f3 = st.columns(3)
            with col_f1:
                freq_ngram = st.radio("Unidade (top-N):", ["Unigramas", "Bigramas", "Trigramas"], horizontal=True, key="freq_ngram")
            with col_f2:
                freq_top_n = st.slider("Top N termos", 10, 100, 30, step=5, key="freq_top")
            with col_f3:
                freq_min = st.slider("Frequência mínima", 1, 20, 2, key="freq_min")

            ngram_sel = (1, 3) if freq_ngram == "Trigramas" else (1, 2) if freq_ngram == "Bigramas" else (1, 1)

            with st.spinner("Calculando frequências..."):
                df_freq = _contagem_termos(textos_sel, ngram=ngram_sel, min_freq=freq_min)

            # --- Resultado da busca específica ---
            if termo_busca.strip():
                termo_norm = " ".join(termo_busca.strip().lower().split())
                palavras = termo_norm.split()
                if len(palavras) != ngram_busca_val:
                    st.warning(f"O termo digitado tem {len(palavras)} palavra(s), mas a opção selecionada é '{ngram_busca}'. Ajuste o termo ou a opção.")
                else:
                    # Busca no df_freq calculado (já limpo com stopwords)
                    match = df_freq[df_freq["termo"] == termo_norm]
                    if not match.empty:
                        freq_encontrada = int(match.iloc[0]["freq"])
                        st.success(f"**“{termo_busca.strip()}”** encontrado: **{freq_encontrada}** ocorrência(s) no corpus filtrado.")
                    else:
                        st.info(f"**“{termo_busca.strip()}”** NÃO encontrado no corpus filtrado (pode estar abaixo da frequência mínima {freq_min} ou ser stopword).")

            if df_freq.empty:
                st.info("Nenhum termo acima da frequência mínima.")
            else:
                # Gráfico de barras top-N
                top = df_freq.head(freq_top_n).iloc[::-1]
                fig_freq = px.bar(
                    top, x="freq", y="termo", orientation="h", height=400,
                    labels={"freq": "ocorrências", "termo": ""},
                    title=f"Top {freq_top_n} {freq_ngram.lower()} mais frequentes",
                )
                st.plotly_chart(fig_freq, width="stretch")

                # Evolução temporal (heatmap termo × ano)
                st.markdown("**Evolução temporal (termos × ano)**")
                # Preparar textos por ano
                anos_disponiveis = sorted(flt[flt["doc_id"].isin(textos_sel.keys())]["ano"].unique())
                anos_disponiveis = [a for a in anos_disponiveis if a > 0]
                if len(anos_disponiveis) > 1 and len(df_freq) > 0:
                    top_termos = df_freq.head(min(20, freq_top_n))["termo"].tolist()
                    dados_heatmap = []
                    for ano in anos_disponiveis:
                        ids_ano = tuple(flt[(flt["ano"] == ano) & (flt["doc_id"].isin(textos_sel.keys()))]["doc_id"].tolist())
                        if not ids_ano:
                            continue
                        textos_ano = {k: v for k, v in carregar_textos(ids_ano).items() if v.strip()}
                        if not textos_ano:
                            continue
                        df_ano = _contagem_termos(textos_ano, ngram=ngram_sel, min_freq=1)
                        freq_dict = dict(zip(df_ano["termo"], df_ano["freq"]))
                        for termo in top_termos:
                            dados_heatmap.append({"termo": termo, "ano": str(ano), "freq": freq_dict.get(termo, 0)})
                    if dados_heatmap:
                        df_hm = pd.DataFrame(dados_heatmap)
                        pivot = df_hm.pivot_table(index="termo", columns="ano", values="freq", fill_value=0)
                        fig_hm = px.imshow(
                            pivot.values,
                            x=pivot.columns.tolist(),
                            y=pivot.index.tolist(),
                            color_continuous_scale="Blues",
                            aspect="auto",
                            title="Frequência dos top termos por ano",
                        )
                        fig_hm.update_layout(height=max(300, len(pivot) * 20))
                        st.plotly_chart(fig_hm, width="stretch")
                    else:
                        st.info("Dados insuficientes para evolução temporal.")
                else:
                    st.info("É necessário pelo menos 2 anos com dados para mostrar evolução temporal.")

                # Comparação entre instituições (opcional)
                st.markdown("**Comparação entre instituições**")
                insts_sel = st.multiselect(
                    "Instituições para comparar (máx. 4):",
                    sorted(flt["instituicao"].unique()),
                    default=sorted(flt["instituicao"].unique())[:3],
                    key="freq_comp_inst",
                )
                if len(insts_sel) >= 2:
                    dfs_comp = []
                    for inst in insts_sel[:4]:
                        ids_inst = tuple(flt[(flt["instituicao"] == inst) & (flt["doc_id"].isin(textos_sel.keys()))]["doc_id"].tolist())
                        if not ids_inst:
                            continue
                        textos_inst = {k: v for k, v in carregar_textos(ids_inst).items() if v.strip()}
                        if not textos_inst:
                            continue
                        df_inst = _contagem_termos(textos_inst, ngram=ngram_sel, min_freq=freq_min)
                        df_inst = df_inst.head(15).copy()
                        df_inst["instituicao"] = inst
                        dfs_comp.append(df_inst)
                    if dfs_comp:
                        df_comp = pd.concat(dfs_comp, ignore_index=True)
                        fig_comp = px.bar(
                            df_comp, x="freq", y="termo", color="instituicao", orientation="h",
                            barmode="group", height=400,
                            title="Top termos por instituição",
                            labels={"freq": "ocorrências", "termo": "", "instituicao": "Instituição"},
                        )
                        st.plotly_chart(fig_comp, width="stretch")
                    else:
                        st.info("Dados insuficientes para comparação.")
                elif insts_sel:
                    st.info("Selecione pelo menos 2 instituições para comparar.")

        # Sub-aba 3: Co-ocorrências
        with sub_tab3:
            st.markdown("**Co-ocorrências — Matriz de termos que aparecem juntos**")
            col_c1, col_c2, col_c3 = st.columns(3)
            with col_c1:
                cooc_top = st.slider("Top N termos", 10, 50, 20, step=5, key="cooc_top")
            with col_c2:
                cooc_janela = st.slider("Janela (palavras)", 3, 30, 10, step=1, key="cooc_jan")
            with col_c3:
                cooc_min = st.slider("Freq. mínima do par", 1, 10, 2, key="cooc_min")

            with st.spinner("Calculando co-ocorrências..."):
                matriz_cooc = _coocorrencias_janela(textos_sel, top_n=cooc_top, janela=cooc_janela, min_freq=cooc_min)

            if matriz_cooc.empty:
                st.info("Sem co-ocorrências suficientes com os parâmetros atuais.")
            else:
                # Heatmap
                fig_cooc = px.imshow(
                    matriz_cooc.values,
                    x=matriz_cooc.columns.tolist(),
                    y=matriz_cooc.index.tolist(),
                    color_continuous_scale="Reds",
                    aspect="auto",
                    title=f"Matriz de co-ocorrência (janela={cooc_janela} palavras, top={cooc_top})",
                )
                fig_cooc.update_layout(height=max(400, len(matriz_cooc) * 18))
                st.plotly_chart(fig_cooc, width="stretch")

                # Pares mais frequentes
                st.markdown("**Pares mais frequentes**")
                pares = []
                for i, t1 in enumerate(matriz_cooc.index):
                    for t2 in matriz_cooc.columns[i + 1 :]:
                        val = matriz_cooc.loc[t1, t2]
                        if val >= cooc_min:
                            pares.append({"Termo 1": t1, "Termo 2": t2, "Co-ocorrências": int(val)})
                if pares:
                    df_pares = pd.DataFrame(pares).sort_values("Co-ocorrências", ascending=False).head(20)
                    st.dataframe(df_pares, width="stretch", hide_index=True)
                else:
                    st.info("Nenhum par acima da frequência mínima.")

        # Sub-aba 4: Mineração Quadro 1.6
        with sub_tab4:
            st.markdown("**Mineração dos Sinais do Quadro 1.6 — 5 Dimensões Analíticas**")
            st.caption(
                "Busca automática dos termos-sinal das 5 dimensões analíticas "
                "(Fundamentos Epistemológicos, Atores/Redes/Poder, Impacto Social, "
                "Governança Institucional, Desenvolvimento Regional) no corpus filtrado."
            )

            with st.spinner("Minerando sinais do Quadro 1.6..."):
                df_sinais = _minerar_sinais_quadro16(textos_sel, sinais_config)

            if df_sinais.empty:
                st.info("Sem textos para mineração.")
            else:
                # Métricas gerais
                dimensoes = df_sinais["dimensao_id"].unique()
                col_m1, col_m2, col_m3, col_m4, col_m5 = st.columns(5)
                for i, dim_id in enumerate(dimensoes):
                    df_dim = df_sinais[df_sinais["dimensao_id"] == dim_id]
                    pct = (df_dim["sinais_encontrados"] > 0).mean() * 100
                    if i == 0:
                        col_m1.metric(df_dim["dimensao_nome"].iloc[0], f"{pct:.1f}% docs")
                    elif i == 1:
                        col_m2.metric(df_dim["dimensao_nome"].iloc[0], f"{pct:.1f}% docs")
                    elif i == 2:
                        col_m3.metric(df_dim["dimensao_nome"].iloc[0], f"{pct:.1f}% docs")
                    elif i == 3:
                        col_m4.metric(df_dim["dimensao_nome"].iloc[0], f"{pct:.1f}% docs")
                    elif i == 4:
                        col_m5.metric(df_dim["dimensao_nome"].iloc[0], f"{pct:.1f}% docs")

                # Heatmap dimensão × instituição
                st.markdown("**Presença das dimensões por instituição**")
                dados_inst = []
                for inst in sorted(flt["instituicao"].unique()):
                    ids_inst = set(flt[flt["instituicao"] == inst]["doc_id"]) & set(textos_sel.keys())
                    if not ids_inst:
                        continue
                    df_inst = df_sinais[df_sinais["doc_id"].isin(ids_inst)]
                    for dim_id in dimensoes:
                        df_dim = df_inst[df_inst["dimensao_id"] == dim_id]
                        pct = (df_dim["sinais_encontrados"] > 0).mean() * 100 if len(df_dim) > 0 else 0
                        nome_dim = df_dim["dimensao_nome"].iloc[0] if len(df_dim) > 0 else dim_id
                        dados_inst.append({"Dimensão": nome_dim, "Instituição": inst, "Presença (%)": round(pct, 1)})
                if dados_inst:
                    df_hm_inst = pd.DataFrame(dados_inst)
                    pivot_inst = df_hm_inst.pivot_table(index="Dimensão", columns="Instituição", values="Presença (%)", fill_value=0)
                    fig_hm_inst = px.imshow(
                        pivot_inst.values,
                        x=pivot_inst.columns.tolist(),
                        y=pivot_inst.index.tolist(),
                        color_continuous_scale="Greens",
                        aspect="auto",
                        title="% de documentos com sinais da dimensão por instituição",
                        text_auto=".1f",
                    )
                    fig_hm_inst.update_layout(height=350)
                    st.plotly_chart(fig_hm_inst, width="stretch")

                # Heatmap dimensão × ano
                st.markdown("**Evolução temporal das dimensões**")
                dados_ano = []
                for ano in sorted(flt[flt["doc_id"].isin(textos_sel.keys())]["ano"].unique()):
                    if ano <= 0:
                        continue
                    ids_ano = set(flt[flt["ano"] == ano]["doc_id"]) & set(textos_sel.keys())
                    if not ids_ano:
                        continue
                    df_ano = df_sinais[df_sinais["doc_id"].isin(ids_ano)]
                    for dim_id in dimensoes:
                        df_dim = df_ano[df_ano["dimensao_id"] == dim_id]
                        pct = (df_dim["sinais_encontrados"] > 0).mean() * 100 if len(df_dim) > 0 else 0
                        nome_dim = df_dim["dimensao_nome"].iloc[0] if len(df_dim) > 0 else dim_id
                        dados_ano.append({"Dimensão": nome_dim, "Ano": str(ano), "Presença (%)": round(pct, 1)})
                if dados_ano:
                    df_hm_ano = pd.DataFrame(dados_ano)
                    # Gráfico de linha: evolução temporal das 5 dimensões
                    fig_ev = px.line(
                        df_hm_ano,
                        x="Ano",
                        y="Presença (%)",
                        color="Dimensão",
                        markers=True,
                        title="Evolução temporal — presença dos sinais por dimensão",
                        labels={"Ano": "Ano", "Presença (%)": "% dos documentos com sinais"},
                    )
                    fig_ev.update_layout(height=400)
                    st.plotly_chart(fig_ev, width="stretch")
                    pivot_ano = df_hm_ano.pivot_table(index="Dimensão", columns="Ano", values="Presença (%)", fill_value=0)
                    fig_hm_ano = px.imshow(
                        pivot_ano.values,
                        x=pivot_ano.columns.tolist(),
                        y=pivot_ano.index.tolist(),
                        color_continuous_scale="Oranges",
                        aspect="auto",
                        title="% de documentos com sinais da dimensão por ano",
                        text_auto=".1f",
                    )
                    fig_hm_ano.update_layout(height=350)
                    st.plotly_chart(fig_hm_ano, width="stretch")

                # Drill-down: KWIC dos sinais por dimensão × instituição × ano
                st.markdown("**Drill-down — Concordância dos sinais por dimensão, instituição e ano**")
                col_d1, col_d2, col_d3, col_d4 = st.columns(4)
                with col_d1:
                    dim_drill = st.selectbox(
                        "Dimensão:",
                        options=[(d["id"], d["nome"]) for d in sinais_config.get("dimensoes", [])],
                        format_func=lambda x: x[1],
                        key="drill_dim",
                    )
                with col_d2:
                    sinais_dim = (
                        next((d for d in sinais_config["dimensoes"] if d["id"] == dim_drill[0]), {}).get("sinais", [])
                        if dim_drill
                        else []
                    )
                    sinal_drill = st.selectbox(
                        "Sinal:",
                        ["Todos os sinais", *sinais_dim],
                        key="drill_sinal",
                    )
                with col_d3:
                    inst_drill = st.selectbox(
                        "Instituição (opcional):",
                        ["Todas", *sorted(flt["instituicao"].unique())],
                        key="drill_inst",
                    )
                with col_d4:
                    anos_drill_opts = sorted(
                        flt[flt["doc_id"].isin(textos_sel.keys())]["ano"].unique()
                    )
                    anos_drill_opts = [a for a in anos_drill_opts if a > 0]
                    sel_anos_drill = st.multiselect(
                        "Anos (independente da sidebar):",
                        [str(a) for a in anos_drill_opts],
                        default=[str(a) for a in anos_drill_opts],
                        key="drill_anos",
                    )
                sel_anos_int = {int(a) for a in sel_anos_drill}

                if dim_drill:
                    dim_id, dim_nome = dim_drill
                    dim_config = next((d for d in sinais_config["dimensoes"] if d["id"] == dim_id), None)
                    if dim_config:
                        sinais_da_dim = dim_config["sinais"]
                        st.caption(f"Sinais da dimensão “{dim_nome}”: {', '.join(sinais_da_dim)}")
                        # Filtrar documentos (dimensão fixa; sinal/inst/ano como controles)
                        ids_filtro = set(textos_sel.keys())
                        if inst_drill != "Todas":
                            ids_filtro &= set(flt[flt["instituicao"] == inst_drill]["doc_id"])
                        if sel_anos_int:
                            ids_filtro &= set(
                                flt[flt["ano"].isin(sel_anos_int)]["doc_id"]
                            )
                        textos_drill = {k: v for k, v in textos_sel.items() if k in ids_filtro}
                        sinais_visar = [sinal_drill] if sinal_drill != "Todos os sinais" else sinais_da_dim
                        if not textos_drill:
                            st.info("Nenhum documento para a seleção.")
                        else:
                            # Ocorrências por instituição (barra) para os sinais visados
                            meta_ids = flt[flt["doc_id"].isin(textos_drill.keys())][
                                ["doc_id", "instituicao", "ano"]
                            ].drop_duplicates("doc_id")
                            linhas_ocorrencias = []
                            for sinal in sinais_visar:
                                df_k = _buscar_kwic(textos_drill, sinal, contexto=40, limite=500)
                                if df_k.empty:
                                    continue
                                df_k = df_k.merge(
                                    meta_ids, on="doc_id", how="left"
                                )
                                for inst, n in df_k.groupby("instituicao").size().items():
                                    linhas_ocorrencias.append(
                                        {"Sinal": sinal, "Instituição": inst, "Ocorrências": int(n)}
                                    )
                            if linhas_ocorrencias:
                                df_ocorrencias = pd.DataFrame(linhas_ocorrencias)
                                fig_oc = px.bar(
                                    df_ocorrencias,
                                    x="Ocorrências",
                                    y="Instituição",
                                    color="Sinal",
                                    orientation="h",
                                    barmode="stack",
                                    title="Ocorrências dos sinais por instituição",
                                )
                                fig_oc.update_layout(height=max(280, len(df_ocorrencias["Instituição"].unique()) * 22))
                                st.plotly_chart(fig_oc, width="stretch")

                            for sinal in sinais_visar:
                                df_kwic_sinal = _buscar_kwic(textos_drill, sinal, contexto=60, limite=10)
                                if not df_kwic_sinal.empty:
                                    with st.expander(f"Sinal: “{sinal}” — {len(df_kwic_sinal)} ocorrência(s)"):
                                        meta = flt[flt["doc_id"].isin(df_kwic_sinal["doc_id"].unique())][
                                            ["doc_id", "instituicao", "ano", "portal"]
                                        ].drop_duplicates("doc_id")
                                        df_kwic_sinal = df_kwic_sinal.merge(meta, on="doc_id", how="left")
                                        st.dataframe(
                                            df_kwic_sinal[["doc_id", "instituicao", "ano", "portal", "posicao", "trecho"]],
                                            width="stretch",
                                            hide_index=True,
                                            column_config={"trecho": st.column_config.TextColumn("Trecho", width="large")},
                                        )

        # Sub-aba 5: Classificação Eixo 3
        with sub_tab5:
            st.markdown("**Classificação do Eixo 3 — Relevância e Impacto Social**")
            st.caption(
                "Classificação automática segundo o Quadro 1.6 (Dimensão 3: Relevância e Impacto Social). "
                "Três códigos possíveis:\n"
                "- **IMPACTO_INSTRUMENTAL**: critérios de avaliação pontuam EXCLUSIVAMENTE 'potencial de mercado' e 'viabilidade financeira'\n"
                "- **IMPACTO_SUBSTANTIVO**: edital reserva cotas/pontuação/bolsas para tecnologias sociais, comunidades vulneráveis ou economia solidária\n"
                "- **AUSENTE_SILENCIAMENTO**: nenhum dos padrões acima encontrado"
            )

            # Verificar se coluna existe
            if "eixo3_classificacao" not in flt.columns:
                st.warning("Coluna 'eixo3_classificacao' não encontrada. Rode a migração v9 do Manifesto e o comando 'classificar_eixo3'.")
            else:
                # Filtro temporal independente da sidebar (Fase 2.3): todas as
                # análises abaixo do Eixo 3 respondem a ESTA janela, e não ao
                # slider genérico de ano da sidebar.
                anos_e3 = sorted(flt[flt["ano"] > 0]["ano"].unique())
                if len(anos_e3) > 1:
                    e3_ini, e3_fim = st.slider(
                        "Período (independente da sidebar)",
                        min_value=int(anos_e3[0]),
                        max_value=int(anos_e3[-1]),
                        value=(int(anos_e3[0]), int(anos_e3[-1])),
                        key="e3_janela",
                    )
                    flt_eixo3 = flt[(flt["ano"] >= e3_ini) & (flt["ano"] <= e3_fim)].copy()
                else:
                    flt_eixo3 = flt.copy()
                st.caption(f"{len(flt_eixo3)} documento(s) na janela selecionada.")

                # Métricas gerais
                col_e1, col_e2, col_e3, col_e4 = st.columns(4)
                contagens = flt_eixo3["eixo3_classificacao"].value_counts()
                with col_e1:
                    st.metric("Instrumental", int(contagens.get("IMPACTO_INSTRUMENTAL", 0)))
                with col_e2:
                    st.metric("Substantivo", int(contagens.get("IMPACTO_SUBSTANTIVO", 0)))
                with col_e3:
                    st.metric("Silêncio", int(contagens.get("AUSENTE_SILENCIAMENTO", 0)))
                with col_e4:
                    st.metric("Total", len(flt_eixo3))

                # Pie chart
                if not contagens.empty:
                    fig_pie = px.pie(
                        values=contagens.values,
                        names=contagens.index,
                        title="Distribuição Eixo 3",
                        color_discrete_map={
                            "IMPACTO_INSTRUMENTAL": "#e74c3c",
                            "IMPACTO_SUBSTANTIVO": "#2ecc71",
                            "AUSENTE_SILENCIAMENTO": "#95a5a6",
                        },
                    )
                    fig_pie.update_layout(height=350)
                    st.plotly_chart(fig_pie, width="stretch")

                # Heatmap: Eixo 3 × Instituição
                st.markdown("**Eixo 3 × Instituição**")
                dados_inst_e3 = []
                for inst in sorted(flt_eixo3["instituicao"].unique()):
                    df_inst = flt_eixo3[flt_eixo3["instituicao"] == inst]
                    for codigo in eixo3_opcoes:
                        qtd = len(df_inst[df_inst["eixo3_classificacao"] == codigo])
                        total = len(df_inst)
                        pct = (qtd / total * 100) if total > 0 else 0
                        dados_inst_e3.append({"Eixo 3": codigo, "Instituição": inst, "Qtd": qtd, "Presença (%)": round(pct, 1)})
                if dados_inst_e3:
                    df_hm_inst_e3 = pd.DataFrame(dados_inst_e3)
                    pivot_inst_e3 = df_hm_inst_e3.pivot_table(index="Eixo 3", columns="Instituição", values="Presença (%)", fill_value=0)
                    fig_hm_inst_e3 = px.imshow(
                        pivot_inst_e3.values,
                        x=pivot_inst_e3.columns.tolist(),
                        y=pivot_inst_e3.index.tolist(),
                        color_continuous_scale="RdYlGn",
                        aspect="auto",
                        title="% de documentos por código Eixo 3 por instituição",
                        text_auto=".1f",
                    )
                    fig_hm_inst_e3.update_layout(height=300)
                    st.plotly_chart(fig_hm_inst_e3, width="stretch")

                # Heatmap: Eixo 3 × Ano
                st.markdown("**Eixo 3 × Ano**")
                dados_ano_e3 = []
                anos_disp = sorted(flt_eixo3[flt_eixo3["ano"] > 0]["ano"].unique())
                for ano in anos_disp:
                    df_ano = flt_eixo3[flt_eixo3["ano"] == ano]
                    for codigo in eixo3_opcoes:
                        qtd = len(df_ano[df_ano["eixo3_classificacao"] == codigo])
                        total = len(df_ano)
                        pct = (qtd / total * 100) if total > 0 else 0
                        dados_ano_e3.append({"Eixo 3": codigo, "Ano": str(ano), "Qtd": qtd, "Presença (%)": round(pct, 1)})
                if dados_ano_e3:
                    df_hm_ano_e3 = pd.DataFrame(dados_ano_e3)
                    pivot_ano_e3 = df_hm_ano_e3.pivot_table(index="Eixo 3", columns="Ano", values="Presença (%)", fill_value=0)
                    fig_hm_ano_e3 = px.imshow(
                        pivot_ano_e3.values,
                        x=pivot_ano_e3.columns.tolist(),
                        y=pivot_ano_e3.index.tolist(),
                        color_continuous_scale="RdYlBu",
                        aspect="auto",
                        title="% de documentos por código Eixo 3 por ano",
                        text_auto=".1f",
                    )
                    fig_hm_ano_e3.update_layout(height=300)
                    st.plotly_chart(fig_hm_ano_e3, width="stretch")

                # Tabela detalhada com trecho probatório
                st.markdown("**Detalhamento por documento**")
                st.caption("Clique no trecho probatório para ver o contexto completo (abre KWIC na sub-aba Concordância).")

                # Preparar dados para a tabela
                df_detalhe = flt_eixo3[["doc_id", "instituicao", "ano", "portal", "eixo3_classificacao", "eixo3_trecho_comprobatorio"]].copy()
                df_detalhe = df_detalhe.rename(columns={
                    "doc_id": "Doc ID",
                    "instituicao": "Instituição",
                    "ano": "Ano",
                    "portal": "Portal",
                    "eixo3_classificacao": "Código Eixo 3",
                    "eixo3_trecho_comprobatorio": "Trecho Probatório",
                })

                # Colorir por código
                def color_eixo3(val):
                    if val == "IMPACTO_INSTRUMENTAL":
                        return "background-color: #ffeaea"
                    elif val == "IMPACTO_SUBSTANTIVO":
                        return "background-color: #eaffea"
                    elif val == "AUSENTE_SILENCIAMENTO":
                        return "background-color: #f5f5f5"
                    return ""

                st.dataframe(
                    df_detalhe.style.applymap(color_eixo3, subset=["Código Eixo 3"]),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Trecho Probatório": st.column_config.TextColumn("Trecho Probatório", width="large"),
                    },
                )

                # Botão exportar CSV
                csv_bytes = df_detalhe.to_csv(index=False).encode("utf-8")
                st.download_button(
                    label="📥 Exportar CSV",
                    data=csv_bytes,
                    file_name=f"eixo3_classificacao_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                )


# ── aba 6: varredura editais ────────────────────────────────────────────────

with tab6:
    st.subheader("Varredura Editais")
    st.caption(
        "Registre URLs de páginas institucionais que listam editais. "
        "A varredura e a classificação rodam pela CLI em um subprocess — este "
        "dashboard nunca acessa a rede (AD-5) e respeita a janela off-peak "
        "do host."
    )

    urls_raw = st.text_area(
        "URLs (uma por linha)",
        placeholder="https://portal.ifex.edu.br/editais/chamadas/abertas/",
        height=110,
        key="varr_urls",
    )
    col_b1, col_b2, _col_rest = st.columns([1, 1, 3])
    with col_b1:
        registrar = st.button("Registrar", type="primary")
    with col_b2:
        rodar_agora = st.button("Registrar e rodar agora")

    forcar = st.checkbox(
        "Ignorar a janela off-peak (--fora-da-janela)",
        help="Fora da janela, a CLI recusa comandos que acessam rede.",
        key="varr_forcar",
    )

    if registrar or rodar_agora:
        urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]
        if not urls:
            st.warning("Cole ao menos uma URL (uma por linha).")
        else:
            with st.spinner("Registrando varreduras..."):
                cod, saida = _roda_cli("varredura", "adicionar", *urls)
            if cod == 0:
                st.success(saida)
            else:
                st.error(saida)
            if rodar_agora and cod == 0:
                with st.spinner("Rodando varreduras pendentes..."):
                    args = ["varredura", "rodar"]
                    if forcar:
                        args.append("--fora-da-janela")
                    cod2, saida2 = _roda_cli(*args)
                if cod2 == 0:
                    st.success(saida2)
                else:
                    st.error(saida2)
            carregar_dados.clear()
            st.rerun()

    if rodar_agora and not urls_raw.strip():
        with st.spinner("Rodando varreduras pendentes..."):
            args = ["varredura", "rodar"]
            if forcar:
                args.append("--fora-da-janela")
            cod, saida = _roda_cli(*args)
        if cod == 0:
            st.success(saida)
        else:
            st.error(saida)
        st.rerun()

    st.divider()
    st.markdown("**Status das varreduras**")
    varr = _varreduras()
    if varr.empty:
        st.info("Nenhuma varredura registrada até agora.")
    else:
        st.dataframe(varr, width="stretch", hide_index=True)

        concluidas = varr[varr["Status"] == "concluida"]
        if concluidas.empty:
            st.info(
                "Nenhuma varredura concluída ainda — registre uma URL e rode a "
                "varredura para acompanhar os candidatos no pipeline."
            )
        else:
            st.markdown("**Candidatos no pipeline da varredura (v10)**")
            opcoes = {
                int(linha.ID): f"#{int(linha.ID)} — {linha['Instituição']} — {linha['URL']}"
                for _, linha in concluidas.sort_values("ID", ascending=False).iterrows()
            }
            escolhida = st.selectbox(
                "Varredura concluída",
                options=list(opcoes),
                format_func=lambda vid: opcoes[vid],
                key="varr_ver_candidatos",
            )
            cand = _candidatos_de_varredura(escolhida)
            if cand.empty:
                st.info("Esta varredura não descobriu candidatos novos.")
            else:
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Baixados", int((cand["Baixado"] == "✔").sum()))
                m2.metric("Textuados", int((cand["Textuado"] == "✔").sum()))
                m3.metric("Datados", int((cand["Datado"] == "✔").sum()))
                m4.metric("Classificados", int((cand["Classificado"] == "✔").sum()))
                m5.metric("Eixo 3", int((cand["Eixo 3"] != "–").sum()))
                st.dataframe(
                    cand,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "URL": st.column_config.LinkColumn("URL", width="large"),
                    },
                )

            log = _log_varredura(escolhida)
            if log:
                with st.expander("Log da varredura"):
                    for marcador, descricao in _entradas_de_varredura(log):
                        st.markdown(f"- `{marcador}` — {descricao}")

            st.divider()
            st.markdown("**Seguir para o pipeline**")
            st.caption(
                "Roda coletar → textuar → datar → classificar → eixo 3 somente "
                "sobre os candidatos desta varredura — síncrono pela CLI "
                "(subprocess) e para no primeiro estágio que falhar."
            )
            if st.button(
                f"Seguir para o pipeline — varredura #{escolhida}",
                type="primary",
            ):
                estagios = [
                    ("coletar", "Colhendo PDFs"),
                    ("textuar", "Extraindo texto"),
                    ("datar", "Datando os editais"),
                    ("classificar", "Classificando L1"),
                    ("classificar-eixo3", "Eixo 3 (relevância e impacto)"),
                ]
                barra = st.progress(0.0, text="Pronto")
                falhou: str | None = None
                for indice, (estagio, rotulo) in enumerate(estagios):
                    barra.progress(
                        indice / len(estagios), text=f"{rotulo} ({estagio})"
                    )
                    with st.spinner(f"{rotulo} ({estagio})..."):
                        cod, saida = _roda_cli(estagio, "--varredura", str(escolhida))
                    if cod == 0:
                        st.success(rotulo)
                        with st.expander(f"Saída de {estagio}"):
                            st.code(saida, language="text")
                    else:
                        falhou = estagio
                        st.error(f"Estágio {estagio} falhou.")
                        st.code(saida, language="text")
                        break
                barra.progress(1.0, text="Concluído")
                if falhou is None:
                    st.success("Pipeline completo para a varredura.")
                carregar_dados.clear()
                st.rerun()


# ── aba 7: cobertura do corpus ───────────────────────────────────────────────

with tab7:
    st.subheader("Cobertura do Corpus — avanço do pipeline por instituição e ano")
    st.caption(
        "Contagem de TODOS os documentos do Manifesto e a taxa de avanço de cada "
        "estágio: baixado → textuado → com texto útil → datado → classificado L1 → "
        "Eixo 3 → catálogo L2 (CAP-7). Independe dos filtros da sidebar."
    )

    cobertura = _cobertura_corpus()
    if cobertura.empty:
        st.info("Sem dados de cobertura — rode ao menos `textuar` uma vez.")
    else:
        n_docs_total = int(cobertura["n_docs"].sum())
        n_editais = len(todos["edital_id"].unique())
        n_insts = cobertura["instituicao"].nunique()
        anos_com_dados = cobertura[cobertura["ano"] > 0]["ano"]
        n_anos = int(anos_com_dados.nunique()) if len(anos_com_dados) else 0
        c_l2 = _catalogo_l2()

        pct_texto = int(cobertura["com_texto_util"].sum() / n_docs_total * 100)
        pct_datado = int(cobertura["datados"].sum() / n_docs_total * 100)
        pct_l1 = int(cobertura["classificados_l1"].sum() / n_docs_total * 100)

        k1, k2, k3, k4, k5, k6, k7, k8 = st.columns(8)
        k1.metric("Documentos", f"{n_docs_total:,}")
        k2.metric("Editais", f"{n_editais:,}")
        k3.metric("Instituições", n_insts)
        k4.metric("Anos cobertos", n_anos)
        k5.metric("Com texto útil", f"{pct_texto}%")
        k6.metric("Datados", f"{pct_datado}%")
        k7.metric("Classificados L1", f"{pct_l1}%")
        k8.metric("No catálogo L2", c_l2["editais_l2"])

        # Funil dos estágios
        funil = pd.DataFrame(
            {
                "Estágio": [
                    "Baixados (documentos)",
                    "Textuados",
                    "Com texto útil (chars > 0)",
                    "Datados",
                    "Classificados L1 (sinais)",
                    "Eixo 3 (impacto)",
                    "L2 — no catálogo analítico",
                ],
                "Documentos": [
                    n_docs_total,
                    int(cobertura["textuados"].sum()),
                    int(cobertura["com_texto_util"].sum()),
                    int(cobertura["datados"].sum()),
                    int(cobertura["classificados_l1"].sum()),
                    int(cobertura["eixo3"].sum()),
                    c_l2["editais_l2"],
                ],
            }
        )
        funil["% do corpus"] = (funil["Documentos"] / max(n_docs_total, 1) * 100).round(1)

        col_f1, col_f2 = st.columns([1, 1])
        with col_f1:
            fig_funil = px.funnel(funil, x="Documentos", y="Estágio", title="Funil do pipeline")
            fig_funil.update_layout(height=420)
            st.plotly_chart(fig_funil, width="stretch")
        with col_f2:
            st.dataframe(
                funil,
                width="stretch",
                hide_index=True,
                column_config={
                    "Estágio": st.column_config.TextColumn("Estágio", width="large"),
                    "Documentos": st.column_config.NumberColumn("Documentos", format="%d"),
                    "% do corpus": st.column_config.ProgressColumn("% do corpus", min_value=0, max_value=100),
                },
            )

        # Heatmap cobertura ano × instituição (nº de documentos)
        if "ano" in cobertura.columns:
            pivot_cob = cobertura.pivot_table(
                index="instituicao", columns="ano", values="n_docs", fill_value=0
            )
            fig_cob = px.imshow(
                pivot_cob.values,
                x=[str(c) for c in pivot_cob.columns],
                y=pivot_cob.index,
                color_continuous_scale="Blues",
                title="Documentos por instituição × ano",
                aspect="auto",
                text_auto=True,
            )
            fig_cob.update_layout(height=max(420, len(pivot_cob) * 24))
            st.plotly_chart(fig_cob, width="stretch")

        # Barras de cobertura relativa por instituição
        inst_agg = cobertura.groupby("instituicao", as_index=False)[
            ["n_docs", "textuados", "com_texto_util", "datados", "classificados_l1", "escaneados"]
        ].sum()
        inst_agg["% texto útil"] = (inst_agg["com_texto_util"] / inst_agg["n_docs"] * 100).round(1)
        inst_agg["% datados"] = (inst_agg["datados"] / inst_agg["n_docs"] * 100).round(1)
        inst_agg["% L1"] = (inst_agg["classificados_l1"] / inst_agg["n_docs"] * 100).round(1)
        longas = inst_agg.melt(
            id_vars=["instituicao"],
            value_vars=["% texto útil", "% datados", "% L1"],
            var_name="Estágio",
            value_name="% do corpus",
        )
        fig_inst = px.bar(
            longas,
            x="instituicao",
            y="% do corpus",
            color="Estágio",
            barmode="group",
            title="Avanço do pipeline por instituição (%)",
        )
        fig_inst.update_layout(height=420)
        st.plotly_chart(fig_inst, width="stretch")

        # Tabela detalhada
        st.markdown("**Tabela detalhada**")
        detalhe_cob = cobertura.rename(
            columns={
                "instituicao": "Instituição",
                "ano": "Ano",
                "n_docs": "Docs",
                "textuados": "Textuados",
                "com_texto_util": "Com texto útil",
                "datados": "Datados",
                "classificados_l1": "L1",
                "eixo3": "Eixo 3",
                "escaneados": "Escaneados",
                "resgatados_ocr": "Resgatados OCR",
                "excluidos": "Excluídos",
            }
        )
        st.dataframe(detalhe_cob, width="stretch", hide_index=True)
