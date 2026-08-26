---
title: "PRD — Agente de Editais de Inovação da Rede Federal EPCT"
status: final
created: 2026-08-21
updated: 2026-08-21
---

# PRD: Agente de Editais de Inovação da Rede Federal EPCT

## 0. Propósito do Documento

Este PRD define os requisitos do Agente de Editais de Inovação da Rede Federal EPCT — instrumento computacional de uma pesquisa de doutorado (PPGCS/UFBA) em sociologia da inovação. Destina-se ao(a) pesquisador(a) responsável e aos fluxos downstream do BMad Method (arquitetura, epics/stories). Constrói sobre três insumos que NÃO duplica: `brief.md` (visão e escopo), `brainstorm-intent.md` (configuração V1 "Salvaguarda" e restrições C1–C5) e o digest do Quadro Conceitual-Analítico (`framework-fonte.txt`). Vocabulário ancorado no Glossário (§3); funcionalidades agrupadas com FRs numerados globalmente; suposições marcadas inline e indexadas no §12. Detalhes técnicos de implementação vivem no `addendum.md`.

## 1. Visão

O agente explora, baixa e cataloga editais de fomento, bolsas e incubação publicados nos portais institucionais da Rede Federal de Educação Profissional, Científica e Tecnológica (2019–2026), e analisa-os sob um framework de sociologia da inovação com recorte decolonial, praticado como **Sociologia Pública** (entrelace crítico Burawoy × Guerreiro Ramos). Seu produto não é a lista de documentos: é um **corpus íntegro, rastreável e codificável** que permite perguntar *como os editais constroem modelos de governança pública e mobilizam conceitos sociais*.

Como não existe repositório central desses editais e o "link rot" os apaga continuamente, o agente **torna-se ele próprio o repositório** — salvaguarda documental com cadeia de custódia (hash + origem + método de datação). A contribuição científica central é a ponte entre teoria e texto: operacionalizar o Quadro Conceitual-Analítico em um codebook aplicável ao gênero "edital", com validação humana da extração assistida por LLM.

### 1.1 Por Que Agora

- O link rot é ativo: cada edital removido sem cópia é dado perdido para sempre — a janela 2019–2026 fecha com o passar do tempo.
- A maturação pós-Lei nº 13.243/2016 torna o período analisável como trajetória de política pública.
- LLMs tornam viável pela primeira vez codificar corpus textual extenso sob lente qualitativa com validação humana por amostragem.

## 2. Usuário-Alvo

### 2.1 Jobs To Be Done

- **Funcional:** coletar editais dispersos em portais heterogêneos e catalogá-los com metadados íntegros, sem trabalho manual desigual.
- **Analítico:** codificar cada edital segundo as dimensões do Quadro Conceitual-Analítico, com evidência citável por campo.
- **Contextual:** retomar coletas interrompidas sem refazer trabalho; ter certeza de que nada foi baixado duas vezes nem perdido em silêncio.
- **Acadêmico:** produzir tabelas comparativas (instituição × ano × dimensões) sustentáveis perante banca — com cadeia de custódia publicável.
- **Emocional:** dormir tranquilo(a) sabendo que o corpus da tese sobrevive ao sumiço dos portais.

### 2.2 Não-Usuários (v1)

- Gestores de NITs e formuladores de política — consumidores potenciais do dataset futuro, não atendidos nesta fase.
- Pesquisadores de outros domínios (editais de ensino/pesquisa stricto sensu fora do recorte "inovação").

### 2.3 Key User Journeys

*Ferramenta CLI/notebooks de operador único; jornadas enxutas.*

