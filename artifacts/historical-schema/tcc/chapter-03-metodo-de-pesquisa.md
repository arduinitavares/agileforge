# CAPÍTULO 3 — Método de pesquisa

Este capítulo descreve o método de pesquisa empregado para concepção, desenvolvimento e avaliação do artefato proposto (Plataforma de Gestão Ágil Autônoma). A investigação parte de um problema prático de engenharia de software — a aplicação de práticas Scrum em equipes muito pequenas — e culmina na construção de um artefato de software com avaliação empírica baseada em evidências extraídas do próprio sistema.

## 3.1 Enquadramento metodológico

O estudo adota **Design Science Research (DSR)** como abordagem metodológica por ser orientada à criação e avaliação de artefatos de TI destinados a resolver problemas organizacionais relevantes (Hevner et al., 2004). O artefato desta pesquisa é uma plataforma baseada em sistema multiagente e governança por especificação, com orquestração por máquina de estados finita e persistência em banco de dados.

No enquadramento de contribuições em DSR, este trabalho caracteriza-se como **melhoria** (*improvement*), pois propõe uma alternativa presumivelmente superior para reduzir sobrecarga de coordenação e custos de planejamento em cenários de equipe reduzida, mantendo a rastreabilidade de decisões por meio de validações **auditáveis** (passa/falha) e trilhas de auditoria (Gregor; Hevner, 2013).

## 3.2 Processo de pesquisa (Peffers et al.)

A pesquisa segue as seis atividades de DSR conforme Peffers et al. (2007), com adaptação ao contexto de um artefato de software executável:

1. **Identificação do problema e motivação:** o problema é a dificuldade prática de executar Scrum “como prescrito” em equipes muito pequenas (1 a 4 desenvolvedores), especialmente devido à sobrecarga de papéis, coordenação e manutenção de artefatos.

2. **Definição dos objetivos de uma solução:** (i) reduzir o esforço de coordenação e a carga de trabalho do operador; (ii) manter consistência e rastreabilidade entre especificação, backlog e planejamento; (iii) tornar o fluxo operacionalmente executável com governança por validação **auditável** (passa/falha).

3. **Design e desenvolvimento:** implementação do artefato em **Python 3.12+** (conforme `pyproject.toml`), com agentes e orquestração via **Google ADK** e integração com LLM via **LiteLLM**. A persistência utiliza **SQLModel/SQLite**, viabilizando extração reprodutível de métricas (contagens, tempos e evidências de validação).

4. **Demonstração:** execução ponta a ponta de um ciclo reduzido de planejamento (visão, especificação, backlog e planejamento de sprint) em ambiente controlado, com persistência dos artefatos e registros no banco de dados.

5. **Avaliação:** avaliação exploratória baseada em métricas extraídas do banco de dados e de arquivos de execução (*logs* e *smoke runs* agregados do pipeline), complementada por instrumento de carga de trabalho (NASA-TLX) aplicado ao operador.

6. **Comunicação:** produção desta monografia e documentação de apoio no repositório (scripts de extração e artefatos exportados), permitindo reexecução do protocolo.

## 3.3 Desenho da avaliação

A avaliação foi delineada para responder às questões de pesquisa (QP1–QP3) de forma **exploratória**, com foco em viabilidade e consistência interna das evidências geradas pelo artefato.

### 3.3.1 Desenho experimental e baseline

Adota-se um desenho **intra-sujeito** (single-participant), no qual o próprio pesquisador executa as tarefas em dois modos:

- **Baseline (manual):** execução das tarefas de planejamento com suporte mínimo de automação (produção manual de visão, especificação e backlog, e organização manual de itens para o sprint), registrando tempo e esforço de forma controlada.
- **Intervenção (com a plataforma):** execução do mesmo conjunto de tarefas com suporte do fluxo orquestrado por agentes, registrando evidências diretamente na base de dados.

O escopo do cenário experimental é compatível com equipes pequenas: definição de visão, formalização de especificação técnica, geração/refino de histórias e planejamento de sprint. O objetivo não é inferir significância estatística, mas **verificar viabilidade**, coerência e rastreabilidade operacional do fluxo.

### 3.3.2 Fontes de dados e rastreabilidade

As evidências quantitativas e de rastreabilidade são extraídas principalmente de:

- **Banco de dados SQLite**: registra produtos, histórias, sprints, versões de especificação, aceites e eventos de fluxo.
- **Evidências de validação por autoridade de especificação**: cada história pode armazenar (i) a versão de especificação aceita à qual foi “pinada” e (ii) um JSON de evidências de validação (pass/fail e regras aplicadas).
- **Eventos de workflow**: a plataforma persiste eventos para medição de esforço/tempo em atividades de planejamento (por exemplo, eventos associados ao rascunho/revisão/salvamento do plano de sprint e marcações operacionais relevantes ao protocolo de avaliação).
- **Registros de execução (*smoke runs*)**, quando disponíveis, para sumarização agregada de desempenho e consistência do pipeline (evidência complementar, não restrita ao `product_id` avaliado).

Para assegurar reprodutibilidade, a extração de métricas utiliza script dedicado que gera saídas auditáveis (CSV/JSON) e também sumarização em formato compatível com inserção no Capítulo 6.

## 3.4 Métricas e instrumentos

A avaliação é organizada em três dimensões: carga de trabalho percebida, qualidade/rastreabilidade dos artefatos e eficiência do fluxo.

