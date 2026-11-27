# Runbook Operacional — Fase 15

Procedimentos práticos para instalar, operar e reagir a incidentes.
Para arquitetura/decisões de design, ver os demais documentos em
`docs/` — este arquivo é deliberadamente só "o que fazer", não "por quê".

## 1. Primeira instalação

```powershell
# 1. Instalar dependencias (";.[mt5]" so no Windows, com terminal MT5 real)
pip install -e ".[mt5]"

# 2. Copiar e preencher o .env (nunca versionar o .env real)
copy .env.example .env
# edite .env: gere APP_SECRET_KEY real (openssl rand -hex 32), preencha
# DB_* com um MySQL 8 real quando disponivel, preencha MT5_* se for
# coletar dados de um terminal MetaTrader 5 instalado nesta maquina.

# 3. Aplicar as migrations
alembic upgrade head

# 4. Validar o ambiente de ponta a ponta
python -m app.cli preflight check
```

## 1b. Instalação via Docker (API/dashboard/backtest/ML/monitoring)

```powershell
copy .env.example .env
# preencha, no minimo: APP_SECRET_KEY, DB_ROOT_PASSWORD, DB_NAME, DB_USER,
# DB_PASSWORD (nao precisam bater com nenhum MySQL existente -- o compose
# cria um MySQL 8 novo, do zero, com essas credenciais).

docker compose up -d --build
docker compose logs -f app   # acompanha migrations + preflight + uvicorn subindo
```

O `entrypoint.sh` do container roda `alembic upgrade head` e depois
`preflight check` (nao-bloqueante — só relata) antes de subir o
`uvicorn`. A API/dashboard fica em `http://localhost:8000`.

**O que funciona dentro do container**: API (`/health`, `/api/auth/*`),
dashboard (`/login`, `/dashboard/*`), e qualquer comando de
`python -m app.cli` que **não** dependa de conexão real ao MetaTrader —
`preflight check`, `quality check`, `features build`, `backtest *`,
`ml *`, `monitor *`, `mode show/set`, `paper status`, `demo status`.

**O que NÃO funciona dentro do container** (e nunca vai funcionar num
container Linux): `mt5 check`, `mt5 symbols`, `collect candles`/
`collect ticks`, `paper run`, `demo run` — todos dependem do pacote
`MetaTrader5`, que só publica wheel para Windows (fala com o terminal
via DLL/named pipe). Dentro do container esses comandos falham com uma
mensagem clara (`ERRO: Pacote 'MetaTrader5' nao instalado...`), nunca
uma resposta inventada. Rode-os no host Windows, com o terminal MT5
instalado/autenticado e `pip install -e ".[mt5]"`, apontando `DB_HOST`
para a porta publicada do MySQL do compose (adicione `ports: ["3306:3306"]`
ao serviço `db` se precisar acessá-lo de fora do compose).

```powershell
docker compose exec app python -m app.cli preflight check
docker compose down          # para os containers, mantem o volume do MySQL
docker compose down -v       # para e APAGA os dados do MySQL (irreversivel)
```

`preflight check` deve reportar `OK`/`AVISO` em tudo antes de prosseguir
— qualquer `FALHA` (segredo padrão, banco inacessível, migrations
pendentes, diretório sem permissão de escrita) precisa ser corrigida
primeiro. `AVISO` em credenciais MT5 é aceitável se você só for rodar
backtests/ML sobre dados já coletados, sem conectar a um terminal.

## 2. Rotina de operação (paper/demo)

```powershell
# Ver o modo atual
python -m app.cli mode show

# Avancar um passo por vez (nunca pula estado)
python -m app.cli mode set DATA_ONLY
python -m app.cli mode set BACKTEST
python -m app.cli mode set REPLAY
python -m app.cli mode set PAPER

# Paper trading (nunca envia ordem real)
python -m app.cli paper run --symbol EURUSD --timeframe M1 --strategy ema_crossover --iterations 999999 --poll-seconds 30
```