- **UJ-1. JonataFB executa o piloto de coleta num IF e confere o catálogo.** Roda o crawl para um portal do Mapa-Mestre, acompanha o progresso retomável, e ao final abre o Manifesto conferindo que cada PDF tem Registro L1 completo (hash, URL, data, método).
- **UJ-2. JonataFB valida o Catálogo L2 contra sua própria leitura — às cegas.** Codifica a amostra estratificada SEM ver a saída do LLM, registra e trava suas decisões; só então destrava a codificação L2, compara campo a campo e produz as métricas de Acordo Humano-Máquina (e, na fração com segundo codificador, de Confiabilidade Inter-Codificador).
- **UJ-3. JonataFB gera o comparativo para o capítulo.** Consulta o catálogo por instituição × ano × Dimensão Analítica e exporta CSV/tabelas para análise qualitativa no notebook.

## 3. Glossário

- **Edital** — conjunto documental de fomento/bolsa/incubação publicado por instituição da Rede Federal EPCT: documento principal, suas **Retificações** e seus **Anexos**; objeto elementar do corpus. Um Edital pertence a uma Instituição e a um Ano e agrega um ou mais Documentos.
- **Documento** — arquivo individual pertencente a um Edital (principal, retificação ou anexo); unidade de download e de Registro L1.
- **Rede Federal EPCT** — conjunto dos institutos federais, CEFETs e equivalentes; universo documental da pesquisa.
- **Portal Institucional** — site onde uma Instituição publica Editais; classificado em categorias: **Integra**, **NIT**, **PRPGI/PRPPG**, **Agência de Inovação**.
- **Mapa-Mestre** — cadastro curado de Instituições, Portais, categorias e URLs semente mantido pelo agente.
- **Seed (URL semente)** — ponto de partida de navegação de um Portal no Mapa-Mestre.
- **Palavra-chave de Seção** — termo que identifica seções candidatas a conter Editais ("Inovação", "NIT", "PRPGI", "PRPPG", "Agência de Inovação").
- **Crawl Híbrido** — estratégia de coleta: HTTP leve (requests+BS4) por padrão; navegador headless (Playwright) apenas quando o Portal exigir renderização dinâmica.
- **Filtro Temporal em Cascata** — inferência da data de publicação na ordem: padrão de ano na URL → texto da âncora → tag `<time>`/classe CSS → metadados do PDF.
- **Método de Datação** — etapa da cascata que produziu a data aceita para o Edital; registrado como metadado.
- **Fila de Revisão Manual** — lista de Editais sem data confiável após a cascata; nunca descartados silenciosamente.
- **Estrutura Instituição/Ano** — layout de pastas dos PDFs baixados, espelhando Instituição e Ano.
- **Manifesto** — banco SQLite canônico do projeto; guarda Registros L1, Catálogo L2 e estado do crawl.
- **Registro L1** — metadados automáticos de captura de um Edital: instituição, ano, URL de origem, data de captura, Hash SHA-256, flag de PDF escaneado, Método de Datação, versão do crawler.
- **Catálogo L2** — campos analíticos por Edital, derivados do Codebook, produzidos por extração assistida por LLM com citação-evidência.
- **Codebook** — esquema de codificação operacionalizando as Dimensões Analíticas do Quadro Conceitual-Analítico para o gênero "edital".
- **Quadro Conceitual-Analítico** — framework da pesquisa: sociologia da inovação × decolonialidade; 5 princípios, 5 Dimensões Analíticas e 3 Lentes Transversais.
- **Dimensão Analítica** — eixo do Quadro: Fundamentos Epistemológicos; Atores, Redes e Relações de Poder; Relevância e Impacto Social; Desenho Institucional e Governança; Contextualização e Desenvolvimento Regional.
- **Lente Transversal** — olhar crítico aplicável a todo o corpus: solucionismo tecnológico; visão hierarquizante; visão desenvolvimentista.
- **Data de Publicação no Portal** — construto temporal alvo do projeto: quando o Edital tornou-se acessível publicamente no Portal Institucional. Distinta da data de assinatura interna do PDF e da data de postagem; cada método de datação a aproxima com fidelidade diferente, sempre registrada.
- **Acordo Humano-Máquina** — métrica de concordância entre codificação humana cega e extração LLM na amostra L3; validação de instrumento, distinta de Confiabilidade Inter-Codificador clássica (dois humanos independentes), que pode existir em fração da amostra com segundo codificador.
- **Validação Humana (L3)** — estudo de validação do Catálogo L2: codificação humana às cegas (registrada antes de qualquer exposição à saída LLM), estratificada e separada das iterações de desenvolvimento do Codebook.
- **Confiabilidade Inter-Codificador** — métrica de concordância entre dois codificadores humanos independentes; aplicável apenas à fração da amostra L3 com segundo codificador.
- **Polidez** — conjunto de regras de coleta responsável (robots.txt, delays, janela off-peak, identificação do agente, retry/backoff).

