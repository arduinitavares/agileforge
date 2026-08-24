# CAPÍTULO 4 — Desenvolvimento do artefato

Este capítulo descreve a implementação técnica da Plataforma de Gestão Ágil Autônoma desenvolvida neste trabalho. O texto é ancorado no estado atual do repositório: o sistema é composto por um agente orquestrador, uma máquina de estados finita (FSM) que governa o fluxo, um conjunto de agentes especializados organizados em ferramentas, e uma camada de persistência relacional (SQLModel/SQLite) que registra artefatos, decisões e evidências de validação.

## 4.1 Requisitos e restrições

O artefato foi construído para apoiar equipes pequenas (1–4 pessoas) na execução de um fluxo de planejamento inspirado no Scrum, reduzindo a carga de coordenação e aumentando a rastreabilidade dos artefatos gerados. O foco do sistema é transformar entradas textuais (ideia do usuário e especificação técnica) em artefatos de planejamento estruturados e auditáveis.

### 4.1.1 Requisitos funcionais

Os requisitos funcionais foram definidos a partir do pipeline efetivamente implementado (conforme o fluxo do sistema) e das responsabilidades associadas aos papéis do Scrum, operacionalizadas via agentes especializados:

- **RF01 — Compilação de autoridade a partir de especificação:** o sistema deve permitir registrar versões de especificação técnica, compilar uma autoridade estruturada (regras e restrições) e registrar uma decisão de aceitação para uso no pipeline.
- **RF02 — Definição de visão de produto:** o sistema deve conduzir a elicitação e síntese da visão do produto por diálogo, persistindo-a como base para as fases seguintes.
- **RF03 — Geração de backlog inicial (alto nível):** o sistema deve produzir um backlog inicial como lista ordenada de requisitos de alto nível, incluindo priorização e estimativa relativa.
- **RF04 — Construção de roadmap:** o sistema deve organizar o backlog em uma sequência de releases/marcos (milestones), levando em conta dependências e restrições técnicas.
- **RF05 — Escrita de histórias de usuário:** o sistema deve decompor requisitos do roadmap em histórias de usuário estruturadas, com critérios de aceitação e validações automáticas de formato.
- **RF06 — Planejamento de sprint:** o sistema deve selecionar histórias para um sprint, gerar uma meta do sprint e (opcionalmente) decompor trabalho em tarefas técnicas.
- **RF07 — Validação e evidências pós-salvamento:** o sistema deve validar histórias de usuário contra a autoridade compilada e registrar evidências de validação (passa/falha) para auditoria.

### 4.1.2 Requisitos não funcionais e restrições

Para garantir reprodutibilidade, auditabilidade e controle de escopo, foram adotadas as seguintes restrições técnicas:

- **RNF01 — Aceitação auditável (passa/falha):** embora a geração de conteúdo utilize modelos probabilísticos, a aceitação de artefatos críticos (por exemplo, histórias) é registrada como um resultado binário (passa/falha) com evidência persistida para auditoria.
- **RNF02 — Especialistas sem estado interno persistente:** os agentes especializados não mantêm memória oculta. O estado necessário é passado explicitamente como estruturas serializadas (JSON) e persistido no banco.
- **RNF03 — Fronteiras explícitas de ferramentas (tool boundaries):** cada estado da FSM expõe apenas as ferramentas necessárias à fase corrente, reduzindo ações fora de contexto e tornando o fluxo mais previsível.
- **RNF04 — Persistência local e replicável:** a persistência deve operar em ambiente local, sem dependência de infraestrutura externa, para facilitar replicação em estudos futuros.

## 4.2 Visão geral da arquitetura

A arquitetura segue o padrão de orquestração centralizada (*hub-and-spoke*): um agente orquestrador conduz a interação com o usuário e delega a geração de artefatos para agentes especializados. O controle do fluxo é realizado por uma máquina de estados finita (FSM) que explicita fases, transições permitidas e o conjunto de ferramentas habilitadas em cada etapa.

Como referência do fluxo ponta a ponta, o repositório contém o diagrama em `system_flowchart.mmd`, utilizado neste trabalho como fonte de verdade arquitetural.

### 4.2.1 Camadas e responsabilidades

O sistema é composto por três camadas:

1. **Camada de orquestração (conversa + FSM):** coordena fases do pipeline, aplica instruções por estado e realiza roteamento para ferramentas e subagentes.
2. **Camada de especialistas (agentes/ferramentas):** implementa responsabilidades específicas (visão, backlog, roadmap, histórias, sprint, compilação e validação de autoridade).
3. **Camada de persistência (estado + auditoria):** armazena projetos, especificações versionadas, autoridade compilada, histórias, sprints, tarefas e eventos/evidências.

### 4.2.2 Pilha tecnológica

