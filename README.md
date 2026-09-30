# MT5 AI Scalper

Plataforma modular para pesquisa, simulação e execução controlada de
estratégias de scalping no MetaTrader 5. Construída em fases incrementais —
ver `docs/development-phases.md` para o roadmap completo e o status atual.

**Estado atual: Fases 1 (Fundação), 2 (Conector MT5 somente leitura), 3
(Qualidade e armazenamento de dados), 4 (Indicadores e regimes), 5
(Backtester por candles), 6 (Estratégias base) e 7 (Backtest por tick)
concluídas.** Há conexão de leitura com o MetaTrader (candles, ticks,
símbolos, conta, posições, ordens/histórico), coleta incremental,
checagens de qualidade, retenção de ticks, cálculo de indicadores/regime
de mercado e dois motores de backtest — por candle e por tick (fills
realistas com latência, spread real e rejeição) — com 5 estratégias
(baseline + tendência com pullback + rompimento + retorno à média +
momentum) — mas nenhum envio de ordem real, execução ao vivo ou modelo de
IA foi implementado ainda. O modo do sistema (`SYSTEM_MODE`) inicia e
permanece em `DISABLED`.

## Avisos importantes (leia antes de usar em qualquer fase futura)

- Desempenho passado não garante resultado futuro.
- Conta demo não reproduz perfeitamente a execução em conta real.
- Spread, latência e slippage podem inviabilizar uma estratégia de scalping
  mesmo que ela pareça lucrativa em backtest.
- O feed de dados depende inteiramente da corretora usada.
- Volume e livro de ofertas fornecidos pelo MetaTrader podem ser
  incompletos (nem sempre representam o mercado inteiro).
- Modelos de machine learning degradam com o tempo e precisam de
  monitoramento contínuo (drift).
- O modo de operação real (`REAL_ENABLED`) é desabilitado por padrão e exige
  liberação manual explícita — nunca é ativado automaticamente.

## Requisitos

- Windows 10/11 (o conector MetaTrader 5 exige o terminal MT5 instalado e
  autenticado localmente para operações reais; os comandos `mt5 *`/`collect
  *` falham com mensagem clara se não houver terminal configurado).
- Python 3.12 a 3.14. **Nesta instalação, o ambiente usa Python 3.14.6**
  (única versão disponível na máquina no momento da Fase 0/1) — ver
  `docs/assumptions.md` seção 2.1. `MetaTrader5`, `numpy` e `pandas` (checados
  nas Fases 2 e 4) publicam wheel para 3.14 normalmente; o risco permanece
  em aberto apenas para `xgboost`/`lightgbm`, a checar na Fase 8.
- MySQL 8 (opcional nesta fase — ver seção "Banco de dados" abaixo).

## Instalação

```powershell
cd mt5_ai_scalper
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev,mt5]"
copy .env.example .env
```

Edite `.env` e preencha, no mínimo, `APP_SECRET_KEY` (gere um valor com
`python -c "import secrets; print(secrets.token_hex(32))"`).

O extra `mt5` (pacote `MetaTrader5`) só instala no Windows — é a única
camada do sistema que precisa dele (`app/mt5/connection.py`), e só existe
wheel para Windows porque o pacote fala com o terminal via DLL/named
pipe. Rodando em outro SO (ou dentro do container Docker, abaixo), omita
esse extra — os comandos que não dependem de MT5 (API, dashboard,
backtest, ML, monitor, preflight) continuam funcionando normalmente.

## Rodando com Docker

Contém a API/dashboard + MySQL 8, prontos com `docker compose`:

```powershell
copy .env.example .env
# preencha APP_SECRET_KEY, DB_ROOT_PASSWORD, DB_NAME, DB_USER, DB_PASSWORD

docker compose up -d --build
docker compose logs -f app   # migrations -> preflight check -> uvicorn
docker compose ps            # app e db devem aparecer como healthy
```

Abre em `http://localhost:8000`. O entrypoint do container já roda
`alembic upgrade head` e `python -m app.cli preflight check` antes de
subir o servidor. O MySQL é publicado somente em `127.0.0.1` para que o
worker MT5 do próprio Windows consiga compartilhar o banco sem expô-lo na
rede local. Se a porta estiver ocupada, ajuste `DB_EXPOSED_PORT` no `.env`
e use o mesmo valor em `DB_PORT` no ambiente do worker Windows.

O Compose possui healthchecks reais para MySQL e API. O app só inicia
depois que o banco responde, e uma falha de migrations, banco ou diretórios
no preflight interrompe a inicialização em vez de deixar um container
aparentemente pronto.

**Limitação importante**: o pacote `MetaTrader5` só existe para Windows,
então **nada dentro do container consegue conectar a um terminal MT5
real**. Comandos como `mt5 check`, `collect candles`/`collect ticks`,
`paper run` e `demo run` falham dentro do container com uma mensagem
clara (nunca uma resposta inventada) — rode-os no host Windows (com
`pip install -e ".[mt5]"` e um terminal MT5 instalado/autenticado)
apontando para o MySQL do compose. Dentro do container funcionam
normalmente: a API, o dashboard, e os comandos de CLI que não dependem
de conexão MT5 (`preflight check`, `backtest *`, `ml *`, `monitor *`,
`quality check`, `mode show/set`). Ver `docs/runbook.md` seção 1b para o
procedimento completo.

```powershell
docker compose exec app python -m app.cli preflight check
docker compose down       # para os containers, mantem os dados do MySQL
docker compose down -v    # para e apaga os dados do MySQL (irreversivel)
```

## Banco de dados

Esta fase não exige um MySQL real rodando. Quando `APP_ENV=test`, a
aplicação usa automaticamente SQLite em memória (ver
`app/core/config.py::Settings.database_url` e `docs/assumptions.md` seção
2.2) — é assim que a suíte de testes valida a camada de banco sem depender
de infraestrutura externa.

Quando você tiver um MySQL 8 acessível, preencha `DB_HOST`, `DB_PORT`,
`DB_USER`, `DB_PASSWORD`, `DB_NAME` no `.env` e rode as migrations:

```powershell
alembic upgrade head
```

## Conector MetaTrader 5

O dashboard possui uma central em `/dashboard/mt5` que substitui a coleta
manual. Nela é possível selecionar os ativos, ativar/pausar a automação,
solicitar atualização imediata, testar a conexão e acompanhar terminal,
corretora, conta, heartbeat, candles e ticks.