## 4. Funcionalidades

### 4.1 Mapa-Mestre de Portais

**Descrição:** Cadastro curado das Instituições piloto e seus Portais, com categoria e Seeds. Fontes iniciais: listas do brainstorming (integra.ifba/ifsp/ifms/ifrj; inova.ifsp; nit.ifsuldeminas, nit.ifc; prpgi.ifma, proppg.ifpa, portal.ifba.edu.br/prpgi; proex.ifes.edu.br/agencia-de-inovacao; ifpr.edu.br/inovacao/agif; www.ifrr.edu.br/a-instituicao/agencia-de-inovacao). Realiza UJ-1.

**Functional Requirements:**

#### FR-1: Cadastro estruturado de portais
O(a) pesquisador(a) pode manter, em arquivo versionado consumido pelo agente, o cadastro de Instituições → Portais → categoria → Seeds.

**Consequences (testable):**
- Cada entrada possui: sigla da Instituição, nome, URL(s) seed, categoria ∈ {Integra, NIT, PRPGI/PRPPG, Agência de Inovação}.
- O agente recusa executar crawl de portal ausente do Mapa-Mestre.
- Alterações no Mapa-Mestre ficam versionadas em git.

#### FR-2: Verificação pré-voo de seeds
O agente verifica cada Seed antes do crawl e reporta seeds inacessíveis sem abortar o lote.

**Consequences (testable):**
- Seed inacessível gera registro de falha no Manifesto e continuação do lote.
- Relatório pré-voo lista acessíveis/inacessíveis antes da coleta.

### 4.2 Descoberta por Palavras-Chave

**Descrição:** Navegação a partir das Seeds identificando seções candidatas via Palavras-chave de Seção, com Crawl Híbrido. Realiza UJ-1.

**Functional Requirements:**

#### FR-3: Detecção de seções candidatas
O agente identifica links de seções cujo texto âncora, título ou URL contenha Palavras-chave de Seção e os enfileira para exploração.

**Consequences (testable):**
- Correspondência insensível a caixa e acentuação.
- Profundidade máxima de navegação configurável por portal `[ASSUMPTION: default 3 níveis]`.
- Seções visitadas registradas no Manifesto para evitar revisita.

#### FR-4: Renderização híbrida
O agente usa requests+BS4 por padrão e Playwright apenas quando detectar conteúdo dinâmico.

**Consequences (testable):**
- Gatilho de troca para Playwright definido por heurística configurável `[ASSUMPTION: conteúdo-alvo ausente no HTML estático]`.
- Uso do Playwright registrado no Manifesto por portal.

#### FR-5: Extração de candidatos a edital
Dentro de seções candidatas, o agente extrai links para PDFs e páginas de edital.

**Consequences (testable):**
- Links .pdf diretos entram na fila de download; páginas de edital seguem para extração de links.
- Retificações e Anexos são candidatos legítimos, detectados por padrões no texto âncora/URL/nome `[ASSUMPTION: padrões "retificação" e "anexo"]`, e vinculados ao Edital pai.
- Candidatos deduplicados por URL normalizada dentro do mesmo portal.

### 4.3 Filtro Temporal em Cascata

**Descrição:** Determina a Data de Publicação no Portal (aproximada) de cada candidato e registra como cada fonte contribuiu. Realiza UJ-1.

**Functional Requirements:**

