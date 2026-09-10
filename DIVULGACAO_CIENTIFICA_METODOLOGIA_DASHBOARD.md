# Mineração e Análise Documental de Editais e Chamadas de Inovação da Rede Federal EPCT

## Nota metodológica de divulgação científica — o dashboard analítico e o pipeline que o alimenta

> **Público-alvo:** cientistas de dados e cientistas sociais.
> Este documento explica, em linguagem dupla, **o que o sistema faz**, **como faz** (nível computacional) e **o que cada resultado significa** (tradução interpretativa). As seções que começam com *[Cientista de dados]* trazem os detalhes técnicos; as que começam com *[Cientista social]*, a tradução conceitual e metodológica para quem trabalha com análise documental sem necessariamente ser familiarizado com as ferramentas.

---

## 1. Contexto e objeto de pesquisa

Este instrumento computacional automatiza a **coleta, preservação, caracterização e análise documental** de editais, chamadas e instrumentos normativos de fomento à inovação publicados pelos **Institutos Federais (Rede Federal de Educação Profissional, Científica e Tecnológica — EPCT)**.

O objeto empírico é o **corpus documental institucional** dessas IFs: editais de bolsas, incubação, extensão tecnológica, inovação social, chamadas de pró-reitorias de pesquisa, agências de inovação e Núcleos de Inovação Tecnológica (NITs). Esses documentos são tratados como **artefatos de discurso institucional** — não como mera transparência passiva, mas como evidência dos enquadramentos de inovação que cada instituição adota, das racionalidades que prioriza (instrumental-mercadológica versus substantiva-emancipatória) e dos atores que nomeia como destinatários das políticas.

### 1.1 Por que isso importa para as ciências sociais

*[Cientista social]* O estudo da inovação no setor público carrega uma tensão paradigmática bem documentada: a inovação pode ser tratada como **motor econômico linear** (patentes, royalties, transferência de tecnologia, sucesso de mercado — racionalidade instrumental) ou como **fenômeno social e emancipatório** (tecnologias sociais, economia solidária, saberes tradicionais, impacto social no território — racionalidade substantiva). Os editais são a materialização escrita dessa escolha: ao redigir critérios de avaliação, reservar cotas ou nomear destinatários, a instituição **faz uma aposta de valor** que pode ser lida e comparada sistematicamente.

O instrumento aqui descrito converte essa leitura interpretativa em um **protocolo auditável**, combinando:

1. **Extrações determinísticas** (dicionários de sinais textuais, concordância KWIC, frequências, co-ocorrências) — para levantar evidências de presença/ausência de temas;
2. **Métodos vetoriais e de redução de dimensionalidade** (TF-IDF + UMAP) — para revelar agrupamentos estruturais no corpus;
3. **Codificação assistida por LLM com verificação de citação** (Catálogo L2) — para extração estruturada de campos analíticos com **antídoto à alucinação**;
4. **Classificação de impacto social (Eixo 3)** — para diferenciar editais instrumentais de editais substantivos, com trecho probatório literal obrigatório.

### 1.2 A arquitetura em camadas

*[Cientista de dados]* O sistema é estruturado em um **pipeline em lotes orientado a estado** (SQLite como fonte única de verdade — o "Manifesto"), com separação rígida entre estágios. Cada estágio escreve **eventos append-only** e é **idempotente e retomável**: re-executar um estágio não refaz trabalho já concluído e não corrompe dados. A filosofia é *write-once, read-many*, com o `corpus/` sendo imutável (política AD-2) e o banco versionado por migrações de schema.

```
Descobrir (crawl) → Coletar (PDF) → Textuar (OCR/extração) → Datar (datação) → Classificar (inovação) → Analisar (L2) → Eixo 3
   CAP-2              CAP-4            CAP-6                  CAP-3            L1                    CAP-7          Quadro 1.6 / Dimensão 3
```

A **varredura** é uma entrada **sob demanda** de descoberta: o pesquisador cola a URL de uma página que lista editais e o sistema navega exatamente aquela página, despejando os candidatos no mesmo pipeline (da coleta ao Eixo 3). Desde a migração de schema **v10**, cada candidato registrado por uma varredura carrega um `varredura_id` — a **proveniência** de cada documento fica rastreável até a página que o revelou.

O **dashboard** é uma camada **somente-leitura** sobre esse estado: ele nunca acessa a rede, nunca grava no banco em competição com a CLI e apenas **visualiza** o que o pipeline produziu (e dispare a execução em subprocessos da CLI). Isso garante que a interface de análise não contamine a coleta.

---

## 2. O pipeline de dados: do crawling à análise

Cada estágio merece uma explicação dupla, porque cada transformação carrega uma decisão metodológica.

### 2.1 Descoberta (crawling — CAP-2)