Antes de avançar para `DEMO`, revise `/dashboard` (modo atual, paper
trades recentes) e `/dashboard/drift` — se algum modelo em uso mostrar
`CRITICAL`, resolva antes de avançar.

```powershell
python -m app.cli mode set DEMO   # so alcancavel a partir de PAPER

python -m app.cli demo run --symbol EURUSD --timeframe M1 --strategy ema_crossover `
    --risk-per-trade-pct 1.0 --max-daily-loss-pct 3.0 --iterations 999999 --poll-seconds 30
```

`demo run` verifica a cada iteração que a conta conectada é
efetivamente demo (`AccountSnapshot.is_demo`), mesmo que o modo do
sistema já diga `DEMO` — dupla checagem, nunca uma única flag.

**`REAL_LOCKED`/`REAL_ENABLED` permanecem bloqueados incondicionalmente**
— não existe um procedimento operacional para eles ainda, porque a
maquinaria de segurança que o prompt mestre exige (chave de liberação,
confirmação manual multi-etapa, prazo de expiração, valor máximo diário)
não está implementada.

## 3. Checagens periódicas recomendadas

```powershell
# Saude do feed de um simbolo (roda manualmente ou via agendador externo)
python -m app.cli monitor feed --symbol EURUSD --timeframe M1