Como a biblioteca oficial `MetaTrader5` só funciona no Windows e o dashboard
normalmente roda no Docker Linux, a integração usa um worker Windows leve.
Baixe o instalador de um clique na própria central MT5. Ele cria um ambiente
isolado, instala o conector oficial e registra a tarefa
`AI Trader PRO - Conector MT5` para iniciar automaticamente no login.

O worker usa a sessão já autenticada do terminal; `MT5_LOGIN`,
`MT5_PASSWORD` e `MT5_SERVER` são opcionais. Se preenchidos, permanecem
somente no `.env` do host Windows — nunca são enviados ao navegador nem
persistidos em `system_settings`.

O conector resolve automaticamente símbolos com prefixos/sufixos da
corretora, mantém uma conexão persistente, reconecta com backoff, coleta
somente candles fechados nos nove timeframes e atualiza ticks. A persistência
em `symbols`, `candles` e `ticks` é incremental e idempotente.

### Quando ele fica offline — não reinstale

O conector é supervisionado em três camadas, e nenhuma delas exige
reinstalação:

1. **Dentro do processo.** Uma exceção no ciclo custa um ciclo, não o
   processo. Antes, um segundo de banco indisponível derrubava o conector;
   hoje ele registra a falha, espera (5s, 10s, 15s… até 60s) e continua.
2. **No Windows.** A tarefa agendada tem dois gatilhos: no login e uma
   repetição a cada 5 minutos com `MultipleInstances IgnoreNew`. Se já está
   rodando, a repetição é descartada; se morreu, ele volta em no máximo
   5 minutos.
3. **Manual, sem reinstalar.** `scripts\reiniciar_conector_mt5.cmd` para e
   sobe a tarefa. Reinstalar refaz o ambiente Python inteiro e só é
   necessário quando o ambiente muda — dependência nova, caminho diferente.

Duas causas de queda que não são bug e valem conhecer:

- **O MetaTrader 5 fechou.** Sem terminal aberto e logado, o conector não
  tem com quem falar. O painel mostra isso em `last_error`.
- **Logoff em vez de desconectar (VPS).** Fechar o RDP com o **X** mantém a
  sessão do Windows viva e o MT5 rodando. *Fazer logoff* encerra a sessão e
  derruba os dois — o conector só volta quando alguém entrar de novo.

O mesmo worker executa a automação configurada em **Operações
automáticas**. O dashboard permanece no container Linux e grava o plano no
MySQL; o worker Windows o consome após cada sincronização. Ordens só podem
ser enviadas quando o modo persistido está em `DEMO`, a conta conectada foi
identificada como demo e todos os limites do motor de risco aprovam o sinal.
O container nunca tenta instalar ou emular o pacote `MetaTrader5`.

## Qualidade de dados e retenção (Fase 3)

`collect candles`/`collect ticks` agora são **incrementais**: se já existir
dado coletado para o símbolo, buscam apenas o que falta desde o último
registro conhecido (`--count`/`--seconds` só valem na primeira coleta,
usada como backfill). Toda coleta roda checagens de qualidade e imprime uma
nota de 0 a 100 — nunca escondendo as ocorrências individuais atrás do
número:

```powershell
python -m app.cli collect candles --symbol EURUSD --timeframe M1 --count 500
# modo: backfill | candles buscadas: 500 | novas inseridas: 500 | qualidade: 100/100 (0 ocorrencia(s))

python -m app.cli collect candles --symbol EURUSD --timeframe M1
# modo: incremental | candles buscadas: 3 | novas inseridas: 3 | qualidade: 100/100 (0 ocorrencia(s))
```

Para checar a qualidade de dados já armazenados sem reconectar ao
MetaTrader (ex.: num cron separado de monitoramento):

```powershell
python -m app.cli quality check --symbol EURUSD --timeframe M1
```

O código de saída é `1` quando os dados não passam no gate de qualidade
(`QUALITY_MIN_SCORE`, padrão 70, ou qualquer ocorrência `CRITICAL`).

Ticks acumulam rápido; a retenção configurável (`TICK_RETENTION_DAYS`,
padrão 30) remove os mais antigos. Candles nunca são expurgadas.

```powershell
python -m app.cli data purge-ticks                       # usa TICK_RETENTION_DAYS
python -m app.cli data purge-ticks --older-than-days 7   # sobrescreve pontualmente
```

## Indicadores, features e regime de mercado (Fase 4)

Sobre candles já armazenadas (sem reconectar ao MetaTrader), calcule
indicadores/features e veja o regime de mercado atual:

```powershell
python -m app.cli features build --symbol EURUSD --timeframe M1 --rows 5
```

Isso imprime uma tabela com as últimas barras (close, RSI, ADX, ATR,
tendência, volatilidade, adequação de spread/liquidez) e o regime completo
da barra mais recente. Ver `docs/features.md` para o catálogo completo de
features (fórmula, janela, atraso, risco de vazamento, custo) e
`app/market/regimes.py` para os limiares de classificação de regime
(`RegimeThresholds`, configuráveis por chamada).

Algumas features (ex.: `ema_200`) só ficam sem `NaN` depois de 200 barras —
o comando avisa em stderr quando há menos histórico que isso.

## Backtest por candle (Fase 5)

Roda a estratégia baseline (cruzamento de EMA9/EMA21 — Estratégia B do
prompt mestre, usada apenas como referência de comparação) sobre candles já
armazenadas:

```powershell
python -m app.cli backtest run --symbol EURUSD --timeframe M1 --fast 9 --slow 21 --stop-points 100 --target-points 200 --volume 0.01 --commission-per-lot 7 --slippage-points 1 --json-out reports/backtest.json
```

Isso imprime um relatório completo (lucro líquido, drawdown, profit
factor, Sharpe/Sortino/Calmar, MAE/MFE, custo total, resultado por hora/
dia da semana/tendência/direção etc. — nunca escondido atrás de um único
número) e, com `--json-out`, salva o relatório completo (métricas + cada
trade) em JSON.

Duas regras inegociáveis do motor (`app/backtesting/engine.py`), sempre
ativas:

- **Nunca escolhe o resultado favorável**: se o stop-loss e o take-profit
  caem dentro do intervalo `[low, high]` da mesma candle, o motor sempre
  assume que o stop foi atingido primeiro (o pior cenário), nunca o alvo.
- **Sem dados futuros**: um sinal gerado na barra `t` só é executado na
  abertura da barra `t+1` — nunca no fechamento da própria barra em que foi
  gerado.

Esta fase validou o motor com uma única estratégia; as demais chegaram na
Fase 6 (abaixo).

## Estratégias base e relatório comparativo (Fase 6)

