# CAPÍTULO 5 — Protocolo de avaliação

Este capítulo apresenta o protocolo de avaliação adotado para verificar a viabilidade e analisar, de forma exploratória, os efeitos do artefato proposto. O protocolo foi definido para ser **replicável**: descreve tarefas, instrumentos, fontes de dados, critérios de coleta e etapas de extração de métricas, com rastreabilidade para os artefatos gerados pelo próprio sistema.

## 5.1 Questões de pesquisa e variáveis observáveis

A avaliação é orientada por questões de pesquisa (QP) derivadas do problema e dos objetivos:

- **QP1 (carga de trabalho):** o uso da plataforma está associado a menor carga de trabalho percebida pelo operador durante tarefas de planejamento?
- **QP2 (qualidade e completude):** os artefatos gerados atendem a critérios mínimos de completude e qualidade estrutural, de forma auditável?
- **QP3 (viabilidade operacional):** o fluxo ponta a ponta é executável de forma consistente, com persistência e trilhas de evidência suficientes para auditoria?

Para cada QP, são usadas variáveis observáveis extraídas de instrumentos e do banco de dados:

- **QP1:** escores NASA-TLX (instrumento) e tempos de execução (baseline manual e/ou eventos registrados).
- **QP2:** proporção de histórias com evidência de validação, proporção de histórias aceitas (quando aplicável) e motivos de falha.
- **QP3:** existência de vínculo explícito entre histórias e a versão de especificação aceita (pinagem) e existência de evidências persistidas.

## 5.2 Desenho experimental e baseline (definição operacional)

O desenho é **intra-sujeito** (single-participant), em que o participante é o próprio pesquisador, coerente com o caráter exploratório da Prova de Conceito. O baseline e a intervenção são definidos operacionalmente, com execução das mesmas tarefas em dois modos.

### 5.2.1 Cenário baseline (sem plataforma)

No baseline, o participante executa as tarefas de planejamento manualmente, utilizando ferramentas genéricas (editor de texto e planilha) e um cronômetro para registrar tempos. Os seguintes artefatos são produzidos de forma manual:

- visão do produto;
- especificação técnica;
- lista de itens de backlog e histórias;
- plano de sprint (seleção de escopo e, quando possível, decomposição em tarefas).

O baseline representa uma execução realista sem automação, não uma prática “ótima” universal.

### 5.2.2 Cenário experimental (com a plataforma)

No cenário experimental, as mesmas tarefas são executadas por meio do fluxo orquestrado por agentes. A plataforma persiste artefatos e registra evidências no banco de dados, incluindo eventos de workflow (para atividades instrumentadas), resultados de validação e trilhas de rastreabilidade.

## 5.3 Objeto de estudo e ambiente

### 5.3.1 Objeto de estudo

O objeto de estudo é a instância executável do artefato descrito no Capítulo 4, incluindo:

- orquestração do fluxo por máquina de estados;
- ferramentas/agentes para visão, backlog inicial, roadmap, histórias e planejamento de sprint;
- governança por especificação com versionamento, compilação e aceitação explícita;
- persistência em banco SQLite.

### 5.3.2 Ambiente de execução

O artefato é executado em ambiente Python 3.12+ (conforme `pyproject.toml`) e utiliza persistência em SQLite. Nesta monografia, os resultados reportados no Capítulo 6 são extraídos de `db/spec_authority_dev.db` e se restringem ao `product_id = 1`. Para fins de reprodutibilidade, os números reportados são acompanhados de metadados de extração (timestamp, caminho da base e hash de commit), conforme registrado no artefato de métricas.

## 5.4 Tarefas e procedimentos (passo a passo)

As tarefas foram definidas para cobrir o fluxo “Scrum-shaped” do artefato (conforme o diagrama de fluxo do sistema), incluindo a etapa de governança por especificação.

### 5.4.1 Sequência de tarefas

As tarefas são executadas na ordem abaixo, no baseline e na intervenção, respeitando as particularidades do cenário:

1. **T1 — Definição de visão:** produzir a visão do produto em formato estruturado.
2. **T2 — Especificação técnica:** produzir a especificação técnica do produto (regras e restrições).
3. **T3 — Compilação e aceitação de autoridade (intervenção):** compilar a autoridade a partir da especificação e registrar o aceite explícito (quando aplicável).
4. **T4 — Backlog inicial (intervenção) / lista inicial (baseline):** derivar requisitos de alto nível a partir de visão e especificação.
5. **T5 — Roadmap (intervenção) / planejamento macro (baseline):** organizar itens em horizonte temporal e dependências.
6. **T6 — Histórias de usuário:** decompor itens priorizados em histórias com critérios de aceitação.
7. **T7 — Planejamento de sprint:** selecionar escopo para um sprint e, quando aplicável, decompor em tarefas.

### 5.4.2 Regras de parada e intervenção manual

Para preservar comparabilidade e rastreabilidade, são definidas regras de parada:

- uma tarefa é considerada concluída quando o artefato correspondente é aceito pelo participante e, no cenário experimental, persistido;
- quando ocorrer falha determinística (por exemplo, ausência de critérios de aceitação), o participante registra a intervenção necessária (refino do texto) e reexecuta a etapa correspondente.

As intervenções manuais são tratadas como **observação qualitativa** e também impactam o tempo total (baseline e intervenção).

## 5.5 Participante

O participante é o próprio pesquisador (identificado nos artefatos como `Alexandre`), com experiência em desenvolvimento de software e familiaridade com práticas ágeis. A coleta reportada nesta monografia foi realizada em 19/02/2026. Essa configuração viabiliza a avaliação em TCC e caracteriza os resultados como exploratórios, sem generalização estatística.

