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

import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, time as hora_toml
from pathlib import Path

import requests
from pydantic import BaseModel, Field, field_validator

from .mapa import hostname_de, normalizar_url


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


_CHAVES_RAIZ = ("delay_minimo_s", "off_peak", "user_agent")
_CHAVES_PROBE = ("timeout_s", "respeitar_janela_off_peak")


def _numero_positivo(caminho: Path, campo: str, valor: object) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErroConfigPolidez(
            f"{caminho}: campo '{campo}' deve ser número, recebido {type(valor).__name__}."
        )
    return float(valor)


def _texto_obrigatorio(caminho: Path, campo: str, valor: object) -> str:
    if not isinstance(valor, str):
        raise ErroConfigPolidez(
            f"{caminho}: campo '{campo}' deve ser texto, recebido {type(valor).__name__}."
        )
    return valor


def carregar_polidez(caminho: Path) -> Polidez:
    """Lê e valida politeness.toml — paridade com o extra=forbid do mapa."""
    caminho = Path(caminho)
    try:
        dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ErroConfigPolidez(f"{caminho}: não foi possível ler ({exc}).") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ErroConfigPolidez(f"{caminho}: TOML malformado — {exc}") from exc

    desconhecidas = sorted(set(dados) - set(_CHAVES_RAIZ) - {"probe"})
    if desconhecidas:
        raise ErroConfigPolidez(
            f"{caminho}: chaves desconhecidas na raiz: {', '.join(desconhecidas)}; "
            f"aceitas: {', '.join(_CHAVES_RAIZ)}, [probe]"
        )
    faltantes = [chave for chave in _CHAVES_RAIZ if chave not in dados]
    if faltantes:
        raise ErroConfigPolidez(f"{caminho}: chave obrigatória ausente: {', '.join(faltantes)}")

    probe_bruto = dados.get("probe", {})
    if not isinstance(probe_bruto, dict):
        raise ErroConfigPolidez(f"{caminho}: seção '[probe]' deve ser uma tabela TOML.")
    desconhecidas_probe = sorted(set(probe_bruto) - set(_CHAVES_PROBE))
    if desconhecidas_probe:
        raise ErroConfigPolidez(
            f"{caminho}: chaves desconhecidas em [probe]: {', '.join(desconhecidas_probe)}; "
            f"aceitas: {', '.join(_CHAVES_PROBE)}"
        )

    respeitar = probe_bruto.get("respeitar_janela_off_peak", False)
    if not isinstance(respeitar, bool):
        raise ErroConfigPolidez(
            f"{caminho}: 'probe.respeitar_janela_off_peak' exige booleano estrito "
            f"(true/false), recebido {type(respeitar).__name__}."
        )

    try:
        return Polidez(
            delay_minimo_s=_numero_positivo(caminho, "delay_minimo_s", dados["delay_minimo_s"]),
            off_peak=_texto_obrigatorio(caminho, "off_peak", dados["off_peak"]),
            user_agent=_texto_obrigatorio(caminho, "user_agent", dados["user_agent"]),
            probe_timeout_s=(
                _numero_positivo(caminho, "probe.timeout_s", probe_bruto.get("timeout_s", 10.0))
                if "timeout_s" in probe_bruto
                else 10.0
            ),
            probe_respeitar_janela_off_peak=respeitar,
        )
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
    inicio, fim = _hora_de(pedacos[0]), _hora_de(pedacos[1])
    if inicio == fim:
        raise ValueError(f"janela '{off_peak}' é vazia (início = fim)")
    return inicio, fim


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
_trava_delay = threading.Lock()


def reiniciar_estado_polidez() -> None:
    """Limpa o registro de último pedido (uso em testes)."""
    with _trava_delay:
        _ultimo_pedido_por_host.clear()


def _aguardar_delay(host: str, delay_minimo_s: float) -> None:
    with _trava_delay:
        anterior = _ultimo_pedido_por_host.get(host)
    if anterior is not None and delay_minimo_s > 0:
        resta = delay_minimo_s - (time.monotonic() - anterior)
        if resta > 0:
            time.sleep(resta)


def _marcar_pedido(host: str) -> None:
    with _trava_delay:
        _ultimo_pedido_por_host[host] = time.monotonic()


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


def _nova_sessao(user_agent: str) -> requests.Session:
    sessao = requests.Session()
    sessao.headers["User-Agent"] = user_agent
    return sessao


def probe_seed(url: str, polidez: Polidez, sessao: requests.Session | None = None) -> ResultadoSeed:
    """Verifica alcançabilidade de uma seed: HEAD, caindo para GET em 405/501.

    ``sessao`` permite reaproveitar TCP/TLS ao longo de um lote (pré-voo);
    sem ela, uma sessão dedicada é criada e descartada.
    """
    destino = normalizar_url(url)
    host_origem = hostname_de(destino)

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
    url_final = destino
    propria = sessao is None
    if propria:
        sessao = _nova_sessao(polidez.user_agent)
    assert sessao is not None
    try:
        # delay vale para o hostname SEM porta; redirects caem no mesmo balde
        # quando o host final coincide com o de origem.
        _aguardar_delay(host_origem, polidez.delay_minimo_s)
        resposta = sessao.head(destino, timeout=tempo, allow_redirects=True)
        _marcar_pedido(host_origem)
        if resposta.status_code in _METODOS_COM_HEAD_BLOQUEADO:
            metodo = "GET"
            # o fallback é uma nova requisição ao mesmo host: delay de novo
            _aguardar_delay(host_origem, polidez.delay_minimo_s)
            resposta = sessao.get(destino, timeout=tempo, stream=True)
            _marcar_pedido(host_origem)
            resposta.close()
        status = resposta.status_code
        url_final = str(resposta.url)
        host_final = hostname_de(url_final)
        if host_final and host_final != host_origem:
            # o redirect atingiu OUTRO host: cortesia registrada/aplicada a ele
            _aguardar_delay(host_final, polidez.delay_minimo_s)
            _marcar_pedido(host_final)
    except requests.RequestException as exc:
        erro = f"{type(exc).__name__}: {exc}"
    finally:
        if propria:
            sessao.close()

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
    """Sonda cada seed em sequência; falhas NUNCA abortam o lote (FR-2).

    Uma única Session serve todo o lote (reuso de TCP/TLS). Exceção por-seed
    (ex.: URL malformada) vira ``ResultadoSeed(ok=False)`` — só ``ViolacaoPolidez``
    interrompe o lote inteiro, pois é condição global de politude.
    """
    reiniciar_estado_polidez()
    resultados: list[ResultadoSeed] = []
    with _nova_sessao(polidez.user_agent) as sessao:
        for indice, seed in enumerate(seeds, start=1):
            try:
                resultado = probe_seed(seed, polidez, sessao=sessao)
            except ViolacaoPolidez:
                raise
            except Exception as exc:  # noqa: BLE001 — seed ruim não derruba o lote
                try:
                    destino = normalizar_url(seed)
                except Exception:  # noqa: BLE001 — nem normalizar dá
                    destino = seed.strip()
                resultado = ResultadoSeed(
                    url=destino,
                    ok=False,
                    status_http=None,
                    metodo="-",
                    erro=f"{type(exc).__name__}: {exc}",
                    duracao_s=0.0,
                )
            resultados.append(resultado)
            if informar_progresso is not None:
                informar_progresso(indice, len(seeds), resultado)
    return resultados