Na implementação analisada nesta monografia, foram utilizadas as seguintes tecnologias:

- **Linguagem:** Python 3.12+ (conforme `pyproject.toml`).
- **Framework de agentes:** Google ADK (Agent Development Kit), empregado para estruturar agentes, ferramentas e sessão de execução.
- **Integração com LLM:** LiteLLM, como camada de abstração para provedores compatíveis com a API OpenAI (via OpenRouter).
- **Persistência:** SQLModel (sobre SQLAlchemy) e SQLite.

No estado atual do repositório, o arquivo `pyproject.toml` define `requires-python = ">=3.12"`.

## 4.3 Componentes do sistema

Esta seção descreve os principais componentes que materializam a arquitetura.

### 4.3.1 Domínio de workflow e grafo de decisões

O `WorkflowDomain` é a autoridade de roteamento. Ele deriva a posição atual a partir de fatos duráveis do projeto, avalia o grafo de decisões e aceita somente transições compatíveis com a versão e os fingerprints apresentados pelo operador.

As integrações com modelos são receitas de execução associadas a nós específicos do grafo. Elas produzem saídas estruturadas, mas não escolhem a próxima etapa nem persistem decisões fora da transação controlada pelo domínio.

### 4.3.2 Especificação versionada, autoridade compilada e aceitação

A governança por especificação é operacionalizada por três entidades principais no banco:

- `SpecRegistry`: registra versões da especificação técnica, incluindo conteúdo e *hash*.
- `CompiledSpecAuthority`: armazena o resultado da compilação (artefato estruturado com invariantes/regras), incluindo metadados como versão do compilador e *hash* do prompt.
- `SpecAuthorityAcceptance`: registra uma decisão explícita (aceita/rejeita) que habilita o uso daquela versão no pipeline.

Esse desenho separa três momentos: (i) registrar uma especificação, (ii) compilar uma autoridade a partir dela, e (iii) aceitar formalmente a autoridade para uso. Essa separação é relevante porque a compilação, por ser conduzida por um modelo de linguagem, pode produzir resultados que exigem revisão antes de se tornarem “normativos” no fluxo.

### 4.3.3 Agentes especializados e contratos de entrada/saída

Os agentes especializados residem em `adapters/adk/agents/` e são invocados por receitas associadas ao grafo. Cada agente implementa uma responsabilidade específica e opera com contratos estruturados de entrada e saída.

Os especialistas principais são:

- **Ferramenta de visão (`product_vision_tool`):** sintetiza uma visão do produto em formato canônico (template de visão), produzindo um estado estruturado e perguntas de esclarecimento quando necessário.
- **Ferramenta de backlog inicial (`backlog_primer_tool`):** gera requisitos de alto nível, com priorização e estimativa relativa, como pré-condição para o roadmap.
- **Ferramenta de roadmap (`roadmap_builder_tool`):** agrupa e sequencia itens do backlog em marcos, considerando dependências e capacidade.
- **Ferramenta de histórias (`user_story_writer_tool`):** decompõe requisitos do roadmap em histórias de usuário e critérios de aceitação, aplicando validações de estrutura (por exemplo, formato do enunciado e consistência de avisos).
- **Ferramenta de planejamento de sprint (`sprint_planner_tool`):** seleciona histórias para um sprint e produz um plano com meta do sprint, análise de capacidade e decomposição em tarefas.
- **Agente compilador de autoridade (`spec_authority_compiler_agent`):** compila a especificação em um conjunto estruturado de regras e invariantes.

### 4.3.4 Validação pós-salvamento e evidências de rastreabilidade

Além de gerar e persistir histórias, o sistema inclui uma etapa de validação por autoridade compilada. Essa validação é realizada pela ferramenta `validate_story_with_spec_authority`, que recebe um identificador de história, um identificador de versão de especificação e um modo de validação (`deterministic`, `llm` ou `hybrid`).

O resultado da validação é persistido em dois campos principais do registro de história:

- `validation_evidence`: um JSON com evidências do processo de validação (regras/invariantes checadas, avisos/falhas de alinhamento quando aplicável), versão do validador e o resultado (passa/falha).
- `accepted_spec_version_id`: uma chave estrangeira que é preenchida **apenas quando a história passa** na validação. Esse vínculo materializa a pinagem da história a uma versão de autoridade aceita.

Um aspecto importante observado no estado atual do repositório é que essa validação não estava inicialmente encadeada automaticamente ao salvamento de histórias, o que causou um cenário de integração parcial: histórias eram persistidas via `save_stories_tool`, porém não recebiam automaticamente validação pós-salvamento. Essa lacuna foi tratada por meio de um procedimento de *backfill* com script dedicado (`scripts/apply_story_validation.py`), que aplicou a validação contra uma versão aprovada de especificação.