# Drift de um modelo em uso, contra um dataset recente
python -m app.cli ml build-dataset --symbol EURUSD --timeframe M1 --strategy ema_crossover --out datasets/recente.csv
python -m app.cli monitor model --recent-dataset datasets/recente.csv
```

Não existe um agendador embutido nesta fase — use o Agendador de Tarefas
do Windows (ou uma tarefa cron, se operando em Linux) para rodar esses
comandos periodicamente. Ambos retornam código de saída `1` quando
encontram um problema, adequados para scripts de monitoramento externo.

## 4. Resposta a incidentes

### `EMERGENCY_STOP` foi acionado (manual ou pelo motor de risco)

1. Pare qualquer `paper run`/`demo run` em execução (Ctrl+C).
2. Rode `python -m app.cli mode show` para confirmar o modo atual.
3. Investigue a causa antes de qualquer coisa: `/dashboard/audit-log`
   mostra quem/o que acionou a transição.
4. **Nunca** pule direto para `PAPER`/`DEMO` na recuperação —
   `EMERGENCY_STOP` só permite voltar para `DISABLED`
   (`app.core.system_mode`), e dali é preciso avançar de novo, um passo
   por vez, revalidando cada estágio.

### Drift `CRITICAL` detectado (`/dashboard/drift` ou `monitor model`)

1. Não é uma parada automática — o sistema continua operando com o
   modelo atual até uma decisão manual.
2. Revise `entry.metrics` do modelo (`ml evaluate --version <v>`) contra
   o que o comando `monitor model` reportou.
3. Opções: registrar e aprovar uma nova versão treinada com dados mais
   recentes (`ml train --approve`), ou reverter para uma versão anterior
   (`ModelRegistry.set_current`, hoje só via código/CLI direta — não há
   comando dedicado ainda).

### MT5 desconectado / feed atrasado (`monitor feed` retorna `FALHA`)

1. Confirme que o terminal MetaTrader 5 está aberto e autenticado nesta
   máquina.
2. Rode `python -m app.cli mt5 check` para um diagnóstico direto da
   conexão (nunca inventa uma resposta — reporta exatamente o que
   `MetaTrader5.last_error()` devolveu).
3. `paper run`/`demo run` já recusam operar com feed atrasado
   (`app.risk.feed_health`) — não é necessário parar manualmente, mas
   nenhuma nova posição abre até o feed normalizar.

### Banco de dados inacessível

1. `python -m app.cli preflight check` confirma e isola o problema
   (conexão vs. migrations pendentes).
2. Nenhum comando desta CLI tenta reconectar automaticamente ao banco —
   corrija a conexão e rode o comando de novo.

### Não consigo entrar no painel

A tela de login diz "Usuário ou senha inválidos" para quatro causas
diferentes — de propósito, para não confirmar a um estranho qual usuário
existe. Do lado do servidor as quatro se separam. No Docker, prefixe os
comandos com `docker compose exec app`.

1. **Veja o que existe.** `python -m app.cli user list` mostra usuário,
   e-mail, papéis e se a conta está ativa.
2. **Tabela vazia** → não há o que redefinir; crie o primeiro admin:
   `python -m app.cli user create --username admin --email voce@exemplo.com --admin`
3. **Usuário existe, senha esquecida** →
   `python -m app.cli user reset-password --username admin`
4. **Conta inativa** → a senha nova não faz o login voltar sozinha; use
   `reset-password --username admin --activate`.

A senha é sempre pedida no terminal, nunca passada como argumento:
argumento aparece no histórico do shell e no `ps` de qualquer usuário da
máquina. Ela é gravada com hash, e o audit log registra **que** houve
troca — jamais o valor.

`DASHBOARD_AUTH_DISABLED=true` também abre o painel, mas é bypass de
desenvolvimento: libera todas as páginas sem login para quem alcançar a
porta. Nunca deixe ligado numa VPS.

## 5. Backup e retenção

- **Banco de dados**: sem automação de *backup* nesta fase — use
  `mysqldump` (MySQL 8) periodicamente.
- **Retenção**: automática desde a Fase 20, numa thread dentro do container
  `app` (ver `docs/data-model.md` seção 3.1). Padrões: ticks 7 dias,
  `data_quality_events` 14 dias (90 para CRITICAL), a cada 24h.

  **Primeira limpeza, contra um acúmulo que já existe.** A rotina
  automática limpa no máximo 2 milhões de linhas por execução, então um
  acúmulo de meses levaria vários dias. Para resolver de uma vez, com o
  mercado fechado:

  ```bash
  docker compose exec app python -m app.cli data retention --dry-run
  docker compose exec app python -m app.cli data retention --max-chunks 100000
  docker compose exec app python -m app.cli data retention --optimize
  ```

  O `--optimize` é o passo que devolve espaço ao sistema de arquivos:
  `DELETE` sozinho libera páginas dentro do `.ibd` e **`df` não se mexe**.
  Ele reconstrói a tabela, precisa de espaço livre da ordem do tamanho dela
  e **bloqueia escrita** enquanto roda — por isso não está na rotina
  automática. Confira o tamanho antes e depois:

  ```sql
  SELECT table_name,
         ROUND(data_length/1024/1024) AS dados_mb,
         ROUND(index_length/1024/1024) AS indices_mb,
         table_rows
    FROM information_schema.tables
   WHERE table_schema = DATABASE()
   ORDER BY data_length + index_length DESC;
  ```
- **Artefatos de modelo** (`models/`): cada versão é um arquivo
  `<versao>.joblib` + `<versao>_test.csv` imutável — nenhum é
  sobrescrito, só `manifest.json` muda (o ponteiro `current`). Faça
  backup do diretório inteiro periodicamente; nunca edite `manifest.json`
  manualmente.
- **Datasets** (`datasets/`): reproduzíveis a partir de candles já
  coletadas (`ml build-dataset`) — não são, estritamente, dados que
  precisem de backup, mas evitam ter que reconstruir tudo.
- **Logs** (`logs/`): sem rotação automática configurada nesta fase —
  monitore o tamanho do diretório manualmente ou configure rotação no
  nível do SO/orquestrador escolhido para produção.

## 6. Lembrete das regras inegociáveis (prompt mestre, seção 2)

Sem martingale/soros/grade infinita, sem operação sem stop-loss, sem uso
de dados futuros no treino, sem backtest sem custos, sem ocultar perdas,
sem escolher estratégia só pelo lucro líquido, sem credenciais no
código, **sem ativação automática de conta real**. Nenhum procedimento
deste runbook contorna essas regras — se um passo aqui parecer exigir
isso, pare e reavalie antes de prosseguir.
