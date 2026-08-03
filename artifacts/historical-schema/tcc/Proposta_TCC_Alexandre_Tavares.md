Plataforma de Gestão Ágil com Agentes Autônomos: Uma Abordagem

Cognitiva para Equipes de Desenvolvimento Reduzidas

# Resumo

Este trabalho aborda as significativas dificuldades metodológicas e a sobrecarga cognitiva que equipes de software pequenas (1-4 membros) enfrentam ao tentar adotar o framework Scrum. Para solucionar essa lacuna, propõe-se o desenvolvimento de uma plataforma de gestão ágil autônoma que utiliza um sistema multi-agente para simular os papéis do Scrum (Product Owner, Scrum Master, Desenvolvedor), criando uma camada de abstração metodológica. A validação da proposta será realizada através da construção de uma Prova de Conceito (PoC), cujo resultado esperado é demonstrar a viabilidade da arquitetura e a capacidade dos agentes de orquestrar um ciclo de trabalho ágil de ponta a ponta, validando a hipótese central do projeto.

# Introdução

As metodologias ágeis revolucionaram o desenvolvimento de software e, entre elas, o Scrum se estabeleceu como o framework dominante. Relatórios anuais da indústria, como o "16th State of Agile Report" (DIGITAL.AI, 2022), apontam que o Scrum foi utilizado por 87% das equipes ágeis, um dado reforçado por entidades como a Scrum.org (SCRUM.ORG, 2023). Seus princípios, projetados para otimizar a comunicação e a entrega de valor, pressupõem uma organização com um número mínimo de membros para garantir o equilíbrio entre as demandas técnicas e as de negócio (Schwaber & Sutherland, 2020).

Contudo, apesar da sua dominância, a aplicação deste paradigma encontra barreiras significativas em um segmento crescente do mercado: o de profissionais autônomos e equipes de software pequenas (1-4 desenvolvedores). A literatura acadêmica e relatos da indústria apontam para um consenso de que a eficácia do Scrum está atrelada ao tamanho da equipe, com estudos indicando que o excesso de cerimônias e a rigidez processual podem gerar uma sobrecarga operacional desproporcional para times muito pequenos (SOFTWARE ENGINEERING STACK EXCHANGE, 2011). Essa visão é corroborada por pesquisas que exploram os desafios de implementação ágil, onde a falta de pessoal para preencher adequadamente os papéis distintos é um inibidor conhecido para a adoção bem-sucedida do framework (MIDDELBURG, 2020).

O desafio enfrentado por esses times menores não é apenas uma sobrecarga de tarefas de gestão, mas uma dificuldade metodológica fundamental com o Scrum. O framework prescreve três papéis distintos e interdependentes: o Product Owner, cuja missão é maximizar o valor do produto; a Equipe de Desenvolvimento, responsável por construir os incrementos; e o Scrum Master, que atua como líder-servidor do processo (Schwaber & Sutherland, 2020). A separação deliberada desses papéis foi concebida para criar um sistema de equilíbrio saudável entre a visão de negócio (o quê), a execução técnica (o como) e a saúde do processo (Layton et al., 2022).

Em um time de um ou dois membros, essa estrutura de equilíbrio se desfaz. Um único profissional é forçado a reconciliar, simultaneamente, três mentalidades distintas e conflitantes: ele precisa atuar como o Product Owner, com foco estratégico; como a Equipe de Desenvolvimento, com foco técnico; e como o Scrum Master, com foco no processo. Na prática, isso torna a aplicação fiel do Scrum impraticável (SOFTWARE ENGINEERING STACK EXCHANGE, 2011), resultando em uma sobrecarga cognitiva severa e na exclusão metodológica dessas equipes do ecossistema ágil padrão (MIDDELBURG, 2020).

Para solucionar essa lacuna, este trabalho propõe o desenvolvimento da Plataforma de Gestão Ágil Autônoma, uma solução tecnológica que atua como uma equipe de apoio virtual. A plataforma utiliza um conjunto de agentes cognitivos para simular fielmente as funções dos três papéis do Scrum. Ao mimetizar as responsabilidades do Product Owner, do Scrum Master e oferecer suporte à Equipe de Desenvolvimento, o sistema resolve o conflito de atribuições e permite que os profissionais se concentrem em suas atividades técnicas primárias.

