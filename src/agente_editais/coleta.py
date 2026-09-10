"""Coleta de PDFs (CAP-4): Registro L1 nascido na captura, dedupe e retomada.

Governado por:
- AD-11: este é o ÚNICO estágio que cria EDITAL e DOCUMENTO; ``documento_id``
  = hash SHA-256 dos BYTES capturados; demais estágios só fazem UPDATE;
- AD-2: PDF é write-once — gravação em temporário com hash calculado no
  stream, move para o destino final e VERIFICA o hash depois de mover;
  bytes válidos nunca são reescritos. Reparo após corrupção põe os bytes
  danificados em quarentena (renomeio auditável ``<nome>.corrompido-<ts>-<u>``)
  antes de repor o caminho. Temporários vivem em ``corpus/.tmp/`` com o PID
  da execução no nome — a limpeza só remove os do PRÓPRIO processo.
- AD-8: dedupe por hash vale DENTRO do portal — mesmo conteúdo de outra URL
  do portal gera um registro-alias com ``referencia_para`` ao original;
  cruzar portais da mesma instituição é decisão humana (PRD OQ-4). A
  comparação de identidade é SEMPRE pelo hash COMPLETO, nunca pelo prefixo
  12-hex do id (colisão de prefixo não gera alias/restauração falsa), e o
  alias só nasce se os bytes do canônico existem e batem no hash.
- AD-5: TODA rede acontece VIA ``fetcher.baixar_stream`` (robots, delay por
  host, redirects hop-a-hop, cap de tamanho, sniff de %PDF- no 1º chunk);
  403 consecutivos SUSPENDEM o host por execução — o restante DO HOST é
  pulado, inclusive nos outros portais da mesma execução (--todos compartilha
  a suspensão); URLs puladas contam no relatório.

Retomada (AD-1): o estado do crawl vive no Manifesto. Candidato cuja URL já
tem documento íntegro no disco (hash conferido) é pulado sem rede; candidato
sem registro, ou cujos bytes locais não batem no hash registrado ("mudança
real"), é baixado de novo: hash igual ⇒ restauração do próprio documento
(com correção de caminho stale no L1 quando o destino recalculado difere);
hash diferente ⇒ nova versão ligada por ``predecessor_id``.

Ano provisório: padrão de ano (2019–2026) INEQUÍVOCO nos segmentos do
caminho da URL escolhe a pasta ``{instituicao}/{ano}/``; sem match confiável
— inclusive ano FORA da janela (ex.: 2027) — ⇒ ``_sem_ano/`` nesta fase. A
datação autoritativa (Story 5) confirma/move depois.

Falhas pontuais NUNCA abortam o lote: IntegrityError/OSError na gravação,
no rename/quarentena, no mkdir ou no pós-move hashing viram evento +
desfecho de erro daquele candidato; um ciclo A→B→A (linha já existente) vira
skip limpo. ``data_captura`` é gravada NORMALIZADA em UTC (chave de
ordenação da retomada).
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import sqlite3
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from . import __version__
from .fetcher import (
    Polidez,
    ResultadoDownload,
    baixar_stream,
    nova_sessao,
)
from .manifest import Manifesto, agora_iso_utc
from .mapa import Portal, hostname_de, normalizar_url

VERSAO_CRAWLER = __version__

_ANOS_JANELA = range(2019, 2027)
_RE_ANO = re.compile(r"(?<!\d)(20(?:1[9]|2[0-6]))(?!\d)")
_EXTENSAO_PDF = ".pdf"
_MAGICO_PDF = b"%PDF-"
_TAMANHO_SLUG = 80
_PASTA_SEM_ANO = "_sem_ano"
_PASTA_TMP = ".tmp"
_CHUNK_LEITURA = 1024 * 1024


class ErroVerificacaoCaptura(RuntimeError):
    """Hash pós-mover divergiu do hash do stream — captura não é confiável."""


def anos_janela_no_path(url: str) -> set[int]:
    """Todos os anos 2019–2026 encontrados nos SEGMENTOS DO CAMINHO da URL.

    Fonte única da semântica de ano-por-URL: varre apenas o PATH (hostname,
    porta e query ficam de fora — um subdomínio ``edital2023.if...`` não é
    data de publicação). Reusada pela datação (Story 5) para manter coleta e
    datação lendo a MESMA evidência da URL sem duplicar regex.
    """
    anos: set[int] = set()
    for segmento in urlsplit(url).path.split("/"):
        for achou in _RE_ANO.findall(unquote(segmento)):
            ano = int(achou)
            if ano in _ANOS_JANELA:
                anos.add(ano)
    return anos


def ano_provisorio_da_url(url: str) -> int | None:
    """Ano (2019–2026) quando INEQUÍVOCO nos segmentos do caminho; senão None.

    ``/2023/edital.pdf`` → 2023; ``/2021-1/x.pdf`` → 2021 (prefixo do
    segmento); dois anos distintos no caminho ⇒ ambíguo ⇒ None (Design Notes:
    a pasta vira ``_sem_ano/`` e a datação da Story 5 decide).
    """
    anos = anos_janela_no_path(url)
    return anos.pop() if len(anos) == 1 else None


def slug_de_url(url: str) -> str:
    """Slug do nome-base do arquivo na URL — pt-BR sem acento, delimitador '-'."""
    nome = posixpath.basename(unquote(urlsplit(url).path))
    if nome.lower().endswith(_EXTENSAO_PDF):
        nome = nome[: -len(_EXTENSAO_PDF)]
    decomposto = unicodedata.normalize("NFKD", nome)
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", sem_acento.lower()).strip("-")
    return slug[:_TAMANHO_SLUG] or "documento"


def pasta_do_documento(raiz_corpus: Path, sigla: str, ano_provisorio: int | None) -> Path:
    """``{raiz}/{sigla}/{ano|_sem_ano}/`` — estrutura Instituição/Ano (CAP-4)."""
    pasta = (
        raiz_corpus / sigla.upper() / (str(ano_provisorio) if ano_provisorio else _PASTA_SEM_ANO)
    )
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def hash_arquivo_local(caminho: Path) -> str:
    """SHA-256 de arquivo em disco, em chunks (verificação de integridade)."""
    hasher = hashlib.sha256()
    with caminho.open("rb") as entrada:
        for pedaco in iter(lambda: entrada.read(_CHUNK_LEITURA), b""):
            hasher.update(pedaco)
    return hasher.hexdigest()


@dataclass(slots=True)
class ContextoColeta:
    """Portal resolvido contra o Manifesto para uma execução de coleta."""

    instituicao_sigla: str
    portal: Portal
    portal_id: int


@dataclass(slots=True)
class ResumoColetaPortal:
    """Contagens da coleta de UM portal — vira saída CLI e evento."""

    instituicao_sigla: str
    portal_nome: str
    candidatos_pdf: int = 0
    baixados: int = 0
    novas_versoes: int = 0
    restaurados: int = 0
    aliases_duplicados: int = 0
    ja_integros: int = 0
    tamanho_excedido: int = 0
    conteudo_inesperado: int = 0
    falhas_download: int = 0
    bloqueios_robots: int = 0
    urls_invalidas: int = 0
    puladas_host_suspenso: int = 0
    hosts_suspensos: list[str] = field(default_factory=list)
    urls_perdidas: list[str] = field(default_factory=list)

    def totalizar(self) -> dict:
        return {
            "candidatos_pdf": self.candidatos_pdf,
            "baixados": self.baixados,
            "novas_versoes": self.novas_versoes,
            "restaurados": self.restaurados,
            "aliases_duplicados": self.aliases_duplicados,
            "ja_integros": self.ja_integros,
            "tamanho_excedido": self.tamanho_excedido,
            "conteudo_inesperado": self.conteudo_inesperado,
            "falhas_download": self.falhas_download,
            "bloqueios_robots": self.bloqueios_robots,
            "urls_invalidas": self.urls_invalidas,
            "puladas_host_suspenso": self.puladas_host_suspenso,
            "hosts_suspensos": list(self.hosts_suspensos),
            "urls_perdidas": list(self.urls_perdidas),
        }


def _bytes_integros(linha_documento, caminho: Path) -> bool:
    """True se o arquivo local existe E o SHA-256 dos bytes bate no L1."""
    try:
        if not caminho.is_file():
            return False
        return hash_arquivo_local(caminho) == linha_documento["hash_sha256"]
    except OSError:
        return False


def _quarentena(caminho: Path, manifesto: Manifesto, comando: str, motivo: str) -> str | None:
    """Afasta bytes inválidos do caminho canônico SEM apagá-los (custódia).

    Renomeia para ``<nome>.corrompido-<ts>-<uuid8>`` ao lado do original e
    registra evento; devolve o novo nome (ou None se nada havia lá). O sufixo
    uuid curto evita colisão de dois reparos no MESMO segundo (Windows não
    sobrescreve rename existente). OSError propaga — o chamador decide.
    """
    if not caminho.exists():
        return None
    carimbo = datetime.now().strftime("%Y%m%dT%H%M%S")
    destino = caminho.with_name(f"{caminho.name}.corrompido-{carimbo}-{uuid.uuid4().hex[:8]}")
    caminho.rename(destino)
    manifesto.registrar_evento(
        tipo="bytes_em_quarentena",
        comando=comando,
        detalhe={"caminho_original": str(caminho), "quarentena": destino.name, "motivo": motivo},
    )
    return destino.name


def _mover_com_verificacao(temporario: Path, destino: Path, hash_esperado: str) -> None:
    """AD-2: move temp→destino e RECONFERE o hash dos bytes gravados."""
    os.replace(temporario, destino)
    if hash_arquivo_local(destino) != hash_esperado:
        destino.unlink(missing_ok=True)
        raise ErroVerificacaoCaptura(
            f"hash pós-mover divergiu em {destino} (esperado {hash_esperado[:12]}…)"
        )


def coletar_portal(
    contexto: ContextoColeta,
    manifesto: Manifesto,
    polidez: Polidez,
    *,
    raiz_corpus: Path,
    comando: str = "coletar",
    sessao: requests.Session | None = None,
    hosts_suspensos_execucao: list[str] | None = None,
    urls_restritas: set[str] | None = None,
) -> ResumoColetaPortal:
    """Coleta os candidatos PDF de UM portal (lote idempotente, AD-1).

    Itera ``candidatos WHERE tipo='pdf'`` em ordem estável; cada documento
    concluído é uma transação própria — interrupção retoma exatamente dali.
    Host com 403 persistente suspende o RESTANTE DO HOST por execução:
    ``hosts_suspensos_execucao`` é a lista COMPARTILHADA entre todos os
    portais da mesma execução (--todos) — candidatos de outros portais no
    mesmo hostname são pulados sem rede e CONTAM no relatório
    (``puladas_host_suspenso`` + ``urls_perdidas``). ``urls_restritas``
    limita o lote a um recorte de URLs (ex.: ``--varredura``) — as contagens
    do resumo refletem SOMENTE o recorte.
    """
    rotulo = {"instituicao": contexto.instituicao_sigla, "portal": contexto.portal.nome}
    resumo = ResumoColetaPortal(contexto.instituicao_sigla, contexto.portal.nome)
    suspensos_da_execucao = (
        hosts_suspensos_execucao
        if hosts_suspensos_execucao is not None
        else resumo.hosts_suspensos
    )

    propria = sessao is None
    if propria:
        sessao = nova_sessao(polidez.user_agent)
    assert sessao is not None

    try:
        candidatos = manifesto.candidatos_pdf_do_portal(contexto.portal_id)
        if urls_restritas is not None:
            candidatos = [c for c in candidatos if c["url"] in urls_restritas]
        resumo.candidatos_pdf = len(candidatos)
        tmp_dir = Path(raiz_corpus) / _PASTA_TMP
        tmp_dir.mkdir(parents=True, exist_ok=True)

        for candidato in candidatos:
            url_bruta = candidato["url"]
            try:
                url = normalizar_url(url_bruta)
            except ValueError as exc:
                resumo.urls_invalidas += 1
                resumo.urls_perdidas.append(url_bruta)
                manifesto.registrar_evento(
                    tipo="candidato_url_invalida",
                    comando=comando,
                    detalhe={**rotulo, "url": url_bruta, "erro": str(exc)},
                )
                continue

            host_atual = hostname_de(url)
            if host_atual in suspensos_da_execucao:
                # restante DO HOST pulado nesta execução — URL conta no relatório
                resumo.puladas_host_suspenso += 1
                resumo.urls_perdidas.append(url)
                continue

            desfecho = _processar_candidato(
                manifesto,
                polidez,
                raiz_corpus,
                contexto.instituicao_sigla,
                url,
                portal_id=contexto.portal_id,
                comando=comando,
                sessao=sessao,
                tmp_dir=tmp_dir,
            )
            _acumular(resumo, desfecho, url, host_atual)

            if desfecho.desfecho == "host_suspenso":
                resumo.hosts_suspensos.append(host_atual)
                if host_atual not in suspensos_da_execucao:
                    suspensos_da_execucao.append(host_atual)
                manifesto.registrar_evento(
                    tipo="host_suspenso",
                    comando=comando,
                    detalhe={
                        **rotulo,
                        "host": host_atual,
                        "url_gatilho": url,
                        "limite_403_consecutivos": desfecho.contador_403,
                        "retomada": "proxima_execucao",
                    },
                )
    finally:
        if propria:
            sessao.close()

    manifesto.registrar_evento(
        tipo="coleta_portal_concluida",
        comando=comando,
        detalhe={**rotulo, **resumo.totalizar()},
    )
    return resumo


@dataclass(slots=True)
class DesfechoCandidato:
    """Resultado unitário de um candidato — agrega no resumo do portal."""

    desfecho: str  # baixado|nova_versao|restaurado|alias|skip|...
    contador_403: int = 0


def _acumular(
    resumo: ResumoColetaPortal, desfecho: DesfechoCandidato, url: str, host: str
) -> None:
    tipo = desfecho.desfecho
    if tipo == "baixado":
        resumo.baixados += 1
    elif tipo == "nova_versao":
        resumo.novas_versoes += 1
    elif tipo == "restaurado":
        resumo.restaurados += 1
    elif tipo == "alias":
        resumo.aliases_duplicados += 1
    elif tipo == "skip":
        resumo.ja_integros += 1
    elif tipo == "tamanho_excedido":
        resumo.tamanho_excedido += 1
        resumo.urls_perdidas.append(url)
    elif tipo == "conteudo_inesperado":
        resumo.conteudo_inesperado += 1
        resumo.urls_perdidas.append(url)
    elif tipo == "falha_download":
        resumo.falhas_download += 1
        resumo.urls_perdidas.append(url)
    elif tipo == "robots_bloqueado":
        resumo.bloqueios_robots += 1
        resumo.urls_perdidas.append(url)
    elif tipo == "host_suspenso":
        # sem contador próprio: o gatilho é tratado pelo chamador
        # (coletar_portal registra o host e pula o restante DO HOST)
        pass
    else:
        raise AssertionError(
            f"desfecho de candidato desconhecido: {tipo!r} "
            "(todo desfecho novo precisa de uma entrada em _acumular)"
        )


def _processar_candidato(
    manifesto: Manifesto,
    polidez: Polidez,
    raiz_corpus: Path,
    sigla: str,
    url: str,
    *,
    portal_id: int,
    comando: str,
    sessao: requests.Session,
    tmp_dir: Path,
) -> DesfechoCandidato:
    """Ciclo completo de UM candidato — função pura o bastante p/ retomada.

    Ordem deliberada (kill-safe): verificação local sem rede → download para
    temporário → decisões de dedupe/restauração → transação única que grava
    linha(s) + evento. Um crash em qualquer ponto deixa estado recuperável:
    ou a transação fechou (skip na próxima), ou nada foi gravado (refaz).
    """
    rotulo = {"url": url}

    # -- retomada: trabalho já concluído? (verificação 100% local) ----------
    anterior = manifesto.ultimo_documento_da_url(url)
    if anterior is not None and _bytes_integros(anterior, Path(anterior["caminho"])):
        return DesfechoCandidato("skip")

    # -- download VIA fetcher (polidez integral, AD-5) -----------------------
    _fd, nome_tmp = tempfile_mkstemp(tmp_dir)
    temporario = Path(nome_tmp)
    try:
        resultado = baixar_stream(
            url,
            temporario,
            polidez,
            max_bytes=int(polidez.max_mb_documento * 1024 * 1024),
            sessao=sessao,
        )

        if resultado.bloqueio_robots:
            manifesto.registrar_evento(tipo="robots_bloqueio", comando=comando, detalhe=rotulo)
            return DesfechoCandidato("robots_bloqueado")

        if resultado.excedeu_cap:
            manifesto.registrar_evento(
                tipo="tamanho_excedido",
                comando=comando,
                detalhe={
                    **rotulo,
                    "bytes_baixados": resultado.bytes_baixados,
                    "max_mb_documento": polidez.max_mb_documento,
                },
            )
            return DesfechoCandidato("tamanho_excedido")

        # -- conteúdo não-PDF nunca vira Documento (I/O matrix): o fetcher
        # faz o SNIFF do 1º chunk e devolve ok=False + conteudo_inesperado
        # sem gravar o corpo — precisa ser tratado ANTES da falha genérica
        if resultado.conteudo_inesperado:
            manifesto.registrar_evento(
                tipo="conteudo_inesperado",
                comando=comando,
                detalhe={
                    **rotulo,
                    "status_http": resultado.status_http,
                    "bytes_baixados": resultado.bytes_baixados,
                    "content_type": resultado.content_type,
                },
            )
            return DesfechoCandidato("conteudo_inesperado")

        if not resultado.ok or resultado.hash_sha256 is None:
            if (
                resultado.status_http == 403
                and resultado.contador_403 >= polidez.max_403_consecutivos
            ):
                return DesfechoCandidato("host_suspenso", contador_403=resultado.contador_403)
            manifesto.registrar_evento(
                tipo="download_falha",
                comando=comando,
                detalhe={
                    **rotulo,
                    "status_http": resultado.status_http,
                    "erro": resultado.erro,
                },
            )
            return DesfechoCandidato("falha_download")

        # -- conteúdo não-PDF nunca vira Documento (I/O matrix) --------------
        with temporario.open("rb") as cabeca:
            if cabeca.read(len(_MAGICO_PDF)) != _MAGICO_PDF:
                manifesto.registrar_evento(
                    tipo="conteudo_inesperado",
                    comando=comando,
                    detalhe={
                        **rotulo,
                        "status_http": resultado.status_http,
                        "bytes_baixados": resultado.bytes_baixados,
                    },
                )
                return DesfechoCandidato("conteudo_inesperado")

        return _registrar_captura(
            manifesto,
            raiz_corpus,
            sigla,
            url,
            portal_id=portal_id,
            comando=comando,
            resultado=resultado,
            anterior=anterior,
            temporario=temporario,
        )
    finally:
        try:
            temporario.unlink(missing_ok=True)
        except OSError:
            pass  # limpeza best-effort — nunca derruba o lote por lixo de tmp


def _registrar_captura(
    manifesto: Manifesto,
    raiz_corpus: Path,
    sigla: str,
    url: str,
    *,
    portal_id: int,
    comando: str,
    resultado: ResultadoDownload,
    anterior,
    temporario: Path,
) -> DesfechoCandidato:
    """Decide restauração × alias × nova versão × captura nova e grava.

    Ordem deliberada dos branches (revisão):
    1. O histórico da PRÓPRIA URL decide primeiro — hash igual ao registro
       anterior é restauração; hash diferente é mudança real (nova versão via
       ``predecessor_id``), MESMO que outro candidato do portal já tenha os
       mesmos bytes (nunca vira alias);
    2. a identidade é comparada pelo hash SHA-256 COMPLETO, nunca pelo
       prefixo 12-hex do id (colisão de prefixo ≠ mesmo documento);
    3. alias só nasce se o canônico existe no disco e bate no hash — canônico
       ausente/corrompido segue o caminho normal de captura/restauração.
    ``data_captura`` é gravada em UTC (chave de ordenação da retomada).
    Falhas NUNCA abortam o lote: IntegrityError (linha já existente, ciclo
    A→B→A) vira skip limpo; OSError/OSError-verificação viram evento +
    desfecho de erro do candidato.
    """
    hash_hex = resultado.hash_sha256
    if hash_hex is None:  # só chega aqui com download completo — guarda barata
        return DesfechoCandidato("falha_download")
    documento_id = hash_hex[:12]
    agora = agora_iso_utc()

    linha_instituicao = manifesto.consultar(
        "SELECT id FROM instituicoes WHERE sigla = ? COLLATE NOCASE", (sigla,)
    )
    if not linha_instituicao:
        # sigla ausente ⇒ FK NULL no INSERT de editais seria crash — desfecho
        # de erro com evento, e o lote segue
        manifesto.registrar_evento(
            tipo="instituicao_ausente",
            comando=comando,
            detalhe={"url": url, "sigla": sigla},
        )
        return DesfechoCandidato("falha_download")
    instituicao_id = int(linha_instituicao[0]["id"])

    try:
        # -- restauração: re-captura com a MESMA identidade registrada — os
        # bytes locais estavam ausentes/corrompidos; comparação por HASH
        # COMPLETO (não pelo prefixo do id). O L1 permanece; se o destino
        # recalculado difere do caminho registrado (path stale), o caminho é
        # atualizado — retomada nunca re-baixa eternamente.
        if anterior is not None and anterior["hash_sha256"] == hash_hex:
            _quarentena(Path(anterior["caminho"]), manifesto, comando, "corrompido")
            ano = ano_provisorio_da_url(url)
            pasta = pasta_do_documento(Path(raiz_corpus), sigla, ano)
            destino = pasta / f"{documento_id}-{slug_de_url(url)}{_EXTENSAO_PDF}"
            _mover_com_verificacao(temporario, destino, hash_hex)
            caminho_recalculado = str(destino)
            caminho_corrigido = caminho_recalculado != anterior["caminho"]
            if caminho_corrigido:
                with manifesto.transacao() as conn:
                    conn.execute(
                        "UPDATE documentos SET caminho = ? WHERE id = ? AND url_origem = ?",
                        (caminho_recalculado, anterior["id"], anterior["url_origem"]),
                    )
            manifesto.registrar_evento(
                tipo="documento_restaurado",
                comando=comando,
                detalhe={
                    "url": url,
                    "documento_id": documento_id,
                    "caminho": caminho_recalculado,
                    "caminho_anterior": anterior["caminho"],
                    "caminho_corrigido": caminho_corrigido,
                    "predecessor_id": anterior["id"],
                    "data_captura_original": anterior["data_captura"],
                },
            )
            return DesfechoCandidato("restaurado")

        # -- alias intra-portal: mesmos bytes (hash completo), outra URL, sem
        # registro próprio ainda E com os bytes do canônico ÍNTEGROS no disco;
        # canônico ausente/corrompido ⇒ segue para captura/restauração normal
        if anterior is None:
            duplicado = manifesto.documento_mesmo_hash_no_portal(
                portal_id, hash_hex, exceto_url=url
            )
            if duplicado is not None and _bytes_integros(duplicado, Path(duplicado["caminho"])):
                with manifesto.transacao() as conn:
                    conn.execute(
                        """
                        INSERT INTO documentos (
                            id, edital_id, url_origem, caminho, hash_sha256,
                            data_captura, ano_provisorio, versao_crawler, referencia_para
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            documento_id,
                            duplicado["edital_id"],
                            url,
                            duplicado["caminho"],
                            hash_hex,
                            agora,
                            duplicado["ano_provisorio"],
                            VERSAO_CRAWLER,
                            duplicado["id"],
                        ),
                    )
                manifesto.registrar_evento(
                    tipo="documento_alias_duplicado",
                    comando=comando,
                    detalhe={
                        "url": url,
                        "documento_id": documento_id,
                        "referencia_para": duplicado["id"],
                        "url_referenciada": duplicado["url_origem"],
                    },
                )
                return DesfechoCandidato("alias")

        # -- captura nova OU nova versão (mesma URL, bytes diferentes ⇒
        # mudança real), mesmo que outro candidato do portal tenha esses bytes
        ano = ano_provisorio_da_url(url)
        pasta = pasta_do_documento(Path(raiz_corpus), sigla, ano)
        slug = slug_de_url(url)
        destino = pasta / f"{documento_id}-{slug}{_EXTENSAO_PDF}"
        predecessor_id = anterior["id"] if anterior is not None else None
        edital_id = f"{sigla.lower()}-{ano if ano else _PASTA_SEM_ANO}-{slug}"
        tipo_evento = "nova_versao_documento" if predecessor_id else "documento_baixado"

        _mover_com_verificacao(temporario, destino, hash_hex)

        with manifesto.transacao() as conn:
            existe_edital = conn.execute(
                "SELECT 1 FROM editais WHERE id = ?", (edital_id,)
            ).fetchone()
            if existe_edital is None:
                conn.execute(
                    """
                    INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
                    VALUES (?, ?, ?, ?)
                    """,
                    (edital_id, instituicao_id, ano, agora),
                )
            conn.execute(
                """
                INSERT INTO documentos (
                    id, edital_id, url_origem, caminho, hash_sha256, data_captura,
                    ano_provisorio, versao_crawler, predecessor_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    documento_id,
                    edital_id,
                    url,
                    str(destino),
                    hash_hex,
                    agora,
                    ano,
                    VERSAO_CRAWLER,
                    predecessor_id,
                ),
            )
        manifesto.registrar_evento(
            tipo=tipo_evento,
            comando=comando,
            detalhe={
                "url": url,
                "documento_id": documento_id,
                "edital_id": edital_id,
                "caminho": str(destino),
                "hash_sha256": hash_hex,
                "bytes": resultado.bytes_baixados,
                "ano_provisorio": ano,
                "versao_crawler": VERSAO_CRAWLER,
                "predecessor_id": predecessor_id,
                "status_http": resultado.status_http,
                "duracao_s": round(resultado.duracao_s, 3),
            },
        )
        return DesfechoCandidato("nova_versao" if predecessor_id else "baixado")
    except sqlite3.IntegrityError as exc:
        # ciclo A→B→A: a linha (id, url_origem) já existia — skip limpo, o
        # trabalho está feito; nada de abortar o lote
        manifesto.registrar_evento(
            tipo="captura_registro_existente",
            comando=comando,
            detalhe={"url": url, "documento_id": documento_id, "erro": str(exc)},
        )
        return DesfechoCandidato("skip")
    except ErroVerificacaoCaptura as exc:
        manifesto.registrar_evento(
            tipo="captura_falha_verificacao",
            comando=comando,
            detalhe={"url": url, "erro": str(exc)},
        )
        return DesfechoCandidato("falha_download")
    except OSError as exc:
        # rename/quarentena, mkdir ou pós-move hashing falharam — evento +
        # desfecho de erro DESTE candidato; o lote continua
        manifesto.registrar_evento(
            tipo="captura_falha_io",
            comando=comando,
            detalhe={"url": url, "erro": f"{type(exc).__name__}: {exc}"},
        )
        return DesfechoCandidato("falha_download")


def tempfile_mkstemp(diretorio: Path) -> tuple[int, str]:
    """Temporário DENTRO do volume do corpus — rename atômico no mover.

    O descritor é FECHADO na hora via with-statement (no Windows, arquivo
    aberto bloqueia o ``open('wb')`` do baixador — PermissionError de
    compartilhamento). O nome embute o PID da execução:
    ``captura-<pid>-<aleatório>.pdf`` — a limpeza de órfãos só remove
    temporários do PRÓPRIO processo.
    """
    diretorio.mkdir(parents=True, exist_ok=True)
    fd, nome = tempfile.mkstemp(prefix=f"captura-{os.getpid()}-", suffix=".pdf", dir=diretorio)
    with os.fdopen(fd, "wb"):
        pass  # fecha o descritor imediatamente
    return fd, nome


def limpar_temporarios(raiz_corpus: Path) -> None:
    """Remove temporários órfãos DESTE processo (best-effort).

    Só toca em arquivos ``captura-<pid>-*`` do PRÓPRIO pid: uma execução
    concorrente tem os seus — removê-los seria corromper o lote alheio.
    Temporários de processos mortos ficam (limpeza manual/documentada).
    """
    pasta = Path(raiz_corpus) / _PASTA_TMP
    if not pasta.is_dir():
        return
    for sobra in pasta.glob(f"captura-{os.getpid()}-*"):
        try:
            sobra.unlink(missing_ok=True)
        except OSError:
            pass  # nada de abortar o lote por lixo de tmp
