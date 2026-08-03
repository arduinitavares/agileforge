# CAPÍTULO 8 — Ameaças à validade e limitações

Este capítulo explicita as ameaças à validade e as limitações do estudo, em alinhamento com o caráter **exploratório** da avaliação e com a regra metodológica adotada ao longo desta monografia: reportar apenas resultados sustentados por evidências auditáveis no repositório (banco SQLite e artefatos de extração) e por instrumentos coletados externamente (por exemplo, NASA-TLX), sem inferências não suportadas.

A discussão organiza-se em categorias usuais de validade: **construção**, **interna**, **externa** e **conclusão**, além de limitações de **confiabilidade/reprodutibilidade** e de **instrumentação**.

## 8.1 Validade de construção (construct validity)

A validade de construção diz respeito a quão bem as métricas e instrumentos utilizados representam os conceitos que se pretende avaliar.

### 8.1.1 Carga de trabalho percebida (NASA-TLX)

O estudo utiliza NASA-TLX em formato RAW (não ponderado) e com cinco dimensões (Demanda Mental, Demanda Temporal, Desempenho, Esforço e Frustração), excluindo Demanda Física por não ser pertinente às tarefas. Essa decisão reduz ruído para o tipo de atividade avaliada (planejamento e produção de artefatos textuais), porém introduz duas limitações:

- A medida permanece **subjetiva** e sensível a expectativas do participante, efeitos de novidade e familiaridade com a ferramenta.
- A aplicação em **um único participante** não permite estimar variabilidade interindividual; assim, a medida serve como evidência descritiva de viabilidade, não como confirmação estatística.

Além disso, a dimensão **Desempenho (Performance)** pode ser ambígua em estudos por depender de ancoragem. Para mitigar, o protocolo fixa explicitamente as âncoras utilizadas (0 = desempenho perfeito; 100 = desempenho muito ruim), de modo que a interpretação “menor é melhor” permaneça inequívoca.

### 8.1.2 Qualidade e completude das histórias

A qualidade de histórias de usuário é um conceito multifacetado, que envolve clareza, valor, negociabilidade e testabilidade. Neste trabalho, parte da avaliação é operacionalizada por **regras de validação** (determinísticas e, quando configurado, validação por LLM) e evidências persistidas no banco.

Essa estratégia fortalece a auditabilidade, mas limita o que é efetivamente medido:

- As regras capturam **completude mínima** (por exemplo, presença de critérios de aceitação) e conformidade estrutural, mas não garantem que o conteúdo seja, de fato, valioso ou suficiente para implementação.
- Proxies de conceitos amplos (por exemplo, INVEST) podem super-representar aspectos testáveis por regra e sub-representar aspectos dependentes de julgamento humano.

Portanto, a interpretação correta é: o artefato demonstra capacidade de **detectar e registrar** incompletude mínima, e não de garantir qualidade semântica plena em todos os itens gerados.

### 8.1.3 Eficiência temporal

O conceito de “tempo” pode representar esforço humano, tempo de processamento do sistema, latência de rede, e pausas operacionais. No repositório, a evidência temporal disponível para a intervenção inclui:

- deltas wall-clock entre timestamps persistidos no banco (marcos do fluxo);
- `duration_seconds` preenchido em eventos de workflow (incluindo `SPRINT_PLAN_SAVED`) e dwell times por estado (`FSM_STATE_DWELL`), que ainda assim não isolam esforço humano;
- ausência de tempos baseline (manual) persistidos no banco.

Assim, o estudo mede, de forma reprodutível, o **intervalo decorrido** entre marcos do fluxo (intervenção), mas não mede de forma isolada o esforço humano nem sustenta comparação temporal completa com baseline apenas a partir do DB.

## 8.2 Validade interna (internal validity)

Validade interna refere-se ao grau em que os efeitos observados podem ser atribuídos à intervenção, e não a fatores de confusão.

### 8.2.1 Desenho intra-sujeito com o próprio pesquisador

A avaliação é intra-sujeito e realizada pelo próprio pesquisador. Essa escolha é adequada para uma Prova de Conceito, mas implica ameaças relevantes:

- **Viés de expectativa e confirmação:** o participante tem conhecimento do objetivo do artefato e pode, conscientemente ou não, avaliar de forma mais favorável a intervenção.
- **Efeito de aprendizagem:** a segunda condição executada pode se beneficiar de familiaridade com o problema e com o fluxo de tarefas, reduzindo tempo e esforço percebido independentemente do artefato.
- **Efeito de instrumentação:** no baseline, o tempo é medido por cronômetro externo; na intervenção, parte do tempo é derivada de timestamps e eventos do sistema. Diferenças de método podem introduzir assimetria na comparação.

