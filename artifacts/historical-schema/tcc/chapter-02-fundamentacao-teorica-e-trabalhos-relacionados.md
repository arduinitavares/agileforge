# CAPÍTULO 2 — Fundamentação teórica e trabalhos relacionados

Este capítulo apresenta os fundamentos teóricos que sustentam a pesquisa e sistematiza trabalhos relacionados. O objetivo é estabelecer uma base conceitual coerente com o artefato implementado, sem confundir fundamentação teórica com documentação técnica. Assim, (i) discute-se o Scrum e limitações práticas em equipes pequenas; (ii) descrevem-se conceitos essenciais de agentes com modelos de linguagem e sistemas multiagentes, no nível necessário para entender o artefato; (iii) apresenta-se a noção de governança por especificação como mecanismo de controle de qualidade e viabilidade técnica; e (iv) posiciona-se este trabalho frente ao estado da arte.

## 2.1 Scrum em equipes pequenas / limitações práticas

O Scrum é descrito no *Scrum Guide* como um framework para o desenvolvimento e manutenção de produtos complexos, baseado em empirismo (transparência, inspeção e adaptação). Sua operacionalização envolve papéis, eventos e artefatos, organizados em ciclos denominados sprints (SCHWABER; SUTHERLAND, 2020).

Em sua forma canônica, o Scrum define papéis com responsabilidades distintas: Product Owner (maximização de valor), Scrum Master (facilitação e melhoria do processo) e Developers (construção do incremento). Em equipes pequenas, particularmente quando um único profissional acumula mais de um papel, a separação de responsabilidades tende a se tornar instável. O resultado prático é a alternância frequente de contexto decisório (produto, processo e implementação), com potencial aumento de carga operacional e carga cognitiva.

Além do acúmulo de papéis, o custo relativo dos eventos formais do Scrum pode ser proporcionalmente maior em equipes reduzidas. Cerimônias como planejamento, revisão e retrospectiva são essenciais para o empirismo do framework. Contudo, quando há poucos participantes, o tempo investido em coordenação compete diretamente com o tempo disponível para desenvolvimento. Estudos de adoção ágil e relatos acadêmicos sobre desafios de implementação indicam que adaptações e “hibridizações” de práticas são comuns em contextos com restrições de tamanho, maturidade e disponibilidade de tempo (HODA; NOBLE; MARSHALL, 2013; STETTINA; HÖRZ, 2015).

Neste TCC, a limitação investigada não é a “inadequação absoluta” do Scrum, mas a fricção operacional decorrente de aplicar um conjunto de práticas concebidas para equipes com divisão de responsabilidades em um contexto de escala reduzida. Essa fricção se manifesta na manutenção de artefatos (por exemplo, backlog e planejamento), no gerenciamento de regras e restrições técnicas e na necessidade de manter consistência entre artefatos ao longo do tempo.

## 2.2 Sistemas multiagentes e agentes com LLM

Agentes baseados em Modelos de Linguagem de Grande Porte (LLMs) podem ser entendidos, no escopo desta pesquisa, como componentes de software que recebem entradas textuais (por exemplo, objetivos e especificações), produzem saídas textuais estruturadas e executam ações por meio de ferramentas. Em sistemas desse tipo, o comportamento emerge da combinação entre: (i) instruções e restrições explícitas; (ii) contratos estruturados de entrada e saída; e (iii) mecanismos de execução que conectam o agente a ferramentas e a uma memória persistente.

Quando múltiplos agentes são combinados, forma-se um sistema multiagente com divisão de responsabilidades. Em termos arquiteturais, duas decisões são particularmente relevantes para este trabalho:

1. **Orquestração centralizada:** um componente controlador coordena quais agentes podem atuar em cada etapa, em vez de permitir que agentes chamem uns aos outros livremente.
2. **Memória persistente e compartilhada:** o estado do projeto (artefatos, versões de especificação, decisões e evidências) é registrado em uma base de dados, reduzindo dependência de memória implícita do modelo.