*[Cientista de dados]* O `descobrir` navega os portais institucionais cadastrados no `mapa-mestre.toml`, honrando `robots.txt`, respeitando uma **janela off-peak** do fuso do host (para não sobrecarregar servidores públicos) e usando delay mínimo por host (`politeness.toml`). Detecta PDFs e links cujo slug contenha `edital`/`chamada` (classificação **sintática**) e usa Playwright apenas como gatilho em páginas dinâmicas. O resultado são **candidatos** deduplicados por URL normalizada.

*[Cientista social]* Este é o estágio que **define o que entra no corpus**. As decisões aqui — quais portais, quais categorias (pró-reitoria de pesquisa, extensão, NIT, agência de inovação), quais páginas de listagem — moldam a cobertura do corpus. É fundamental documentá-las, porque o corpus reflete **apenas os documentos acessíveis e publicados** nas fontes escolhidas: uma conclusão sobre "lacunas" institucionais precisa ser lida como *déficit de discurso documentado*, nunca como inexistência de prática.

### 2.2 Coleta de documentos (CAP-4)

*[Cientista de dados]* Cada PDF baixado recebe um **id de 12 hex do SHA-256** dos bytes (dedupe por hash completo, não por prefixo). O arquivo é gravado em `corpus/{INSTITUICAO}/{ano|_sem_ano}/<hash12>-<slug>.pdf`. Dedupe intramural por hash; cruzar portais é decisão humana, nunca automática. Downloads são resumíveis, com cap de tamanho e suspensão de host em falhas repetidas.

*[Cientista social]* Este estágio garante a **integridade e a reprodutibilidade**: cada documento tem uma impressão digital única (hash), de modo que é possível auditar que um dado texto é exatamente o PDF publicado. Isso sustenta a *cadeia de custódia* que depois permite citar trechos literais com endereço verificável.

### 2.3 Textuação / extração (CAP-6)

*[Cientista de dados]* O `textuar` é o **único estágio autorizado a ler PDF** (extrator único, AD-11). Extrai texto por página (pypdf, tolerante a falha por página); se todas falharem, marca `flag_escaneado=1`. O texto vira um `.txt` irmão do PDF, UTF-8. Limiar de caracteres por página define escaneado.

**Resgate por OCR (`ocrescer`, Fase 3.1).** Opcional e **não automático**: nenhum estágio dispara OCR sozinho — o pesquisador chama `agente-editais ocrescer` (`--portal` ou `--todos`), que renderiza cada página do PDF escaneado (pypdfium2) e a submete ao Tesseract. Páginas abaixo do limiar de confiança (`ocr_confianca_minima`, padrão 60/100) são descartadas; se nenhuma página passar, o documento **permanece escaneado** (o texto óptico ruim nunca vira "texto"). Se o resgate valida: o `.txt` irmão é sobrescrito com o texto óptico, a `flag_escaneado` é zerada e o documento é carimbado (`ocr_em`, `ocr_metodo`, `ocr_confianca_media`, `ocr_paginas_resgatadas`) — gravadas como colunas na **migração de schema v11**, com índice parcial. Retomada idempotente: documento já resgatado (`ocr_em`) é pulado no próximo ciclo, e PDFs que o pypdf já extraiu (não-escaneados) nunca voltam ao OCR. Requer o extra opcional `uv sync --extra ocr` + binário Tesseract com o pack de idioma (guard recusa com exit 2 sem eles).

*[Cientista social]* A extração converte documentos "fechados" (PDF) em **texto indexável e pesquisável**. Documentos escaneados (imagem) não têm texto automático no extrator — são sinalizados e ficam **fora das análises textuais** até um resgate por OCR. O resgate é uma **decisão de pesquisa**, não automática: recupera o texto de PDFs-cuja-página-é-imagem (históricos, escaneados), mas com **controle de qualidade explícito** — só entra no corpus o texto cuja confiança óptica atingiu o limiar. Portanto, nas métricas textuais, **documentos escaneados aparecem como lacuna de presença**, não como ausência real de conteúdo; a aba Cobertura (3.8) distingue escaneados de resgatados por OCR para que essa lacuna seja mensurável.

### 2.4 Datação multi-fonte (CAP-3)

*[Cientista de dados]* O `datar` decide o ano de publicação consultando **todas as fontes locais** em cascata: padrão na URL → texto da âncora do link → metadados do PDF. Regras de aceite: um único ano é aceito se corroborado por ≥2 fontes ou oriundo de fonte ≠ URL; divergências e anos sem corroboração vão para uma **fila de revisão humana** (com justificativa e autoria obrigatórias). Nada é descartado sem enfileirar.

*[Cientista social]* A **datação é um ponto de vulnerabilidade metodológica** em corpora históricos: a data aparente na URL pode diferir da publicação efetiva, e PDFs antigos têm metadados ruidosos. A solução — usar múltiplas fontes e exigir corroboração, com fila humana — é um controle de qualidade que impede que a série temporal por ano seja corrompida por datas "adivinhadas". A coluna `ano_fonte` registra a **origem da data** (`automatica`/`fila_humana`/`vazio`), tornando transparente a confiabilidade de cada rótulo temporal.

