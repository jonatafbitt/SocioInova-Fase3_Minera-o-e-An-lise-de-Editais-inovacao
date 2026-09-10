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
import importlib
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .manifest import Manifesto
from .mapa import Portal

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


_CAMPOS_DOCINFO: tuple[tuple[str, str], ...] = (
    ("titulo", "/Title"),
    ("criado_em", "/CreationDate"),
    ("modificado_em", "/ModDate"),
)


def ler_metadados(caminho: str | Path) -> dict[str, str | None] | None:
    """Lê o docinfo do PDF — ÚNICA porta de metadados fora do extrator (AD-11).

    A datação (Story 5) importa ESTA função e nunca o pypdf diretamente.
    Tolerante por contrato: devolve ``None`` quando os bytes NÃO podem ser
    lidos como PDF (arquivo ausente ou corrompido) — o chamador trata como
    erro isolado; devolve dict com valores possivelmente ``None`` quando o
    PDF é válido mas o docinfo não traz o campo. Exceções JAMAIS propagam.
    """
    try:
        leitor = PdfReader(str(caminho))
        informacoes = leitor.metadata
    except Exception:
        return None
    metadados: dict[str, str | None] = {}
    for campo, chave in _CAMPOS_DOCINFO:
        try:
            bruto = informacoes.get(chave) if informacoes is not None else None
        except Exception:
            bruto = None
        metadados[campo] = str(bruto) if bruto else None
    return metadados


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
    except Exception as exc:
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
        except Exception:
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
    urls_restritas: set[str] | None = None,
) -> ResumoTextoPortal:
    """Textura os Documentos de UM portal (lote idempotente, AD-1).

    Itera ``documentos`` ligados ao portal em ordem estável; cada documento
    concluído é uma transação própria — interrupção retoma exatamente dali.
    Documentos já extraídos com hash vigente são pulados sem RE-PARSEAR nem
    re-baixar — o custo da retomada é re-hashear os bytes locais de cada
    candidato para verificar a vigência (leitura + SHA-256; critério congelado
    da spec). Nota: registros-alias (``referencia_para``) têm o MESMO
    MESMO ``caminho``/hash do canônico e são processados como linhas próprias
    — o custo é re-parsear PDF raro (dedupe intra-portal); o ``.txt`` irmão é
    um só e a regravação produz conteúdo idêntico. ``urls_restritas`` limita
    o lote a um recorte de URLs (ex.: ``--varredura``) — as contagens do
    resumo refletem SOMENTE o recorte.
    """
    rotulo = {"instituicao": contexto.instituicao_sigla, "portal": contexto.portal.nome}
    resumo = ResumoTextoPortal(contexto.instituicao_sigla, contexto.portal.nome)

    documentos = manifesto.documentos_do_portal(contexto.portal_id)
    if urls_restritas is not None:
        documentos = [d for d in documentos if d["url_origem"] in urls_restritas]
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


# -- resgate por OCR (Fase 3.1) ------------------------------------------------
#
# Estágio OPCIONAL e NÃO automático (decisão de design): NENHUM pipeline
# dispara OCR — só o comando explícito ``ocrescer``. O texto de um PDF
# escaneado entra no corpus quando um operador pede UM portal/lote. Retomável:
# ``ocr_em`` preenchido ⇒ pulado no ciclo seguinte; o ``.txt`` irmão (AD-2:
# artefato derivado) é sobrescrito com o texto óptico e a ``flag_escaneado`` é
# zerada APENAS quando o resgate valida (limiar de confiança por página).
# Engine: ``pypdfium2`` (render sem binário de sistema) + ``pytesseract``
# (binário Tesseract + pack de idioma instalados fora do projeto) + Pillow.
# As importações são opcionais — sem o extra 'ocr', o guard recusa com exit 2.


class ErroOcrIndisponivel(RuntimeError):
    """OCR indisponível: extra 'ocr' ausente OU binário tesseract/idioma."""


def _importar_ocr() -> tuple[Any, Any, Any]:
    """Importa o empilhamento OCR (tesseract, pdfium, PIL) — recusa com exit 2.

    ``pytesseract`` é um wrapper fino sobre o binário Tesseract; ``pypdfium2``
    renderiza páginas PDF→imagem SEM binários de sistema (roda no Windows e no
    CI); Pillow é a imagem em memória. Faltou qualquer um ⇒ extra 'ocr' não
    sincronizado (``uv sync --extra ocr``).
    """
    try:
        pytesseract = importlib.import_module("pytesseract")
        pdfium = importlib.import_module("pypdfium2")
        pil = importlib.import_module("PIL")
    except Exception as exc:
        raise ErroOcrIndisponivel(
            "OCR indisponível: faltam dependências do extra opcional "
            "'ocr' (pytesseract + pypdfium2 + Pillow). Rode "
            "`uv sync --extra ocr` antes de ocrescer."
        ) from exc
    return pytesseract, pdfium, pil


