"""CLI — superfície do pipeline em lotes (AD-1): um comando por execução.

Subcomandos desta story: ``mapa validar``, ``preflight``, ``descobrir``,
``coletar``, ``textuar``, ``datar``, ``fila listar|decidir``, ``consultar``,
``custodia``, ``analise`` e ``status``.

Códigos de saída:
- 0  sucesso (inclusive pré-voo com seeds inacessíveis, descoberta, coleta,
       textuação, datação e ANÁLISE com falhas por portal/edital/documento —
       o lote segue, FR-2/CAP-4/CAP-6/CAP-3/CAP-7; ``consultar``/``custodia``
       com zero resultados);
- 1  erro operacional genérico: Manifesto inexistente no ``status``/
       ``consultar``/``custodia``, Manifesto mais novo que o agente, falha de
       abertura do banco, violação de janela off-peak (pré-voo exigente OU
       crawling), sigla desconhecida no ``descobrir``/``coletar``/``textuar``/
       ``datar``/``analise`` ou portal ausente do Manifesto; item de fila
       inexistente ou já resolvido no ``fila decidir``; edital inexistente no
       ``custodia``; falha de escrita do CSV/JSON no ``consultar``/
       ``custodia``;
- 2  configuração declarativa inválida — compartilhado entre mapa-mestre.toml,
       politeness.toml e CODEBOOK.YAML (nada é escrito; banco intocado) —
       inclui os PHASE-GATES do L2 (congelamento/Dahlin/κ ausentes no
       codebook.yaml, AD-6; o ``analise`` recusa ANTES de tocar o provedor)
       — ou flags malformadas: --portal/--todos dos comandos de lote, decisão
       inválida no ``fila decidir`` (sem justificativa/autoria, destino
       ausente ou duplo, ano fora da janela 2019–2026), ``--status`` inválido
       no ``fila listar``, filtros malformados no ``consultar``
       (--instituicao vazia, --categoria fora do CHECK do banco, --ano fora
       de 2019–2026), ``--edital`` vazio no ``custodia`` e flags do
       ``analise`` (--modelo/LLM_MODELO ausente, --temperatura fora de
       [0, 2], --tentativas < 1) — todos validados ANTES de abrir o banco;
       ``--saida`` apontando para o próprio Manifesto no
       ``consultar``/``custodia`` (o export truncaria o banco);
- 3  engine SQLite abaixo do guard AD-10;
- 4  Manifesto ocupado por outro processo (lock AD-3, no startup OU na gravação).
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import typer

from . import __version__, llm_adapter
from .analise import (
    PROMPT_VERSAO,
    ContextoAnalise,
    ResumoAnalisePortal,
    analisar_portal,
    montar_prompt_sistema,
)
from .codebook import Codebook, ErroCodebook, carregar_codebook_de_bytes, hash_de_bytes
from .coleta import ContextoColeta, ResumoColetaPortal, coletar_portal, limpar_temporarios
from .datacao import ContextoDatacao, ResumoDatacaoPortal, datar_portal
from .descoberta import ContextoPortal, ResumoPortal, navegar_portal
from .fetcher import (
    ErroConfigPolidez,
    Polidez,
    ResultadoSeed,
    ViolacaoPolidez,
    carregar_polidez,
    dentro_da_janela_off_peak,
    executar_pre_voo,
    nova_sessao,
    reiniciar_estado_polidez,
)
from .manifest import (
    ENGINE_MINIMA,
    ErroAberturaManifesto,
    ErroEngineIncompativel,
    ErroManifestoOcupado,
    ErroSchemaFuturo,
    Manifesto,
)
from .mapa import (
    CATEGORIAS,
    ErroMapa,
    MapaMestre,
    carregar_mapa,
    hash_arquivo,
    sincronizar_mapa,
)
from .texto import ContextoTexto, ResumoTextoPortal, textuar_portal

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Agente de Editais de Inovação da Rede Federal EPCT — piloto PPGCS/UFBA.",
)
mapa_app = typer.Typer(no_args_is_help=True, help="Operações sobre o Mapa-Mestre (CAP-1).")
app.add_typer(mapa_app, name="mapa")
fila_app = typer.Typer(
    no_args_is_help=True,
    help="Fila de Revisão Manual da datação (FR-8/CAP-3): listar e decidir.",
)
app.add_typer(fila_app, name="fila")

_ANO_MINIMO, _ANO_MAXIMO = 2019, 2026
_STATUS_FILA = ("pendente", "resolvida")
# Intervalo INTEGER do SQLite (assinado 64 bits) — seeds fora dele seriam
# recusadas pelo banco só DEPOIS do lote aberto; valida-se ANTES (exit 2).
_SEED_MINIMA = -(2**63)
_SEED_MAXIMA = 2**63 - 1

ENV_CONFIGS = "AGENTE_EDITAIS_CONFIGS"
ENV_MANIFESTO = "AGENTE_EDITAIS_MANIFESTO"
ENV_CORPUS = "AGENTE_EDITAIS_CORPUS"


def _raiz_projeto() -> Path:
    atual = Path.cwd().resolve()
    for candidato in (atual, *atual.parents):
        if (candidato / "pyproject.toml").exists():
            return candidato
    return atual


def _dir_configs() -> Path:
    override = os.environ.get(ENV_CONFIGS)
    return Path(override) if override else _raiz_projeto() / "configs"


def _caminho_manifesto() -> Path:
    override = os.environ.get(ENV_MANIFESTO)
    return Path(override) if override else _raiz_projeto() / "dados" / "manifesto.sqlite3"


def _raiz_corpus() -> Path:
    override = os.environ.get(ENV_CORPUS)
    return Path(override) if override else _raiz_projeto() / "corpus"


def _abrir_manifesto() -> Manifesto:
    try:
        return Manifesto(_caminho_manifesto())
    except ErroEngineIncompativel as exc:
        typer.echo(f"ERRO: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    except ErroManifestoOcupado as exc:
        typer.echo(f"ERRO: {exc}", err=True)
        raise typer.Exit(code=4) from exc
    except (ErroAberturaManifesto, ErroSchemaFuturo) as exc:
        typer.echo(f"ERRO: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@contextmanager
def uso_manifesto() -> Iterator[Manifesto]:
    """Abre o Manifesto mapeando lock de gravação (transação) para exit 4."""
    try:
        with _abrir_manifesto() as manifesto:
            yield manifesto
    except ErroManifestoOcupado as exc:
        typer.echo(f"ERRO: {exc}", err=True)
        raise typer.Exit(code=4) from exc


def _carregar_mapa_seguro() -> tuple[MapaMestre, Path]:
    caminho = _dir_configs() / "mapa-mestre.toml"
    try:
        return carregar_mapa(caminho), caminho
    except ErroMapa as exc:
        for problema in exc.problemas:
            typer.echo(f"ERRO: {problema}", err=True)
        raise typer.Exit(code=2) from exc


def _carregar_polidez_segura() -> tuple[Polidez, Path]:
    caminho = _dir_configs() / "politeness.toml"
    try:
        return carregar_polidez(caminho), caminho
    except ErroConfigPolidez as exc:
        typer.echo(f"ERRO: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _carregar_codebook_seguro() -> tuple[Codebook, Path, str]:
    """Codebook válido + hash DOS MESMOS bytes, ou exit 2 antes de qualquer
    rede/provedor/banco (AD-6/AD-9).

    TOCTOU: os bytes são lidos UMA vez; o parse acontece sobre eles e o
    ``codebook_sha256`` devolvido é o hash DELES — o instrumento registrado
    no lote é exatamente o executado, mesmo se o arquivo mudar depois.
    """
    caminho = _dir_configs() / "codebook.yaml"
    try:
        conteudo = caminho.read_bytes()
    except OSError as exc:
        typer.echo(f"ERRO: {caminho}: não foi possível ler o arquivo ({exc}).", err=True)
        raise typer.Exit(code=2) from exc
    try:
        return (
            carregar_codebook_de_bytes(conteudo, caminho),
            caminho,
            hash_de_bytes(conteudo),
        )
    except ErroCodebook as exc:
        for problema in exc.problemas:
            typer.echo(f"ERRO: {problema}", err=True)
        raise typer.Exit(code=2) from exc


def _credenciais_provedor_ausentes() -> list[str]:
    """Env do provedor exigidas ANTES de abrir banco/lote (fail-fast).

    Sem este preflight, a credencial ausente só apareceria DENTRO do adapter
    — com o lote já aberto e um edital cheio de erros pela frente.
    """
    ausentes = []
    for nome in (llm_adapter.ENV_BASE_URL, llm_adapter.ENV_CHAVE):
        if not os.environ.get(nome, "").strip():
            ausentes.append(nome)
    return ausentes


@mapa_app.command("validar")
def mapa_validar() -> None:
    """Lista instituições→portais→categoria→seeds e sincroniza com o Manifesto."""
    mapa, caminho_mapa = _carregar_mapa_seguro()

    total_seeds = 0
    for instituicao in mapa.instituicao:
        typer.echo(f"{instituicao.sigla} — {instituicao.nome}")
        for portal in instituicao.portal:
            sufixo = " [dinâmico]" if portal.dinamico else ""
            typer.echo(f"  [{portal.categoria}] {portal.nome} <{portal.url}>{sufixo}")
            for seed in portal.seeds:
                typer.echo(f"      seed: {seed}")
                total_seeds += 1

    total_portais = sum(len(i.portal) for i in mapa.instituicao)
    typer.echo("")
    typer.echo(
        f"Mapa válido: {len(mapa.instituicao)} instituições, "
        f"{total_portais} portais, {total_seeds} seeds "
        f"(categorias aceitas: {', '.join(CATEGORIAS)})."
    )

    with uso_manifesto() as manifesto:
        resumo = sincronizar_mapa(
            mapa,
            manifesto,
            comando="mapa validar",
            hash_mapa=hash_arquivo(caminho_mapa),
        )
    typer.echo(
        f"Manifesto sincronizado: {len(resumo['criados'])} portais novos, "
        f"{len(resumo['atualizados'])} atualizados "
        f"(mapa-mestre.toml sha256 {resumo['hash_mapa'][:12]}…)."
    )


@app.command()
def preflight() -> None:
    """Pré-voo: verifica cada seed VIA fetcher; falhas não abortam o lote."""
    mapa, _ = _carregar_mapa_seguro()
    polidez, caminho_polidez = _carregar_polidez_segura()
    pares = mapa.seeds_unicas()
    if not pares:
        typer.echo("Nenhuma seed declarada no Mapa-Mestre.")
        raise typer.Exit(code=2)

    politeness_sha256 = hash_arquivo(caminho_polidez)
    urls = [url for _, _, url in pares]
    total = len(pares)
    typer.echo(f"Pré-voo de {total} seeds (delay ≥ {polidez.delay_minimo_s:g}s/host)...")

    acessiveis: list[str] = []
    inacessiveis: list[str] = []
    sondadas = 0
    abortado = False

    try:
        with uso_manifesto() as manifesto:

            def _registrar_seed(indice: int, _total: int, resultado: ResultadoSeed) -> None:
                nonlocal sondadas
                sondadas = indice
                if resultado.ok:
                    acessiveis.append(resultado.url)
                    marcador = "[OK   ]"
                    detalhe_extra = f"HTTP {resultado.status_http} via {resultado.metodo}"
                else:
                    inacessiveis.append(resultado.url)
                    marcador = "[FALHA]"
                    detalhe_extra = str(resultado.erro)
                instituicao, portal = pares[indice - 1][0], pares[indice - 1][1]
                typer.echo(
                    f"({indice}/{total}) {marcador} {resultado.url} "
                    f"[{instituicao.sigla} · {portal.nome}] — {detalhe_extra}"
                )
                manifesto.registrar_evento(
                    tipo="seed_acessivel" if resultado.ok else "seed_inacessivel",
                    comando="preflight",
                    detalhe={
                        "url": resultado.url,
                        "instituicao": instituicao.sigla,
                        "portal": portal.nome,
                        "status_http": resultado.status_http,
                        "metodo": resultado.metodo,
                        "erro": resultado.erro,
                        "duracao_s": round(resultado.duracao_s, 3),
                        # recalculado POR SEED — pode cruzar a meia-noite no lote
                        "dentro_janela_off_peak": dentro_da_janela_off_peak(polidez.off_peak),
                    },
                )

            try:
                executar_pre_voo(urls, polidez, informar_progresso=_registrar_seed)
                manifesto.registrar_evento(
                    tipo="preflight_concluido",
                    comando="preflight",
                    detalhe={
                        "seeds": total,
                        "acessiveis": len(acessiveis),
                        "inacessiveis": len(inacessiveis),
                        "politeness_sha256": politeness_sha256,
                    },
                )
            finally:
                if sondadas < total:
                    abortado = True
                    try:
                        manifesto.registrar_evento(
                            tipo="preflight_abortado",
                            comando="preflight",
                            detalhe={
                                "seeds": total,
                                "sondadas": sondadas,
                                "restantes": total - sondadas,
                                "politeness_sha256": politeness_sha256,
                            },
                        )
                    except Exception:
                        pass
    except ViolacaoPolidez as exc:
        typer.echo(f"ERRO: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("")
    typer.echo(f"Acessíveis:   {len(acessiveis)}")
    for url in acessiveis:
        typer.echo(f"  - {url}")
    typer.echo(f"Inacessíveis: {len(inacessiveis)}")
    for url in inacessiveis:
        typer.echo(f"  - {url}")
    if abortado:
        typer.echo("Pré-voo INTERROMPIDO no meio do lote (evento preflight_abortado gravado).")
        raise typer.Exit(code=1)
    typer.echo("Pré-voo concluído; lote não abortado por falhas de seed (FR-2).")


@app.command()
def descobrir(
    portal: str = typer.Option(
        None,
        "--portal",
        help="Sigla da instituição cujos portais serão navegados (ex.: IFBA).",
    ),
    todos: bool = typer.Option(False, "--todos", help="Navega todos os portais do Mapa-Mestre."),
    revisitar: bool = typer.Option(
        False,
        "--revisitar",
        help="Limpa as seções já visitadas do(s) portal(is) alvo antes de navegar "
        "(use quando a curadoria mudar o comportamento, ex.: dinamico=true).",
    ),
) -> None:
    """CAP-2: descobre seções e candidatos a edital navegando pelas seeds.

    Rede VIA fetcher com robots.txt mandatório; estático por padrão e
    Playwright no gatilho (portal dinâmico OU conteúdo ausente). Perdas
    viram eventos; o lote NUNCA aborta por falha de rede.
    """
    if todos == (portal is not None):
        typer.echo("ERRO: use exatamente um de --portal SIGLA ou --todos.", err=True)
        raise typer.Exit(code=2)
    if portal is not None and not portal.strip():
        # --portal "" (ou só espaços) é flag malformada, não sigla desconhecida
        typer.echo("ERRO: --portal exige uma sigla não vazia (ex.: --portal IFBA).", err=True)
        raise typer.Exit(code=2)

    mapa, caminho_mapa = _carregar_mapa_seguro()
    polidez, caminho_polidez = _carregar_polidez_segura()
    reiniciar_estado_polidez()  # estado de polidez vale POR EXECUÇÃO (§9.1)

    # Regra que estreia na Story 2: crawling OBRIGA a janela off-peak
    # (fuso do host), conforme politeness.toml ([crawl]) e notas da Story 1.
    if polidez.crawl_respeitar_janela_off_peak and not dentro_da_janela_off_peak(polidez.off_peak):
        typer.echo(
            f"ERRO: fora da janela off-peak ({polidez.off_peak}) no fuso do host; "
            "crawling recusado pela polidez centralizada (AD-5). O probe do "
            "'preflight' continua livre — a restrição é do crawling.",
            err=True,
        )
        raise typer.Exit(code=1)

    alvo = portal.strip().upper() if portal else None
    pares = [
        (instituicao, p)
        for instituicao in mapa.instituicao
        for p in instituicao.portal
        if todos or instituicao.sigla.upper() == alvo
    ]
    if not pares:
        if todos:
            typer.echo(
                "ERRO: nenhum portal no Mapa-Mestre — cadastre instituições e "
                "portais no mapa antes de descobrir.",
                err=True,
            )
        else:
            siglas_conhecidas = ", ".join(sorted({i.sigla.upper() for i in mapa.instituicao}))
            typer.echo(
                f"ERRO: nenhuma instituição com sigla '{portal}' no Mapa-Mestre. "
                f"Siglas conhecidas: {siglas_conhecidas}.",
                err=True,
            )
        raise typer.Exit(code=1)

    mapa_sha256 = hash_arquivo(caminho_mapa)
    politeness_sha256 = hash_arquivo(caminho_polidez)

    resumos: list[ResumoPortal] = []
    try:
        with uso_manifesto() as manifesto:
            contextos: list[ContextoPortal] = []
            ausentes: list[str] = []
            for instituicao, p in pares:
                id_portal = manifesto.id_portal_por_url(p.url)
                if id_portal is None:
                    ausentes.append(f"[{instituicao.sigla}] {p.url}")
                    continue
                contextos.append(ContextoPortal(instituicao.sigla, p, id_portal))
            if ausentes:
                typer.echo(
                    "ERRO: portais ainda não sincronizados no Manifesto — rode "
                    f"'agente-editais mapa validar' antes de descobrir: {'; '.join(ausentes)}",
                    err=True,
                )
                raise typer.Exit(code=1)

            if revisitar:
                for contexto in contextos:
                    removidas = manifesto.limpar_secoes_do_portal(contexto.portal_id)
                    manifesto.registrar_evento(
                        tipo="secoes_reiniciadas",
                        comando="descobrir",
                        detalhe={
                            "portal": contexto.portal.url,
                            "instituicao": contexto.instituicao_sigla,
                            "secoes_removidas": removidas,
                        },
                    )
                    typer.echo(
                        f"[{contexto.instituicao_sigla}] {removidas} seção(ões) esquecidas — navegação recomeça do zero."
                    )

            with nova_sessao(polidez.user_agent) as sessao:
                for contexto in contextos:
                    resumos.append(navegar_portal(contexto, manifesto, polidez, sessao=sessao))
            manifesto.registrar_evento(
                tipo="descobrir_concluido",
                comando="descobrir",
                detalhe={
                    "portais": len(resumos),
                    "alvo": "--todos" if todos else alvo,
                    "mapa_sha256": mapa_sha256,
                    "politeness_sha256": politeness_sha256,
                    "totais": {
                        chave: sum(getattr(resumo, chave) for resumo in resumos)
                        for chave in (
                            "secoes_visitadas",
                            "secoes_ja_conhecidas",
                            "secoes_falha",
                            "secoes_excedidas",
                            "candidatos_novos",
                            "candidatos_duplicados",
                            "usos_playwright",
                            "bloqueios_robots",
                            "urls_invalidas",
                            "links_repetidos",
                            "links_fora_do_portal",
                            "paginas_baixadas",
                        )
                    },
                    "cortes_por_teto": sum(1 for r in resumos if r.corte_por_teto),
                    "urls_perdidas": [url for resumo in resumos for url in resumo.urls_perdidas],
                },
            )
    except ViolacaoPolidez as exc:
        typer.echo(f"ERRO: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    for resumo in resumos:
        typer.echo(f"[{resumo.instituicao_sigla}] {resumo.portal_nome}")
        typer.echo(
            f"  Seções:     {resumo.secoes_visitadas} visitadas, "
            f"{resumo.secoes_ja_conhecidas} já conhecidas (nunca revisitam), "
            f"{resumo.secoes_falha} falhas"
        )
        typer.echo(
            f"  Candidatos: {resumo.candidatos_novos} novos, "
            f"{resumo.candidatos_duplicados} duplicados ignorados"
        )
        gatilhos = ", ".join(
            f"{nome}={contagem}" for nome, contagem in resumo.gatilhos_playwright.items()
        )
        typer.echo(f"  Playwright: {resumo.usos_playwright} uso(s) ({gatilhos})")
        linha_robots = f"  robots.txt: {resumo.bloqueios_robots} bloqueio(s)"
        if resumo.robots_inacessivel:
            linha_robots += "; inacessível — permitido com evento"
        typer.echo(linha_robots)
        notas = (
            f"  Notas:      {resumo.links_repetidos} repetidos, "
            f"{resumo.links_fora_do_portal} fora do portal, "
            f"{resumo.secoes_excedidas} além da profundidade, "
            f"{resumo.urls_invalidas} URLs inválidas, "
            f"{resumo.paginas_baixadas}/{polidez.max_paginas_por_portal} páginas"
        )
        if resumo.corte_por_teto:
            notas += " [TETO ATINGIDO]"
        typer.echo(notas)
        for perdida in resumo.urls_perdidas:
            typer.echo(f"  Perdida:    {perdida}")

    totais = {
        chave: sum(getattr(resumo, chave) for resumo in resumos)
        for chave in ("secoes_visitadas", "candidatos_novos")
    }
    typer.echo("")
    typer.echo(
        f"Descoberta concluída: {len(resumos)} portal(is), "
        f"{totais['secoes_visitadas']} seções visitadas, "
        f"{totais['candidatos_novos']} candidatos novos "
        "(eventos no Manifesto; lote não abortado por falhas)."
    )


@app.command()
def coletar(
    portal: str = typer.Option(
        None,
        "--portal",
        help="Sigla da instituição cujos portais serão coletados (ex.: IFBA).",
    ),
    todos: bool = typer.Option(False, "--todos", help="Coleta todos os portais do Mapa-Mestre."),
) -> None:
    """CAP-4: baixa candidatos PDF com Registro L1 nascido na captura.

    Dedupe por hash intra-portal (alias com referência cruzada), retomada
    pelo estado no Manifesto (re-execução idempotente), suspensão de host
    com 403 persistente — o restante DO HOST é pulado, e a suspensão é
    compartilhada entre os portais da mesma execução —, cap de tamanho:
    tudo VIA fetcher (AD-5/AD-2/AD-11). Falhas NUNCA abortam o lote;
    exit 0 mesmo com perdas registradas.
    """
    if todos == (portal is not None):
        typer.echo("ERRO: use exatamente um de --portal SIGLA ou --todos.", err=True)
        raise typer.Exit(code=2)
    if portal is not None and not portal.strip():
        typer.echo("ERRO: --portal exige uma sigla não vazia (ex.: --portal IFBA).", err=True)
        raise typer.Exit(code=2)

    mapa, caminho_mapa = _carregar_mapa_seguro()
    polidez, caminho_polidez = _carregar_polidez_segura()
    reiniciar_estado_polidez()  # delay/robots/403 começam zerados POR EXECUÇÃO

    if polidez.crawl_respeitar_janela_off_peak and not dentro_da_janela_off_peak(polidez.off_peak):
        typer.echo(
            f"ERRO: fora da janela off-peak ({polidez.off_peak}) no fuso do host; "
            "coleta recusada pela polidez centralizada (AD-5). O probe do "
            "'preflight' continua livre — a restrição é do crawling.",
            err=True,
        )
        raise typer.Exit(code=1)

    alvo = portal.strip().upper() if portal else None
    pares = [
        (instituicao, p)
        for instituicao in mapa.instituicao
        for p in instituicao.portal
        if todos or instituicao.sigla.upper() == alvo
    ]
    if not pares:
        if todos:
            typer.echo(
                "ERRO: nenhum portal no Mapa-Mestre — cadastre instituições e "
                "portais no mapa antes de coletar.",
                err=True,
            )
        else:
            siglas_conhecidas = ", ".join(sorted({i.sigla.upper() for i in mapa.instituicao}))
            typer.echo(
                f"ERRO: nenhuma instituição com sigla '{portal}' no Mapa-Mestre. "
                f"Siglas conhecidas: {siglas_conhecidas}.",
                err=True,
            )
        raise typer.Exit(code=1)

    mapa_sha256 = hash_arquivo(caminho_mapa)
    politeness_sha256 = hash_arquivo(caminho_polidez)
    raiz_corpus = _raiz_corpus()
    try:
        raiz_corpus.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.echo(
            f"ERRO: não foi possível criar a raiz do corpus em {raiz_corpus}: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    limpar_temporarios(raiz_corpus)

    resumos: list[ResumoColetaPortal] = []
    # suspensão de host COMPARTILHADA entre todos os portais desta execução:
    # host suspenso num portal pula o restante DO HOST nos demais também
    suspensos_da_execucao: list[str] = []
    try:
        with uso_manifesto() as manifesto:
            contextos: list[ContextoColeta] = []
            ausentes: list[str] = []
            for instituicao, p in pares:
                id_portal = manifesto.id_portal_por_url(p.url)
                if id_portal is None:
                    ausentes.append(f"[{instituicao.sigla}] {p.url}")
                    continue
                contextos.append(ContextoColeta(instituicao.sigla, p, id_portal))
            if ausentes:
                typer.echo(
                    "ERRO: portais ainda não sincronizados no Manifesto — rode "
                    f"'agente-editais mapa validar' antes de coletar: {'; '.join(ausentes)}",
                    err=True,
                )
                raise typer.Exit(code=1)

            with nova_sessao(polidez.user_agent) as sessao:
                for contexto in contextos:
                    resumos.append(
                        coletar_portal(
                            contexto,
                            manifesto,
                            polidez,
                            raiz_corpus=raiz_corpus,
                            comando="coletar",
                            sessao=sessao,
                            hosts_suspensos_execucao=suspensos_da_execucao,
                        )
                    )
            manifesto.registrar_evento(
                tipo="coletar_concluido",
                comando="coletar",
                detalhe={
                    "portais": len(resumos),
                    "alvo": "--todos" if todos else alvo,
                    "mapa_sha256": mapa_sha256,
                    "politeness_sha256": politeness_sha256,
                    "raiz_corpus": str(raiz_corpus),
                    "max_mb_documento": polidez.max_mb_documento,
                    "totais": {
                        chave: sum(getattr(resumo, chave) for resumo in resumos)
                        for chave in (
                            "candidatos_pdf",
                            "baixados",
                            "novas_versoes",
                            "restaurados",
                            "aliases_duplicados",
                            "ja_integros",
                            "tamanho_excedido",
                            "conteudo_inesperado",
                            "falhas_download",
                            "bloqueios_robots",
                            "urls_invalidas",
                            "puladas_host_suspenso",
                        )
                    },
                    "hosts_suspensos": [
                        host for resumo in resumos for host in resumo.hosts_suspensos
                    ],
                    "urls_perdidas": [url for resumo in resumos for url in resumo.urls_perdidas],
                },
            )
    except ViolacaoPolidez as exc:
        typer.echo(f"ERRO: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    total_puladas_suspensao = sum(r.puladas_host_suspenso for r in resumos)
    for resumo in resumos:
        typer.echo(f"[{resumo.instituicao_sigla}] {resumo.portal_nome}")
        typer.echo(
            f"  Candidatos PDF: {resumo.candidatos_pdf} "
            f"(baixados: {resumo.baixados}, novas versões: {resumo.novas_versoes}, "
            f"restaurados: {resumo.restaurados})"
        )
        typer.echo(
            f"  Dedupe/retomada: {resumo.aliases_duplicados} alias por hash duplicado, "
            f"{resumo.ja_integros} já íntegros (pulados sem rede)"
        )
        notas = (
            f"  Perdas:      {resumo.tamanho_excedido} acima do cap "
            f"({polidez.max_mb_documento:g} MB), "
            f"{resumo.conteudo_inesperado} não-PDF, "
            f"{resumo.falhas_download} falhas de download, "
            f"{resumo.bloqueios_robots} bloqueios de robots"
        )
        typer.echo(notas)
        if resumo.hosts_suspensos:
            typer.echo(
                f"  Host suspenso (403×{polidez.max_403_consecutivos}): "
                f"{', '.join(resumo.hosts_suspensos)} — "
                "restante DO HOST é pulado nesta execução; tenta de novo na próxima."
            )
        if resumo.puladas_host_suspenso:
            typer.echo(f"  Puladas por suspensão de host: {resumo.puladas_host_suspenso}")
        for perdida in resumo.urls_perdidas:
            typer.echo(f"  Perdida:    {perdida}")

    total_baixados = sum(r.baixados + r.novas_versoes + r.restaurados for r in resumos)
    typer.echo("")
    if total_puladas_suspensao:
        typer.echo(f"URLs puladas por suspensão de host nesta execução: {total_puladas_suspensao}")
    typer.echo(
        f"Coleta concluída: {len(resumos)} portal(is), {total_baixados} documento(s) "
        f"gravados em {raiz_corpus} "
        "(L1 completo; eventos no Manifesto; lote não abortado)."
    )


@app.command()
def textuar(
    portal: str = typer.Option(
        None,
        "--portal",
        help="Sigla da instituição cujos portais serão textuados (ex.: IFBA).",
    ),
    todos: bool = typer.Option(False, "--todos", help="Textua todos os portais do Mapa-Mestre."),
) -> None:
    """CAP-6: extrai texto de cada Documento para um .txt irmão.

    Extrator ÚNICO do sistema (AD-11): pypdf com extração tolerante por
    página; ``flag_escaneado`` pelo limiar configurável ([texto]
    ``limiar_chars_por_pagina``); proveniência gravada no Manifesto (v4).
    Retomável e idempotente: documentos já extraídos com hash vigente são
    pulados. NÃO faz I/O de rede — a janela off-peak não se aplica aqui.
    Falhas por documento viram evento e o lote segue (exit 0).
    """
    if todos == (portal is not None):
        typer.echo("ERRO: use exatamente um de --portal SIGLA ou --todos.", err=True)
        raise typer.Exit(code=2)
    if portal is not None and not portal.strip():
        typer.echo("ERRO: --portal exige uma sigla não vazia (ex.: --portal IFBA).", err=True)
        raise typer.Exit(code=2)

    mapa, caminho_mapa = _carregar_mapa_seguro()
    polidez, caminho_polidez = _carregar_polidez_segura()

    alvo = portal.strip().upper() if portal else None
    pares = [
        (instituicao, p)
        for instituicao in mapa.instituicao
        for p in instituicao.portal
        if todos or instituicao.sigla.upper() == alvo
    ]
    if not pares:
        if todos:
            typer.echo(
                "ERRO: nenhum portal no Mapa-Mestre — cadastre instituições e "
                "portais no mapa antes de textuar.",
                err=True,
            )
        else:
            siglas_conhecidas = ", ".join(sorted({i.sigla.upper() for i in mapa.instituicao}))
            typer.echo(
                f"ERRO: nenhuma instituição com sigla '{portal}' no Mapa-Mestre. "
                f"Siglas conhecidas: {siglas_conhecidas}.",
                err=True,
            )
        raise typer.Exit(code=1)

    politeness_sha256 = hash_arquivo(caminho_polidez)
    limiar = polidez.texto_limiar_chars_por_pagina

    resumos: list[ResumoTextoPortal] = []
    totais: dict[str, int] = {}
    with uso_manifesto() as manifesto:
        contextos: list[ContextoTexto] = []
        ausentes: list[str] = []
        for instituicao, p in pares:
            id_portal = manifesto.id_portal_por_url(p.url)
            if id_portal is None:
                ausentes.append(f"[{instituicao.sigla}] {p.url}")
                continue
            contextos.append(ContextoTexto(instituicao.sigla, p, id_portal))
        if ausentes:
            typer.echo(
                "ERRO: portais ainda não sincronizados no Manifesto — rode "
                f"'agente-editais mapa validar' antes de textuar: {'; '.join(ausentes)}",
                err=True,
            )
            raise typer.Exit(code=1)

        for contexto in contextos:
            resumos.append(textuar_portal(contexto, manifesto, limiar, comando="textuar"))
        # computado UMA vez: payload do evento e eco CLI compartilham o mesmo dict
        totais = {
            chave: sum(getattr(resumo, chave) for resumo in resumos)
            for chave in ("documentos", "extraidos", "escaneados", "erros", "pulados")
        }
        manifesto.registrar_evento(
            tipo="textuar_concluido",
            comando="textuar",
            detalhe={
                "portais": len(resumos),
                "alvo": "--todos" if todos else alvo,
                "mapa_sha256": hash_arquivo(caminho_mapa),
                "politeness_sha256": politeness_sha256,
                "limiar_chars_por_pagina": limiar,
                "totais": totais,
                "urls_perdidas": [url for resumo in resumos for url in resumo.urls_perdidas],
            },
        )

    for resumo in resumos:
        typer.echo(f"[{resumo.instituicao_sigla}] {resumo.portal_nome}")
        typer.echo(
            f"  Documentos: {resumo.documentos} "
            f"(extraídos: {resumo.extraidos}, escaneados: {resumo.escaneados}, "
            f"erros: {resumo.erros}, pulados: {resumo.pulados})"
        )
        for perdida in resumo.urls_perdidas:
            typer.echo(f"  Perdida:    {perdida}")

    typer.echo("")
    typer.echo(
        f"Extração concluída: {len(resumos)} portal(is), "
        f"{totais['extraidos']} extraído(s), {totais['escaneados']} escaneado(s), "
        f"{totais['erros']} erro(s), {totais['pulados']} pulado(s) "
        "(.txt irmãos no corpus; eventos no Manifesto; lote não abortado)."
    )


@app.command()
def datar(
    portal: str = typer.Option(
        None,
        "--portal",
        help="Sigla da instituição cujos portais serão datados (ex.: IFBA).",
    ),
    todos: bool = typer.Option(False, "--todos", help="Data todos os portais do Mapa-Mestre."),
) -> None:
    """CAP-3: datação multi-fonte OFFLINE com fila humana fundamentada.

    Consulta TODAS as fontes locais disponíveis (url, âncora da descoberta e
    docinfo VIA extrator único), grava evidência bruta por fonte e decide
    pelas regras de aceite da janela 2019–2026: convergência aceita com
    ``metodo_datacao`` (primeiro da cascata url→ancora→pdf_meta que
    converge); só-URL sem corroboração, divergência ou ausência de data vão
    à Fila de Revisão Manual (``fila decidir``). Retomável e idempotente:
    documento datado ou já presente na fila é pulado. NÃO faz I/O de rede —
    a janela off-peak não se aplica. Falhas pontuais viram evento
    ``datacao_erro`` e o lote segue (exit 0).
    """
    if todos == (portal is not None):
        typer.echo("ERRO: use exatamente um de --portal SIGLA ou --todos.", err=True)
        raise typer.Exit(code=2)
    if portal is not None and not portal.strip():
        typer.echo("ERRO: --portal exige uma sigla não vazia (ex.: --portal IFBA).", err=True)
        raise typer.Exit(code=2)

    mapa, caminho_mapa = _carregar_mapa_seguro()

    alvo = portal.strip().upper() if portal else None
    pares = [
        (instituicao, p)
        for instituicao in mapa.instituicao
        for p in instituicao.portal
        if todos or instituicao.sigla.upper() == alvo
    ]
    if not pares:
        if todos:
            typer.echo(
                "ERRO: nenhum portal no Mapa-Mestre — cadastre instituições e "
                "portais no mapa antes de datar.",
                err=True,
            )
        else:
            siglas_conhecidas = ", ".join(sorted({i.sigla.upper() for i in mapa.instituicao}))
            typer.echo(
                f"ERRO: nenhuma instituição com sigla '{portal}' no Mapa-Mestre. "
                f"Siglas conhecidas: {siglas_conhecidas}.",
                err=True,
            )
        raise typer.Exit(code=1)

    mapa_sha256 = hash_arquivo(caminho_mapa)

    resumos: list[ResumoDatacaoPortal] = []
    totais: dict[str, int] = {}
    with uso_manifesto() as manifesto:
        contextos: list[ContextoDatacao] = []
        ausentes: list[str] = []
        for instituicao, p in pares:
            id_portal = manifesto.id_portal_por_url(p.url)
            if id_portal is None:
                ausentes.append(f"[{instituicao.sigla}] {p.url}")
                continue
            contextos.append(ContextoDatacao(instituicao.sigla, p, id_portal))
        if ausentes:
            typer.echo(
                "ERRO: portais ainda não sincronizados no Manifesto — rode "
                f"'agente-editais mapa validar' antes de datar: {'; '.join(ausentes)}",
                err=True,
            )
            raise typer.Exit(code=1)

        for contexto in contextos:
            resumos.append(datar_portal(contexto, manifesto, comando="datar"))
        # computado UMA vez: payload do evento e eco CLI compartilham o mesmo dict
        totais = {
            chave: sum(getattr(resumo, chave) for resumo in resumos)
            for chave in ("documentos", "aceitos", "enfileirados", "erros", "pulados")
        }
        manifesto.registrar_evento(
            tipo="datar_concluido",
            comando="datar",
            detalhe={
                "portais": len(resumos),
                "alvo": "--todos" if todos else alvo,
                "mapa_sha256": mapa_sha256,
                "totais": totais,
                "urls_perdidas": [url for resumo in resumos for url in resumo.urls_perdidas],
            },
        )

    for resumo in resumos:
        typer.echo(f"[{resumo.instituicao_sigla}] {resumo.portal_nome}")
        typer.echo(
            f"  Documentos: {resumo.documentos} "
            f"(aceitos: {resumo.aceitos}, fila: {resumo.enfileirados}, "
            f"erros: {resumo.erros}, pulados: {resumo.pulados})"
        )
        for perdida in resumo.urls_perdidas:
            typer.echo(f"  Perdida:    {perdida}")

    typer.echo("")
    typer.echo(
        f"Datação concluída: {len(resumos)} portal(is), "
        f"{totais['aceitos']} aceito(s), {totais['enfileirados']} na fila, "
        f"{totais['erros']} erro(s), {totais['pulados']} pulado(s) "
        "(evidências brutas + eventos no Manifesto; lote não abortado; "
        "pendentes aguardam 'fila decidir')."
    )


def _recusar_decisao(mensagem: str) -> None:
    """Flags de decisão malformadas: recusa ANTES de tocar o banco (exit 2)."""
    typer.echo(f"ERRO: {mensagem}", err=True)
    raise typer.Exit(code=2)


@app.command()
def analise(
    portal: str = typer.Option(
        None,
        "--portal",
        help="Sigla da instituição cujos portais serão analisados (ex.: IFBA).",
    ),
    todos: bool = typer.Option(False, "--todos", help="Analisa todos os portais do Mapa-Mestre."),
    modelo: str = typer.Option(
        None,
        "--modelo",
        help="Modelo do instrumento LLM; default vem de LLM_MODELO (OQ-1).",
    ),
    temperatura: float = typer.Option(
        0.0, "--temperatura", min=0.0, max=2.0, help="Temperatura da chamada (default 0.0)."
    ),
    seed: int | None = typer.Option(None, "--seed", help="Seed determinística do provedor."),
    tentativas: int = typer.Option(
        2,
        "--tentativas",
        min=1,
        max=10,
        help="Tentativas TOTAIS por edital quando a saída violar o esquema ou o "
        "provedor falhar (default 2; teto 10).",
    ),
) -> None:
    """CAP-7: codifica editais no Catálogo L2 com instrumento congelado.

    Phase-gates BLOQUEANTES (AD-6): recusa (exit 2) ANTES de qualquer
    chamada ao provedor enquanto o codebook.yaml não tiver congelamento
    completo, Tríade de Dahlin resolvida e limiar κ fixado a priori — a
    mensagem nomeia o gate faltante. Cada edital recebe UMA chamada
    (textos vigentes em ordem cronológica de captura); saída inválida ao
    esquema é reprocessada até --tentativas e JAMAIS gravada; citação
    inexistente no texto citado grava o campo como 'citacao_invalidada'
    (FR-18). Mesma assinatura continua o lote aberto pulando editais já
    codificados; componente diferente ⇒ novo lote. Falhas pontuais viram
    eventos e o lote segue (exit 0).
    """
    if todos == (portal is not None):
        typer.echo("ERRO: use exatamente um de --portal SIGLA ou --todos.", err=True)
        raise typer.Exit(code=2)
    if portal is not None and not portal.strip():
        typer.echo("ERRO: --portal exige uma sigla não vazia (ex.: --portal IFBA).", err=True)
        raise typer.Exit(code=2)

    mapa, caminho_mapa = _carregar_mapa_seguro()
    codebook, _caminho_codebook, codebook_sha256 = _carregar_codebook_seguro()

    # PHASE-GATES (AD-6): bloqueiam ANTES de tocar rede/provedor — exit 2
    # nomeando o gate faltante; ZERO chamadas ao LLM neste caminho.
    pendentes = codebook.gates_pendentes()
    if pendentes:
        for gate in pendentes:
            typer.echo(f"ERRO: {gate}", err=True)
        raise typer.Exit(code=2)

    # Preflight de credenciais (fail-fast): sem BASE_URL/chave o lote abriria
    # e todo edital viraria erro dentro do adapter. Recusa ANTES de abrir
    # banco/lote/provedor (exit 2).
    ausentes_env = _credenciais_provedor_ausentes()
    if ausentes_env:
        for nome in ausentes_env:
            typer.echo(
                f"ERRO: variável de ambiente '{nome}' não definida ou vazia — "
                "configure as credenciais do provedor LLM antes de analisar.",
                err=True,
            )
        raise typer.Exit(code=2)

    modelo_efetivo = (modelo or "").strip() or os.environ.get(llm_adapter.ENV_MODELO, "").strip()
    if not modelo_efetivo:
        _recusar_flag("--modelo é obrigatório (ou defina a variável de ambiente LLM_MODELO).")
    versao_do_modelo = os.environ.get(llm_adapter.ENV_VERSAO_DO_MODELO, "").strip()
    if seed is not None and not (_SEED_MINIMA <= seed <= _SEED_MAXIMA):
        _recusar_flag(
            f"--seed {seed} está fora do intervalo INTEGER do SQLite "
            f"({_SEED_MINIMA} a {_SEED_MAXIMA})."
        )
    try:
        llm_adapter.definir_instrumento(
            modelo=modelo_efetivo,
            temperatura=temperatura,
            seed=seed,
        )
    except ValueError as exc:
        _recusar_flag(str(exc))

    alvo = portal.strip().upper() if portal else None
    pares = [
        (instituicao, p)
        for instituicao in mapa.instituicao
        for p in instituicao.portal
        if todos or instituicao.sigla.upper() == alvo
    ]
    if not pares:
        if todos:
            typer.echo(
                "ERRO: nenhum portal no Mapa-Mestre — cadastre instituições e "
                "portais no mapa antes de analisar.",
                err=True,
            )
        else:
            siglas_conhecidas = ", ".join(sorted({i.sigla.upper() for i in mapa.instituicao}))
            typer.echo(
                f"ERRO: nenhuma instituição com sigla '{portal}' no Mapa-Mestre. "
                f"Siglas conhecidas: {siglas_conhecidas}.",
                err=True,
            )
        raise typer.Exit(code=1)

    mapa_sha256 = hash_arquivo(caminho_mapa)

    resumos: list[ResumoAnalisePortal] = []
    with uso_manifesto() as manifesto:
        contextos: list[ContextoAnalise] = []
        ausentes: list[str] = []
        for instituicao, p in pares:
            id_portal = manifesto.id_portal_por_url(p.url)
            if id_portal is None:
                ausentes.append(f"[{instituicao.sigla}] {p.url}")
                continue
            contextos.append(ContextoAnalise(instituicao.sigla, p, id_portal))
        if ausentes:
            typer.echo(
                "ERRO: portais ainda não sincronizados no Manifesto — rode "
                f"'agente-editais mapa validar' antes de analisar: {'; '.join(ausentes)}",
                err=True,
            )
            raise typer.Exit(code=1)

        sistema, prompt_sha256 = montar_prompt_sistema(codebook)
        assinatura = {
            "modelo": modelo_efetivo,
            "versao_do_modelo": versao_do_modelo,
            "prompt_versao": PROMPT_VERSAO,
            "prompt_sha256": prompt_sha256,
            "temperatura": temperatura,
            "seed": seed,
            "codebook_sha256": codebook_sha256,
            "versao_agente": __version__,
            "schema_version": manifesto.schema_version(),
        }
        lote, criado = manifesto.obter_ou_abrir_lote(assinatura)
        lote_id = int(lote["id"])
        manifesto.registrar_evento(
            tipo="lote_l2" if criado else "lote_l2_continuado",
            comando="analise",
            detalhe={
                "lote_id": lote_id,
                **assinatura,
                "status": str(lote["status"]),
            },
        )

        for contexto in contextos:
            resumos.append(
                analisar_portal(
                    contexto,
                    manifesto,
                    codebook,
                    sistema=sistema,
                    lote_id=lote_id,
                    tentativas=tentativas,
                    comando="analise",
                )
            )
        fechou = manifesto.fechar_lote(lote_id)
        # Chave ÚNICA com analise_portal_concluida (Resumo.totalizar):
        # 'pulados_ja_codificados' — contadores idênticos em ambos os eventos.
        totais = {
            "editais": sum(r.editais for r in resumos),
            "codificados": sum(r.codificados for r in resumos),
            "pulados_ja_codificados": sum(r.pulados for r in resumos),
            "invalidos": sum(r.invalidos for r in resumos),
            "erros": sum(r.erros for r in resumos),
            "excluidos": sum(r.excluidos for r in resumos),
            "escaneados_sem_ocr": sum(r.escaneados_sem_ocr for r in resumos),
        }
        manifesto.registrar_evento(
            tipo="analise_concluido",
            comando="analise",
            detalhe={
                "portais": len(resumos),
                "alvo": "--todos" if todos else alvo,
                "lote_id": lote_id,
                "lote_fechado": fechou,
                "modelo": modelo_efetivo,
                "temperatura": temperatura,
                "seed": seed,
                "codebook_sha256": codebook_sha256,
                "prompt_versao": PROMPT_VERSAO,
                "prompt_sha256": prompt_sha256,
                "mapa_sha256": mapa_sha256,
                "totais": totais,
                "editais_perdidos": [
                    edital for resumo in resumos for edital in resumo.editais_perdidos
                ],
            },
        )

    for resumo in resumos:
        typer.echo(f"[{resumo.instituicao_sigla}] {resumo.portal_nome}")
        typer.echo(
            f"  Editais: {resumo.editais} "
            f"(codificados: {resumo.codificados}, já codificados: {resumo.pulados}, "
            f"inválidos: {resumo.invalidos}, erros: {resumo.erros}, "
            f"excluídos: {resumo.excluidos})"
        )
        if resumo.escaneados_sem_ocr:
            typer.echo(f"  Documentos escaneados pulados (sem OCR): {resumo.escaneados_sem_ocr}")
        for perdido in resumo.editais_perdidos:
            typer.echo(f"  Perdido:    {perdido}")

    typer.echo("")
    typer.echo(
        f"Análise concluída (lote #{lote_id}): {len(resumos)} portal(is), "
        f"{totais['codificados']} edital(is) codificado(s), "
        f"{totais['excluidos']} excluído(s), "
        f"{totais['invalidos']} inválido(s), {totais['erros']} erro(s) "
        "(eventos no Manifesto; lote não abortado por falhas pontuais)."
    )


def _recusar_flag(mensagem: str) -> None:
    """Flag malformada: recusa ANTES de abrir o banco (exit 2)."""
    typer.echo(f"ERRO: {mensagem}", err=True)
    raise typer.Exit(code=2)


@fila_app.command("listar")
def fila_listar(
    status: str = typer.Option(
        "pendente",
        "--status",
        help="Filtra por status: pendente | resolvida | todas.",
    ),
) -> None:
    """Lista itens da fila com motivo e evidências brutas coletadas."""
    status_normalizado = status.strip().lower()
    if status_normalizado not in (*_STATUS_FILA, "todas"):
        typer.echo(
            f"ERRO: --status deve ser 'pendente', 'resolvida' ou 'todas' (recebido {status!r}).",
            err=True,
        )
        raise typer.Exit(code=2)

    with uso_manifesto() as manifesto:
        itens = manifesto.consultar_fila(
            None if status_normalizado == "todas" else status_normalizado
        )
        rotulo_status = status_normalizado
        typer.echo(f"Fila de revisão ({rotulo_status}): {len(itens)} item(ns)")
        if not itens:
            typer.echo("  (nada a decidir aqui)")
            return
        for item in itens:
            typer.echo(
                f"  #{item['id']} [{item['status']}] {item['url_origem']} "
                f"(portal {item['portal_id']}) motivo={item['motivo']} "
                f"enfileirado_em={item['criado_em']}"
            )
            evidencias = manifesto.evidencias_da_url(item["url_origem"])
            if evidencias:
                typer.echo(
                    "    Evidências: "
                    + "; ".join(f"{ev['fonte']} ({ev['localizacao']})" for ev in evidencias)
                )
                for ev in evidencias:
                    valor = ev["valor_bruto"]
                    if valor and len(valor) > 100:
                        valor = valor[:100] + "…"
                    typer.echo(f"      - {ev['fonte']}: {valor}")
            else:
                typer.echo("    Evidências: (nenhuma registrada)")
            if item["status"] == "resolvida":
                destino = (
                    f"ANO {item['decidido_ano']}"
                    if item["decidido_ano"] is not None
                    else "EXCLUSÃO"
                )
                anexa = (
                    f"; evidência anexa: {item['evidencia_anexa']}"
                    if item["evidencia_anexa"]
                    else ""
                )
                typer.echo(
                    f"    Decisão: {destino} por {item['autor']} em {item['decidido_em']}{anexa}"
                )
                typer.echo(f"      Justificativa: {item['justificativa']}")


@fila_app.command("decidir")
def fila_decidir(
    id: int = typer.Option(None, "--id", help="Id do item pendente na fila."),
    ano: int = typer.Option(None, "--ano", help="Ano atribuído ao documento (2019–2026)."),
    excluir: bool = typer.Option(
        False, "--excluir", help="Marca o documento como EXCLUÍDO do corpus."
    ),
    justificativa: str = typer.Option(
        None, "--justificativa", help="Justificativa textual OBRIGATÓRIA (FR-8)."
    ),
    autor: str = typer.Option(None, "--autor", help="Autoria da decisão (obrigatória, FR-8)."),
    evidencia: str = typer.Option(
        None, "--evidencia", help="Referência opcional da evidência consultada."
    ),
) -> None:
    """Decide UM item pendente: ano atribuído OU exclusão + justificativa.

    Recusa decidir sem justificativa/autoria, sem destino único (--ano XOR
    --excluir) ou com ano fora da janela 2019–2026 — nada é gravado (exit 2).
    """
    if id is None:
        _recusar_decisao("--id é obrigatório (veja o número em 'fila listar').")
    justificativa_limpa = (justificativa or "").strip()
    if not justificativa_limpa:
        _recusar_decisao("--justificativa é OBRIGATÓRIA para decidir (FR-8).")
    autor_limpo = (autor or "").strip()
    if not autor_limpo:
        _recusar_decisao("--autor é OBRIGATÓRIA para decidir (FR-8).")
    if ano is None and not excluir:
        _recusar_decisao("a decisão precisa de um destino: --ano AAAA OU --excluir.")
    if ano is not None and excluir:
        _recusar_decisao("--ano e --excluir são mutuamente exclusivos: escolha UM destino.")
    if ano is not None and not (_ANO_MINIMO <= ano <= _ANO_MAXIMO):
        _recusar_decisao(
            f"--ano {ano} está fora da janela 2019–2026 — ano fora da janela "
            "nunca é aceito (Never da story)."
        )

    with uso_manifesto() as manifesto:
        linhas = manifesto.consultar("SELECT * FROM fila_revisao WHERE id = ?", (id,))
        if not linhas:
            typer.echo(f"ERRO: item de fila #{id} não existe.", err=True)
            raise typer.Exit(code=1)
        item = linhas[0]
        if item["status"] != "pendente":
            typer.echo(
                f"ERRO: item de fila #{id} já foi resolvido em "
                f"{item['decidido_em']} por {item['autor']} — decisão humana "
                "não é refeta.",
                err=True,
            )
            raise typer.Exit(code=1)
        ok = manifesto.registrar_decisao_fila(
            id,
            decidido_ano=ano,
            decidido_exclusao=excluir,
            justificativa=justificativa_limpa,
            autor=autor_limpo,
            evidencia_anexa=(evidencia or "").strip() or None,
        )
        if not ok:
            # corrida entre a checagem e o UPDATE — nada gravado
            typer.echo(f"ERRO: item de fila #{id} deixou de estar pendente.", err=True)
            raise typer.Exit(code=1)
        manifesto.registrar_evento(
            tipo="fila_decidida",
            comando="fila decidir",
            detalhe={
                "fila_id": id,
                "url": item["url_origem"],
                "portal_id": item["portal_id"],
                "motivo_original": item["motivo"],
                "decidido_ano": ano,
                "decidido_exclusao": excluir,
                "justificativa": justificativa_limpa,
                "autor": autor_limpo,
                "evidencia_anexa": (evidencia or "").strip() or None,
            },
        )
        destino = f"ano {ano} atribuído" if ano is not None else "documento marcado para EXCLUSÃO"
        typer.echo(
            f"Item #{id} resolvido: {destino} — autor {autor_limpo}, "
            "decisão registrada com justificativa e data (FR-8)."
        )


# -- consulta essencial (CAP-9/UJ-3/§10): comandos ONLY-leitura ---------------------

_COLUNAS_CSV: tuple[str, ...] = (
    "documento_id",
    "edital_id",
    "instituicao",
    "categoria",
    "ano",
    "ano_fonte",
    "metodo_datacao",
    "url_origem",
    "data_captura",
    "hash_sha256",
    "caminho",
)

_ERROS_DE_ESCRITA = (OSError, UnicodeEncodeError, csv.Error)


def _recusar_saida_no_manifesto(saida: Path | None) -> None:
    """Guarda catastrófica: ``--saida`` igual ao Manifesto é recusado cedo.

    Um export gravado por cima do banco truncaria o estado único do sistema;
    a comparação resolve ambos os caminhos e ignora caixa (Windows).
    """
    if saida is None:
        return
    caminho_manifesto = _caminho_manifesto()
    try:
        mesmo_caminho = os.path.normcase(str(Path(saida).resolve())) == os.path.normcase(
            str(Path(caminho_manifesto).resolve())
        )
    except OSError:
        mesmo_caminho = False  # resolução falhou ⇒ deixa a escrita falhar depois
    if mesmo_caminho:
        _recusar_flag(
            "--saida não pode apontar para o próprio Manifesto "
            f"({caminho_manifesto}): o export sobrescreveria o banco."
        )


def _tabela_l1(linhas: list[sqlite3.Row]) -> list[str]:
    """Tabela alinhada do catálogo L1 com colunas compactas para o terminal.

    O CSV de ``--saida`` carrega TODAS as colunas da query; aqui ficam só as
    de leitura rápida — o conteúdo listado é exatamente o mesmo.
    """
    cabecalho = ("ANO", "FONTE", "INST", "CATEGORIA", "EDITAL", "DOCUMENTO")
    registros = [
        (
            "" if linha["ano"] is None else str(linha["ano"]),
            str(linha["ano_fonte"]),
            str(linha["instituicao"]),
            "" if linha["categoria"] is None else str(linha["categoria"]),
            str(linha["edital_id"]),
            str(linha["documento_id"]),
        )
        for linha in linhas
    ]
    larguras = [len(coluna) for coluna in cabecalho]
    for registro in registros:
        for indice, valor in enumerate(registro):
            larguras[indice] = max(larguras[indice], len(valor))
    saida = ["  ".join(coluna.ljust(larguras[i]) for i, coluna in enumerate(cabecalho))]
    saida.append("  ".join("-" * largura for largura in larguras))
    for registro in registros:
        saida.append("  ".join(valor.ljust(larguras[i]) for i, valor in enumerate(registro)))
    return saida


@app.command()
def consultar(
    instituicao: str | None = typer.Option(
        None,
        "--instituicao",
        help="Sigla da instituição, insensível a caixa (ex.: IFES).",
    ),
    ano: int | None = typer.Option(
        None,
        "--ano",
        help="Ano EFETIVO do documento na janela fixa 2019–2026.",
    ),
    categoria: str | None = typer.Option(
        None,
        "--categoria",
        help=f"Categoria do portal de origem ({', '.join(CATEGORIAS)}).",
    ),
    saida: Path | None = typer.Option(
        None,
        "--saida",
        help="Grava todas as colunas como CSV UTF-8 (separador vírgula) neste caminho.",
    ),
) -> None:
    """UJ-3: consulta ONLY-leitura do catálogo L1 com contagens coerentes.

    Unidade = DOCUMENTO: ano EFETIVO = COALESCE(ano_aceito, decidido_ano da
    fila), com origem explícita em ``ano_fonte`` (∈ automatica|fila_humana|
    vazio). Excluídos por decisão humana ficam FORA da listagem e aparecem
    no resumo — contagem que IGNORA o filtro de ano, pois a população
    excluída não participa da janela; pendentes na fila aparecem com ano
    vazio e por último na ordenação. As contagens impressas derivam da MESMA
    query das linhas (FR-20). A listagem sai ANTES da tentativa de export:
    falha de escrita vira exit 1 sem esconder o resultado. Filtros combinam
    por E; zero resultados é sucesso (exit 0).
    """
    # flags validadas ANTES de abrir o banco (exit 2; nada é executado)
    if instituicao is not None and not instituicao.strip():
        _recusar_flag("--instituicao exige uma sigla não vazia (ex.: --instituicao IFES).")
    if ano is not None and not (_ANO_MINIMO <= ano <= _ANO_MAXIMO):
        _recusar_flag(f"--ano {ano} está fora da janela fixa 2019–2026.")
    if categoria is not None and categoria.strip() not in CATEGORIAS:
        _recusar_flag(
            f"--categoria deve ser uma de: {', '.join(CATEGORIAS)} (recebido {categoria!r})."
        )
    _recusar_saida_no_manifesto(saida)

    filtros = {
        "instituicao": instituicao.strip() if instituicao else None,
        "ano": ano,
        "categoria": categoria.strip() if categoria else None,
    }

    caminho_manifesto = _caminho_manifesto()
    if not Path(caminho_manifesto).exists():
        typer.echo(
            f"ERRO: Manifesto não encontrado em {caminho_manifesto}. "
            "Rode 'agente-editais mapa validar' para criá-lo.",
            err=True,
        )
        raise typer.Exit(code=1)

    with uso_manifesto() as manifesto:
        linhas = manifesto.listar_l1(**filtros)
        excluidos = manifesto.contar_l1_excluidos(**filtros)
        # FR-20: contagens derivam das MESMAS linhas da listagem — nunca de
        # uma segunda contagem paralela que poderia divergir do Manifesto.
        total = len(linhas)
        por_fonte = {"automatica": 0, "fila_humana": 0, "vazio": 0}
        for linha in linhas:
            por_fonte[str(linha["ano_fonte"])] += 1

        # eco ANTES do export: falha de gravação não esconde o resultado
        for linha_tabela in _tabela_l1(linhas):
            typer.echo(linha_tabela)
        if not linhas:
            typer.echo("(nenhum documento corresponde aos filtros)")
        typer.echo("")
        typer.echo(f"Resumo: {total} documento(s) listado(s), {excluidos} excluído(s)")
        typer.echo(
            "Por ano_fonte: "
            f"automatica={por_fonte['automatica']}, "
            f"fila_humana={por_fonte['fila_humana']}, "
            f"vazio={por_fonte['vazio']}"
        )

        detalhe_evento = {
            "filtros": filtros,
            "listados": total,
            "excluidos": excluidos,
            "por_ano_fonte": por_fonte,
            "csv": str(saida) if saida else None,
        }
        if saida is not None:
            try:
                with open(saida, "w", encoding="utf-8", newline="") as arquivo:
                    escritor_csv = csv.DictWriter(arquivo, fieldnames=list(_COLUNAS_CSV))
                    escritor_csv.writeheader()
                    for linha in linhas:
                        escritor_csv.writerow({coluna: linha[coluna] for coluna in _COLUNAS_CSV})
            except _ERROS_DE_ESCRITA as exc:
                # evento honesto: o log registra a FALHA antes do exit 1
                manifesto.registrar_evento(
                    tipo="consultar_concluido",
                    comando="consultar",
                    detalhe={
                        **detalhe_evento,
                        "escrita": "falha",
                        "erro_export": str(exc),
                    },
                )
                typer.echo(f"ERRO: falha ao gravar o CSV em {saida}: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            typer.echo(f"CSV gravado em {saida}")

        # evento só DEPOIS da tentativa de escrita — nunca afirma export
        # concluída que não aconteceu (AD-10)
        manifesto.registrar_evento(
            tipo="consultar_concluido",
            comando="consultar",
            detalhe={**detalhe_evento, "escrita": "ok"},
        )


@app.command()
def custodia(
    edital: str | None = typer.Option(
        None,
        "--edital",
        help="ID do Edital no Manifesto (coluna EDITAL do 'consultar').",
    ),
    saida: Path | None = typer.Option(
        None,
        "--saida",
        help="Grava o JSON UTF-8 (indent=2) neste caminho; sem ela, imprime no stdout.",
    ),
) -> None:
    """§10/UJ-3: cadeia de custódia de UM Edital, montada só do Manifesto.

    Reconstrói captura (hash SHA-256, URL de origem, versão do crawler,
    caminho, predecessor) → datação (método, ano aceito e evidências brutas
    por fonte) → decisão humana da fila quando existir — sempre pela MESMA
    linha vigente da listagem. Nenhum PDF nem arquivo do corpus é lido
    (AD-4). Falha de gravação do arquivo vira exit 1 SEM fallback no stdout
    (o JSON não vaza parcial); sem ``--saida``, o stdout é o destino.
    """
    if edital is None or not edital.strip():
        _recusar_flag("--edital exige um identificador não vazio.")
    id_edital = edital.strip()
    _recusar_saida_no_manifesto(saida)

    caminho_manifesto = _caminho_manifesto()
    if not Path(caminho_manifesto).exists():
        typer.echo(
            f"ERRO: Manifesto não encontrado em {caminho_manifesto}. "
            "Rode 'agente-editais mapa validar' para criá-lo.",
            err=True,
        )
        raise typer.Exit(code=1)

    with uso_manifesto() as manifesto:
        dados = manifesto.custodia_do_edital(id_edital)
        if dados is None:
            typer.echo(
                f"ERRO: Edital '{id_edital}' não existe no Manifesto. "
                "Veja os IDs na coluna EDITAL do 'agente-editais consultar'.",
                err=True,
            )
            raise typer.Exit(code=1)
        conteudo = json.dumps(dados, ensure_ascii=False, indent=2)

        def _registrar(escrita: str, **extra: str) -> None:
            manifesto.registrar_evento(
                tipo="custodia_concluida",
                comando="custodia",
                detalhe={
                    "edital": id_edital,
                    "documentos": len(dados["documentos"]),
                    "json": str(saida) if saida else None,
                    "escrita": escrita,
                    **extra,
                },
            )

        if saida is None:
            typer.echo(conteudo)
            _registrar("ok")
            return

        try:
            saida.write_text(conteudo + "\n", encoding="utf-8")
        except _ERROS_DE_ESCRITA as exc:
            # evento honesto registra a falha; SEM fallback no stdout
            _registrar("falha", erro_export=str(exc))
            typer.echo(f"ERRO: falha ao gravar o JSON em {saida}: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            f"Custódia do edital {id_edital}: {len(dados['documentos'])} documento(s) "
            f"— JSON gravado em {saida}"
        )
        _registrar("ok")


@app.command()
def status() -> None:
    """Resumo do Manifesto: engine, schema, contagens e últimos eventos."""
    caminho = _caminho_manifesto()
    if not Path(caminho).exists():
        typer.echo(
            f"ERRO: Manifesto não encontrado em {caminho}. "
            "Rode 'agente-editais mapa validar' para criá-lo.",
            err=True,
        )
        raise typer.Exit(code=1)

    with uso_manifesto() as manifesto:
        typer.echo(f"Manifesto:       {caminho}")
        typer.echo(
            f"SQLite engine:   {sqlite3.sqlite_version} (guard ≥ {'.'.join(map(str, ENGINE_MINIMA))})"
        )
        typer.echo(f"Schema version:  {manifesto.schema_version()}")
        typer.echo(f"Instituições:    {manifesto.contar_instituicoes()}")
        typer.echo(f"Portais:         {manifesto.contar_portais()}")
        typer.echo(f"Candidatos:      {manifesto.contar_candidatos()}")
        typer.echo(f"Seções visitadas:{manifesto.contar_secoes_visitadas()}")
        typer.echo(f"Editais (L1):    {manifesto.contar_editais()}")
        typer.echo(f"Documentos:      {manifesto.contar_documentos()}")
        typer.echo(
            f"Datados (CAP-3): {manifesto.contar_documentos_datados()} "
            "(automáticos + decididos em fila)"
        )
        typer.echo(
            f"Catálogo L2:     {manifesto.contar_catalogo_l2()} campo(s) válido(s) "
            "(verificacao='ok'; invalidados ficam fora da contagem)"
        )
        typer.echo(
            f"Fila revisão:    {manifesto.contar_fila('pendente')} pendente(s), "
            f"{manifesto.contar_fila('resolvida')} resolvida(s)"
        )
        typer.echo("")
        typer.echo("Últimos eventos:")
        eventos = manifesto.ultimos_eventos(limite=10)
        if not eventos:
            typer.echo("  (nenhum evento registrado)")
        for evento in reversed(eventos):
            typer.echo(
                f"  #{evento['id']} {evento['ts']} [{evento['comando']}] {evento['tipo']} {evento['detalhe']}"
            )


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")  # PYTHONUTF8=1 na prática
        except Exception:
            pass
    app()


if __name__ == "__main__":
    main()
