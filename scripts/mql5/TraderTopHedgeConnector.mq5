//+------------------------------------------------------------------+
//| TraderTopHedgeConnector.mq5                                       |
//| Conector de DADOS do Hedge AI. Nao envia ordens.                  |
//|                                                                   |
//| O QUE ELE E                                                       |
//|                                                                   |
//| Um EA que so conversa: le a cotacao, manda ao painel, recebe a    |
//| analise e publica no canal local para o indicador desenhar. Nao   |
//| ha `#include <Trade\Trade.mqh>`, nao ha `OrderSend`, nao ha       |
//| `CTrade` neste arquivo, e um teste no servidor cobra isso a cada  |
//| build. Anexar ao grafico e tao seguro quanto anexar um indicador. |
//|                                                                   |
//| POR QUE UM EA, E NAO O PROPRIO INDICADOR                          |
//|                                                                   |
//| `WebRequest()` NAO PODE ser chamada de um indicador: indicadores  |
//| rodam na thread de interface e a plataforma recusa. Um EA roda em |
//| thread propria e pode. Por isso o par: este fala com a rede, o    |
//| indicador desenha.                                                 |
//|                                                                   |
//| DEIXE-O NUM GRAFICO SEPARADO                                       |
//|                                                                   |
//| Um grafico so aceita um EA. Se voce ja roda outro EA no grafico   |
//| principal, abra um grafico qualquer (o ativo nem precisa ser o    |
//| mesmo) e anexe este ali: informe `SetupId` e `SymbolOverride`, e  |
//| ele trabalha sobre o ativo certo de qualquer janela. O indicador  |
//| vai no grafico que voce quer ver.                                 |
//|                                                                   |
//| Nesse caso informe `ChartIdOverride` com o MESMO valor nos dois,  |
//| senao cada um usaria o id do seu proprio grafico e eles falariam  |
//| em espacos diferentes do canal.                                    |
//|                                                                   |
//| TRES COISAS QUE FALHAM EM SILENCIO                                 |
//|                                                                   |
//| 1. A URL precisa estar liberada em Ferramentas > Opcoes >         |
//|    Expert Advisors > "Permitir WebRequest para as URLs listadas". |
//|    Sem isso `WebRequest` devolve -1 com erro 4014 e nada aparece. |
//|    E a causa numero um de "nao funciona".                          |
//| 2. "AutoTrading" precisa estar LIGADO. Nao porque este EA opere — |
//|    ele nao opera — mas porque com o botao desligado o terminal    |
//|    nao executa EA nenhum.                                          |
//| 3. O Strategy Tester NAO executa `WebRequest`. Testar aqui da     |
//|    sempre -1. Use conta demo real.                                 |
//+------------------------------------------------------------------+
#property copyright "AI Trader PRO"
#property version   "1.00"
#property strict
#property description "Conector de dados do Hedge AI. Le cotacao, consulta o painel e publica para o indicador. NAO envia ordens."

#include <TraderTop/HedgeChannel.mqh>
#include <TraderTop/HedgeJson.mqh>

//--- Conexao
input string ApiUrl           = "https://trader-top.navit.com.br"; // URL do painel (sem barra no fim)
input string ApiKey           = "";        // Chave do conector (painel > Hedge AI > Chaves)
input long   SetupId          = 0;         // Id do setup no painel
input string SymbolOverride   = "";        // Simbolo no painel (vazio = o do grafico)
input string ChartIdOverride  = "";        // Mesmo valor no indicador quando em graficos separados

//--- Ritmo
input int    IntervalSeconds  = 2;         // Intervalo entre snapshots (1-60)
input int    TimeoutMs        = 4000;      // Timeout de cada chamada
input int    MaxBackoffSeconds = 120;      // Teto da espera apos falhas

//--- Volatilidade enviada ao modelo
input int    AtrPeriod        = 14;        // Periodo do ATR
input ENUM_TIMEFRAMES AtrTimeframe = PERIOD_M15;  // Timeframe do ATR

#define CONNECTOR_VERSION "1.0.0"
#define SCHEMA_VERSION    1

