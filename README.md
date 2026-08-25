# agente-editais

Agente de coleta e preservação de editais de fomento/bolsas/incubação dos
portais institucionais da **Rede Federal EPCT** — piloto de pesquisa
acadêmica (PPGCS/UFBA). Pipeline em lotes orientado ao Manifesto SQLite;
nenhum serviço residente, nenhum scheduler (spine AD-1).

> **Escopo atual (Story 6):** fundação + Mapa-Mestre + pré-voo + descoberta
> híbrida (CAP-2) + **coleta de PDFs** (CAP-4) + **texto por documento**
> (CAP-6) + **datação multi-fonte com fila humana** (CAP-3): evidência bruta
> por fonte consultada (`url`/`ancora`/`pdf_meta`), regras de aceite na
> janela 2019–2026 e Fila de Revisão Manual com decisão fundamentada +
> **consulta e exportação ONLY-leitura** (CAP-9/UJ-3/§10): `consultar` com
> contagens coerentes e CSV, `custodia --edital` com a cadeia completa em
> JSON (schema v5). `<time>`/CSS, backfill Wayback, OCR, catálogo L2 e a
> view pública são passos futuros — `metodo_datacao` só é preenchido pelo
> aceite da datação.

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
uv run agente-editais textuar --portal IFBA     # CAP-6 para os portais da sigla
uv run agente-editais textuar --todos           # CAP-6 para todos os portais
uv run agente-editais datar --portal IFBA       # CAP-3 para os portais da sigla
uv run agente-editais datar --todos             # CAP-3 para todos os portais
uv run agente-editais fila listar               # itens pendentes com motivo e evidências
uv run agente-editais fila decidir --id 1 --ano 2023 \
    --justificativa "Capa declara 2023." --autor "Pesquisadora"
uv run agente-editais consultar                  # CAP-9: lista o catálogo L1
uv run agente-editais consultar --instituicao IFES --ano 2024 \
    --categoria agencia_inovacao                 # filtros combinam por E (AND)
