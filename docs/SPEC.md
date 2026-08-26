---
id: SPEC-agente-editais-inovacao
companions:
  - ../../planning-artifacts/prds/prd-AGENTE PARA EDITAIS DA REDE FEDERAL-2026-08-21/prd.md
  - ../../planning-artifacts/prds/prd-AGENTE PARA EDITAIS DA REDE FEDERAL-2026-08-21/addendum.md
  - ../../planning-artifacts/architecture/architecture-AGENTE PARA EDITAIS DA REDE FEDERAL-2026-08-21/ARCHITECTURE-SPINE.md
sources:
  - ../../planning-artifacts/briefs/brief-AGENTE PARA EDITAIS DA REDE FEDERAL-2026-08-21/brief.md
  - ../../_bmad-output/brainstorming/brainstorm-editais-inovacao-rede-federal-2026-08-21/brainstorm-intent.md
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# SPEC — Agente de Editais de Inovação da Rede Federal EPCT

## Why

**Dor + visão.** Não existe repositório central dos editais de fomento/bolsas/incubação publicados pelos portais institucionais da Rede Federal EPCT, e o link rot os apaga continuamente — a janela 2019–2026 fecha com o tempo. A afetada é a própria pesquisa de doutorado (PPGCS/UFBA) que precisa de um corpus íntegro e codificável para analisar **como os editais constroem modelos de governança pública e mobilizam conceitos sociais**, sob o Quadro Conceitual-Analítico (sociologia da inovação × decolonialidade). O agente, ao coletar com cadeia de custódia, **torna-se ele próprio o repositório** — e produz os dados do capítulo de desenvolvimento/resultados da tese.

## Capabilities

- **CAP-1**
  - **intent:** Manter cadastro curado e verificado (Mapa-Mestre) das instituições piloto, portais, categorias e seeds que define onde e como o agente coleta.
  - **success:** Crawl recusa portal ausente do cadastro; pré-voo lista seeds acessíveis/inacessíveis sem abortar o lote; alterações versionadas em git.
- **CAP-2**
  - **intent:** Descobrir seções e candidatos a edital navegando por palavras-chave nos portais, com renderização híbrida (HTTP leve por padrão; Playwright no gatilho definido).
  - **success:** Correspondência insensível a caixa/acento; troca para Playwright ocorre só nos gatilhos (portal marcado dinâmico OU conteúdo ausente no HTML estático) e fica registrada por portal; candidatos deduplicados por URL normalizada.
- **CAP-3**
  - **intent:** Determinar a Data de Publicação no Portal de cada candidato consultando TODAS as fontes (URL, âncora, `<time>`, metadados), persistindo evidência bruta, e rotear não-datáveis para fila humana fundamentada.
  - **success:** 100% dos aceitos carregam `metodo_datacao` + evidências brutas por fonte consultada; divergências registradas como sinal de qualidade; ano só-URL sem corroboração vai à fila; toda decisão humana tem justificativa obrigatória, autoria e data.
- **CAP-4**
  - **intent:** Coletar PDFs na estrutura Instituição/Ano com Registro L1 completo nascido no download, dedupe intra-portal e crawl retomável sob polidez.
  - **success:** Re-crawl idempotente ("mudança real" = hash ou existência); L1 ≥95% campos obrigatórios em todo corpus; robots.txt/delays ≥2s/off-peak do fuso do host aplicados no fetcher único e auditáveis em eventos; interrupção retoma sem retrabalho.
- **CAP-5** *(SHOULD)*
  - **intent:** Resgatar via Wayback Machine editais removidos dos portais, com datação própria pelo conteúdo do snapshot.
  - **success:** PDF resgatado recebe L1 com `origem=wayback`, snapshot preservada e método `wayback_snapshot`; falha de resgate registrada sem bloquear o fluxo principal.
- **CAP-6**
  - **intent:** Extrair texto de cada Documento com um extrator único, sinalizar PDFs escaneados e aplicar OCR sob demanda.
  - **success:** Um .txt irmão por Documento; flag escaneado acionada pelo limiar configurável (<100 caracteres/página); OCR executado apenas por seleção explícita, com proveniência marcada.
- **CAP-7**
  - **intent:** Codificar cada edital segundo o Codebook derivado do Quadro Conceitual-Analítico, via extração LLM com instrumento congelado e citação-evidência verificável.
  - **success:** Lote L2 roda somente com codebook congelado + Dahlin resolvida + limiar a priori fixado (phase-gates); saída inválida ao schema nunca grava; citação inexistente no texto invalida o campo; cada catálogo registra model+versão+prompt+temperatura+seed.