## 5.6 Coleta de dados: evidência empírica vs. observação

Em resposta à exigência de transparência metodológica, a coleta separa explicitamente:

- **evidências empíricas automatizadas** (extraídas do banco e arquivos gerados);
- **observações qualitativas** (registro do participante durante a execução).

### 5.6.1 Evidências automatizadas (banco SQLite)

As evidências automatizadas são extraídas de tabelas e campos persistidos, incluindo:

- **eventos de workflow:** registros em `workflow_events` com timestamps e, quando aplicável, duração e contagem de turnos;
- **artefatos persistidos:** produtos, histórias e sprints (incluindo timestamps disponíveis);
- **governança por especificação:** versões de especificação, autoridade compilada, decisão de aceite e evidências de validação das histórias.

Observação importante: a interpretação de "tempo" depende do que está instrumentado como evento (por exemplo, eventos de workflow com `duration_seconds`) e do que é derivável de timestamps (por exemplo, quando há apenas `created_at`/`timestamp`). O protocolo não assume métricas não registradas. No baseline, os tempos são coletados externamente (cronômetro) e não são persistidos no banco. Na intervenção, há eventos com `workflow_events.duration_seconds` (incluindo `VISION_SAVED`, `SPEC_COMPILED`, `SPRINT_PLAN_SAVED` e `FSM_STATE_DWELL`) e, quando necessário, podem-se calcular deltas entre timestamps; em ambos os casos, as medidas representam *wall-clock* e **não isolam esforço humano**, podendo incluir latência de rede, processamento de LLM e pausas.

### 5.6.2 Evidências automatizadas (scripts e artefatos de extração)

Para garantir reprodutibilidade e auditoria dos números, utiliza-se um script de extração de métricas que:

- identifica o esquema da base (tabelas/colunas) no momento da extração;
- extrai métricas por produto (contagens, tempos agregados e indicadores de governança);
- registra metadados de reprodutibilidade (timestamp, caminho da base e hash do commit, quando disponível);
- gera saídas em JSON/CSV e resultados de consultas.

Adicionalmente, quando necessário, aplica-se validação em lote (backfill) das histórias contra a versão aprovada de especificação, garantindo que evidências e pinagem estejam persistidas antes da extração.

### 5.6.3 Instrumento de percepção (NASA-TLX)

Ao final de cada cenário (baseline e intervenção), aplica-se o NASA-TLX em formato **RAW (não ponderado)**, com cinco dimensões: Demanda Mental, Demanda Temporal, Desempenho, Esforço e Frustração. A Demanda Física é excluída por não ser pertinente às tarefas do estudo (planejamento e produção textual). As respostas são registradas como artefato do estudo em `artifacts/nasa_tlx_raw_5d_form.csv` e reportadas no Capítulo 6.

Para eliminar ambiguidade na dimensão **Desempenho (Performance)**, adota-se explicitamente a seguinte âncora: **0 = desempenho perfeito (sem problemas)** e **100 = desempenho muito ruim (muitos problemas/falha)**; portanto, **valores menores indicam melhor desempenho percebido**.

### 5.6.4 Observações qualitativas

Durante a execução, o participante registra:

- pontos de fricção no fluxo (por exemplo, necessidade de reformulação de comandos);
- casos de inconsistência e como foram resolvidos;
- incidência de falhas determinísticas e tipo de correção aplicada;
- percepção de clareza e utilidade dos artefatos gerados.

Esses registros não são tratados como métrica automatizada, mas como suporte interpretativo para a discussão.

## 5.7 Critérios de consistência dos dados e de sucesso (viabilidade)

Por tratar-se de avaliação exploratória, os critérios de sucesso são formulados como **critérios de viabilidade** e **consistência de evidência**, e não como metas de desempenho “ótimo”. O artefato é considerado viável para fins desta pesquisa quando:

1. o fluxo é executável ponta a ponta ao menos uma vez, com persistência dos artefatos principais;
2. existe trilha de auditoria mínima: histórias possuem evidência de validação persistida e, quando aprovadas, vínculo com a versão de especificação aceita;
3. as métricas extraídas são reproduzíveis a partir do banco e do script (saídas JSON/CSV com metadados);
4. os resultados de NASA-TLX e tempos permitem comparação descritiva entre baseline e intervenção (sem inferência estatística).

## 5.8 Plano de análise

A análise segue abordagem mista e descritiva:

### 5.8.1 Quantitativo (descritivo)

- **NASA-TLX (QP1):** comparação descritiva entre baseline e intervenção para cada dimensão, e sumarização por escore global RAW quando aplicável.
- **Eficiência (QP1):** tempos medidos no baseline (cronômetro) e tempos agregados no cenário experimental conforme eventos instrumentados e/ou timestamps disponíveis.
- **Qualidade e completude (QP2):** proporção de histórias com evidência de validação, proporção de histórias aceitas e distribuição de falhas por regra.
- **Rastreabilidade (QP3):** proporção de histórias pinadas a uma versão de especificação aceita e completude de evidências persistidas.

### 5.8.2 Qualitativo (triangulação)

As observações registradas pelo participante são usadas para:

- explicar causas prováveis de falhas determinísticas (por exemplo, ausência de critérios de aceitação);
- identificar trade-offs e custos de governança (refino necessário, tempo adicional de validação);
- contextualizar inconsistências do modelo de linguagem e mitigação por regras.

Os resultados são reportados no Capítulo 6 (evidências) e interpretados no Capítulo 7 (discussão), mantendo separação explícita entre evidência automatizada e observação qualitativa.
