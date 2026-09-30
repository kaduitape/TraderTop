//+------------------------------------------------------------------+
//| AITraderPulse.mq5                                                 |
//| Desenha no grafico do MetaTrader a analise do AI Trader PRO.      |
//|                                                                   |
//| POR QUE ISTO E UM EXPERT ADVISOR, E NAO UM INDICADOR              |
//|                                                                   |
//| `WebRequest()` NAO PODE ser chamada de um indicador. Indicadores  |
//| rodam na thread de interface do terminal, e uma chamada de rede   |
//| ali congelaria a interface inteira — a plataforma simplesmente    |
//| recusa. Um EA roda em thread propria e pode.                      |
//|                                                                   |
//| Entao isto e um EA que se comporta como indicador: ele desenha, e |
//| SO desenha. Nao ha `#include <Trade\Trade.mqh>`, nao ha           |
//| `OrderSend`, nao ha `CTrade` neste arquivo. Anexar ao grafico e   |
//| tao seguro quanto anexar um indicador — a diferenca e que este    |
//| consegue falar com o servidor.                                    |
//|                                                                   |
//| TRES COISAS QUE FALHAM EM SILENCIO SE FOREM IGNORADAS             |
//|                                                                   |
//| 1. A URL precisa estar liberada em Ferramentas > Opcoes >         |
//|    Expert Advisors > "Permitir WebRequest para as URLs listadas". |
//|    Sem isso `WebRequest` devolve -1 com erro 4014 e NADA e        |
//|    desenhado. E a causa numero um de "nao funciona".              |
//|                                                                   |
//| 2. "AutoTrading" precisa estar LIGADO no terminal. Nao porque     |
//|    este EA opere — ele nao opera — mas porque com o botao         |
//|    desligado o terminal nao executa EA nenhum.                    |
//|                                                                   |
//| 3. Os precos vem do servidor no simbolo do PAINEL. Se a corretora |
//|    usa sufixo (EURUSDm, EURUSD.raw), informe em SymbolOverride o  |
//|    nome como ele existe no painel — desenhar niveis de EURUSD num |
//|    grafico de EURUSDm colocaria as zonas no lugar errado sem      |
//|    nenhum aviso.                                                  |
//+------------------------------------------------------------------+
#property copyright "AI Trader PRO"
#property version   "1.00"
#property strict
#property description "Desenha zonas de entrada, take, stop e risco vindas do AI Trader PRO. Nao envia ordens."

//--- Conexao
input string ApiUrl          = "http://127.0.0.1:8000"; // URL do painel (sem barra no fim)
input string ApiKey          = "";                      // Chave de API (Configuracoes > Chaves)
input string SymbolOverride  = "";                      // Simbolo no painel (vazio = o do grafico)
input string TimeframeOverride = "";                    // M1..MN1 (vazio = o do grafico)

//--- Analise
input int    TakeTicks       = 20;      // Take desejado, em ticks
input string Direction       = "AUTO";  // AUTO | COMPRA | VENDA
input int    RefreshSeconds  = 15;      // Intervalo de consulta

//--- Desenho
input bool   ShowHeatZones   = true;    // Faixas de confluencia do mapa de calor
input bool   ShowRiskArea    = true;    // Area alem do stop
input bool   ShowPanel       = true;    // Legenda no canto do grafico
input int    ZoneTransparency = 85;     // 0-100 (maior = mais transparente)

//--- Alertas
input bool   AlertOnReady    = true;    // Avisar quando a entrada ficar pronta
input bool   PushOnReady     = false;   // Tambem enviar push (MetaQuotes ID)

//--- Classificacao visual (opcional, vem do servidor)
input bool   ShowSignalAI    = true;    // Mostrar a leitura da IA, quando o servidor enviar

#define PREFIX "AITP_"
#define CONTRACT_SUPPORTED 1   // versao do contrato da API que este arquivo entende
#define MAX_ZONES 64
#define MAX_REASON_CODES 3     // quantos motivos cabem na legenda sem virar sujeira

//--- Estado
bool     g_enabled      = true;
bool     g_last_ok      = false;
bool     g_busy         = false;
double   g_tick_size    = 0.0;
int      g_version      = 0;
string   g_status       = "";
string   g_status_ant   = "";
datetime g_next_try     = 0;
int      g_falhas       = 0;
string   g_headline     = "iniciando...";
string   g_detail       = "";
string   g_error        = "";
datetime g_last_fetch   = 0;
string   g_symbol       = "";
string   g_timeframe    = "";

//--- Classificacao visual. Vazio = o servidor nao enviou, ou enviou
//--- available=false. Os dois casos desenham a mesma coisa: nada.
string   g_ai_state     = "";
double   g_ai_conf      = 0.0;
bool     g_ai_review    = false;
string   g_ai_reasons   = "";