#### FR-6: Datação multi-fonte com evidência persistida
O agente consulta TODAS as fontes de datação (padrão de ano na URL, texto da âncora, `<time>`/classes CSS, metadados do PDF) e grava o valor bruto extraído de cada uma — não apenas o primeiro acerto.

**Consequences (testable):**
- Registro L1 carrega, por fonte consultada: método, valor bruto e localização (trecho da âncora, atributo da tag, campo do PDF).
- Divergências entre fontes são registradas como sinal de qualidade; convergência independente eleva a confiança.
- Ano inferido apenas da URL (fonte reconhecidamente frágil — upload/migração) é classificado como **baixa confiança** e encaminhado à Fila de Revisão Manual quando não corroborado.
- Nos anos-limite da janela (2019 e 2026), aceitação exige corroboração interna (data no PDF ou em outra fonte) `[ASSUMPTION: anos-limite exigem corroboração interna]`.

#### FR-7: Registro do Método de Datação
Todo Edital aceito carrega no Registro L1 o método que definiu o ano aceito.

**Consequences (testable):**
- Campo `metodo_datacao` ∈ {url, ancora, time_tag, pdf_meta, wayback_snapshot} presente em 100% dos registros aceitos, acompanhado das evidências brutas do FR-6.

#### FR-8: Tratamento de não-datáveis e decisão fundamentada
Edital sem data confiável vai para a Fila de Revisão Manual; toda decisão humana exige justificativa.

**Consequences (testable):**
- Item na fila preserva URL, portal, evidências coletadas e motivo; nada é descartado silenciosamente.
- Decisão humana grava: ano atribuído ou exclusão, justificativa textual obrigatória, evidência anexa, autoria e data.

### 4.4 Coleta e Armazenamento

**Descrição:** Download dos PDFs na Estrutura Instituição/Ano com Registro L1 imediato — baixar já é catalogar. Realiza UJ-1.

**Functional Requirements:**

#### FR-9: Download organizado
O agente baixa PDFs aceitos para `{raiz}/{Instituição}/{Ano}/` com nome de arquivo estável derivado do documento `[ASSUMPTION: hash curto + slug do título]`.

**Consequences (testable):**
- Caminho final registrado no Manifesto e reversível até a URL de origem.

#### FR-10: Registro L1 completo
Cada download grava Registro L1 no Manifesto.

**Consequences (testable):**
- Campos obrigatórios: instituição, ano, url_origem, data_captura, hash_sha256, flag_escaneado, metodo_datacao, versao_crawler.
- Cada Documento recebe Registro L1 próprio com vínculo (`edital_id`) ao Edital pai; o conjunto documental é reconstruível a partir do Manifesto.
- Hash calculado sobre o conteúdo baixado e verificado pós-gravação.

#### FR-11: Deduplicação
Downloads repetidos são reconhecidos por hash e URL normalizada.

**Consequences (testable):**
- Mesmo arquivo em dois caminhos do mesmo portal gera um único Registro L1 com referência cruzada. "Mudança real" = alteração de hash de conteúdo ou mudança de existência (página/documento removido ou novo) — mudanças cosméticas de página não re-disparam download.
- Re-crawl idempotente: zero novos downloads sem mudança real.

#### FR-12: Crawl retomável
Estado do crawl persiste no Manifesto; interrupções retomam de onde pararam.

**Consequences (testable):**
- Interromper e relançar o processo não refaz trabalho concluído.
- Filas pendentes/concluídas consultáveis via comando de status.

#### FR-13: Polidez
Toda requisição respeita regras de coleta responsável.

**Consequences (testable):**
- robots.txt consultado e honrado por host.
- Delay mínimo entre requisições ao mesmo host `[ASSUMPTION: ≥2s, configurável]`; janela off-peak definida pelo fuso do HOST `[ASSUMPTION: default 22h–6h horário local do host]`.
- User-Agent identificando o agente e finalidade acadêmica.
- Retry com backoff exponencial; bloqueio definitivo (403 persistente/WAF) suspende o host e segue o lote, registrando o evento.

