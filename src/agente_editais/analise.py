"""Análise L2 (CAP-7) — catalogação POR EDITAL com instrumento congelado.

Governado por:
- AD-6: gates bloqueantes EM CÓDIGO (chamador ``consulta.analise`` recusa com
  exit 2 antes de qualquer rede quando ``codebook.gates_pendentes()``
  devolve algo); provedor acessado EXCLUSIVAMENTE via ``llm_adapter``
  (função única ``concluir``); verificador citação↔texto NO CAMINHO DE
  GRAVAÇÃO (FR-18): trecho normalizado (caixa/espaços) que não existe no
  ``.txt`` do documento citado grava o campo com
  ``verificacao='citacao_invalidada'`` — custódia preservada, fora do
  catálogo válido. Saída inválida ao esquema derivado do codebook é
  reprocessada até ``--tentativas`` e JAMAIS gravada.
- AD-11: consumo exclusivo do TEXTO_EXTRAIDO (``documentos.texto_caminho``,
  vigência triple-check estilo ``ja_extraido`` do extrator único); PDF nunca
  é parseado aqui; ``flag_escaneado=1`` é pulado com evento
  (OCR é pré-requisito futuro).
- FR-17: unidade de processamento = EDITAL — textos de todos os seus
  documentos não excluídos alimentam UMA chamada, ordenados por
  ``data_captura, rowid`` (precedência cronológica: captura posterior
  prevalece na consolidação); a regra também vive declarada no codebook.
- Exclusão PARCIAL (semântica explícita): a decisão humana de exclusão é
  POR DOCUMENTO — um edital com apenas PARTE dos documentos excluídos segue
  no lote alimentado pelos documentos RESTANTES (excluídos nunca entram na
  entrada); só quando TODOS os seus documentos estão sob exclusão vigente o
  edital fica fora, contado como excluído no resumo/evento.
- FR-18: instrumento congelado POR LOTE — a assinatura completa mora em
  ``lotes_l2``; mesma assinatura CONTINUA o lote aberto (editais já
  codificados são pulados); componente diferente ⇒ NOVO lote, linhas
  delimitadas por ``lote_id``.
- AD-1/AD-10: lote retomável e idempotente sobre o Manifesto; cada desfecho
  vira evento append-only (``analise_aplicada``/``analise_invalida``/
  ``analise_erro``/``analise_escaneado_sem_ocr``/
  ``analise_portal_concluida`` + ``analise_concluido``); falha pontual
  NUNCA aborta o lote (exit 0 com perdas registradas).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import llm_adapter
from .codebook import Campo, Codebook
from .manifest import Manifesto
from .mapa import Portal

PROMPT_VERSAO = "l2-catalogacao-v1"
SENTINELA_NA = "N/A"
_CHUNK_LEITURA = 1024 * 1024


class SaidaInvalida(ValueError):
    """Saída do provedor fora do esquema derivado do codebook (nunca grava)."""


@dataclass(slots=True)
class ContextoAnalise:
    """Portal resolvido contra o Manifesto para uma execução de análise."""

    instituicao_sigla: str
    portal: Portal
    portal_id: int


@dataclass(slots=True)
class ResumoAnalisePortal:
    """Contagens da análise de UM portal — vira saída CLI e evento."""

    instituicao_sigla: str
    portal_nome: str
    editais: int = 0
    codificados: int = 0
    pulados: int = 0
    invalidos: int = 0
    erros: int = 0
    excluidos: int = 0
    escaneados_sem_ocr: int = 0
    editais_perdidos: list[str] = field(default_factory=list)

    def totalizar(self) -> dict:
        return {
            "editais": self.editais,
            "codificados": self.codificados,
            "pulados_ja_codificados": self.pulados,
            "invalidos": self.invalidos,
            "erros": self.erros,
            "excluidos": self.excluidos,
            "escaneados_sem_ocr": self.escaneados_sem_ocr,
            "editais_perdidos": list(self.editais_perdidos),
        }


@dataclass(slots=True)
class DesfechoAnalise:
    """Resultado unitário de UM Edital — agrega no resumo do portal."""

    desfecho: str  # codificado|invalido|erro
    motivo: str | None = None
    escaneados_pulados: int = 0


# -- prompt congelado ----------------------------------------------------------


_PROMPT_TEMPLATE = """\
Você é um codificador de editais de fomento/bolsas/incubação em uma pesquisa \
acadêmica sobre governança e inovação na Rede Federal EPCT.

