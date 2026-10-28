//+------------------------------------------------------------------+
//| TraderTopHedgeIndicator.mq5                                       |
//| Desenha o cenario do Hedge AI. Nao fala com a rede.               |
//|                                                                   |
//| Le o canal local que o EA `TraderTopHedgeConnector` publica (ver  |
//| `Include/TraderTop/HedgeChannel.mqh`). Nao ha `WebRequest` aqui — |
//| indicadores rodam na thread de interface e a plataforma recusa.   |
//|                                                                   |
//| CORES E O OBJETIVO                                                |
//|                                                                   |
//| No modo "favorecer corretora" a regiao do STOP da mesa fica verde |
//| e a do TAKE fica vermelha — porque o que se quer e o stop. No modo|
//| "favorecer mesa" inverte. Os NOMES dos niveis nunca mudam: o take |
//| da mesa continua sendo o take da mesa, so a cor diz se ele e bem  |
//| ou mal vindo.                                                      |
//|                                                                   |
//| A decisao de cor vem do SERVIDOR (`colors` no contrato), nao      |
//| daqui: duas implementacoes da mesma regra divergem, e a           |
//| divergencia pintaria de verde a regiao que o operador quer evitar.|
//|                                                                   |
//| ONDE CADA REGIAO FICA                                              |
//|                                                                   |
//| Depende de comprado ou vendido, nunca de cima/baixo fixo. Numa    |
//| venda o take fica ABAIXO da entrada, e fixar verde em cima poria  |
//| a cor certa no nivel errado em toda operacao vendida.              |
//|                                                                   |
//| OBJETOS                                                            |
//|                                                                   |
//| Todos com o prefixo desta sessao. Ao remover o indicador, so os   |
//| proprios objetos sao apagados — nunca os de outro indicador nem   |
//| os de outro grafico. E eles sao ATUALIZADOS, nao recriados: criar |
//| e destruir a cada tick pisca a tela e consome CPU a toa.           |
//+------------------------------------------------------------------+
#property copyright "AI Trader PRO"
#property version   "1.00"
#property strict
#property indicator_chart_window
#property indicator_plots 0
#property description "Desenha entrada, take e stop da mesa com as probabilidades do Hedge AI. Nao envia ordens e nao faz requisicoes de rede."

#include <TraderTop/HedgeChannel.mqh>

//--- Identificacao (precisa casar com o conector)
input long   SetupId          = 0;      // Mesmo SetupId do conector
input string SymbolOverride   = "";     // Mesmo SymbolOverride do conector
input string ChartIdOverride  = "";     // Mesmo valor nos dois quando em graficos separados

//--- Aparencia
input int    ZoneOpacity      = 88;     // 0-100 (maior = mais transparente)
input bool   ShowZones        = true;   // Pintar as regioes ate as barreiras
input bool   ShowPanel        = true;   // Painel com percentuais e financeiro
input int    PanelCorner      = 0;      // 0=sup.esq 1=sup.dir 2=inf.esq 3=inf.dir
input int    PanelFontSize    = 10;
input color  EntryColor       = clrGold;

#define PFX_BASE "TTHI_"

string   g_pfx     = "";
string   g_tag     = "";
string   g_symbol  = "";
long     g_chart   = 0;
double   g_last_seq = -1;
TTHFrame g_frame;
bool     g_tem_quadro = false;

//+------------------------------------------------------------------+
int OnInit()
  {
   g_symbol = (StringLen(SymbolOverride) > 0) ? SymbolOverride : _Symbol;
   g_chart  = (StringLen(ChartIdOverride) > 0)
              ? StringToInteger(ChartIdOverride)
              : ChartID();

   g_tag = TTH_SessionTag(g_chart, g_symbol, SetupId);
   // Prefixo proprio por sessao: dois indicadores no mesmo grafico (dois
   // setups) nao podem apagar os objetos um do outro.
   g_pfx = PFX_BASE + StringSubstr(g_tag, StringLen(TTH_PREFIX));

   IndicatorSetString(INDICATOR_SHORTNAME, "Hedge AI #" + IntegerToString(SetupId));
   EventSetTimer(1);   // o canal muda por conta do EA, nao por tick
   Atualizar();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   ApagarMeusObjetos();
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Nenhuma rede, nenhum calculo pesado aqui. OnCalculate so mantem   |
//| as ancoras quando o grafico rola.                                  |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
   if(g_tem_quadro && ShowZones)
      Reancorar();
   return(rates_total);
  }

