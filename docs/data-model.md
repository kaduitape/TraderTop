# Modelo de Dados — MT5 AI Scalper

## 1. Convenções gerais

- Todos os timestamps são armazenados em UTC (`DateTime(timezone=True)`,
  sempre gravados como `datetime.now(timezone.utc)`).
- Valores financeiros (preços, lotes, PnL, custos) usam `DECIMAL` com
  precisão explícita — nunca `FLOAT`/`DOUBLE`.
- Toda tabela principal tem `id` (chave primária técnica) e, quando
  aplicável, `uuid`/`external_id` como identificador público estável usado
  em logs e APIs.
- `created_at`/`updated_at` em todas as tabelas mutáveis.
- Índices compostos por `(symbol, timestamp)` em tabelas de mercado, e por
  `(strategy_id, created_at)` em sinais/ordens.
- Restrições de unicidade evitam candles e ticks duplicados (ver §3).

## 2. Catálogo completo de tabelas (arquitetura-alvo)

O catálogo completo previsto pelo prompt mestre é:

`users, roles, permissions, system_settings, broker_accounts, mt5_terminals,
symbols, symbol_specifications, candles, ticks, order_book_snapshots,
economic_events, strategies, strategy_versions, strategy_parameters,
signals, signal_features, model_experiments, model_versions, model_metrics,
model_predictions, backtest_runs, backtest_trades, walk_forward_runs,
monte_carlo_runs, paper_orders, live_orders, executions, positions,
position_events, risk_snapshots, daily_performance, system_events,
audit_logs, alerts, data_quality_events, model_drift_events.`

Implementar as ~36 tabelas de uma vez violaria a regra de "alterações
pequenas e verificáveis" e a diretriz de não avançar além da fundação na
Fase 1. Por isso, esta fase implementa **apenas as tabelas necessárias para
autenticação, configuração e auditoria** — a base sobre a qual tudo o mais
será construído:

| Tabela (Fase 1) | Propósito |
|---|---|
| `users` | Contas de usuário do dashboard/API |
| `roles` | Perfis: VIEWER, ANALYST, OPERATOR, RISK_MANAGER, ADMIN |
| `user_roles` | Associação N:N usuário↔papel |
| `system_settings` | Configurações chave-valor persistidas (ex.: modo atual do sistema) |
| `audit_logs` | Registro de ações sensíveis (login, alteração de configuração) |

Na Fase 2 foram adicionadas (migration 0002):

| Tabela (Fase 2) | Propósito |
|---|---|
| `symbols` | Símbolo negociável + especificação (digits, volume min/max/step, contract size) numa única tabela — ver §3 sobre a decisão de não separar `symbol_specifications` ainda |
| `candles` | OHLCV por símbolo/timeframe |
| `ticks` | Bid/ask/last por símbolo |

E na Fase 3 (migration 0003):

| Tabela (Fase 3) | Propósito |
|---|---|
| `data_quality_events` | Uma linha por ocorrência de qualidade detectada (`app/market/data_quality.py`): check, severidade, mensagem, símbolo/timeframe, timestamp |

E na Fase 10 (migration 0004):

| Tabela (Fase 10) | Propósito |
|---|---|
| `paper_trades` | Posições/trades de paper trading — no máximo uma `OPEN` por símbolo/timeframe/estratégia (imposto pelo `PaperTradeRepository`, não por constraint de banco) |

**Nota de nomenclatura**: o catálogo-alvo do prompt mestre usa
`paper_orders`; esta implementação usa `paper_trades` para manter
consistência com a nomenclatura `Trade` já usada em todo o motor de
backtest (Fases 5/7/9) — uma posição de paper trading resolvida é
conceitualmente o mesmo tipo de registro que um `Trade` de backtest,
apenas com origem ao vivo em vez de histórica.

E na Fase 11 (migration 0005):

| Tabela (Fase 11) | Propósito |
|---|---|
| `live_trades` | Uma linha por sinal processado pelo executor em conta demo — inclusive quando rejeitado pelo risco (`RISK_REJECTED`) ou pelo broker (`REJECTED`), nunca só as que viraram posição. `order_state` segue `app.execution.order_state.OrderState`; no máximo uma linha ativa (`POSITION_OPEN`/`RECONCILING`) por símbolo/timeframe/estratégia |

E na Fase 13 (migration 0006):

| Tabela (Fase 13) | Propósito |
|---|---|
| `drift_events` | Uma linha por ocorrência de drift `WARNING`/`CRITICAL` detectada (`app/monitoring/drift.py`) — um resultado `NONE` nunca é gravado. Cobre drift de feature (PSI), de calibração/desempenho (degradação de métrica) e de saúde do feed de dados |

As demais tabelas (estratégias, sinais formais, ordens reais em conta
real, backtests persistidos, risco agregado, alertas gerais etc.) serão
introduzidas incrementalmente nas fases correspondentes (Fase 14 em
diante), cada uma com sua própria migration Alembic, para manter os
critérios de aceite verificáveis por fase.

## 3. Regras específicas de dados de mercado (Fase 2/3)

