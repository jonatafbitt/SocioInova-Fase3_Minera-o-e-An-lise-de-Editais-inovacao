"""Datação multi-fonte (CAP-3) — OFFLINE: determina a Data de Publicação no
Portal consultando TODAS as fontes locais disponíveis e roteia não-datáveis
à Fila de Revisão Manual fundamentada.

Governado por:
- AD-11: aqui NÃO há INSERT em ``editais``/``documentos`` nem parseio de PDF
  fora do extrator único — o único toque no Documento é o UPDATE de
  ``metodo_datacao``/``ano_aceito`` VIA ``manifest.aplicar_datacao``; os
  metadados do PDF chegam SOMENTE por ``texto.ler_metadados``.
- FR-6 sem short-circuit: TODAS as fontes disponíveis são consultadas e cada
  uma rende linha em ``evidencias_datacao`` (fonte, valor bruto, localização)
  — url, âncora (capturada na descoberta, v5), docinfo e time_tag (re-busca
  HTTP da página de origem). A ordem da cascata é url → ancora → pdf_meta →
  time_tag; divergência vira sinal de qualidade no evento.
- Regras de aceite (janela 2019–2026): um único ano candidato é aceito se
  corroborado por ≥2 fontes OU produzido por fonte ≠ url; só-URL sem
  corroboração vai à fila (``baixa_confianca_sourl`` — regra que cobre
  também os anos-limite 2019/2026, que exigem corroboração interna); ≥2 anos
  distintos entre fontes vão à fila (``divergencia`` — humano resolve, sem
  voto); ano fora da janela NUNCA é aceito (fica como evidência bruta e, se
  for o caso único, motiva a fila). |S|=0 ⇒ fila ``sem_data`` — nada é
  descartado sem enfileirar.
- FR-8: item da fila preserva URL, portal, evidências coletadas e motivo;
  a decisão grava ano atribuído OU exclusão + justificativa OBRIGATÓRIA +
  autoria + data — via ``consulta.py fila decidir``.
- AD-1/AD-10: lote idempotente e retomável — documento é pulado quando
  ``metodo_datacao`` já está preenchido OU quando sua url já tem item na
  fila (pendente OU resolvida: a decisão humana nunca é refeta nem
  duplicada); falha pontual vira ``datacao_erro`` e o lote segue (exit 0).
  EXCEÇÃO à cobertura: documento cujo PDF está sumido/corrompido fica SEM
  destino nesta passada (erro de leitura) até o reparo dos bytes — a
  retomada re-tenta automaticamente na próxima execução.

Alias (``referencia_para``) é datado como linha própria: mesma mídia no
disco, evidências próprias pela sua url_origem. Corpus antigo sem
``texto_ancora``: a fonte simplesmente fica indisponível (a evidência não
nasce para ela) — o resumo/eventos continuam auditáveis pelas demais.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .coleta import _ANOS_JANELA, _RE_ANO, anos_janela_no_path
from .manifest import Manifesto
from .mapa import Portal
from .texto import ler_metadados

FONTE_ORDEM: tuple[str, ...] = ("url", "ancora", "pdf_meta", "time_tag")
MOTIVO_SEM_DATA = "sem_data"
MOTIVO_SO_URL = "baixa_confianca_sourl"
MOTIVO_DIVERGENCIA = "divergencia"

# Regex LIVRE para RELATÓRIO de anos fora da janela (ex.: 2027, 2018) — o
# aceite continua usando _RE_ANO/_ANOS_JANELA da coleta (mesma janela).
_RE_ANO_LIVRE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_LOCAL_URL = "documentos.url_origem"
_LOCAL_ANCORA = "candidatos.texto_ancora"
_LOCAL_DOCINFO = "docinfo"
_LOCAL_TIME_TAG = "pagina_origem"


def _anos_in_janela(texto: str) -> set[int]:
    """Anos 2019–2026 num texto livre (mesma regex da pasta provisória)."""
    return {int(ano) for ano in _RE_ANO.findall(texto)} if texto else set()


def _anos_da_url(url: str) -> tuple[set[int], set[int]]:
    """(dentro, fora) da janela para a fonte URL — SOMENTE o caminho.

    Mesma varredura por segmento do PATH que ``coleta.ano_provisorio_da_url``
    (função importada, sem duplicar regex — AD-4 a favor do fluxo): hostname,
    subdomínio e QUERY não são evidência de publicação
    (``edital2023.ifba.edu.br?ano=2021`` não produz ano nenhum).
    """
    dentro = anos_janela_no_path(url)
    fora: set[int] = set()
    for segmento in urlsplit(url).path.split("/"):
        fora |= {int(achou) for achou in _RE_ANO_LIVRE.findall(unquote(segmento))}
    return dentro, {ano for ano in fora if ano not in _ANOS_JANELA}


def _anos_fora_da_janela(texto: str) -> set[int]:
    """Anos 20XX FORA da janela presentes no texto — só para o relatório."""
    return {int(ano) for ano in _RE_ANO_LIVRE.findall(texto) if int(ano) not in _ANOS_JANELA}


def _ano_de_data_docinfo(bruto: str) -> int | None:
    """Ano de uma data de docinfo ('D:AAAAMMDD…' ou 'AAAA-MM-DD…').

    Datas PDF colam ano+mês+dia SEM separador — a regex com fronteira de
    dígito não serve aqui; os quatro PRIMEIROS dígitos são o ano. Fora da
    janela devolve None (o valor bruto segue como evidência).
    """
    quatro = _quatro_digitos_de_data(bruto)
    if quatro is None:
        return None
    ano = int(quatro)
    return ano if ano in _ANOS_JANELA else None


def _fora_da_janela_de_data(bruto: str) -> set[int]:
    """Ano de data docinfo FORA da janela — só para o relatório do evento."""
    quatro = _quatro_digitos_de_data(bruto)
    if quatro is None:
        return set()
    ano = int(quatro)
    return set() if ano in _ANOS_JANELA else {ano}


def _quatro_digitos_de_data(bruto: str) -> str | None:
    texto = bruto.strip()
    if texto.upper().startswith("D:"):
        texto = texto[2:].strip()
    quatro = texto[:4]
    return quatro if len(quatro) == 4 and quatro.isdigit() else None


def _extrair_time_tag_da_pagina(html: str) -> tuple[set[int], str]:
    """Extrai anos de tags <time> e classes CSS de data de uma página HTML.

    Procura por:
    1. Tags <time> com atributo datetime (formato ISO 8601)
    2. Tags <time> com texto contendo ano
    3. Elementos com classes CSS comuns de data (date, published, etc.)

    Retorna (anos_dentro_janela, valor_bruto_para_evidencia).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    anos: set[int] = set()
    evidencias: list[str] = []

    # 1. Tags <time> com atributo datetime
    for tag in soup.find_all("time"):
        datetime_attr = tag.get("datetime", "")
        if datetime_attr:
            # Extrair ano do datetime (ISO 8601: 2024-01-15 ou 2024-01-15T10:30:00)
            match = re.match(r"(\d{4})", str(datetime_attr))
            if match:
                ano = int(match.group(1))
                if ano in _ANOS_JANELA:
                    anos.add(ano)
                evidencias.append(f"time[datetime={datetime_attr}]")

        # 2. Texto da tag <time>
        texto_time = tag.get_text(strip=True)
        if texto_time:
            anos_no_texto = _anos_in_janela(texto_time)
            if anos_no_texto:
                anos |= anos_no_texto
                evidencias.append(f"time[text={texto_time}]")

    # 3. Classes CSS comuns de data
    classes_data = [
        "date", "published", "entry-date", "post-date", "article-date",
        "created", "modified", "updated", "datetime", "data-publicacao",
    ]
    for classe in classes_data:
        for elemento in soup.find_all(class_=re.compile(classe, re.I)):
            texto_elem = elemento.get_text(strip=True)
            if texto_elem:
                anos_elem = _anos_in_janela(texto_elem)
                if anos_elem:
                    anos |= anos_elem
                    evidencias.append(f"css(.{classe}={texto_elem[:50]})")

    valor_bruto = "; ".join(evidencias) if evidencias else ""
    return anos, valor_bruto