### 3.4.1 Carga de trabalho percebida (NASA-TLX)

Para abordar a **QP1 (carga de trabalho)**, utiliza-se o **NASA-TLX (Task Load Index)** aplicado ao operador ao final das execuções (baseline e intervenção), com foco em carga de trabalho percebida.

Neste estudo, adota-se o NASA-TLX em formato **RAW (não ponderado)** e com **cinco dimensões**: Demanda Mental, Demanda Temporal, Desempenho, Esforço e Frustração. A dimensão **Demanda Física** é excluída por não ser pertinente ao tipo de tarefa (planejamento e produção de artefatos textuais), cujo custo é majoritariamente cognitivo. As respostas são coletadas fora do banco de dados (instrumento de pesquisa), registradas como artefato em `artifacts/nasa_tlx_raw_5d_form.csv`, e utilizadas para análise descritiva.

### 3.4.2 Qualidade e conformidade dos artefatos (proxies de INVEST + regras)

Para abordar a **QP2 (qualidade e completude)**, a avaliação considera dois níveis complementares:

1. **Conformidade estrutural e completude mínima**: regras determinísticas verificam a presença e consistência de elementos essenciais (por exemplo, título e critérios de aceitação em histórias), produzindo evidências persistidas como JSON. Essas regras reduzem ambiguidade e tornam auditável a aderência mínima a padrões.

2. **Proxies de INVEST**: parte dos critérios INVEST pode ser operacionalizada por verificações automatizadas (especialmente o “Testable”, associado à presença de critérios de aceitação verificáveis). Reconhece-se que aspectos como “Valuable” permanecem dependentes de julgamento humano, motivo pelo qual a análise combina indicadores objetivos com interpretação qualitativa.

### 3.4.3 Eficiência do fluxo (tempos e eventos)

Para avaliar eficiência operacional, são utilizadas métricas extraídas do banco de dados:

- **Duração de atividades de planejamento**, quando registrada em eventos de workflow (por exemplo, duração associada ao salvamento de planos de sprint).
- **Métricas derivadas de timestamps** em histórias (por exemplo, tempo médio até conclusão quando há registro de criação e conclusão).

Essas medidas permitem comparar, de forma descritiva, a execução assistida pela plataforma versus o baseline manual (quando medido), além de caracterizar o comportamento do artefato ao longo das execuções.

## 3.5 Protocolo de replicação

Para permitir reexecução do estudo e auditoria dos números apresentados nos resultados, o protocolo mínimo de replicação é:

1. Executar o fluxo do artefato para um produto (criação/atualização de especificação, compilação/aceite quando aplicável, geração de histórias e planejamento de sprint), garantindo persistência no banco.
2. Quando necessário, executar o procedimento de validação das histórias contra a autoridade de especificação aceita (backfill) para garantir que evidências e pinagem estejam registradas.
3. Extrair métricas do banco com o script de extração, gerando os artefatos em `artifacts/`.

Exemplo de extração de métricas:

```bash
python -m scripts.extract_tcc_metrics db/spec_authority_dev.db
```

O script gera, no mínimo, os arquivos `artifacts/metrics_summary.json` e `artifacts/metrics_summary.csv`, além de resultados de consultas em `artifacts/query_results/`.

## 3.6 Planejamento de análise

Os dados são analisados de forma **descritiva e triangulada**:

- **Quantitativo (descritivo):** contagens, proporções e médias extraídas do banco e dos arquivos de execução (por exemplo, total de histórias, proporção com evidência de validação, duração média de atividades instrumentadas).
- **Qualitativo (observacional):** inspeção da coerência dos artefatos gerados (clareza de histórias, adequação de critérios de aceitação e consistência com a especificação) e análise de casos de falha, quando regras de validação bloqueiam aceitação.

Como se trata de avaliação exploratória com único participante, não se realizam inferências estatísticas; os resultados são interpretados como evidência de viabilidade, consistência interna e rastreabilidade do fluxo.

## 3.7 Aspectos éticos

A avaliação é conduzida em ambiente controlado e não envolve coleta de dados pessoais sensíveis de terceiros. O operador é o próprio pesquisador, e os dados persistidos referem-se a artefatos de engenharia de software (especificações, histórias, sprints) e evidências de validação. Instrumentos subjetivos (NASA-TLX) são tratados como dados de pesquisa e não como dados operacionais do sistema.

## 3.8 Ameaças à validade

Reconhecem-se as seguintes ameaças e limitações:

- **Validade de construção:** parte dos conceitos (qualidade textual, valor de histórias) não é plenamente capturável por regras automáticas. Mitiga-se com combinação de proxies objetivos (evidências determinísticas) e análise qualitativa.
- **Validade interna (instrumentação):** o que pode ser medido depende do que o artefato registra (eventos, timestamps e evidências). Mitiga-se com extração padronizada via script e inspeção do esquema do banco no momento da extração.
- **Validade externa:** resultados são limitados ao escopo do cenário e ao perfil do operador (single-participant), não permitindo generalização para contextos organizacionais distintos.
- **Confiabilidade de LLM:** modelos de linguagem podem produzir variação. A arquitetura de governança por especificação visa reduzir efeitos nocivos por meio de validações auditáveis (passa/falha), aceites explícitos e trilhas de evidência persistidas.