### 2.5 Classificação L1 — tipo de edital (inovação vs. não-inovação)

*[Cientista de dados]* O `classificar` decide `tipo_edital ∈ {inovacao, nao_inovacao, sem_texto}` casando **sinais textuais** (`configs/sinais_inovacao.yaml`) por **token inteiro, insensível a caixa e acento**, com normalização (lowercase + remoção NFD de acentos + collapse de whitespace). O veredito é **ADVISORY**: nunca exclui/oculta documento. Se há texto, o sinal pode estar no texto OU na âncora (registra `metodo`); sem texto, só âncora; nunca se rotula `nao_inovacao` sem ler texto.

*[Cientista social]* Esta camada **separa do corpus** o que é efetivamente um instrumento de fomento à inovação daquilo que é normativo genérico. O dicionário de sinais (termos como "inovação", "transferência de tecnologia", "incubadora", "tecnologia social", "parque tecnológico", etc.) é tratado como **heurística e não como medida definitiva**: a presença de um termo é *indicador de presença discursiva*, não prova de orientação substantiva. Por isso o sistema registra **qual** sinal disparou e **se** veio do texto ou da âncora, permitindo auditar cada rótulo.

### 2.6 Análise L2 — catálogo analítico com instrumento congelado (CAP-7)

*[Cientista de dados]* O `analise` codifica **por Edital** (unidade de processamento) um catálogo analítico estruturado, usando um LLM isolado atrás de um adaptador único (`llm_adapter`), via transporte OpenAI-compatível por `requests`. Antes de rodar, exige **três phase-gates bloqueantes**: (1) codebook congelado (data + autoria), (2) tríade de Dahlin resolvida, (3) limiar de Acordo Humano-Máquina (κ) fixado. O instrumento (codebook) é **congelado por lote** via hash SHA-256: mudar o codebook abre um novo lote. No caminho de gravação, todo valor ≠ N/A exige **citação literal** que é verificada contra o `.txt` do documento citado; trecho inexistente ⇒ `citacao_invalidada` (fora do catálogo válido). Saída fora do esquema é reprocessada e **jamais gravada**.

*[Cientista social]* Este é o estágio de maior poder analítico e maior risco. Ele transforma a leitura interpretativa em **campos codificados comparáveis** (cada dimensão do Quadro conceitual vira uma variável com escala e regra de decisão). Três garantias protegem a validade:

- **Instrumento congelado:** não se muda a régua de medição no meio do caminho — o mesmo codebook hashado define exatamente o que cada lote mede;
- **Verificação citação↔texto (anti-alucinação):** o modelo **não pode** inventar uma evidência; se o trecho citado não existe literalmente no documento, o campo é invalidado. Isso endereça diretamente o risco de "alucinação" e de complacência do LLM;
- **Regra de consenso a priori (Dahlin/κ):** o limiar de concordância humano-máquina é fixado antes da execução, evitando *p-hacking* ou ajuste *ad hoc* dos critérios.

A saída é um **catálogo por edital** com evidência textual endereçada — pronto para comparação entre instituições, anos e categorias.

### 2.7 Classificação do Eixo 3 — Relevância e Impacto Social

*[Cientista de dados]* Novo módulo (`configs/eixo3.yaml` + `eixo3.py`) que classifica cada documento em **três códigos**, com precedência fixa e **anti-alucinação por citação literal**:

- **IMPACTO_SUBSTANTIVO** — verificado **primeiro** (prioridade analítica): o edital reserva cotas/pontuação/bolsas para *tecnologias sociais*, *comunidades vulneráveis* ou *economia solidária*. Realizado por **gatilho + alvo** (ex.: "cota"/"reserva de vaga"/"bolsa específica" **e** o destinatário vulnerável), ambos por token inteiro.
- **IMPACTO_INSTRUMENTAL** — critérios de avaliação pontuam **exclusivamente** "potencial de mercado" e "viabilidade financeira": exige **TODOS** os sinais obrigatórios E **NENHUM** dos sinais vedados (substantivos), garantindo exclusividade.
- **AUSENTE_SILENCIAMENTO** — nenhum dos padrões acima (padrão).

Cada desfecho carrega `trecho_comprobatorio` (janela de caracteres ao redor do match), `regra_disparada` e `sinais_encontrados`. Precedência: SUBSTANTIVO → INSTRUMENTAL → SILÊNCIO.

*[Cientista social]* Este módulo operacionaliza a **Dimensão 3 do Quadro 1.6** (Relevância e Impacto Social). A distinção entre impacto **instrumental** e **substantivo** decorre diretamente da literatura: um edital que pontua apenas "potencial de mercado" e "viabilidade financeira" subordina a inovação a uma racionalidade técnico-mercadológica; um que **reserva espaços** (cotas, pontuação adicional, critério de desempate) para tecnologias sociais, comunidades vulneráveis ou economia solidária adota uma orientação **substantiva e emancipatória**. A ausência de ambos (silêncio) é, ela mesma, um **achado**: a não-menção indica possível *pensamento imitativo* ou subordinação à racionalidade instrumental — mas deve ser sempre confirmada por leitura manual (KWIC) antes de virar inferência.

