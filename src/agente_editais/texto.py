"""Texto (CAP-6) — EXTRATOR ÚNICO do sistema (AD-11): um ``.txt`` irmão por
Documento, flag de escaneado pelo limiar configurável e proveniência no
Manifesto.

Governado por:
- AD-11: este é o ÚNICO extrator de texto do sistema — a datação (Story 5) e
  o verificador de citações do L2 consomem SUA saída; nenhum outro módulo
  parseia PDF. Aqui não há INSERT em ``documentos``: só UPDATE das colunas de
  proveniência (v4) e da ``flag_escaneado`` nascida NULL na v3, VIA
  ``manifest.registrar_texto_extraido``.
- AD-2: o PDF original fica INTACTO — o ``.txt`` é artefato DERIVADO novo
  (bytes novos não violam write-once). Gravação atômica: temporário ao lado
  do destino + ``os.replace``; um crash nunca deixa meio-txt no caminho
  canônico sem que a transação de proveniência também tenha abortado.
- AD-1/AD-3: operação retomável e idempotente sobre o Manifesto — documento
  com ``extraido_em`` preenchido E hash dos bytes locais vigente E ``.txt``
  presente é PULADO; documento novo ou com bytes alterados (hash diverge) é
  re-extraído (nova versão ⇒ novo txt).
- AD-9: limiar vem de ``politeness.toml`` ([texto]
  ``limiar_chars_por_pagina``, default 100) — nada hard-coded.
- AD-10: cada desfecho vira evento append-only (``texto_extraido`` /
  ``texto_escaneado`` / ``arquivo_ausente`` / ``texto_erro``); falha pontual
  NUNCA aborta o lote.

Regra da flag (FR-16/CAP-6): ``flag_escaneado=true`` quando os caracteres
EXTRAÍDOS (sem os separadores entre páginas) < ``limiar × nº de páginas``.
Antes de contar e gravar, o texto é sanitizado: surrogates não pareados do
pypdf são trocados pelo marcador do encode (``encode(errors="replace")``) —
sem isso a gravação levantaria ``UnicodeEncodeError`` (ValueError) e
abortaria o lote inteiro. Tolerância por página (Design Notes): página cujo
parsing lança exceção rende string vazia sem matar o documento; se TODAS as
páginas falharem ⇒ tratado como candidato a OCR: flag true com
``falha_parsing_total`` no evento para decisão futura. OCR automático NÃO
existe nesta story — aqui só a sinalização.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from .mapa import Portal
from .manifest import Manifesto

_EXTENSAO_TXT = ".txt"
_CHUNK_LEITURA = 1024 * 1024


def _hash_arquivo(caminho: Path) -> str:
    """SHA-256 de arquivo em disco, em chunks (vigência do hash na retomada)."""
    hasher = hashlib.sha256()
    with caminho.open("rb") as entrada:
        for pedaco in iter(lambda: entrada.read(_CHUNK_LEITURA), b""):
            hasher.update(pedaco)
    return hasher.hexdigest()


@dataclass(slots=True)
class ContextoTexto:
    """Portal resolvido contra o Manifesto para uma execução de textuar."""

    instituicao_sigla: str
    portal: Portal
    portal_id: int


@dataclass(slots=True)
class ResumoTextoPortal:
    """Contagens da texturação de UM portal — vira saída CLI e evento."""

    instituicao_sigla: str
    portal_nome: str
    documentos: int = 0
    extraidos: int = 0
    escaneados: int = 0
    erros: int = 0
    pulados: int = 0
    urls_perdidas: list[str] = field(default_factory=list)

    def totalizar(self) -> dict:
        return {
            "documentos": self.documentos,
            "extraidos": self.extraidos,
            "escaneados": self.escaneados,
            "erros": self.erros,
            "pulados": self.pulados,
            "urls_perdidas": list(self.urls_perdidas),
        }


@dataclass(slots=True)
class DesfechoTexto:
    """Resultado unitário de UM Documento — agrega no resumo do portal."""

    desfecho: str  # extraido|escaneado|erro|arquivo_ausente|skip


def caminho_txt_do(pdf: Path) -> Path:
    """Irmão do PDF: mesmo nome/caminho com extensão ``.txt`` (CAP-6)."""
    return pdf.with_suffix(_EXTENSAO_TXT)


def ja_extraido(documento: sqlite3.Row) -> bool:
    """True se o Documento já tem texto com hash vigente (retomada AD-1).

    Trava tripla: ``extraido_em`` preenchido E o ``.txt`` registrado existe E
    os bytes do PDF em disco ainda batem no ``hash_sha256`` do L1 — bytes
    alterados (ou sumidos) derrubam a vigência e forçam re-extração.
    """
    if documento["extraido_em"] is None or documento["texto_caminho"] is None:
        return False
    pdf = Path(documento["caminho"])
    txt = Path(documento["texto_caminho"])
    try:
        if not pdf.is_file() or not txt.is_file():
            return False
        return _hash_arquivo(pdf) == documento["hash_sha256"]
    except OSError:
        return False


def _texto_da_pagina(pagina) -> str:
    """Extração DE UMA página; chamador tolera exceção (Design Notes)."""
    return pagina.extract_text() or ""


def extrair_documento(
    documento: sqlite3.Row,
    manifesto: Manifesto,
    limiar_chars_por_pagina: int,
    *,
    comando: str = "textuar",
) -> DesfechoTexto:
    """Extrai UM Documento: ``.txt`` irmão + métricas + flag + proveniência.

    Ordem deliberada (kill-safe): decisão local de retomada fica NO CHAMADOR;
    aqui leitura tolerante → gravação atômica do txt → transação única de
    UPDATE → evento. Um crash antes do UPDATE deixa estado recuperável: o
    txt órfão é sobrescrito na próxima execução, e a linha continua NULL
    (re-extração limpa).
    """
    documento_id = documento["id"]
    url = documento["url_origem"]
    rotulo = {"documento_id": documento_id, "url": url}
    pdf = Path(documento["caminho"])

    if not pdf.is_file():
        manifesto.registrar_evento(
            tipo="arquivo_ausente",
            comando=comando,
            detalhe={**rotulo, "caminho": str(pdf)},
        )
        return DesfechoTexto("arquivo_ausente")

    try:
        leitor = PdfReader(str(pdf))
        paginas = list(leitor.pages)
    except Exception as exc:  # noqa: BLE001 — PDF inválido é desfecho, não crash
        manifesto.registrar_evento(
            tipo="texto_erro",
            comando=comando,
            detalhe={**rotulo, "fase": "abertura_pdf", "erro": f"{type(exc).__name__}: {exc}"},
        )
        return DesfechoTexto("erro")

    pedacos: list[str] = []
    falhas_parse = 0
    for pagina in paginas:
        try:
            pedacos.append(_texto_da_pagina(pagina))
        except Exception:  # noqa: BLE001 — página problemática rende vazio
            pedacos.append("")
            falhas_parse += 1
    texto = "\n".join(pedacos)
    # Sanitiza ANTES de contar e gravar: surrogate não pareado do pypdf
    # levantaria UnicodeEncodeError no encode da gravação (ValueError, fora do
    # except OSError abaixo) e abortaria o lote inteiro. O replace do encode é
    # 1:1 por code point, então chars reflete exatamente os bytes gravados.
    texto = texto.encode("utf-8", errors="replace").decode("utf-8")
    # Caracteres EXTRAÍDOS, sem os separadores do join: a regra da flag é
    # estritamente sobre o conteúdo extraído (limiar × páginas).
    chars = sum(len(p) for p in pedacos)
    numero_paginas = len(paginas)
    # PDF sem páginas não tem base de comparação: trata como 1 página (sem
    # texto extraível ⇒ candidato a OCR, coerente com a regra do limiar).
    divisor = max(numero_paginas, 1)
    falha_parsing_total = bool(paginas) and falhas_parse == numero_paginas
    flag_escaneado = falha_parsing_total or chars < limiar_chars_por_pagina * divisor
    txt = caminho_txt_do(pdf)

    try:
        temporario = txt.with_name(f"{txt.name}.texto-tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            temporario.write_bytes(texto.encode("utf-8"))
            os.replace(temporario, txt)
        finally:
            temporario.unlink(missing_ok=True)
    except OSError as exc:
        manifesto.registrar_evento(
            tipo="texto_erro",
            comando=comando,
            detalhe={**rotulo, "fase": "gravacao_txt", "erro": f"{type(exc).__name__}: {exc}"},
        )
        return DesfechoTexto("erro")

    persistiu = manifesto.registrar_texto_extraido(
        documento_id,
        url,
        texto_caminho=str(txt),
        texto_chars=chars,
        texto_paginas=numero_paginas,
        flag_escaneado=flag_escaneado,
    )
    if not persistiu:
        # UPDATE sem linha casando ⇒ nada de evento de sucesso sem
        # proveniência: vira erro isolado; o .txt órfão é sobrescrito na
        # próxima execução (comportamento documentado no docstring).
        manifesto.registrar_evento(
            tipo="texto_erro",
            comando=comando,
            detalhe={**rotulo, "fase": "persistencia"},
        )
        return DesfechoTexto("erro")
    detalhe = {
        **rotulo,
        "txt": str(txt),
        "chars": chars,
        "paginas": numero_paginas,
        "limiar_chars_por_pagina": limiar_chars_por_pagina,
        "paginas_com_falha_de_parsing": falhas_parse,
    }
    if flag_escaneado:
        detalhe["falha_parsing_total"] = falha_parsing_total
        manifesto.registrar_evento(tipo="texto_escaneado", comando=comando, detalhe=detalhe)
    else:
        manifesto.registrar_evento(tipo="texto_extraido", comando=comando, detalhe=detalhe)
    return DesfechoTexto("escaneado" if flag_escaneado else "extraido")


def _acumular(resumo: ResumoTextoPortal, desfecho: DesfechoTexto, url: str) -> None:
    tipo = desfecho.desfecho
    if tipo == "extraido":
        resumo.extraidos += 1
    elif tipo == "escaneado":
        resumo.escaneados += 1
    elif tipo in ("erro", "arquivo_ausente"):
        resumo.erros += 1
        resumo.urls_perdidas.append(url)
    elif tipo == "skip":
        resumo.pulados += 1
    else:
        raise AssertionError(
            f"desfecho de texto desconhecido: {tipo!r} "
            "(todo desfecho novo precisa de uma entrada em _acumular)"
        )


def textuar_portal(
    contexto: ContextoTexto,
    manifesto: Manifesto,
    limiar_chars_por_pagina: int,
    *,
    comando: str = "textuar",
) -> ResumoTextoPortal:
    """Textura todos os Documentos de UM portal (lote idempotente, AD-1).

    Itera ``documentos`` ligados ao portal em ordem estável; cada documento
    concluído é uma transação própria — interrupção retoma exatamente dali.
    Documentos já extraídos com hash vigente são pulados sem RE-PARSEAR nem
    re-baixar — o custo da retomada é re-hashear os bytes locais de cada
    candidato para verificar a vigência (leitura + SHA-256; critério congelado
    da spec). Nota: registros-alias (``referencia_para``) têm o MESMO
    MESMO ``caminho``/hash do canônico e são processados como linhas próprias
    — o custo é re-parsear PDF raro (dedupe intra-portal); o ``.txt`` irmão é
    um só e a regravação produz conteúdo idêntico.
    """
    rotulo = {"instituicao": contexto.instituicao_sigla, "portal": contexto.portal.nome}
    resumo = ResumoTextoPortal(contexto.instituicao_sigla, contexto.portal.nome)

    documentos = manifesto.documentos_do_portal(contexto.portal_id)
    for documento in documentos:
        resumo.documentos += 1
        url = documento["url_origem"]
        if ja_extraido(documento):
            resumo.pulados += 1
            continue
        desfecho = extrair_documento(
            documento, manifesto, limiar_chars_por_pagina, comando=comando
        )
        _acumular(resumo, desfecho, url)

    manifesto.registrar_evento(
        tipo="texto_portal_concluida",
        comando=comando,
        detalhe={**rotulo, **resumo.totalizar()},
    )
    return resumo