def _buscar_pagina_origem(url: str) -> str | None:
    """Busca a página de origem (não o PDF) para extrair metadados HTML.

    Tenta buscar a URL como página HTML. Se o conteúdo for PDF ou
    inacessível, retorna None.
    """
    try:
        import requests
        from requests.exceptions import RequestException

        headers = {
            "User-Agent": "agente-editais/0.1 (pesquisa academica PPGCS/UFBA)"
        }
        response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "pdf" in content_type.lower():
            return None

        return response.text
    except (RequestException, Exception):
        return None


def _avaliar(anos_por_fonte: dict[str, set[int]]) -> tuple[int | None, str | None, str | None]:
    """Matriz de decisão congelada (Design Notes da story).

    Devolve ``(ano_aceito, metodo, motivo_fila)``: motivo None ⇒ ACEITO com
    ``metodo`` = PRIMEIRA fonte na cascata (url→ancora→pdf_meta) cujo valor
    produziu o consenso (FR-7). Anos-limite 2019/2026 não têm regra extra
    além da geral: exigem corroboração interna ⇒ se a única produtora for a
    url, caem em ``baixa_confianca_sourl`` como qualquer só-URL.
    """
    conjunto: set[int] = set()
    for anos in anos_por_fonte.values():
        conjunto |= anos
    if len(conjunto) >= 2:
        return None, None, MOTIVO_DIVERGENCIA
    if not conjunto:
        return None, None, MOTIVO_SEM_DATA
    ano = conjunto.pop()
    produtoras = [f for f in FONTE_ORDEM if ano in anos_por_fonte.get(f, set())]
    if not produtoras or produtoras == ["url"]:
        # Guarda defensiva: ``produtoras`` vazia significa que uma fonte fora
        # de FONTE_ORDEM alimentou anos (extensão futura — time_tag,
        # wayback_snapshot — ainda não registrada na cascata): sem produtora
        # conhecida o ano é tratado como baixa confiança, nunca IndexError.
        # ``["url"]`` é a regra só-URL da story (inclui limites 2019/2026).
        return None, None, MOTIVO_SO_URL
    return ano, produtoras[0], None


