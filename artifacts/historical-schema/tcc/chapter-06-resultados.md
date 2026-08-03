# CAPÍTULO 6 — Resultados

Este capítulo apresenta os resultados obtidos na avaliação empírica exploratória do artefato. Os valores numéricos reportados são derivados de evidências extraídas do banco de dados e de artefatos de extração (CSV/JSON) gerados por script, conforme o protocolo definido no Capítulo 5. Quando um dado não está disponível no banco ou não é rastreável no repositório, isso é indicado explicitamente, evitando inferências não suportadas.

## 6.1 Caracterização da execução e do conjunto de evidências

Os resultados quantitativos deste capítulo são extraídos de uma base SQLite específica utilizada na avaliação, com metadados de extração registrados em artefato JSON.

**Fonte de verdade (evidência):**

- Base: `db/spec_authority_dev.db`
- Timestamp de extração: 2026-02-19T14:00:40.161314+00:00
- Commit (hash curto): b1318f5b2005

**Produto avaliado (evidência):**

- `product_id = 1`
- Nome: *P&ID Review & Extraction Platform*
- Especificação: 1 versão registrada e aprovada (`spec_version_id = 1`, status `approved`)

## 6.2 Resultados por questão de pesquisa (QP1–QP3)

### 6.2.1 QP2 — Qualidade e completude mínima das histórias (evidência de validação)

Esta dimensão reporta resultados de validação persistidos no banco por história. A evidência é composta por:

- campo `validation_evidence` (JSON) em `user_stories`, contendo resultado da validação e lista de falhas;
- campo `accepted_spec_version_id` em `user_stories`, preenchido quando a história passa na validação e é pinada à versão aprovada.

**Resumo (evidência):**

| Métrica | Valor | Interpretação |
|---|---:|---|
| Histórias no produto | 23 | Total de histórias persistidas no produto avaliado |
| Histórias refinadas canônicas | 8 | Histórias com `is_refined = 1` e `is_superseded = 0` |
| Histórias refinadas com evidência de validação | 8 (100,0%) | Evidência JSON persistida para cada história refinada canônica |
| Histórias refinadas aceitas/pinadas (`accepted_spec_version_id` não nulo) | 4 (50,0%) | Histórias refinadas que passaram na validação e foram pinadas |
| Histórias refinadas reprovadas (não pinadas) | 4 (50,0%) | Histórias refinadas com resultado `passed = false` |

**Motivos de reprovação (histórias refinadas; evidência):**

| Tipo/código | Ocorrências | Observação |
|---|---:|---|
| `FORBIDDEN_CAPABILITY` | 3 | falha de alinhamento registrada em `alignment_failures` |
| `RULE_LLM_SPEC_VALIDATION` | 1 | falha registrada em `failures` (validação por LLM) |

Esses resultados indicam que a camada de governança registra aceitação (pinagem) e reprovação (motivo) de forma auditável no escopo avaliado (histórias refinadas canônicas).

### 6.2.2 QP3 — Rastreabilidade e pinagem à especificação aprovada

O objetivo desta dimensão é demonstrar que a plataforma preserva rastreabilidade entre a especificação aprovada e histórias derivadas. No conjunto analisado, observa-se:

- existência de uma versão de especificação aprovada (`spec_version_id = 1`);
- existência de autoridade compilada (1 registro em `compiled_spec_authority`);
- decisões de aceite registradas (1 aceite, 0 rejeições);
- trilha por história (escopo: refinadas canônicas): evidência JSON de validação em 100% das histórias refinadas e pinagem em 50,0%.

Em termos metodológicos, mesmo as histórias reprovadas permanecem rastreáveis, pois a causa da reprovação é registrada no `validation_evidence` e pode ser utilizada como insumo para refino.

### 6.2.3 QP1 — Eficiência do fluxo (evidências disponíveis)

Os resultados de eficiência dependem do que está instrumentado como evento ou do que é derivável de timestamps. No conjunto de evidências extraído, há eventos de workflow com `duration_seconds` (por exemplo, `SPRINT_PLAN_SAVED`) e eventos de permanência por estado da FSM (`FSM_STATE_DWELL`). Essas medidas representam *wall-clock* do sistema, podendo incluir processamento de LLM, latência externa e tempo de interação humana.

**Resumo de eventos (evidência no banco):**

| Evento | Contagem | Duração média (s) | Total (s) |
|---|---:|---:|---:|
| `VISION_SAVED` | 1 | 25,66 | 25,66 |
| `SPEC_COMPILED` | 1 | 25,44 | 25,44 |
| `BACKLOG_SAVED` | 1 | 0,12 | 0,12 |
| `ROADMAP_SAVED` | 1 | 0,02 | 0,02 |
| `STORIES_SAVED` | 1 | 0,05 | 0,05 |
| `SPRINT_PLAN_SAVED` | 1 | 0,10 | 0,10 |
| `FSM_STATE_DWELL` | 12 | 119,51 | 1434,10 |

**Tempo de permanência por estado (FSM; evidência no banco):**