Essas decisões se conectam ao objetivo do trabalho: reduzir a carga de coordenação do usuário e aumentar previsibilidade do fluxo. O artefato implementado adota orquestração centralizada e persistência relacional, como detalhado no Capítulo 4.

## 2.3 Governança por especificação (Spec-Driven) como controle de viabilidade e qualidade

Em projetos de software, decisões de planejamento precisam respeitar simultaneamente desejabilidade (valor) e viabilidade (restrições técnicas e de qualidade). Na literatura de referência adotada pelo artefato, a viabilidade aparece como entrada necessária para planejar roadmap, e padrões como *Definition of Done* (DoD) são utilizados para garantir transparência de qualidade e critérios de aceitação (LAYTON, 2018).

Neste trabalho, “governança por especificação” é utilizada como conceito operacional: a especificação técnica é tratada como fonte explícita de regras e restrições, e parte dessas restrições é compilada em um artefato estruturado (autoridade compilada). A partir disso, o sistema separa dois momentos:

1. **Geração (não determinística):** produção inicial de artefatos textuais por LLM.
2. **Aceitação (auditável):** validação “passa/falha” baseada em regras (determinísticas e, quando configurado, validação por LLM), com evidência persistida.

Do ponto de vista teórico, essa separação é compatível com a noção de que critérios de qualidade (como DoD e critérios de aceitação) precisam ser explícitos para viabilizar inspeção e adaptação. Do ponto de vista arquitetural, essa abordagem busca reduzir deriva de escopo e tornar decisões auditáveis. A operacionalização concreta (registro versionado de especificação, compilação e pinagem de aceitação) é descrita no Capítulo 4.

## 2.4 Trabalhos relacionados (tabela comparativa)

Trabalhos recentes investigam o uso de LLMs e sistemas multiagentes em engenharia de software e processos inspirados em práticas ágeis. Para posicionar este TCC, esta seção sintetiza três referências mencionadas na proposta do projeto.

Tabela 1 — Comparação de trabalhos relacionados

| Trabalho | Objetivo | Contexto / Escala | Método / Avaliação | Lacuna para este TCC |
|---|---|---|---|---|
| He et al. (2024) | Revisar e organizar sistemas multiagentes com LLM em engenharia de software | Visão ampla do domínio | Revisão de literatura e síntese de frameworks | Não foca em gestão ágil para equipes 1–4 nem em governança por especificação aplicada a artefatos de planejamento |
| Cinkusz; Chudziak (2025) | Simular papéis e cerimônias ágeis com agentes | Times e processos em escala (ex.: SAFe) | Simulações e métricas de qualidade/alinhamento | Direcionado a escala organizacional; não enfatiza suporte operacional a um desenvolvedor humano em contexto de equipe reduzida |
| Nguyen et al. (2024) | Emular ciclos ágeis com agentes para geração de software | Engenharia de software com foco em código | Benchmarks e estudos de ablação | O objetivo principal é produção/validação de código; não é uma plataforma de planejamento com rastreabilidade por autoridade compilada |

Ainda que os trabalhos acima se aproximem do tema ao empregar agentes e inspiração em práticas ágeis, eles não tratam, de forma central, a redução de carga metodológica em equipes pequenas por meio de um pipeline de planejamento com mecanismos explícitos de auditoria e controle por especificação.

## 2.5 Lacunas e posicionamento do trabalho

Com base na síntese apresentada, identifica-se uma lacuna específica: a ausência de uma solução voltada a apoiar diretamente um desenvolvedor (ou equipe muito pequena) na produção e manutenção de artefatos de planejamento (visão, backlog, roadmap, histórias e sprint), com rastreabilidade explícita a uma especificação versionada.

O trabalho proposto difere por combinar (i) orquestração centralizada por FSM, (ii) agentes especializados para cada artefato do pipeline e (iii) um mecanismo de governança por especificação que produz evidências de validação e pinagem de aceitação. Assim, o foco não é substituir a equipe humana, mas reduzir a sobrecarga de coordenação e aumentar a auditabilidade do planejamento em um contexto de escala reduzida.