@dataclass(slots=True)
class ContextoDatacao:
    """Portal resolvido contra o Manifesto para uma execução de datar."""

    instituicao_sigla: str
    portal: Portal
    portal_id: int


@dataclass(slots=True)
class ResumoDatacaoPortal:
    """Contagens da datação de UM portal — vira saída CLI e evento."""

    instituicao_sigla: str
    portal_nome: str
    documentos: int = 0
    aceitos: int = 0
    enfileirados: int = 0
    erros: int = 0
    pulados: int = 0
    urls_perdidas: list[str] = field(default_factory=list)

    def totalizar(self) -> dict:
        return {
            "documentos": self.documentos,
            "aceitos": self.aceitos,
            "enfileirados": self.enfileirados,
            "erros": self.erros,
            "pulados": self.pulados,
            "urls_perdidas": list(self.urls_perdidas),
        }


@dataclass(slots=True)
class DesfechoDatacao:
    """Resultado unitário de UM Documento — agrega no resumo do portal."""

    desfecho: str  # aceito|fila|erro|skip
    motivo: str | None = None
    ano: int | None = None
    metodo: str | None = None


def _texto_ancora_do_candidato(manifesto: Manifesto, url_origem: str) -> str | None:
    """Âncora persistida pela descoberta (v5); ausente/vazia ⇒ indisponível.

    Corpus descoberto ANTES da v5 não tem a coluna preenchida: a fonte
    simplesmente não nasce para ele (Design Notes) — nunca vira erro.
    """
    linhas = manifesto.consultar(
        "SELECT texto_ancora FROM candidatos WHERE url = ?", (url_origem,)
    )
    if not linhas:
        return None
    ancora = linhas[0]["texto_ancora"]
    if ancora is None or not str(ancora).strip():
        return None
    return str(ancora)