### 4.5 Resgate Wayback Machine `[SHOULD]`

**Descrição:** Camada de resgate para Editais removidos dos portais (restrição C1). Realiza UJ-1 (modo complementar).

**Functional Requirements:**

#### FR-14: Busca de snapshots
Para URLs mortas conhecidas ou seções esvaziadas, o agente consulta snapshots da Wayback Machine.

**Consequences (testable):**
- PDF recuperado via Wayback recebe Registro L1 com `origem = wayback` e URL do snapshot preservada; a datação é própria `[ASSUMPTION: datação via conteúdo do snapshot]`: a Data de Publicação no Portal é buscada no conteúdo do snapshot com o mesmo aparato FR-6..FR-8 (método `wayback_snapshot`); data do snapshot ≠ data de publicação.
- Falha de resgate registrada; nunca bloqueia o fluxo principal.

### 4.6 Texto e OCR

**Descrição:** Prepara o conteúdo textual para o Catálogo L2. Realiza UJ-2.

**Functional Requirements:**

#### FR-15: Extração de texto nativo
O agente extrai texto de PDFs nativos e grava lado a lado ao PDF (`{mesmo nome}.txt`) `[ASSUMPTION: formato .txt simples, um por edital]`.

**Consequences (testable):**
- Texto vazio ou insuficiente aciona flag_escaneado=true `[ASSUMPTION: limiar <100 caracteres extraíveis por página, ou razão caracteres/página abaixo do mínimo configurável]`.

#### FR-16: OCR sob demanda `[SHOULD]`
PDFs escaneados passam por OCR quando necessário para o L2.

**Consequences (testable):**
- OCR executado por seleção explícita (lote ou item), não automaticamente em todo o corpus.
- Texto OCR gravado com marcação de proveniência no Manifesto.

### 4.7 Catálogo Analítico L2 (Codebook)

**Descrição:** Operacionaliza o Quadro Conceitual-Analítico em campos codificáveis por Edital — o coração científico do agente. Realiza UJ-2.

**Functional Requirements:**

#### FR-17: Codebook versionado e completo antes do L2
O(a) pesquisador(a) mantém o Codebook em arquivo versionado derivando campos das 5 Dimensões Analíticas e das 3 Lentes Transversais. **O Codebook deve estar completo e congelado antes da primeira execução de extração em lote (L2)** — incluindo a resolução da Tríade de Dahlin (§11.6) e a adaptação ao gênero "edital".

**Consequences (testable):**
- Cada campo especifica: dimensão de origem, escala adequada ao seu tipo `[ASSUMPTION: ordinal presente/ausente/grau 0–2 para campos graduáveis; nominal para categorias como modelo de governança]`, definição operacional, regra de decisão, categoria "não aplicável" (por tipo de edital), exemplos-âncora positivos e negativos por valor, e regra para menção boilerplate vs. presença estruturante.
- Regra de conflito intra-edital definida: evidências entre documento principal, retificações e anexos têm precedência documentada (cronologia: retificação mais recente prevalece sobre principal).
- Todo campo codificado exige citação-evidência verificável (trecho + página do PDF conforme extrator registrado).
- Versão congelada do Codebook é identificável e referenciada por cada lote L2.

#### FR-18: Extração assistida por LLM com congelamento de instrumento
O agente aplica o Codebook via LLM com saída estruturada validada contra schema, sob política de congelamento.

**Consequences (testable):**
- Saída inválida ao schema é reprocessada, jamais gravada.
- **Regra de congelamento:** um par modelo+versão+prompt+parâmetros (temperatura/seed registrados) por versão do Catálogo; qualquer troca cria nova versão — o corpus é reprocessado integralmente ou as versões ficam explicitamente delimitadas no dado.
- Cada Catálogo L2 registra modelo, versão, prompt e parâmetros usados.
- Verificação automática citação↔texto antes da gravação: citação que não existe no texto apontado invalida o campo (antídoto a alucinação e à contaminação por memória de treinamento do modelo).