Duas salvaguardas são essenciais: (1) o **trecho probatório literal obrigatório** impede que o sistema classifique sem evidência textual endereçada; (2) a **precedência** garante que um edital substantivo não seja, por acidente, rotulado como instrumental apenas por também citar o mercado.

---

## 3. O Dashboard Analítico — guia completo das abas

O dashboard é acessível com:

```bash
uv run streamlit run dashboard.py
```

Ele abre com uma **barra lateral de filtros** global e **sete abas**. Os filtros da barra lateral determinam o subconjunto do corpus analisado em **todas** as abas.

### 3.1 Barra lateral — filtros globais

*[Cientista de dados]* Filtros combinam por **E (AND)** sobre o DataFrame carregado do Manifesto:

- **Instituição** (multiselect) — siglas dos IFS no Mapa-Mestre;
- **Categoria** — tipo de portal (`integra`, `prppg_inovacao`, `extensao`, `ensino`, `reitoria`, `nit`, `agencia_inovacao`);
- **Tipo de edital** — resultado da classificação L1 (`inovacao`/`nao_inovacao`/`não classificado`);
- **Eixo 3 (Impacto)** — novo filtro por `eixo3_classificacao`;
- **Ano** (slider range) — sobre `ano = COALESCE(ano_aceito, decidido_ano)`;
- **Limite de docs para vetorização** — usado no mapa vetorial.

Também há o expander **"Ficha técnica — Análise textual"**, que mostra a versão do dicionário de stopwords, do conectivos multiword e o *token pattern* — essencial para **reprodutibilidade** (saber qual dicionário foi usado em cada análise).

*[Cientista social]* Toda análise no dashboard é **sobre um recorte escolhido**, e o recorte define o que os números significam. A coluna `ano_fonte` distingue datas automáticas (corroboradas) de datas atribuídas por revisão humana ou ainda pendentes (`vazio`) — os documentos com ano vazio permanecem visíveis, sem serem silenciosamente descartados. Este é um ponto metodológico importante: **subamostras pequenas** (ex.: um único IF em um ano) precisam ter suas generalizações restringidas ao recorte efetivamente analisado.

As métricas gerais no topo (Documentos, Instituições, Anos, Total de caracteres, Escaneados, Excluídos, Editais com várias versões, Dimensão principal) dão o **retrato de cobertura e qualidade do corpus** antes mesmo de qualquer análise temática.

---

### 3.2 Aba 1 — Distribuição

*[Cientista de dados]* Três visualizações:

1. **Histograma "Documentos por ano"** (empilhado por instituição);
2. **Pizza "Por categoria"** — distribuição por tipo de portal;
3. **Pizza "Origem da data"** — proporção de `ano_fonte` (`automatica`/`fila_humana`/`vazio`);
4. **Heatmap Instituição × Ano** — matriz de contagem de documentos.

*[Cientista social]* Esta aba responde a perguntas **de cobertura e fiabilidade da série temporal**:

- *A produção documental cresceu ou declinou por ano?*
- *Qual IF e qual categoria (pesquisa, extensão, NIT) respondem pela maior parte do corpus?*
- *Quanto do corpus ainda depende de datação humana ou está sem data?* — este é um controle de qualidade: se muitos documentos têm `vazio`, a série por ano é incompleta e conclusões temporais devem ser cautelosas.
- O **heatmap Instituição × Ano** revela padrões de regularidade/irregularidade institucional na publicação de editais — útil para identificar institutos "mais ativos" vs. "lacunares" no recorte.

---

### 3.3 Aba 2 — Mapa vetorial (TF-IDF + UMAP)

*[Cientista de dados]* Pipeline:

1. Amostra aleatória (até `limite`, `random_state=42`);
2. **TF-IDF**: `TfidfVectorizer(max_features=1500, ngram_range=(1,1), min_df=2, max_df=0.9)`, com stopwords PT;
3. **UMAP** (`n_neighbors=min(10, n-1)`, `n_components=2`, `metric="cosine"`, `random_state=42`) projeta os documentos em 2D;
4. Scatterplot colorido por `instituicao`/`ano`/`categoria`/`ano_fonte`;
5. Barra dos **top termos** por frequência.

*[Cientista social]* Este é o **mapa de proximidade semântica** do corpus: documentos que usam vocabulário parecido ficam próximos uns dos outros. Agrupamentos ("clusters") sugerem famílias de discurso — por exemplo, um bloco denso de editais de incubação tecnológica (vocabulário empresa/patente) e outro de editais de extensão social (vocabulário comunidade/território). Colorir por instituição ou ano revela **como o discurso se organiza institucional e temporalmente**.

