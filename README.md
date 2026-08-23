# agente-editais

Agente de coleta e preservação de editais de fomento/bolsas/incubação dos
portais institucionais da **Rede Federal EPCT** — piloto de pesquisa
acadêmica (PPGCS/UFBA). Pipeline em lotes orientado ao Manifesto SQLite;
nenhum serviço residente, nenhum scheduler (spine AD-1).

> **Escopo atual (Story 1):** fundação + Mapa-Mestre + pré-voo de seeds.
> **Crawling/renderização real chega na Story 2** — o `preflight` desta fase
> só verifica alcançabilidade das seeds, sem navegar nem baixar nada.

## Instalação

Requisitos: [uv](https://docs.astral.sh/uv/) ≥ 0.12. Python/SQLite são
gerenciados pelo uv (`python-preference = "only-managed"` garante engine
SQLite ≥ 3.51.3 exigida pelo guard de startup).

```bash
uv sync
```

## Uso

```bash
uv run agente-editais mapa validar   # valida o Mapa-Mestre e sincroniza com o Manifesto
uv run agente-editais preflight      # pré-voo: testa cada seed; falhas não abortam o lote
uv run agente-editais status         # resumo do Manifesto + últimos eventos
```

## Códigos de saída

| Código | Significado |
| ------ | ----------- |
| 0 | sucesso (inclusive pré-voo com seeds inacessíveis) |
| 1 | erro operacional (Manifesto ausente, banco mais novo que o agente, falha de abertura, violação de janela off-peak) |
| 2 | configuração inválida — mapa-mestre.toml **ou** politeness.toml |
| 3 | engine SQLite abaixo do guard ≥ 3.51.3 |
| 4 | Manifesto ocupado por outro processo |

## Configuração e dados

- `configs/mapa-mestre.toml` — cadastro curado (instituições → portais →
  categoria → seeds). Versionado em git; alteração exige commit (AD-9).
- `configs/politeness.toml` — delay mínimo por host, janela off-peak (fuso do
  host), User-Agent acadêmico.
- `dados/manifesto.sqlite3` — Manifesto (estado único; criado no primeiro
  comando que grava). **Fora do git**; backup = copiar pasta após
  `PRAGMA wal_checkpoint(TRUNCATE)`.

### Variáveis de ambiente

| Variável | Default | Função |
| -------- | ------- | ------ |
| `AGENTE_EDITAIS_CONFIGS` | `<raiz>/configs` | diretório dos configs |
| `AGENTE_EDITAIS_MANIFESTO` | `<raiz>/dados/manifesto.sqlite3` | caminho do Manifesto |

## Desenvolvimento

```bash
uv run python -m pytest -q    # suíte completa (servidor HTTP local fake)
```

Arquitetura e invariantes: `_bmad-output/planning-artifacts/architecture/`
(ARCHITECTURE-SPINE.md) e SPEC do projeto.
