//+------------------------------------------------------------------+
//| HedgeJson.mqh                                                     |
//| Leitura do JSON do Hedge AI — o minimo que resolve, e nada mais.  |
//|                                                                   |
//| NAO e um parser de JSON. E um extrator por busca de chave, como o |
//| do AITraderPulse, com duas diferencas que o contrato do Hedge     |
//| exige:                                                            |
//|                                                                   |
//| 1. ESCOPO. O contrato e aninhado (`forecast`, `financial`,        |
//|    `scenarios`), e varias chaves se repetem entre blocos —        |
//|    `available` aparece em dois, `net` em tres. Buscar do inicio    |
//|    leria o valor do bloco errado em silencio. Por isso toda       |
//|    funcao aqui recebe uma posicao de inicio, e ha                 |
//|    `JsonObjectStart` para achar o bloco primeiro.                 |
//| 2. `null` E DIFERENTE DE ZERO. No Pulso a ausencia virava 0 com   |
//|    uma flag ao lado. Aqui uma probabilidade pode ser legitimamente|
//|    nula, e confundir com zero seria exibir "0% de chance" onde    |
//|    nao ha estimativa. `JsonIsNull` separa os dois.                |
//+------------------------------------------------------------------+
#property copyright "AI Trader PRO"
#property strict

//+------------------------------------------------------------------+
//| Posicao de abertura de um objeto nomeado, ou -1.                  |
//+------------------------------------------------------------------+
int JsonObjectStart(const string json, const string key, const int from = 0)
  {
   int p = StringFind(json, "\"" + key + "\":", from);
   if(p < 0)
      return(-1);
   int abre = StringFind(json, "{", p);
   return(abre);
  }

//+------------------------------------------------------------------+
//| Valor bruto de uma chave, a partir de `from`.                     |
//| Devolve "" quando a chave nao existe.                              |
//+------------------------------------------------------------------+
string JsonRawAt(const string json, const string key, const int from = 0)
  {
   string alvo = "\"" + key + "\":";
   int inicio = StringFind(json, alvo, from);
   if(inicio < 0)
      return("");
   inicio += StringLen(alvo);

   while(inicio < StringLen(json) && StringGetCharacter(json, inicio) == ' ')
      inicio++;

   if(inicio >= StringLen(json))
      return("");

   if(StringGetCharacter(json, inicio) == '"')
     {
      inicio++;
      int fim = StringFind(json, "\"", inicio);
      if(fim < 0)
         return("");
      return(StringSubstr(json, inicio, fim - inicio));
     }

   int fim2 = inicio;
   while(fim2 < StringLen(json))
     {
      ushort c = StringGetCharacter(json, fim2);
      if(c == ',' || c == '}' || c == ']')
         break;
      fim2++;
     }
   string bruto = StringSubstr(json, inicio, fim2 - inicio);
   StringTrimLeft(bruto);
   StringTrimRight(bruto);
   return(bruto);
  }

//+------------------------------------------------------------------+
//| `null` explicito. Distinto de ausente E de zero.                  |
//|                                                                   |
//| Esta distincao e a razao de o arquivo existir: uma probabilidade  |
//| nula significa "nao ha estimativa", e mostra-la como 0% afirmaria |
//| que o evento nao acontece.                                         |
//+------------------------------------------------------------------+
bool JsonIsNull(const string json, const string key, const int from = 0)
  {
   return(JsonRawAt(json, key, from) == "null");
  }

bool JsonHasKey(const string json, const string key, const int from = 0)
  {
   return(StringFind(json, "\"" + key + "\":", from) >= 0);
  }

double JsonNumAt(const string json, const string key, const int from = 0,
                 const double fallback = 0.0)
  {
   string bruto = JsonRawAt(json, key, from);
   if(StringLen(bruto) == 0 || bruto == "null")
      return(fallback);
   return(StringToDouble(bruto));
  }

bool JsonBoolAt(const string json, const string key, const int from = 0)
  {
   return(JsonRawAt(json, key, from) == "true");
  }

string JsonStrAt(const string json, const string key, const int from = 0)
  {
   string bruto = JsonRawAt(json, key, from);
   if(bruto == "null")
      return("");
   return(bruto);
  }

//+------------------------------------------------------------------+
//| Numero dentro de um cenario especifico de `financial.scenarios`.  |
//|                                                                   |
//| `net` aparece em DESK_TAKE, DESK_STOP e TIMEOUT. Sem localizar o  |
//| cenario antes, toda leitura devolveria o primeiro.                 |
//+------------------------------------------------------------------+
double JsonScenarioNum(const string json, const string scenario, const string field,
                       const int financialFrom)
  {
   int bloco = JsonObjectStart(json, scenario, financialFrom);
   if(bloco < 0)
      return(0.0);
   return(JsonNumAt(json, field, bloco));
  }

//+------------------------------------------------------------------+
//| Escape minimo para montar o corpo do POST.                        |
//|                                                                   |
//| So aspas e barra invertida: os campos que enviamos sao simbolo,   |
//| id de terminal e numeros. Nao ha texto livre do operador no corpo,|
//| e por isso nao ha caso para escapar.                               |
//+------------------------------------------------------------------+
string JsonEscape(const string value)
  {
   string saida = value;
   StringReplace(saida, "\\", "\\\\");
   StringReplace(saida, "\"", "\\\"");
   return(saida);
  }