**Cautela metodológica:** o UMAP otimiza para preservar vizinhança local e é sensível a parâmetros e à semente. A posição absoluta dos pontos não tem significado; apenas as **proximidades relativas** são interpretáveis. O mapa é uma ferramenta **exploratória de descoberta** de hipóteses, não prova de categorização — deve ser sempre validado pela leitura em contexto (aba 5).

---

### 3.4 Aba 3 — Nuvem de palavras

*[Cientista de dados]* `CountVectorizer` (unigramas ou bigramas) com stopwords PT e limpeza multiword, ordenado por frequência; `WordCloud.generate_from_frequencies` renderiza o top-N. Parâmetros: unidade, máximo de palavras, frequência mínima, colormap, instituição (Todas ou específica).

*[Cientista social]* A nuvem é uma **primeira impressão visual do vocabulário dominante**. O tamanho da palavra = frequência. É o mais acessível dos recursos, mas também o mais vulnerável a viés: palavras curtas e polissêmicas podem dominar sem relevância analítica, e a ausência de palavras não é visível. Serve para **gerar hipóteses** (que terminologia um IF privilegia?) e para comunicar resultados a não especialistas, nunca como evidência isolada.

---

### 3.5 Aba 4 — Explorar documentos

*[Cientista de dados]* Tabela dos documentos do recorte com metadados (instituição, portal, ano, `ano_fonte`, tipo de edital, caracteres, escaneado), buscável por termo no texto (insensível a caixa), com visualização do trecho inicial de cada documento em expanders.

*[Cientista social]* Esta é a **porta de entrada para a leitura integral assistida**: permite navegar do nível agregado ao documento original, essencial para a fase de **verificação manual (human-in-the-loop)**. É aqui que o pesquisador confirma se um sinal encontrado por máquina tem, de fato, o sentido esperado no contexto. A busca por termo responde a perguntas do tipo *"quais editais mencionam 'economia solidária'?"* — antes de qualquer interpretação.

---

### 3.6 Aba 5 — Análise Textual

Esta é a aba mais densa metodologicamente, com **cinco sub-abas**.

#### Sub-aba 5.1: Concordância (KWIC)

*[Cientista de dados]* **KWIC — Key Word In Context**: localiza cada ocorrência exata de um termo (busca por substring, caixa-insensível) e extrai uma janela de contexto (`20–200` caracteres), marcando o termo em **negrito**. Retorna doc_id, instituição, ano, portal, categoria, posição e trecho. Permite ajustar contexto e limite de resultados.

*[Cientista social]* O KWIC é a **técnica central de análise de concordância** (disciplinas de linguística de corpus e metodologia qualitativa assistida). Ele responde à pergunta *"em que contextos este termo aparece?"* — revelando **usos e sentidos** da palavra (ex.: "inovação" aparece sempre junto de "mercado"? de "comunidade"? de "patente"?). É o instrumento de **verificação anti-viés**: antes de afirmar que um IF prioriza inovação social, deve-se ler cada ocorrência de "social" em contexto para checar se o sentido é o esperado. O negrito destaca o nó lexical dentro do ambiente textual, preservando o contexto discursivo.

#### Sub-aba 5.2: Frequência de Termos

*[Cientista de dados]* Três recursos em um:

1. **Busca específica** de um termo (unigrama/bigrama/trigrama) contra a contagem normalizada — retorna a frequência exata no recorte, com aviso se abaixo do mínimo ou se stopword;
2. **Top-N** de unigramas/bigramas/trigramas mais frequentes (gráfico de barras);
3. **Evolução temporal** (heatmap termo × ano) e **Comparação entre instituições** (barras agrupadas de top termos por IF, máx. 4).

*[Cientista social]* A frequência de termos **quantifica a saliência discursiva**: quanto mais um termo aparece, mais central é o tema no código institucional do recorte. A **evolução temporal** (termo × ano) revela mudanças de ênfase ao longo do tempo (*"a frequência de 'tecnologia social' cresceu entre 2019 e 2026?"*). A **comparação entre instituições** permite benchmarking discursivo: quais IFS verbalizam mais determinadas racionalidades. A **busca específica** valida uma hipótese concreta com um número verificável.

**Cautela:** frequência conta **presença lexical**, não peso analítico — uma menção em rodapé pesa tanto quanto uma seção inteira. Por isso, todo número aqui é **indicador exploratório** a ser checado via KWIC.

#### Sub-aba 5.3: Co-ocorrências

*[Cientista de dados]* Matriz de co-ocorrência por **janela deslizante de palavras** (`3–30`): conta quantas vezes dois termos do top-N aparecem juntos dentro da janela. Heatmap (colormap Reds) e tabela dos pares mais frequentes. Parâmetros: top-N, tamanho da janela, frequência mínima do par.

*[Cientista social]* A co-ocorrência revela **associações contextuais estáveis**: quais termos "andam juntos" no discurso. Por exemplo, se "tecnologia" co-ocorre sistematicamente com "mercado" e raramente com "comunidade", isso indica um enquadramento dominante. A matriz é a base empírica para **analisar campos semânticos** e detectar **enquadramentos (framings)** da inovação — quem é agrupado junto no texto revela o modelo mental institucional.