5 estratégias estão registradas (`app/strategies/registry.py`) e podem ser
escolhidas em `backtest run --strategy <nome>`:

| Nome (`--strategy`) | Descrição |
|---|---|
| `ema_crossover` (padrão) | Baseline — cruzamento de EMA9/EMA21 |
| `trend_pullback` | Tendência com pullback (Estratégia A) |
| `range_breakout` | Rompimento de consolidação (Estratégia C) |
| `zscore_mean_reversion` | Retorno à média (Estratégia D) |
| `momentum_continuation` | Momentum (Estratégia E) |

```powershell
python -m app.cli backtest run --symbol EURUSD --timeframe M1 --strategy trend_pullback
```

Para comparar todas de uma vez, lado a lado, sobre os mesmos dados e
custos:

```powershell
python -m app.cli backtest compare --symbol EURUSD --timeframe M1 --commission-per-lot 7 --slippage-points 1
```

O relatório comparativo (`app/backtesting/comparison.py`) mostra as
mesmas métricas para cada estratégia **na ordem em que rodaram, nunca
ordenadas por lucro** — o prompt mestre proíbe eleger uma estratégia
"pronta" automaticamente ou só pelo lucro líquido; isso é avaliação de
robustez (Fase 9) e, no fim, decisão humana.

Cada estratégia é isolada e configurável via seu próprio dataclass de
`Config` (ex.: `TrendPullbackConfig`, `RangeBreakoutConfig`) — a CLI expõe
apenas os parâmetros da baseline (`--fast`/`--slow`/`--stop-points`/
`--target-points`); as demais usam parâmetros padrão nesta fase, ajustáveis
diretamente em código Python.

## Backtest por tick (Fase 7)

Mesmas estratégias, execução mais realista: em vez de aproximar a entrada/
saída pela OHLC da candle seguinte, o motor por tick busca fills reais
contra a sequência de ticks (bid/ask), com latência, spread variável,
slippage e rejeição de entrada quando o spread está largo demais:

```powershell
python -m app.cli backtest run-ticks --symbol EURUSD --timeframe M1 --strategy trend_pullback --latency-ms 100 --slippage-points 1 --max-spread-points 30 --max-holding-seconds 300 --trailing-stop-points 50 --json-out reports/tick_backtest.json
```

Requer ticks já coletados (`collect ticks`) cobrindo o mesmo período das
candles usadas para gerar os sinais — se não houver ticks suficientes no
período, o comando falha com uma mensagem clara em vez de inventar dados.

A diferença mais importante em relação ao motor por candle: quando
stop-loss e take-profit cabem na mesma candle, aquele motor **sempre**
assume o pior caso (não tem como saber a ordem real dos preços). Aqui, com
a sequência real de ticks, a ordem cronológica verdadeira resolve isso —
o motor simplesmente verifica qual nível foi cruzado primeiro. Cada fill
(entrada e saída) fica registrado com auditoria completa (preço
solicitado, preço de execução, latência, spread, motivo de rejeição) no
JSON exportado.

Funcionalidades adicionais deste motor: trailing stop
(`--trailing-stop-points`), fechamento por tempo (`--max-holding-seconds`)
e aviso de liquidez insuficiente quando há gaps grandes entre ticks
consecutivos durante o monitoramento de uma posição.

## Modelos de IA (Fase 8)

**O que o modelo estima:** dado um sinal que uma estratégia (Fase 6) já
geraria, qual a probabilidade de o alvo ser atingido antes do stop? O
modelo nunca decide *quando* entrar — isso continua sendo a estratégia.
Ver `docs/ml.md` para o pipeline completo (rotulagem por barreira tripla,
split temporal com embargo, calibração de probabilidades, explicabilidade
via SHAP/coeficientes e critérios formais de aprovação).

```powershell
# 1. Gera o dataset de sinais rotulado (uma linha por sinal real da estratégia)
python -m app.cli ml build-dataset --symbol EURUSD --timeframe M1 --strategy ema_crossover --max-horizon-bars 50 --out datasets/eurusd_m1_ema_crossover.csv

# 2. Treina + calibra + avalia (fora da amostra) + registra a versão
python -m app.cli ml train --dataset datasets/eurusd_m1_ema_crossover.csv --symbol EURUSD --timeframe M1 --strategy-name ema_crossover_baseline --model logistic_regression

# 3. Recarrega uma versão registrada (ou a "current") e recalcula suas métricas
python -m app.cli ml evaluate
```

Modelos disponíveis: `logistic_regression`, `random_forest`,
`hist_gradient_boosting`, `xgboost` — sempre comparados contra a
ausência de IA (a própria estratégia sem filtro), nunca escolhidos
apenas pelo maior ROC-AUC. A flag `--approve` do comando `train` marca
uma versão como aprovada no manifesto — **essa decisão é sempre manual**,
o pipeline nunca aprova um modelo sozinho.

**Limitações conhecidas desta fase** (detalhadas em `docs/ml.md`): sem
teste de estabilidade multi-símbolo (walk-forward multi-período é a
Fase 9, abaixo), `LightGBM` não instalado (opcional, sem consumidor
concreto ainda), e algumas features (MACD, momentum, forma do candle)
ainda em escala de preço bruta.

## Walk-forward e robustez (Fase 9)

Uma estratégia ou modelo nunca é aprovado por um único backtest/split —
o prompt mestre exige medir estabilidade entre períodos e resistência a
custos mais adversos do que os observados.

**Estratégias** (`app/backtesting/walk_forward.py`,
`monte_carlo.py`, `robustness.py`):

```powershell
# Roda a estrategia em N janelas cronologicas nao sobrepostas
python -m app.cli backtest walk-forward --symbol EURUSD --timeframe M1 --strategy ema_crossover --n-windows 5

# Risco de ruina empirico (bootstrap dos trades de um backtest)
python -m app.cli backtest monte-carlo --symbol EURUSD --timeframe M1 --strategy ema_crossover --num-simulations 1000

# Degradacao do resultado com slippage/comissao multiplicados
python -m app.cli backtest stress-test --symbol EURUSD --timeframe M1 --strategy ema_crossover --slippage-multiplier 3 --commission-multiplier 3
```

O walk-forward marca o resultado como `ESTAVEL` apenas quando pelo menos
metade das janelas com trades suficientes foram lucrativas **e** nenhuma
janela isolada respondeu por mais de 80% do lucro positivo total —
protege contra uma estratégia "boa" só por sorte de um período.

**Modelos de ML** (`app/ml/walk_forward.py`, `app/ml/approval.py`):