def ocr_disponivel(idioma: str = "por") -> bool:
    """True só se o empilhamento importa E o binário Tesseract responde E o
    idioma está instalado. Chamado pelo guard do CLI UMA vez por lote."""
    try:
        pytesseract, _pdfium, _pil = _importar_ocr()
    except ErroOcrIndisponivel:
        return False
    try:
        return idioma in pytesseract.get_languages(config="")
    except Exception:
        return False


def _renderizar_paginas(pdfium: Any, pil: Any, caminho_pdf: Path, escala: float = 2.0) -> list[Any]:
    """Renderiza TODAS as páginas do PDF como PIL RGB (ppi = 72 × escala)."""
    doc = pdfium.PdfDocument(str(caminho_pdf))
    try:
        paginas = []
        for _indice in range(len(doc)):
            imagem = doc[_indice].render(scale=escala).to_pil()
            paginas.append(imagem.convert("RGB"))
        return paginas
    finally:
        doc.close()


def _ocr_uma_pagina(pytesseract: Any, imagem: Any, idioma: str) -> tuple[str, float]:
    """Texto + confiança MÉDIA das palavras da página (0..100).

    Palavras sem confiança (conf < 0) são descartadas da média e do texto —
    o Tesseract sinaliza com -1 palavras que não pertencem a nenhuma linha.
    """
    dados = pytesseract.image_to_data(
        imagem, lang=idioma, config="--psm 3", output_type=pytesseract.Output.DICT
    )
    palavras: list[str] = []
    confiancas: list[float] = []
    textos = dados.get("text") or []
    confs = dados.get("conf") or []
    for texto, conf in zip(textos, confs):
        if not isinstance(texto, str) or not texto.strip():
            continue
        try:
            valor = int(conf)
        except (TypeError, ValueError):
            continue
        if valor < 0:
            continue
        palavras.append(texto)
        confiancas.append(float(valor))
    media = sum(confiancas) / len(confiancas) if confiancas else 0.0
    return " ".join(palavras), media


