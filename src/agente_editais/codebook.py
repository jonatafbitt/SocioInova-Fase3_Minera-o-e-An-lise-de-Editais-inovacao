"""Codebook (CAP-7) — carga e validação de ``configs/codebook.yaml``.

Governado por:
- AD-9: config declarativa versionada em git; cada lote L2 registra o
  ``codebook_sha256`` — alterar o arquivo muda o hash ⇒ novo instrumento
  (novo lote). O CONTEÚDO CIENTÍFICO (dimensões, campos, escalas, âncoras)
  é do(a) pesquisador(a); este módulo só valida a ESTRUTURA.
- AD-6: os TRÊS PHASE-GATES do L2 moram no próprio arquivo porque o
  congelamento É uma propriedade dele: ``congelamento`` completo
  (`congelado_em` + `congelado_por`), ``trietica_dahlin.resolvida == true``
  (OQ-6) e ``acordo_humano_maquina.limiar_kappa`` fixado a priori (OQ-5).
  ``gates_pendentes()`` nomeia os ausentes; o comando ``analise`` recusa
  rodar com qualquer gate pendente.

Moldado em ``mapa.carregar_mapa``: erros saem como ``ErroCodebook`` com uma
lista de problemas ACIONÁVEIS, cada um citando o caminho e a(s) linha(s) do
YAML. Modelos pydantic com ``extra="forbid"`` — chave desconhecida é erro,
nunca silêncio.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SCHEMA_VERSION_SUPORTADA = 1

_RE_ID = re.compile(r"[a-z0-9_]+")


class ErroCodebook(ValueError):
    """Codebook inválido; ``problemas`` traz uma linha acionável por erro."""

    def __init__(self, problemas: list[str]) -> None:
        self.problemas = problemas
        super().__init__("\n".join(problemas))


class Congelamento(BaseModel):
    """Bloco do phase-gate de congelamento — preenchido pelo(a) pesquisador(a)."""

    model_config = ConfigDict(extra="forbid")

    congelado_em: str | None = None
    congelado_por: str | None = None

    @field_validator("congelado_em")
    @classmethod
    def _data_iso_valida(cls, valor: str | None) -> str | None:
        """ISO-8601 parseável quando presente; vazio vira None (gate pendente).

        ``'banana'`` é recusado AQUI (erro de carga), não no momento do lote:
        congelamento com data malformada é config inválida, nunca estado
        "quase congelado".
        """
        if valor is None or not valor.strip():
            return None
        texto = valor.strip()
        try:
            datetime.fromisoformat(texto)
        except ValueError as exc:
            raise ValueError(
                "'congelado_em' deve ser data/hora ISO 8601 parseável "
                f"(ex.: 2026-09-01T10:00:00-03:00); recebido {valor!r}"
            ) from exc
        return texto

    @field_validator("congelado_por")
    @classmethod
    def _autoria_nao_vazia(cls, valor: str | None) -> str | None:
        if valor is None or not valor.strip():
            return None
        return valor.strip()


DECISOES_DAHLIN: tuple[str, ...] = ("incorporada", "excluida_justificada")


class TrieticaDahlin(BaseModel):
    """Bloco da Tríade de Dahlin (OQ-6) — decisão registrada por escrito."""

    model_config = ConfigDict(extra="forbid")

    resolvida: bool
    decisao: str | None = None
    justificativa: str | None = None

    @model_validator(mode="after")
    def _decisao_coerente(self) -> "TrieticaDahlin":
        decisao = self.decisao.strip() if isinstance(self.decisao, str) else None
        justificativa = self.justificativa.strip() if isinstance(self.justificativa, str) else None
        decisao = decisao or None
        justificativa = justificativa or None
        if self.resolvida and decisao not in DECISOES_DAHLIN:
            raise ValueError(
                "trietica_dahlin resolvida exige 'decisao' em "
                f"{', '.join(DECISOES_DAHLIN)}; recebido {self.decisao!r}"
            )
        # exclusão precisa estar JUSTIFICADA EM ALGUM LUGAR — mesmo quando a
        # resolução ainda não foi marcada (estado intermediário de preenchimento)
        if decisao == "excluida_justificada" and not justificativa:
            raise ValueError("decisao 'excluida_justificada' exige 'justificativa' não vazia")
        if not self.resolvida and not justificativa:
            raise ValueError(
                "trietica_dahlin não resolvida exige 'justificativa' registrando "
                "onde/por que a decisão segue pendente (OQ-6)"
            )
        self.decisao = decisao
        self.justificativa = justificativa
        return self


class AcordoHumanoMaquina(BaseModel):
    """Bloco do limiar de Acordo Humano-Máquina (OQ-5), fixado a priori."""

    model_config = ConfigDict(extra="forbid")

    limiar_kappa: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    nota: str | None = None


class Campo(BaseModel):
    """Um campo codificável com escala fechada e âncoras de decisão."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    nome: str = Field(min_length=1)
    escala: Literal["ordinal", "nominal"]
    definicao_operacional: str = Field(min_length=1)
    regra_decisao: str = Field(min_length=1)
    permite_na: bool
    valores: list[int] | None = None
    opcoes: list[str] | None = None
    ancoras_positivas: list[str] = Field(min_length=1)
    ancoras_negativas: list[str] = Field(min_length=1)
    regra_boilerplate: str = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _id_seguro(cls, valor: str) -> str:
        # o id vira coluna ``campo`` no catálogo e chave do JSON esperado do
        # provedor: charset restrito impede chaves ambíguas/vazias.
        if not _RE_ID.fullmatch(valor):
            raise ValueError(
                "id deve casar com [a-z0-9_]+ — minúsculas, dígitos e "
                "sublinhado (ele nomeia o campo no catálogo L2)"
            )
        return valor

    @model_validator(mode="after")
    def _escala_consistente(self) -> "Campo":
        if self.escala == "ordinal":
            if not self.valores:
                raise ValueError(f"campo ordinal '{self.id}' exige 'valores' (ex.: [0, 1, 2])")
            if len(set(self.valores)) != len(self.valores):
                raise ValueError(f"campo ordinal '{self.id}' tem valores repetidos em 'valores'")
            if any(isinstance(v, bool) for v in self.valores):
                raise ValueError(f"campo ordinal '{self.id}' aceita apenas inteiros")
            if self.opcoes is not None:
                raise ValueError(
                    f"campo ordinal '{self.id}' não usa 'opcoes' (isso é de escala nominal)"
                )
        else:
            if not self.opcoes or any(not o.strip() for o in self.opcoes):
                raise ValueError(f"campo nominal '{self.id}' exige 'opcoes' não vazias")
            if len(set(self.opcoes)) != len(self.opcoes):
                raise ValueError(f"campo nominal '{self.id}' tem opções repetidas em 'opcoes'")
            if self.valores is not None:
                raise ValueError(
                    f"campo nominal '{self.id}' não usa 'valores' (isso é de escala ordinal)"
                )
        return self

    def valor_valido(self, valor: object) -> bool:
        """True se ``valor`` (já decodificado do JSON) pertence à escala."""
        if valor == "N/A":
            return self.permite_na
        if self.escala == "ordinal":
            return (
                isinstance(valor, int)
                and not isinstance(valor, bool)
                and valor in (self.valores or [])
            )
        return isinstance(valor, str) and valor in (self.opcoes or [])