A contribuição central deste projeto não reside meramente na automação de tarefas, mas na criação de uma camada de abstração metodológica. Essa camada traduz e adapta os princípios do Scrum para um contexto de escala reduzida, tornando-os acessíveis e viáveis para equipes que, de outra forma, não poderiam adotá-los fielmente. O objetivo principal deste trabalho é, portanto, desenvolver e validar uma Prova de Conceito (PoC) que demonstre a interação sinérgica dos três agentes que simulam o time Scrum. A PoC implementará funcionalidades mínimas para o Agente de Produto, o Agente Scrum Master e um Agente de Suporte ao Desenvolvimento, visando validar a arquitetura do sistema e sua capacidade de orquestrar um ciclo de trabalho ágil de ponta a ponta.

Este trabalho está organizado da seguinte forma: a Seção 2 apresenta a fundamentação teórica sobre metodologias ágeis e arquiteturas de agentes de IA. A Seção 3 detalha os objetivos geral e específicos do trabalho. A Seção 4 descreve a metodologia de pesquisa e desenvolvimento a ser adotada e, por fim, a Seção 5 apresenta o cronograma de execução do projeto.

# Fundamentação Teórica

Esta seção apresenta os conceitos fundamentais que sustentam este trabalho. Primeiramente, detalha-se o framework Scrum, que constitui o domínio do problema. Em seguida, serão abordados os paradigmas de agentes cognitivos e Modelos de Linguagem de Grande Porte (LLMs), que formam a base da solução tecnológica proposta.

## O Framework Scrum

O Scrum é um framework ágil para o desenvolvimento e a manutenção de produtos complexos, fundamentado nos pilares do empirismo: transparência, inspeção e adaptação (Schwaber & Sutherland, 2020). Conforme descrito por Layton et al. (2022), o Scrum não é uma metodologia prescritiva, mas sim um conjunto de papéis, eventos e artefatos que fornecem uma estrutura para que as equipes possam resolver problemas complexos de forma iterativa e incremental (Layton et al., 2022; Schwaber & Sutherland, 2020). O objetivo é maximizar o valor entregue, a previsibilidade e a capacidade de resposta a mudanças.

### Os Papéis do Scrum

A eficácia do Scrum depende de uma clara separação de responsabilidades, distribuídas em três papéis distintos que, juntos, formam o Time Scrum (Layton et al., 2022).

Product Owner (PO): O PO é o único responsável por maximizar o valor do produto resultante do trabalho do time. Atuando como a voz do cliente e dos stakeholders, suas principais responsabilidades incluem a gestão e priorização do Product Backlog, a definição da meta do produto e a aceitação dos incrementos ao final de cada Sprint. Para tal, é esperado que o PO possua um profundo conhecimento do negócio e do mercado, além de fortes habilidades de comunicação e decisão para alinhar as necessidades dos stakeholders com a capacidade de execução da equipe (Layton et al., 2022; Schwaber & Sutherland, 2020).

Scrum Master (SM): O Scrum Master atua como um líder-servidor, cuja função é garantir que o Time Scrum adira às práticas e valores do framework. Ele é responsável por facilitar os eventos do Scrum, remover impedimentos que possam bloquear o progresso da equipe, e atuar como um coach tanto para o time quanto para a organização na adoção dos princípios ágeis. O SM protege a equipe de interrupções externas e promove um ambiente de auto-organização e melhoria contínua, sem exercer autoridade gerencial (Layton et al., 2022; Masood et al., 2021; Schwaber & Sutherland, 2020).

