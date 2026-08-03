# CAPÍTULO 7 — Discussão

Este capítulo discute os resultados apresentados no Capítulo 6 à luz da fundamentação teórica e dos trabalhos relacionados (Capítulo 2). A discussão é conduzida com foco em (i) evidências quantitativas disponíveis no repositório e (ii) implicações e trade-offs observáveis no desenho do artefato. Por tratar-se de avaliação empírica exploratória (intra-sujeito com um participante), as conclusões são apresentadas como interpretações descritivas, sem pretensão de generalização estatística.

## 7.1 Síntese interpretativa dos achados

Os resultados do Capítulo 6 sustentam três pontos principais.

1. **Governança por especificação aumenta transparência e auditabilidade:** no conjunto analisado de histórias refinadas canônicas (`product_id = 1`, `is_refined = 1` e `is_superseded = 0`), 8/8 histórias possuem evidência JSON de validação persistida; e 4/8 foram aceitas/pinadas à especificação aprovada (`accepted_spec_version_id`). Isso configura uma trilha reprodutível de “passou/falhou” por item, com causa de falha registrada.
2. **Gates de validação explicitam falhas de forma objetiva e auditável:** entre as histórias refinadas canônicas, 4/8 foram reprovadas com evidências persistidas; as causas concentraram-se em `FORBIDDEN_CAPABILITY` (3) e `RULE_LLM_SPEC_VALIDATION` (1).
3. **Redução de carga de trabalho percebida é compatível com o objetivo do artefato, mas eficiência temporal permanece parcialmente inconclusiva:** o instrumento NASA-TLX registrado em `artifacts/nasa_tlx_raw_5d_form.csv` (run_01, `product_id = 1`) indica redução descritiva da média RAW de 81 (baseline) para 27 (intervenção). Adicionalmente, o baseline manual não está persistido no DB e as durações registradas (`workflow_events.duration_seconds`) caracterizam execução wall-clock do sistema, não isolando esforço humano.

Em conjunto, os achados reforçam que o artefato não deve ser interpretado como um mecanismo de “automação total” do planejamento, mas como uma infraestrutura de coordenação e governança que: (i) explicita regras, (ii) registra evidências e (iii) oferece pontos de inspeção **auditáveis** (passa/falha) em um pipeline predominantemente não determinístico.

## 7.2 Diálogo com os trabalhos relacionados

A comparação com os trabalhos relacionados (Capítulo 2) deve ser feita com base no problema-alvo deste TCC: reduzir fricção operacional e carga de coordenação no planejamento ágil em contexto de equipe reduzida, sem perder rastreabilidade e critérios mínimos de qualidade.

### 7.2.1 Multiagentes com LLM e foco de engenharia

He et al. (2024) organizam o espaço de sistemas multiagentes com LLM em engenharia de software, enfatizando padrões arquiteturais, orquestração e integração com ferramentas. O artefato deste TCC converge com esse enquadramento ao adotar orquestração centralizada por FSM, agentes especialistas e persistência de estado em base relacional. Entretanto, o resultado distintivo aqui não é a diversidade de agentes em si, mas a combinação entre multiagentes e **mecanismos explícitos de governança por especificação** que tornam decisões do fluxo auditáveis por meio de evidências persistidas.

Em outras palavras, enquanto revisões e frameworks tendem a descrever capacidades, este trabalho materializa um recorte operacional: pipeline de planejamento com gates de validação **passa/falha** que estabelecem um “contrato mínimo” de completude para artefatos textuais (por exemplo, critérios de aceitação).

### 7.2.2 Simulação de papéis e processos ágeis

Cinkusz e Chudziak (2025) exploram a simulação de papéis e cerimônias ágeis por agentes, frequentemente em contextos de processo mais amplo e com ênfase em emulação de ritos e dinâmicas de equipe. O artefato aqui implementado se diferencia por não tentar substituir as funções humanas do Scrum, e sim reduzir custo de coordenação e retrabalho associado à consistência entre artefatos.

Os resultados (reprovações registradas por regras explícitas, como `FORBIDDEN_CAPABILITY`) ilustram um ponto prático: a automação do “rito” não garante qualidade mínima do conteúdo. Assim, um ganho central do artefato é deslocar o foco da simulação para a **verificação auditável de propriedades mínimas**, preservando evidência do porquê um item não deve avançar no fluxo.

### 7.2.3 Emulação de ciclos ágeis orientados a código

Nguyen et al. (2024) enfatizam ciclos ágeis com agentes orientados à geração/validação de código e benchmarks associados. O recorte deste TCC é anterior ao código: a produção e governança de artefatos de planejamento (visão, backlog, roadmap, histórias, sprint) com rastreabilidade a uma especificação versionada.