- `candles`: unicidade em `(symbol_id, timeframe, open_time)` — implementado
  na migration 0002 e reforçado em `CandleRepository.bulk_upsert` (dedup
  antes de inserir, não depende só da constraint do banco).
- `ticks`: unicidade em `(symbol_id, timestamp, bid, ask)` — o MetaTrader5
  não fornece um identificador de sequência por tick de forma confiável
  entre corretoras, então esta é a chave pragmática usada (ver
  `TickRepository`). Revisitar apenas se isso mudar.
- `symbols` concentra identidade e especificação numa única tabela (sem
  `symbol_specifications` separada) — decisão deliberada de não dividir em
  duas tabelas sem uma necessidade concreta (ex.: histórico de mudança de
  especificação ao longo do tempo), reavaliável quando essa necessidade
  aparecer.
- Retenção: **automática** desde a Fase 20 — ver seção 3.1. Candles **não**
  são expurgadas (volume bem menor e são a base do backtesting por candle,
  Fase 5).
- Qualidade de dados: `app/market/data_quality.py` detecta timestamps
  duplicados, buracos, OHLC inconsistente, preço/volume inválido, ticks
  fora de ordem, spread absurdo, atraso do feed e timestamp no futuro
  (indício de timezone incorreto). Cada ocorrência vira uma linha em
  `data_quality_events`; a nota agregada (`compute_score`) nunca substitui
  as ocorrências individuais. Divergência entre candles e ticks fica fora
  do escopo por ora (ver docstring do módulo).

## 3.1 Retenção automática (Fase 20)

Duas tabelas cresciam sem teto. `ticks` tinha purga, mas só manual
(`data purge-ticks`); `data_quality_events` não tinha nenhuma. Política que
depende de alguém lembrar não é política.

| Tabela | Janela | Observação |
|---|---|---|
| `ticks` | `TICK_RETENTION_DAYS` = **7** | Era 30. É de longe a maior tabela. |
| `data_quality_events` (INFO/WARNING e severidades desconhecidas) | `QUALITY_EVENT_RETENTION_DAYS` = **14** | |
| `data_quality_events` (CRITICAL) | `QUALITY_EVENT_CRITICAL_RETENTION_DAYS` = **90** | É o que se procura depois de um incidente. |
| `candles`, `audit_log`, trades, setups | — | Mantidas indefinidamente, de propósito. |

**O que a janela de ticks não afeta.** A análise ao vivo lê no máximo 1
hora de ticks (`tick_window_seconds`, limitado a 3600 no painel), a
FotoAnálise lê 1 tick, e o backtest por candle não usa ticks. O que a
janela limita é o **backtest por tick**, que precisa da sequência real para
simular fills. Subir o valor é uma variável de ambiente; o custo é linear
em disco.

**Por que não afinar em vez de apagar.** Guardar 1 tick por segundo depois
de N dias foi rejeitado: o backtest por tick existe justamente para simular
preenchimento contra a sequência real, e rodado sobre ticks afinados ele
continua produzindo número — otimista, sem avisar. Backtest ausente é
problema visível; backtest que mente não é.

**Onde roda.** Thread de manutenção no lifespan da API
(`app/maintenance/scheduler.py`). Na VPS sobem apenas `db` e `app`:
`mt5-worker` está atrás do profile `local-mt5` e não sobe lá, e não há cron
no arranjo. O horário da última execução fica em `system_settings`
(`retention_last_run_utc`), então um container que reinicia não dispara uma
purga por reinício. `RETENTION_ENABLED=false` desliga; `APP_ENV=test`
desliga sozinho, porque uma thread apagando linhas em paralelo com a suíte
produziria falha intermitente.

**Em lotes, com commit por lote.** Um `DELETE` único de milhões de linhas no
InnoDB segura uma transação gigante: o undo log cresce, a tabela de ticks
trava para a coleta que tenta escrever nela, e um timeout desfaz tudo — a
execução seguinte encara a mesma montanha e falha igual.
`RETENTION_MAX_CHUNKS_PER_RUN` limita o trabalho por execução (padrão 2
milhões de linhas); o resto fica para a próxima.

**`DELETE` não devolve espaço ao disco.** O InnoDB libera páginas para reuso
dentro do mesmo `.ibd` — depois da primeira purga grande, `df` não se mexe.
Para encolher o arquivo é preciso `OPTIMIZE TABLE`
(`data retention --optimize`), que **reconstrói a tabela e bloqueia
escrita** enquanto roda. Faz sentido uma vez, com o mercado fechado; não
toda madrugada. Em regime, as páginas liberadas são reaproveitadas e o
arquivo para de crescer.

## 4. Rastreabilidade obrigatória em sinais e ordens (a implementar na Fase 6+)

Cada sinal e ordem deve registrar, no mínimo: identificador único, horário
do sinal, horário de envio, horário de confirmação, estratégia responsável,
versão do modelo, features utilizadas, probabilidade prevista, motivo da
entrada, stop-loss, take-profit, spread, slippage, risco monetário,
resultado, motivo de encerramento, resposta completa do MetaTrader, logs de
falha/rejeição/reconexão — conforme prompt mestre, seção 2.