class Dimensao(BaseModel):
    """Agrupamento conceitual de campos (uma entrada do Quadro Analítico)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    nome: str = Field(min_length=1)
    definicao_operacional: str = Field(min_length=1)
    campos: list[Campo] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _id_seguro(cls, valor: str) -> str:
        if not _RE_ID.fullmatch(valor):
            raise ValueError("id deve casar com [a-z0-9_]+")
        return valor


class Codebook(BaseModel):
    """Contrato completo do catálogo L2 + os três phase-gates (AD-6)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION_SUPORTADA)
    precedencia_intra_edital: str = Field(min_length=1)
    dimensoes: list[Dimensao] = Field(min_length=1)
    congelamento: Congelamento
    trietica_dahlin: TrieticaDahlin
    acordo_humano_maquina: AcordoHumanoMaquina

    def campos(self) -> list[Campo]:
        """Campos na ordem do arquivo — contrato exato exigido do provedor."""
        return [campo for dimensao in self.dimensoes for campo in dimensao.campos]

    def ids_de_campos(self) -> list[str]:
        return [campo.id for campo in self.campos()]

    def gates_pendentes(self) -> list[str]:
        """Gates do L2 ainda abertos, NOMEADOS (mensagem acionável, AD-6)."""
        pendentes: list[str] = []
        if not self.congelamento.congelado_em or not self.congelamento.congelado_por:
            pendentes.append(
                "gate 'congelamento' pendente: preencha 'congelado_em' "
                "(data ISO 8601) e 'congelado_por' (autoria) no codebook.yaml "
                "e faça commit antes do primeiro lote L2 (phase-gate AD-6)"
            )
        if self.trietica_dahlin.resolvida is not True:
            pendentes.append(
                "gate 'trietica_dahlin' pendente: 'resolvida' deve ser true "
                "(OQ-6 — registre a decisão com justificativa junto ao(à) "
                "orientador(a))"
            )
        if self.acordo_humano_maquina.limiar_kappa is None:
            pendentes.append(
                "gate 'acordo_humano_maquina' pendente: 'limiar_kappa' ausente "
                "(OQ-5 — fixe o limiar a priori POR ESCRITO; assumido κ ≥ 0,75)"
            )
        return pendentes


# -- carga e validação --------------------------------------------------------


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


def _linhas_do_valor(texto: str, valor: object) -> list[int]:
    """Localiza o valor como escalar YAML entre aspas; cai para busca solta."""
    exatas = [n for n, linha in enumerate(texto.splitlines(), start=1) if f"'{valor}'" in linha]
    return exatas if exatas else _linhas_com(texto, str(valor))


def _linhas_formatadas(linhas: list[int]) -> str:
    return ", ".join(str(n) for n in linhas)


