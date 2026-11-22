//+------------------------------------------------------------------+
//| TraderTopHedgeIndicator.mq5                                       |
//| Desenha o cenario do Hedge AI. Nao fala com a rede.               |
//|                                                                   |
//| Le o canal local que o EA `TraderTopHedgeConnector` publica (ver  |
//| `Include/TraderTop/HedgeChannel.mqh`). Nao ha `WebRequest` aqui — |
//| indicadores rodam na thread de interface e a plataforma recusa.   |
//|                                                                   |
//| O QUE A TELA RESPONDE                                              |
//|                                                                   |
//| A pergunta do operador no meio do pregao nao e "quais sao os      |
//| numeros": e "o que esta acontecendo agora". A primeira linha do    |
//| painel e um VEREDITO — qual barreira esta mais perto, a quantos   |
//| ticks, e quanto custa se ela for tocada. O resto do painel        |
//| sustenta esse veredito; ele nao precisa ser lido para agir.        |
//|                                                                   |
//| O veredito DESCREVE, nunca recomenda. Nao existe aqui "feche",    |
//| "aumente" ou "saia": a decisao e de quem tem o dinheiro na mesa,  |
//| e um indicador que a tomasse estaria operando sem assumir o risco.|
//|                                                                   |
//| CORES E O OBJETIVO                                                |
//|                                                                   |
//| No modo "favorecer corretora" a regiao do STOP da mesa fica verde |
//| e a do TAKE fica vermelha — porque o que se quer e o stop. No modo|
//| "favorecer mesa" inverte. Os NOMES dos niveis nunca mudam: o take |
//| da mesa continua sendo o take da mesa, so a cor diz se ele e bem  |
//| ou mal vindo.                                                      |
//|                                                                   |
//| ONDE CADA REGIAO FICA                                              |
//|                                                                   |
//| Depende de comprado ou vendido, nunca de cima/baixo fixo. Numa    |
//| venda o take fica ABAIXO da entrada, e fixar verde em cima poria  |
//| a cor certa no nivel errado em toda operacao vendida.              |
//|                                                                   |
//| REDESENHO                                                          |
//|                                                                   |
//| Toda alteracao de objeto termina em `ChartRedraw()`. Sem isso o   |
//| terminal so repinta quando chega tick — e com o mercado fechado,  |
//| ou num ativo parado, o painel inteiro fica invisivel. Era a causa |
//| de "nao aparece nada": os objetos existiam, ninguem os pintava.    |
//|                                                                   |
//| OBJETOS                                                            |
//|                                                                   |
//| Todos com o prefixo desta sessao. Ao remover o indicador, so os   |
//| proprios objetos sao apagados — nunca os de outro indicador nem   |
//| os de outro grafico. E eles sao ATUALIZADOS, nao recriados: criar |
//| e destruir a cada tick pisca a tela e consome CPU a toa.           |
//+------------------------------------------------------------------+
#property copyright "AI Trader PRO"
#property version   "1.10"
#property strict
#property indicator_chart_window
#property indicator_plots 0
#property description "Veredito, distancia ate as barreiras, cobertura e probabilidades do Hedge AI. Nao envia ordens e nao faz requisicoes de rede."

#include <TraderTop/HedgeChannel.mqh>

//--- Identificacao (precisa casar com o conector)
input long   SetupId          = 0;      // Mesmo SetupId do conector
input string SymbolOverride   = "";     // Mesmo SymbolOverride do conector
input string ChartIdOverride  = "";     // Mesmo valor nos dois quando em graficos separados

//--- Aparencia
input bool   ShowPanel        = true;   // Painel
input int    PanelCorner      = 0;      // 0=sup.esq 1=sup.dir 2=inf.esq 3=inf.dir
input int    PanelX           = 12;     // Distancia do canto, horizontal
input int    PanelY           = 16;     // Distancia do canto, vertical
input int    PanelWidth       = 300;    // Largura do painel em pixels
input int    PanelFontSize    = 10;
input bool   ShowZones        = true;   // Pintar as regioes ate as barreiras
input int    ZoneOpacity      = 88;     // 0-100 (maior = mais transparente)
input color  EntryColor       = clrGold;

//--- Aviso de aproximacao
input int    AlertTicks       = 0;      // Avisar a N ticks da barreira (0 = nunca)
input bool   AlertSound       = true;   // Som junto com o aviso