Nesse sentido, o que este trabalho adiciona ao conjunto relacionado não é uma técnica de geração de software, mas um mecanismo de **controle de viabilidade e rastreabilidade** para o planejamento. A evidência de validação por história e a pinagem à especificação aprovada (quando ocorre) são características do domínio de gestão de requisitos e planejamento, em vez de métricas de correção de código.

## 7.3 Interpretação por questão de pesquisa (QP1–QP3) e implicações

### 7.3.1 QP1 — Carga de trabalho percebida e eficiência

A redução descritiva do NASA-TLX (média RAW de 81 para 27), registrada em `artifacts/nasa_tlx_raw_5d_form.csv` (run_01, `product_id = 1`), é compatível com a QP1 no recorte de carga de trabalho percebida, no sentido de que um pipeline assistido por agentes pode externalizar parte do trabalho operacional de estruturação de artefatos.

Todavia, o mesmo conjunto de evidências não permite concluir redução temporal em comparação ao baseline, pois:

- os tempos baseline (manual) não estão persistidos no banco;
- as durações registradas em `workflow_events.duration_seconds` (incluindo `SPRINT_PLAN_SAVED`) caracterizam execução wall-clock do sistema e não distinguem tempo de interação humana (edição/refino);
- deltas wall-clock entre marcos de timestamps e dwell times de estados caracterizam intervalos, mas podem incluir latências e pausas não relacionadas ao esforço do participante.

Assim, a contribuição para a QP1 deve ser interpretada como suporte empírico exploratório de **redução de carga percebida**, mantendo a comparação temporal como lacuna a ser preenchida por instrumentação ou coleta externa robusta.

### 7.3.2 QP2 — Qualidade/completude estrutural

Os resultados mostram que a plataforma não “força” artificialmente um backlog a parecer válido: 4/8 histórias refinadas canônicas foram reprovadas por regras explícitas, com evidências persistidas (predominantemente `FORBIDDEN_CAPABILITY`). Esse achado tem duas implicações.

1. **Qualidade como gate, não como suposição:** o backlog inicial pode ser produzido rapidamente, mas a plataforma explicita onde a qualidade mínima não foi atingida, evitando que itens inadequados ou desalinhados sejam usados como entrada de planejamento sem inspeção.
2. **Trade-off inevitável: governança introduz refino:** o gate reduz risco de itens inadequados avançarem, mas cria a necessidade de um loop de refino para corrigir reprovados. Na prática, esse trade-off é desejável quando o objetivo é reduzir ambiguidade e manter rastreabilidade, ainda que ao custo de esforço adicional de refinamento.

### 7.3.3 QP3 — Rastreabilidade e governança por especificação

A QP3 é respondida positivamente no sentido operacional: há uma versão de especificação aprovada, há autoridade compilada, e as histórias refinadas canônicas carregam evidência de validação e, quando aceitas, pinagem à especificação (4/8 com `accepted_spec_version_id`). Mesmo em casos de falha, a plataforma registra a causa de forma explícita, preservando rastreabilidade negativa (isto é, por que algo não foi aceito).

Essa rastreabilidade é particularmente relevante em contexto de equipe reduzida: ao invés de depender de memória informal do planejador, o sistema preserva um histórico verificável de regras e decisões que pode ser inspecionado posteriormente.

## 7.4 Contribuições do artefato e limites do que foi demonstrado

Com base nos capítulos anteriores, as principais contribuições podem ser sintetizadas como:

- **Pipeline de planejamento com governança por especificação:** separação explícita entre geração não determinística e aceitação auditável (passa/falha) por regras e evidências persistidas.
- **Rastreabilidade operacional:** capacidade de relacionar histórias a uma especificação aprovada e registrar causas de falha por item.
- **Redução descritiva de carga percebida (instrumento):** evidência exploratória via NASA-TLX RAW.

Ao mesmo tempo, a avaliação conduzida demonstra limites claros:

- **Escopo empírico restrito:** um participante e um recorte de produto limitam generalização.
- **Instrumentação parcial de tempo:** sem baseline persistido e sem instrumentação que isole tempo humano (edição/refino) do tempo wall-clock de execução do sistema, a evidência temporal se restringe a medidas de execução na intervenção.
- **Qualidade final depende de refino:** o gate detecta falhas, mas não as corrige automaticamente; o impacto líquido na qualidade do backlog depende do loop de refinamento aplicado.

## 7.5 Síntese e encaminhamento

A discussão reforça que a proposta central do artefato é substituir “confiança implícita” por “evidência explícita” em um pipeline de planejamento assistido por agentes: quando um artefato passa, há pinagem; quando falha, há motivo registrado. Os resultados exploratórios indicam redução de carga percebida e aumento de auditabilidade, ao custo de introduzir (de forma deliberada) um ponto de inspeção **auditável** (passa/falha) que pode demandar refino adicional.

O próximo capítulo aprofunda, de forma sistemática, as ameaças à validade e limitações do estudo, explicitando fontes de viés e lacunas de instrumentação.
