# CAPÍTULO 9 — Conclusão e trabalhos futuros

Este capítulo apresenta as conclusões da monografia, retomando o problema e os objetivos definidos no Capítulo 1 e sintetizando os achados empíricos reportados no Capítulo 6, interpretados no Capítulo 7 e delimitados pelas ameaças à validade do Capítulo 8. As conclusões são mantidas no nível **exploratório** compatível com o desenho intra-sujeito (N=1) e com as evidências auditáveis disponíveis no repositório.

## 9.1 Conclusões por objetivo e por questão de pesquisa (QP1–QP3)

### 9.1.1 Atendimento ao objetivo geral

O objetivo geral foi projetar, implementar e avaliar empiricamente uma plataforma de gestão ágil autônoma baseada em sistema multiagente, orquestrando um fluxo inspirado no Scrum e incorporando governança por especificação para mitigar sobrecarga metodológica em equipes pequenas.

A implementação descrita no Capítulo 4 e os resultados do Capítulo 6 indicam que o artefato é **viável** como Prova de Conceito: o fluxo ponta a ponta é executável e produz artefatos persistidos em base relacional, com trilhas de evidência de validação e rastreabilidade para inspeção posterior.

### 9.1.2 Síntese por questão de pesquisa (QP1–QP3)

- **QP1 (carga de trabalho e eficiência):** o instrumento NASA-TLX (RAW, 5 dimensões) registrado em `artifacts/nasa_tlx_raw_5d_form.csv` (run_01, `product_id = 1`) indica redução descritiva da média de 81 (baseline) para 27 (intervenção). Além disso, os tempos baseline (manual) não estão persistidos no DB e as durações registradas (`workflow_events.duration_seconds`) caracterizam execução wall-clock do sistema, não isolando esforço humano; logo, não se sustenta conclusão sobre redução de tempo a partir do DB.

- **QP2 (qualidade/completude estrutural dos artefatos):** no conjunto de histórias refinadas canônicas (`product_id = 1`), 8/8 histórias possuem evidência de validação persistida; 4/8 foram aceitas/pinadas; e 4/8 foram reprovadas com evidências registradas (principalmente `FORBIDDEN_CAPABILITY` e `RULE_LLM_SPEC_VALIDATION`). Isso indica que o artefato aplica critérios auditáveis de “passou/falhou” e registra falhas de forma rastreável. Ao mesmo tempo, os resultados mostram que a qualidade final do backlog depende de um loop de refino para itens reprovados, e que a validação não substitui julgamento humano sobre valor e adequação semântica.

- **QP3 (rastreabilidade e governança por especificação):** os resultados suportam QP3 no sentido operacional: existe versão de especificação aprovada, autoridade compilada, decisão de aceite e trilha por história refinada canônica (evidência de validação em 100% e pinagem em 4/8). O artefato produz rastreabilidade tanto em casos de aceite (pinagem) quanto em casos de falha (registro explícito do motivo), reduzindo dependência de memória informal do operador.

## 9.2 Contribuições

As contribuições desta monografia podem ser agrupadas em duas dimensões: artefato tecnológico e evidência/conhecimento.

1. **Artefato tecnológico (prova de conceito):**

- implementação de um pipeline de planejamento inspirado no Scrum (visão, especificação, backlog, histórias e planejamento) com orquestração explícita por máquina de estados;
- separação operacional entre geração não determinística (LLM) e aceitação auditável (passa/falha) baseada em regras/contratos e evidências persistidas por item;
- versionamento e governança por especificação, com mecanismos de compilação, aceite e pinagem de histórias à versão aprovada.

2. **Evidência e conhecimento (exploratórios):**

- demonstração empírica de que é possível tornar auditável um pipeline de planejamento assistido por agentes, registrando evidência de validação por história refinada e rastreabilidade associada;
- evidência exploratória de redução de carga de trabalho percebida (NASA-TLX), registrada como artefato externo do estudo;
- explicitação de trade-offs: a governança aumenta transparência e controle, mas introduz a necessidade de refino para itens reprovados, e a instrumentação temporal precisa ser planejada para suportar comparações robustas.

## 9.3 Trabalhos futuros

Os trabalhos futuros abaixo são propostos para ampliar validade, generalização e capacidade de mensuração do artefato.

1. **Instrumentação temporal consistente no próprio sistema:**

- registrar métricas de tempo de interação humana (edição/refino) e separá-las, quando possível, do tempo de processamento do sistema (por exemplo, chamadas a LLM e latência de rede);

2. **Coleta de baseline e instrumentos como artefatos de pesquisa versionados:**

- manter registros baseline (tempos por tarefa e observações) e instrumentos (NASA-TLX) como artefatos versionados do estudo, com metadados de execução, para fortalecer auditoria e replicação.

3. **Avaliação com múltiplos participantes e replicações independentes:**

- conduzir o protocolo com N participantes (com diferentes níveis de experiência), permitindo estimar variabilidade e reduzir viés do pesquisador;
- executar replicações independentes em produtos e domínios distintos, observando estabilidade da taxa de aceitação, tipos de falhas e necessidade de refino.

4. **Loop de refino sistemático para itens reprovados:**

- implementar um ciclo explícito de refino para histórias reprovadas (por exemplo, geração de proposta de critérios de aceitação e revalidação), preservando evidência de antes/depois e mensurando o impacto na taxa de aceitação.

5. **Ampliação de métricas de qualidade e rastreabilidade:**

- incorporar rubricas complementares (humanas) para avaliar clareza e valor das histórias;
- incluir métricas adicionais de consistência entre artefatos (por exemplo, cobertura de regras da especificação no backlog) e de evolução longitudinal (múltiplos sprints).

6. **Análise de sensibilidade a modelos e configurações:**

- avaliar a robustez do pipeline a diferentes modelos de linguagem e configurações, mantendo a governança como mecanismo de contenção de variabilidade e registrando evidência de divergência quando ocorrer.

Em síntese, este trabalho mostra que a combinação entre multiagentes e governança por especificação é uma estratégia viável para tornar mais auditável o planejamento assistido por LLM em contexto de equipe reduzida. A evolução natural é fortalecer a instrumentação, ampliar a avaliação empírica e consolidar um loop de refino que aumente a proporção de artefatos aceitos sem perder rastreabilidade.