def datar_documento(
    documento: sqlite3.Row,
    manifesto: Manifesto,
    *,
    portal_id: int,
    comando: str = "datar",
) -> DesfechoDatacao:
    """Datá UM Documento: coleta fontes → evidências → aceite ou fila.

    Ordem deliberada (kill-safe, espelho de texto.extrair_documento): a
    decisão local de retomada fica NO CHAMADOR; aqui coleta tolerante →
    evidências idempotentes → transação única de aceite/fila → eventos. Um
    crash entre etapas deixa estado recuperável: sem aceite e sem item de
    fila, a próxima execução refaz o documento inteiro (evidência é
    substituída pela PK, nunca duplicada).
    """
    documento_id = documento["id"]
    url = documento["url_origem"]
    rotulo = {"documento_id": documento_id, "url": url}

    # -- coleta das fontes DISPONÍVEIS (FR-6: sem short-circuit) -------------
    evidencias: list[tuple[str, str, str]] = [("url", url, _LOCAL_URL)]
    anos_dentro_url, anos_fora_url = _anos_da_url(url)
    anos_por_fonte: dict[str, set[int]] = {"url": anos_dentro_url}
    fora_da_janela: set[int] = set(anos_fora_url)

    ancora = _texto_ancora_do_candidato(manifesto, url)
    if ancora is not None:
        evidencias.append(("ancora", ancora, _LOCAL_ANCORA))
        anos_por_fonte["ancora"] = _anos_in_janela(ancora)
        fora_da_janela |= _anos_fora_da_janela(ancora)

    pdf = Path(documento["caminho"])
    metadados = ler_metadados(pdf) if pdf.is_file() else None
    if metadados is None:
        # bytes inválidos/ausentes NA LEITURA DE METADADOS: erro isolado
        # (I/O matrix) — o lote segue e a retomada reprocessa este documento
        manifesto.registrar_evento(
            tipo="datacao_erro",
            comando=comando,
            detalhe={
                **rotulo,
                "fase": "leitura",
                "caminho": str(pdf),
                "existente": pdf.is_file(),
            },
        )
        return DesfechoDatacao("erro")

    valor_docinfo = json.dumps(metadados, ensure_ascii=False, sort_keys=True)
    evidencias.append(("pdf_meta", valor_docinfo, _LOCAL_DOCINFO))
    anos_pdf: set[int] = set()
    for campo in ("criado_em", "modificado_em"):
        bruto = metadados.get(campo)
        if bruto:
            ano_data = _ano_de_data_docinfo(bruto)
            if ano_data is not None:
                anos_pdf.add(ano_data)
            else:
                fora_da_janela |= _fora_da_janela_de_data(bruto)
    titulo = metadados.get("titulo")
    if titulo:
        anos_pdf |= _anos_in_janela(titulo)
        fora_da_janela |= _anos_fora_da_janela(titulo)
    anos_por_fonte["pdf_meta"] = anos_pdf

    # -- fonte <time>/CSS: re-busca HTTP da página de origem (híbrido) -------
    # Só busca se ainda não temos consenso (≥2 fontes com mesmo ano)
    # para evitar requisições desnecessárias.
    tem_consenso = len({ano for anos in anos_por_fonte.values() for ano in anos}) == 1 and sum(1 for anos in anos_por_fonte.values() if anos) >= 2
    if not tem_consenso:
        html_pagina = _buscar_pagina_origem(url)
        if html_pagina is not None:
            anos_time, valor_time = _extrair_time_tag_da_pagina(html_pagina)
            if anos_time:
                evidencias.append(("time_tag", valor_time, _LOCAL_TIME_TAG))
                anos_por_fonte["time_tag"] = anos_time
                fora_da_janela |= _anos_fora_da_janela(valor_time)

    # -- decisão + gravação ---------------------------------------------------
    ano, metodo, motivo = _avaliar(anos_por_fonte)
    manifesto.registrar_evidencias(documento_id, url, evidencias)

    if motivo is None:
        persistiu = manifesto.aplicar_datacao(documento_id, url, metodo=metodo, ano=ano)
        if not persistiu:
            manifesto.registrar_evento(
                tipo="datacao_erro",
                comando=comando,
                detalhe={**rotulo, "fase": "persistencia"},
            )
            return DesfechoDatacao("erro")
        manifesto.registrar_evento(
            tipo="datacao_aplicada",
            comando=comando,
            detalhe={
                **rotulo,
                "ano_aceito": ano,
                "metodo": metodo,
                "anos_por_fonte": {
                    fonte: sorted(anos) for fonte, anos in anos_por_fonte.items() if anos
                },
                "fontes_consultadas": [fonte for fonte, *_ in evidencias],
                "total_evidencias": len(evidencias),
            },
        )
        return DesfechoDatacao("aceito", ano=ano, metodo=metodo)

    inserido = manifesto.enfileirar(url, portal_id, motivo)
    if not inserido:
        # False só quando já havia item pendente para a url que o pulo não viu
        # (corrida/estado divergente): NÃO é sucesso mudo nem conta como fila —
        # vira erro isolado e a retomada reprocessa com a fila já visível.
        manifesto.registrar_evento(
            tipo="datacao_erro",
            comando=comando,
            detalhe={**rotulo, "fase": "fila_duplicada"},
        )
        return DesfechoDatacao("erro")
    detalhe_fila = {
        **rotulo,
        "motivo": motivo,
        "anos_encontrados": sorted({ano_ for anos in anos_por_fonte.values() for ano_ in anos}),
        "fontes_consultadas": [fonte for fonte, *_ in evidencias],
        "total_evidencias": len(evidencias),
        "divergencia": motivo == MOTIVO_DIVERGENCIA,
    }
    if fora_da_janela:
        # sinal de qualidade: anos fora da janela viram EVIDÊNCIA, jamais aceite
        detalhe_fila["anos_fora_da_janela"] = sorted(fora_da_janela)
    manifesto.registrar_evento(tipo="datacao_fila", comando=comando, detalhe=detalhe_fila)
    return DesfechoDatacao("fila", motivo=motivo)


