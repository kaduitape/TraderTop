# Hedge AI

Cobertura entre uma operação de referência **na mesa** e a operação oposta
**na corretora**, com indicador ao vivo no MetaTrader 5.

O módulo responde duas perguntas que são deliberadamente separadas:

1. **Quanto dá?** — conta determinística, nas duas contas, por cenário.
2. **Qual barreira vem primeiro?** — estimativa, com a origem declarada.

Misturar as duas produziria um número com cara de esperança matemática que
não é, porque a segunda ainda não foi validada neste projeto.

> **Não envia, altera nem cancela ordens.** O EA é um canal de dados. Um
> teste estrutural reprova o build se `app/hedge/`, a rota ou os `.mq5`
> passarem a importar execução.

---

## Arquitetura

```
MetaTrader 5                          Trader Top
┌──────────────────────┐              ┌────────────────────────────┐
│ TraderTopHedge       │  HTTPS       │ POST /api/v1/hedge/snapshot │
│ Connector.mq5  (EA)  │─────────────▶│   X-API-Key                 │
│  · lê bid/ask, ATR   │              │   ├─ motor financeiro       │
│  · POST por timer    │◀─────────────│   ├─ motor de previsão      │
│  · sem OnCalculate   │   análise    │   └─ JEV (observação)       │
└──────────┬───────────┘              └────────────────────────────┘
           │ variáveis globais do terminal (canal local)
           ▼
┌──────────────────────┐
│ TraderTopHedge       │
│ Indicator.mq5        │  desenha linhas, regiões e percentuais
└──────────────────────┘
```

**Por que dois arquivos.** `WebRequest()` não pode ser chamada de um
indicador: indicadores rodam na thread de interface e a plataforma recusa.
O EA roda em thread própria e pode. Daí o par.

**Por que variáveis globais do terminal** como canal, e não arquivo: elas
são atômicas por variável, não tocam disco a cada 1–2 s e o terminal as
limpa sozinho. Só guardam `double` — que aqui é virtude, porque tudo que o
indicador desenha já é número, e ele nunca exibe texto vindo do servidor.

A consistência de quadro usa `seq_begin` / `seq_end`: o EA escreve a
sequência antes e depois dos campos; o indicador lê ao contrário e só
aceita quando os dois batem. Leitura no meio de uma escrita é detectada, e
o quadro anterior permanece.

**Isolamento de sessão.** Toda variável leva o hash de
`conta + gráfico + símbolo + setup`. Dois gráficos do mesmo ativo são duas
sessões — o caso normal, já que o conector costuma ficar num gráfico
separado para não disputar o gráfico principal com outro EA.

---

## Motor financeiro

### As quatro grandezas que não podem se misturar

| | o que é |
|---|---|
| `point` | menor incremento **representável** (`10^-digits`) |
| `tick_size` | menor incremento **negociável** — no MNQ vale 0,25 contra 0,01 do point |
| `contract_size` | unidades do ativo em 1,0 lote |
| lote / contrato | "1 lote" no MT5 e "1 contrato" na mesa raramente são a mesma quantidade |

A única ponte entre preço e dinheiro:

```
dinheiro = (variação_de_preço / tick_size) × tick_value × volume
```

`tick_value` já embute `contract_size`. Dividir também por ele conta duas
vezes — e o resultado continua parecendo dinheiro.

### Cobertura é por exposição

Dois instrumentos lineares estão cobertos quando movem **o mesmo dinheiro
para o mesmo movimento de preço**. Igualar número de contratos a número de
lotes é o erro clássico.

```
lotes = (exposição_da_mesa × cobertura) / (exposição_por_lote × price_ratio)
```

O lote é arredondado **para baixo** ao `volume_step`: arredondar para cima
cobriria mais do que foi pedido, o que é uma posição direcional que ninguém
decidiu tomar. A diferença aparece como **exposição residual**, e o painel
mostra o lote pedido *e* o efetivo.

Abaixo do `volume_min` o resultado é **zero**, não o mínimo — entregar o
mínimo quando o cálculo pediu metade dobraria a exposição em silêncio.

### Instrumentos diferentes

Quando os símbolos não são equivalentes, `price_ratio` é **obrigatório** e
o resultado vem marcado com `basis_risk`. O sistema não assume equivalência
entre futuro e CFD: os dois podem descolar, e aí a cobertura "perfeita" no
papel vira exposição real.

### Indisponível é um resultado

Faltando `tick_value`, moeda ou mapeamento, o motor devolve
`available: false` com o motivo. Não estima, não aproxima, não preenche com
zero.

Moedas diferentes entre as pernas **recusam o cálculo** em vez de converter:
sem taxa confiável, somar BRL com USD dá um número sem significado.

---

## Probabilidades

O evento é:

> Dado o estado atual e o prazo restante, **qual barreira será atingida
> primeiro**?

Três desfechos mutuamente exclusivos que somam 100%: take da mesa primeiro,
stop da mesa primeiro, nenhuma até o prazo.

### Quatro coisas que parecem a mesma