```powershell
python -m app.cli ml walk-forward --dataset datasets/eurusd_m1_ema_crossover.csv --symbol EURUSD --model logistic_regression --n-windows 5
```

Treina/calibra/avalia em janelas expansivas (o treino cresce a cada
janela; o teste é sempre a fatia cronológica seguinte) e imprime um
veredito por critério dos 5 critérios formais de aprovação — **sempre
uma recomendação, nunca uma aprovação automática**; a decisão de gravar
`approved=True` continua exclusivamente em `ml train --approve`.

## Paper trading e máquina de estados (Fase 10)

O modo do sistema (`SystemMode`, desde a Fase 1) ganha validação e
persistência reais pela primeira vez — avanço só um passo por vez
(`DISABLED -> DATA_ONLY -> BACKTEST -> REPLAY -> PAPER -> DEMO`),
retrocesso sempre permitido, `EMERGENCY_STOP` a partir de qualquer
estado ativo. `REAL_LOCKED`/`REAL_ENABLED` **continuam bloqueados**
incondicionalmente. Ver `docs/paper-trading.md` para a máquina de
estados completa e o motor de paper trading incremental.

```powershell
python -m app.cli mode set DATA_ONLY
python -m app.cli mode set BACKTEST
python -m app.cli mode set REPLAY
python -m app.cli mode set PAPER

# So roda se o modo atual for PAPER -- nunca envia uma ordem real
python -m app.cli paper run --symbol EURUSD --timeframe M1 --strategy ema_crossover --iterations 1

python -m app.cli paper status --symbol EURUSD --strategy ema_crossover
```

Cada chamada de `paper run` só avalia as barras novas desde a última vez
(cursor persistido) — nunca reprocessa o histórico inteiro. Diferença
deliberada em relação ao backtester: a execução acontece no fechamento
da própria barra do sinal (não há look-ahead a evitar quando os dados
chegam ao vivo), e a posição aberta é persistida em banco, sobrevivendo
a reinício do processo.

## Executor em conta demo (Fase 11)

Primeira vez que o sistema envia uma ordem real ao MetaTrader 5 — sempre
contra uma conta **DEMO**, nunca uma conta real. Dois portões de
segurança independentes: o modo do sistema precisa estar em `DEMO`
(`mode set DEMO`, só alcançável após `PAPER`), e
`app.mt5.orders.send_market_order` recusa qualquer conta que não seja
demo (o executor de demo nunca pede `allow_real_account`) — verificado a
cada iteração, não só uma vez.
Entre os dois, um motor de risco com poder de veto (`app/risk`) aprova
ou rejeita cada sinal, sempre com um motivo explícito. Ver
`docs/execution.md` para o motor de risco, a máquina de estados de
ordem e a lógica de reconciliação completos.

```powershell
python -m app.cli mode set DEMO   # exige ja estar em PAPER

python -m app.cli demo run --symbol EURUSD --timeframe M1 --strategy ema_crossover `
    --risk-per-trade-pct 1.0 --max-daily-loss-pct 3.0 --max-consecutive-losses 3 --iterations 1

