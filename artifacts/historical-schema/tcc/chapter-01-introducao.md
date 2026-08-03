# CAPÍTULO 1 — Introdução

Este capítulo apresenta o contexto e a motivação desta pesquisa, o problema investigado, os objetivos, as questões de pesquisa (QP), as contribuições e a organização do trabalho.

## 1.1 Contexto e motivação

As metodologias ágeis consolidaram-se como um paradigma relevante no desenvolvimento de software por favorecerem entrega incremental, inspeção e adaptação. Entre os frameworks mais difundidos, o Scrum estrutura o trabalho em ciclos (Sprints), define papéis, eventos e artefatos, e enfatiza transparência, inspeção e adaptação (Schwaber; Sutherland, 2020). Relatórios de indústria também sugerem ampla adoção, mas, neste trabalho, a fundamentação prioriza fontes normativas e acadêmicas.

O framework Scrum estrutura o trabalho em ciclos denominados Sprints e define três papéis distintos e interdependentes: o Product Owner, responsável por maximizar o valor do produto; a Equipe de Desenvolvimento, responsável por construir os incrementos; e o Scrum Master, que atua como líder-servidor do processo. A separação deliberada desses papéis foi concebida para criar um sistema de equilíbrio saudável entre a visão de negócio, a execução técnica e a saúde do processo. Além dos papéis, o framework prescreve eventos formais com duração máxima definida, incluindo Sprint Planning, Daily Scrum, Sprint Review e Sprint Retrospective.

Contudo, a aplicação do Scrum pode impor barreiras quando o contexto de trabalho não comporta a separação de papéis e a manutenção de cerimônias com regularidade. Em equipes muito pequenas (por exemplo, 1 a 4 desenvolvedores), é comum que um único profissional acumule responsabilidades de produto, processo e desenvolvimento. Isso tende a aumentar custos de coordenação e a alternância de contexto, reduzindo o tempo efetivo dedicado à entrega. Estudos sobre times ágeis e projetos Scrum indicam que dinâmicas de equipe, comunicação e coordenação são fatores críticos e podem ser pressionados em contextos de tamanho reduzido (Moe; Dingsøyr; Dybå, 2010; Dingsøyr et al., 2012).

O desafio, portanto, não é apenas “fazer mais com menos”, mas manter um fluxo de planejamento consistente com restrições de tempo e atenção. Quando papéis e decisões competem pela mesma pessoa, o risco é que o processo seja simplificado de forma ad hoc, perdendo rastreabilidade de decisões e coerência entre visão, requisitos, backlog e execução.

A motivação desta pesquisa é investigar se uma solução de software baseada em agentes pode reduzir parte da carga de coordenação associada ao planejamento. Em particular, agentes baseados em modelos de linguagem podem apoiar a criação e manutenção de artefatos, desde que exista uma camada de governança que limite inconsistências e preserve rastreabilidade. A proposta desta monografia é, portanto, construir e avaliar um artefato que orquestre um fluxo inspirado no Scrum e torne verificável (auditável) a aceitação de certos artefatos por meio de regras e evidências persistidas.

## 1.2 Problema de pesquisa

O problema de pesquisa situa-se na interseção entre gestão ágil e sistemas multiagentes baseados em inteligência artificial. A questão central é: **como reduzir a sobrecarga cognitiva e metodológica de equipes pequenas ao executarem um fluxo inspirado no Scrum, utilizando uma plataforma de gestão autônoma baseada em agentes?**

A dor prática identificada manifesta-se na impossibilidade de equipes de um a quatro desenvolvedores aplicarem fielmente o framework Scrum. Esses profissionais enfrentam dois problemas interconectados: primeiro, a necessidade de assumir simultaneamente múltiplos papéis com responsabilidades distintas e por vezes conflitantes; segundo, a carga administrativa associada à manutenção de cerimônias, artefatos e processos que foram concebidos para equipes maiores.

Quanto à lacuna de pesquisa, parte do estado da arte em sistemas multiagentes para engenharia de software concentra-se em apoiar a produção de código ou em simulações e cenários de maior escala. Este trabalho concentra-se em um recorte distinto: apoiar o planejamento e a governança de artefatos em equipes pequenas, com ênfase na rastreabilidade entre especificação, backlog e planejamento.

Não foi identificada na literatura uma solução que utilize agentes autônomos especificamente para atuar como equipe de suporte virtual para desenvolvedores individuais ou pequenas equipes, criando uma camada de abstração metodológica que absorva a carga processual do Scrum. Esta pesquisa propõe-se a preencher essa lacuna.

## 1.3 Objetivo geral

O objetivo geral deste trabalho é projetar, implementar e avaliar empiricamente, por meio de uma Prova de Conceito, uma plataforma de gestão ágil autônoma baseada em sistema multiagente que orquestra um fluxo de trabalho inspirado no Scrum e abstrai responsabilidades típicas dos papéis de Product Owner, Scrum Master e Equipe de Desenvolvimento, visando mitigar a sobrecarga metodológica em equipes de um a quatro desenvolvedores.