Developers (Desenvolvedores): A equipe de Desenvolvedores é composta pelos profissionais que realizam o trabalho de criar um incremento de produto utilizável a cada Sprint. Eles são responsáveis por planejar o trabalho do Sprint (Sprint Backlog), garantir a qualidade do incremento através da "Definição de Pronto" (Definition of Done), e adaptar seu plano a cada dia para atingir a Meta do Sprint. O Scrum preza pela auto-organização, cabendo à equipe de Desenvolvedores a autonomia para decidir como transformar os itens do Product Backlog em um incremento de valor (Layton et al., 2022; Schwaber & Sutherland, 2020).

### Os Eventos do Scrum

O trabalho no Scrum é realizado em ciclos chamados Sprints, que são eventos com duração fixa de um mês ou menos para conter todos os demais eventos (SCHWABER; SUTHERLAND, 2020). Cada Sprint contém outros quatro eventos formais, todos com uma duração máxima (timebox) para otimizar o tempo e garantir a regularidade (LAYTON et al., 2022; SCRUM.ORG, 2024). Conforme o Guia do Scrum, a duração desses eventos é geralmente mais curta para Sprints de menor duração (SCHWABER; SUTHERLAND, 2020).

Tabela 1 – Eventos do Scrum: Propósito e Duração Máxima

Fonte: Adaptado de Layton et al. (2022).

*Nota: A duração dos eventos de Sprint Planning, Sprint Review e Sprint Retrospective é proporcionalmente menor para Sprints mais curtos (ex: para um Sprint de duas semanas, o Sprint Planning tem um timebox de 4 horas).

### Os Artefatos do Scrum

Os artefatos do Scrum representam o trabalho ou valor, sendo projetados para maximizar a transparência das informações-chave.

Product Backlog: Uma lista ordenada e emergente de tudo o que é conhecido ser necessário no produto. É a única fonte de trabalho para o Time Scrum, gerenciada pelo Product Owner.

Sprint Backlog: O conjunto de itens do Product Backlog selecionados para o Sprint, mais um plano para entregar o incremento do produto e realizar a Meta do Sprint. É de propriedade dos Desenvolvedores.

Incremento: A soma de todos os itens do Product Backlog completados durante um Sprint e o valor dos incrementos de todos os Sprints anteriores. Para que um incremento seja considerado pronto, ele deve estar em condição de uso e atender à Definição de Pronto (Definition of Done), um acordo formal do time que assegura a qualidade do trabalho.

## Agentes Cognitivos e Modelos de Linguagem de Grande Porte (LLMs)

Para endereçar o problema da incompatibilidade metodológica, a solução proposta neste trabalho se baseia no paradigma de agentes autônomos, impulsionados por Modelos de Linguagem de Grande Porte (LLMs). Esta seção detalha a arquitetura e as capacidades dessa tecnologia.

### Definição e Arquitetura de Agentes Autônomos

Um agente cognitivo ou autônomo, no contexto de sistemas guiados por LLMs, é uma entidade de software capaz de perceber seu ambiente, tomar decisões contextuais e executar ações de forma independente para atingir objetivos específicos (Cao et al., 2023). Diferente de simples programas, esses agentes operam em um ciclo contínuo de Percepção-Raciocínio-Ação (Perceive-Think-Act):

Percepção (Perceive): O agente coleta informações do seu ambiente, como ler novos documentos de requisitos, receber mensagens de um usuário ou consultar o estado de um projeto através de uma API.

Raciocínio (Think): O LLM, atuando como o motor de raciocínio central do agente, processa as informações percebidas. Ele decompõe objetivos complexos em passos menores, avalia diferentes cursos de ação e planeja a próxima tarefa a ser executada, frequentemente utilizando uma cadeia de pensamento (chain-of-thought) para estruturar sua lógica (Li, Wang, & Yang, 2024).

Ação (Act): Com base no plano, o agente executa uma ação, que pode ser gerar um artefato em linguagem natural (como uma user story), invocar uma ferramenta externa (como atualizar um quadro Kanban) ou comunicar-se com outro agente.

Essa arquitetura cíclica permite que os agentes atuem com um grau de intencionalidade e adaptabilidade, tornando-os aptos a simular as responsabilidades dinâmicas dos papéis do Scrum.

### Padrões de Arquitetura para Sistemas Multiagente (MAS)