def _validacoes_semanticas(codebook: Codebook, texto_bruto: str, caminho: Path) -> None:
    problemas: list[str] = []

    if codebook.schema_version != SCHEMA_VERSION_SUPORTADA:
        problemas.append(
            f"{caminho}: campo 'schema_version' = {codebook.schema_version} não "
            f"suportado (esperado {SCHEMA_VERSION_SUPORTADA}); atualize o agente "
            "ou o arquivo."
        )

    vistas_dimensoes: dict[str, int] = {}
    ids_de_campos: dict[str, tuple[str, int]] = {}
    for i, dimensao in enumerate(codebook.dimensoes):
        if dimensao.id in vistas_dimensoes:
            linhas = sorted(_linhas_do_valor(texto_bruto, dimensao.id))
            onde = f", linha(s) {_linhas_formatadas(linhas)}" if linhas else ""
            problemas.append(
                f"{caminho}{onde}: id de dimensão '{dimensao.id}' duplicado "
                f"(dimensoes[{vistas_dimensoes[dimensao.id]}] e dimensoes[{i}]); "
                "cada dimensão deve ter id único."
            )
        else:
            vistas_dimensoes[dimensao.id] = i
        for j, campo in enumerate(dimensao.campos):
            if campo.id in ids_de_campos:
                origem, k = ids_de_campos[campo.id]
                linhas = sorted(_linhas_do_valor(texto_bruto, campo.id))
                onde = f", linha(s) {_linhas_formatadas(linhas)}" if linhas else ""
                problemas.append(
                    f"{caminho}{onde}: id de campo '{campo.id}' duplicado "
                    f"({origem}.campos[{k}] e dimensoes[{i}].campos[{j}]); "
                    "ids de campos são chaves do catálogo L2 e devem ser únicos."
                )
            else:
                ids_de_campos[campo.id] = (f"dimensoes[{i}]", j)

    if problemas:
        raise ErroCodebook(problemas)


def carregar_codebook_de_bytes(conteudo: bytes, caminho: Path) -> Codebook:
    """Carrega e valida o codebook a partir de bytes JÁ LIDOS pelo chamador.

    Núcleo do TOCTOU: quem precisa do hash do instrumento (o comando
    ``analise``) lê os bytes UMA vez, parseia AQUI e hasheia OS MESMOS bytes —
    o ``codebook_sha256`` registrado no lote é exatamente o arquivo executado,
    mesmo se o disco mudar entre leitura e hash.
    """
    caminho = Path(caminho)
    try:
        texto_bruto = conteudo.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ErroCodebook([f"{caminho}: o conteúdo não é UTF-8 válido ({exc})."]) from exc

    try:
        dados = yaml.safe_load(texto_bruto)
    except yaml.YAMLError as exc:
        mensagem = str(exc)
        achou = re.search(r"line (\d+)", mensagem)
        onde = f", linha {achou.group(1)}" if achou else ""
        raise ErroCodebook(
            [f"{caminho}{onde}: YAML malformado — {mensagem.splitlines()[0]}"]
        ) from exc

    if not isinstance(dados, dict):
        raise ErroCodebook(
            [
                f"{caminho}: o conteúdo raiz deve ser um mapeamento YAML "
                f"(recebido {type(dados).__name__})."
            ]
        )

    try:
        codebook = Codebook.model_validate(dados)
    except ValidationError as exc:
        problemas = []
        for erro in exc.errors():
            local = _formatar_localizacao(erro["loc"])
            entrada = erro.get("input")
            # candidatos de localização, do mais específico ao mais genérico:
            # o VALOR recebido pode ser curto demais ("1") e casar com linhas
            # alheias — o NOME do campo costuma ser único no arquivo. Vence o
            # candidato com MENOS linhas (match mais estreito).
            candidatos: list[str] = []
            if isinstance(entrada, (str, int, float, bool)):
                candidatos.append(str(entrada))
            if erro["loc"]:
                candidatos.append(str(erro["loc"][-1]))
            linhas: list[int] = []
            for candidato in candidatos:
                achou_linhas: list[int] = _linhas_do_valor(texto_bruto, candidato)
                if achou_linhas and (not linhas or len(achou_linhas) < len(linhas)):
                    linhas = achou_linhas
            onde = f", linha {_linhas_formatadas(linhas)}" if linhas else ""
            problemas.append(
                f"{caminho}{onde}: campo '{local}' — {erro['msg']}"
                + (f" (valor recebido: {entrada!r})" if entrada is not None else "")
            )
        raise ErroCodebook(problemas) from exc

    _validacoes_semanticas(codebook, texto_bruto, caminho)
    return codebook


def carregar_codebook(caminho: Path) -> Codebook:
    """Carrega e valida ``codebook.yaml``; levanta ``ErroCodebook`` se inválido."""
    caminho = Path(caminho)
    try:
        conteudo = caminho.read_bytes()
    except OSError as exc:
        raise ErroCodebook([f"{caminho}: não foi possível ler o arquivo ({exc})."]) from exc
    return carregar_codebook_de_bytes(conteudo, caminho)


def hash_de_bytes(conteudo: bytes) -> str:
    """SHA-256 dos bytes do codebook (AD-9) — par de ``carregar_codebook_de_bytes``."""
    return hashlib.sha256(conteudo).hexdigest()