#### Sub-aba 5.4: Mineração Quadro 1.6

*[Cientista de dados]* Implementa `_minerar_sinais_quadro16`: para cada uma das **5 dimensões** de `sinais_analiticos.yaml`, e por documento, casa os sinais por **token inteiro normalizado** (case-insensitive, acento-removido, whitespace colapsado, bigramas opcionais). Produz `sinais_encontrados`/`total_sinais` por dimensão-documento. Visualizações:
- **Métricas** — % de documentos com ≥1 sinal por dimensão;
- **Evolução temporal** — gráfico de linha da presença dos sinais por dimensão ao longo dos anos, **independente do filtro global** (`janela_anos` própria);
- **Heatmap Dimensão × Instituição** (% de presença);
- **Heatmap Dimensão × Ano** (% evolução temporal);
- **Drill-down** — seleciona dimensão e sinal (colunas independentes), com **instituição** e **janela de anos próprias** (4 seletores), mais barra empilhada de ocorrências por instituição e **KWIC de cada sinal** da dimensão (concordância em contexto, até 10 ocorrências por sinal).

*[Cientista social]* Este é o coração do **Quadro 1.6** — a operacionalização das cinco dimensões analíticas do referencial teórico em sinais textuais observáveis:

| Dimensão | Pergunta sociológica central |
|---|---|
| **1. Fundamentos Epistemológicos** | A política reconhece saberes além do científico (tradicional, indígena, comunitário) ou hierarquiza o conhecimento? |
| **2. Atores, Redes e Relações de Poder** | Quais atores o instrumento nomeia? O edital centraliza poder ou distribui agência? |
| **3. Relevância e Impacto Social** | Prioriza problemas locais ou só métricas econômico-tecnológicas? |
| **4. Desenho Institucional e Governança** | As normas são código rígido ou abrem margem de manobra? Decisão é transparente/participativa? |
| **5. Contextualização e Desenvolvimento Regional** | A política responde às especificidades regionais ou reproduz dependências? |

O **drill-down com KWIC** é decisivo para a validade: ele permite descer de uma taxa agregada ("40% dos editais mencionam impacto social") às **evidências textuais literais** que a sustentam. É a ponte entre o quantitativo e o qualitativo, e o principal antídoto contra a **reificação** de métricas de contagem.

#### Sub-aba 5.5: Classificação Eixo 3

*[Cientista de dados]* Exibe a distribuição dos três códigos (`eixo3_classificacao`) — com um **slider de janela temporal independente** do filtro global (`eixo3_janela`), de modo que a evolução do impacto social pode ser lida sobre um recorte temporal próprio:

- **Métricas** — Instrumental / Substantivo / Silêncio / Total;
- **Pizza** de distribuição (cores: vermelho=instrumental, verde=substantivo, cinza=silêncio);
- **Heatmap Eixo 3 × Instituição** (% por código);
- **Heatmap Eixo 3 × Ano** (evolução %);
- **Tabela detalhada por documento** — doc_id, instituição, ano, portal, código, **trecho probatório literal**, com coloração por código;
- **Botão Exportar CSV**.

*[Cientista social]* Esta aba torna explícito o **balanço entre racionalidade instrumental e substantiva** do corpus e como ele varia por instituição e ao longo do tempo. A tabela com **trecho probatório literal** é o que permite **auditar** cada classificação: o pesquisador lê o trecho exato que a máquina encontrou e julga se a classificação procede. O **export CSV** permite levar os resultados para fora do dashboard para análise estatística ou arquivamento. A cor vermelha/verde/cinza comunica visualmente a tensão paradigmática instrumental vs. substantiva vs. silenciosa.

---

### 3.7 Aba 6 — Varredura Editais

*[Cientista de dados]* Painel de operação da varredura sob demanda (página institucional que lista editais). O dashboard é somente-leitura e **nunca acessa a rede** (AD-5) — toda escrita e toda navegação são delegadas à CLI em **subprocessos** (`_roda_cli`), respeitando a janela off-peak do host. Recursos:

1. **Registro de URLs** — caixa de texto (uma URL por linha) com os botões "Registrar" e "Registrar e rodar agora", e o checkbox "Ignorar a janela off-peak" (`--fora-da-janela`), que força a execução mesmo quando a CLI recusaria o acesso à rede;
2. **Status das varreduras** — tabela com ID, URL, instituição, portal, status (`pendente`/`rodando`/`concluida`/`falhou`) e datas;
3. **Candidatos no pipeline da varredura (v10)** — por varredura concluída, métricas e uma tabela de candidatos com o **status por estágio do pipeline** (Baixado, Textuado, Datado, Classificado, Eixo 3 — ✔/–). Os candidatos são lidos **direto do banco** via `varredura_id` (proveniência, AD-3), sem tocar rede;
4. **Log da varredura** — expander com o resumo/erro registrado na linha da varredura (normalizado para leitura);
5. **"Seguir para o pipeline"** — botão que roda **síncrono** os cinco estágios (coletar → textuar → datar → classificar → classificar-eixo3) **apenas sobre os candidatos da varredura escolhida** (via `--varredura N`), com barra de progresso por estágio e parada no primeiro que falhar.