void OnTimer()
  {
   Atualizar();
  }

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
  {
   if(id == CHARTEVENT_CHART_CHANGE && g_tem_quadro)
     {
      // Retangulos presos a primeira barra visivel ficariam para tras ao
      // rolar o grafico ate o proximo ciclo.
      Reancorar();
      ChartRedraw();
     }
  }

//+------------------------------------------------------------------+
void Atualizar()
  {
   TTHFrame f;
   if(!TTH_ReadFrame(g_tag, f))
     {
      // Quadro pela metade ou EA de outra versao: mantem o anterior. O
      // painel ja mostra a idade, entao o operador ve que envelheceu.
      if(!g_tem_quadro)
         DesenharPainelVazio();
      return;
     }

   bool mudou = (f.seq != g_last_seq);
   g_frame = f;
   g_tem_quadro = true;
   g_last_seq = f.seq;

   int estado = (int)f.state;
   bool desenhavel = (estado == TTH_ST_CONNECTED || estado == TTH_ST_MODEL_NA
                      || estado == TTH_ST_STALE || estado == TTH_ST_CLOSED);

   if(desenhavel && f.entry > 0)
      DesenharNiveis();
   else
      ApagarNiveis();

   DesenharPainel();
   if(mudou)
      ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Linhas e regioes                                                  |
//+------------------------------------------------------------------+
void DesenharNiveis()
  {
   bool comprado = (g_frame.is_long > 0.5);
   bool favoreceCorretora = ((int)g_frame.objective == TTH_OBJ_FAVOR_BROKER);

   // A cor diz se a regiao e bem-vinda; o nome do nivel nao muda.
   color corTake = favoreceCorretora ? clrCrimson : clrSeaGreen;
   color corStop = favoreceCorretora ? clrSeaGreen : clrCrimson;

   Linha("entry", g_frame.entry, EntryColor, STYLE_SOLID, 2, "ENTRADA MESA");
   Linha("take",  g_frame.take,  corTake, STYLE_DASH, 2,
         "TAKE MESA" + Sufixo(favoreceCorretora ? "evitar" : "desejado"));
   Linha("stop",  g_frame.stop,  corStop, STYLE_DASH, 2,
         "STOP MESA" + Sufixo(favoreceCorretora ? "desejado" : "evitar"));

   if(!ShowZones)
     {
      Apagar("zona_take");
      Apagar("zona_stop");
      return;
     }

   // A posicao segue o LADO. Numa venda o take fica abaixo do preco.
   Regiao("zona_take", g_frame.price, g_frame.take, corTake);
   Regiao("zona_stop", g_frame.price, g_frame.stop, corStop);
  }

string Sufixo(const string texto)
  {
   return("  (" + texto + ")");
  }

void Linha(const string id, const double preco, const color cor,
           const int estilo, const int largura, const string texto)
  {
   if(preco <= 0)
     {
      Apagar(id);
      return;
     }

   string nome = g_pfx + id;
   if(ObjectFind(0, nome) < 0)
      ObjectCreate(0, nome, OBJ_HLINE, 0, 0, 0);

   ObjectSetDouble(0, nome, OBJPROP_PRICE, preco);
   ObjectSetInteger(0, nome, OBJPROP_COLOR, cor);
   ObjectSetInteger(0, nome, OBJPROP_STYLE, estilo);
   ObjectSetInteger(0, nome, OBJPROP_WIDTH, largura);
   ObjectSetInteger(0, nome, OBJPROP_BACK, false);
   ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, nome, OBJPROP_HIDDEN, true);
   ObjectSetString(0, nome, OBJPROP_TOOLTIP, texto);
   ObjectSetString(0, nome, OBJPROP_TEXT, texto);
  }

//+------------------------------------------------------------------+
//| Regiao entre dois precos.                                         |
//|                                                                   |
//| `OBJ_RECTANGLE` com `OBJPROP_FILL` e `OBJPROP_BACK`: o MQL5 nao   |
//| tem alfa por objeto, e a transparencia real vem de desenhar ATRAS |
//| das velas com uma cor misturada ao fundo do grafico. Canvas daria |
//| alfa verdadeiro ao custo de reimplementar a escala de precos — e  |
//| duas escalas divergem.                                             |
//+------------------------------------------------------------------+
void Regiao(const string id, const double de, const double ate, const color cor)
  {
   if(de <= 0 || ate <= 0)
     {
      Apagar(id);
      return;
     }

   string nome = g_pfx + id;
   if(ObjectFind(0, nome) < 0)
     {
      ObjectCreate(0, nome, OBJ_RECTANGLE, 0, 0, 0, 0, 0);
      ObjectSetInteger(0, nome, OBJPROP_FILL, true);
      ObjectSetInteger(0, nome, OBJPROP_BACK, true);
      ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, nome, OBJPROP_HIDDEN, true);
     }

   ObjectSetInteger(0, nome, OBJPROP_COLOR, Misturar(cor, ZoneOpacity));
   ObjectSetDouble(0, nome, OBJPROP_PRICE, 0, de);
   ObjectSetDouble(0, nome, OBJPROP_PRICE, 1, ate);
   AncorarTempo(nome);
  }