1. Probabilidade de **tocar** um nível — ignora o outro; quase sempre maior.
2. Probabilidade de tocá-lo **antes do outro** — é esta.
3. **Pontuação de tendência** — ordena, não soma 1.
4. **Confiança do modelo** — fala do modelo, não do mercado.

### O modelo que acompanha esta versão

`THEORETICAL_BASELINE` (`baseline-2barrier-1.0.0`). Duas contas clássicas
sobre um passeio aleatório sem tendência: a série de sobrevivência com duas
fronteiras absorventes, e a divisão `distância relativa` para quem vem
primeiro.

Hipóteses, que viajam junto com o número na resposta da API:

- sem tendência — mercado em tendência clara viola isto;
- volatilidade constante no horizonte;
- movimento contínuo — gap pula barreira sem tocá-la;
- a divisão da massa absorvida usa a proporção de horizonte **infinito**,
  o que favorece ligeiramente a barreira mais distante em prazos curtos.

**Nenhum modelo treinado acompanha esta versão**, e um teste reprova se
algum se declarar `TRAINED` sem passar pelos critérios de validação. Sem
volatilidade estimável a resposta é `INSUFFICIENT_DATA` com as três
probabilidades em `null` — nunca zero, que seria lido como "estimamos que
não acontece".

### Depois da barreira

A previsão **encerra**. O setup é fechado com o motivo (`DESK_TAKE`,
`DESK_STOP`, `TIMEOUT`) e os ciclos seguintes respondem `CLOSED`.

O toque é julgado por **preço executável**: comprado sai no bid, vendido no
ask. Usar o meio declararia barreira tocada antes de o preço de saída ter
chegado — meio tick de otimismo que, repetido, vira um histórico melhor do
que foi.

### Caminho para um modelo treinado

Cada análise é gravada em `hedge_analyses` com `setup_version`, sequência,
instante de captura e o motivo quando não houve previsão. É a base para,
mais adiante, avaliar Brier score e calibração contra o baseline — com
divisão cronológica e sem contar atualizações da mesma operação como
observações independentes. Até existir essa avaliação, nada muda.

---

## Estados

| Estado | Significa | Desenha? |
|---|---|---|
| `CONNECTED` | snapshot recente aceito | sim, como sinal atual |
| `WAITING_DATA` | sessão aberta, sem snapshot | não |
| `MODEL_UNAVAILABLE` | servidor respondeu, previsão não existe | níveis sim, percentuais não |
| `STALE_DATA` | passou da validade | último valor como **histórico** |
| `DISCONNECTED` | sem dados há mais de 90 s | mantém o desenho, marca a idade |
| `CLOSED` | barreira ou prazo | desfecho, sem percentual |
| `INVALID_KEY` | chave recusada | não |

Só `CONNECTED` autoriza a tela a parecer sinal atual.

---

## Contrato da API

Base: `/api/v1/hedge`. Autenticação por `X-API-Key` no **header** — nunca
na URL, que vai para log de servidor, proxy e histórico.

Caminho versionado (e não só um campo no corpo) porque o cliente é um
`.mq5` **compilado** na máquina do operador: atualizar o servidor não
atualiza o terminal dele.

| Endpoint | Escopo | Para quê |
|---|---|---|
| `GET /ping` | `hedge:telemetry` | validar conexão e identificar a chave |
| `GET /setups` | `hedge:setup:read` | setups **do dono da chave** |
| `POST /snapshot` | `telemetry` + `analysis` | enviar cotação, receber análise |
| `GET /analysis/{id}` | `hedge:analysis` | última análise, com a própria idade |
| `GET /history/{id}` | `hedge:analysis` | histórico |

Exemplos completos de requisição e resposta: `docs/hedge-api-exemplos.json`.

### Regras do contrato

- **`null` nunca é zero.** Probabilidade ausente é `null`.
- **Blocos independentes.** `forecast` e `financial` têm `available`
  próprio: sem `tick_value` o financeiro para e a previsão continua.
- **`jev` fica fora da previsão**, em modo observação.
- **Isolamento.** Setup de outro usuário responde **404 idêntico** ao de
  setup inexistente: distinguir contaria a uma chave vazada quais ids
  existem.

---

## Chaves

Geradas em **Hedge AI → Conexões e chaves**. 256 bits de `secrets`, hash
SHA-256 no banco, prefixo `tt_` visível para identificar sem revelar.

- O segredo aparece **uma vez**, na criação. Não há recuperação.
- Escopos fixos: telemetria, leitura de setup e análise. Nunca ordem.
- `created_by_user_id` é a autorização: chave sem dono **falha fechado**.
- Revogação é imediata (validação consulta o banco a cada requisição).
- Limite de 4 req/s por chave com rajada de 20, suficiente para vários
  gráficos a 1–2 s. Em memória, por processo.

**Onde guardar.** No campo `ApiKey` do EA, no próprio terminal. **Não** em
arquivos `.set`, perfis ou templates compartilhados — eles viajam em claro.
Uma chave por máquina: perder o notebook custa revogar uma só.

A chave do **JEV** fica só no backend (`JEV_API_KEY` no `.env`), sem campo
no painel e sem linha no banco.

