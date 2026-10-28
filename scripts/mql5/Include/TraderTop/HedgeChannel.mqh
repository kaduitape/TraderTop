//+------------------------------------------------------------------+
//| HedgeChannel.mqh                                                  |
//| Canal local entre o EA conector e o indicador.                    |
//|                                                                   |
//| O PROBLEMA                                                        |
//|                                                                   |
//| Indicadores nao podem chamar `WebRequest()`: rodam na thread de   |
//| interface do terminal e a plataforma recusa. Quem fala com a rede |
//| e o EA. Mas os dois precisam dividir o resultado, e MQL5 nao tem  |
//| memoria compartilhada entre programas.                            |
//|                                                                   |
//| A ESCOLHA: variaveis globais do terminal                          |
//|                                                                   |
//| Tres candidatos foram considerados:                                |
//|                                                                   |
//| 1. ARQUIVO em MQL5\Files. Aguenta texto longo, mas escrever a      |
//|    cada 1-2s desgasta disco e abre corrida de leitura: o          |
//|    indicador pode ler no meio de uma escrita e pegar JSON pela    |
//|    metade. Exigiria arquivo temporario + rename atomico, que o    |
//|    MQL5 nao garante entre processos.                               |
//| 2. RECURSO (`ResourceCreate`). Nao e compartilhavel entre         |
//|    programas distintos da forma que precisamos.                   |
//| 3. VARIAVEIS GLOBAIS DO TERMINAL. So guardam `double`, o que      |
//|    parece limitacao e aqui e virtude: tudo que o indicador        |
//|    precisa desenhar JA E numero (precos, percentuais, prazo,      |
//|    estado). Escrita e leitura sao atomicas por variavel, nao      |
//|    tocam disco, e o terminal as limpa sozinho.                     |
//|                                                                   |
//| Nenhuma DLL. Nenhuma dependencia externa.                          |
//|                                                                   |
//| O QUE NAO CABE AQUI                                                |
//|                                                                   |
//| Texto. O indicador nao recebe frase nenhuma do servidor — ele      |
//| traduz CODIGOS numericos usando tabelas escritas no proprio       |
//| arquivo. Isso e proposital: o indicador nunca exibe texto livre de |
//| origem externa.                                                    |
//|                                                                   |
//| CONSISTENCIA: por que `seq` e escrito duas vezes                   |
//|                                                                   |
//| O indicador pode ler no meio de uma atualizacao e montar um quadro |
//| com metade dos valores novos. Para evitar isso o EA escreve        |
//| `seq_begin`, depois todos os campos, depois `seq_end` com o mesmo  |
//| numero. O indicador le `seq_end` primeiro, depois os campos,       |
//| depois `seq_begin`: se os dois baterem, o quadro e integro. Se nao,|
//| ele mantem o anterior e tenta no proximo tick.                     |
//|                                                                   |
//| ISOLAMENTO DE SESSAO                                                |
//|                                                                   |
//| Toda variavel leva no nome o hash de terminal+grafico+simbolo+     |
//| setup. Dois graficos do mesmo ativo escrevem em espacos distintos, |
//| que e o caso NORMAL: o conector costuma ficar num grafico separado |
//| para nao brigar com outro EA.                                      |
//+------------------------------------------------------------------+
#property copyright "AI Trader PRO"
#property strict

#define TTH_PREFIX        "TTH_"
#define TTH_CHANNEL_VER   1

//--- Estados. Espelham `SessionState` no servidor; o indicador so ve
//--- numero e traduz com a tabela abaixo.
#define TTH_ST_WAITING        0
#define TTH_ST_CONNECTED      1
#define TTH_ST_MODEL_NA       2
#define TTH_ST_STALE          3
#define TTH_ST_DISCONNECTED   4
#define TTH_ST_CLOSED         5
#define TTH_ST_INVALID_KEY    6

//--- Objetivo
#define TTH_OBJ_FAVOR_DESK    0
#define TTH_OBJ_FAVOR_BROKER  1

//--- Motivo de encerramento
#define TTH_CLOSE_NONE        0
#define TTH_CLOSE_TAKE        1
#define TTH_CLOSE_STOP        2
#define TTH_CLOSE_TIMEOUT     3

//--- Tipo de modelo. O indicador precisa saber para nao apresentar um
//--- baseline teorico como se fosse modelo treinado.
#define TTH_MODEL_NONE        0
#define TTH_MODEL_BASELINE    1
#define TTH_MODEL_TREND       2
#define TTH_MODEL_TRAINED     3