#### FR-19: Validação Humana (L3) com protocolo cego e estimação em subamostra virgem
Amostra estratificada do piloto recebe validação em duas fases separadas: desenvolvimento do Codebook (iterativo) e **estimação final** (subamostra virgem, codebook já congelado).

**Consequences (testable):**
- **Cegamento protocolado:** codificação humana é registrada e travada ANTES de qualquer exposição à saída L2; a ordem é imposta pelo sistema.
- **Quadro de amostragem estratificado** documentado: instituição × ano × regime textual (nativo vs. OCR — o estrato escaneado entra obrigatoriamente na amostra assim que existir).
- Amostragem reprodutível (seed documentada) `[ASSUMPTION: n mínimo de 15 editais ou 25% do corpus piloto, o que for maior]`, com n mínimo de decisões por campo para estabilidade da métrica.
- Métricas reportadas por dimensão e global, com intervalos de confiança; a métrica humano×LLM chama-se Acordo Humano-Máquina.
- **Fração de ICR verdadeira:** ao menos uma subamostra `[ASSUMPTION: ≥20% da amostra L3]` recebe segundo codificador humano independente — dela nasce Confiabilidade Inter-Codificador clássica.
- Discordâncias das fases de desenvolvimento alimentam revisão do Codebook; a rodada de estimação usa apenas a subamostra virgem, sem retroalimentação.

### 4.8 Consulta e Análise

**Descrição:** Superfície CLI/notebooks sobre o Manifesto. Realiza UJ-3.

**Functional Requirements:**

#### FR-20: Consulta via CLI
O(a) pesquisador(a) filtra o catálogo por instituição, ano, categoria de portal, dimensão e valor de campo.

**Consequences (testable):**
- Resultado exportável para CSV.
- Toda consulta imprime contagens coerentes com o Manifesto (fonte única).

#### FR-21: Notebooks comparativos
Notebooks geram tabelas instituição × ano × Dimensão Analítica e visualizações descritivas da evolução no piloto.

**Consequences (testable):**
- Notebooks partem exclusivamente do Manifesto (sem leitura ad-hoc de PDFs).
- Toda saída carrega aviso de escopo: **leitura descritiva e ilustrativa, restrita ao piloto** — sem pretensão inferencial ou de série temporal generalizável.
- Saídas reproduzíveis de ponta a ponta com o mesmo Manifesto.

## 5. Não-Objetivos

- Não é buscador web genérico — coleta apenas Portais do Mapa-Mestre.
- Não consulta DOU nem agregadores externos (validação cruzada fica para pesquisa futura).
- Não faz snowballing nem busca `site:` em buscadores.
- Não oferece interface visual — Streamlit fica para v2.
- Não varre as ~64 instituições da rede — apenas o piloto de 3–5 IFs.
- Não realiza inscrição/submissão em editais; apenas leitura e preservação.
- Não publica o corpus aberto na v1 (dataset público é visão futura).

## 6. Escopo MVP

### 6.1 Dentro do MVP

- Mapa-Mestre curado + verificação pré-voo (FR-1, FR-2)
- Descoberta por Palavras-Chave com Crawl Híbrido (FR-3..FR-5)
- Filtro Temporal em Cascata + Método de Datação + Fila de Revisão Manual (FR-6..FR-8)
- Coleta na Estrutura Instituição/Ano com Registro L1, dedupe, retomável e Polido (FR-9..FR-13)
- Extração de texto nativo + flag escaneado (FR-15)
- Codebook + extração LLM (L2) + Validação Humana (L3) no piloto (FR-17..FR-19)
- Consulta CLI e notebooks comparativos (FR-20, FR-21)
- Piloto em 3–5 IFs diversos `[ASSUMPTION: um por categoria de portal + ao menos um portal de infraestrutura precária — ver §6.3]`

### 6.2 Fora do MVP