*[Cientista social]* Este é o "painel de operação" da coleta: permite **ampliar o corpus** registrando novas fontes institucionais e acompanhar o estado do processo. Ao registrar mais URLs de um IF, decide-se na prática **a cobertura do corpus** — fundamental para documentar a construção da base. Mais que isso, a varredura oferece um **recorte propositivo de análise**: com o `--varredura N` (CLI), o pesquisador pode confinar **todo o pipeline** — da coleta ao Eixo 3 — ao conjunto de candidatos de uma única varredura, sem tocar o restante do corpus. Isso é útil para amostras intencionais (ex.: analisar integralmente os editais de uma chamada específica antes de generalizar) e para **auditar a cadeia de custódia**: cada candidato responde por seu `varredura_id`, de modo que se sabe exatamente de qual página cada documento veio. O log e o painel por candidato tornam o avanço do pipeline transparente estágio a estágio.

---

### 3.8 Aba 7 — Cobertura do Corpus

*[Cientista de dados]* Leitura **direta do Manifesto** (SQL), sem passar pelo DataFrame filtrado — percentuais sobre o corpus **total**, não sobre o recorte da sidebar. Linhas agregadas por (instituição, ano):
- **KPIs** — total de documentos, textuados, com texto útil, datados, classificados (L1), **escaneados** e **resgatados por OCR**;
- **Funil (funnel)** — documentos → textuados → com texto útil → datados → L1 → Eixo 3 → excluídos, com Tabela de progressão (colunas com barras de progresso);
- **Heatmap Instituição × Ano** (contagem de documentos) e **barras de avanço do pipeline por instituição** (% texto útil, % datados, % L1);
- **Tabela detalhada** por instituição × ano, incluindo **Resgatados OCR** (coluna `ocr_em` da v11).

*[Cientista social]* Esta é a aba da **autoconsciência de cobertura**: mostra quantos documentos o corpus tem, quanto efetivamente virou texto utilizável, quanto foi datado e classificado, e **quantos PDFs escaneados ainda esperam resgate por OCR**. Sem ela, o pesquisador corre o risco de interpretar "lacunas" na análise textual quando a lacuna é, na verdade, de **processamento** (documento escaneado ainda não resgatado). Distinguir "não está no corpus" de "está no corpus mas sem texto útil" é pré-condição para qualquer generalização válida — e a aba torna essas duas situações visíveis e mensuráveis. O **funil** resume em um só olhar o desperdício acumulado entre cada estágio do pipeline, apontando onde investir esforço (ex.: um gargalo em "texto útil" sugere rodar `ocrescer`).

---

## 4. Síntese metodológica: como os recursos se complementam

*[Cientista social]* Para produzir uma inferência válida, os recursos **não devem ser usados isoladamente** (triangulação metodológica):

1. **Gerar hipóteses** com a **Nuvem** (aba 3) e o **Mapa vetorial** (aba 2);
2. **Quantificar** a presença de temas com **Frequências, Co-ocorrências e Mineração Quadro 1.6** (abas 5.2–5.4);
3. **Qualificar e verificar** com **KWIC** (aba 5.1) e **drill-down Quadro 1.6** (aba 5.4) — lendo cada evidência em contexto;
4. **Categorizar normativamente** com a **Classificação Eixo 3** (aba 5.5), auditando pelo trecho probatório;
5. **Recontextualizar** com a **Distribuição** (aba 1) e a **Varredura** (aba 6), entendendo a cobertura e qualidade do corpus.

> **Princípio transversal:** *lacuna* e *tema dominante* só se tornam **achados** após verificação manual. Todo quantitativo é um **indicador de presença discursiva** no recorte, com parâmetros e filtros explicitados.

### 4.1 Indicadores computacionais e seus significados (tabela-resumo)

| Recurso / aba | Indicador computacional | O que significa para a pesquisa |
|---|---|---|
| Sidebar / métricas | `ano_fonte` (auto/humana/vazio) | Confiabilidade da série temporal |
| Distribuição | Contagem por ano/categoria/instituição | Cobertura e regularidade do corpus |
| Distribuição | Heatmap Instituição × Ano | Padrões de atividade institucional |
| Mapa vetorial | Distância UMAP (TF-IDF/cosine) | Proximidade lexical/estrutural entre documentos |
| Nuvem | Frequência de top termos | Vocabulário dominante (geração de hipóteses) |
| KWIC | Ocorrências + contexto | Sentido e uso do termo no discurso (verificação) |
| Frequência | Top-N / evolução termo×ano / comparação IF | Saliência discursiva e mudança temporal |
| Co-ocorrência | Pares coocorrentes | Enquadramentos e campos semânticos |
| Quadro 1.6 | % de presença por dimensão × IF/ano + KWIC | Operacionalização das 5 dimensões analíticas |
| Eixo 3 | Código + trecho probatório | Balanço instrumental vs. substantivo vs. silêncio |
| Varredura (aba 6) | `varredura_id` + status por estágio por candidato | Proveniência e progresso do pipeline para um recorte sob demanda |
| Cobertura (aba 7) | Funil por estágio + escaneados vs. resgatados OCR | Autoconsciência de cobertura: lacuna de processamento ≠ lacuna de conteúdo |