//+------------------------------------------------------------------+
//| Nome do espaco da sessao.                                         |
//|                                                                   |
//| Hash FNV-1a de 32 bits: nao e criptografia, e so um identificador |
//| curto e estavel. Variaveis globais tem limite de 63 caracteres no |
//| nome, entao o nome bruto nao caberia.                              |
//+------------------------------------------------------------------+
string TTH_SessionTag(const long chartId, const string symbol, const long setupId)
  {
   string material = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "|"
                     + IntegerToString(chartId) + "|"
                     + symbol + "|"
                     + IntegerToString(setupId);

   uint hash = 2166136261;
   for(int i = 0; i < StringLen(material); i++)
     {
      hash ^= (uint)StringGetCharacter(material, i);
      hash *= 16777619;
     }
   return(TTH_PREFIX + StringFormat("%08X", hash) + "_");
  }

string TTH_Var(const string tag, const string field)
  {
   return(tag + field);
  }

//+------------------------------------------------------------------+
//| Escrita (lado do EA)                                              |
//+------------------------------------------------------------------+
void TTH_Set(const string tag, const string field, const double value)
  {
   GlobalVariableSet(TTH_Var(tag, field), value);
  }

double TTH_Get(const string tag, const string field, const double fallback = 0.0)
  {
   string nome = TTH_Var(tag, field);
   if(!GlobalVariableCheck(nome))
      return(fallback);
   return(GlobalVariableGet(nome));
  }

//+------------------------------------------------------------------+
//| Um quadro completo, escrito de forma que leitura parcial se       |
//| detecte.                                                           |
//|                                                                   |
//| A ordem importa: `seq_begin` ANTES dos campos e `seq_end` DEPOIS. |
//| Invertida, o leitor acharia integro um quadro pela metade.         |
//+------------------------------------------------------------------+
struct TTHFrame
  {
   double            seq;
   double            state;
   double            objective;
   double            is_long;
   double            entry;
   double            take;
   double            stop;
   double            price;
   double            bid;
   double            ask;
   double            take_pct;
   double            stop_pct;
   double            none_pct;
   double            has_forecast;
   double            model_kind;
   double            seconds_left;
   double            updated_at;      // TimeCurrent() da ultima resposta
   double            closed_reason;
   double            net_take;
   double            net_stop;
   double            net_timeout;
   double            lots;
   double            residual;
   double            has_financial;
   double            basis_risk;
   double            setup_version;
  };

void TTH_WriteFrame(const string tag, const TTHFrame &f)
  {
   TTH_Set(tag, "seq_begin", f.seq);

   TTH_Set(tag, "state",          f.state);
   TTH_Set(tag, "objective",      f.objective);
   TTH_Set(tag, "is_long",        f.is_long);
   TTH_Set(tag, "entry",          f.entry);
   TTH_Set(tag, "take",           f.take);
   TTH_Set(tag, "stop",           f.stop);
   TTH_Set(tag, "price",          f.price);
   TTH_Set(tag, "bid",            f.bid);
   TTH_Set(tag, "ask",            f.ask);
   TTH_Set(tag, "take_pct",       f.take_pct);
   TTH_Set(tag, "stop_pct",       f.stop_pct);
   TTH_Set(tag, "none_pct",       f.none_pct);
   TTH_Set(tag, "has_forecast",   f.has_forecast);
   TTH_Set(tag, "model_kind",     f.model_kind);
   TTH_Set(tag, "seconds_left",   f.seconds_left);
   TTH_Set(tag, "updated_at",     f.updated_at);
   TTH_Set(tag, "closed_reason",  f.closed_reason);
   TTH_Set(tag, "net_take",       f.net_take);
   TTH_Set(tag, "net_stop",       f.net_stop);
   TTH_Set(tag, "net_timeout",    f.net_timeout);
   TTH_Set(tag, "lots",           f.lots);
   TTH_Set(tag, "residual",       f.residual);
   TTH_Set(tag, "has_financial",  f.has_financial);
   TTH_Set(tag, "basis_risk",     f.basis_risk);
   TTH_Set(tag, "setup_version",  f.setup_version);
   TTH_Set(tag, "channel_ver",    (double)TTH_CHANNEL_VER);

   TTH_Set(tag, "seq_end", f.seq);
  }