Como mitigação parcial, o protocolo define tarefas equivalentes e explicita, no Capítulo 6, quais medidas são suportadas por evidência automatizada e quais dependem de instrumento externo.

### 8.2.2 Variabilidade de LLM e não determinismo

Modelos de linguagem podem produzir variação de saída com o mesmo input. Essa variabilidade ameaça a estabilidade de resultados qualitativos e quantitativos (por exemplo, taxa de histórias aprovadas). O artefato busca mitigar esse risco por meio de governança por especificação e validações por regras e evidências persistidas, porém permanecem limitações:

- A variabilidade pode afetar a proporção de itens reprovados e a necessidade de refino.
- A execução depende de configurações de modelos e parâmetros que podem mudar com o tempo, ainda que a arquitetura de evidências preserve o resultado daquela execução.

## 8.3 Validade externa (external validity)

Validade externa diz respeito à generalização dos resultados para outros contextos.

As evidências reportadas referem-se a um recorte específico:

- um produto avaliado (`product_id = 1` na base analisada);
- um participante;
- um conjunto de tarefas alinhadas ao pipeline do artefato;
- um ambiente de execução controlado.

Portanto, não se pode concluir, a partir deste estudo, que a redução de carga percebida ou os efeitos de governança ocorrerão com a mesma magnitude em:

- equipes maiores, com papéis distintos de Product Owner/Scrum Master/Developers;
- domínios com restrições regulatórias mais rígidas ou artefatos não textuais;
- organizações com processos ágeis formalmente estabelecidos e ferramentas integradas.

A contribuição pretendida é mais restrita: demonstrar viabilidade e utilidade potencial do mecanismo de governança por especificação e da rastreabilidade operacional em contexto de equipe reduzida.

## 8.4 Validade de conclusão (conclusion validity)

Validade de conclusão trata de quão apropriadas são as inferências feitas a partir dos dados.

Nesta monografia, as conclusões são deliberadamente **descritivas**, e isso reduz o risco de conclusões indevidas. Ainda assim, existem limites:

- Não há base para inferência estatística (N=1, sem replicações independentes).
- A comparação temporal baseline vs intervenção é incompleta no conjunto de evidências atual, pois o baseline não está no DB e as durações registradas no DB caracterizam execução wall-clock do sistema (não isolando esforço humano); portanto, qualquer afirmação de “redução de tempo” deve ser evitada sem dados externos adicionais.

O Capítulo 6 adota explicitamente essas restrições ao sintetizar as respostas por questão de pesquisa (QP1–QP3) com base no que está disponível no repositório.

## 8.5 Confiabilidade e reprodutibilidade (reliability)

A confiabilidade relaciona-se à capacidade de reproduzir o procedimento e obter os mesmos resultados a partir do mesmo conjunto de dados.

O estudo possui pontos fortes nesse aspecto:

- Os números do Capítulo 6 são derivados de base SQLite específica, e os artefatos de extração registram metadados (timestamp, caminho do DB e hash de commit).
- A extração é realizada por script, gerando outputs em CSV/JSON e consultas reproduzíveis em `artifacts/`.

Por outro lado, a reprodutibilidade em sentido estrito possui limitações:

- Executar novamente o pipeline pode produzir resultados distintos por variação de LLM e por mudanças de dependências externas.
- Instrumentos externos (baseline e NASA-TLX) dependem de coleta manual; portanto, é necessário preservá-los como artefatos do estudo e descrevê-los com precisão no protocolo.

## 8.6 Limitações práticas do estudo

Além das ameaças de validade, há limitações operacionais relevantes para interpretação do artefato e do estudo.

1. **Instrumentação parcial de tempo humano vs. tempo de sistema:** embora existam durações registradas em eventos (`duration_seconds`), elas caracterizam execução wall-clock do processamento e não distinguem tempo de interação humana (edição/refino), o que limita a mensuração de eficiência.
2. **Necessidade de loop de refino:** a detecção de falhas por regras explícitas evidencia que o backlog inicial pode exigir refino para atingir critérios mínimos; o sistema registra o problema, mas não o elimina por si só.
3. **Escopo de avaliação:** o estudo prioriza evidência de rastreabilidade, governança e viabilidade do fluxo; não avalia efeitos de longo prazo (por exemplo, manutenção contínua de backlog ao longo de múltiplos sprints) nem efeitos organizacionais.

## 8.7 Encaminhamento

As ameaças e limitações descritas delimitam o alcance das conclusões desta monografia e apontam caminhos concretos de evolução: ampliar a amostra de participantes, instrumentar duração de eventos de forma consistente, e conduzir replicações independentes do protocolo em produtos/domínios diferentes. O Capítulo 9 consolida a conclusão do trabalho e apresenta propostas de trabalhos futuros alinhadas a essas lacunas.