uv run agente-editais consultar --saida catalogo.csv   # exporta CSV UTF-8 (vírgula)
uv run agente-editais custodia --edital ifba-2023-edital-x --saida custodia.json
uv run agente-editais status         # resumo do Manifesto + últimos eventos
```

Em todos os comandos com seleção de alvo, `--portal SIGLA` é insensível a
caixa: `--portal ifba` equivale a `--portal IFBA` (`descobrir`, `coletar`,
`textuar`, `datar`).

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
- `*.texto-tmp-*` residuais — temporários da gravação atômica do `.txt`
  (escreve no temporário e renomeia): um crash entre write e rename pode
  deixá-los para trás. Podem ser apagados manualmente; JAMAIS ocupam o
  caminho canônico do `.txt` irmão, que só aparece completo ou não aparece.
- `*.txt` ao lado dos PDFs — artefatos derivados do estágio de texto (ver
  abaixo).

### Texto (CAP-6)

- **Extrator único (AD-11):** `textuar` é o único estágio que parseia PDF;
  datação e verificação de citações consomem sua saída. Usa pypdf com
  extração tolerante por página — página que falha rende string vazia sem
  matar o documento; se TODAS falharem, o documento vira candidato a OCR
  (`flag_escaneado=true` + `falha_parsing_total` no evento).
- **`.txt` irmão:** mesmo nome/caminho do PDF com extensão `.txt`, UTF-8.
  O PDF original fica intacto (AD-2) — o txt é artefato derivado novo.
- **Flag escaneado:** `flag_escaneado=true` quando os caracteres extraíveis
  ficam abaixo de `[texto] limiar_chars_por_pagina` × nº de páginas (default
  100). OCR automático NÃO existe — a flag só sinaliza para resgate sob
  demanda futuro.
- **Proveniência (schema v4):** colunas `texto_caminho`, `texto_chars`,
  `texto_paginas` e `extraido_em` em `documentos`, mais eventos
  `texto_extraido`/`texto_escaneado`/`arquivo_ausente`/`texto_erro`. A
  migração é `ALTER TABLE` irreversível (sem downgrade): o backup continua
  exigindo `PRAGMA wal_checkpoint(TRUNCATE)` antes de copiar a pasta, um
  Manifesto mais novo que o agente segue recusado no startup — versões
  mistas do agente exigem upgrade coordenado.
- **Retomável e idempotente:** re-executar pula documentos já extraídos cujos
  bytes ainda batem no hash do L1 e cujo `.txt` existe — pulados não são
  re-parseados nem re-baixados, mas os bytes locais são re-hasheados
  (SHA-256) para verificar a vigência; documento novo ou com bytes alterados
  é re-extraído (nova versão ⇒ novo `.txt`). Falha pontual (PDF corrompido,
  arquivo sumido) vira evento e o lote segue — exit 0.
- **Sem rede:** a textuação é 100% local — a janela off-peak não se aplica.

### Datação multi-fonte e fila humana (CAP-3)

`datar` é 100% OFFLINE (sem rede, sem janela off-peak) e decide a Data de
Publicação no Portal consultando TODAS as fontes locais disponíveis:

- **Fontes da cascata (`url → ancora → pdf_meta`):**
  - `url` — padrão de ano (2019–2026) nos segmentos do caminho;
  - `ancora` — texto do link capturado pela descoberta desde a Story 5
    (`candidatos.texto_ancora`, guardado integralmente; truncado só no
    detalhe do evento);
  - `pdf_meta` — docinfo do PDF (criação/modificação/título), lido SEMPRE
    pela função `ler_metadados` do extrator único (AD-11: nada parseia PDF
    fora de `texto.py`). Datas `D:AAAAMMDD…` têm os quatro primeiros dígitos
    lidos como ano; título entra como texto livre.
- **Evidência bruta por fonte (FR-6):** cada fonte disponível rende uma
  linha em `evidencias_datacao` (fonte, valor bruto, localização) — a prova
  do que foi consultado, inclusive quando não produz ano. Re-executar
  substitui a própria linha (PK documento+url+fonte), nunca duplica.
  **Limitação do corpus antigo:** documentos descobertos antes da Story 5
  não têm âncora gravada — essa fonte simplesmente fica indisponível (a
  evidência não nasce para ela); re-descobrir o portal RETROALIMENTA as
  âncoras ausentes (preenche só quando NULL; âncora já gravada nunca é
  sobrescrita).
- **Regras de aceite (janela fixa 2019–2026):**
  - um único ano candidato → ACEITO se corroborado por ≥2 fontes OU produzido
    por fonte ≠ url; `metodo_datacao` = PRIMEIRA fonte da cascata cujo valor
    converge para o consenso + `ano_aceito` (evento `datacao_aplicada`);
  - só-URL sem corroboração → FILA `baixa_confianca_sourl` (baixa confiança —
    regra que cobre também os anos-limite 2019/2026, que exigem corroboração
    interna);
  - ≥2 anos distintos entre fontes → FILA `divergencia` (humano resolve; sem
    voto automático; sinal de qualidade no evento);
  - nenhum ano na janela → FILA `sem_data`; ano fora da janela NUNCA é
    aceito — fica como evidência bruta e, se for o caso único, motiva a fila.
  Nada é descartado sem enfileirar: cada documento termina COM
  `metodo_datacao`+evidências OU com item ativo na fila — **exceto** o de
  falha de LEITURA (ver Idempotência e erros abaixo).
- **Motivos da fila:** `sem_data` — nenhuma fonte produz ano dentro da
  janela (inclui caso único fora dela); `baixa_confianca_sourl` — único ano
  vem só da URL, sem corroboração interna; `divergencia` — ≥2 anos distintos
  entre fontes.
- **Fila de Revisão Manual (FR-8):** `fila_revisao` preserva URL, portal,
  motivo e aponta para as evidências coletadas; UNIQUE parcial impõe um item
  pendente por URL no banco. Decisão via CLI:
  - `fila listar [--status pendente|resolvida|todas]` — itens com motivo e
    evidências;
  - `fila decidir --id N (--ano AAAA|--excluir) --justificativa T --autor T
    [--evidencia T]` — grava ano atribuído OU exclusão + justificativa
    OBRIGATÓRIA + autoria + data (tudo na própria linha + evento
    `fila_decidida`). Recusa decidir sem justificativa/autoria, com destino
    ausente ou duplo, ou com ano fora da janela — nada é gravado (exit 2).
  Item resolvido não é redecidido; re-executar `datar` pula tanto datados
  quanto urls com QUALQUER item de fila (pendente ou resolvido) — zero
  re-decisões e fila intacta entre execuções. Nota: a decisão humana vive em
  `fila_revisao` (com sua autoria/justificativa); `documentos.metodo_datacao`
  permanece reservado aos métodos da cascata.
- **Idempotência e erros:** PDF sumido/corrompido pós-coleta vira evento
  `datacao_erro` fase `leitura` e o lote segue (exit 0). **Esse documento
  fica SEM destino nesta passada** — nem aceite, nem fila — até o reparo dos
  bytes: a retomada o re-tenta automaticamente na próxima execução. Falha de
  persistência vira `datacao_erro` fase `persistencia`. Alias
  (`referencia_para`) é datado como linha própria (mesma mídia, evidências
  próprias).
- **Movimentação física** dos PDFs de `_sem_ano/` para a pasta do ano
  definitivo fica para decisão futura — nesta etapa só o banco muda.

### Consulta e exportação (CAP-9)

Dois comandos **ONLY-leitura** sobre os dados do Manifesto (AD-3/AD-4):
nenhuma rede, nenhum parseio de PDF, nenhum acesso ao `corpus/` — toda
linha vem do banco. A ÚNICA escrita é o evento append-only de custódia
(`consultar_concluido`/`custodia_concluida`, AD-10), registrado após a
tentativa de export com o resultado honesto (`"escrita": "ok" | "falha"`).

```bash
uv run agente-editais consultar [--instituicao SIGLA] [--ano AAAA] \
    [--categoria integra|nit|prpgi_prppg|agencia_inovacao] [--saida CAMINHO.csv]
