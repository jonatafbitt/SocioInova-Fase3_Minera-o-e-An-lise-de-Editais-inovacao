"""Mapa-Mestre (CAP-1): carga, validação e sincronização com o Manifesto.

Governado por:
- AD-9: config declarativa versionada em git; lotes registram o hash usado;
- AD-8: identidade por URL normalizada (host lowercase, sem fragment/utm);
- AD-4: este módulo NUNCA faz I/O de rede (o pré-voo vai VIA fetcher).

Categorias são um conjunto fechado: {integra, nit, prpgi_prppg, agencia_inovacao}.
Erros de validação saem como ``ErroMapa`` com mensagem acionável nomeando
campo e linha no TOML.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .manifest import Manifesto, agora_iso

CATEGORIAS: tuple[str, ...] = ("integra", "nit", "prpgi_prppg", "agencia_inovacao")
SCHEMA_VERSION_SUPORTADA = 1

_RE_SIGLA = re.compile(r"[A-Za-z0-9-]{2,}")


class ErroMapa(ValueError):
    """Mapa-Mestre inválido; ``problemas`` traz uma linha acionável por erro."""

    def __init__(self, problemas: list[str]) -> None:
        self.problemas = problemas
        super().__init__("\n".join(problemas))


_PORTAS_DEFAULT = {"http": 80, "https": 443}


def normalizar_url(url: str) -> str:
    """Identidade de URL por AD-8: esquema/host lowercase, sem fragment,
    sem utm_*, sem porta default (http 80 / https 443); query re-codificada."""
    texto = url.strip()
    partes = urlsplit(texto)
    esquema = partes.scheme.lower()
    if esquema not in ("http", "https"):
        raise ValueError(
            f"esquema inválido ('{esquema or 'ausente'}') — esperado http(s): {url!r}"
        )
    hostname = partes.hostname
    if not partes.netloc or not hostname:
        raise ValueError(f"URL sem host: {url!r}")
    porta = partes.port  # ValueError se porta inválida — propagada como recusa
    if porta is not None and porta != _PORTAS_DEFAULT.get(esquema):
        netloc = f"{hostname.lower()}:{porta}"
    else:
        netloc = hostname.lower()
    pares = [
        (chave, valor)
        for chave, valor in parse_qsl(partes.query, keep_blank_values=True)
        if not chave.lower().startswith("utm_")
    ]
    consulta = urlencode(pares) if pares else ""
    return urlunsplit((esquema, netloc, partes.path, consulta, ""))


def hostname_de(url: str) -> str:
    """Hostname (sem porta) — chave dos registros de polidez."""
    return (urlparse(url).hostname or "").lower()


class Portal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nome: str = Field(min_length=1)
    categoria: str = Field(min_length=1)
    url: str
    seeds: list[str] = Field(min_length=1)
    dinamico: bool = False
    profundidade_maxima: int = Field(default=3, gt=0)

    @field_validator("categoria")
    @classmethod
    def _categoria_minuscula(cls, valor: str) -> str:
        return valor.strip().lower()

    @field_validator("url")
    @classmethod
    def _url_normalizada(cls, valor: str) -> str:
        return normalizar_url(valor)

    @field_validator("seeds")
    @classmethod
    def _seeds_normalizadas(cls, valores: list[str]) -> list[str]:
        return [normalizar_url(v) for v in valores]


class Instituicao(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sigla: str = Field(min_length=2)
    nome: str = Field(min_length=1)
    portal: list[Portal] = Field(min_length=1)

    @field_validator("sigla")
    @classmethod
    def _sigla_segura(cls, valor: str) -> str:
        # a sigla nomeia pastas em corpus/ (CAP-4): charset restrito impede
        # escape da estrutura ({instituicao}/{ano}/) via '..', '/', espaços etc.
        if not _RE_SIGLA.fullmatch(valor):
            raise ValueError(
                "sigla deve casar com [A-Za-z0-9-]{2,} — sem espaços, barras "
                "ou pontuação (ela nomeia a pasta da instituição em corpus/)"
            )
        return valor.strip()


class MapaMestre(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION_SUPORTADA)
    instituicao: list[Instituicao] = Field(min_length=1)

    def seeds_unicas(self) -> list[tuple[Instituicao, Portal, str]]:
        """Pares (instituição, portal, seed) na ordem do arquivo."""
        pares: list[tuple[Instituicao, Portal, str]] = []
        for instituicao in self.instituicao:
            for portal in instituicao.portal:
                for seed in portal.seeds:
                    pares.append((instituicao, portal, seed))
        return pares


# -- carga e validação ------------------------------------------------------


def _formatar_localizacao(loc: tuple) -> str:
    pedacos: list[str] = []
    for parte in loc:
        if isinstance(parte, int):
            pedacos[-1] += f"[{parte}]"
        else:
            pedacos.append(str(parte))
    return ".".join(pedacos)


def _linhas_com(texto: str, trecho: str) -> list[int]:
    return [n for n, linha in enumerate(texto.splitlines(), start=1) if trecho in linha]


def _linhas_do_valor(texto: str, valor: str) -> list[int]:
    """Localiza o valor como token TOML entre aspas; cai para busca solta."""
    exatas = [n for n, linha in enumerate(texto.splitlines(), start=1) if f'"{valor}"' in linha]
    return exatas if exatas else _linhas_com(texto, valor)


def _validacoes_semanticas(mapa: MapaMestre, texto_bruto: str, caminho: Path) -> None:
    problemas: list[str] = []

    if mapa.schema_version != SCHEMA_VERSION_SUPORTADA:
        problemas.append(
            f"{caminho}: campo 'schema_version' = {mapa.schema_version} não suportado "
            f"(esperado {SCHEMA_VERSION_SUPORTADA}); atualize o agente ou o arquivo."
        )

    for i, instituicao in enumerate(mapa.instituicao):
        for j, portal in enumerate(instituicao.portal):
            if portal.categoria not in CATEGORIAS:
                linhas = _linhas_do_valor(texto_bruto, portal.categoria)
                onde = f", linha {_linhas_formatadas(linhas)}" if linhas else ""
                problemas.append(
                    f"{caminho}{onde}: campo 'instituicao[{i}].portal[{j}].categoria' "
                    f"do portal '{portal.nome}' tem valor '{portal.categoria}' inválido; "
                    f"aceitos: {', '.join(CATEGORIAS)}"
                )

    vistas_siglas: dict[str, int] = {}
    for i, instituicao in enumerate(mapa.instituicao):
        chave = instituicao.sigla.upper()
        if chave in vistas_siglas:
            linhas = sorted(
                {
                    *_linhas_do_valor(texto_bruto, instituicao.sigla),
                    *_linhas_do_valor(texto_bruto, mapa.instituicao[vistas_siglas[chave]].sigla),
                }
            )
            onde = f", linha(s) {_linhas_formatadas(linhas)}" if linhas else ""
            problemas.append(
                f"{caminho}{onde}: sigla '{instituicao.sigla}' duplicada "
                f"(instituicao[{vistas_siglas[chave]}] e instituicao[{i}]); "
                "cada instituição deve ter sigla única."
            )
        else:
            vistas_siglas[chave] = i

    urls_vistas: dict[str, str] = {}
    for i, instituicao in enumerate(mapa.instituicao):
        for j, portal in enumerate(instituicao.portal):
            if portal.url in urls_vistas:
                linhas = _linhas_do_valor(texto_bruto, portal.url)
                onde = f", linha(s) {_linhas_formatadas(linhas)}" if linhas else ""
                problemas.append(
                    f"{caminho}{onde}: URL '{portal.url}' declarada em dois portais "
                    f"('{urls_vistas[portal.url]}' e 'instituicao[{i}].portal[{j}]'); "
                    "URLs normalizadas devem ser únicas."
                )
            else:
                urls_vistas[portal.url] = f"instituicao[{i}].portal[{j}]"

    for i, instituicao in enumerate(mapa.instituicao):
        for j, portal in enumerate(instituicao.portal):
            vistas: dict[str, int] = {}
            for k, seed in enumerate(portal.seeds):
                if seed in vistas:
                    linhas = _linhas_do_valor(texto_bruto, seed)
                    onde = f", linha(s) {_linhas_formatadas(linhas)}" if linhas else ""
                    problemas.append(
                        f"{caminho}{onde}: seed '{seed}' repetida em "
                        f"instituicao[{i}].portal[{j}].seeds (posições {vistas[seed]} e {k})."
                    )
                else:
                    vistas[seed] = k

    seeds_por_portal: dict[str, list[str]] = {}
    for i, instituicao in enumerate(mapa.instituicao):
        for j, portal in enumerate(instituicao.portal):
            rotulo = f"{instituicao.sigla}/instituicao[{i}].portal[{j}] ('{portal.nome}')"
            for seed in portal.seeds:
                seeds_por_portal.setdefault(seed, []).append(rotulo)
    for seed, portais_com_a_seed in sorted(seeds_por_portal.items()):
        if len(portais_com_a_seed) > 1:
            linhas = _linhas_do_valor(texto_bruto, seed)
            onde = f", linha(s) {_linhas_formatadas(linhas)}" if linhas else ""
            problemas.append(
                f"{caminho}{onde}: seed '{seed}' declarada em múltiplos portais — "
                + " vs ".join(portais_com_a_seed)
                + "; cada seed deve pertencer a um único portal."
            )

    if problemas:
        raise ErroMapa(problemas)


def _linhas_formatadas(linhas: list[int]) -> str:
    return ", ".join(str(n) for n in linhas)


def carregar_mapa(caminho: Path) -> MapaMestre:
    """Carrega e valida ``mapa-mestre.toml``; levanta ``ErroMapa`` se inválido."""
    caminho = Path(caminho)
    try:
        texto_bruto = caminho.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ErroMapa([f"{caminho}: não foi possível ler o arquivo ({exc})."]) from exc

    try:
        dados = tomllib.loads(texto_bruto)
    except tomllib.TOMLDecodeError as exc:
        mensagem = str(exc)
        achou = re.search(r"line (\d+)", mensagem)
        onde = f", linha {achou.group(1)}" if achou else ""
        raise ErroMapa([f"{caminho}{onde}: TOML malformado — {mensagem}"]) from exc

    try:
        mapa = MapaMestre.model_validate(dados)
    except ValidationError as exc:
        problemas = []
        for erro in exc.errors():
            local = _formatar_localizacao(erro["loc"])
            entrada = erro.get("input")
            linhas = (
                _linhas_com(texto_bruto, str(entrada))
                if isinstance(entrada, (str, int, float, bool))
                else []
            )
            onde = f", linha {_linhas_formatadas(linhas)}" if linhas else ""
            problemas.append(
                f"{caminho}{onde}: campo '{local}' — {erro['msg']}"
                + (f" (valor recebido: {entrada!r})" if entrada is not None else "")
            )
        raise ErroMapa(problemas) from exc

    _validacoes_semanticas(mapa, texto_bruto, caminho)
    return mapa


def hash_arquivo(caminho: Path) -> str:
    """SHA-256 do arquivo de config — registrado em eventos (AD-9)."""
    try:
        conteudo = Path(caminho).read_bytes()
    except OSError as exc:
        raise ErroMapa([f"{caminho}: não foi possível calcular o hash ({exc})."]) from exc
    return hashlib.sha256(conteudo).hexdigest()


# -- sincronização ----------------------------------------------------------


def sincronizar_mapa(
    mapa: MapaMestre, manifesto: Manifesto, *, comando: str, hash_mapa: str
) -> dict:
    """Sincroniza o cadastro com o Manifesto (idempotente — AD-1) e registra evento."""
    agora = agora_iso()
    portais_criados: list[str] = []
    portais_atualizados: list[str] = []
    instituicoes_criadas: list[str] = []

    with manifesto.transacao() as conn:
        for instituicao in mapa.instituicao:
            existente = conn.execute(
                "SELECT id, nome FROM instituicoes WHERE sigla = ? COLLATE NOCASE",
                (instituicao.sigla,),
            ).fetchone()
            if existente is None:
                conn.execute(
                    "INSERT INTO instituicoes (sigla, nome, criado_em) VALUES (?, ?, ?)",
                    (instituicao.sigla, instituicao.nome, agora),
                )
                id_instituicao = conn.execute(
                    "SELECT id FROM instituicoes WHERE sigla = ? COLLATE NOCASE",
                    (instituicao.sigla,),
                ).fetchone()["id"]
                instituicoes_criadas.append(instituicao.sigla)
            else:
                id_instituicao = existente["id"]
                if existente["nome"] != instituicao.nome:
                    conn.execute(
                        "UPDATE instituicoes SET nome = ? WHERE id = ?",
                        (instituicao.nome, id_instituicao),
                    )

            for portal in instituicao.portal:
                registro = conn.execute(
                    """
                    SELECT instituicao_id, nome, categoria, dinamico, profundidade_maxima
                    FROM portais WHERE url = ?
                    """,
                    (portal.url,),
                ).fetchone()
                if registro is None:
                    conn.execute(
                        """
                        INSERT INTO portais (
                            instituicao_id, nome, categoria, url,
                            dinamico, profundidade_maxima, criado_em
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            id_instituicao,
                            portal.nome,
                            portal.categoria,
                            portal.url,
                            int(portal.dinamico),
                            portal.profundidade_maxima,
                            agora,
                        ),
                    )
                    portais_criados.append(portal.url)
                else:
                    mudou = (
                        registro["instituicao_id"] != id_instituicao
                        or registro["nome"] != portal.nome
                        or registro["categoria"] != portal.categoria
                        or bool(registro["dinamico"]) != portal.dinamico
                        or registro["profundidade_maxima"] != portal.profundidade_maxima
                    )
                    if mudou:
                        conn.execute(
                            """
                            UPDATE portais SET
                                instituicao_id = ?,
                                nome = ?,
                                categoria = ?,
                                dinamico = ?,
                                profundidade_maxima = ?
                            WHERE url = ?
                            """,
                            (
                                id_instituicao,
                                portal.nome,
                                portal.categoria,
                                int(portal.dinamico),
                                portal.profundidade_maxima,
                                portal.url,
                            ),
                        )
                        portais_atualizados.append(portal.url)

        manifesto.registrar_evento(
            tipo="mapa_sincronizado",
            comando=comando,
            detalhe={
                "mapa_sha256": hash_mapa,
                "instituicoes": len(mapa.instituicao),
                "portais": sum(len(i.portal) for i in mapa.instituicao),
                "instituicoes_criadas": instituicoes_criadas,
                "portais_criados": portais_criados,
                "portais_atualizados": portais_atualizados,
            },
        )

    return {
        "instituicoes": len(mapa.instituicao),
        "portais": sum(len(i.portal) for i in mapa.instituicao),
        "criados": portais_criados,
        "atualizados": portais_atualizados,
        "hash_mapa": hash_mapa,
    }