---

## 5. Limitações e controles de viés

*[Cientista de dados]* O projeto documenta formalmente as famílias de risco (Quadro 1.7 do referencial) e as mitiga em código:

| Família de viés | Mitigação implementada |
|---|---|
| **Construto do dicionário** | Token-inteiro + normalização; dicionário tratado como heurística; KWIC para validar |
| **Circularidade estudo-instrumento** | Instrumento congelado por hash por lote; κ a priori; verificação manual (KWIC) |
| **Corpus/disponibilidade** | `ano_fonte` transparente; escaneados sinalizados e **resgatados por OCR documentados** (confiança + páginas); cobertura mensurável na aba 7 |
| **Métricas por contagem** | Todo número rotulado indicador exploratório; drill-down KWIC obrigatório |
| **Geração com IA (RAG/L2)** | Verificação citação↔texto obrigatória; saída fora do esquema nunca gravada; anti-alucinação |
| **Interação/agregação** | Filtros com E explícito; subamostras sinalizadas; recorte sempre documentado |

*[Cientista social]* Em síntese: use o sistema como um **auxiliar rigoroso de leitura assistida**, nunca como oráculo. As garantias de rastreabilidade (hash por documento, versão de dicionários, instrumento congelado, trecho probatório literal, origem da data) foram desenhadas para que **cada número possa ser audado de volta ao documento e ao contexto**, preservando a soberania interpretativa do pesquisador. As conclusões sobre "lacunas" ou "temas dominantes" devem sempre ser confirmadas por leitura manual e consideradas como *déficit de discurso documentado* — não como medida de prática efetiva ou de implementação.

---

## 6. Reproductibilidade e boas práticas de uso

```bash
# Pipeline completo (uma vez por ambiente)
uv sync [--extra ocr]                 # --extra ocr: habilita o resgate de escaneados
uv run agente-editais mapa validar      # valida Mapa-Mestre e sincroniza Manifesto
uv run agente-editais descobrir --todos  # crawling
uv run agente-editais coletar --todos   # baixa PDFs
uv run agente-editais textuar --todos   # extrai texto
uv run agente-editais datar --todos     # datação multi-fonte
uv run agente-editais classificar --todos   # classificação L1 (inovação)
uv run agente-editais classificar-eixo3 --todos  # Eixo 3 (impacto social)
uv run streamlit run dashboard.py       # abre o dashboard

# Resgate optativo de PDFs escaneados (Fase 3.1 — só sob demanda)
uv run agente-editais ocrescer --portal IFBA     # escaneados de uma instituição
uv run agente-editais ocrescer --todos           # escaneados de todos os portais
uv run agente-editais ocrescer --portal IFBA --confianca-minima 70 --idioma por

# Varredura sob demanda (aba do dashboard)
uv run agente-editais varredura adicionar "https://portal.ifex.edu.br/editais/chamadas/abertas/"
uv run agente-editais varredura rodar        # respeita a janela off-peak
uv run agente-editais varredura rodar --fora-da-janela  # força a execução

# Recorte de análise: um estágio SÓ sobre os candidatos da varredura N
uv run agente-editais coletar --varredura N
uv run agente-editais textuar --varredura N
uv run agente-editais datar --varredura N
uv run agente-editais classificar --varredura N
uv run agente-editais classificar-eixo3 --varredura N

# Testes
uv run python -m pytest -q
```

**Recomendações:**

1. **Registre os filtros e parâmetros** usados em cada análise (instituição, ano, categoria, tipo, eixo 3, limite de vetorização) — eles fazem parte do resultado.
2. **Fixe a versão dos dicionários** (stopwords, conectivos, sinais, codebook) — a ficha técnica da sidebar e o hash do codebook existem para isso.
3. **Valide por amostra** com KWIC: escolha uma amostra aleatória de classificações/sinais e leia o contexto de cada uma.
4. **Busque contra-evidências** (análise adversativa): procure ativamente sinais que deveriam estar ausentes.
5. **Codifique em duplicata** (dois pesquisadores independentes) e meça o acordo (Kappa de Cohen) na matriz de extração.

---

*Documento gerado como nota de divulgação científica do instrumento de análise documental de editais da Rede Federal EPCT. Referência metodológica: "Diretrizes Analíticas — Sociologia da Inovação e Confluências" (Capítulo 1, Quadros 1.5–1.7).*
