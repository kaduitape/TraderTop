"""Quem escolhe entre comprar e vender quando a direcao e AUTO.

## Por que isto virou uma interface

Ate aqui a direcao automatica era uma linha:

    return SHORT if report.trend == DOWN else LONG

Funciona e e honesto, mas e grosseiro: ignora tudo que a analise ja
calculou alem da tendencia. Substituir essa linha por uma opiniao externa,
porem, nao pode ser feito apagando o que existe — nao ha nada neste
projeto que prove que a opiniao externa acerta mais.

Entao o desenho e uma CADEIA, nao uma troca:

- `SentimentDirectionProvider` usa o sentimento da MarketPulse/AIsa;
- `TrendDirectionProvider` mantem o comportamento antigo;
- o primeiro que tiver opiniao decide.

Um provedor ausente, fora do ar ou inseguro demais nunca derruba a tela:
ele apenas se cala e o proximo responde. Mas quando ninguem opina — e
mercado lateral e o caso normal disso — a cadeia devolve None em vez de
sortear um lado. Ficar sem direcao e um resultado, nao uma falha.

## O custo: zero chamada nova

`analyze_symbol` JA consulta a AIsa em toda analise, e o resultado ja vem
dentro do `AnalysisReport` como o fator `news` — atras do CoverageGuard,
do cache, do armazenamento e do teto diario. Ler esse fator aqui nao gasta
nem uma requisicao a mais.

Isso importa porque este projeto ja queimou a assinatura inteira uma vez,
com uma consulta por ciclo que nao virou nenhuma entrada. Qualquer coisa
que chame a AIsa de novo no laco do Pulso repetiria o episodio.

## O que isto NAO faz

Nao muda o score, nao muda a zona, nao muda o stop. Escolhe o LADO que
sera analisado; toda a geometria continua vindo dos mesmos motores. Um
provedor externo mexendo no score exigiria validacao que nao existe (ver
`docs/foto-analise.md`), e o vocabulario de confluencia deixaria de ser
verdade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.strategies.base import SignalDirection

SOURCE_HEURISTIC = "TENDENCIA"
SOURCE_SENTIMENT = "AISA_SENTIMENTO"
SOURCE_MANUAL = "OPERADOR"
"""Direcao forcada na tela. Nao e um provedor — e um desvio da cadeia —
mas entra na origem para que a comparacao nao misture escolha humana com
escolha automatica."""

NEUTRAL_SCORE = 50.0
"""O ponto morto do fator de noticias: 50 significa "sem inclinacao"."""

MIN_MARGIN = 8.0
"""Quanto o sentimento precisa se afastar de 50 para valer como direcao.