python -m app.cli demo status --symbol EURUSD --strategy ema_crossover
```

Regras inegociáveis do prompt mestre implementadas em código: sinal sem
stop-loss é rejeitado incondicionalmente; o dimensionamento de posição
nunca depende do resultado de trades anteriores (sem martingale/soros,
por construção); circuit breakers de 4 níveis bloqueiam novas entradas
antes que perdas consecutivas ou prejuízo diário virem "recuperação
compulsiva". Quem fecha a posição é o broker (stop-loss/take-profit
anexados ao próprio pedido) — este processo só reconcilia o que já
aconteceu, nunca envia uma ordem de fechamento por conta própria.

## Piloto automático — escolha a moeda, o robô decide o resto

Escolha um par em `/dashboard/trading`, escolha DEMO ou REAL, clique em
*Começar a operar*, e o robô passa a decidir
sozinho **como** operar: lê a sessão de negociação daquele par
(`app/market/sessions.py`), compara o volume atual com a mediana histórica
da **mesma hora** (`app/market/volume_profile.py`) e elege um dos
operacionais já validados — tendência com pullback, rompimento, retorno à
média, momentum ou cruzamento — junto do timeframe de execução, do score
mínimo e do multiplicador de risco (`app/execution/playbook.py`).

Enquanto trabalha, um painel ao vivo mostra em que etapa ele está — lendo o
mercado, escolhendo o operacional, analisando, aguardando o gatilho,
enviando a ordem, acompanhando a posição — com o motivo de cada decisão e um
feed das últimas atividades. Se o worker parar de publicar, a tela mostra
"desatualizado" em vez de fingir que o robô continua trabalhando.

Duas invariantes de segurança, impostas por código e cobertas por teste: o
**score mínimo nunca fica abaixo** do configurado (horário ruim só torna o
robô mais exigente) e o **multiplicador de risco nunca passa de 1.0**.

São dois tipos de operação, escolhidos na mesma tela: **DEMO** (dinheiro
fictício) e **REAL** (dinheiro de verdade). Ligar exige que a conta
conectada no MetaTrader seja **do mesmo tipo** do modo escolhido, nas duas
direções — DEMO com conta real é recusado, e REAL com conta demo também —
e um clique percorre a escada de modos do sistema inteira, auditada. O
mesmo botão de *Começar a operar*, com o status ao vivo, aparece embutido
em **Dados de mercado** e em **Análise PRO**.

```powershell
python -m app.cli autopilot status
python -m app.cli autopilot run --iterations 20 --poll-seconds 15
```

Ver `docs/autopilot.md`.

## Radar de oportunidades — qual moeda operar agora

O sistema sempre soube avaliar **um** símbolo: o que você escolheu. O radar
(`app/market/scanner.py`) compara todos os coletados e ordena por sessão de
negociação (40%), volume relativo (35%) e **custo de entrada** (25%).

Três decisões que separam isso de um scanner ingênuo:

**Tudo é normalizado.** Volume bruto de XAUUSD não se compara com o de
EURUSD. Cada critério mede o ativo contra ele mesmo — volume contra a mediana
da própria hora, spread contra o próprio ATR.

**O custo entra na nota.** Um sinal ótimo num par com spread três vezes acima
do normal é pior que um sinal bom num par barato: o custo é certo, o sinal é
hipótese.

**Correlação vira veto** (`app/market/correlation.py`). Abrir EURUSD, GBPUSD
e AUDUSD ao mesmo tempo não é diversificação — é a mesma aposta contra o
dólar, três vezes, com 3x o risco. A correlação é **medida** dos retornos que
já estão no banco, não presumida de uma tabela escrita à mão. Correlação
negativa forte também conta: comprar EURUSD e vender USDCHF é a mesma aposta.

**Modo observação.** O radar aumenta o número de oportunidades, não a
qualidade média de cada uma — se a expectativa por operação for negativa,
varrer o mercado faz perder mais rápido. Por isso o diário grava o que ele
*escolheria*, incluindo a margem para o segundo colocado: margem baixa
significa que o ranking está quase sorteando, e a complexidade não se paga.

Liga-se em `/dashboard/scanner`, com dois botões: **Ligar modo observação**
(o worker do MetaTrader passa a gravar uma amostra no intervalo escolhido) e
**Gravar esta varredura agora**. Acumular amostra dependia de alguém lembrar
de rodar um comando periodicamente — o que, na prática, significava que a
amostra nunca existiria. A CLI (`scanner run --record`) continua disponível
para diagnóstico, mas não é mais o caminho.

A tela nunca envia ordem: quem liga o robô continua sendo a tela de operação.

## Calendário econômico — o portão que nunca disparou

O sistema tinha um filtro para não entrar em cima de evento de alto impacto.
Ele comparava a hora atual com a data de **publicação de uma notícia** —
sempre no passado — então a condição nunca era verdadeira: **nunca bloqueou
uma única entrada**. E ignorava a moeda do evento, de modo que um dado do
iene teria barrado uma entrada em EUR/USD.

A raiz era tratar notícia e calendário como a mesma coisa. Agora são tipos
diferentes: manchete tem hora de publicação, evento tem hora de agendamento.
O portão compara com o agendamento, filtra pelas moedas do instrumento e
bloqueia dos **dois lados** do evento (o spread abre depois da divulgação,
não só antes).

Sem calendário configurado, o robô **continua operando** — travar por falta
de dado externo recriaria o problema que o projeto acabou de resolver. A
ausência aparece no status e no relatório do dia. Fonte sugerida: o
calendário nativo do MT5, exportado para JSON por um EA. Ver
`docs/calendar.md`.

## Corretora de execução — MT5 e cTrader

O sistema decide **onde** entrar; a corretora só executa. Essa separação é a
porta `BrokerPort` (`app/broker/port.py`), com quatro operações: ler a conta,
listar posições, enviar ordem a mercado com stop e alvo anexados, e alterar a
proteção. Não existe "fechar posição" na porta — quem encerra é o stop ou o
alvo do lado do broker.

`BROKER=mt5` é o padrão e continua sendo o caminho de produção.
`BROKER=ctrader` liga o adaptador da cTrader Open API, que roda em Linux e
dispensa o Windows. A guarda de coerência entre modo e tipo de conta vale
igual nos dois, nos dois sentidos.

A camada de tradução da cTrader — conversão de lotes para centésimos de
unidade, resolução de símbolo, montagem do pedido — é coberta por testes
determinísticos. O transporte TCP+TLS **ainda não foi validado contra os
servidores reais**: use conta demo antes de qualquer dinheiro. Ver
`docs/broker.md`.

## ApexFlow AI — motor de decisão por fluxo de ticks

Alternativa ao seletor de operacional, ligada em `/dashboard/apexflow`. Em
vez de reagir a um cruzamento de indicador, interpreta o **comportamento**
do mercado: fluxo de ticks (taxa, aceleração, eficiência do trajeto),
microestrutura, price action, liquidez institucional e contexto
multi-timeframe com papéis fixos (H1 macro → M15 contexto → M5 confirmação
→ M1 timing → tick execução; **H1 nunca gera entrada**).

A saída tem exatamente três possibilidades — **COMPRAR**, **VENDER** ou
**NÃO OPERAR** — e a abstenção é o padrão. Vetos duros (spread, volatilidade
insuficiente, caça de liquidez, sensores incompletos) são avaliados **antes**
da probabilidade e não podem ser sobrepostos por ela, nem por um modelo com
99% de confiança.

O cérebro padrão é um scorecard determinístico e totalmente explicável;
qualquer modelo treinado e aprovado pode substituí-lo consumindo o mesmo
feature vector versionado. Toda decisão — inclusive as de não operar, que são
a maioria — vai para o Learning Engine (`apexflow_decisions`) com o vetor
completo, para reavaliação futura.

```powershell
python -m app.cli apexflow analyze --symbol XAUUSD --timeframe M5
python -m app.cli apexflow history --symbol XAUUSD
```

Ver `docs/apexflow.md`.

## Executando a API

```powershell
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

- `GET /health` — status da aplicação, do banco e o modo atual do sistema.
- `POST /api/auth/login` — autenticação (usuário/senha) e emissão de JWT.
- `GET /api/auth/me` — dados do usuário autenticado (requer
  `Authorization: Bearer <token>`).

Não há usuário administrador pré-criado. Crie o primeiro usuário via uma
sessão Python (o comando de CLI dedicado será adicionado em fase futura):

```powershell
python -c "
from app.database.session import get_session_factory, get_engine
from app.database.base import Base
import app.database.models
from app.database.repositories.user_repository import UserRepository
from app.core.security import hash_password

Base.metadata.create_all(bind=get_engine())
session = get_session_factory()()
repo = UserRepository(session)
role = repo.get_or_create_role('ADMIN')
repo.create_user(username='admin', email='admin@example.com', password_hash=hash_password('CHANGE_ME'), roles=[role])
session.commit()
"
```

## Dashboard e Análise PRO

Com a API rodando, acesse `http://localhost:8000/login` no navegador
(usuário criado acima). O menu **Análise PRO** abre o workbench consultivo:
selecione um símbolo sincronizado pelo MetaTrader 5 e um timeframe
operacional para obter score, tendência, padrão, alinhamento dos nove
timeframes, confluências e — somente quando todos os gates forem aprovados —
entrada, stop estrutural, três alvos, break-even e trailing.

O modo profissional fixa score mínimo 90 e bloqueia a operação quando houver
cobertura multi-timeframe incompleta, volume desfavorável, notícia HIGH
iminente ou fontes externas sem confirmação. A página não envia ordens.

A página **Mercados** adiciona um catálogo curado com 32 instrumentos:
principais, cruzados, exóticos, ouro (`XAU/USD`) e prata (`XAG/USD`). O
catálogo reconhece sufixos de corretora como `XAUUSD.a`, diferencia ativos
prontos dos que ainda precisam de coleta e leva cada item diretamente à
seleção automática. Ele não cria preços ou especificações genéricas. Para um
par aparecer no seletor de análise, marque-o em **Conexão MT5**; o worker
descobre o nome real, incluindo sufixos, e carrega a matriz
`MN1/W1/D1/H4/H1/M30/M15/M5/M1` sem comandos.