//+------------------------------------------------------------------+
//| Mistura a cor com o fundo do grafico.                             |
//|                                                                   |
//| Substitui o alfa que o MQL5 nao oferece em objetos de grafico.    |
//| Opacidade 100 devolve o proprio fundo (invisivel); 0 devolve a    |
//| cor cheia.                                                         |
//+------------------------------------------------------------------+
color Misturar(const color cor, const int opacidade)
  {
   int o = (int)MathMax(0, MathMin(100, opacidade));
   color fundo = (color)ChartGetInteger(0, CHART_COLOR_BACKGROUND);

   int r = (int)((cor & 0xFF) * (100 - o) / 100 + (fundo & 0xFF) * o / 100);
   int g = (int)(((cor >> 8) & 0xFF) * (100 - o) / 100 + ((fundo >> 8) & 0xFF) * o / 100);
   int b = (int)(((cor >> 16) & 0xFF) * (100 - o) / 100 + ((fundo >> 16) & 0xFF) * o / 100);

   return((color)(r | (g << 8) | (b << 16)));
  }

void AncorarTempo(const string nome)
  {
   int visiveis = (int)ChartGetInteger(0, CHART_VISIBLE_BARS);
   int primeira = (int)ChartGetInteger(0, CHART_FIRST_VISIBLE_BAR);
   datetime inicio = iTime(_Symbol, _Period, MathMin(primeira, Bars(_Symbol, _Period) - 1));
   datetime fim = TimeCurrent() + PeriodSeconds(_Period) * MathMax(5, visiveis / 4);

   ObjectSetInteger(0, nome, OBJPROP_TIME, 0, inicio);
   ObjectSetInteger(0, nome, OBJPROP_TIME, 1, fim);
}

void Reancorar()
  {
   AncorarTempo(g_pfx + "zona_take");
   AncorarTempo(g_pfx + "zona_stop");
  }

//+------------------------------------------------------------------+
//| Painel                                                            |
//+------------------------------------------------------------------+
void DesenharPainelVazio()
  {
   if(!ShowPanel)
      return;
   Texto(0, "Hedge AI #" + IntegerToString(SetupId), clrSilver, PanelFontSize + 2);
   Texto(1, "aguardando o conector", clrGoldenrod, PanelFontSize);
   Texto(2, "anexe TraderTopHedgeConnector com o mesmo SetupId", clrGray, PanelFontSize - 1);
   for(int i = 3; i < 12; i++)
      Apagar("txt" + IntegerToString(i));
  }