Essa distinção é relevante para este TCC: ela evidencia que a rastreabilidade por pinagem é uma capacidade arquitetural efetivamente implementada, mas que depende de integração correta no pipeline operacional para se manifestar nos dados.

## 4.4 Fluxo end-to-end do pipeline

O fluxo implementado segue a sequência descrita no diagrama `system_flowchart.mmd`:

1. **Pré-fase — Compilação de autoridade:** o usuário fornece uma especificação (arquivo ou texto), o sistema registra a versão e compila um artefato estruturado de autoridade.
2. **Fase 1 — Visão de produto:** o agente de visão elicia e sintetiza a visão; a visão é persistida.
3. **Fase 2 (prep) — Backlog inicial:** o agente de backlog gera requisitos de alto nível priorizados e estimados.
4. **Fase 2 — Roadmap:** o agente de roadmap organiza requisitos em releases/marcos, considerando dependências.
5. **Fase 3 — Histórias de usuário:** o agente de histórias decompõe requisitos do roadmap em histórias com critérios de aceitação.
6. **Fase 4 — Planejamento de sprint:** o planejador seleciona histórias, define meta do sprint, analisa capacidade e decompõe tarefas.

O papel da FSM é garantir que essa sequência seja seguida, explicitando pré-requisitos e transições válidas. Assim, decisões do tipo “ainda não é possível gerar roadmap sem backlog” deixam de ser apenas uma convenção e passam a ser uma regra operacional do sistema.

## 4.5 Modelo de dados e persistência

A persistência do sistema é implementada em SQLite com SQLModel, com chaves estrangeiras e tabelas de apoio para rastreabilidade. No estado atual do repositório, o modelo contempla entidades para produto/projeto, especificação versionada, autoridade compilada, decisão de aceitação, hierarquia temática (themes/epics/features), histórias, sprints, tarefas e registros auxiliares.

Para este trabalho, destacam-se três aspectos do modelo:

1. **Cadeia de rastreabilidade por especificação:** `SpecRegistry → CompiledSpecAuthority → SpecAuthorityAcceptance → UserStory.accepted_spec_version_id`.
2. **Evidência persistida:** `UserStory.validation_evidence` registra o resultado e os motivos (regras) da validação.
3. **Dados para métricas:** timestamps e registros agregados permitem extrair métricas operacionais (por exemplo, contagens e taxas de aprovação).

### 4.5.1 Justificativa do uso de SQLite

SQLite foi adotado por ser um banco embarcado, replicável e suficiente para uma prova de conceito executada localmente. Essa escolha reduz o custo de implantação e facilita a repetição do estudo por terceiros, sem exigir infraestrutura adicional. Como limitação, o desenho não é otimizado para cenários multiusuário concorrentes; isso é tratado como oportunidade para trabalhos futuros.

## 4.6 Cenário de uso e demonstração (execução do pipeline)

Como demonstração do artefato, foi executado um projeto de teste (Produto ID 1) que percorre o pipeline até o planejamento de sprint. O conjunto de métricas extraído do banco (`db/spec_authority_dev.db`) e consolidado pelo comando `python -m scripts.extract_tcc_metrics` registra os seguintes resultados do estado atual:

- **Histórias e tarefas:** 23 histórias de usuário persistidas e 30 tarefas persistidas.
- **Sprint:** 1 sprint persistido, com 5 histórias associadas.
- **Autoridade e aceitação:** 1 versão de especificação aprovada, 1 autoridade compilada e 1 decisão de aceitação.

Para registrar evidências de validação pós-salvamento, foi aplicado o procedimento de *backfill* com `scripts/apply_story_validation.py` contra a versão aprovada de especificação (Spec Version 1). O resultado do *backfill* (aplicado ao conjunto de histórias refinadas canônicas) demonstrou rastreabilidade e auditabilidade:

- **Evidência completa (refinadas):** 8 de 8 histórias refinadas canônicas (100%) passaram a possuir `validation_evidence` persistido (passa/falha).
- **Pinagem por aceitação (refinadas):** 4 de 8 histórias refinadas canônicas (50,0%) receberam `accepted_spec_version_id` válido, indicando validação aprovada.
- **Falhas registradas (refinadas):** 4 histórias falharam com evidências persistidas, predominantemente por `FORBIDDEN_CAPABILITY` (alinhamento com invariantes) e `RULE_LLM_SPEC_VALIDATION` (validação por LLM).

Esses resultados indicam que o mecanismo de pinagem por autoridade compilada é operacional e auditável quando a validação é executada como etapa explícita do pipeline. Ao mesmo tempo, o cenário evidencia um ponto de integração: para maximizar a rastreabilidade em uso contínuo, a validação pós-salvamento deve ser encadeada automaticamente à persistência de histórias.
