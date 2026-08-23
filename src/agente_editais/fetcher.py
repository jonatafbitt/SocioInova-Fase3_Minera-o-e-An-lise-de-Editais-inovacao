"""Fetcher — ÚNICO pacote com I/O de rede (AD-5).

Nesta story (1) existe apenas o **probe do pré-voo** de seeds (FR-2):
HEAD com fallback GET, timeout curto e polidez (delay mínimo por host).
Sem crawling, sem renderização, sem download — isso é Story 2, que estende
este módulo mantendo o monopólio de rede intacto.

A janela off-peak é interpretada NO FUSO DO HOST. Para o probe ela é
registrada nos eventos mas não bloqueia (checagem leve de alcançabilidade);
o crawling obrigará a janela (``probe.respeitar_janela_off_peak`` já prevê o
controle em ``configs/politeness.toml``).
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, time as hora_toml
from pathlib import Path
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, Field, field_validator

from .mapa import normalizar_url


class ViolacaoPolidez(RuntimeError):
    """Regra de politude violada/exigida não satisfeita (AD-5, §9 PRD)."""


class ErroConfigPolidez(ValueError):
    """politeness.toml ausente ou malformado."""


class Polidez(BaseModel):
    """Parâmetros de cortesia vindos de ``configs/politeness.toml``."""

    delay_minimo_s: float = Field(ge=0)
    off_peak: str
    user_agent: str = Field(min_length=1)
    probe_timeout_s: float = Field(default=10.0, gt=0)
    probe_respeitar_janela_off_peak: bool = False

    @field_validator("off_peak")
    @classmethod
    def _janela_valida(cls, valor: str) -> str:
        _janela_de(valor)  # levanta ErroConfigPolidez se malformada
        return valor


def carregar_polidez(caminho: Path) -> Polidez:
    caminho = Path(caminho)
    try:
        dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ErroConfigPolidez(f"{caminho}: não foi possível ler ({exc}).") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ErroConfigPolidez(f"{caminho}: TOML malformado — {exc}") from exc

    try:
        probe = dados.pop("probe", {})
        return Polidez(
            delay_minimo_s=float(dados["delay_minimo_s"]),
            off_peak=str(dados["off_peak"]),
            user_agent=str(dados["user_agent"]),
            probe_timeout_s=float(probe.get("timeout_s", 10.0)),
            probe_respeitar_janela_off_peak=bool(probe.get("respeitar_janela_off_peak", False)),
        )
    except KeyError as exc:
        raise ErroConfigPolidez(f"{caminho}: chave obrigatória ausente: {exc}") from exc
    except ValueError as exc:
        raise ErroConfigPolidez(f"{caminho}: valor inválido — {exc}") from exc


# -- janela off-peak ---------------------------------------------------------


def _hora_de(texto: str) -> hora_toml:
    partes = texto.split(":")
    if len(partes) != 2 or not all(p.isdigit() and len(p) == 2 for p in partes):
        raise ValueError(f"horário '{texto}' inválido — esperado HH:MM")
    horas, minutos = int(partes[0]), int(partes[1])
    if not (0 <= horas <= 23 and 0 <= minutos <= 59):
        raise ValueError(f"horário '{texto}' fora da faixa")
    return hora_toml(horas, minutos)


def _janela_de(off_peak: str) -> tuple[hora_toml, hora_toml]:
    pedacos = off_peak.split("-")
    if len(pedacos) != 2:
        raise ValueError(f"janela '{off_peak}' inválida — esperado 'HH:MM-HH:MM'")
    return _hora_de(pedacos[0]), _hora_de(pedacos[1])


def dentro_da_janela_off_peak(off_peak: str, agora: datetime | None = None) -> bool:
    """True se ``agora`` (default: relógio local do HOST) está em [início, fim).

    Suporta janelas que cruzam a meia-noite (ex.: "22:00-06:00").
    """
    inicio, fim = _janela_de(off_peak)
    momento = (agora or datetime.now().astimezone()).time()
    if inicio <= fim:
        return inicio <= momento < fim
    return momento >= inicio or momento < fim


# -- delay mínimo por host ---------------------------------------------------

_ultimo_pedido_por_host: dict[str, float] = {}


def reiniciar_estado_polidez() -> None:
    """Limpa o registro de último pedido (uso em testes)."""
    _ultimo_pedido_por_host.clear()


def _aguardar_delay(host: str, delay_minimo_s: float) -> None:
    anterior = _ultimo_pedido_por_host.get(host)
    if anterior is not None and delay_minimo_s > 0:
        resta = delay_minimo_s - (time.monotonic() - anterior)
        if resta > 0:
            time.sleep(resta)


# -- probe do pré-voo (FR-2) --------------------------------------------------


@dataclass(slots=True)
class ResultadoSeed:
    url: str
    ok: bool
    status_http: int | None
    metodo: str
    erro: str | None
    duracao_s: float


_METODOS_COM_HEAD_BLOQUEADO = frozenset({405, 501})


def probe_seed(url: str, polidez: Polidez) -> ResultadoSeed:
    """Verifica alcançabilidade de uma seed: HEAD, caindo para GET em 405/501."""
    destino = normalizar_url(url)
    host = urlsplit(destino).netloc.lower()

    if polidez.probe_respeitar_janela_off_peak and not dentro_da_janela_off_peak(polidez.off_peak):
        raise ViolacaoPolidez(
            f"Fora da janela off-peak ({polidez.off_peak}) no fuso do host; "
            "coleta recusada pela polidez centralizada (AD-5)."
        )

    inicio = time.perf_counter()

    tempo = (polidez.probe_timeout_s, polidez.probe_timeout_s)
    metodo = "HEAD"
    status: int | None = None
    erro: str | None = None
    try:
        with requests.Session() as sessao:
            sessao.headers["User-Agent"] = polidez.user_agent
            _aguardar_delay(host, polidez.delay_minimo_s)
            _ultimo_pedido_por_host[host] = time.monotonic()
            resposta = sessao.head(destino, timeout=tempo, allow_redirects=True)
            if resposta.status_code in _METODOS_COM_HEAD_BLOQUEADO:
                metodo = "GET"
                # o fallback é uma nova requisição ao mesmo host: delay de novo
                _aguardar_delay(host, polidez.delay_minimo_s)
                resposta = sessao.get(destino, timeout=tempo, stream=True)
                _ultimo_pedido_por_host[host] = time.monotonic()
                resposta.close()
            status = resposta.status_code
    except requests.RequestException as exc:
        erro = f"{type(exc).__name__}: {exc}"

    duracao = time.perf_counter() - inicio
    ok = status is not None and status < 400
    if not ok and erro is None:
        erro = f"HTTP {status}"
    return ResultadoSeed(
        url=destino,
        ok=ok,
        status_http=status,
        metodo=metodo,
        erro=erro,
        duracao_s=duracao,
    )


def executar_pre_voo(
    seeds: list[str],
    polidez: Polidez,
    *,
    informar_progresso=None,
) -> list[ResultadoSeed]:
    """Sonda cada seed em sequência; falhas NUNCA abortam o lote (FR-2)."""
    reiniciar_estado_polidez()
    resultados: list[ResultadoSeed] = []
    for indice, seed in enumerate(seeds, start=1):
        try:
            resultado = probe_seed(seed, polidez)
        except ViolacaoPolidez:
            raise
        resultados.append(resultado)
        if informar_progresso is not None:
            informar_progresso(indice, len(seeds), resultado)
    return resultados
