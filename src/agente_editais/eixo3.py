"""Classificação do Eixo 3 — Relevância e Impacto Social.

Regras (conforme instruções de codificação):
- IMPACTO_INSTRUMENTAL: critérios de avaliação pontuam EXCLUSIVAMENTE
  'potencial de mercado' e 'viabilidade financeira'
- IMPACTO_SUBSTANTIVO: edital reserva cotas/pontuação/bolsas para
  'tecnologias sociais', 'comunidades vulneráveis' ou 'economia solidária'
- AUSENTE_SILENCIAMENTO: nenhum dos padrões acima encontrado

Obrigatório: extrair citação literal do PDF (trecho probatório).
Anti-alucinação: se não houver menção, retornar AUSENTE_SILENCIAMENTO.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .descoberta import _sem_acento


class ErroConfigEixo3(ValueError):
    """Configuração de Eixo 3 inválida (exit 2)."""


@dataclass(slots=True)
class RegraInstrumental:
    sinais_obrigatorios: tuple[str, ...]
    sinais_vedados: tuple[str, ...]
    padroes_obrigatorios: tuple[re.Pattern[str], ...]
    padroes_vedados: tuple[re.Pattern[str], ...]


@dataclass(slots=True)
class RegraSubstantivo:
    sinais_gatilho: tuple[str, ...]
    sinais_alvo: tuple[str, ...]
    padroes_gatilho: tuple[re.Pattern[str], ...]
    padroes_alvo: tuple[re.Pattern[str], ...]


@dataclass(slots=True)
class ConfigEixo3:
    instrumental: RegraInstrumental
    substantivo: RegraSubstantivo
    janela_comprobatoria: int


@dataclass(slots=True)
class DesfechoEixo3:
    """Resultado da classificação de UM documento."""
    codigo: str
    trecho_comprobatorio: str
    regra_disparada: str
    sinais_encontrados: list[str]


def _normalizar(texto: str) -> str:
    """Normalização idêntica à classificação principal: lowercase + sem acento + collapse whitespace."""
    return " ".join(_sem_acento(texto).split())


def _compilar_padroes(termos: list[str]) -> tuple[re.Pattern[str], ...]:
    """Compila regex de token inteiro para cada termo normalizado."""
    return tuple(
        re.compile(rf"\b{re.escape(_normalizar(t))}\b")
        for t in termos
    )


def carregar_config(caminho: Path) -> ConfigEixo3:
    """Carrega e valida configuração de eixo3.yaml."""
    if not caminho.exists():
        raise ErroConfigEixo3(f"Arquivo de configuração não encontrado: {caminho}")

    try:
        with open(caminho, encoding="utf-8") as f:
            dados = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ErroConfigEixo3(f"YAML inválido em {caminho.name}: {exc}") from exc

    if dados.get("schema_version") != 1:
        raise ErroConfigEixo3(f"{caminho.name}: schema_version deve ser 1")

    regras = dados.get("regras", {})
    inst = regras.get("instrumental", {})
    subst = regras.get("substantivo", {})

    instrumental = RegraInstrumental(
        sinais_obrigatorios=tuple(inst.get("sinais_obrigatorios", [])),
        sinais_vedados=tuple(inst.get("sinais_vedados", [])),
        padroes_obrigatorios=_compilar_padroes(inst.get("sinais_obrigatorios", [])),
        padroes_vedados=_compilar_padroes(inst.get("sinais_vedados", [])),
    )

    substantivo = RegraSubstantivo(
        sinais_gatilho=tuple(subst.get("sinais_gatilho", [])),
        sinais_alvo=tuple(subst.get("sinais_alvo", [])),
        padroes_gatilho=_compilar_padroes(subst.get("sinais_gatilho", [])),
        padroes_alvo=_compilar_padroes(subst.get("sinais_alvo", [])),
    )

    janela = int(dados.get("janela_comprobatoria", 120))

    return ConfigEixo3(instrumental, substantivo, janela)


def _extrair_trecho(texto_original: str, posicao: int, janela: int) -> str:
    """Extrai trecho centrado na posição do match, com janela de contexto."""
    inicio = max(0, posicao - janela)
    fim = min(len(texto_original), posicao + janela)
    trecho = texto_original[inicio:fim].strip()
    # Limpar quebras de linha excessivas
    trecho = " ".join(trecho.split())
    return trecho


def _buscar_primeiro_match(texto_normalizado: str, padroes: tuple[re.Pattern[str], ...], texto_original: str, janela: int) -> str | None:
    """Busca o primeiro match de qualquer padrão e retorna trecho probatório."""
    for padrao in padroes:
        match = padrao.search(texto_normalizado)
        if match:
            # Encontrar posição correspondente no texto original
            # Aproximação: usar a posição no texto normalizado
            pos_norm = match.start()
            # Mapear para texto original (aproximado)
            trecho = _extrair_trecho(texto_original, pos_norm, janela)
            return trecho
    return None


def _verificar_instrumental(
    texto_normalizado: str,
    texto_original: str,
    config: ConfigEixo3,
) -> DesfechoEixo3 | None:
    """Verifica regra INSTRUMENTAL: TODOS obrigatórios + NENHUM vedado."""
    inst = config.instrumental
    sinais_encontrados = []

    # Verificar TODOS obrigatórios presentes
    for sinal, padrao in zip(inst.sinais_obrigatorios, inst.padroes_obrigatorios):
        if not padrao.search(texto_normalizado):
            return None
        sinais_encontrados.append(sinal)

    # Verificar NENHUM vedado presente
    for padrao in inst.padroes_vedados:
        if padrao.search(texto_normalizado):
            return None

    # Extrair trecho probatório do primeiro obrigatório
    trecho = _buscar_primeiro_match(texto_normalizado, inst.padroes_obrigatorios, texto_original, config.janela_comprobatoria)

    return DesfechoEixo3(
        codigo="IMPACTO_INSTRUMENTAL",
        trecho_comprobatorio=trecho or "",
        regra_disparada="instrumental",
        sinais_encontrados=sinais_encontrados,
    )


def _verificar_substantivo(
    texto_normalizado: str,
    texto_original: str,
    config: ConfigEixo3,
) -> DesfechoEixo3 | None:
    """Verifica regra SUBSTANTIVO: QUALQUER gatilho + QUALQUER alvo."""
    subst = config.substantivo
    sinais_encontrados = []

    gatilho_match = None
    alvo_match = None

    # Buscar qualquer gatilho
    for sinal, padrao in zip(subst.sinais_gatilho, subst.padroes_gatilho):
        if padrao.search(texto_normalizado):
            gatilho_match = sinal
            sinais_encontrados.append(sinal)
            break

    if not gatilho_match:
        return None

    # Buscar qualquer alvo
    for sinal, padrao in zip(subst.sinais_alvo, subst.padroes_alvo):
        if padrao.search(texto_normalizado):
            alvo_match = sinal
            sinais_encontrados.append(sinal)
            break

    if not alvo_match:
        return None

    # Trecho probatório: tentar pegar gatilho + alvo se próximos, senão gatilho
    trecho = _buscar_primeiro_match(texto_normalizado, subst.padroes_gatilho, texto_original, config.janela_comprobatoria)

    return DesfechoEixo3(
        codigo="IMPACTO_SUBSTANTIVO",
        trecho_comprobatorio=trecho or "",
        regra_disparada="substantivo",
        sinais_encontrados=sinais_encontrados,
    )


def classificar_eixo3(texto: str, config: ConfigEixo3) -> DesfechoEixo3:
    """Classifica UM texto segundo o Eixo 3.

    Ordem de precedência:
    1. SUBSTANTIVO (mais específico, prioridade analítica)
    2. INSTRUMENTAL (exclusivo)
    3. SILÊNCIO (padrão)
    """
    if not texto or not texto.strip():
        return DesfechoEixo3(
            codigo="AUSENTE_SILENCIAMENTO",
            trecho_comprobatorio="",
            regra_disparada="silencio",
            sinais_encontrados=[],
        )

    texto_norm = _normalizar(texto)

    # 1. Verificar SUBSTANTIVO primeiro (prioridade analítica)
    resultado = _verificar_substantivo(texto_norm, texto, config)
    if resultado:
        return resultado

    # 2. Verificar INSTRUMENTAL
    resultado = _verificar_instrumental(texto_norm, texto, config)
    if resultado:
        return resultado

    # 3. Silêncio
    return DesfechoEixo3(
        codigo="AUSENTE_SILENCIAMENTO",
        trecho_comprobatorio="",
        regra_disparada="silencio",
        sinais_encontrados=[],
    )