//+------------------------------------------------------------------+
//| Ciclo de vida                                                     |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_symbol    = (StringLen(SymbolOverride) > 0) ? SymbolOverride : _Symbol;
   g_timeframe = (StringLen(TimeframeOverride) > 0) ? TimeframeOverride : PeriodToName(_Period);

   if(StringLen(ApiKey) == 0)
     {
      // Falha cedo e explicita: sem chave nenhuma consulta funcionaria, e
      // um grafico vazio nao diz por que esta vazio.
      g_error = "Configure ApiKey (painel > Configuracoes > Chaves de API).";
      DrawPanel();
      Print("AITraderPulse: ", g_error);
      return(INIT_SUCCEEDED);
     }

   CreateToggleButton();
   EventSetTimer(MathMax(3, RefreshSeconds));
   Fetch();   // nao esperar o primeiro timer
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   ClearObjects();
   Comment("");
   ChartRedraw();
  }

void OnTimer()
  {
   Fetch();
  }

//+------------------------------------------------------------------+
//| Clique no botao de ligar/desligar                                 |
//+------------------------------------------------------------------+
void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
  {
   if(id == CHARTEVENT_CHART_CHANGE)
     {
      // Rolar ou dar zoom nao muda a analise, mas muda ONDE ela cabe na
      // tela. Os retangulos sao ancorados na primeira barra visivel; sem
      // reancorar, arrastar o grafico deixava as zonas para tras ate o
      // proximo ciclo. Reancorar e local — nao gasta uma consulta.
      Reanchor();
      return;
     }

   if(id != CHARTEVENT_OBJECT_CLICK || sparam != PREFIX + "toggle")
      return;

   if(g_busy)
     {
      // `WebRequest` bloqueia a thread do EA; o clique nao se perde, so
      // espera. Dizer isso e melhor que um botao que parece morto.
      Comment("AITraderPulse: consultando o painel, aguarde...");
      return;
     }

   // O botao volta sozinho ao estado nao-pressionado: quem manda no rotulo
   // e a RESPOSTA do servidor, nao o clique. Se a chamada falhar, o botao
   // nao pode ficar mostrando um estado que o servidor nao tem.
   ObjectSetInteger(0, PREFIX + "toggle", OBJPROP_STATE, false);

   if(Toggle(!g_enabled))
      Fetch();
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Rede                                                              |
//+------------------------------------------------------------------+
string BuildUrl()
  {
   return(ApiUrl + "/api/pulso"
          + "?symbol=" + g_symbol
          + "&timeframe=" + g_timeframe
          + "&take_ticks=" + IntegerToString(TakeTicks)
          + "&direction=" + Direction);
  }

//--- Devolve o corpo da resposta, ou "" com g_error preenchido.
string HttpGet(const string url)
  {
   char   corpo[];
   char   resposta[];
   string cabecalhos;
   string envio = "X-API-Key: " + ApiKey + "\r\n";

   ResetLastError();
   int codigo = WebRequest("GET", url, envio, 5000, corpo, resposta, cabecalhos);

   if(codigo == -1)
     {
      int erro = GetLastError();
      if(erro == 4014)
         g_error = "Libere " + ApiUrl + " em Opcoes > Expert Advisors > WebRequest.";
      else
         g_error = "Falha de rede (" + IntegerToString(erro) + "). Painel acessivel?";
      return("");
     }

   string texto = CharArrayToString(resposta, 0, WHOLE_ARRAY, CP_UTF8);

   if(codigo == 401)
     {
      g_error = "Chave de API recusada. Gere outra no painel.";
      return("");
     }
   if(codigo == 404)
     {
      g_error = g_symbol + " sem candles coletadas no painel.";
      return("");
     }
   if(codigo != 200)
     {
      g_error = "Servidor respondeu " + IntegerToString(codigo) + ".";
      return("");
     }

   g_error = "";
   return(texto);
  }

bool Toggle(const bool ligar)
  {
   char   corpo[];
   char   resposta[];
   string cabecalhos;
   string json = "{\"symbol\":\"" + g_symbol + "\",\"enabled\":"
                 + (ligar ? "true" : "false") + "}";

   StringToCharArray(json, corpo, 0, StringLen(json), CP_UTF8);
   // StringToCharArray anexa o terminador nulo; envia-lo faria o servidor
   // ver um byte a mais e recusar o JSON.
   ArrayResize(corpo, StringLen(json));

   string envio = "X-API-Key: " + ApiKey + "\r\nContent-Type: application/json\r\n";

   ResetLastError();
   int codigo = WebRequest("POST", ApiUrl + "/api/pulso/toggle", envio, 5000,
                           corpo, resposta, cabecalhos);
   if(codigo != 200)
     {
      g_error = "Nao foi possivel alternar (codigo " + IntegerToString(codigo) + ").";
      DrawPanel();
      return(false);
     }
   return(true);
  }

//+------------------------------------------------------------------+
//| Leitura do JSON                                                   |
//|                                                                   |
//| MQL5 nao tem parser de JSON. Estas funcoes fazem busca de string  |
//| sobre um objeto RASO — e a API foi desenhada rasa exatamente por  |
//| isso. Nao tente usa-las em JSON aninhado arbitrario.              |
//+------------------------------------------------------------------+
string JsonRaw(const string json, const string chave, const int desde = 0)
  {
   string alvo = "\"" + chave + "\":";
   int inicio = StringFind(json, alvo, desde);
   if(inicio < 0)
      return("");
   inicio += StringLen(alvo);

   while(inicio < StringLen(json) && StringGetCharacter(json, inicio) == ' ')
      inicio++;

   if(StringGetCharacter(json, inicio) == '"')
     {
      inicio++;
      int fim = StringFind(json, "\"", inicio);
      if(fim < 0)
         return("");
      return(StringSubstr(json, inicio, fim - inicio));
     }

   int fim = inicio;
   while(fim < StringLen(json))
     {
      ushort c = StringGetCharacter(json, fim);
      if(c == ',' || c == '}' || c == ']')
         break;
      fim++;
     }
   string bruto = StringSubstr(json, inicio, fim - inicio);
   StringTrimLeft(bruto);
   StringTrimRight(bruto);
   return(bruto);
  }

double JsonNum(const string json, const string chave, const int desde = 0)
  {
   string bruto = JsonRaw(json, chave, desde);
   if(StringLen(bruto) == 0)
      return(0.0);
   return(StringToDouble(bruto));
  }

bool JsonBool(const string json, const string chave, const int desde = 0)
  {
   return(JsonRaw(json, chave, desde) == "true");
  }

//+------------------------------------------------------------------+
//| Array de strings: ["A","B","C"]                                   |
//|                                                                   |
//| `JsonRaw` nao serve aqui. Diante de um `[`, ela cai no ramo        |
//| numerico e para na primeira virgula, devolvendo `["A"` — lixo que  |
//| pareceria um valor. Esta funcao le ate o `]` e extrai so o que     |
//| esta entre aspas.                                                  |
//+------------------------------------------------------------------+
int JsonStrArray(const string json, const string chave, string &saida[],
                 const int maximo, const int desde = 0)
  {
   ArrayResize(saida, 0);

   string alvo = "\"" + chave + "\":";
   int inicio = StringFind(json, alvo, desde);
   if(inicio < 0)
      return(0);

   int abre = StringFind(json, "[", inicio);
   int fecha = StringFind(json, "]", abre);
   if(abre < 0 || fecha < 0)
      return(0);

   int achados = 0;
   int cursor = abre + 1;
   while(achados < maximo && cursor < fecha)
     {
      int aspa = StringFind(json, "\"", cursor);
      if(aspa < 0 || aspa > fecha)
         break;
      int fim = StringFind(json, "\"", aspa + 1);
      if(fim < 0 || fim > fecha)
         break;

      ArrayResize(saida, achados + 1);
      saida[achados] = StringSubstr(json, aspa + 1, fim - aspa - 1);
      achados++;
      cursor = fim + 1;
     }
   return(achados);
  }

//+------------------------------------------------------------------+
//| Rotulo curto de cada codigo de motivo.                            |
//|                                                                   |
//| A tabela vive AQUI, e nao vem do servidor, porque este EA nao      |
//| exibe texto livre de origem externa. O servidor manda codigos de   |
//| um conjunto fechado; o que aparece no grafico e escrito neste      |
//| arquivo. Codigo desconhecido (servidor mais novo) e mostrado cru,  |
//| que e feio mas honesto — melhor que sumir sem deixar rastro.       |
//+------------------------------------------------------------------+
string ReasonLabel(const string codigo)
  {
   if(codigo == "MTF_ALIGNED")         return("timeframes alinhados");
   if(codigo == "MTF_CONFLICT")        return("timeframes em conflito");
   if(codigo == "VOLUME_FAVORABLE")    return("volume favoravel");
   if(codigo == "VOLUME_WEAK")         return("volume fraco");
   if(codigo == "LIQUIDITY_FAVORABLE") return("liquidez favoravel");
   if(codigo == "SPREAD_ACCEPTABLE")   return("spread aceitavel");
   if(codigo == "SPREAD_WIDE")         return("spread alargado");
   if(codigo == "DATA_STALE")          return("dados atrasados");
   if(codigo == "BLOCKERS_PRESENT")    return("bloqueios ativos");
   if(codigo == "NO_DIRECTION")        return("sem direcao definida");
   if(codigo == "RR_FAVORABLE")        return("retorno/risco favoravel");
   if(codigo == "RR_POOR")             return("retorno/risco ruim");
   return(codigo);
  }

//+------------------------------------------------------------------+
//| Le o bloco signal_ai, se o servidor mandar um.                    |
//|                                                                   |
//| Tolerante por desenho: servidor antigo nao tem o bloco, e nesse    |
//| caso g_ai_state fica vazio e nada e desenhado. Nao ha versao de    |
//| contrato nova por causa disto — o campo e aditivo, e exigir        |
//| atualizacao do servidor para desenhar o que ja funcionava seria    |
//| quebrar quem nao pediu nada.                                       |
//|                                                                   |
//| A busca comeca na POSICAO do bloco, nao em zero: se um campo de    |
//| mesmo nome aparecer antes no JSON, buscar do inicio leria o valor  |
//| errado em silencio.                                                |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| Esquece a leitura da IA.                                          |
//|                                                                   |
//| Chamado tanto antes de reler quanto quando a conexao cai. No      |
//| segundo caso e o comportamento desejado: sem servidor nao ha      |
//| leitura atual, e uma leitura velha na tela nao se distingue de    |
//| uma nova.                                                          |
//+------------------------------------------------------------------+
void ClearSignalAI()
  {
   g_ai_state   = "";
   g_ai_conf    = 0.0;
   g_ai_review  = false;
   g_ai_reasons = "";
  }

void ParseSignalAI(const string json)
  {
   ClearSignalAI();

   int bloco = StringFind(json, "\"signal_ai\"");
   if(bloco < 0)
      return;   // servidor mais antigo: o resto do grafico segue igual

   if(!JsonBool(json, "available", bloco))
      return;

   g_ai_state  = JsonRaw(json, "state", bloco);
   g_ai_conf   = JsonNum(json, "confidence", bloco);
   g_ai_review = JsonBool(json, "needs_review", bloco);

   string codigos[];
   int achados = JsonStrArray(json, "reason_codes", codigos, MAX_REASON_CODES, bloco);
   for(int i = 0; i < achados; i++)
     {
      if(i > 0)
         g_ai_reasons += ", ";
      g_ai_reasons += ReasonLabel(codigos[i]);
     }
  }

color AIStateColor()
  {
   if(g_ai_state == "STRONG_SETUP")      return(clrMediumSeaGreen);
   if(g_ai_state == "CAUTION")           return(clrGold);
   if(g_ai_state == "WAIT")              return(clrSilver);
   if(g_ai_state == "INSUFFICIENT_DATA") return(clrDarkOrange);
   return(clrLightGray);
  }

//+------------------------------------------------------------------+
//| A linha de texto do bloco de IA.                                  |
//|                                                                   |
//| "confianca" aqui e o quanto o classificador se concentrou em um    |
//| rotulo — NAO e chance de o trade dar lucro, e a palavra escolhida  |
//| precisa deixar isso claro em um grafico onde tudo mais e preco.    |
//+------------------------------------------------------------------+
string AIStateLine()
  {
   if(StringLen(g_ai_state) == 0)
      return("");

   string linha = "IA: " + g_ai_state
                  + "  (confianca na leitura " + DoubleToString(g_ai_conf * 100.0, 0) + "%)";
   if(g_ai_review)
      linha += "  [CONFERIR]";
   if(StringLen(g_ai_reasons) > 0)
      linha += "  - " + g_ai_reasons;
   return(linha);
  }

//+------------------------------------------------------------------+
//| Consulta e redesenho                                              |
//+------------------------------------------------------------------+
void Fetch()
  {
   if(StringLen(ApiKey) == 0)
      return;
   if(g_busy)
      return;
   if(g_next_try > 0 && TimeCurrent() < g_next_try)
      return;   // ainda dentro do backoff

   g_busy = true;
   string json = HttpGet(BuildUrl());
   g_busy = false;
   Comment("");

   if(StringLen(json) == 0)
     {
      // Backoff: com o painel fora do ar, repetir no mesmo ritmo so enche o
      // log. Dobra ate 5 minutos e volta ao normal no primeiro sucesso.
      g_falhas++;
      int espera = (int)MathMin(300, RefreshSeconds * MathPow(2, MathMin(5, g_falhas)));
      g_next_try = TimeCurrent() + espera;
      // Erro de rede NAO apaga o desenho anterior de proposito: um grafico
      // que se esvazia a cada oscilacao de conexao e pior que um que
      // mantem o ultimo cenario e avisa que ele envelheceu.
      g_last_ok = false;

      // O rotulo da IA e a excecao a essa regra, e a diferenca e de
      // especie. Zonas, stop e alvo sao NIVEIS: continuam onde estavam
      // mesmo sem conexao, e envelhecem devagar. "STRONG_SETUP" e uma
      // AFIRMACAO SOBRE AGORA — mantê-la na tela enquanto o servidor esta
      // mudo e deixar uma leitura que ninguem esta mais sustentando com
      // cara de leitura atual.
      //
      // A linha some inteira em vez de mudar de cor: um rotulo apagado e
      // inequivoco, enquanto um cinza se confunde com o proprio WAIT.
      ClearSignalAI();

      DrawPanel();
      ChartRedraw();
      return;
     }

   g_next_try = 0;

   g_last_ok    = true;
   g_falhas     = 0;
   g_last_fetch = TimeCurrent();
   g_enabled    = JsonBool(json, "enabled");
   g_headline   = JsonRaw(json, "headline");
   g_version    = (int)JsonNum(json, "contract_version");

   // O tick vem do PAINEL, nao do grafico. Sao diferentes com frequencia:
   // no MNQ o `_Point` e 0.01 e o tick e 0.25. Usar o do grafico
   // encolheria a area de risco em 25x, e ela continuaria parecendo certa.
   double tick = JsonNum(json, "tick_size");
   g_tick_size = (tick > 0.0) ? tick : _Point;

   // Antes da checagem de versao: mesmo que o contrato esteja a frente e o
   // desenho seja abortado, ter lido isto nao custa nada e nao atrapalha.
   if(ShowSignalAI)
      ParseSignalAI(json);
   else
      ClearSignalAI();

   if(g_version > CONTRACT_SUPPORTED)
     {
      g_error = "Painel atualizado (contrato v" + IntegerToString(g_version)
                + "). Recompile o AITraderPulse.";
      DrawPanel();
      return;
     }

   ClearZones();

   if(!g_enabled)
     {
      g_status     = "DISABLED";
      g_status_ant = "DISABLED";
      g_detail = "Clique em LIGAR para voltar a receber a analise.";
      DrawPanel();
      UpdateButton();
      ChartRedraw();
      return;
     }

   bool   temEntrada = JsonBool(json, "has_entry");
   double entryMin   = JsonNum(json, "entry_min");
   double entryMax   = JsonNum(json, "entry_max");
   double sweet      = JsonNum(json, "sweet_spot");
   bool   temTake    = JsonBool(json, "has_take");
   double take       = JsonNum(json, "take");
   bool   temStop    = JsonBool(json, "has_stop");
   double stop       = JsonNum(json, "stop");
   bool   temNivel   = JsonBool(json, "has_decision_level");
   double nivel      = JsonNum(json, "decision_level");
   bool   velho      = JsonBool(json, "is_stale");
   string vies       = JsonRaw(json, "bias");
   int    distancia  = (int)JsonNum(json, "distance_ticks");

   bool comprando = (vies == "LONG");
   color corLado  = comprando ? clrLimeGreen : clrTomato;

   if(ShowHeatZones)
      DrawHeatZones(json, comprando);

   if(temEntrada)
     {
      DrawZone("entry", entryMin, entryMax, corLado,
               (comprando ? "BUY ZONE " : "SELL ZONE ") + FormatPrice(entryMin)
               + "-" + FormatPrice(entryMax));
      DrawLevel("sweet", sweet, corLado, STYLE_SOLID, 2,
                "MELHOR ENTRADA " + FormatPrice(sweet));
     }

   if(temTake)
      DrawLevel("take", take, clrLimeGreen, STYLE_DASH, 2,
                "TAKE +" + IntegerToString(TakeTicks) + " ticks " + FormatPrice(take));

   if(temStop)
     {
      DrawLevel("stop", stop, clrRed, STYLE_DASH, 2,
                "STOP / INVALIDACAO " + FormatPrice(stop));
      if(ShowRiskArea)
         DrawRiskArea(stop, comprando);
     }

   if(temNivel)
      DrawLevel("decision", nivel, clrMediumPurple, STYLE_DOT, 1,
                "DECISION LEVEL " + FormatPrice(nivel));

   g_status_ant = g_status;
   g_status     = JsonRaw(json, "status");

   NotifyIfReady(velho, comprando);

   g_detail = BuildDetail(velho, temEntrada, distancia, json);
   DrawPanel();
   UpdateButton();
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Alerta de entrada pronta                                          |
//|                                                                   |
//| Sem isto o indicador e passivo: so serve para quem esta olhando na|
//| hora certa. O alerta dispara na TRANSICAO para READY, nao enquanto|
//| ele durar — um aviso repetido a cada 15s vira ruido, e ruido e    |
//| ignorado exatamente quando importa.                               |
//+------------------------------------------------------------------+
void NotifyIfReady(const bool velho, const bool comprando)
  {
   if(!AlertOnReady || g_status != "READY")
      return;
   if(g_status_ant == "READY" || StringLen(g_status_ant) == 0)
      return;   // ja estava pronto, ou e a primeira resposta apos anexar

   if(velho)
      return;   // dados parados nao viram convite para operar

   string aviso = g_symbol + " " + g_timeframe + ": "
                  + (comprando ? "COMPRA" : "VENDA")
                  + " pronta - preco dentro da zona";

   Alert(aviso);
   if(PushOnReady)
      SendNotification(aviso);
  }

string BuildDetail(const bool velho, const bool temEntrada, const int distancia,
                   const string json)
  {
   if(velho)
      return("DADOS DESATUALIZADOS ha "
             + DoubleToString(JsonNum(json, "data_age_minutes"), 0)
             + " min - coletor MT5 parado");

   string status = JsonRaw(json, "status");
   if(status == "READY")
      return("Preco dentro da zona.");
   if(status == "WAIT_PULLBACK" && temEntrada)
      return("Aguardar: " + IntegerToString(distancia) + " ticks ate a zona.");
   if(status == "MISSED")
      return("O preco ja passou pela zona.");
   if(status == "NO_SETUP")
      return("Sem entrada boa agora.");
   return("");
  }

//+------------------------------------------------------------------+
//| Desenho                                                           |
//+------------------------------------------------------------------+
void DrawHeatZones(const string json, const bool comprando)
  {
   // Percorre `zones[]` procurando os itens HEAT. Cada iteracao avanca a
   // partir da posicao do item anterior — sem isso `StringFind` acharia
   // sempre o primeiro e o laco nunca terminaria.
   int posicao = StringFind(json, "\"zones\"");
   if(posicao < 0)
      return;

   int desenhadas = 0;
   while(desenhadas < MAX_ZONES)
     {
      int item = StringFind(json, "\"kind\":", posicao);
      if(item < 0)
         break;
      posicao = item + 7;

      string tipo = JsonRaw(json, "kind", item);
      if(tipo != "HEAT")
         continue;

      double preco = JsonNum(json, "price_min", item);
      string cor   = JsonRaw(json, "color", item);
      if(preco <= 0.0)
         continue;

      DrawLevel("heat" + IntegerToString(desenhadas), preco,
                (cor == "GREEN") ? clrSeaGreen : clrGoldenrod,
                STYLE_DOT, 1, "");
      desenhadas++;
     }
  }

void DrawZone(const string id, const double p1, const double p2, const color cor,
              const string texto)
  {
   string nome = PREFIX + id;
   datetime t1 = ChartFirstVisibleTime();
   datetime t2 = TimeCurrent() + PeriodSeconds(_Period) * 30;

   if(ObjectFind(0, nome) < 0)
      ObjectCreate(0, nome, OBJ_RECTANGLE, 0, t1, p1, t2, p2);

   ObjectSetInteger(0, nome, OBJPROP_TIME, 0, t1);
   ObjectSetDouble(0, nome, OBJPROP_PRICE, 0, p1);
   ObjectSetInteger(0, nome, OBJPROP_TIME, 1, t2);
   ObjectSetDouble(0, nome, OBJPROP_PRICE, 1, p2);
   ObjectSetInteger(0, nome, OBJPROP_COLOR, Fade(cor));
   ObjectSetInteger(0, nome, OBJPROP_FILL, true);
   ObjectSetInteger(0, nome, OBJPROP_BACK, true);
   ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
   ObjectSetString(0, nome, OBJPROP_TOOLTIP, texto);

   if(StringLen(texto) > 0)
      DrawTag(id + "_tag", MathMax(p1, p2), cor, texto);
  }

void DrawRiskArea(const double stop, const bool comprando)
  {
   // Numa compra a invalidacao e ABAIXO. Pintar do lado errado marcaria
   // como perigosa exatamente a regiao do alvo.
   // `g_tick_size` vem do painel. O `_Point` do grafico nao serve: no MNQ
   // ele e 0.01 contra um tick de 0.25, e a area sairia 25x menor.
   double alcance = MathMax(g_tick_size, _Point) * MathMax(1, TakeTicks) * 10;
   double limite = comprando ? stop - alcance : stop + alcance;

   string nome = PREFIX + "risk";
   datetime t1 = ChartFirstVisibleTime();
   datetime t2 = TimeCurrent() + PeriodSeconds(_Period) * 30;

   if(ObjectFind(0, nome) < 0)
      ObjectCreate(0, nome, OBJ_RECTANGLE, 0, t1, stop, t2, limite);

   ObjectSetInteger(0, nome, OBJPROP_TIME, 0, t1);
   ObjectSetDouble(0, nome, OBJPROP_PRICE, 0, stop);
   ObjectSetInteger(0, nome, OBJPROP_TIME, 1, t2);
   ObjectSetDouble(0, nome, OBJPROP_PRICE, 1, limite);
   ObjectSetInteger(0, nome, OBJPROP_COLOR, Fade(clrFireBrick));
   ObjectSetInteger(0, nome, OBJPROP_FILL, true);
   ObjectSetInteger(0, nome, OBJPROP_BACK, true);
   ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
   ObjectSetString(0, nome, OBJPROP_TOOLTIP, "Zona de risco: alem da invalidacao");
  }

void DrawLevel(const string id, const double preco, const color cor,
               const ENUM_LINE_STYLE estilo, const int largura, const string texto)
  {
   string nome = PREFIX + id;
   if(ObjectFind(0, nome) < 0)
      ObjectCreate(0, nome, OBJ_HLINE, 0, 0, preco);

   ObjectSetDouble(0, nome, OBJPROP_PRICE, preco);
   ObjectSetInteger(0, nome, OBJPROP_COLOR, cor);
   ObjectSetInteger(0, nome, OBJPROP_STYLE, estilo);
   ObjectSetInteger(0, nome, OBJPROP_WIDTH, largura);
   ObjectSetInteger(0, nome, OBJPROP_BACK, false);
   ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
   ObjectSetString(0, nome, OBJPROP_TOOLTIP, texto);

   if(StringLen(texto) > 0)
      DrawTag(id + "_tag", preco, cor, texto);
  }

void DrawTag(const string id, const double preco, const color cor, const string texto)
  {
   string nome = PREFIX + id;
   datetime quando = TimeCurrent() + PeriodSeconds(_Period) * 3;

   if(ObjectFind(0, nome) < 0)
      ObjectCreate(0, nome, OBJ_TEXT, 0, quando, preco);

   ObjectSetInteger(0, nome, OBJPROP_TIME, quando);
   ObjectSetDouble(0, nome, OBJPROP_PRICE, preco);
   ObjectSetString(0, nome, OBJPROP_TEXT, " " + texto);
   ObjectSetInteger(0, nome, OBJPROP_COLOR, cor);
   ObjectSetInteger(0, nome, OBJPROP_FONTSIZE, 8);
   ObjectSetInteger(0, nome, OBJPROP_ANCHOR, ANCHOR_LEFT);
   ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
  }

void DrawPanel()
  {
   if(!ShowPanel)
      return;

   // A linha da IA ocupa a terceira posicao quando existe, e some quando
   // nao existe — em vez de ficar vazia. Uma linha em branco fixa no
   // painel parece coisa quebrada.
   string linhaIA = AIStateLine();
   bool   temIA   = (StringLen(linhaIA) > 0);
   int    total   = temIA ? 4 : 3;

   string linhas[4];
   color  cores[4];

   linhas[0] = g_headline;
   cores[0]  = g_enabled ? clrWhite : clrSilver;

   linhas[1] = (StringLen(g_error) > 0) ? g_error : g_detail;
   cores[1]  = (StringLen(g_error) > 0) ? clrOrangeRed : clrLightGray;

   int ultima = 1;
   if(temIA)
     {
      ultima++;
      linhas[ultima] = linhaIA;
      // A cor e o estado: verde/amarelo/cinza/laranja. Quando ha aviso de
      // conferencia, o vermelho vence — um [CONFERIR] em verde seria lido
      // como "tudo certo" pelo canto do olho, que e como um painel de
      // grafico e lido na maior parte do tempo.
      cores[ultima] = g_ai_review ? clrOrangeRed : AIStateColor();
     }

   ultima++;
   linhas[ultima] = (g_last_fetch > 0)
                    ? ("atualizado " + TimeToString(g_last_fetch, TIME_SECONDS))
                    : "sem resposta ainda";
   cores[ultima] = g_last_ok ? clrMediumSeaGreen : clrOrangeRed;

   for(int i = 0; i < total; i++)
     {
      string nome = PREFIX + "panel" + IntegerToString(i);
      if(ObjectFind(0, nome) < 0)
         ObjectCreate(0, nome, OBJ_LABEL, 0, 0, 0);

      ObjectSetInteger(0, nome, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, nome, OBJPROP_XDISTANCE, 12);
      ObjectSetInteger(0, nome, OBJPROP_YDISTANCE, 20 + i * 16);
      ObjectSetString(0, nome, OBJPROP_TEXT, linhas[i]);
      ObjectSetInteger(0, nome, OBJPROP_COLOR, cores[i]);
      ObjectSetInteger(0, nome, OBJPROP_FONTSIZE, (i == 0) ? 11 : 8);
      ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
     }

   // O painel encolhe quando a linha da IA deixa de existir (servidor
   // desligou o Jev, ou o operador desmarcou ShowSignalAI). Sem apagar as
   // sobras, a linha antiga fica no grafico com o texto da ultima leitura
   // — um veredito congelado, que e exatamente o tipo de coisa que alguem
   // le como atual.
   for(int i = total; i < 4; i++)
      ObjectDelete(0, PREFIX + "panel" + IntegerToString(i));
  }

void CreateToggleButton()
  {
   string nome = PREFIX + "toggle";
   if(ObjectFind(0, nome) < 0)
      ObjectCreate(0, nome, OBJ_BUTTON, 0, 0, 0);

   ObjectSetInteger(0, nome, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, nome, OBJPROP_XDISTANCE, 12);
   ObjectSetInteger(0, nome, OBJPROP_YDISTANCE, 72);
   ObjectSetInteger(0, nome, OBJPROP_XSIZE, 150);
   ObjectSetInteger(0, nome, OBJPROP_YSIZE, 24);
   ObjectSetInteger(0, nome, OBJPROP_FONTSIZE, 9);
   ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
   UpdateButton();
  }

void UpdateButton()
  {
   string nome = PREFIX + "toggle";
   if(ObjectFind(0, nome) < 0)
      return;

   // O rotulo diz a ACAO do clique, nao o estado atual: "DESLIGAR" num
   // botao ligado e ambiguo o suficiente para alguem clicar sem querer.
   ObjectSetString(0, nome, OBJPROP_TEXT, g_enabled ? "DESLIGAR IA" : "LIGAR IA");
   ObjectSetInteger(0, nome, OBJPROP_BGCOLOR, g_enabled ? clrSeaGreen : clrDimGray);
   ObjectSetInteger(0, nome, OBJPROP_COLOR, clrWhite);
  }

//+------------------------------------------------------------------+
//| Limpeza                                                           |
//+------------------------------------------------------------------+
//--- Reposiciona os retangulos na janela visivel atual, sem consultar.
void Reanchor()
  {
   datetime t1 = ChartFirstVisibleTime();
   datetime t2 = TimeCurrent() + PeriodSeconds(_Period) * 30;
   datetime tag = TimeCurrent() + PeriodSeconds(_Period) * 3;

   for(int i = ObjectsTotal(0) - 1; i >= 0; i--)
     {
      string nome = ObjectName(0, i);
      if(StringFind(nome, PREFIX) != 0)
         continue;

      long tipo = ObjectGetInteger(0, nome, OBJPROP_TYPE);
      if(tipo == OBJ_RECTANGLE)
        {
         ObjectSetInteger(0, nome, OBJPROP_TIME, 0, t1);
         ObjectSetInteger(0, nome, OBJPROP_TIME, 1, t2);
        }
      else if(tipo == OBJ_TEXT)
         ObjectSetInteger(0, nome, OBJPROP_TIME, tag);
     }
   ChartRedraw();
  }

void ClearZones()
  {
   // Apaga so o desenho da analise; o painel e o botao sobrevivem, porque
   // sao eles que explicam o que aconteceu quando nao ha o que desenhar.
   for(int i = ObjectsTotal(0) - 1; i >= 0; i--)
     {
      string nome = ObjectName(0, i);
      if(StringFind(nome, PREFIX) != 0)
         continue;
      if(StringFind(nome, PREFIX + "panel") == 0 || nome == PREFIX + "toggle")
         continue;
      ObjectDelete(0, nome);
     }
  }

void ClearObjects()
  {
   for(int i = ObjectsTotal(0) - 1; i >= 0; i--)
     {
      string nome = ObjectName(0, i);
      if(StringFind(nome, PREFIX) == 0)
         ObjectDelete(0, nome);
     }
  }

//+------------------------------------------------------------------+
//| Utilidades                                                        |
//+------------------------------------------------------------------+
datetime ChartFirstVisibleTime()
  {
   datetime tempos[];
   int primeira = (int)ChartGetInteger(0, CHART_FIRST_VISIBLE_BAR);
   if(CopyTime(_Symbol, _Period, primeira, 1, tempos) == 1)
      return(tempos[0]);
   return(TimeCurrent() - PeriodSeconds(_Period) * 100);
  }

//--- Mistura a cor com o fundo do grafico.
//
//  `ZoneTransparency` era um input MORTO: existia na tela de propriedades e
//  nao fazia nada. ARGB em OBJ_RECTANGLE depende da build do terminal, entao
//  a mistura e calculada aqui — funciona em qualquer versao e o resultado e
//  o mesmo em tema claro ou escuro, porque parte do fundo real.
color Fade(const color cor)
  {
   int alpha = MathMax(0, MathMin(100, ZoneTransparency));
   color fundo = (color)ChartGetInteger(0, CHART_COLOR_BACKGROUND);

   int r = ((cor & 0x0000FF)) * (100 - alpha) / 100 + ((fundo & 0x0000FF)) * alpha / 100;
   int g = ((cor >> 8) & 0x00FF) * (100 - alpha) / 100 + ((fundo >> 8) & 0x00FF) * alpha / 100;
   int b = ((cor >> 16) & 0x00FF) * (100 - alpha) / 100 + ((fundo >> 16) & 0x00FF) * alpha / 100;

   return((color)(r | (g << 8) | (b << 16)));
  }

string FormatPrice(const double preco)
  {
   // Casas derivadas do tick do PAINEL. Com `SymbolOverride` apontando para
   // outro ativo, os `_Digits` do grafico truncariam o preco do simbolo
   // analisado — 1.10345 viraria 1.10 num grafico de indice.
   int casas = _Digits;
   if(g_tick_size > 0.0 && g_tick_size < 1.0)
     {
      casas = 0;
      double t = g_tick_size;
      while(t < 1.0 && casas < 8)
        {
         t *= 10.0;
         casas++;
        }
     }
   else if(g_tick_size >= 1.0)
      casas = 0;

   return(DoubleToString(preco, casas));
  }

string PeriodToName(const ENUM_TIMEFRAMES periodo)
  {
   switch(periodo)
     {
      case PERIOD_M1:  return("M1");
      case PERIOD_M5:  return("M5");
      case PERIOD_M15: return("M15");
      case PERIOD_M30: return("M30");
      case PERIOD_H1:  return("H1");
      case PERIOD_H4:  return("H4");
      case PERIOD_D1:  return("D1");
      case PERIOD_W1:  return("W1");
      case PERIOD_MN1: return("MN1");
     }
   // Timeframe fora da matriz de analise do painel: cair para M15 seria
   // desenhar um cenario de outro periodo sem avisar.
   Print("AITraderPulse: periodo do grafico nao suportado pelo painel; use TimeframeOverride.");
   return("M15");
  }
//+------------------------------------------------------------------+