uv run agente-editais custodia --edital ID [--saida CAMINHO.json]
```

- **Unidade = Documento** (`edital_id` é uma coluna; agregar linhas por
  Edital fica para decisão futura). Filtros combinam por E; zero resultados
  é sucesso (exit 0 — pesquisar hipóteses vazias não é erro).
- **Ano efetivo e origem são colunas explícitas:** `ano` =
  `COALESCE(ano_aceito, decidido_ano da fila)` e `ano_fonte` ∈
  `automatica` (datação convergente), `fila_humana` (decisão com ano
  atribuído) ou `vazio` (pendente na fila ou sem destino — nada desaparece
  silenciosamente; anos vazios ordenam POR ÚLTIMO).
- **Excluídos:** ficam FORA da listagem e aparecem no resumo
  (`excluídos: N`). A contagem de excluídos aplica instituição/categoria,
  mas **IGNORA o filtro `--ano`** — a população excluída não participa da
  janela de listagem (exclusão não tem ano), então `consultar --ano 2024`
  continua mostrando quantos foram excluídos na instituição pedida em vez
  de um "excluídos: 0" enganoso.
- **Contagens coerentes (FR-20):** o resumo impresso deriva da MESMA query
  das linhas — nunca de contagem paralela.
- **CSV (`--saida`):** stdlib, UTF-8 sem BOM, separador vírgula, cabeçalho
  completo mesmo com zero linhas; grava somente no caminho dado. A listagem
  e as contagens saem no terminal ANTES da tentativa de escrita — falha de
  gravação vira exit 1 SEM esconder o resultado da consulta. Para Excel,
  use importação explícita; notebooks/pandas consomem direto (Story 9).
- **Custódia (§10):** JSON pretty UTF-8 (`ensure_ascii=False, indent=2`)
  com metadados autodescritivos no raiz (`gerado_em` UTC, `versao_agente`,
  `schema_version`) reconstruindo, por documento do edital, captura (data
  UTC, hash SHA-256, URL de origem, versão do crawler, caminho, predecessor)
  → datação (método, ano aceito, evidências brutas por fonte com
  valor/localização) → bloco da fila quando existir (motivo, status e
  decisão humana: ano/exclusão, justificativa, autoria, data). O bloco da
  fila usa a MESMA linha vigente por URL da listagem (resolvida vence
  pendente; entre resolvidas, a mais recente). Edital existente SEM
  documentos sai 0 com `"documentos": []`. Sem `--saida`, imprime no
  stdout; com `--saida`, falha de gravação vira exit 1 SEM fallback no
  stdout (o JSON não vaza parcial).
- Flags malformadas são recusadas ANTES de abrir o banco (exit 2):
  `--instituicao` vazia, `--categoria` fora do CHECK do banco, `--ano`
  fora de 2019–2026, `--edital` vazio e `--saida` apontando para o PRÓPRIO
  Manifesto (o export truncaria o banco). Manifesto inexistente ou edital
  desconhecido ⇒ exit 1; falha de escrita do CSV/JSON ⇒ exit 1.
- **Limitações:** filtros de dimensão/valor de campo do catálogo L2
  (Stories 7–8), a view pública `v_catalogo_publicavel` (Story 8) e a
  agregação por Edital ainda não existem; a consulta reflete o estado
  L1+fila corrente.


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
| 0 | sucesso (inclusive pré-voo com seeds inacessíveis, descoberta, coleta, textuar e datar com falhas por portal/documento, e `consultar`/`custodia` com zero resultados) |
| 1 | erro operacional (Manifesto ausente, banco mais novo que o agente, falha de abertura, janela off-peak exigida fora da janela — probe ou crawling, sigla desconhecida no `descobrir`/`coletar`/`textuar`/`datar`, portal ausente do Manifesto; item de fila inexistente ou já resolvido no `fila decidir`; Manifesto inexistente ou edital desconhecido no `consultar`/`custodia`; falha de escrita do CSV/JSON) |
| 2 | configuração inválida — mapa-mestre.toml **ou** politeness.toml, ou flags malformadas do `descobrir`/`coletar`/`textuar`/`datar` (`--portal` vazio, `--portal` com `--todos`), da decisão na fila (`fila decidir`: sem justificativa/autoria, destino ausente ou duplo, ano fora da janela), do filtro de listagem (`fila listar --status` inválido), dos filtros do `consultar` (`--instituicao` vazia, `--categoria` fora do CHECK, `--ano` fora de 2019–2026) e do `custodia` (`--edital` vazio) — todos validados antes de abrir o banco; também `--saida` apontando para o próprio Manifesto no `consultar`/`custodia` |
| 3 | engine SQLite abaixo do guard ≥ 3.51.3 |
| 4 | Manifesto ocupado por outro processo |

## Configuração e dados

- `configs/mapa-mestre.toml` — cadastro curado (instituições → portais →
  categoria → seeds). Versionado em git; alteração exige commit (AD-9).
- `configs/politeness.toml` — delay mínimo por host, janela off-peak (fuso do
  host), User-Agent acadêmico e knobs de crawl: cap de tamanho por documento
  (`[crawl] max_mb_documento`), prazo/timeout de download
  (`download_prazo_s`, `download_timeout_s`) e limite de 403 consecutivos
  que suspende um host (`max_403_consecutivos`); além do limiar de PDF
  escaneado (`[texto] limiar_chars_por_pagina`, default 100) usado pelo
  extrator único.
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