Nao e um numero otimizado — nao ha backtest aqui que o otimizasse. E o
reconhecimento de que 52 contra 48 nao e opiniao, e ruido: trocar uma
regra explicavel por um sorteio piora o sistema mesmo nas vezes em que
acerta."""


@dataclass(frozen=True, slots=True)
class DirectionDecision:
    """A direcao e de onde ela veio.

    `source` existe para a comparacao: sem registrar quem decidiu, nao ha
    como medir se o sentimento melhorou alguma coisa — a troca teria sido
    feita no escuro.
    """

    direction: SignalDirection
    source: str
    rationale: str = ""

    @property
    def from_sentiment(self) -> bool:
        return self.source == SOURCE_SENTIMENT


@dataclass(frozen=True, slots=True)
class DirectionContext:
    """O que os provedores consomem — tudo ja calculado.

    Campos simples de proposito, sem objetos do dominio: e o que mantem os
    provedores testaveis sem banco e sem rede.
    """

    symbol: str
    trend: str
    news_score: float | None = None
    """`raw_score` do fator `news` do relatorio. `None` quando a AIsa nao
    respondeu, nao cobre o ativo, ou nao esta configurada — casos em que
    `has_data` e False e o fator ja fica fora da conta do score."""


class DirectionProvider(Protocol):
    def decide(self, context: DirectionContext) -> DirectionDecision | None:
        """A decisao, ou None quando nao ha opiniao.

        `None` e resultado legitimo, nao erro: provedor sem cobertura, sem
        chave ou inseguro demais deve se calar para o proximo da cadeia
        decidir. Levantar excecao aqui derrubaria a tela por causa de um
        fator acessorio.
        """
        ...


class SentimentDirectionProvider:
    """Direcao pelo sentimento agregado das noticias (MarketPulse/AIsa).

    Le o fator que o relatorio ja traz. Acima de 50 + margem, o noticiario
    puxa para cima; abaixo de 50 - margem, para baixo; no meio, silencio.

    ## Onde ela nao opina, e por que isso e o esperado

    A AIsa cobre acoes e cripto — nao pares de moedas nem metais. Para
    XAUUSD, EURUSD e afins, `CoverageGuard` nem chega a consultar, o fator
    vem sem dados e este provedor devolve None. Nesses ativos a direcao
    continua sendo decidida pela tendencia, exatamente como antes.
    """

    def __init__(self, *, min_margin: float = MIN_MARGIN) -> None:
        self._margin = min_margin

    def decide(self, context: DirectionContext) -> DirectionDecision | None:
        score = context.news_score
        if score is None:
            return None

        distancia = score - NEUTRAL_SCORE
        if abs(distancia) < self._margin:
            return None

        comprando = distancia > 0
        return DirectionDecision(
            direction=SignalDirection.LONG if comprando else SignalDirection.SHORT,
            source=SOURCE_SENTIMENT,
            rationale=(
                f"sentimento das noticias em {score:.0f}/100 "
                f"({'comprador' if comprando else 'vendedor'})"
            ),
        )


class TrendDirectionProvider:
    """A regra que sempre existiu, agora nomeada.

    Mantida como implementacao de primeira classe, e nao como "fallback
    temporario": ela e o piso de comparacao. Se o sentimento nao superar
    isto, a resposta certa e continuar aqui.

    ## Lateral nao vira compra

    Alta devolve LONG, baixa devolve SHORT, e lateral devolve None. Nao e
    omissao: mercado sem tendencia nao da lado, e escolher compra por
    ausencia de sinal produziria um setup que ninguem defendeu. O painel
    segue mostrando o mapa de calor; o que ele deixa de fazer e fingir
    que ha um lado.
    """

    def decide(self, context: DirectionContext) -> DirectionDecision | None:
        tendencia = context.trend.upper()
        if tendencia == "UP":
            lado = SignalDirection.LONG
        elif tendencia == "DOWN":
            lado = SignalDirection.SHORT
        else:
            return None
        return DirectionDecision(
            direction=lado,
            source=SOURCE_HEURISTIC,
            rationale=f"tendencia {context.trend}",
        )


class FallbackDirectionProvider:
    """Encadeia provedores: o primeiro com opiniao decide.

    A ordem e sempre a mesma, e a heuristica fica por ultimo por ser o
    piso de comparacao — nao por ser infalivel. Quando NENHUM provedor
    opina, a cadeia devolve None, e isso e resposta: a tela mostra o mapa
    sem lado em vez de inventar um.
    """

    def __init__(self, *providers: DirectionProvider) -> None:
        if not providers:
            raise ValueError("a cadeia precisa de ao menos um provedor")
        self._providers = providers

    def decide(self, context: DirectionContext) -> DirectionDecision | None:
        for provedor in self._providers:
            decisao = provedor.decide(context)
            if decisao is not None:
                return decisao
        return None


def default_chain() -> FallbackDirectionProvider:
    """Sentimento primeiro, tendencia depois; nenhum dos dois e obrigado
    a opinar.

    O sentimento vem na frente porque um noticiario claramente inclinado
    e informacao que a tendencia nao carrega — inclusive em mercado
    lateral, onde a heuristica se cala. Isso nao contradiz "lateral nao
    vira compra": a diferenca e entre um lado que alguem afirmou e um
    lado escolhido por ausencia de alternativa.
    """
    return FallbackDirectionProvider(
        SentimentDirectionProvider(),
        TrendDirectionProvider(),
    )


def news_score_from(report) -> float | None:
    """O `raw_score` do fator `news`, ou None quando ele nao tem dados.

    `has_data=False` cobre tres situacoes que precisam do mesmo
    tratamento: sem chave, sem cobertura para o ativo, e API fora do ar.
    Nos tres, a AIsa nao tem opiniao — e "nao sei" nao pode virar 50, que
    seria uma opiniao neutra que ninguem emitiu.
    """
    for fator in getattr(report.score, "factors", []):
        if fator.name == "news":
            return fator.raw_score if fator.has_data else None
    return None