TAREFA: codificar o edital fornecido na mensagem do usuário, preenchendo \
TODOS os campos do codebook abaixo.

REGRAS ABSOLUTAS:
1. Baseie cada decisão EXCLUSIVAMENTE nos textos dos documentos fornecidos.
2. Todo valor diferente de "{sentinela_na}" exige citação-evidência: \
"citacao_trecho" deve ser trecho LITERAL copiado do documento citado — a \
citação é verificada contra o texto; trecho inexistente invalida o campo.
3. "citacao_documento" deve ser o id de um dos blocos \
"=== DOCUMENTO <id> ===" presentes na entrada.
4. Use "{sentinela_na}" somente quando a regra do campo permitir e o texto \
não trouxer base para decidir; nesse caso "citacao_documento" e \
"citacao_trecho" devem ser null.
5. Responda APENAS com um objeto JSON válido, sem texto fora do JSON e sem \
cercas de código.

CONTRATO JSON (todos os campos são obrigatórios):
{{"campos": {{"<campo_id>": {{"valor": <valor conforme a escala>, \
"citacao_documento": "<documento_id> ou null", "citacao_trecho": "<trecho literal> \
ou null"}}, ...}}}}

IDS DE CAMPOS OBRIGATÓRIOS: {ids_campos}

CODEBOOK (definições operacionais, regras de decisão, escalas e âncoras):
{codebook_yaml}"""


def montar_prompt_sistema(codebook: Codebook) -> tuple[str, str]:
    """Renderiza o prompt-sistema do lote e calcula seu SHA-256 (AD-9).

    O hash registrado no lote cobre o texto EFETIVO do prompt-sistema desta
    execução (template + codebook serializado) — constante entre os editais
    do lote porque o codebook é fixo por assinatura; mudou hash ⇒ novo lote.
    """
    codebook_yaml = yaml.safe_dump(
        codebook.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )
    texto = _PROMPT_TEMPLATE.format(
        sentinela_na=SENTINELA_NA,
        ids_campos=", ".join(codebook.ids_de_campos()),
        codebook_yaml=codebook_yaml.strip(),
    )
    return texto, hashlib.sha256(texto.encode("utf-8")).hexdigest()


# -- verificação citação ↔ texto (FR-18) ----------------------------------------


def _normalizar(texto: str) -> str:
    """Caixa e espaços colapsados — comparação de trechos (FR-18)."""
    return " ".join(texto.casefold().split())


def _normalizar_com_mapa(texto: str) -> tuple[str, list[int]]:
    """Par da ``_normalizar`` mantendo, por caractere normalizado, o índice
    ORIGINAL de onde veio — permite mapear o trecho achado de volta à página."""
    caracteres: list[str] = []
    mapa: list[int] = []
    em_espaco = True
    for i, ch in enumerate(texto):
        dobrado = ch.casefold()
        if ch.isspace() and dobrado.isspace():
            if not em_espaco:
                caracteres.append(" ")
                mapa.append(i)
                em_espaco = True
            continue
        for parte in dobrado:
            caracteres.append(parte)
            mapa.append(i)
        em_espaco = False
    while caracteres and caracteres[-1] == " ":
        caracteres.pop()
        mapa.pop()
    return "".join(caracteres), mapa


def verificar_citacao(trecho: str, texto_documento: str) -> int | None:
    """Página (1-based) do trecho no txt do documento; None se inexistente.

    Normaliza caixa/espaços dos dois lados; a PÁGINA usa a convenção do
    extrator único: páginas separadas por "\\n" — conta-se o número de
    separadores até a posição original do trecho (+1).

    LIMITAÇÃO DE PROVENIÊNCIA (documentada): quando o trecho citado CRUZA a
    fronteira "\\n" entre duas páginas, a página gravada é a do PRIMEIRO
    caractere casado — a citação fica atribuída à página inicial, sem
    registro de span multi-página.
    """
    alvo = _normalizar(trecho)
    if not alvo:
        return None
    normalizado, mapa = _normalizar_com_mapa(texto_documento)
    indice = normalizado.find(alvo)
    if indice == -1:
        return None
    indice_original = mapa[indice]
    return texto_documento.count("\n", 0, indice_original) + 1


# -- validação da saída contra o esquema derivado do codebook ---------------------


_CHAVES_CITACAO = {"valor", "citacao_documento", "citacao_trecho"}


def _extrair_json(bruto: str) -> object:
    texto = bruto.strip()
    inicio = texto.find("{")
    fim = texto.rfind("}")
    if inicio == -1 or fim <= inicio:
        raise SaidaInvalida("resposta não contém um objeto JSON")
    try:
        return json.loads(texto[inicio : fim + 1])
    except ValueError as exc:
        raise SaidaInvalida(f"JSON malformado ({exc})") from exc


def validar_saida(
    bruto: str,
    codebook: Codebook,
    documentos_por_id: dict[str, sqlite3.Row],
) -> list[dict]:
    """Valida a saída bruta contra o codebook; devolve registros normalizados.

    Estrito por contrato: objeto raiz com a única chave ``campos``; exatamente
    os ids do codebook (faltante OU extra ⇒ inválido); valor dentro da escala
    do campo (ordinal inteiro em ``valores``, nominal em ``opcoes``,
    "{SENTINELA_NA}" só quando ``permite_na``); todo valor real exige
    citação com documento pertencente à entrada e trecho não vazio.
    Qualquer violação levanta ``SaidaInvalida`` com TODOS os problemas.
    """
    problemas: list[str] = []
    objeto = _extrair_json(bruto)
    if not isinstance(objeto, dict):
        raise SaidaInvalida("raiz do JSON deve ser um objeto")
    chaves_raiz = set(objeto)
    if chaves_raiz != {"campos"}:
        problemas.append(
            f"raiz deve ter exatamente a chave 'campos' (recebido: {sorted(chaves_raiz)})"
        )
        raise SaidaInvalida("; ".join(problemas))
    campos_recebidos = objeto["campos"]
    if not isinstance(campos_recebidos, dict):
        problemas.append("'campos' deve ser um objeto mapeando campo → decisão")
        raise SaidaInvalida("; ".join(problemas))

    esperados = codebook.ids_de_campos()
    recebidos = set(campos_recebidos)
    faltantes = [cid for cid in esperados if cid not in recebidos]
    if faltantes:
        problemas.append(f"campos ausentes na saída: {', '.join(sorted(faltantes))}")
    excedentes = sorted(recebidos - set(esperados))
    if excedentes:
        problemas.append(f"campos desconhecidos na saída: {', '.join(excedentes)}")

    registros: list[dict] = []
    for campo in codebook.campos():
        if campo.id not in campos_recebidos:
            continue
        item = campos_recebidos[campo.id]
        if not isinstance(item, dict):
            problemas.append(f"'{campo.id}': decisão deve ser um objeto")
            continue
        chaves = set(item)
        if not chaves <= _CHAVES_CITACAO:
            problemas.append(
                f"'{campo.id}': chaves desconhecidas {sorted(chaves - _CHAVES_CITACAO)}"
            )
        if "valor" not in chaves:
            problemas.append(f"'{campo.id}': 'valor' ausente")
            continue
        valor = item["valor"]
        if not campo.valor_valido(valor):
            problemas.append(
                f"'{campo.id}': valor {valor!r} fora da escala ({_escala_descritiva(campo)})"
            )
            continue
        if valor == SENTINELA_NA:
            for chave in ("citacao_documento", "citacao_trecho"):
                if item.get(chave) is not None:
                    problemas.append(
                        f"'{campo.id}': '{chave}' deve ser null quando o valor é {SENTINELA_NA}"
                    )
            registros.append(
                {
                    "campo": campo.id,
                    "valor": valor,
                    "documento_id": None,
                    "url_origem": None,
                    "trecho": None,
                }
            )
            continue
        documento_id = item.get("citacao_documento")
        trecho = item.get("citacao_trecho")
        if not isinstance(documento_id, str) or documento_id not in documentos_por_id:
            problemas.append(
                f"'{campo.id}': 'citacao_documento' deve ser um dos ids da "
                f"entrada (recebido {documento_id!r})"
            )
            continue
        if not isinstance(trecho, str) or not trecho.strip():
            problemas.append(f"'{campo.id}': 'citacao_trecho' ausente ou vazio")
            continue
        linha_documento = documentos_por_id[documento_id]
        registros.append(
            {
                "campo": campo.id,
                "valor": valor,
                "documento_id": documento_id,
                "url_origem": str(linha_documento["url_origem"]),
                "trecho": trecho,
            }
        )

    if problemas:
        raise SaidaInvalida("; ".join(problemas))
    return registros


def _escala_descritiva(campo: Campo) -> str:
    if campo.escala == "ordinal":
        return f"ordinal {'/'.join(str(v) for v in campo.valores or [])}"
    return f"nominal {'/'.join(campo.opcoes or [])}" + (
        f" + {SENTINELA_NA}" if campo.permite_na else ""
    )


# -- entrada do edital -------------------------------------------------------------


def _sha256_arquivo(caminho: Path) -> str:
    hasher = hashlib.sha256()
    with caminho.open("rb") as entrada:
        for pedaco in iter(lambda: entrada.read(_CHUNK_LEITURA), b""):
            hasher.update(pedaco)
    return hasher.hexdigest()


def _texto_vigente(documento: sqlite3.Row) -> Path | None:
    """Triple-check estilo ``ja_extraido`` (AD-11): proveniência + txt presente
    + bytes do PDF batendo no hash do L1. Devolve o caminho do txt ou None."""
    if documento["extraido_em"] is None or documento["texto_caminho"] is None:
        return None
    pdf = Path(documento["caminho"])
    txt = Path(documento["texto_caminho"])
    try:
        if not pdf.is_file() or not txt.is_file():
            return None
        if _sha256_arquivo(pdf) != documento["hash_sha256"]:
            return None
    except OSError:
        return None
    return txt


def _montar_entrada(documentos: list[sqlite3.Row], textos: dict[str, str]) -> str:
    """Textos dos documentos em UMA mensagem, precedência cronológica (FR-17)."""
    secoes = [
        f"=== DOCUMENTO {documento['id']} ===\n{textos[documento['id']]}"
        for documento in documentos
    ]
    return "\n\n".join(secoes)


# -- execução ------------------------------------------------------------------------


def analisar_edital(
    edital_id: str,
    documentos: list[sqlite3.Row],
    manifesto: Manifesto,
    codebook: Codebook,
    *,
    sistema: str,
    lote_id: int,
    tentativas: int,
    comando: str = "analise",
) -> DesfechoAnalise:
    """Codifica UM Edital: entrada → chamada → validação → citações → catálogo.

    Ordem deliberada (kill-safe): decisões locais (escaneados/vigência) viram
    eventos isolados; a chamada ao provedor acontece só com entrada pronta;
    saída inválida é reprocessada sem gravar NADA; a gravação é transação
    única com verificação de citação no caminho (FR-18). Falha pontual NUNCA
    aborta o lote.
    """
    rotulo = {"lote_id": lote_id, "edital_id": edital_id}

    utilizaveis: list[sqlite3.Row] = []
    escaneados_pulados = 0
    for documento in documentos:
        if documento["flag_escaneado"] == 1:
            escaneados_pulados += 1
            manifesto.registrar_evento(
                tipo="analise_escaneado_sem_ocr",
                comando=comando,
                detalhe={
                    **rotulo,
                    "documento_id": documento["id"],
                    "url": documento["url_origem"],
                },
            )
            continue
        utilizaveis.append(documento)

    if escaneados_pulados == len(documentos):
        manifesto.registrar_evento(
            tipo="analise_erro",
            comando=comando,
            detalhe={
                **rotulo,
                "motivo": "edital_escaneado_sem_ocr",
                "documentos": len(documentos),
            },
        )
        return DesfechoAnalise(
            "erro", motivo="edital_escaneado_sem_ocr", escaneados_pulados=escaneados_pulados
        )

    textos: dict[str, str] = {}
    for documento in utilizaveis:
        txt = _texto_vigente(documento)
        if txt is None:
            manifesto.registrar_evento(
                tipo="analise_erro",
                comando=comando,
                detalhe={
                    **rotulo,
                    "motivo": "texto_indisponivel",
                    "documento_id": documento["id"],
                    "url": documento["url_origem"],
                },
            )
            continue
        try:
            textos[documento["id"]] = txt.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            manifesto.registrar_evento(
                tipo="analise_erro",
                comando=comando,
                detalhe={
                    **rotulo,
                    "motivo": "texto_ilegivel",
                    "documento_id": documento["id"],
                    "url": documento["url_origem"],
                    "erro": f"{type(exc).__name__}",
                },
            )
    if not textos:
        manifesto.registrar_evento(
            tipo="analise_erro",
            comando=comando,
            detalhe={
                **rotulo,
                "motivo": "entrada_sem_texto_util",
                "documentos": len(documentos),
                "escaneados": escaneados_pulados,
            },
        )
        return DesfechoAnalise(
            "erro", motivo="entrada_sem_texto_util", escaneados_pulados=escaneados_pulados
        )

    documentos_ordenados = [d for d in utilizaveis if d["id"] in textos]
    documentos_por_id = {d["id"]: d for d in documentos_ordenados}
    usuario = _montar_entrada(documentos_ordenados, textos)

    # Orçamento ÚNICO de tentativas cobre AMBAS as falhas reprocessáveis:
    # saída inválida ao esquema (motivo 'esquema') E falha do provedor
    # (rede/HTTP/timeout — motivo 'provedor'). Um blip HTTP não perde mais o
    # edital para sempre na rodada; esgotadas as tentativas, o evento registra
    # qual dos motivos derrubou a última tentativa.
    ultimo_problema: str | None = None
    ultimo_motivo: str | None = None  # 'esquema' | 'provedor'
    usadas = 0
    validados: list[dict] = []
    for numero in range(1, max(1, tentativas) + 1):
        usadas = numero
        try:
            bruto = llm_adapter.concluir(sistema, usuario)
        except llm_adapter.ErroProvedorLLM as exc:
            ultimo_problema = f"{type(exc).__name__}: {exc}"
            ultimo_motivo = "provedor"
            continue
        try:
            validados = validar_saida(bruto, codebook, documentos_por_id)
            break
        except SaidaInvalida as exc:
            ultimo_problema = str(exc)
            ultimo_motivo = "esquema"
    else:
        if ultimo_motivo == "provedor":
            manifesto.registrar_evento(
                tipo="analise_erro",
                comando=comando,
                detalhe={
                    **rotulo,
                    "motivo": "provedor",
                    "tentativas": usadas,
                    "erro": ultimo_problema,
                },
            )
            return DesfechoAnalise(
                "erro", motivo="provedor", escaneados_pulados=escaneados_pulados
            )
        manifesto.registrar_evento(
            tipo="analise_invalida",
            comando=comando,
            detalhe={
                **rotulo,
                "motivo": "esquema",
                "tentativas": usadas,
                "problema": ultimo_problema,
            },
        )
        return DesfechoAnalise(
            "invalido", motivo="saida_invalida", escaneados_pulados=escaneados_pulados
        )

    registros_gravacao: list[dict] = []
    campos_ok = 0
    campos_invalidados = 0
    for item in validados:
        if item["valor"] == SENTINELA_NA:
            registros_gravacao.append(
                {
                    "campo": item["campo"],
                    "valor": SENTINELA_NA,
                    "documento_id": None,
                    "url_origem": None,
                    "citacao_trecho": None,
                    "citacao_pagina": None,
                    "verificacao": "ok",
                }
            )
            campos_ok += 1
            continue
        texto_documento = textos[item["documento_id"]]
        pagina = verificar_citacao(item["trecho"], texto_documento)
        valida = pagina is not None
        if valida:
            campos_ok += 1
        else:
            campos_invalidados += 1
        valor = item["valor"]
        registros_gravacao.append(
            {
                "campo": item["campo"],
                "valor": valor if isinstance(valor, str) else str(valor),
                "documento_id": item["documento_id"],
                "url_origem": item["url_origem"],
                "citacao_trecho": item["trecho"],
                "citacao_pagina": pagina,
                "verificacao": "ok" if valida else "citacao_invalidada",
            }
        )

    gravados = manifesto.registrar_campos_l2(lote_id, edital_id, registros_gravacao)
    if gravados != len(registros_gravacao):  # defesa: transação única é all-or-nothing
        manifesto.registrar_evento(
            tipo="analise_erro",
            comando=comando,
            detalhe={**rotulo, "motivo": "persistencia"},
        )
        return DesfechoAnalise(
            "erro", motivo="persistencia", escaneados_pulados=escaneados_pulados
        )
    manifesto.registrar_evento(
        tipo="analise_aplicada",
        comando=comando,
        detalhe={
            **rotulo,
            "documentos_entrada": len(documentos_ordenados),
            "campos_total": len(registros_gravacao),
            "campos_ok": campos_ok,
            "campos_invalidados": campos_invalidados,
            "tentativas_usadas": usadas,
        },
    )
    return DesfechoAnalise("codificado", escaneados_pulados=escaneados_pulados)


def analisar_portal(
    contexto: ContextoAnalise,
    manifesto: Manifesto,
    codebook: Codebook,
    *,
    sistema: str,
    lote_id: int,
    tentativas: int,
    comando: str = "analise",
) -> ResumoAnalisePortal:
    """Analisa todos os Editais de UM portal (lote idempotente, AD-1).

    Editais já codificados NO LOTE são pulados (retomada); edital cujos
    documentos estão TODOS sob exclusão VIGENTE fica fora do lote e é
    contado como excluído. Exclusão PARCIAL segue no lote: o edital é
    processado com os documentos RESTANTES — a decisão de exclusão é por
    documento e o material excluído nunca entra na entrada. Cada edital
    concluído é uma transação própria — interrupção retoma exatamente dali.
    """
    rotulo = {"instituicao": contexto.instituicao_sigla, "portal": contexto.portal.nome}
    resumo = ResumoAnalisePortal(contexto.instituicao_sigla, contexto.portal.nome)

    documentos = manifesto.documentos_do_portal_para_analise(contexto.portal_id)
    por_edital: dict[str, list[sqlite3.Row]] = {}
    for documento in documentos:
        por_edital.setdefault(str(documento["edital_id"]), []).append(documento)

    codificados = manifesto.editais_codificados_no_lote(lote_id)
    for edital_id, docs in por_edital.items():
        resumo.editais += 1
        if edital_id in codificados:
            resumo.pulados += 1
            continue
        vigentes = [d for d in docs if not d["excluido_vigente"]]
        if not vigentes:
            resumo.excluidos += 1
            continue
        desfecho = analisar_edital(
            edital_id,
            vigentes,
            manifesto,
            codebook,
            sistema=sistema,
            lote_id=lote_id,
            tentativas=tentativas,
            comando=comando,
        )
        resumo.escaneados_sem_ocr += desfecho.escaneados_pulados
        if desfecho.desfecho == "codificado":
            resumo.codificados += 1
        elif desfecho.desfecho == "invalido":
            resumo.invalidos += 1
            resumo.editais_perdidos.append(edital_id)
        elif desfecho.desfecho == "erro":
            resumo.erros += 1
            resumo.editais_perdidos.append(edital_id)
        else:
            raise AssertionError(f"desfecho de análise desconhecido: {desfecho.desfecho!r}")

    manifesto.registrar_evento(
        tipo="analise_portal_concluida",
        comando=comando,
        detalhe={**rotulo, **resumo.totalizar()},
    )
    return resumo