- Resgate Wayback Machine (FR-14) — SHOULD, entra após o primeiro ciclo de coleta limpa
- OCR (FR-16) — SHOULD, acionado conforme demanda real do piloto
- Streamlit, DOU, snowballing, busca `site:` — adiados (v2/pesquisa futura)
- Varredura completa da rede — pós-validação do piloto

### 6.3 Limitações Assumidas (declaradas, não escondidas)

- **Piloto ≠ amostra estatística:** os 3–5 IFs são piloto de engenharia com função analítica exploratória. Seleção por categoria de portal favorece portais estruturados (viés de sobrevivência) — precisamente onde o link rot menos age. O critério final de seleção (§11.3) incluirá ao menos um portal de infraestrutura precária para confrontar o caso crítico.
- **Recall não mensurável nesta fase:** sem enumeração-ouro (DOU/agregadores fora do escopo), não há denominador para precisão de cobertura — limitação a declarar abertamente na tese.
- **Validade local:** resultados de datação e de Acordo Humano-Máquina valem para o piloto, sua janela e seu Codebook; generalização exige a varredura completa.
- **Tabelas comparativas são descritivas** (FR-21) — células com n baixo são ruído formatado se lidas como inferência.

## 7. Métricas de Sucesso

**Primária**

- **SM-1**: Corpus íntegro — todos os Editais coletados no piloto têm Registro L1 com ≥95% dos campos obrigatórios preenchidos; a faixa 30–60 editais é critério de **viabilidade** do pipeline, não de sucesso (o universo real dos IFs piloto é desconhecido — ver Limitações §6.3). Valida FR-9, FR-10.

**Secundárias**

- **SM-2**: Datação resolvida E auditada — ≥90% dos Editais do corpus com ano aceito (multi-fonte ou decisão fundamentada na Fila), **mais auditoria de acurácia em subamostra** contra a data interna do PDF. Valida FR-6..FR-8.
- **SM-3**: Acordo Humano-Máquina substantivo na estimação final — **critério único, fixado a priori por escrito com o(a) orientador(a) ANTES da primeira rodada L2** `[ASSUMPTION: κ ≥0,75 sobre a subamostra virgem]`, reportado com intervalos de confiança e válido apenas para o escopo do piloto. Valida FR-17..FR-19.
- **SM-4**: Robustez operacional — crawl do piloto completa sem bloqueio definitivo; interrupções retomadas sem retrabalho. Valida FR-12, FR-13.

**Counter-metrics (não otimizar)**

- **SM-C1**: Volume bruto de PDFs baixados — precisão do recorte importa mais que quantidade; medição formal de precisão exige enumeração-ouro inexistente nesta fase, o que é limitação declarada (§6.3), não meta. Contrapesa SM-1.
- **SM-C2**: Velocidade de coleta — nunca à custa da Polidez; hosts lentos são tratados com paciência, não paralelismo agressivo. Contrapesa SM-4.

## 8. NFRs Transversais

- **Simplicidade metodológica sobre sofisticação técnica** — o método é a contribuição; o software é meio. Toda decisão de engenharia se subordina ao rigor e à rastreabilidade do método.
- **Reprodutibilidade científica** — versões de software, LLM, prompts e configurações fixadas e registradas por execução. L1 é reproduzível de ponta a ponta; **L2 é re-executável condicionado à disponibilidade do modelo** — o congelamento (FR-18) garante comparabilidade interna das versões do Catálogo, não re-execução eterna.
- **Rastreabilidade total** — todo artefato (PDF, txt, registro) reversível até sua origem (URL, captura, evidências brutas de datação, método).
- **Retomabilidade** — nenhum processo longo depende de sessão contínua; falha é evento esperado, não exceção.
- **Imutabilidade do cru** — PDFs baixados nunca alterados; correções acontecem no Manifesto, com histórico.
- **Portabilidade** — Manifesto em SQLite padrão; corpus em arquivos planos; backup = copiar pasta.

## 9. Restrições e Guardrails

### 9.1 Coleta Responsável

