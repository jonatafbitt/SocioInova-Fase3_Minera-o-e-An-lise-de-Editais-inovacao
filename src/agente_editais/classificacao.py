"""Classificação automática do tipo de edital (veredito ADVISORY, sem LLM).

Decide, por documento do portal, ``tipo_edital ∈ {inovacao, nao_inovacao,
sem_texto}`` com base em sinais textuais de ``configs/sinais_inovacao.yaml``
e atribui dimensões a partir de ``configs/sinais_analiticos.yaml`` (Quadro
1.6 — as DIMENSÕES do corpus já aspirado).

Decisões da story:

- Veredito ADVISORY: a classificação NUNCA exclui/oculta documento; quem
  exclui é a curadoria, pela fila de revisão (``excluido_vigente`` aqui
  simplesmente sai da população).
- Matching por token inteiro, insensível a caixa E acento (AD-9 e mesmo
  ``_sem_acento``/``contem_palavra_chave`` da descoberta — FR-3).
- Semântica por método (matriz I/O):
  * texto extraído presente → sinal no texto OU na âncora ⇒ ``inovacao``;
    senão ⇒ ``nao_inovacao``. O ``metodo`` registra a fonte que DECIDIU:
    ``'texto'`` ou ``'ancora'``.
  * sem texto → sinal na âncora ⇒ ``inovacao`` (metodo ``'ancora'``);
    senão ⇒ ``sem_texto`` (metodo ``'sem_texto'``). Nunca ``nao_inovacao``
    sem leitura de texto.
- Never da story: re-classificação só preenche quando VAZIO — um resultado
  com ``metodo in ('texto','ancora')`` nunca é sobrescrito ("já classificado");
  ``sem_texto`` pode virar veredito definitivo depois.
- Escrita exclusiva via ``manifest.registrar_*`` (AD-3); falhas por documento
  viram eventos e o lote segue (padrão AD-1/AD-10 do restante do agente).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from .descoberta import ContextoPortal, _sem_acento
from .manifest import Manifesto


class ErroConfigSinais(ValueError):
    """Config de sinais/dimensões inválida (recusa exit 2, config declarativa)."""


class ErroClassificacao(RuntimeError):
    """Problema pontual da classificação (ex.: URL excluída da curadoria)."""


@dataclass(slots=True)
class SinaisInovacao:
    """Sinais carregados + padrões compilados + dimensões atingidas por sinal.

    ``sinais`` preserva a ORDEM do YAML (ordem de reporte/dedupe); os padrões
    casam sobre o texto normalizado (caixa/acento removidos) por token inteiro.
    As dimensões referem-se a ``id`` das dimensões de sinais_analiticos.yaml.
    """

    sinais: tuple[str, ...]
    padroes: tuple[re.Pattern[str], ...]
    sinal_para_dimensoes: dict[str, tuple[str, ...]]

    def casar(self, texto_normalizado: str) -> tuple[list[str], list[str]]:
        """Sinais casados e dimensões atingidas, na ordem da config, sem dupla."""
        casados: list[str] = []
        dimensoes: list[str] = []
        for sinal, padrao in zip(self.sinais, self.padroes, strict=True):
            if padrao.search(texto_normalizado):
                casados.append(sinal)
                for dimensao in self.sinal_para_dimensoes.get(sinal, ()):
                    if dimensao not in dimensoes:
                        dimensoes.append(dimensao)
        return casados, dimensoes


def _normalizar(texto: str) -> str:
    """Collapse + remoção de acento/caixa — base de todo o casamento."""
    return " ".join(_sem_acento(texto).split())


def carregar_sinais(
    caminho_sinais: Path, caminho_dimensoes: Path
) -> SinaisInovacao:
    """Carrega e valida os sinais de inovação e o mapa sinal→dimensões.

    Levanta ``ErroConfigSinais`` se algum arquivo estiver ausente/malformado
    ou com ``schema_version != 1`` (config declarativa inválida → exit 2).
    """
    problemas: list[str] = []

    def ler(arquivo: Path) -> dict:
        if not arquivo.exists():
            problemas.append(f"arquivo ausente: {arquivo.name}")
            return {}
        try:
            bruto = arquivo.read_text(encoding="utf-8")
            return dict(yaml.safe_load(bruto) or {})
        except (OSError, UnicodeDecodeError, yaml.YAMLError, TypeError, ValueError) as exc:
            problemas.append(f"{arquivo.name}: YAML inválido ({exc})")
            return {}

    dados_sinais = ler(caminho_sinais)
    dados_dimensoes = ler(caminho_dimensoes)
    if problemas:
        raise ErroConfigSinais("; ".join(problemas))

    if dados_sinais.get("schema_version") != 1:
        problemas.append(
            f"{caminho_sinais.name}: schema_version != 1 "
            "(versionar mudanças de config é exigência AD-9)"
        )
    lista_sinais = dados_sinais.get("sinais")
    if not isinstance(lista_sinais, list) or not all(
        isinstance(sinal, str) and sinal.strip() for sinal in lista_sinais
    ):
        problemas.append(
            f"{caminho_sinais.name}: 'sinais' deve ser lista de textos "
            "(config declarativa inválida)"
        )

    if dados_dimensoes.get("schema_version") != 1:
        problemas.append(
            f"{caminho_dimensoes.name}: schema_version != 1 (AD-9)"
        )
    lista_dimensoes = dados_dimensoes.get("dimensoes")
    if not isinstance(lista_dimensoes, list):
        problemas.append(
            f"{caminho_dimensoes.name}: 'dimensoes' deve ser lista"
        )

    if problemas:
        raise ErroConfigSinais("; ".join(problemas))

    assert isinstance(lista_sinais, list)
    assert isinstance(lista_dimensoes, list)

    sinais_validos = tuple(_normalizar(sinal) for sinal in lista_sinais)
    if len(set(sinais_validos)) != len(sinais_validos):
        problemas.append(
            f"{caminho_sinais.name}: dois sinais colapsam no mesmo token "
            "(normalizar remove acento/caixa)"
        )
        raise ErroConfigSinais("; ".join(problemas))

    sinal_para_dimensoes: dict[str, tuple[str, ...]] = {
        sinal: () for sinal in sinais_validos
    }
    for dimensao in lista_dimensoes:
        if not isinstance(dimensao, dict) or not dimensao.get("id"):
            problemas.append(
                f"{caminho_dimensoes.name}: cada dimensão precisa de 'id'"
            )
            continue
        id_dimensao = str(dimensao["id"])
        sinais_dimensao = dimensao.get("sinais")
        if not isinstance(sinais_dimensao, list):
            problemas.append(
                f"{caminho_dimensoes.name}: dimensão '{id_dimensao}' "
                "sem lista de 'sinais'"
            )
            continue
        for sinal in sinais_dimensao:
            chave = _normalizar(str(sinal))
            if chave in sinal_para_dimensoes:
                atuais = sinal_para_dimensoes[chave]
                if id_dimensao not in atuais:
                    sinal_para_dimensoes[chave] = (*atuais, id_dimensao)
            else:
                problemas.append(
                    f"{caminho_dimensoes.name}: dimensão '{id_dimensao}' "
                    f"referencia sinal desconhecido '{sinal}' "
                    "(não está na lista de sinais de sinais_inovacao.yaml)"
                )

    if problemas:
        raise ErroConfigSinais("; ".join(problemas))

    padroes = tuple(
        re.compile(rf"\b{re.escape(sinal)}\b") for sinal in sinais_validos
    )
    return SinaisInovacao(sinais_validos, padroes, sinal_para_dimensoes)


@dataclass(slots=True)
class DesfechoClassificacao:
    """Veredito de UM documento — o que vai para a tabela ``classificacoes``."""

    url_origem: str
    tipo_edital: str
    metodo: str
    sinais: list[str]
    dimensoes: list[str]
    fonte_ancora: str | None


@dataclass(slots=True)
class ResumoClassificacao:
    """Contagens da classificação de UM portal — vira saída CLI e evento."""

    instituicao_sigla: str
    portal_nome: str
    documentos: int = 0
    inovacao: int = 0
    nao_inovacao: int = 0
    sem_texto: int = 0
    ja_classificados: int = 0
    excluidos_ignorados: int = 0
    arquivos_ausentes: int = 0

    def totalizar(self) -> dict:
        return {
            "documentos": self.documentos,
            "inovacao": self.inovacao,
            "nao_inovacao": self.nao_inovacao,
            "sem_texto": self.sem_texto,
            "ja_classificados": self.ja_classificados,
            "excluidos_ignorados": self.excluidos_ignorados,
            "arquivos_ausentes": self.arquivos_ausentes,
        }


def _ler_texto(documento: sqlite3.Row) -> str | None:
    """Texto extraído pelo CAP-6 (``texto_caminho`` relativo ao repo)."""
    caminho = documento["texto_caminho"]
    if not caminho:
        return None
    try:
        conteudo = Path(caminho).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return conteudo


def _trecho_ancora(ancora: str, limite: int = 160) -> str:
    """Primeira janela legível da âncora para rastreabilidade (fonte_ancora)."""
    return " ".join(ancora.split())[:limite]


def avaliar(
    texto_extraido: str | None,
    texto_ancora: str | None,
    sinais: SinaisInovacao,
) -> DesfechoClassificacao:
    """Decide o veredito de UM documento (sem interações — função pura).

    Regras (matriz I/O):
    - texto real extraído → sinal no texto OU na âncora ⇒ ``inovacao``
      (metodo ``'texto'``); senão ⇒ ``nao_inovacao`` (metodo ``'texto'``);
    - sem texto → sinal na âncora ⇒ ``inovacao`` (metodo ``'ancora'``);
      senão ⇒ ``sem_texto`` (metodo ``'sem_texto'``). Nunca ``nao_inovacao``
      sem leitura de texto.
    """
    tem_texto = bool(texto_extraido and texto_extraido.strip())
    texto_normalizado = _normalizar(texto_extraido or "")
    ancora_normalizada = _normalizar(texto_ancora or "")

    if tem_texto:
        casados, dimensoes = sinais.casar(texto_normalizado)
        if casados:
            fonte_ancora = None
        else:
            casados, dimensoes = sinais.casar(ancora_normalizada)
            fonte_ancora = _trecho_ancora(texto_ancora or "") if casados else None
        if casados:
            return DesfechoClassificacao(
                url_origem="",
                tipo_edital="inovacao",
                metodo="ancora" if fonte_ancora else "texto",
                sinais=casados,
                dimensoes=dimensoes,
                fonte_ancora=fonte_ancora,
            )
        return DesfechoClassificacao(
            url_origem="",
            tipo_edital="nao_inovacao",
            metodo="texto",
            sinais=[],
            dimensoes=[],
            fonte_ancora=None,
        )

    casados, dimensoes = sinais.casar(ancora_normalizada)
    if casados:
        return DesfechoClassificacao(
            url_origem="",
            tipo_edital="inovacao",
            metodo="ancora",
            sinais=casados,
            dimensoes=dimensoes,
            fonte_ancora=_trecho_ancora(texto_ancora or ""),
        )
    return DesfechoClassificacao(
        url_origem="",
        tipo_edital="sem_texto",
        metodo="sem_texto",
        sinais=[],
        dimensoes=[],
        fonte_ancora=None,
    )


def _classificar_documentos(
    manifesto: Manifesto,
    sinais: SinaisInovacao,
    resumo: ResumoClassificacao,
    documentos: list[sqlite3.Row],
    *,
    comando: str,
) -> None:
    """Núcleo compartilhado por portal e por URL individual."""
    rotulo = {
        "instituicao": resumo.instituicao_sigla,
        "portal": resumo.portal_nome,
    }
    for documento in documentos:
        url = documento["url_origem"]
        if documento["excluido_vigente"]:
            resumo.excluidos_ignorados += 1
            continue
        previo = manifesto.classificacao_obter(url)
        if previo is not None and previo["metodo"] != "sem_texto":
            resumo.ja_classificados += 1
            continue
        try:
            texto = _ler_texto(documento)
            if documento["texto_caminho"] and texto is None:
                resumo.arquivos_ausentes += 1
            desfecho = avaliar(texto, documento["texto_ancora"], sinais)
            desfecho.url_origem = url
            manifesto.classificacao_registrar(
                url_origem=desfecho.url_origem,
                tipo_edital=desfecho.tipo_edital,
                metodo=desfecho.metodo,
                sinais=desfecho.sinais,
                dimensoes=desfecho.dimensoes,
                fonte_ancora=desfecho.fonte_ancora,
            )
        except Exception as exc:  # avaliar OU escrita falham → evento; lote segue (AD-10)
            manifesto.registrar_evento(
                tipo="classificacao_documento_erro",
                comando=comando,
                detalhe={**rotulo, "url": url, "erro": str(exc)},
            )
            continue
        resumo.documentos += 1
        if desfecho.tipo_edital == "inovacao":
            resumo.inovacao += 1
        elif desfecho.tipo_edital == "nao_inovacao":
            resumo.nao_inovacao += 1
        else:
            resumo.sem_texto += 1
        manifesto.registrar_evento(
            tipo="classificacao_documento_concluida",
            comando=comando,
            detalhe={
                **rotulo,
                "url": url,
                "tipo_edital": desfecho.tipo_edital,
                "metodo": desfecho.metodo,
                "sinais": list(desfecho.sinais),
            },
        )


def classificar_portal(
    contexto: ContextoPortal,
    manifesto: Manifesto,
    sinais: SinaisInovacao,
    *,
    comando: str = "classificar",
    urls_restritas: set[str] | None = None,
) -> ResumoClassificacao:
    """Classifica os documentos do portal (matriz I/O da story).

    Documentos excluídos pela curadoria e resultados já definitivos
    (``metodo != 'sem_texto'``) são pulados — veredito ADVISORY no primeiro
    caso; "não sobrescrever" (Never) no segundo. ``urls_restritas`` limita o
    lote a um recorte de URLs (ex.: ``--varredura``) — as contagens do resumo
    refletem SOMENTE o recorte.
    """
    resumo = ResumoClassificacao(contexto.instituicao_sigla, contexto.portal.nome)
    documentos = manifesto.documentos_para_classificar(contexto.portal_id)
    if urls_restritas is not None:
        documentos = [d for d in documentos if d["url_origem"] in urls_restritas]
    _classificar_documentos(manifesto, sinais, resumo, documentos, comando=comando)
    manifesto.registrar_evento(
        tipo="classificacao_portal_concluida",
        comando=comando,
        detalhe={
            "instituicao": contexto.instituicao_sigla,
            "portal": contexto.portal.nome,
            **resumo.totalizar(),
        },
    )
    return resumo


def classificar_documento(
    manifesto: Manifesto,
    sinais: SinaisInovacao,
    url_origem: str,
    *,
    comando: str = "classificar",
) -> ResumoClassificacao | None:
    """Classifica UMA URL (comando ``classificar --documento URL``).

    Retorna ``None`` se nenhum documento responde pela URL (exit 1 no CLI).
    """
    documentos = manifesto.documentos_para_classificar(url_origem=url_origem)
    if not documentos:
        return None
    documento = documentos[0]
    resumo = ResumoClassificacao(
        documento["instituicao_sigla"], documento["portal_nome"]
    )
    _classificar_documentos(manifesto, sinais, resumo, documentos, comando=comando)
    if resumo.excluidos_ignorados and resumo.documentos == 0:
        raise ErroClassificacao(
            f"{url_origem}: documento excluído da curadoria "
            "(fila de revisão) — nada a classificar"
        )
    return resumo