Configure o MarketPulse em **API AIsa** ou pela variável `AISA_API_KEY`.

### A API não cobre câmbio — leia antes de assinar

Os endpoints usados (`financial/news`, `financial/financial-metrics`) são de
**mercado acionário**. "Métricas financeiras anuais" descrevem uma empresa:
receita, margem, alavancagem. **EURUSD não tem balanço.** Nenhuma chave faz
esse dado existir, e a cobertura anunciada da AIsa para essa família é ações
e cripto — não pares de moedas nem metais.

Era isso que consumia o crédito: cada análise pedia duas coisas impossíveis,
recebia erro, e — como falha não era guardada — tentava de novo no ciclo
seguinte. `app/news/coverage.py` corta a pergunta antes de sair, com custo
zero. Os fatores Notícias e Fundamentos saem do cálculo com o peso
redistribuído (`SKIPPED`, não `ERROR`: não houve falha da API). O robô não
piora com isso — ele já não recebia informação nenhuma dali.

**Para um sistema de câmbio, essa assinatura hoje não entrega nada.** O
substituto correto é o calendário econômico (`docs/calendar.md`), que é
gratuito e vem do próprio MetaTrader.

### Camadas de proteção da cota

Cada leitura nova consome **duas** chamadas. Quatro camadas protegem, de fora
para dentro — cada uma existe porque a de dentro custa mais que ela:

1. **Cobertura** (`app/news/coverage.py`): a pergunta faz sentido para este
   ativo? Custo zero.
2. **Portões locais antes da API.** Se a entrada já está bloqueada por
   cobertura de dados ou volume, a MarketPulse não é consultada.
3. **Armazenamento no banco** (`app/news/store.py`): uma leitura por dia por
   moeda, compartilhada entre o painel e o conector Windows e sobrevivendo a
   reinício. Guarda **também as falhas**, por uma janela menor (60 min) — sem
   isso um endpoint quebrado era tentado a cada ciclo de 15 s.
4. **Teto diário** (`NEWS_DAILY_CALL_BUDGET`, padrão 300), contado no banco.
   Ao atingir o limite, os fatores externos saem do cálculo.

Há ainda um cache de memória por processo (`NEWS_CACHE_TTL_SECONDS`) como
atalho barato. Prazos, consumo do dia, o que está guardado e **o registro de
cada chamada real, com origem** (painel, robô ou linha de comando) aparecem
em `/dashboard/settings/aisa`, junto com um botão de **Testar conexão** que
mostra a resposta crua da API.

A resposta reaproveitada **declara a própria idade** (`[guardado há 2 h]`):
o sistema nunca apresenta dado velho como se fosse fresco.

Ver `docs/dashboard.md` para autenticação, rotas e detalhes operacionais.

## Leitura visual do Pulso com Jev/TypeSafe (opcional)

Camada **desligada por padrão** que recebe o resumo técnico já calculado e
devolve **um rótulo entre quatro** para o gráfico do MetaTrader:
`STRONG_SETUP` (verde), `CAUTION` (amarelo), `WAIT` (cinza),
`INSUFFICIENT_DATA` (laranja), mais um aviso de "confira antes de agir".

Ela existe porque o operador hoje precisa cruzar sete números de cabeça para
ler o conjunto. O que ela **não** faz: não gera preço, zona, entrada, stop,
alvo, direção nem ordem. Esses valores continuam vindo exclusivamente do
motor técnico determinístico, e nenhum deles chega a ser enviado — o resumo
leva distâncias em percentual, nunca os níveis. Desligar o Jev não muda um
número no gráfico.

Não é previsão garantida, não é aconselhamento financeiro e não automatiza
execução. A **confiança** devolvida mede o quanto o classificador se
concentrou em um rótulo, não a chance de o trade dar lucro — a mesma ressalva
que vale para o score, que é confluência.

```bash
JEV_ENABLED=false
JEV_API_KEY=                 # só do ambiente: sem campo no painel, sem linha no banco
JEV_API_BASE_URL=https://api.typesafe.ai/v1/systemone
JEV_MODEL=jev-latest
JEV_TIMEOUT_SECONDS=2.5
JEV_CACHE_TTL_SECONDS=300
```

**Não reaproveite `AISA_API_KEY`** — são serviços distintos, e uma chave
compartilhada faz o vazamento de um virar o vazamento dos dois.

**Fallback.** Desligado, sem chave, indisponível, lento ou com resposta
inválida, o Pulso responde igual, com a análise técnica inteira;
`signal_ai.available` vem `false`, `confidence` vem `0` (nenhuma confiança é
inventada) e o estado vem `INSUFFICIENT_DATA` — que é o rótulo honesto para
"ninguém classificou", enquanto `CAUTION` seria um julgamento que nenhum
modelo emitiu. Nada é bloqueado e nada é executado por causa da ausência. Um
EA antigo ignora o bloco: o campo é aditivo e a versão do contrato não mudou.

**Custo.** O veredito é reaproveitado enquanto a candle e o resumo técnico
não mudam, então o indicador consultar a cada 15 s não vira uma chamada a
cada 15 s. Detalhes em `docs/indicador-mt5.md`.

## Detecção de drift (Fase 13)

Um modelo aprovado não fica bom para sempre. `monitor model` compara um
modelo registrado contra um dataset recente (drift de features via PSI +
degradação de calibração/desempenho em relação ao treino); `monitor
feed` verifica se o feed de candles está atualizado. Ambos só relatam e
persistem ocorrências `WARNING`/`CRITICAL` em `drift_events` — nenhuma
decisão automática (retreinar, desativar um modelo, parar o sistema).
Ver `docs/monitoring.md`.

```powershell
python -m app.cli monitor model --recent-dataset datasets/eurusd_m1_recente.csv
python -m app.cli monitor feed --symbol EURUSD --timeframe M1
```

Esta fase também fechou uma pendência da Fase 11: o motor de risco
(`app.risk.engine.evaluate_signal`) agora recusa incondicionalmente um
sinal quando o feed de dados está atrasado além de
`RiskLimits.max_feed_delay_seconds` (`app/risk/feed_health.py`).

## Preparação operacional (Fase 15)

Antes de operar por um período estendido (paper ou demo), valide o
ambiente de ponta a ponta:

```powershell
python -m app.cli preflight check
```