//--- Estado
string   g_symbol      = "";
string   g_tag         = "";
long     g_chart       = 0;
int      g_atr_handle  = INVALID_HANDLE;
int      g_seq         = 0;
int      g_falhas      = 0;
datetime g_next_try    = 0;
bool     g_busy        = false;
bool     g_encerrado   = false;
string   g_erro        = "";

//+------------------------------------------------------------------+
int OnInit()
  {
   g_symbol = (StringLen(SymbolOverride) > 0) ? SymbolOverride : _Symbol;
   g_chart  = (StringLen(ChartIdOverride) > 0)
              ? StringToInteger(ChartIdOverride)
              : ChartID();
   g_tag = TTH_SessionTag(g_chart, g_symbol, SetupId);

   if(StringLen(ApiKey) == 0 || SetupId <= 0)
     {
      // Falha cedo e explicita: sem chave ou sem setup nenhuma consulta
      // funcionaria, e um grafico vazio nao diz por que esta vazio.
      g_erro = "Configure ApiKey e SetupId.";
      Print("TraderTopHedgeConnector: ", g_erro);
      PublicarEstado(TTH_ST_INVALID_KEY);
      return(INIT_SUCCEEDED);
     }

   if(StringFind(ApiUrl, "https://") != 0)
      Print("TraderTopHedgeConnector: AVISO — a chave viaja em claro sem HTTPS.");

   g_atr_handle = iATR(g_symbol, AtrTimeframe, AtrPeriod);
   if(g_atr_handle == INVALID_HANDLE)
      Print("TraderTopHedgeConnector: ATR indisponivel; o modelo pode ficar sem volatilidade.");

   EventSetTimer(MathMax(1, MathMin(60, IntervalSeconds)));
   Print("TraderTopHedgeConnector ", CONNECTOR_VERSION, " ativo. Sessao: ", g_tag);
   Enviar();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_atr_handle != INVALID_HANDLE)
      IndicatorRelease(g_atr_handle);

   // So as variaveis DESTA sessao. `GlobalVariablesDeleteAll(TTH_PREFIX)`
   // derrubaria as de outro grafico que continua rodando.
   TTH_ClearSession(g_tag);
  }

void OnTimer()
  {
   Enviar();
  }

//+------------------------------------------------------------------+
//| Nao fazemos rede em OnTick: o tick chega em rajada e empilharia  |
//| requisicoes. Todo o trafego sai do timer.                         |
//+------------------------------------------------------------------+
void OnTick()
  {
  }

//+------------------------------------------------------------------+
//| Um ciclo                                                          |
//+------------------------------------------------------------------+
void Enviar()
  {
   if(StringLen(ApiKey) == 0 || SetupId <= 0)
      return;
   if(g_encerrado)
      return;   // operacao acabou: parar de consultar e o correto
   if(g_busy)
      return;   // evita requisicoes sobrepostas quando a rede atrasa
   if(g_next_try > 0 && TimeCurrent() < g_next_try)
      return;

   MqlTick tick;
   if(!SymbolInfoTick(g_symbol, tick) || tick.bid <= 0 || tick.ask <= 0)
     {
      PublicarEstado(TTH_ST_WAITING);
      return;
     }

   g_seq++;
   string corpo = MontarCorpo(tick);

   g_busy = true;
   string resposta = "";
   int codigo = Http(ApiUrl + "/api/v1/hedge/snapshot", corpo, resposta);
   g_busy = false;

   if(codigo == 401 || codigo == 403)
     {
      g_erro = "Chave recusada (HTTP " + IntegerToString(codigo) + ").";
      PublicarEstado(TTH_ST_INVALID_KEY);
      Recuar();
      return;
     }

   if(codigo == 409)
     {
      g_erro = "Servidor mudou de contrato. Atualize o conector.";
      PublicarEstado(TTH_ST_MODEL_NA);
      Recuar();
      return;
     }

   if(codigo != 200 || StringLen(resposta) == 0)
     {
      g_erro = "Sem resposta do painel (HTTP " + IntegerToString(codigo) + ").";
      Recuar();
      // Desconectado so depois que o backoff ja cresceu: um engasgo de
      // rede nao pode apagar o desenho na primeira falha.
      if(g_falhas >= 3)
         PublicarEstado(TTH_ST_DISCONNECTED);
      return;
     }

   g_erro = "";
   g_falhas = 0;
   g_next_try = 0;
   Publicar(resposta);
  }

