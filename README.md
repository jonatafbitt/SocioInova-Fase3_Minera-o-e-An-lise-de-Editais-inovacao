# agente-editais

Agente de coleta e preservação de editais de fomento/bolsas/incubação dos
portais institucionais da **Rede Federal EPCT** — piloto de pesquisa
acadêmica (PPGCS/UFBA). Pipeline em lotes orientado ao Manifesto SQLite;
nenhum serviço residente, nenhum scheduler (spine AD-1).

> **Escopo atual (Story 2):** fundação + Mapa-Mestre + pré-voo + **descoberta
> híbrida** (CAP-2): navegação por palavras-chave a partir das seeds, com
> HTTP leve por padrão e Playwright no gatilho. Downloads de PDF chegam na
> Story 3 — o `descobrir` desta fase só registra seções visitadas e
> *candidatos* a edital no Manifesto.

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
uv run agente-editais descobrir --portal IFBA   # CAP-2 para os portais da sigla
uv run agente-editais descobrir --todos         # CAP-2 para todos os portais
uv run agente-editais status         # resumo do Manifesto + últimos eventos
```

### Descoberta (CAP-2)

- `robots.txt` é consultado/honrado por host **antes** de qualquer requisição
  ao caminho; robots inacessível ⇒ permite, com evento registrando a decisão.
- Palavras-chave de seção: Inovação, PRPGI, PRPPG, NIT, Agência de Inovação —
  matching insensível a caixa **e** acento, por token inteiro.
- Gatilho Playwright: portal marcado `dinamico = true` no Mapa-Mestre OU
  conteúdo-alvo ausente no HTML estático — troca única, auditada por portal
  em eventos.
- Candidatos são classificação **sintática** (`.pdf` ou slug `edital`/`chamada`),
  deduplicados por URL normalizada intra-portal; profundidade padrão 3 níveis
  por portal (`profundidade_maxima`).
- O crawling **obriga a janela off-peak** do fuso do host
  (`[crawl] respeitar_janela_off_peak`, default `true`); fora dela o comando
  recusa rodar (exit 1). O `preflight` não é afetado.

### Renderização Playwright — verificação manual opcional

A suíte exercita os gatilhos com o engine monkeypatched (sem navegador).
Para um smoke com browser real (opcional, uma vez por ambiente): marque
`dinamico = true` em um portal DE TESTE do seu mapa local (nenhum portal do
mapa piloto versionado está marcado como dinâmico), aponte
`AGENTE_EDITAIS_CONFIGS` para esse diretório e rode `descobrir` contra ele.

```bash
uv run playwright install chromium
uv run playwright install-deps        # Linux apenas
# configs-de-teste/mapa-mestre.toml com um portal 'dinamico = true'
AGENTE_EDITAIS_CONFIGS=./configs-de-teste uv run agente-editais descobrir --portal TST
```

## Códigos de saída

| Código | Significado |
| ------ | ----------- |
| 0 | sucesso (inclusive pré-voo com seeds inacessíveis e descoberta com falhas por portal) |
| 1 | erro operacional (Manifesto ausente, banco mais novo que o agente, falha de abertura, janela off-peak exigida fora da janela — probe ou crawling, sigla desconhecida no `descobrir`, portal ausente do Manifesto) |
| 2 | configuração inválida — mapa-mestre.toml **ou** politeness.toml, ou flags malformadas do `descobrir` (`--portal` vazio, `--portal` com `--todos`) |
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