Checa segredo de aplicação (placeholder do `.env.example` esquecido),
conexão com o banco, migrations em dia, diretórios de artefato
(`logs/`, `models/`, `datasets/`) graváveis e credenciais MT5
configuradas — cada item reporta `OK`/`AVISO`/`FALHA` com uma explicação;
código de saída `1` só quando há `FALHA`. Ver `docs/runbook.md` para o
procedimento operacional completo (primeira instalação, rotina de
operação, checagens periódicas e resposta a incidentes).

Esta fase também corrigiu um bug real: `GET /health` reportava o modo do
sistema estático de boot (`SYSTEM_MODE` do `.env`) em vez do modo
persistido de verdade — agora consulta o banco corretamente.

## Testes, lint e type-check

```powershell
pytest --cov=app --cov-report=term-missing
ruff check app tests
black --check app tests
mypy app main.py
```

A suíte principal (`pytest`) nunca exige um terminal MetaTrader instalado
nem um MySQL real — todos os testes rodam com SQLite em memória e mocks.

## Estrutura do projeto

Ver `docs/architecture.md` para a descrição completa de cada módulo.
Existem hoje: `app/core` (config, logging, segurança, enums, exceções),
`app/database` (modelos, sessão, repositórios), `app/api` (rotas de
saúde, autenticação de API e do dashboard, dashboard HTML — Fase 12),
`app/mt5` (conector MetaTrader 5, leitura + envio de ordem em conta
demo), `app/market` (qualidade de dados, indicadores, features e
regime), `app/strategies` (interface `Strategy`/`Signal`, registro e 5
estratégias: baseline, pullback, rompimento, retorno à média, momentum),
`app/backtesting` (motores por candle e por tick, custos, fills,
métricas, relatórios, comparação, walk-forward, Monte Carlo, stress test
de custos), `app/ml` (rotulagem por barreira tripla, dataset de sinais,
split temporal, treino, calibração, validação, explicabilidade, registro
de modelos, walk-forward e critérios formais de aprovação),
`app/paper_trading` (motor incremental persistido, Fase 10), `app/risk`
(motor de risco com poder de veto + saúde do feed, Fases 11/13),
`app/execution` (máquina de estados de ordem e motor de execução em
conta demo, Fase 11), `app/monitoring` (detecção de drift + checagens de
prontidão operacional, Fases 13/15), `app/apexflow` (motor de decisão por
fluxo de ticks: tick flow, contexto de mercado, multi-timeframe, momentum,
volatilidade, spread, liquidez, feature vector, decisão, risco dinâmico e
Learning Engine) e `app/cli.py` (comandos de
diagnóstico/coleta/análise/backtest/ML/paper trading/demo/autopilot/
apexflow/monitor/preflight). Os demais diretórios (`app/strategies/microstructure`,
`app/strategies/ensemble` etc.) existem como esqueleto vazio, reservados
para as fases correspondentes.

## Documentação

- `docs/architecture.md` — arquitetura completa (alvo) do sistema.
- `docs/security.md` — modelo de segurança e regras inegociáveis.
- `docs/risk-management.md` — matriz de riscos do projeto e do motor de
  risco de trading.
- `docs/data-model.md` — catálogo de tabelas e convenções de dados.
- `docs/features.md` — catálogo de indicadores/features (fórmula, janela,
  atraso, risco de vazamento, custo computacional).
- `docs/ml.md` — pipeline de machine learning (rotulagem, dataset,
  split, treino, calibração, validação, explicabilidade, registro e
  critérios de aprovação de modelo).
- `docs/paper-trading.md` — máquina de estados do sistema e motor de
  paper trading incremental (Fase 10).
- `docs/execution.md` — motor de risco, máquina de estados de ordem e
  motor de execução em conta demo (Fase 11).
- `docs/dashboard.md` — autenticação por cookie e páginas do dashboard
  HTML somente leitura (Fase 12).
- `docs/monitoring.md` — detecção de drift de features/métricas e saúde
  do feed de dados (Fase 13).
- `docs/runbook.md` — procedimentos operacionais práticos: instalação,
  rotina de operação, checagens periódicas e resposta a incidentes
  (Fase 15).
- `docs/indicador-mt5.md` — indicador (EA) que desenha as zonas do painel
  no gráfico do MetaTrader, a API de chaves que o autentica, o botão de
  ligar/desligar a análise da IA e a camada opcional de classificação
  visual Jev/TypeSafe (`signal_ai`).
- `docs/foto-analise.md` — FotoAnálise: camada visual sobre a análise
  existente (mapa de calor de confluência, zona de entrada, sweet spot),
  o que reaproveita e por que o score não é probabilidade de lucro.
- `docs/mt5-bridge.md` — ponte RPyC para um MetaTrader sob Wine em
  container: quando usar, como configurar pelo painel, por que a porta
  18812 nunca vai para a internet e o diagnóstico de seis passos.
- `docs/autopilot.md` — piloto automático: sessões de negociação, perfil de
  volume por hora, seleção de operacional e status ao vivo.
- `docs/apexflow.md` — motor ApexFlow AI: fluxo de ticks, contexto de
  mercado, feature vector, decisão com probabilidade e Learning Engine.
- `docs/development-phases.md` — roadmap de 15 fases e critérios de aceite.
- `docs/assumptions.md` — decisões e premissas tomadas na Fase 0, incluindo
  o motivo de usar Python 3.14 e SQLite nos testes nesta fase.

## Solução de problemas

- **`pydantic_core.ValidationError` na inicialização**: uma variável
  obrigatória está ausente no `.env`. A mensagem de erro indica qual campo
  falhou — a aplicação falha rápido de propósito, em vez de rodar com
  configuração indefinida.
- **`alembic upgrade head` falha ao conectar**: confirme que `DB_HOST`,
  `DB_PORT`, `DB_USER`, `DB_PASSWORD` e `DB_NAME` no `.env` apontam para um
  MySQL 8 acessível e que `APP_ENV` não está definido como `test` (nesse
  caso a URL de banco seria SQLite em memória, não o MySQL configurado).
- **Testes falhando por causa de bcrypt/passlib**: este projeto usa a
  biblioteca `bcrypt` diretamente (não `passlib`), justamente porque
  `passlib` (sem atualização desde 2020) é incompatível com versões
  recentes do `bcrypt` (>=4.1). Não reintroduza `passlib`.
- **`mt5 check`/`collect *` retornam `ERRO: Nao foi possivel conectar...`**:
  confirme que o terminal MetaTrader 5 está instalado, aberto e que
  `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER` no `.env` correspondem a uma
  conta válida. O comando nunca inventa uma resposta — reporta exatamente o
  que `MetaTrader5.last_error()` devolveu.