//+------------------------------------------------------------------+
//| Leitura (lado do indicador). Devolve false se o quadro estiver    |
//| pela metade OU se o canal for de outra versao.                     |
//+------------------------------------------------------------------+
bool TTH_ReadFrame(const string tag, TTHFrame &f)
  {
   if((int)TTH_Get(tag, "channel_ver", 0) != TTH_CHANNEL_VER)
      return(false);   // EA de outra versao: melhor nada do que campo trocado

   double fim = TTH_Get(tag, "seq_end", -1);
   if(fim < 0)
      return(false);

   f.state         = TTH_Get(tag, "state",         TTH_ST_WAITING);
   f.objective     = TTH_Get(tag, "objective",     TTH_OBJ_FAVOR_DESK);
   f.is_long       = TTH_Get(tag, "is_long",       1);
   f.entry         = TTH_Get(tag, "entry",         0);
   f.take          = TTH_Get(tag, "take",          0);
   f.stop          = TTH_Get(tag, "stop",          0);
   f.price         = TTH_Get(tag, "price",         0);
   f.bid           = TTH_Get(tag, "bid",           0);
   f.ask           = TTH_Get(tag, "ask",           0);
   f.take_pct      = TTH_Get(tag, "take_pct",      0);
   f.stop_pct      = TTH_Get(tag, "stop_pct",      0);
   f.none_pct      = TTH_Get(tag, "none_pct",      0);
   f.has_forecast  = TTH_Get(tag, "has_forecast",  0);
   f.model_kind    = TTH_Get(tag, "model_kind",    TTH_MODEL_NONE);
   f.seconds_left  = TTH_Get(tag, "seconds_left",  0);
   f.updated_at    = TTH_Get(tag, "updated_at",    0);
   f.closed_reason = TTH_Get(tag, "closed_reason", TTH_CLOSE_NONE);
   f.net_take      = TTH_Get(tag, "net_take",      0);
   f.net_stop      = TTH_Get(tag, "net_stop",      0);
   f.net_timeout   = TTH_Get(tag, "net_timeout",   0);
   f.lots          = TTH_Get(tag, "lots",          0);
   f.residual      = TTH_Get(tag, "residual",      0);
   f.has_financial = TTH_Get(tag, "has_financial", 0);
   f.basis_risk    = TTH_Get(tag, "basis_risk",    0);
   f.setup_version = TTH_Get(tag, "setup_version", 0);

   double inicio = TTH_Get(tag, "seq_begin", -1);
   if(inicio != fim)
      return(false);   // escrita em andamento: mantem o quadro anterior

   f.seq = fim;
   return(true);
  }

//+------------------------------------------------------------------+
//| Limpeza. O EA apaga SO as variaveis da sua sessao ao sair —       |
//| `GlobalVariablesDeleteAll(TTH_PREFIX)` derrubaria tambem as de     |
//| outro grafico que continua rodando.                                |
//+------------------------------------------------------------------+
void TTH_ClearSession(const string tag)
  {
   GlobalVariablesDeleteAll(tag);
  }

//+------------------------------------------------------------------+
//| Rotulos. Vivem AQUI, e nao vem do servidor: o indicador nunca      |
//| exibe texto livre de origem externa.                               |
//+------------------------------------------------------------------+
string TTH_StateLabel(const int state)
  {
   switch(state)
     {
      case TTH_ST_WAITING:      return("aguardando dados");
      case TTH_ST_CONNECTED:    return("conectado");
      case TTH_ST_MODEL_NA:     return("modelo indisponivel");
      case TTH_ST_STALE:        return("dados desatualizados");
      case TTH_ST_DISCONNECTED: return("desconectado");
      case TTH_ST_CLOSED:       return("operacao encerrada");
      case TTH_ST_INVALID_KEY:  return("chave invalida");
     }
   return("estado desconhecido");
  }

string TTH_CloseLabel(const int reason)
  {
   switch(reason)
     {
      case TTH_CLOSE_TAKE:    return("take da mesa atingido");
      case TTH_CLOSE_STOP:    return("stop da mesa atingido");
      case TTH_CLOSE_TIMEOUT: return("prazo esgotado");
     }
   return("");
  }

string TTH_ModelLabel(const int kind)
  {
   switch(kind)
     {
      // "teorico" aparece na tela de proposito: o numero vem de hipoteses
      // declaradas, nao de frequencia medida neste ativo.
      case TTH_MODEL_BASELINE: return("baseline teorico");
      case TTH_MODEL_TREND:    return("pontuacao de tendencia");
      case TTH_MODEL_TRAINED:  return("modelo treinado");
     }
   return("sem modelo");
  }

//+------------------------------------------------------------------+
//| Prazo restante em texto curto.                                     |
//+------------------------------------------------------------------+
string TTH_Countdown(const int seconds)
  {
   if(seconds <= 0)
      return("encerrado");
   int h = seconds / 3600;
   int m = (seconds % 3600) / 60;
   int s = seconds % 60;
   if(h > 0)
      return(StringFormat("%dh%02dm", h, m));
   if(m > 0)
      return(StringFormat("%dm%02ds", m, s));
   return(StringFormat("%ds", s));
  }