| Estado | Saídas | Dwell médio (s) |
|---|---:|---:|
| `ROUTING_MODE` | 1 | 130,06 |
| `VISION_INTERVIEW` | 1 | 70,17 |
| `VISION_REVIEW` | 1 | 169,24 |
| `VISION_PERSISTENCE` | 1 | 49,82 |
| `BACKLOG_INTERVIEW` | 1 | 130,96 |
| `BACKLOG_REVIEW` | 1 | 335,33 |
| `BACKLOG_PERSISTENCE` | 1 | 93,01 |
| `ROADMAP_INTERVIEW` | 1 | 169,35 |
| `ROADMAP_PERSISTENCE` | 1 | 62,41 |
| `STORY_REVIEW` | 1 | 74,64 |
| `STORY_PERSISTENCE` | 1 | 65,88 |
| `SPRINT_DRAFT` | 1 | 83,22 |

### 6.2.4 QP1 — Carga de trabalho percebida (NASA-TLX)

O protocolo prevê NASA-TLX RAW com cinco dimensões (Demanda Mental, Demanda Temporal, Desempenho, Esforço e Frustração). As respostas, por definição do protocolo, são coletadas em instrumento externo (formulário/planilha), registradas como artefato do estudo e **não** são persistidas no banco.

Para esta execução, os valores foram registrados em `artifacts/nasa_tlx_raw_5d_form.csv` (run_01), em escala 0–100, adotando a convenção **menor = melhor** também para a dimensão de Desempenho (isto é, 0 indica desempenho percebido excelente; 100 indica desempenho percebido ruim). No CSV, a coleta utilizada neste capítulo está associada a `product_id = 1`.

| Dimensão NASA-TLX | Baseline | Intervenção |
|---|---:|---:|
| Demanda Mental | 90 | 50 |
| Demanda Temporal | 95 | 20 |
| Desempenho | 80 | 15 |
| Esforço | 90 | 20 |
| Frustração | 50 | 30 |

**Resumo (evidência do instrumento):**

| Medida | Baseline | Intervenção |
|---|---:|---:|
| Média RAW (5 dimensões) | 81 | 27 |

### 6.2.5 QP3 — Evidências de execução automatizada (*smoke runs*)

Além das métricas por produto, foram extraídas métricas agregadas de execuções automatizadas (*smoke runs*) a partir de `artifacts/smoke_runs.jsonl`, quando disponível. Observação: esse conjunto é **agregado** (inclui múltiplos cenários/produtos) e, portanto, não é filtrado apenas para o `product_id = 1`; os números abaixo são reportados como evidência complementar do comportamento do pipeline. No conjunto analisado:

| Métrica | Valor |
|---|---:|
| Execuções totais | 24 |
| Pipeline executado | 11 |
| Rejeições por alinhamento | 5 |
| Bloqueios por aceite/gate | 8 |
| Execuções com contrato aprovado | 3 |
| Match de `spec_version_id` | 11 |
| Mismatch de `spec_version_id` | 0 |

Esses números caracterizam, de forma agregada, o comportamento do pipeline sob execução automatizada. No conjunto analisado, o indicador `spec_version_id_match` não registrou divergências nas execuções em que o pipeline rodou.

## 6.3 Observações qualitativas (não métricas)

As observações desta seção não são métricas automatizadas e não substituem os resultados quantitativos. Elas servem para contextualizar os achados:

- As reprovações observadas nas histórias refinadas concentraram-se em falhas de alinhamento registradas como `FORBIDDEN_CAPABILITY` e em um caso de falha por `RULE_LLM_SPEC_VALIDATION`.
- A presença de evidência de validação em 100% das histórias refinadas canônicas reduz ambiguidade na auditoria: para cada história, há registro explícito de “passou/falhou” e, quando falha, do motivo.
- Há `duration_seconds` instrumentado em eventos relevantes, mas as medidas são *wall-clock* e podem incluir interação humana e latências externas; não representam esforço humano isolado.

## 6.4 Síntese por questão de pesquisa (QP1–QP3)

### QP1 (carga de trabalho)

**Evidência disponível:** há respostas NASA-TLX (RAW, 5 dimensões) registradas em `artifacts/nasa_tlx_raw_5d_form.csv` (run_01, `product_id = 1`), com redução descritiva da média de 81 (baseline) para 27 (intervenção). Para eficiência temporal, há evidência por eventos de workflow e dwell por estado da FSM, porém sem tempos baseline manual persistidos para comparação direta.

### QP2 (qualidade e completude)

**Evidência disponível:** no conjunto de histórias refinadas canônicas (`product_id = 1`), 8/8 histórias possuem evidência de validação persistida; 4/8 foram aceitas/pinadas; e 4/8 foram reprovadas com evidências registradas (principalmente `FORBIDDEN_CAPABILITY` e `RULE_LLM_SPEC_VALIDATION`).

### QP3 (viabilidade operacional)

**Evidência disponível:** há especificação aprovada (`spec_version_id = 1`), autoridade compilada, decisão de aceite e trilhas por história (evidência de validação e pinagem no escopo refinado canônico). Como evidência complementar (não filtrada por `product_id`), as métricas agregadas de *smoke runs* indicam execução repetida do pipeline e consistência de `spec_version_id` nas execuções em que o pipeline rodou (sem mismatch registrado).