## Pendências conhecidas desta fase

- Validação de conexão real contra MySQL 8 (nenhum servidor disponível no
  ambiente de desenvolvimento até o momento — ver `docs/assumptions.md`).
- Validação de conexão real contra um terminal MetaTrader 5 autenticado
  (nenhum disponível neste ambiente de desenvolvimento — o conector foi
  validado com o pacote `MetaTrader5` real instalado, mas sem terminal
  configurado, e exaustivamente com um cliente fake nos testes automatizados).
- Aviso de depreciação do `httpx`/`starlette.testclient` sugerindo
  `httpx2`; não tratado nesta fase por ser um aviso não-bloqueante em uma
  dependência de teste.
- Divergência entre candles e ticks (item do prompt mestre, seção 8) não é
  checada ainda — exige alinhar janelas de coleta que não são garantidas
  sobrepostas nesta fase. Documentado em `app/market/data_quality.py`.
- Features de microestrutura de tick, proximidade de notícia, correlação
  entre ativos e tendência do timeframe superior (seção 9 do prompt mestre)
  ficam para quando as estratégias/fases que as consomem existirem (ver
  `docs/features.md`, seção "Fora de escopo").
- Backtest por candle é uma simplificação deliberada (uma posição por vez,
  volume fixo, sem conversão cambial, custos não segmentados por
  corretora/conta/sessão) — documentado em `app/backtesting/engine.py` e
  `app/backtesting/costs.py`. O dimensionamento de risco real (Fase 17)
  trata essa última lacuna.
- `estimated_risk_of_ruin` (métrica de backtest, Fase 5) continua sendo
  uma aproximação analítica; a Fase 9 adicionou a alternativa empírica
  (`app.backtesting.monte_carlo.simulate_bootstrap`), mas não substituiu
  a analítica — ambas ficam disponíveis, cada uma com seu uso.
- `LightGBM` não foi instalado (opcional, sem consumidor concreto
  ainda). Ver `docs/ml.md` para a lista completa de limitações do
  pipeline de ML.
- Walk-forward (Fase 9) ainda não testa estabilidade multi-símbolo (só
  multi-período, no mesmo símbolo); o critério "supera o baseline" do
  relatório de aprovação de ML é avaliado como "edge positivo depois de
  custos" (baseline implícito = 0), não uma comparação direta contra a
  mesma estratégia sem o filtro de IA nas mesmas janelas — essa
  integração só existe de fato a partir da Fase 11. Documentado em
  `app/ml/approval.py`.
- Nas 4 novas estratégias (Fase 6): filtro de notícia de alto impacto
  (Estratégia A), confirmação por tick e as demais variantes de execução de
  rompimento — imediata, reteste, ordem stop (Estratégia C), e velocidade
  de tick + confirmação do timeframe superior (Estratégia E) ficam fora do
  escopo — dependem de calendário econômico (Fase 19), microestrutura de
  tick (Estratégia H) ou multi-timeframe (Estratégia I), nenhum ainda
  implementado. Documentado no docstring de cada estratégia.
- Motor por tick (Fase 7): execução parcial (exigiria profundidade de
  livro de ofertas, não garantida por todas as corretoras — ver Fase 2) e
  horário de mercado/calendário de sessão (nenhum calendário de feriados
  implementado ainda) ficam fora do escopo. Documentado em
  `app/backtesting/tick_engine.py`.
- `TickRepository.purge_older_than` sem `symbol_id` continua purgando
  globalmente (todos os símbolos) por design — a política de retenção do
  sistema (`TICK_RETENTION_DAYS`) é system-wide, não por símbolo. Passe
  `symbol_id` explicitamente para uma purga pontual de um único símbolo.
- Paper trading (Fase 10) só processa candles, sem latência de execução
  simulada (isso existe no backtest por tick, Fase 7). Ver
  `docs/paper-trading.md`.
- Executor em conta demo (Fase 11): sem preenchimento parcial monitorado
  (ordens a mercado retail são resolvidas sincronamente); sem
  `CLOSE_PENDING` ativo (todo fechamento vem do stop-loss/take-profit
  anexado ao pedido original, nunca de uma decisão do sistema); sem
  múltiplos símbolos/estratégias numa única invocação de `demo run`.
  `demo run` continua exclusivo de conta demo; operar em conta real é
  feito por `/dashboard/trading` escolhendo o modo REAL, sem comando de
  CLI equivalente. Ver `docs/execution.md`.
- Dashboard (Fase 12) é somente leitura, sem paginação de tabelas
  (limite fixo de 100 linhas), sem métricas de backtest/walk-forward
  persistidas (não existe uma tabela `backtest_runs` ainda) e sem
  atualização automática (cada página exige recarregar). Ver
  `docs/dashboard.md`.
- Detecção de drift (Fase 13): sem decisão automática a partir de um
  drift detectado (sempre uma recomendação manual); só drift univariado
  (feature por feature, não a estrutura de correlação entre elas);
  `monitor model`/`monitor feed` são comandos ad-hoc, sem agendador
  próprio que os rode periodicamente. Ver `docs/monitoring.md`.
- Fase 14 (modelos avançados — LSTM/Transformer/RL) fica **deliberadamente
  adiada**: o prompt mestre só autoriza considerá-la se os modelos
  tabulares mostrarem limitações reais em produção, e essa evidência
  ainda não existe (o sistema nunca rodou fora de testes automatizados
  com dados sintéticos). Condição de retomada documentada em
  `docs/development-phases.md`.
- Preflight (Fase 15): `python -m app.cli preflight check` só relata —
  não corrige nada sozinho e não tem agendador embutido (o runbook aponta
  para Agendador de Tarefas do Windows/cron externos). Nenhuma
  notificação por canal externo (e-mail/Telegram/Slack) existe quando uma
  checagem falha — só saída/código de saída da CLI e as páginas do
  dashboard.
- Fase 15 é a última do roteiro explícito de 15 fases do prompt mestre.
  `REAL_LOCKED`/`REAL_ENABLED` foram liberados **por decisão explícita do
  dono do sistema**, que escolheu um seletor DEMO/REAL simples em vez da
  maquinaria de confirmação multi-etapa do prompt mestre (chave de
  liberação, prazo de expiração, teto de perda diária) — essa maquinaria
  não existe. O que protege a operação real hoje é: a escada de modos sem
  atalhos, a coerência obrigatória entre modo e tipo de conta (nas duas
  direções) e os limites de risco de `/dashboard/trading`. Não há teto de
  perda que interrompa a conta real além do circuit breaker diário
  configurado.