void Recuar()
  {
   g_falhas++;
   // Dobra ate o teto. Repetir no mesmo ritmo com o painel fora do ar so
   // enche o log e gasta a cota de requisicoes.
   int espera = (int)MathMin(MaxBackoffSeconds,
                             IntervalSeconds * MathPow(2, MathMin(6, g_falhas)));
   g_next_try = TimeCurrent() + espera;
   Print("TraderTopHedgeConnector: ", g_erro, " Nova tentativa em ", espera, "s.");
  }

//+------------------------------------------------------------------+
//| Corpo do POST                                                     |
//|                                                                   |
//| Enviamos a especificacao do contrato junto porque o servidor nao  |
//| tem como saber o `tick_value` da SUA conta: ele depende da moeda  |
//| da conta e do cambio do momento, e so o terminal logado conhece.  |
//+------------------------------------------------------------------+
string MontarCorpo(const MqlTick &tick)
  {
   double atr = 0.0;
   double buffer[];
   if(g_atr_handle != INVALID_HANDLE && CopyBuffer(g_atr_handle, 0, 0, 1, buffer) == 1)
      atr = buffer[0];

   double tickValue = SymbolInfoDouble(g_symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(g_symbol, SYMBOL_TRADE_TICK_SIZE);
   double ponto     = SymbolInfoDouble(g_symbol, SYMBOL_POINT);
   double volMin    = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_MIN);
   double volMax    = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_MAX);
   double volStep   = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_STEP);
   double contrato  = SymbolInfoDouble(g_symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   string moeda     = AccountInfoString(ACCOUNT_CURRENCY);

   int atrSegundos = PeriodSeconds(AtrTimeframe);

   string corpo = "{";
   corpo += "\"schema_version\":" + IntegerToString(SCHEMA_VERSION) + ",";
   corpo += "\"setup_id\":" + IntegerToString(SetupId) + ",";
   corpo += "\"terminal_id\":\"" + JsonEscape(IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN))) + "\",";
   corpo += "\"chart_id\":\"" + IntegerToString(g_chart) + "\",";
   corpo += "\"symbol\":\"" + JsonEscape(g_symbol) + "\",";
   corpo += "\"sequence\":" + IntegerToString(g_seq) + ",";
   corpo += "\"captured_at\":\"" + TimeToIso(TimeGMT()) + "\",";
   corpo += "\"server_time\":\"" + TimeToIso(TimeCurrent()) + "\",";
   corpo += "\"bid\":" + DoubleToString(tick.bid, 8) + ",";
   corpo += "\"ask\":" + DoubleToString(tick.ask, 8) + ",";
   corpo += "\"connector_version\":\"" + CONNECTOR_VERSION + "\",";
   corpo += "\"tick_value\":" + DoubleToString(tickValue, 8) + ",";
   corpo += "\"tick_size\":" + DoubleToString(tickSize, 10) + ",";
   corpo += "\"point\":" + DoubleToString(ponto, 10) + ",";
   corpo += "\"volume_min\":" + DoubleToString(volMin, 8) + ",";
   corpo += "\"volume_max\":" + DoubleToString(volMax, 8) + ",";
   corpo += "\"volume_step\":" + DoubleToString(volStep, 8) + ",";
   corpo += "\"contract_size\":" + DoubleToString(contrato, 4) + ",";
   corpo += "\"account_currency\":\"" + JsonEscape(moeda) + "\",";
   corpo += "\"spread\":" + DoubleToString(tick.ask - tick.bid, 8) + ",";
   // Volume de TICK, nao real: em forex o real raramente existe, e
   // trata-los como a mesma coisa daria ao modelo uma feature que mede
   // outra coisa. O servidor recebe os dois campos separados.
   corpo += "\"tick_volume\":" + IntegerToString((long)tick.volume) + ",";
   if(atr > 0.0)
     {
      corpo += "\"atr\":" + DoubleToString(atr, 8) + ",";
      corpo += "\"atr_period_seconds\":" + IntegerToString(atrSegundos) + ",";
     }
   corpo += "\"setup_version\":null";
   corpo += "}";
   return(corpo);
  }