#define PFX_BASE "TTHI_"
#define FONTE    "Arial"
#define FONTE_B  "Arial Bold"

//--- Cores do painel
#define COR_FUNDO     C'22,26,32'
#define COR_BORDA     C'58,66,78'
#define COR_TRILHA    C'48,54,64'
#define COR_TENUE     C'128,138,152'
#define COR_VERDE     C'46,190,120'
#define COR_VERMELHO  C'232,76,86'
#define COR_LARANJA   C'240,160,48'
#define COR_AZUL      C'96,168,240'

string   g_pfx     = "";
string   g_tag     = "";
string   g_symbol  = "";
string   g_moeda   = "";
long     g_chart   = 0;
double   g_last_seq = -1;
TTHFrame g_frame;
bool     g_tem_quadro = false;
int      g_avisado = 0;   // 0=nenhum 1=take 2=stop; evita repetir o mesmo aviso

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
   g_moeda = AccountInfoString(ACCOUNT_CURRENCY);

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
//| Um ciclo de atualizacao.                                          |
//|                                                                   |
//| Redesenha SEMPRE, mesmo com `seq` igual: o painel mostra idade e  |
//| contagem de prazo, que mudam a cada segundo. Repintar so quando a |
//| sequencia muda deixaria um cronometro paralisado na tela, que e   |
//| pior que nenhum cronometro — parece dado vivo e nao e.             |
//+------------------------------------------------------------------+
void Atualizar()
  {
   TTHFrame f;
   if(!TTH_ReadFrame(g_tag, f))
     {
      // Quadro pela metade ou EA de outra versao: mantem o anterior. O
      // painel ja mostra a idade, entao o operador ve que envelheceu.
      if(!g_tem_quadro)
        {
         DesenharPainelVazio();
         ChartRedraw();
         return;
        }
      DesenharPainel();
      ChartRedraw();
      return;
     }

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
   Avisar();
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Medidas do momento                                                |
//+------------------------------------------------------------------+
double TickDaMesa()
  {
   // O tick da MESA vem do servidor. O do grafico e plano B: o indicador
   // pode estar num grafico da corretora, cujo tick as vezes e outro.
   if(g_frame.desk_tick_size > 0)
      return(g_frame.desk_tick_size);
   double t = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(t > 0)
      return(t);
   return(_Point > 0 ? _Point : 0.01);
  }

int TicksAte(const double nivel)
  {
   if(nivel <= 0 || g_frame.price <= 0)
      return(-1);
   return((int)MathRound(MathAbs(nivel - g_frame.price) / TickDaMesa()));
  }

//+------------------------------------------------------------------+
//| Fracao do caminho entre stop (0) e take (1).                      |
//|                                                                   |
//| Serve para comprado e vendido sem ramo: numa venda o take esta    |
//| ABAIXO do stop, e a divisao inverte o sinal junto.                 |
//+------------------------------------------------------------------+
double FracaoNoTrilho()
  {
   double faixa = g_frame.take - g_frame.stop;
   if(faixa == 0)
      return(0.5);
   double f = (g_frame.price - g_frame.stop) / faixa;
   return(MathMax(0.0, MathMin(1.0, f)));
  }

//+------------------------------------------------------------------+
//| Aviso de aproximacao.                                             |
//|                                                                   |
//| Dispara UMA vez por entrada na faixa. Repetir a cada segundo       |
//| treinaria o operador a ignorar o aviso, que e o mesmo que nao ter.|
//+------------------------------------------------------------------+
void Avisar()
  {
   if(AlertTicks <= 0 || (int)g_frame.state != TTH_ST_CONNECTED)
     {
      g_avisado = 0;
      return;
     }

   int ate_take = TicksAte(g_frame.take);
   int ate_stop = TicksAte(g_frame.stop);

   int perto = 0;
   if(ate_stop >= 0 && ate_stop <= AlertTicks
      && (ate_take < 0 || ate_stop <= ate_take))
      perto = 2;
   else if(ate_take >= 0 && ate_take <= AlertTicks)
      perto = 1;

   if(perto == 0)
     {
      g_avisado = 0;   // saiu da faixa: o proximo aproximar avisa de novo
      return;
     }
   if(perto == g_avisado)
      return;

   g_avisado = perto;
   string texto = (perto == 2)
                  ? StringFormat("Hedge AI #%d: stop da mesa a %d ticks", (int)SetupId, ate_stop)
                  : StringFormat("Hedge AI #%d: take da mesa a %d ticks", (int)SetupId, ate_take);
   Alert(texto);
   if(AlertSound)
      PlaySound("alert.wav");
  }

//+------------------------------------------------------------------+
//| Linhas e regioes                                                  |
//+------------------------------------------------------------------+
void DesenharNiveis()
  {
   bool favoreceCorretora = ((int)g_frame.objective == TTH_OBJ_FAVOR_BROKER);

   // A cor diz se a regiao e bem-vinda; o nome do nivel nao muda.
   color corTake = favoreceCorretora ? COR_VERMELHO : COR_VERDE;
   color corStop = favoreceCorretora ? COR_VERDE : COR_VERMELHO;

   int ate_take = TicksAte(g_frame.take);
   int ate_stop = TicksAte(g_frame.stop);

   Linha("entry", g_frame.entry, EntryColor, STYLE_SOLID, 2, "ENTRADA MESA");
   Linha("take",  g_frame.take,  corTake, STYLE_DASH, 2,
         "TAKE MESA" + Distancia(ate_take)
         + Sufixo(favoreceCorretora ? "evitar" : "desejado"));
   Linha("stop",  g_frame.stop,  corStop, STYLE_DASH, 2,
         "STOP MESA" + Distancia(ate_stop)
         + Sufixo(favoreceCorretora ? "desejado" : "evitar"));

   if(!ShowZones)
     {
      Apagar("zona_take");
      Apagar("zona_stop");
      return;
     }

   // A posicao segue o LADO. Numa venda o take fica abaixo do preco.
   //
   // A INTENSIDADE segue a DISTANCIA. Com opacidade fixa, a regiao do
   // alvo distante cobria a tela inteira de verde enquanto o stop estava
   // a 32 ticks — a cor mais visivel do grafico dizia o contrario do que
   // estava acontecendo. Agora a barreira perto aparece e a longe some,
   // e a cor dominante e sempre a do desfecho iminente.
   Regiao("zona_take", g_frame.price, g_frame.take, corTake,
          OpacidadePorDistancia(ate_take, ate_stop));
   Regiao("zona_stop", g_frame.price, g_frame.stop, corStop,
          OpacidadePorDistancia(ate_stop, ate_take));
  }

//+------------------------------------------------------------------+
//| Opacidade de uma regiao, dada a distancia ate a SUA barreira e a  |
//| distancia ate a outra.                                            |
//|                                                                   |
//| Devolve `ZoneOpacity` (o configurado, mais visivel) quando a       |
//| barreira esta colada, e 100 (invisivel) quando esta no extremo    |
//| oposto. Preco no meio deixa as duas em intensidade igual.          |
//+------------------------------------------------------------------+
int OpacidadePorDistancia(const int minha, const int outra)
  {
   if(minha < 0 || outra < 0)
      return(ZoneOpacity);
   int soma = minha + outra;
   if(soma <= 0)
      return(ZoneOpacity);
   double fracao = (double)minha / (double)soma;
   return((int)MathRound(ZoneOpacity + (100 - ZoneOpacity) * fracao));
  }

string Distancia(const int ticks)
  {
   if(ticks < 0)
      return("");
   return("  " + IntegerToString(ticks) + " ticks");
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
void Regiao(const string id, const double de, const double ate, const color cor,
            const int opacidade)
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

   ObjectSetInteger(0, nome, OBJPROP_COLOR, Misturar(cor, opacidade));
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
//| Geometria do painel                                               |
//|                                                                   |
//| O painel e montado em pixels (`OBJ_RECTANGLE_LABEL` e `OBJ_LABEL`)|
//| e nao na escala de precos: ele precisa ficar parado quando o       |
//| grafico rola ou muda de zoom.                                      |
//|                                                                   |
//| `Px` e `Py` existem porque nos cantos da DIREITA o MetaTrader     |
//| conta X da borda direita para dentro, e nos de BAIXO conta Y de   |
//| baixo para cima. Sem essa conversao, escolher canto 1 jogava o    |
//| painel inteiro para fora da tela — e o sintoma era "o indicador   |
//| nao mostra nada".                                                  |
//+------------------------------------------------------------------+
int  g_altura = 0;   // altura total da ultima montagem, usada por `Py`

bool Direita()
  {
   return(PanelCorner == 1 || PanelCorner == 3);
  }

bool Baixo()
  {
   return(PanelCorner == 2 || PanelCorner == 3);
  }

int Px(const int offset, const int largura)
  {
   if(Direita())
      return(PanelX + PanelWidth - offset - largura);
   return(PanelX + offset);
  }

int Py(const int offset, const int altura)
  {
   if(Baixo())
      return(PanelY + g_altura - offset - altura);
   return(PanelY + offset);
  }

int Lh()
  {
   return(PanelFontSize + 8);
  }

//+------------------------------------------------------------------+
//| Painel                                                            |
//+------------------------------------------------------------------+
void DesenharPainelVazio()
  {
   if(!ShowPanel)
      return;

   int lh = Lh();
   g_altura = 44 + lh * 3;

   Caixa("bg", 0, 0, PanelWidth, g_altura, COR_FUNDO, COR_BORDA);
   Caixa("hdr", 0, 0, PanelWidth, 26, COR_TRILHA, COR_TRILHA);
   Rotulo("t0", 10, 6, "HEDGE AI #" + IntegerToString(SetupId), clrWhite,
          PanelFontSize, true);

   int y = 36;
   Rotulo("t1", 10, y, "SEM CONECTOR", COR_LARANJA, PanelFontSize + 8, true);
   y += lh + 14;
   Rotulo("t2", 10, y, "anexe TraderTopHedgeConnector", COR_TENUE, PanelFontSize);
   y += lh;
   Rotulo("t3", 10, y, "com SetupId " + IntegerToString(SetupId)
          + " e o mesmo ChartIdOverride", COR_TENUE, PanelFontSize - 1);

   LimparSobras(4);
  }

void DesenharPainel()
  {
   if(!ShowPanel)
      return;

   int estado = (int)g_frame.state;
   int idade  = (int)(TimeCurrent() - (datetime)g_frame.updated_at);
   bool vivo  = (estado == TTH_ST_CONNECTED);
   bool temPrev = (g_frame.has_forecast > 0.5 && g_frame.take_pct >= 0);
   bool temFin  = (g_frame.has_financial > 0.5);
   bool temTrilho = (g_frame.take > 0 && g_frame.stop > 0 && g_frame.price > 0);

   int lh = Lh();

   // Altura primeiro: `Py` precisa dela para os cantos de baixo.
   g_altura = 26 + 10                      // cabecalho
              + (PanelFontSize + 14) + 6   // veredito
              + lh                         // sub-veredito
              + (temTrilho ? (18 + lh) : 0)
              // `vivo` entra aqui porque o bloco de probabilidade encolhe
              // de quatro linhas para uma quando o quadro envelhece: so
              // `temPrev` reservaria espaco para barras que nao serao
              // desenhadas, e a caixa fecharia bem abaixo do texto.
              + ((temPrev && vivo) ? (lh * 4 + 6) : lh)
              + (temFin ? (lh * 3 + 6) : lh)
              + lh + 10;                   // rodape

   Caixa("bg", 0, 0, PanelWidth, g_altura, COR_FUNDO, COR_BORDA);

   // --- cabecalho --------------------------------------------------------
   color corEstado = CorDoEstado(estado);
   Caixa("hdr", 0, 0, PanelWidth, 26, COR_TRILHA, COR_TRILHA);
   Caixa("pulso", 0, 0, 4, 26, corEstado, corEstado);
   Rotulo("t0", 10, 6, "HEDGE AI #" + IntegerToString(SetupId)
          + "  v" + IntegerToString((int)g_frame.setup_version),
          clrWhite, PanelFontSize, true);
   RotuloDir("t1", 10, 6, TTH_StateLabel(estado)
             + ((idade >= 0 && idade < 86400) ? ("  " + IntegerToString(idade) + "s") : ""),
             corEstado, PanelFontSize);

   int y = 36;
   int linha = 2;

   // --- veredito ---------------------------------------------------------
   // A unica linha que precisa ser lida. Tudo abaixo dela e sustentacao.
   string veredito = "";
   string detalhe  = "";
   color  corV     = COR_TENUE;
   Veredito(estado, temPrev, temFin, veredito, detalhe, corV);

   Rotulo("t" + IntegerToString(linha++), 10, y, veredito, corV,
          PanelFontSize + 10, true);
   y += PanelFontSize + 14 + 6;
   Rotulo("t" + IntegerToString(linha++), 10, y, detalhe,
          (StringLen(detalhe) > 0 ? clrWhite : COR_TENUE), PanelFontSize);
   y += lh;

   // --- trilho stop <-> take --------------------------------------------
   if(temTrilho)
     {
      int trilhoX = 10;
      int trilhoW = PanelWidth - 20;
      bool favoreceCorretora = ((int)g_frame.objective == TTH_OBJ_FAVOR_BROKER);
      color corTake = favoreceCorretora ? COR_VERMELHO : COR_VERDE;
      color corStop = favoreceCorretora ? COR_VERDE : COR_VERMELHO;

      Caixa("trilho", trilhoX, y, trilhoW, 8, COR_TRILHA, COR_TRILHA);
      // Pontas coloridas: a cor diz de que lado esta o desfecho desejado,
      // e ela segue o objetivo, nao a posicao na tela.
      Caixa("trilho_s", trilhoX, y, 14, 8, corStop, corStop);
      Caixa("trilho_t", trilhoX + trilhoW - 14, y, 14, 8, corTake, corTake);

      int px = trilhoX + (int)MathRound(FracaoNoTrilho() * (trilhoW - 6));
      Caixa("agora", px, y - 4, 6, 16, clrWhite, clrWhite);
      y += 18;

      int ate_stop = TicksAte(g_frame.stop);
      int ate_take = TicksAte(g_frame.take);
      Rotulo("t" + IntegerToString(linha++), 10, y,
             "stop " + IntegerToString(ate_stop) + " tk", corStop,
             PanelFontSize - 1);
      RotuloDir("t" + IntegerToString(linha++), 10, y,
                IntegerToString(ate_take) + " tk take", corTake,
                PanelFontSize - 1);
      y += lh;
     }
   else
     {
      Apagar("trilho"); Apagar("trilho_s"); Apagar("trilho_t"); Apagar("agora");
     }

   // --- probabilidades ---------------------------------------------------
   // Vencida ou encerrada, o numero NAO aparece como barra: depois da
   // validade o ultimo resultado pode ficar, mas identificado como
   // historico, nunca com aparencia de sinal atual.
   if(temPrev && vivo)
     {
      Rotulo("t" + IntegerToString(linha++), 10, y,
             "TOCAR PRIMEIRO ate o prazo", COR_TENUE, PanelFontSize - 1);
      y += lh;
      BarraPct("pb_take", y, (int)g_frame.take_pct, COR_VERDE);
      Rotulo("t" + IntegerToString(linha++), 10, y, "take", COR_TENUE,
             PanelFontSize - 1);
      RotuloDir("t" + IntegerToString(linha++), 10, y,
                IntegerToString((int)g_frame.take_pct) + "%", COR_VERDE,
                PanelFontSize - 1);
      y += lh;
      BarraPct("pb_stop", y, (int)g_frame.stop_pct, COR_VERMELHO);
      Rotulo("t" + IntegerToString(linha++), 10, y, "stop", COR_TENUE,
             PanelFontSize - 1);
      RotuloDir("t" + IntegerToString(linha++), 10, y,
                IntegerToString((int)g_frame.stop_pct) + "%", COR_VERMELHO,
                PanelFontSize - 1);
      y += lh;
      Rotulo("t" + IntegerToString(linha++), 10, y,
             "nenhuma  " + IntegerToString((int)g_frame.none_pct) + "%",
             COR_TENUE, PanelFontSize - 1);
      y += lh + 6;
     }
   else if(temPrev)
     {
      Apagar("pb_take"); Apagar("pb_take_f");
      Apagar("pb_stop"); Apagar("pb_stop_f");
      Rotulo("t" + IntegerToString(linha++), 10, y,
             "ultima leitura " + IntegerToString((int)g_frame.take_pct)
             + "/" + IntegerToString((int)g_frame.stop_pct)
             + "/" + IntegerToString((int)g_frame.none_pct) + "  (historico)",
             COR_TENUE, PanelFontSize - 1);
      y += lh;
     }
   else
     {
      Apagar("pb_take"); Apagar("pb_take_f");
      Apagar("pb_stop"); Apagar("pb_stop_f");
      Rotulo("t" + IntegerToString(linha++), 10, y,
             "sem estimativa de probabilidade", COR_LARANJA, PanelFontSize - 1);
      y += lh;
     }

   // --- cobertura e financeiro ------------------------------------------
   if(temFin)
     {
      Rotulo("t" + IntegerToString(linha++), 10, y, TextoCobertura(),
             CorCobertura(), PanelFontSize, true);
      y += lh;
      Rotulo("t" + IntegerToString(linha++), 10, y,
             StringFormat("lote %.2f   descoberto %.2f/pt",
                          g_frame.lots, g_frame.residual),
             COR_TENUE, PanelFontSize - 1);
      y += lh;
      Rotulo("t" + IntegerToString(linha++), 10, y,
             "se take " + Dinheiro(g_frame.net_take)
             + "   se stop " + Dinheiro(g_frame.net_stop),
             clrWhite, PanelFontSize - 1);
      y += lh + 6;
     }
   else
     {
      Rotulo("t" + IntegerToString(linha++), 10, y,
             "resultado financeiro indisponivel", COR_LARANJA,
             PanelFontSize - 1);
      y += lh;
     }

   // --- rodape -----------------------------------------------------------
   string rodape = "";
   if(estado == TTH_ST_CLOSED)
      rodape = TTH_CloseLabel((int)g_frame.closed_reason);
   else
      rodape = "prazo " + TTH_Countdown((int)g_frame.seconds_left);

   rodape += "  ·  " + TTH_ModelLabel((int)g_frame.model_kind);
   if(g_frame.basis_risk > 0.5)
      rodape += "  ·  risco de base";
   Rotulo("t" + IntegerToString(linha++), 10, y, rodape, COR_TENUE,
          PanelFontSize - 1);

   LimparSobras(linha);
  }

//+------------------------------------------------------------------+
//| O veredito.                                                       |
//|                                                                   |
//| Ordem de precedencia deliberada: um problema de DADO vence         |
//| qualquer numero. Anunciar "stop a 12 ticks" sobre um quadro de    |
//| 40 minutos atras seria a pior tela possivel — urgente, precisa e  |
//| falsa.                                                             |
//|                                                                   |
//| Nunca recomenda acao. Descreve o que esta mais perto e quanto     |
//| custa; o que fazer com isso e de quem tem a posicao.               |
//+------------------------------------------------------------------+
void Veredito(const int estado, const bool temPrev, const bool temFin,
              string &titulo, string &detalhe, color &cor)
  {
   if(estado == TTH_ST_INVALID_KEY)
     {
      titulo = "CHAVE RECUSADA";
      detalhe = "painel > Hedge AI > Chaves";
      cor = COR_VERMELHO;
      return;
     }
   if(estado == TTH_ST_DISCONNECTED)
     {
      titulo = "SEM PAINEL";
      detalhe = "o conector nao alcanca o servidor";
      cor = COR_VERMELHO;
      return;
     }
   if(estado == TTH_ST_WAITING)
     {
      titulo = "AGUARDANDO";
      detalhe = "sem cotacao do ativo ainda";
      cor = COR_LARANJA;
      return;
     }
   if(estado == TTH_ST_STALE)
     {
      titulo = "DADOS VELHOS";
      detalhe = "nao decida por esta tela";
      cor = COR_LARANJA;
      return;
     }
   if(estado == TTH_ST_CLOSED)
     {
      titulo = "ENCERRADO";
      detalhe = TTH_CloseLabel((int)g_frame.closed_reason);
      cor = COR_TENUE;
      return;
     }

   int ate_take = TicksAte(g_frame.take);
   int ate_stop = TicksAte(g_frame.stop);
   if(ate_take < 0 || ate_stop < 0)
     {
      titulo = "SEM NIVEIS";
      detalhe = "o setup nao tem entrada, take e stop";
      cor = COR_LARANJA;
      return;
     }

   bool favoreceCorretora = ((int)g_frame.objective == TTH_OBJ_FAVOR_BROKER);
   bool stopMaisPerto = (ate_stop <= ate_take);

   if(stopMaisPerto)
     {
      titulo = "STOP  " + IntegerToString(ate_stop) + " TICKS";
      cor = favoreceCorretora ? COR_VERDE : COR_VERMELHO;
     }
   else
     {
      titulo = "TAKE  " + IntegerToString(ate_take) + " TICKS";
      cor = favoreceCorretora ? COR_VERMELHO : COR_VERDE;
     }

   // O detalhe responde "e se acontecer": o numero que decide.
   if(temFin)
     {
      double liquido = stopMaisPerto ? g_frame.net_stop : g_frame.net_take;
      detalhe = "se tocar: " + Dinheiro(liquido) + " liquido";
     }
   else if(temPrev)
     {
      int pct = stopMaisPerto ? (int)g_frame.stop_pct : (int)g_frame.take_pct;
      detalhe = IntegerToString(pct) + "% de tocar primeiro";
     }
   else
      detalhe = "sem financeiro e sem previsao";
  }

//+------------------------------------------------------------------+
//| Cobertura em texto.                                                |
//|                                                                   |
//| O numero sozinho nao basta: 95% e 190% sao erros OPOSTOS. Abaixo  |
//| de 100 sobra descoberto e a mesa manda no resultado; acima, o     |
//| "hedge" virou posicao a favor da corretora. A faixa de 2% existe  |
//| porque avisar quando esta certo treina a ignorar o aviso.          |
//+------------------------------------------------------------------+
string TextoCobertura()
  {
   if(g_frame.coverage < 0)
      return("cobertura nao informada");

   int pct = (int)MathRound(g_frame.coverage * 100.0);
   string base = "COBERTURA " + IntegerToString(pct) + "%";
   if(pct >= 98 && pct <= 102)
      return(base + "  ·  neutra");
   if(pct < 98)
      return(base + "  ·  sobra descoberto");
   return(base + "  ·  excede, vira direcional");
  }

color CorCobertura()
  {
   if(g_frame.coverage < 0)
      return(COR_TENUE);
   int pct = (int)MathRound(g_frame.coverage * 100.0);
   if(pct >= 98 && pct <= 102)
      return(COR_VERDE);
   if(pct >= 85 && pct <= 115)
      return(COR_LARANJA);
   return(COR_VERMELHO);
  }

color CorDoEstado(const int estado)
  {
   if(estado == TTH_ST_CONNECTED)      return(COR_VERDE);
   if(estado == TTH_ST_STALE)          return(COR_LARANJA);
   if(estado == TTH_ST_MODEL_NA)       return(COR_LARANJA);
   if(estado == TTH_ST_DISCONNECTED)   return(COR_VERMELHO);
   if(estado == TTH_ST_INVALID_KEY)    return(COR_VERMELHO);
   return(COR_TENUE);
  }

//+------------------------------------------------------------------+
//| Dinheiro com sinal explicito.                                     |
//|                                                                   |
//| O "+" no lucro nao e enfeite: lido de relance, "17.50" e          |
//| "-17.50" se parecem, e sao desfechos opostos.                      |
//+------------------------------------------------------------------+
string Dinheiro(const double valor)
  {
   string sinal = (valor > 0) ? "+" : "";
   return(sinal + DoubleToString(valor, 2) + " " + g_moeda);
  }

//+------------------------------------------------------------------+
//| Barra proporcional. Trilha cheia atras, preenchimento na frente.  |
//+------------------------------------------------------------------+
void BarraPct(const string id, const int y, const int pct, const color cor)
  {
   int x = 10 + 46;
   int w = PanelWidth - 20 - 46 - 44;
   if(w < 20)
      w = 20;

   Caixa(id, x, y + 3, w, 7, COR_TRILHA, COR_TRILHA);
   int cheio = (int)MathRound(w * MathMax(0, MathMin(100, pct)) / 100.0);
   if(cheio <= 0)
     {
      Apagar(id + "_f");
      return;
     }
   Caixa(id + "_f", x, y + 3, cheio, 7, cor, cor);
  }

//+------------------------------------------------------------------+
//| Primitivas de desenho                                             |
//+------------------------------------------------------------------+
void Caixa(const string id, const int x, const int y, const int w, const int h,
           const color fundo, const color borda)
  {
   string nome = g_pfx + id;
   if(ObjectFind(0, nome) < 0)
     {
      ObjectCreate(0, nome, OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, nome, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, nome, OBJPROP_BORDER_TYPE, BORDER_FLAT);
      ObjectSetInteger(0, nome, OBJPROP_BACK, false);
     }

   ObjectSetInteger(0, nome, OBJPROP_CORNER, PanelCorner);
   ObjectSetInteger(0, nome, OBJPROP_XDISTANCE, Px(x, w));
   ObjectSetInteger(0, nome, OBJPROP_YDISTANCE, Py(y, h));
   ObjectSetInteger(0, nome, OBJPROP_XSIZE, MathMax(1, w));
   ObjectSetInteger(0, nome, OBJPROP_YSIZE, MathMax(1, h));
   ObjectSetInteger(0, nome, OBJPROP_BGCOLOR, fundo);
   ObjectSetInteger(0, nome, OBJPROP_COLOR, borda);
  }

void Rotulo(const string id, const int x, const int y, const string conteudo,
            const color cor, const int tamanho, const bool negrito = false)
  {
   Texto(id, x, y, conteudo, cor, tamanho, negrito, false);
  }

void RotuloDir(const string id, const int x, const int y, const string conteudo,
               const color cor, const int tamanho, const bool negrito = false)
  {
   Texto(id, x, y, conteudo, cor, tamanho, negrito, true);
  }

//+------------------------------------------------------------------+
//| Texto do painel.                                                  |
//|                                                                   |
//| A ancora acompanha o canto: num canto da direita o texto precisa  |
//| crescer para a esquerda, senao sai da tela. Era a segunda causa   |
//| de "nao aparece nada" — `PanelCorner=1` desenhava tudo fora.       |
//+------------------------------------------------------------------+
void Texto(const string id, const int x, const int y, const string conteudo,
           const color cor, const int tamanho, const bool negrito,
           const bool alinharNaOutraPonta)
  {
   string nome = g_pfx + id;
   if(ObjectFind(0, nome) < 0)
     {
      ObjectCreate(0, nome, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, nome, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, nome, OBJPROP_HIDDEN, true);
     }

   // A ANCORA so depende do alinhamento pedido: um texto que comeca a
   // esquerda cresce para a direita em qualquer canto. O que muda com o
   // canto e a DISTANCIA, porque a direita o X e contado de tras para
   // frente — e as duas coisas invertem juntas, dai o `!=`.
   bool espelhado = (Direita() != alinharNaOutraPonta);
   int xd = espelhado ? (PanelX + PanelWidth - x) : (PanelX + x);

   ObjectSetInteger(0, nome, OBJPROP_CORNER, PanelCorner);
   ObjectSetInteger(0, nome, OBJPROP_ANCHOR,
                    alinharNaOutraPonta ? ANCHOR_RIGHT_UPPER : ANCHOR_LEFT_UPPER);
   ObjectSetInteger(0, nome, OBJPROP_XDISTANCE, xd);
   // Altura zero: a ancora do texto e o canto SUPERIOR, e `Py` desconta a
   // altura para posicionar caixas pelo canto inferior. Descontar aqui
   // empurraria todo texto uma linha para baixo nos cantos de baixo.
   ObjectSetInteger(0, nome, OBJPROP_YDISTANCE, Py(y, 0));
   ObjectSetString(0, nome, OBJPROP_TEXT, conteudo);
   ObjectSetString(0, nome, OBJPROP_FONT, negrito ? FONTE_B : FONTE);
   ObjectSetInteger(0, nome, OBJPROP_COLOR, cor);
   ObjectSetInteger(0, nome, OBJPROP_FONTSIZE, MathMax(6, tamanho));
  }

//+------------------------------------------------------------------+
//| Apaga os rotulos que a montagem anterior usava e esta nao.        |
//|                                                                   |
//| Sem isso, uma tela que encurta (de conectado para encerrado, por  |
//| exemplo) deixaria as linhas antigas no lugar, misturando dois     |
//| estados na mesma leitura.                                          |
//+------------------------------------------------------------------+
void LimparSobras(const int apartirDe)
  {
   for(int i = apartirDe; i < 40; i++)
      Apagar("t" + IntegerToString(i));
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