A plataforma proposta não tem como objetivo simular fielmente o Scrum no sentido estrito do Scrum Guide. Em vez disso, ela orquestra um fluxo inspirado no Scrum, abrangendo geração de visão, especificação técnica, roadmap, histórias de usuário, planejamento e execução de sprints, com uma camada adicional de governança baseada em especificação para tornar a aceitação de artefatos repetível e auditável.

## 1.4 Objetivos específicos

Para alcançar o objetivo geral, foram definidos os seguintes objetivos específicos:

- Projetar e implementar funcionalidades de suporte ao papel de produto (Product Owner) para elicitar visão e transformar insumos em artefatos iniciais de planejamento (por exemplo, requisitos de alto nível, roadmap e histórias de usuário).

- Projetar e implementar funcionalidades de suporte ao papel de processo (Scrum Master) para planejar sprints e organizar escopo de forma consistente com capacidade e dependências.

- Projetar e implementar apoio operacional ao papel de desenvolvimento, incluindo decomposição em tarefas e registro de evidências de execução quando aplicável.

- Desenvolver e validar a orquestração entre agentes com controle explícito de fluxo, garantindo previsibilidade do encadeamento de etapas e persistência dos artefatos gerados.

- Definir e aplicar um protocolo de avaliação replicável, com métricas de carga de trabalho percebida, qualidade/rastreabilidade de artefatos e eficiência do fluxo, em comparação com um baseline operacional.

## 1.5 Questões de pesquisa

Com base no problema de pesquisa e nos objetivos definidos, a avaliação é orientada pelas seguintes questões de pesquisa:

- **QP1 (carga de trabalho):** o uso da plataforma está associado a menor carga de trabalho percebida pelo operador durante tarefas de planejamento?
- **QP2 (qualidade e completude):** os artefatos gerados atendem a critérios mínimos de completude e qualidade estrutural, de forma auditável?
- **QP3 (viabilidade operacional):** o fluxo ponta a ponta é executável de forma consistente, com persistência e trilhas de evidência suficientes para auditoria?

## 1.6 Contribuições

Este trabalho apresenta contribuições em duas dimensões complementares: a construção de um artefato tecnológico e a geração de conhecimento científico.

No que se refere ao artefato, a principal contribuição é a concepção e implementação de uma plataforma de gestão ágil autônoma baseada em agentes. O sistema executa um pipeline de planejamento inspirado no Scrum (visão, especificação, roadmap, histórias e planejamento de sprint). Além disso, incorpora uma camada de governança baseada em especificação: a especificação técnica é versionada e tratada como fonte de verdade, e certos artefatos só são aceitos quando atendem regras verificáveis, produzindo trilhas de evidência persistidas.

No que se refere ao conhecimento, este trabalho contribui com evidências empíricas exploratórias sobre a viabilidade de empregar sistemas multiagentes baseados em modelos de linguagem como apoio ao planejamento em equipes pequenas, destacando benefícios, limitações e trade-offs. Também descreve, como contribuição conceitual, um mecanismo de governança por especificação para reduzir inconsistências e preservar rastreabilidade em pipelines generativos.

## 1.7 Organização do trabalho

Este trabalho está organizado em nove capítulos, incluindo esta introdução.

O Capítulo 2 apresenta a fundamentação teórica e os trabalhos relacionados, abordando o framework Scrum e suas limitações em equipes pequenas, os conceitos de sistemas multiagentes e agentes baseados em modelos de linguagem, a abordagem de governança por especificação, e uma análise comparativa de trabalhos correlatos que permite posicionar esta pesquisa em relação ao estado da arte.

O Capítulo 3 descreve o método de pesquisa adotado, caracterizando o estudo como Design Science Research conforme a abordagem de apresentação de algo presumivelmente melhor. São detalhadas as etapas do método, o desenho da avaliação, as métricas e instrumentos utilizados, o planejamento analítico, as considerações éticas pertinentes e as ameaças à validade identificadas.

O Capítulo 4 apresenta o desenvolvimento do artefato, descrevendo os requisitos e restrições, a visão geral da arquitetura, os componentes principais incluindo o orquestrador, os agentes especializados, a memória de estado, o registro de especificações e a autoridade compilada. São detalhados o fluxo de trabalho de ponta a ponta, as principais decisões de implementação e os cenários de uso demonstrados.

O Capítulo 5 estabelece o protocolo de avaliação, formulando as questões de pesquisa, definindo operacionalmente o baseline, descrevendo as tarefas e procedimentos experimentais, caracterizando os participantes, especificando os métodos de coleta de dados e apresentando o plano de análise.

O Capítulo 6 apresenta os resultados obtidos na avaliação, organizados por questão de pesquisa (QP1–QP3).

O Capítulo 7 discute os resultados, interpretando os achados, analisando trade-offs identificados, apresentando implicações práticas para equipes pequenas e confrontando os resultados com a literatura existente.

O Capítulo 8 examina as ameaças à validade e limitações do trabalho, abordando validade interna, externa, de construção e de conclusão, além das limitações do artefato e do escopo da pesquisa.

O Capítulo 9 apresenta as conclusões, retomando os objetivos e verificando seu atendimento, sumarizando as contribuições e indicando direções para trabalhos futuros.
