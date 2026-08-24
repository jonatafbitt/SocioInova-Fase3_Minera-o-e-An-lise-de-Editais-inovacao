"""CLI — superfície do pipeline em lotes (AD-1): um comando por execução.

Subcomandos desta story: ``mapa validar``, ``preflight``, ``descobrir``,
``coletar`` e ``status``.

Códigos de saída:
- 0  sucesso (inclusive pré-voo com seeds inacessíveis, descoberta e coleta
       com falhas por portal/URL — o lote segue, FR-2/CAP-4);
- 1  erro operacional genérico: Manifesto inexistente no ``status``,
       Manifesto mais novo que o agente, falha de abertura do banco,
       violação de janela off-peak (pré-voo exigente OU crawling),
       sigla desconhecida no ``descobrir``/``coletar`` ou portal ausente
       do Manifesto;
- 2  configuração declarativa inválida — compartilhado entre mapa-mestre.toml
       e politeness.toml (nada é escrito; banco intocado) — ou flags de
       ``descobrir``/``coletar`` malformadas (--portal/--todos);
- 3  engine SQLite abaixo do guard AD-10;
- 4  Manifesto ocupado por outro processo (lock AD-3, no startup OU na gravação).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import typer

from .coleta import ContextoColeta, ResumoColetaPortal, coletar_portal, limpar_temporarios
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

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Agente de Editais de Inovação da Rede Federal EPCT — piloto PPGCS/UFBA.",
)
mapa_app = typer.Typer(no_args_is_help=True, help="Operações sobre o Mapa-Mestre (CAP-1).")
app.add_typer(mapa_app, name="mapa")

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

            def _registrar_seed(
                indice: int, _total: int, resultado: ResultadoSeed
            ) -> None:
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
                    except Exception:  # noqa: BLE001 — não mascarar a causa original
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

            with nova_sessao(polidez.user_agent) as sessao:
                for contexto in contextos:
                    resumos.append(
                        navegar_portal(contexto, manifesto, polidez, sessao=sessao)
                    )
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
                    "urls_perdidas": [
                        url for resumo in resumos for url in resumo.urls_perdidas
                    ],
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
                    "urls_perdidas": [
                        url for resumo in resumos for url in resumo.urls_perdidas
                    ],
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
            typer.echo(
                f"  Puladas por suspensão de host: {resumo.puladas_host_suspenso}"
            )
        for perdida in resumo.urls_perdidas:
            typer.echo(f"  Perdida:    {perdida}")

    total_baixados = sum(r.baixados + r.novas_versoes + r.restaurados for r in resumos)
    typer.echo("")
    if total_puladas_suspensao:
        typer.echo(
            f"URLs puladas por suspensão de host nesta execução: {total_puladas_suspensao}"
        )
    typer.echo(
        f"Coleta concluída: {len(resumos)} portal(is), {total_baixados} documento(s) "
        f"gravados em {raiz_corpus} "
        "(L1 completo; eventos no Manifesto; lote não abortado)."
    )


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
        typer.echo(f"SQLite engine:   {sqlite3.sqlite_version} (guard ≥ {'.'.join(map(str, ENGINE_MINIMA))})")
        typer.echo(f"Schema version:  {manifesto.schema_version()}")
        typer.echo(f"Instituições:    {manifesto.contar_instituicoes()}")
        typer.echo(f"Portais:         {manifesto.contar_portais()}")
        typer.echo(f"Candidatos:      {manifesto.contar_candidatos()}")
        typer.echo(f"Seções visitadas:{manifesto.contar_secoes_visitadas()}")
        typer.echo(f"Editais (L1):    {manifesto.contar_editais()}")
        typer.echo(f"Documentos:      {manifesto.contar_documentos()}")
        typer.echo("")
        typer.echo("Últimos eventos:")
        eventos = manifesto.ultimos_eventos(limite=10)
        if not eventos:
            typer.echo("  (nenhum evento registrado)")
        for evento in reversed(eventos):
            typer.echo(f"  #{evento['id']} {evento['ts']} [{evento['comando']}] {evento['tipo']} {evento['detalhe']}")


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")  # PYTHONUTF8=1 na prática
        except Exception:  # noqa: BLE001 — stream sem reconfigure (ex.: capturas de teste)
            pass
    app()


if __name__ == "__main__":
    main()
