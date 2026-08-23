"""CLI — superfície do pipeline em lotes (AD-1): um comando por execução.

Subcomandos desta story: ``mapa validar``, ``preflight`` e ``status``.

Códigos de saída:
- 0  sucesso (inclusive pré-voo com seeds inacessíveis — o lote segue, FR-2);
- 1  erro operacional genérico (ex.: Manifesto inexistente no ``status``);
- 2  Mapa-Mestre inválido (nada é escrito — banco intocado);
- 3  engine SQLite abaixo do guard AD-10;
- 4  Manifesto ocupado por outro processo (lock de startup, AD-3).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import typer

from .fetcher import (
    ErroConfigPolidez,
    Polidez,
    carregar_polidez,
    dentro_da_janela_off_peak,
    probe_seed,
)
from .manifest import (
    ENGINE_MINIMA,
    ErroEngineIncompativel,
    ErroManifestoOcupado,
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


def _abrir_manifesto() -> Manifesto:
    try:
        return Manifesto(_caminho_manifesto())
    except ErroEngineIncompativel as exc:
        typer.echo(f"ERRO: {exc}", err=True)
        raise typer.Exit(code=3) from exc
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

    with _abrir_manifesto() as manifesto:
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
    polidez, _ = _carregar_polidez_segura()
    pares = mapa.seeds_unicas()
    if not pares:
        typer.echo("Nenhuma seed declarada no Mapa-Mestre.")
        raise typer.Exit(code=2)

    typer.echo(f"Pré-voo de {len(pares)} seeds (delay ≥ {polidez.delay_minimo_s:g}s/host)...")
    acessiveis: list[str] = []
    inacessiveis: list[str] = []
    off_peak_agora = dentro_da_janela_off_peak(polidez.off_peak)

    with _abrir_manifesto() as manifesto:
        for indice, (instituicao, portal, _url) in enumerate(pares, start=1):
            resultado = probe_seed(_url, polidez)
            if resultado.ok:
                acessiveis.append(resultado.url)
                marcador = "[OK   ]"
                detalhe_extra = f"HTTP {resultado.status_http} via {resultado.metodo}"
            else:
                inacessiveis.append(resultado.url)
                marcador = "[FALHA]"
                detalhe_extra = str(resultado.erro)
            typer.echo(
                f"({indice}/{len(pares)}) {marcador} {resultado.url} "
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
                    "dentro_janela_off_peak": off_peak_agora,
                },
            )

        manifesto.registrar_evento(
            tipo="preflight_concluido",
            comando="preflight",
            detalhe={
                "seeds": len(pares),
                "acessiveis": len(acessiveis),
                "inacessiveis": len(inacessiveis),
            },
        )

    typer.echo("")
    typer.echo(f"Acessíveis:   {len(acessiveis)}")
    for url in acessiveis:
        typer.echo(f"  - {url}")
    typer.echo(f"Inacessíveis: {len(inacessiveis)}")
    for url in inacessiveis:
        typer.echo(f"  - {url}")
    typer.echo("Pré-voo concluído; lote não abortado por falhas de seed (FR-2).")


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

    with _abrir_manifesto() as manifesto:
        typer.echo(f"Manifesto:       {caminho}")
        typer.echo(f"SQLite engine:   {sqlite3.sqlite_version} (guard ≥ {'.'.join(map(str, ENGINE_MINIMA))})")
        typer.echo(f"Schema version:  {manifesto.schema_version()}")
        typer.echo(f"Instituições:    {manifesto.contar_instituicoes()}")
        typer.echo(f"Portais:         {manifesto.contar_portais()}")
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
