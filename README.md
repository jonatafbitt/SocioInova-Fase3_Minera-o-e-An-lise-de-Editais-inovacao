# agente-editais

Agente de coleta e preservação de editais de fomento/bolsas/incubação dos
portais institucionais da **Rede Federal EPCT** — piloto de pesquisa
acadêmica (PPGCS/UFBA). Pipeline em lotes orientado ao Manifesto SQLite;
nenhum serviço residente, nenhum scheduler (spine AD-1).

> **Escopo atual (Story 3):** fundação + Mapa-Mestre + pré-voo + descoberta
> híbrida (CAP-2) + **coleta de PDFs** (CAP-4): download com Registro L1
> nascido na captura, dedupe por hash intra-portal, crawl retomável e
> suspensão de host bloqueado. Texto/datação/catálogo vêm nas stories
> seguintes — `flag_escaneado` e `metodo_datacao` nascem NULL no L1.

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
uv run agente-editais coletar --portal IFBA     # CAP-4 para os portais da sigla
uv run agente-editais coletar --todos           # CAP-4 para todos os portais
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

### Coleta (CAP-4)

- `coletar` consome `candidatos WHERE tipo='pdf'` e baixa VIA fetcher único
  (robots.txt, delay por host, redirects hop-a-hop, janela off-peak obrigatória).
- **L1 na captura:** cada PDF gravado nasce com registro completo em
  `documentos` — id = 12 hex iniciais do SHA-256 dos bytes, verificado
  pós-gravação (write-once, AD-2). `flag_escaneado`/`metodo_datacao` ficam
  NULL para as stories de texto/datação. Dedupe e restauração comparam
  sempre o hash SHA-256 **completo** — nunca o prefixo do id (colisão de
  prefixo não vira alias/restauração falsa).
- **Pastas:** `corpus/{INSTITUICAO}/{ano|_sem_ano}/<hash12>-<slug>.pdf`; o ano
  é provisório (padrão 2019–2026 inequívoco na URL) — ano ausente, ambíguo
  ou FORA da janela (ex.: 2027) vai para `_sem_ano/` nesta fase; a datação
  (Story 5) confirma/move depois.
- **Dedupe por hash intra-portal:** mesmos bytes em outra URL ⇒ um Documento
  só + segundo registro com `referencia_para` — criado só se os bytes do
  canônico existem no disco e batem no hash. Cruzar portais é decisão
  humana (PRD OQ-4), nunca automática.
- **Retomável:** re-executar não re-baixa nada íntegro; arquivo ausente ou
  corrompido é re-baixado — hash igual restaura o documento (corrigindo
  caminho stale no L1 se o destino recalculado mudou), hash diferente cria
  nova versão ligada por `predecessor_id`. Interrupções no meio do lote
  retomam exatamente dali (estado no Manifesto).
- **Suspensão de host:** `[crawl] max_403_consecutivos` (default 3) HTTP 403
  seguidos suspendem o host e pulam o **restante DO HOST** na execução —
  inclusive nos outros portais do mesmo hostname num `coletar --todos`;
  URLs puladas contam no relatório (`Puladas por suspensão de host`) e o
  gatilho gera evento `host_suspenso`. A suspensão vale POR EXECUÇÃO — o
  host tenta de novo no próximo crawl.
- **Cap de tamanho:** `[crawl] max_mb_documento` (default 50 MB; inf/nan
  recusados) — resposta maior aborta ANTES de gravar e vira evento
  `tamanho_excedido`.
- **Prazos de download:** `[crawl] download_prazo_s` (default 300 s) derruba
  trickle que pendura o lote; `[crawl] download_timeout_s` (default 60 s) é
  o timeout só da leitura do corpo, separado do timeout do probe.
- **Falhas não abortam o lote:** erro de I/O (disco/quarentena/mkdir/hash
  pós-mover) ou registro já existente (ciclo A→B→A) viram evento + perda
  daquele candidato; a coleta segue.

#### Higiene do `corpus/`

- `corpus/.tmp/captura-<pid>-*.pdf` — temporários de download; cada execução
  limpa APENAS os do próprio PID (`limpar_temporarios`), então arquivos de
  processos mortos podem acumular — remova-os manualmente com o agente parado.
- `*.corrompido-<ts>-<uuid8>.pdf` ao lado dos originais — bytes danificados
  em quarentena auditável (evento `bytes_em_quarentena`); apague após
  investigar. Nada dentro de `corpus/` é reescrito (AD-2).

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
| 0 | sucesso (inclusive pré-voo com seeds inacessíveis, descoberta e coleta com falhas por portal) |
| 1 | erro operacional (Manifesto ausente, banco mais novo que o agente, falha de abertura, janela off-peak exigida fora da janela — probe ou crawling, sigla desconhecida no `descobrir`/`coletar`, portal ausente do Manifesto) |
| 2 | configuração inválida — mapa-mestre.toml **ou** politeness.toml, ou flags malformadas do `descobrir`/`coletar` (`--portal` vazio, `--portal` com `--todos`) |
| 3 | engine SQLite abaixo do guard ≥ 3.51.3 |
| 4 | Manifesto ocupado por outro processo |

## Configuração e dados

- `configs/mapa-mestre.toml` — cadastro curado (instituições → portais →
  categoria → seeds). Versionado em git; alteração exige commit (AD-9).
- `configs/politeness.toml` — delay mínimo por host, janela off-peak (fuso do
  host), User-Agent acadêmico e knobs de crawl: cap de tamanho por documento
  (`[crawl] max_mb_documento`), prazo/timeout de download
  (`download_prazo_s`, `download_timeout_s`) e limite de 403 consecutivos
  que suspende um host (`max_403_consecutivos`).
- `dados/manifesto.sqlite3` — Manifesto (estado único; criado no primeiro
  comando que grava). **Fora do git**; backup = copiar pasta após
  `PRAGMA wal_checkpoint(TRUNCATE)`.
- `corpus/` — PDFs coletados (`{INSTITUICAO}/{ano|_sem_ano}/`). **Fora do
  git**; mesmo regime de backup do Manifesto.

### Variáveis de ambiente

| Variável | Default | Função |
| -------- | ------- | ------ |
| `AGENTE_EDITAIS_CONFIGS` | `<raiz>/configs` | diretório dos configs |
| `AGENTE_EDITAIS_MANIFESTO` | `<raiz>/dados/manifesto.sqlite3` | caminho do Manifesto |
| `AGENTE_EDITAIS_CORPUS` | `<raiz>/corpus` | raiz do corpus de PDFs |

## Desenvolvimento

```bash
uv run python -m pytest -q    # suíte completa (servidor HTTP local fake)
```

Arquitetura e invariantes: `_bmad-output/planning-artifacts/architecture/`
(ARCHITECTURE-SPINE.md) e SPEC do projeto.
