"""Adaptador LLM (CAP-7) — ÚNICO ponto de rede do estágio de análise (AD-6).

Isola o provedor (escolha adiada — PRD OQ-1): transporte OpenAI-compatível
via ``requests`` POST em ``{LLM_BASE_URL}/chat/completions``, com credenciais
EXCLUSIVAMENTE por variáveis de ambiente — nada de chave em configs
versionadas, hash, evento, log ou exceção.

Variáveis de ambiente:
- ``LLM_BASE_URL``  — base da API (ex.: https://api.exemplo.com/v1);
- ``LLM_API_KEY``   — chave Bearer (segredo; nunca logada);
- ``LLM_MODELO``    — modelo default quando o comando não recebe --modelo;
- ``LLM_VERSAO_MODELO`` — opcional: snapshot/versionamento declarado pelo
  pesquisador (registrado no lote; vazio = provedor sem versão explícita);
- ``LLM_TIMEOUT_S`` — prazo total da chamada (default 120).

O instrumento da EXECUÇÃO é fixado UMA vez por ``definir_instrumento``
(um lote = um instrumento, FR-18); ``concluir(sistema, usuario) -> str`` é a
única função de rede. Erros saem como ``ErroProvedorLLM`` SEM ecoar
credenciais nem o conteúdo da requisição.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass

import requests

ENV_BASE_URL = "LLM_BASE_URL"
ENV_CHAVE = "LLM_API_KEY"
ENV_MODELO = "LLM_MODELO"
ENV_VERSAO_DO_MODELO = "LLM_VERSAO_MODELO"
ENV_TIMEOUT_S = "LLM_TIMEOUT_S"

_TIMEOUT_PADRAO_S = 120.0


class ErroProvedorLLM(RuntimeError):
    """Provedor LLM indisponível/respondeu fora do esperado (lote segue)."""


@dataclass(frozen=True, slots=True)
class Instrumento:
    """Parâmetros congelados da execução — espelham a assinatura do lote."""

    modelo: str
    temperatura: float
    seed: int | None
    timeout_s: float


_instrumento: Instrumento | None = None


def definir_instrumento(
    *,
    modelo: str,
    temperatura: float,
    seed: int | None = None,
    timeout_s: float | None = None,
) -> Instrumento:
    """Fixa o instrumento DESTA execução (chame antes de qualquer ``concluir``).

    ``timeout_s=None`` lê ``LLM_TIMEOUT_S`` (default 120). Temperatura deve
    ser finita em [0, 2]; timeout, finito > 0 — recusados com ValueError
    ANTES de qualquer rede.
    """
    global _instrumento
    if not modelo or not modelo.strip():
        raise ValueError("modelo do instrumento não pode ser vazio")
    if (
        isinstance(temperatura, bool)
        or not isinstance(temperatura, (int, float))
        or not math.isfinite(float(temperatura))
        or not (0.0 <= float(temperatura) <= 2.0)
    ):
        raise ValueError(
            f"temperatura deve ser número finito entre 0 e 2 (recebido {temperatura!r})"
        )
    if timeout_s is None:
        bruto = os.environ.get(ENV_TIMEOUT_S, "").strip()
        if not bruto:
            timeout_efetivo = _TIMEOUT_PADRAO_S
        else:
            try:
                timeout_efetivo = float(bruto)
            except ValueError as exc:
                raise ValueError(
                    f"{ENV_TIMEOUT_S} deve ser numérico (recebido {bruto!r})"
                ) from exc
    else:
        timeout_efetivo = float(timeout_s)
    if (
        isinstance(timeout_efetivo, bool)
        or not math.isfinite(timeout_efetivo)
        or timeout_efetivo <= 0
    ):
        raise ValueError(
            f"timeout deve ser número finito positivo (recebido {timeout_efetivo!r})"
        )
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ValueError(f"seed deve ser inteiro ou ausente (recebido {seed!r})")
    _instrumento = Instrumento(
        modelo=modelo.strip(),
        temperatura=float(temperatura),
        seed=seed,
        timeout_s=timeout_efetivo,
    )
    return _instrumento


def reiniciar_instrumento() -> None:
    """Descarta o instrumento da execução (isolamento entre execuções/testes)."""
    global _instrumento
    _instrumento = None


def instrumento_atual() -> Instrumento | None:
    return _instrumento


def _exigir_env(nome: str) -> str:
    valor = os.environ.get(nome, "").strip()
    if not valor:
        raise ErroProvedorLLM(
            f"variável de ambiente '{nome}' não definida — configure-a "
            "(por .env gitignored ou ambiente) para falar com o provedor LLM"
        )
    return valor


def concluir(sistema: str, usuario: str) -> str:
    """Envia (sistema, usuário) ao provedor e devolve o texto da resposta.

    ÚNICO ponto de rede do estágio (AD-6). A chave viaja só no header
    Authorization e NUNCA aparece em mensagem de erro, log ou evento.
    Falhas de transporte/HTTP/corpo viram ``ErroProvedorLLM`` — o chamador
    registra o desfecho do edital e o lote segue.
    """
    if _instrumento is None:
        raise ErroProvedorLLM(
            "instrumento não definido: chame llm_adapter.definir_instrumento() "
            "antes de concluir()"
        )
    base_url = _exigir_env(ENV_BASE_URL).rstrip("/")
    chave = _exigir_env(ENV_CHAVE)

    corpo: dict[str, object] = {
        "model": _instrumento.modelo,
        "messages": [
            {"role": "system", "content": sistema},
            {"role": "user", "content": usuario},
        ],
        "temperature": _instrumento.temperatura,
    }
    if _instrumento.seed is not None:
        corpo["seed"] = _instrumento.seed

    inicio = time.monotonic()
    try:
        resposta = requests.post(
            f"{base_url}/chat/completions",
            json=corpo,
            headers={"Authorization": f"Bearer {chave}"},
            timeout=_instrumento.timeout_s,
        )
    except requests.RequestException as exc:
        raise ErroProvedorLLM(
            f"falha de rede com o provedor LLM ({type(exc).__name__})"
        ) from exc
    # Prazo TOTAL da chamada (time.monotonic, imune a ajustes de relógio):
    # o timeout do requests limita cada operação de socket — um trickle que
    # pinga dentro do limite por leitura pode estourar o orçamento total.
    if time.monotonic() - inicio > _instrumento.timeout_s:
        raise ErroProvedorLLM("prazo total excedido na chamada ao provedor LLM")
    if resposta.status_code != 200:
        raise ErroProvedorLLM(
            f"provedor LLM respondeu HTTP {resposta.status_code}"
        )
    try:
        dados = resposta.json()
        conteudo = dados["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ErroProvedorLLM(
            "resposta do provedor LLM em formato inesperado"
        ) from exc
    if not isinstance(conteudo, str) or not conteudo.strip():
        raise ErroProvedorLLM("provedor LLM devolveu conteúdo vazio")
    return conteudo