def _acumular(resumo: ResumoDatacaoPortal, desfecho: DesfechoDatacao, url: str) -> None:
    tipo = desfecho.desfecho
    if tipo == "aceito":
        resumo.aceitos += 1
    elif tipo == "fila":
        resumo.enfileirados += 1
    elif tipo == "erro":
        resumo.erros += 1
        resumo.urls_perdidas.append(url)
    elif tipo == "skip":
        resumo.pulados += 1
    else:
        raise AssertionError(
            f"desfecho de datação desconhecido: {tipo!r} "
            "(todo desfecho novo precisa de uma entrada em _acumular)"
        )


def ja_datado(documento: sqlite3.Row, manifesto: Manifesto) -> bool:
    """True se o Documento já tem destino de datação (retomada AD-1).

    Pula quem tem ``metodo_datacao`` preenchido (aceite próprio OU decisão
    humana propagável pela fila) E quem já tem QUALQUER item na fila para a
    url — pendente OU resolvido: reprocessar resolveria de novo uma decisão
    humana e criaria itens duplicados, violando o AC "zero re-decisões e a
    fila permanece intacta".
    """
    if documento["metodo_datacao"] is not None:
        return True
    return manifesto.tem_item_de_fila(documento["url_origem"])


def datar_portal(
    contexto: ContextoDatacao,
    manifesto: Manifesto,
    *,
    comando: str = "datar",
) -> ResumoDatacaoPortal:
    """Data todos os Documentos de UM portal (lote idempotente, AD-1).

    Itera ``documentos`` ligados ao portal em ordem estável; cada documento
    concluído é uma transação própria — interrupção retoma exatamente dali.
    100% OFFLINE: nenhuma rede, nenhuma janela off-peak. Cada documento
    termina COM ``metodo_datacao``+evidências OU com item ativo na fila —
    nenhum fica sem destino (AC da story), EXCETO o de leitura falhada
    (PDF sumido/corrompido): sem destino até o reparo, re-tentado na
    próxima execução.
    """
    rotulo = {"instituicao": contexto.instituicao_sigla, "portal": contexto.portal.nome}
    resumo = ResumoDatacaoPortal(contexto.instituicao_sigla, contexto.portal.nome)

    documentos = manifesto.documentos_do_portal(contexto.portal_id)
    for documento in documentos:
        resumo.documentos += 1
        url = documento["url_origem"]
        if ja_datado(documento, manifesto):
            resumo.pulados += 1
            continue
        desfecho = datar_documento(
            documento, manifesto, portal_id=contexto.portal_id, comando=comando
        )
        _acumular(resumo, desfecho, url)

    manifesto.registrar_evento(
        tipo="datacao_portal_concluida",
        comando=comando,
        detalhe={**rotulo, **resumo.totalizar()},
    )
    return resumo