- **CAP-8**
  - **intent:** Validar o Catálogo L2 contra codificação humana às cegas, estratificada (instituição × ano × regime textual nativo/OCR), com estimação final em subamostra virgem.
  - **success:** Codificação humana travada (`travada_em`) antes de qualquer exposição L2; exposição externa só via `v_catalogo_publicavel`; métricas de Acordo Humano-Máquina (+ ICR em fração ≥20% com segundo codificador humano) reportadas por dimensão com intervalos de confiança.
- **CAP-9**
  - **intent:** Consultar e exportar o corpus codificado via CLI/notebooks descritivos, limitados ao escopo do piloto, com cadeia de custódia exportável por edital.
  - **success:** Consultas coerentes com o Manifesto (fonte única), export CSV; notebooks estampam aviso de escopo descritivo e leem apenas o Manifesto; export de custódia reproduz captura → datação → L2 → validação.

## Constraints

- robots.txt é mandatório (violação = bug crítico); delay mínimo ≥2s/host; janela off-peak no fuso do HOST; User-Agent identificando finalidade acadêmica; bloqueio definitivo suspende o host e segue o lote.
- Corpus bruto imutável: write-once, hash SHA-256 = identidade do Documento; correções criam versão nova (`predecessor_id`), nunca sobrescrevem bytes.
- Phase-gates do L2: codebook completo e congelado + Tríade de Dahlin resolvida + limiar de Acordo Humano-Máquina fixado a priori POR ESCRITO antes da primeira rodada.
- Instrumento LLM congelado por versão do Catálogo (model+versão+prompt+temperatura+seed); troca ⇒ reprocessamento integral ou delimitação explícita no dado.
- Cegamento protocolado: codificação humana travada antes de qualquer exposição à saída LLM; desenvolvimento e estimação sobre subamostras distintas.
- Piloto: 3–5 IFs (um por categoria de portal + ao menos um portal de infraestrutura precária); faixa 30–60 editais; janela temporal fixa 2019–2026.
- Configurações declarativas versionadas em git (mapa-mestre/codebook/politeness), referenciadas por hash nos lotes; segredos NUNCA em config versionada.
- SQLite engine ≥3.51.3 exigida no startup; backup apenas após `wal_checkpoint(TRUNCATE)`.

## Non-goals

- DOU e agregadores externos (validação cruzada futura); foco exclusivo em portais institucionais.
- Snowballing e busca `site:` em buscadores.
- Interface visual (Streamlit) — v2.
- Varredura completa das ~64 instituições — pós-validação do piloto.
- Inscrição/submissão em editais — o agente só lê e preserva.
- Publicação aberta do dataset nesta fase.
- Buscador web genérico — coleta restrita ao Mapa-Mestre.

## Success signal

Demonstração ponta a ponta no piloto: um crawl retomável completa sem bloqueio definitivo, entrega 30–60 editais com Registros L1 íntegros (≥95% campos) e datação auditada (≥90% resolvidos), e a subamostra virgem de validação cega atinge κ ≥0,75 de Acordo Humano-Máquina reportado com intervalos de confiança. Contra-métricas vigentes: volume bruto de downloads e velocidade de coleta jamais são otimizados à custa de precisão do recorte ou polidez.

## Assumptions

- Amostra L3: n mínimo de 15 editais ou 25% do corpus piloto (o que for maior); fração ICR ≥20% da amostra.
- Escalas do codebook: ordinal presente/ausente/grau 0–2 para graduáveis; nominal para categóricos.
- Polidez default: ≥2s/host; off-peak 22h–6h no fuso do host; profundidade de navegação default 3 níveis.
- Nome de arquivo = hash curto + slug; texto gravado como .txt irmão; limiar de escaneado <100 caracteres/página.
- Gatilho Playwright: portal marcado dinâmico OU conteúdo-alvo ausente no HTML estático *(decisão assumida pela arquitetura)*.
- Ética: documentos públicos dispensam avaliação formal (decisão confirmada pelo pesquisador).
- LLM iniciável com qualquer modelo de fronteira; decisão firme antes da análise final do corpus.

## Open Questions

- OQ-1 — Provedor/conta LLM (custo, reproducibilidade, privacidade)? Decidir antes do primeiro lote L2.
- OQ-2 — N exato do piloto junto ao(à) orientador(a)?
- OQ-3 — Critérios finais dos 3–5 IFs piloto (além de um por categoria + um portal precário)?
- OQ-4 — Política para o mesmo edital publicado em portais diferentes da mesma instituição (registro único vs. ambas as capturas)?
- OQ-5 — Limiar de Acordo Humano-Máquina a fixar por escrito com o(à) orientador(a)? (assumido κ ≥0,75) — phase-gate do L2.
- OQ-6 — Tríade de Dahlin entra no codebook ou exclusão justificada na tese? — phase-gate do L2.