string TimeToIso(const datetime t)
  {
   MqlDateTime d;
   TimeToStruct(t, d);
   return(StringFormat("%04d-%02d-%02dT%02d:%02d:%02d+00:00",
                       d.year, d.mon, d.day, d.hour, d.min, d.sec));
  }

//+------------------------------------------------------------------+
//| POST com a chave no HEADER.                                       |
//|                                                                   |
//| Nunca na URL: segredo em query string vai para log de servidor,   |
//| proxy e historico do terminal.                                     |
//+------------------------------------------------------------------+
int Http(const string url, const string body, string &out)
  {
   char post[];
   char result[];
   string headers = "Content-Type: application/json\r\nX-API-Key: " + ApiKey + "\r\n";
   string resultHeaders = "";

   int len = StringToCharArray(body, post, 0, StringLen(body), CP_UTF8);
   if(len > 0)
      ArrayResize(post, len - 1);   // tira o terminador que o servidor nao espera

   ResetLastError();
   int codigo = WebRequest("POST", url, headers, TimeoutMs, post, result, resultHeaders);

   if(codigo == -1)
     {
      int erro = GetLastError();
      if(erro == 4014)
         Print("TraderTopHedgeConnector: libere ", ApiUrl,
               " em Ferramentas > Opcoes > Expert Advisors > WebRequest.");
      else
         Print("TraderTopHedgeConnector: falha de rede (", erro, ").");
      return(-1);
     }

   out = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   return(codigo);
  }

//+------------------------------------------------------------------+
//| Resposta -> canal local                                           |
//+------------------------------------------------------------------+
void Publicar(const string json)
  {
   int schema = (int)JsonNumAt(json, "schema_version", 0, 0);
   if(schema != SCHEMA_VERSION)
     {
      g_erro = "Schema " + IntegerToString(schema) + " inesperado.";
      PublicarEstado(TTH_ST_MODEL_NA);
      return;
     }

   TTHFrame f;
   f.seq           = (double)g_seq;
   f.setup_version = JsonNumAt(json, "setup_version", 0, 0);
   f.state         = (double)EstadoDoTexto(JsonStrAt(json, "state", 0));
   f.objective     = (JsonStrAt(json, "objective", 0) == "FAVOR_BROKER_PROFIT")
                     ? TTH_OBJ_FAVOR_BROKER : TTH_OBJ_FAVOR_DESK;
   f.closed_reason = (double)MotivoDoTexto(JsonStrAt(json, "closed_reason", 0));

   int niveis = JsonObjectStart(json, "levels", 0);
   f.is_long = JsonBoolAt(json, "is_long", niveis) ? 1 : 0;
   f.entry   = JsonNumAt(json, "entry", niveis);
   f.take    = JsonNumAt(json, "take", niveis);
   f.stop    = JsonNumAt(json, "stop", niveis);

   int precos = JsonObjectStart(json, "price", 0);
   f.price = JsonNumAt(json, "current", precos);
   f.bid   = JsonNumAt(json, "bid", precos);
   f.ask   = JsonNumAt(json, "ask", precos);

   int prazo = JsonObjectStart(json, "deadline", 0);
   f.seconds_left = JsonNumAt(json, "seconds_remaining", prazo);

   // --- previsao. Ausente e `null`, nao zero: exibir "0%" afirmaria que
   // --- o evento nao acontece, que e diferente de nao haver estimativa.
   int prev = JsonObjectStart(json, "forecast", 0);
   bool temPrevisao = JsonBoolAt(json, "available", prev);
   f.has_forecast = temPrevisao ? 1 : 0;
   f.model_kind   = (double)ModeloDoTexto(JsonStrAt(json, "model_kind", prev));
   if(temPrevisao && !JsonIsNull(json, "take_first_pct", prev))
     {
      f.take_pct = JsonNumAt(json, "take_first_pct", prev);
      f.stop_pct = JsonNumAt(json, "stop_first_pct", prev);
      f.none_pct = JsonNumAt(json, "no_barrier_pct", prev);
     }
   else
     {
      f.take_pct = -1;   // -1 = sem estimativa. O indicador nao desenha.
      f.stop_pct = -1;
      f.none_pct = -1;
     }

   // --- financeiro, que degrada em separado da previsao
   int fin = JsonObjectStart(json, "financial", 0);
   bool temFin = JsonBoolAt(json, "available", fin);
   f.has_financial = temFin ? 1 : 0;
   f.basis_risk    = JsonBoolAt(json, "basis_risk", fin) ? 1 : 0;
   if(temFin)
     {
      f.lots     = JsonNumAt(json, "effective_lots", fin);
      f.residual = JsonNumAt(json, "residual_exposure", fin);
      int cenarios = JsonObjectStart(json, "scenarios", fin);
      f.net_take    = JsonScenarioNum(json, "DESK_TAKE", "net", cenarios);
      f.net_stop    = JsonScenarioNum(json, "DESK_STOP", "net", cenarios);
      f.net_timeout = JsonScenarioNum(json, "TIMEOUT", "net", cenarios);
     }
   else
     {
      f.lots = 0; f.residual = 0;
      f.net_take = 0; f.net_stop = 0; f.net_timeout = 0;
     }

   f.updated_at = (double)TimeCurrent();

   TTH_WriteFrame(g_tag, f);

   if((int)f.state == TTH_ST_CLOSED)
     {
      g_encerrado = true;
      Print("TraderTopHedgeConnector: operacao encerrada (",
            TTH_CloseLabel((int)f.closed_reason), "). Consultas interrompidas.");
     }
  }