---

## Instalação no MetaTrader

1. Arquivo → Abrir pasta de dados → `MQL5`.
2. Copie:
   - `TraderTopHedgeConnector.mq5` → `MQL5/Experts/`
   - `TraderTopHedgeIndicator.mq5` → `MQL5/Indicators/`
   - `Include/TraderTop/*.mqh` → `MQL5/Include/TraderTop/`
3. Compile os dois no MetaEditor (**F7**).
4. Ferramentas → Opções → Expert Advisors → marque **"Permitir WebRequest
   para as URLs listadas"** e adicione `https://trader-top.navit.com.br`.
   Sem isso `WebRequest` devolve −1 com erro 4014 e nada aparece. **É a
   causa número um de "não funciona".**
5. Ligue o **AutoTrading**. O EA não opera — mas com o botão desligado o
   terminal não executa EA nenhum.
6. Anexe o **conector** (`ApiKey`, `SetupId`) e o **indicador**
   (mesmo `SetupId`).

### Conector em gráfico separado

Um gráfico aceita um EA só. Se o principal já tem outro, abra qualquer
gráfico e anexe o conector ali, informando `SymbolOverride`. Nesse caso
informe `ChartIdOverride` com o **mesmo valor nos dois** — senão cada um
usaria o id do próprio gráfico e eles falariam em espaços diferentes do
canal.

---

## Indicador

Desenha entrada, take e stop da mesa, as regiões entre o preço e cada
barreira, os percentuais em corpo grande, o prazo restante, a última
atualização e o estado.

**Cores e objetivo.** No modo *favorecer corretora* a região do **stop**
fica verde e a do **take** vermelha, porque é o stop que se quer. No modo
*favorecer mesa*, inverte. Os **nomes dos níveis nunca mudam** — só a cor
diz se aquele desfecho é bem-vindo. A decisão vem do servidor (`colors`),
não do indicador: duas implementações da mesma regra divergem.

**Onde cada região fica** depende de comprado/vendido, nunca de cima/baixo
fixo. Numa venda o take fica abaixo da entrada.

**Transparência**: `OBJ_RECTANGLE` com `FILL` + `BACK` e cor misturada ao
fundo do gráfico (`ZoneOpacity`), porque o MQL5 não tem alfa por objeto.
Canvas daria alfa real ao custo de reimplementar a escala de preços — e
duas escalas divergem.

**Objetos** levam prefixo próprio por sessão, são **atualizados** e não
recriados a cada tick, e ao remover o indicador só os dele são apagados.

---

## Teste em conta demo

O **Strategy Tester não executa `WebRequest`** — ali o conector devolve
sempre −1. A separação é:

| Camada | Como |
|---|---|
| Cálculos | `pytest tests/unit/hedge/` — sem rede |
| Contrato | `pytest tests/integration/test_hedge_api.py` |
| HTTP real | conta **demo**, passo a passo abaixo |

1. Crie um setup em **Hedge AI → Setups** com um símbolo que o terminal
   demo tenha.
2. Gere uma chave e copie o segredo.
3. Autorize a URL e anexe o conector com `SetupId` e `ApiKey`.
4. Em **Ao vivo**, a sessão deve aparecer com `CONNECTED` e a sequência
   subindo.
5. Anexe o indicador: linhas e percentuais aparecem em poucos segundos.
6. Para verificar o encerramento, use um setup com barreiras próximas do
   preço e observe o estado virar `CLOSED` com o motivo.

Se nada aparece, a ordem de checagem é: (1) URL autorizada, (2) AutoTrading
ligado, (3) `SetupId` existe e é seu, (4) aba **Experts** do terminal.

---

## Limitações reais

- **Nenhum modelo treinado.** O que há é um baseline teórico, declarado
  como tal na API e na tela. As probabilidades não foram validadas contra
  o comportamento deste ativo.
- **Os `.mq5` não foram compilados** neste ambiente (sem MetaEditor). A
  verificação feita foi balanceamento de delimitadores com lexer próprio e
  a garantia estrutural de ausência de ordens. **A compilação permanece
  pendente — o F7 é do operador.**
- **O risco de base é apontado, não medido.** O sistema avisa que dois
  instrumentos diferentes podem descolar; não estima quanto.
- **`tick_value` vem do terminal**, não do banco: depende da conta logada
  e do câmbio do momento. Sem ele, o financeiro fica indisponível.
- **Conversão cambial entre pernas não é feita.** Moedas diferentes
  recusam o cálculo em vez de converter com uma taxa de procedência
  incerta.
- **O limite de taxa é por processo.** Com vários workers o teto efetivo é
  o limite vezes o número de processos.
- **JEV em modo observação**: classifica contexto, não influencia número.
- **Acompanhamento sem ticket do MT5** é uma anotação do operador. O
  sistema **não afirma** acompanhar uma posição cuja existência não
  consegue verificar.
- **Resultado de mesa não é saque.** O que o módulo calcula é resultado
  operacional; taxas, repasse e regras de avaliação da mesa ficam fora.