- robots.txt é mandatório; violação é bug crítico.
- Identificação honesta do agente no User-Agent (finalidade acadêmica).
- Limites por host (delay, janela off-peak) configurados e auditáveis no log.

### 9.2 Ética e Dados

- Escopo restrito a documentos públicos institucionais; dispensa avaliação ética formal por se tratar de documentos públicos sem dados pessoais — decisão registrada pelo(a) pesquisador(a) e passível de confirmação com as normas do PPGCS/UFBA. A coleta polida respeita robots.txt e a carga dos servidores.
- Nenhum dado pessoal além do conteúdo dos próprios editais é coletado ou armazenado.
- Corpus bruto permanece privado durante a tese; abertura é decisão futura deliberada.

## 10. Proveniência de Decisões e Cadeia de Custódia

- Todo Edital carrega: quem capturou (agente+versão), quando, de onde (URL), integridade (hash) e como foi datado (Método de Datação) — suficientes para reproduzir a captura ou justificar sua impossibilidade (Wayback).
- Todo campo L2 carrega: modelo+versão, versão do prompt, status de validação humana.
- A cadeia completa é exportável por Edital — requisito para publicação metodológica futura (dataset paper).

## 11. Perguntas Abertas

1. **LLM definitiva do L2** — conta institucional vs. API comercial; critérios: custo, reprodutibilidade e **privacidade**. Postura do brief: começar pragmático, com LLM de fronteira, e revisitar após os resultados do piloto; decisão firme antes da análise final do corpus.
2. **N exato do piloto** — 30–60 aprovado como faixa; número fechado com o(a) orientador(a).
3. **Critérios finais dos 3–5 IFs piloto** — proposta atual: um por categoria de portal; confirmar diversidade regional/porte.
4. **Editais duplicados entre portais da mesma instituição** — registro único com referências cruzadas (proposta do FR-11) ou preservar ambas as capturas?
5. **Limiar de Acordo Humano-Máquina** — critério único, **fixado a priori por escrito com o(a) orientador(a) antes da primeira rodada L2** (threshold shopping invalida a estimação). `[ASSUMPTION: κ ≥0,75 sobre a subamostra virgem]` — **phase-gate do L2**.
6. **Tríade de Dahlin** (Organizacional, Ambiental, Relativa) — o paper a declara necessária ao framework; resolver ANTES do congelamento do Codebook, incorporando-a ou justificando a exclusão na tese — **phase-gate do L2**. Owner: pesquisador(a).

> Resolvidas durante a revisão: ~~anexos de edital~~ → escopo documental confirmado como principal + retificações + anexos (ver Glossário: Edital/Documento; FR-5, FR-10).

## 12. Índice de Suposições

- §4.2/FR-3 — profundidade máx. de navegação default 3 níveis.
- §4.2/FR-4 — gatilho Playwright: conteúdo-alvo ausente no HTML estático *(decisão de design aberta, a fechar na arquitetura)*.
- §4.2/FR-5 — padrões "retificação" e "anexo" detectam Retificações e Anexos.
- §4.3/FR-6 — anos-limite (2019/2026) exigem corroboração interna.
- §4.4/FR-9 — nome de arquivo: hash curto + slug do título.
- §4.4/FR-13 — delay mínimo ≥2s/host; off-peak 22h–6h no fuso do host.
- §4.5/FR-14 — datação wayback via conteúdo do snapshot.
- §4.6/FR-15 — limiar de escaneado: <100 caracteres/página ou razão mínima configurável; texto gravado como .txt irmão do PDF.
- §4.7/FR-17 — escala ordinal presente/ausente/grau 0–2 para graduáveis; nominal para categóricos.
- §4.7/FR-19 — amostra L3: n mínimo 15 editais ou 25% do piloto; fração ICR ≥20% da amostra.
- §6.1 — IFs piloto: um por categoria de portal + ao menos um portal precário.
- §7/SM-3 — critério único a priori κ ≥0,75 sobre subamostra virgem.