void DesenharPainel()
  {
   if(!ShowPanel)
      return;

   int estado = (int)g_frame.state;
   int idade = (int)(TimeCurrent() - (datetime)g_frame.updated_at);
   bool vivo = (estado == TTH_ST_CONNECTED);

   color corEstado = clrSilver;
   if(estado == TTH_ST_CONNECTED)         corEstado = clrMediumSeaGreen;
   else if(estado == TTH_ST_STALE)        corEstado = clrDarkOrange;
   else if(estado == TTH_ST_MODEL_NA)     corEstado = clrGoldenrod;
   else if(estado == TTH_ST_DISCONNECTED) corEstado = clrOrangeRed;
   else if(estado == TTH_ST_INVALID_KEY)  corEstado = clrOrangeRed;
   else if(estado == TTH_ST_CLOSED)       corEstado = clrSilver;

   int linha = 0;
   Texto(linha++, "Hedge AI #" + IntegerToString(SetupId)
         + "  v" + IntegerToString((int)g_frame.setup_version),
         clrWhite, PanelFontSize + 2);

   Texto(linha++, TTH_StateLabel(estado)
         + ((idade >= 0 && idade < 86400) ? ("  ha " + IntegerToString(idade) + "s") : ""),
         corEstado, PanelFontSize);

   if(estado == TTH_ST_CLOSED)
     {
      Texto(linha++, TTH_CloseLabel((int)g_frame.closed_reason), clrSilver, PanelFontSize);
     }
   else
     {
      Texto(linha++, "prazo: " + TTH_Countdown((int)g_frame.seconds_left),
            clrLightGray, PanelFontSize);
     }

   Texto(linha++, ((int)g_frame.objective == TTH_OBJ_FAVOR_BROKER)
         ? "objetivo: lucro na corretora (stop da mesa)"
         : "objetivo: take da mesa",
         clrLightGray, PanelFontSize - 1);

   // --- probabilidades --------------------------------------------------
   // Vencida ou encerrada, o numero NAO aparece como percentual grande:
   // depois da validade o ultimo resultado pode ficar, mas identificado
   // como historico, nunca com aparencia de sinal atual.
   bool temPrev = (g_frame.has_forecast > 0.5 && g_frame.take_pct >= 0);
   if(temPrev && vivo)
     {
      Texto(linha++, "TAKE MESA  " + IntegerToString((int)g_frame.take_pct) + "%",
            clrMediumSeaGreen, PanelFontSize + 6);
      Texto(linha++, "STOP MESA  " + IntegerToString((int)g_frame.stop_pct) + "%",
            clrCrimson, PanelFontSize + 6);
      Texto(linha++, "nenhuma ate o prazo  " + IntegerToString((int)g_frame.none_pct) + "%",
            clrLightGray, PanelFontSize);
      Texto(linha++, TTH_ModelLabel((int)g_frame.model_kind)
            + " — nao e garantia de resultado", clrGray, PanelFontSize - 1);
     }
   else if(temPrev)
     {
      Texto(linha++, "ultima leitura: " + IntegerToString((int)g_frame.take_pct)
            + "% / " + IntegerToString((int)g_frame.stop_pct)
            + "% / " + IntegerToString((int)g_frame.none_pct) + "%  (historico)",
            clrGray, PanelFontSize);
     }
   else
     {
      Texto(linha++, "sem estimativa de probabilidade", clrGoldenrod, PanelFontSize);
     }

   // --- financeiro -------------------------------------------------------
   if(g_frame.has_financial > 0.5)
     {
      Texto(linha++, StringFormat("lote %.2f   residual %.2f",
                                  g_frame.lots, g_frame.residual),
            clrLightGray, PanelFontSize);
      Texto(linha++, StringFormat("take %.2f   stop %.2f   tempo %.2f",
                                  g_frame.net_take, g_frame.net_stop, g_frame.net_timeout),
            clrLightGray, PanelFontSize);
      if(g_frame.basis_risk > 0.5)
         Texto(linha++, "instrumentos diferentes: risco de base", clrDarkOrange,
               PanelFontSize - 1);
     }
   else
     {
      Texto(linha++, "resultado financeiro indisponivel", clrGoldenrod, PanelFontSize - 1);
     }

   for(int i = linha; i < 12; i++)
      Apagar("txt" + IntegerToString(i));
  }

void Texto(const int indice, const string conteudo, const color cor, const int tamanho)
  {
   string nome = g_pfx + "txt" + IntegerToString(indice);
   if(ObjectFind(0, nome) < 0)
      ObjectCreate(0, nome, OBJ_LABEL, 0, 0, 0);

   ObjectSetInteger(0, nome, OBJPROP_CORNER, PanelCorner);
   ObjectSetInteger(0, nome, OBJPROP_XDISTANCE, 12);
   ObjectSetInteger(0, nome, OBJPROP_YDISTANCE, 18 + indice * (PanelFontSize + 8));
   ObjectSetString(0, nome, OBJPROP_TEXT, conteudo);
   ObjectSetInteger(0, nome, OBJPROP_COLOR, cor);
   ObjectSetInteger(0, nome, OBJPROP_FONTSIZE, MathMax(6, tamanho));
   ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, nome, OBJPROP_HIDDEN, true);
  }

void Apagar(const string id)
  {
   string nome = g_pfx + id;
   if(ObjectFind(0, nome) >= 0)
      ObjectDelete(0, nome);
  }

void ApagarNiveis()
  {
   Apagar("entry");
   Apagar("take");
   Apagar("stop");
   Apagar("zona_take");
   Apagar("zona_stop");
  }

//+------------------------------------------------------------------+
//| Apaga SO os objetos desta sessao deste indicador.                 |
//|                                                                   |
//| Varrer por prefixo generico derrubaria o painel de outro setup    |
//| aberto no mesmo grafico.                                           |
//+------------------------------------------------------------------+
void ApagarMeusObjetos()
  {
   for(int i = ObjectsTotal(0) - 1; i >= 0; i--)
     {
      string nome = ObjectName(0, i);
      if(StringFind(nome, g_pfx) == 0)
         ObjectDelete(0, nome);
     }
  }