def resgatar_ocr_documento(
    documento: sqlite3.Row,
    manifesto: Manifesto,
    *,
    confianca_minima: int,
    idioma: str,
    tentativas: int = 1,
    comando: str = "ocrescer",
) -> str:
    """OCR de UM Documento escaneado: ``.txt`` irmão + proveniência v11.

    Desfechos (retomáveis como todo estágio):
    - ``pulado``: documento já resgatado (``ocr_em`` preenchido) ou NÃO
      escaneado — nada muda.
    - ``erro``: PDF ausente, render falhou, gravação falhou ou UPDATE não
      casou — evento ``texto_ocr_falhou`` e o lote segue.
    - ``falhou``: todas as páginas ficaram abaixo do limiar de confiança —
      flag permanece e o documento continua candidato (decisão explícita).
    - ``resgatado``: texto óptico gravado, flag zerada e ``ocr_em`` carimbado.
    """
    documento_id = documento["id"]
    url = documento["url_origem"]
    rotulo = {"documento_id": documento_id, "url": url}
    if documento["ocr_em"]:
        # retomada idempotente: já resgatado NUNCA reprocessa (AD-1/AD-3)
        return "pulado"
    pdf = Path(documento["caminho"])
    pdfium = importlib.import_module("pypdfium2")
    pil = importlib.import_module("PIL")
    pytesseract = importlib.import_module("pytesseract")

    if not pdf.is_file():
        manifesto.registrar_evento(
            tipo="texto_ocr_falhou",
            comando=comando,
            detalhe={**rotulo, "fase": "arquivo_ausente", "caminho": str(pdf)},
        )
        return "erro"

    try:
        paginas_imagem = _renderizar_paginas(pdfium, pil, pdf)
    except Exception as exc:
        manifesto.registrar_evento(
            tipo="texto_ocr_falhou",
            comando=comando,
            detalhe={**rotulo, "fase": "renderizacao", "erro": f"{type(exc).__name__}: {exc}"},
        )
        return "erro"

    paginas_texto: list[str] = []
    confiancas: list[float] = []
    for imagem in paginas_imagem:
        try:
            texto_pagina, conf = _ocr_uma_pagina(pytesseract, imagem, idioma)
        except Exception:
            continue
        if texto_pagina.strip() and conf >= confianca_minima:
            paginas_texto.append(texto_pagina)
            confiancas.append(conf)

    if not paginas_texto:
        manifesto.registrar_evento(
            tipo="texto_ocr_falhou",
            comando=comando,
            detalhe={**rotulo, "fase": "confianca",
                     "confianca_minima": confianca_minima, "idioma": idioma},
        )
        return "falhou"

    texto = "\n".join(paginas_texto)
    texto = texto.encode("utf-8", errors="replace").decode("utf-8")
    chars = sum(len(p) for p in paginas_texto)
    txt = caminho_txt_do(pdf)

    try:
        temporario = txt.with_name(f"{txt.name}.ocr-tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            temporario.write_bytes(texto.encode("utf-8"))
            os.replace(temporario, txt)
        finally:
            temporario.unlink(missing_ok=True)
    except OSError as exc:
        manifesto.registrar_evento(
            tipo="texto_ocr_falhou",
            comando=comando,
            detalhe={**rotulo, "fase": "gravacao_txt", "erro": f"{type(exc).__name__}: {exc}"},
        )
        return "erro"

    persistiu = manifesto.registrar_texto_extraido(
        documento_id,
        url,
        texto_caminho=str(txt),
        texto_chars=chars,
        texto_paginas=len(paginas_texto),
        flag_escaneado=False,
    )
    if not persistiu:
        manifesto.registrar_evento(
            tipo="texto_ocr_falhou",
            comando=comando,
            detalhe={**rotulo, "fase": "persistencia"},
        )
        return "erro"
    confianca_media = round(sum(confiancas) / len(confiancas), 1)
    ocr_ok = manifesto.registrar_texto_ocr_proveniencia(
        documento_id,
        url,
        metodo="pypdfium2+tesseract",
        confianca_media=confianca_media,
        paginas_resgatadas=len(paginas_texto),
        tentativas=tentativas,
    )
    if not ocr_ok:
        return "erro"
    manifesto.registrar_evento(
        tipo="texto_ocr_aplicado",
        comando=comando,
        detalhe={
            **rotulo,
            "txt": str(txt),
            "chars": chars,
            "paginas": len(paginas_texto),
            "confianca_media": confianca_media,
            "confianca_minima": confianca_minima,
            "idioma": idioma,
            "metodo": "pypdfium2+tesseract",
        },
    )
    return "resgatado"


@dataclass(slots=True)
class ResumoOcrPortal:
    """Contagens do resgate OCR de UM portal — vira saída CLI e evento."""

    instituicao_sigla: str
    portal_nome: str
    escaneados: int = 0
    resgatados: int = 0
    falhas: int = 0
    pulados: int = 0
    urls_falhas: list[str] = field(default_factory=list)

    def totalizar(self) -> dict:
        return {
            "escaneados": self.escaneados,
            "resgatados": self.resgatados,
            "falhas": self.falhas,
            "pulados": self.pulados,
            "urls_falhas": list(self.urls_falhas),
        }


def ocrescer_portal(
    contexto: ContextoTexto,
    manifesto: Manifesto,
    *,
    confianca_minima: int,
    idioma: str,
    tentativas: int = 1,
    comando: str = "ocrescer",
) -> ResumoOcrPortal:
    """OCR dos Documentos ESCANEADOS de UM portal (lote idempotente, AD-1).

    Só documentos com ``flag_escaneado`` entram no lote — texto já extraível
    NUNCA volta (AD-11: o extrator único continua sendo o pypdf; OCR é resgate
    exclusivo do que o pypdf não conseguiu). Já resgatados (``ocr_em``) pulam.
    """
    resumo = ResumoOcrPortal(contexto.instituicao_sigla, contexto.portal.nome)
    for documento in manifesto.documentos_do_portal(contexto.portal_id):
        url = documento["url_origem"]
        if not documento["flag_escaneado"]:
            resumo.pulados += 1
            continue
        resumo.escaneados += 1
        resultado = resgatar_ocr_documento(
            documento,
            manifesto,
            confianca_minima=confianca_minima,
            idioma=idioma,
            tentativas=tentativas,
            comando=comando,
        )
        if resultado == "resgatado":
            resumo.resgatados += 1
        elif resultado in ("erro", "falhou"):
            resumo.falhas += 1
            resumo.urls_falhas.append(url)
        else:
            resumo.pulados += 1
    manifesto.registrar_evento(
        tipo="ocr_portal_concluida",
        comando=comando,
        detalhe={
            "instituicao": contexto.instituicao_sigla,
            "portal": contexto.portal.nome,
            **resumo.totalizar(),
        },
    )
    return resumo