//+------------------------------------------------------------------+
//| Publica so o estado, preservando o resto do quadro.               |
//|                                                                   |
//| Usado nas falhas: apagar os niveis a cada oscilacao de rede seria |
//| pior que manter o ultimo quadro com o estado dizendo que ele      |
//| envelheceu.                                                        |
//+------------------------------------------------------------------+
void PublicarEstado(const int estado)
  {
   TTH_Set(g_tag, "channel_ver", (double)TTH_CHANNEL_VER);
   double seq = TTH_Get(g_tag, "seq_end", 0) + 1;
   TTH_Set(g_tag, "seq_begin", seq);
   TTH_Set(g_tag, "state", (double)estado);
   TTH_Set(g_tag, "updated_at", (double)TimeCurrent());
   TTH_Set(g_tag, "seq_end", seq);
  }

//+------------------------------------------------------------------+
//| Texto -> codigo. As tabelas vivem no conector; o indicador so ve  |
//| numero.                                                            |
//+------------------------------------------------------------------+
int EstadoDoTexto(const string s)
  {
   if(s == "CONNECTED")         return(TTH_ST_CONNECTED);
   if(s == "MODEL_UNAVAILABLE") return(TTH_ST_MODEL_NA);
   if(s == "STALE_DATA")        return(TTH_ST_STALE);
   if(s == "DISCONNECTED")      return(TTH_ST_DISCONNECTED);
   if(s == "CLOSED")            return(TTH_ST_CLOSED);
   if(s == "INVALID_KEY")       return(TTH_ST_INVALID_KEY);
   return(TTH_ST_WAITING);
  }

int MotivoDoTexto(const string s)
  {
   if(s == "DESK_TAKE") return(TTH_CLOSE_TAKE);
   if(s == "DESK_STOP") return(TTH_CLOSE_STOP);
   if(s == "TIMEOUT")   return(TTH_CLOSE_TIMEOUT);
   return(TTH_CLOSE_NONE);
  }

int ModeloDoTexto(const string s)
  {
   if(s == "THEORETICAL_BASELINE") return(TTH_MODEL_BASELINE);
   if(s == "TREND_SCORE")          return(TTH_MODEL_TREND);
   if(s == "TRAINED")              return(TTH_MODEL_TRAINED);
   return(TTH_MODEL_NONE);
  }