Enquanto um único agente pode executar tarefas complexas, a simulação de um fluxo de trabalho colaborativo como o Scrum exige uma arquitetura de Sistema Multiagente (Multi-Agent System - MAS). Em um MAS, múltiplos agentes especializados recebem papéis distintos (ex: "Agente de Produto", "Agente Scrum Master") e colaboram para alcançar um objetivo comum (Lyu, 2025). O sucesso de um MAS depende de três componentes-chave:

Protocolos de Comunicação: Os agentes precisam de uma forma estruturada para trocar informações, negociar tarefas e delegar responsabilidades. Essa comunicação pode ocorrer através de mensagens em linguagem natural, objetos JSON ou outros formatos que permitam um diálogo coerente.

Memória Compartilhada: Para que a colaboração seja eficaz, os agentes devem manter uma visão consistente do estado do projeto. Isso é geralmente alcançado através de um "contexto compartilhado" ou um banco de dados persistente que todos os agentes podem ler e escrever, garantindo que as ações de um agente sejam percebidas pelos outros (Deepchecks, 2024).

Padrões de Orquestração: A forma como os agentes interagem é definida por um padrão de orquestração. Os mais comuns incluem o padrão hierárquico, onde um agente "supervisor" distribui tarefas para agentes "trabalhadores", e o padrão sequencial, onde o resultado do trabalho de um agente serve como entrada para o próximo, como em uma linha de montagem (Microsoft, 2025; CrewAI, 2025

Frameworks práticos como LangChain, Microsoft AutoGen e CrewAI fornecem ferramentas para implementar essas arquiteturas, permitindo a construção de sistemas onde múltiplos agentes podem, de fato, simular as interações de uma equipe humana.

## Conectando Capacidades dos Agentes às Funções do Scrum

A viabilidade de simular os papéis do Scrum com agentes de IA reside na capacidade dos LLMs de executar tarefas análogas às responsabilidades humanas:

Para o Agente de Produto: A Compreensão de Linguagem Natural (NLU) é usada para interpretar documentos de requisitos e extrair necessidades. A Geração de Linguagem Natural (NLG) é usada para escrever user stories e itens de backlog.

Para o Agente Scrum Master: A capacidade de Decomposição de Tarefas é usada para ajudar a planejar um Sprint, enquanto a habilidade de raciocínio é aplicada para monitorar o progresso e sugerir melhorias no processo.

Para o Agente de Suporte ao Desenvolvimento: A capacidade de Uso de Ferramentas (Tool Use) permite que o agente interaja com sistemas externos, como repositórios de código ou ferramentas de CI/CD, para automatizar tarefas de rotina.

Dessa forma, a tecnologia de agentes autônomos oferece uma base sólida para a construção de uma camada de abstração metodológica, o que nos leva à próxima seção, que detalhará os trabalhos relacionados e a lacuna que este projeto visa preencher.

## Trabalhos Relacionados

A aplicação de Inteligência Artificial na gestão de projetos ágeis tem evoluído rapidamente. Enquanto a maioria das ferramentas de mercado foca na automação de tarefas discretas, a fronteira da pesquisa acadêmica explora o uso de Sistemas Multiagente (MAS) para simular fluxos de trabalho complexos, o que se aproxima significativamente da proposta deste trabalho. A seguir, são analisados os trabalhos mais relevantes.

### Análise de Trabalhos Correlatos

He et al. (2024): Em uma revisão sistemática da literatura, He et al. (2024) propõem uma visão para o uso de sistemas multiagente baseados em LLMs na engenharia de software. O método principal foi a análise de frameworks existentes, como ChatDev e MetaGPT, que utilizam agentes especializados (programador, testador, etc.) para executar o ciclo de desenvolvimento. A avaliação, baseada em estudos de caso, demonstrou que esses sistemas podem desenvolver aplicações simples de forma autônoma e eficiente. Os resultados apontam que, embora a colaboração entre agentes melhore a robustez, a fidelidade dos papéis ainda é baixa e são necessárias melhorias nos protocolos de comunicação para aplicações complexas no mundo real.

Cinkusz & Chudziak (2025): Os autores propõem o "CogniSim", um framework multiagente para simular uma equipe Scrum completa (PO, SM, Dev, QA, etc.) em ambientes de larga escala como o SAFe. O método envolve agentes com módulos de raciocínio baseados em LLMs (GPT-4) e ferramentas específicas para seus papéis, que se comunicam via protocolos formais (FIPA ACL). A avaliação foi realizada através de simulações de cerimônias ágeis, como o PI Planning, com métricas de qualidade dos artefatos gerados (ex: similaridade de cosseno) e de alinhamento dos objetivos. Os resultados mostraram que os agentes conseguiram manter a coerência e aderência aos seus papéis, automatizando tarefas complexas de coordenação.

Nguyen et al. (2024): Para superar a rigidez de sistemas de IA que seguem um fluxo em cascata, Nguyen et al. (2024) propõem o "AgileCoder", um sistema multiagente que emula explicitamente os ciclos de Sprint do Scrum. O método utiliza cinco agentes (Gerente de Produto, Scrum Master, Desenvolvedor, Desenvolvedor Sênior e Testador) que interagem através de um "pool de mensagens global" e um grafo de dependências do código para manter o contexto. A avaliação foi rigorosa, utilizando benchmarks de programação (HumanEval, MBPP) e estudos de ablação para medir o impacto de cada componente (sprints iterativos, revisão de código, etc.). Os resultados foram de ponta, superando sistemas anteriores como ChatDev e MetaGPT, provando que a abordagem iterativa e com papéis especializados, inspirada no Scrum, leva a uma maior taxa de sucesso na geração de código correto.

### Tabela Comparativa e Lacuna de Pesquisa

Tabela 2 – Análise Comparativa de Trabalhos Correlatos

Fonte: Elaborado pelo autor (2025).

A análise do estado da arte confirma a hipótese central deste trabalho. As soluções existentes focam na automação do ciclo de desenvolvimento de software para substituir ou aumentar equipes de tamanho normal, ou na gestão de projetos em larga escala. Nenhuma solução existente aborda diretamente o problema da incompatibilidade metodológica e da sobrecarga cognitiva que equipes de 1 a 4 pessoas enfrentam ao tentar aplicar o Scrum.

Existe, portanto, uma clara lacuna de pesquisa para um sistema que utilize agentes autônomos não para substituir o desenvolvedor, mas para atuar como sua equipe de suporte virtual, criando uma camada de abstração que absorva a carga processual. É esta lacuna que o presente trabalho se propõe a preencher.

## Identificação da Lacuna de Pesquisa

A análise do estado da arte confirma a hipótese central deste trabalho. As soluções atuais, tanto comerciais quanto acadêmicas, concentram-se na automação de tarefas incrementais ou na simulação de fluxos de trabalho de engenharia de software em um nível técnico. Nenhuma solução existente aborda diretamente o problema da incompatibilidade metodológica fundamental que equipes pequenas enfrentam ao tentar aplicar o Scrum. Existe, portanto, uma clara lacuna de pesquisa e de mercado para um sistema que utilize agentes autônomos não apenas para automatizar, mas para simular os papéis do Scrum, criando uma camada de abstração que absorva a sobrecarga cognitiva e processual e permita que equipes pequenas se beneficiem do framework de forma fiel. É esta lacuna que o presente trabalho se propõe a preencher.

# Objetivos

Esta seção detalha o escopo e as metas deste trabalho de conclusão de curso.

### Objetivo Geral

Projetar, implementar e validar, por meio de uma Prova de Conceito (PoC), uma plataforma de gestão ágil autônoma baseada em sistema multiagente que simula os papéis do Scrum, para mitigar a sobrecarga metodológica em equipes de 1–4 desenvolvedores e comprovar sua eficácia com base em métricas pré-definidas de carga cognitiva, qualidade dos artefatos e eficiência do fluxo de trabalho.

Objetivos Específicos

Para alcançar o objetivo geral, os seguintes objetivos específicos serão perseguidos:

Agente de Produto (PO): Projetar e implementar as funcionalidades centrais para interpretar requisitos em linguagem natural e gerar um Product Backlog estruturado (user stories, critérios de aceitação e priorização), avaliando a qualidade dos artefatos segundo critérios de conformidade (por exemplo, INVEST) e por acordo interavaliador.

Agente Scrum Master (SM): Projetar e implementar as funcionalidades centrais para facilitar eventos (como o planejamento de um ciclo) e monitorar o processo, avaliando a aderência a timeboxes, a detecção de impedimentos e a consistência do plano de ciclo gerado.

Agente de Suporte ao Desenvolvimento: Projetar e implementar as funcionalidades centrais para auxiliar na execução e atualização de tarefas do ciclo de trabalho, avaliando a precisão das atualizações de status e a consistência com a Definição de Pronto (DoD).

Protocolo de Comunicação e Orquestração: Desenvolver e validar o protocolo de comunicação e a orquestração entre os três agentes, demonstrando colaboração sinérgica em um ciclo ágil simulado de ponta a ponta e medindo a taxa de conclusão do ciclo, a latência e os custos computacionais.

Métricas e Avaliação da PoC: Definir e aplicar um conjunto de métricas para a avaliação holística da PoC, contemplando a carga cognitiva do usuário (por exemplo, NASA-TLX), a qualidade dos artefatos, a eficiência do fluxo (por exemplo, lead/cycle time) e os custos operacionais, comparando os resultados contra um baseline (por exemplo, desenvolvedor solo utilizando uma ferramenta de gestão padrão).

Nota: As metas numéricas específicas (ex: “redução da carga cognitiva em X%”) serão detalhadas na seção de Metodologia, conforme o desenho do experimento de validação.

# Metodologia

Este trabalho adota a metodologia de Design Science Research (DSR), pois seu objetivo principal é a criação e avaliação de um artefato tecnológico inovador — uma plataforma de gestão ágil autônoma — para resolver um problema prático e relevante. O processo seguirá as seis etapas propostas por Peffers et al. (2007), um modelo consolidado para a condução de DSR em Sistemas de Informação e Engenharia de Software.

Figura 1 – Fluxo das Etapas da Metodologia DSR

Fonte: próprio autor.

## Passo 1: Identificação do Problema e Motivação

Descrição: O problema, já detalhado na Introdução e na Fundamentação Teórica, é a dificuldade metodológica e a sobrecarga cognitiva enfrentadas por equipes de software pequenas ao tentar aplicar o framework Scrum. A motivação para a solução é a necessidade de uma ferramenta que atue como uma camada de abstração metodológica, tornando os princípios do Scrum acessíveis a esse público (Peffers et al., 2007; Hevner et al., 2004).

## Passo 2: Definição dos Objetivos da Solução

Descrição: Os objetivos para o artefato a ser construído estão formalizados na Seção 3. O objetivo principal é mitigar a sobrecarga metodológica, o que será avaliado através de métricas de carga cognitiva, qualidade dos artefatos e eficiência do fluxo de trabalho (Peffers et al., 2007).

## Passo 3: Design e Desenvolvimento do Artefato

Descrição: Esta etapa, a atividade central da Design Science Research, corresponde à criação da Prova de Conceito (PoC) da plataforma e engloba tanto o design técnico quanto a implementação dos agentes (Peffers et al., 2007; Dresch et al., 2015). Ela será dividida nas seguintes sub-etapas:

Design da Arquitetura: Será definida a arquitetura do sistema multiagente (MAS), detalhando os três agentes principais (PO, SM, Suporte Dev), o módulo de memória compartilhada e o orquestrador. As saídas desta fase são o diagrama de arquitetura e os contratos de comunicação.

Seleção Tecnológica: Será selecionado o stack tecnológico (Python, um framework de agentes como CrewAI/LangChain, e uma versão específica de LLM) para garantir a reprodutibilidade.

Implementação dos Agentes e Orquestração: Serão implementadas as funcionalidades centrais de cada agente (Produto, Scrum Master e Suporte ao Desenvolvimento) e o protocolo de comunicação que gerencia o fluxo de trabalho de ponta a ponta, conforme detalhado nos objetivos específicos.

Figura 2 – Arquitetura Conceitual do Sistema Multiagente

Fonte: próprio autor.

## Passo 4: Demonstração

Descrição: O artefato será demonstrado através da sua aplicação em um cenário de teste controlado, onde a PoC será utilizada para gerenciar um projeto de pequeno escopo, demonstrando a interação dos agentes e a geração dos artefatos (Peffers et al., 2007).

## Passo 5: Avaliação

Descrição: A avaliação do artefato será realizada por meio de um experimento comparativo para aferir sua utilidade, qualidade e eficácia (Peffers et al., 2007; Hevner et al., 2004). O desempenho da PoC será comparado a um baseline (desenvolvedor solo utilizando uma ferramenta de gestão tradicional). A avaliação se baseará nas métricas definidas nos objetivos:

Qualidade dos Artefatos: Avaliada por especialistas contra o modelo INVEST.

Eficiência do Fluxo: Medida por tempo de ciclo.

Carga Cognitiva: Medida através do questionário NASA-TLX aplicado ao usuário.

Critérios de Sucesso: O sucesso será determinado pela observação de melhorias (ex: redução da carga cognitiva, qualidade superior dos artefatos) em relação ao baseline e pela capacidade do sistema de completar o ciclo sem falhas.

## Passo 6: Comunicação

Descrição: Os resultados desta pesquisa, incluindo o design do artefato, o processo de desenvolvimento e os resultados da avaliação, serão comunicados através deste documento de TCC e da sua apresentação final (Peffers et al., 2007).

# Cronograma

Tabela 3 – Cronograma Detalhado com Fases, Tarefas e Entregáveis

Fonte: Elaborado pelo autor (2025).

Figura 3 – Cronograma de Execução do TCC (Gráfico de Gantt)

Fonte: Elaborado pelo autor (2025).

## Referências

CINKUSZ, B.; CHUDZIAK, D. Agile Software Management with Cognitive Multi-Agent Systems. In: INTERNATIONAL CONFERENCE ON AGENTS AND ARTIFICIAL INTELLIGENCE, 17., 2025, Roma. Anais [...]. Roma: SciTePress, 2025. p. 1-8.

DIGITAL.AI. 16th Annual State of Agile Report. 2022. Disponível em: https://www.stateofagile.com/. Acesso em: 11 ago. 2025.

DRESCH, A.; LACERDA, D. P.; ANTUNES JUNIOR, J. A. V. Design Science Research: método de pesquisa para avanço da ciência e tecnologia. Porto Alegre: Bookman, 2015.

HE, J.; TREUDE, C.; LO, D. LLM-Based Multi-Agent Systems for Software Engineering: Literature Review, Vision and the Road Ahead. arXiv preprint arXiv:2404.04834, 2024. Disponível em: https://arxiv.org/abs/2404.04834. Acesso em: 13 ago. 2025.

HEVNER, A. R. et al. Design science in information systems research. MIS Quarterly, v. 28, n. 1, p. 75-105, 2004.

MIDDELBURG, B. When Scrum doesn’t fit. Medium – The Liberators, 27 jan. 2020. Disponível em: https://medium.com/the-liberators/when-scrum-doesnt-fit-d90357a3356c. Acesso em: 11 ago. 2025.

NGUYEN, T. et al. AgileCoder: Dynamic Collaborative Agents for Software Development based on Agile Methodology. arXiv preprint arXiv:2406.11912, 2024. Disponível em: https://arxiv.org/abs/2406.11912. Acesso em: 13 ago. 2025.

PEFFERS, K. et al. A design science research methodology for information systems research. Journal of Management Information Systems, v. 24, n. 3, p. 45-77, 2007.

SCRUM.ORG. Why is Scrum the most popular Agile framework?. 18 abr. 2023. Disponível em: https://www.scrum.org/resources/blog/why-scrum-most-popular-agile-framework. Acesso em: 11 ago. 2025.

SOFTWARE ENGINEERING STACK EXCHANGE. How can I use Scrum with a freelance team?. 2011. Disponível em: https://softwareengineering.stackexchange.com/questions/106104/how-can-i-use-scrum-with-a-freelance-team. Acesso em: 11 ago. 2025.

VON ALBERTI, M.; SANTOS, E. A.; RUSSO, S. L. Aplicação do Design Science Research em Engenharia de Software: um mapeamento sistemático. Revista de Sistemas de Informação da FSMA, v. 24, p. 24-36, 2019. Disponível em: http://www.fsma.edu.br/si/edicao24/FSMA_SI_2019_2_03.pdf. Acesso em: 11 ago. 2025.

WIKIPEDIA CONTRIBUTORS. Scrum (software development). In: Wikipedia, the free encyclopedia. 5 ago. 2025. Disponível em: <https://en.wikipedia.org/wiki/Scrum_(software_development>. Acesso em: 11 ago. 2025.



--- TABLES ---


Evento | Propósito | Duração Máxima
(Sprint de 1 mês)*

Sprint | Contêiner para todo o trabalho, visando entregar um incremento de produto. | 1 mês

Sprint Planning | Planejamento do trabalho a ser realizado no Sprint, definindo a Meta do Sprint. | 8 horas

Daily Scrum | Sincronização diária da equipe de Desenvolvedores para inspecionar o progresso. | 15 minutos

Sprint Review | Inspeção do incremento com stakeholders para obter feedback e adaptar o Product Backlog. | 4 horas

Sprint Retrospective | Reflexão do time sobre o processo para identificar e planejar melhorias. | 3 horas



Trabalho | Foco Principal | Limitação Identificada (A Lacuna)

He et al. (2024) | Visão geral e mapeamento da área de MAS para Eng. de Software. | É um trabalho de revisão, não uma implementação. Aponta a baixa fidelidade dos papéis como um desafio.

Cinkusz & Chudziak (2025) | Simulação de equipes ágeis para gestão em larga escala (SAFe). | Foco em grandes projetos. Não aborda o problema da sobrecarga e do conflito de papéis em equipes pequenas.

Nguyen et al. (2024) | Automação da geração de código usando um processo ágil simulado. | O objetivo é a produção de software, não a gestão do projeto em si. O sistema não se destina a ser uma ferramenta para um desenvolvedor humano usar.



Fase (DSR) | Atividade | Descrição Detalhada | Duração (Semanas) | Entregável Principal | Marco de Validação (Orientador)

1. Design | 1.1. Design da Arquitetura | Definir a arquitetura MAS, os agentes, a memória compartilhada e o orquestrador. | 2 | Diagrama de Arquitetura | Revisão e aprovação da arquitetura

1. Design | 1.2. Seleção Tecnológica | Selecionar e documentar o stack (Python, CrewAI/LangChain, LLM). | 1 | Matriz de Decisão Tecnológica | Validação da escolha tecnológica

2. Dev | 2.1. Implementação dos Agentes | Implementar as funcionalidades centrais dos agentes PO, SM e Suporte Dev. | 4 | Código-fonte dos agentes | Revisão de código e funcionalidade

2. Dev | 2.2. Implementação da Orquestração | Desenvolver o protocolo de comunicação e o fluxo de trabalho ponta a ponta. | 2 | Pipeline da PoC funcional | Teste de integração e fluxo

3. Validação | 3.1. Execução do Experimento | Executar a PoC no cenário de teste controlado e coletar os dados. | 2 | Logs e registros da execução | Análise do protocolo experimental

3. Validação | 3.2. Análise dos Resultados | Comparar os dados da PoC com o baseline e analisar as métricas. | 3 | Dataset com resultados brutos | Discussão e interpretação dos resultados

4. Comunicação | 4.1. Escrita da Monografia | Redigir todas as seções do TCC, incluindo resultados e conclusões. | 6 | Versão preliminar e final do TCC | Revisões incrementais do texto

4. Comunicação | 4.2. Preparação da Defesa | Criar os slides e preparar a apresentação final. | 2 | Apresentação de slides | Simulação/revisão da apresentação

