"""A cadeia que escolhe o lado quando a direcao e AUTO.

O risco aqui nao e "escolheu errado" — nao ha como saber isso sem
backtest. E dois outros:

1. **Silencio virar opiniao.** A AIsa nao cobre pares de moedas nem
   metais. Se "sem dados" virasse 50, o sistema leria ausencia de
   noticia como noticia neutra, e um fator que ninguem mediu entraria na
   decisao.

2. **Perder o piso.** Provedor fora do ar, sem chave ou inseguro nao pode
   deixar a tela sem direcao. A heuristica de tendencia tem que continuar
   respondendo sempre.
"""

from __future__ import annotations

from app.foto_analise.direction import (
    MIN_MARGIN,
    SOURCE_HEURISTIC,
    SOURCE_SENTIMENT,
    DirectionContext,
    FallbackDirectionProvider,
    SentimentDirectionProvider,
    TrendDirectionProvider,
    default_chain,
    news_score_from,
)
from app.strategies.base import SignalDirection


def _contexto(**overrides) -> DirectionContext:
    base = {"symbol": "TESTE", "trend": "UP", "news_score": None}
    base.update(overrides)
    return DirectionContext(**base)


# --- sentimento ------------------------------------------------------------


def test_bullish_news_choose_long() -> None:
    decisao = SentimentDirectionProvider().decide(_contexto(news_score=80.0))

    assert decisao.direction == SignalDirection.LONG
    assert decisao.source == SOURCE_SENTIMENT


def test_bearish_news_choose_short() -> None:
    """Contra a tendencia de alta: se o noticiario pesa para baixo, e ele
    que decide — senao o provedor nao serviria para nada."""
    decisao = SentimentDirectionProvider().decide(
        _contexto(trend="UP", news_score=20.0)
    )

    assert decisao.direction == SignalDirection.SHORT


def test_news_near_neutral_have_no_opinion() -> None:
    """52 contra 48 nao e opiniao, e ruido. Trocar uma regra explicavel
    por um sorteio piora o sistema mesmo nas vezes em que acerta."""
    assert SentimentDirectionProvider().decide(_contexto(news_score=52.0)) is None
    assert SentimentDirectionProvider().decide(_contexto(news_score=48.0)) is None


def test_the_margin_is_the_boundary() -> None:
    provedor = SentimentDirectionProvider()

    assert provedor.decide(_contexto(news_score=50.0 + MIN_MARGIN)) is not None
    assert provedor.decide(_contexto(news_score=50.0 + MIN_MARGIN - 0.1)) is None


def test_absent_data_is_not_an_opinion() -> None:
    """"Nao sei" nao pode virar 50: seria uma opiniao neutra que ninguem
    emitiu. A AIsa nao cobre forex, e esse caso e o mais comum aqui."""
    assert SentimentDirectionProvider().decide(_contexto(news_score=None)) is None


# --- a heuristica continua sendo o piso ------------------------------------


def test_the_trend_provider_answers_when_there_is_a_trend() -> None:
    provedor = TrendDirectionProvider()

    assert provedor.decide(_contexto(trend="DOWN")).direction == SignalDirection.SHORT
    assert provedor.decide(_contexto(trend="UP")).direction == SignalDirection.LONG


def test_sideways_gives_no_side() -> None:
    """Mercado lateral nao produz compra por omissao.

    A regra vale para a heuristica isolada e para a cadeia inteira: sem
    tendencia e sem sentimento, a resposta e "nao ha lado", nao "compra
    porque nao sobrou outra coisa". Um setup que ninguem defendeu e pior
    que nenhum setup, porque chega a tela com a mesma aparencia dos bons.
    """
    assert TrendDirectionProvider().decide(_contexto(trend="SIDEWAYS")) is None
    assert default_chain().decide(_contexto(trend="SIDEWAYS", news_score=None)) is None


def test_sentiment_still_gives_a_side_in_a_sideways_market() -> None:
    """Sem lado por omissao nao e o mesmo que sem lado nunca.

    Quando o noticiario se inclina de verdade, ha uma opiniao afirmada —
    e e justamente em mercado lateral que ela e a unica que existe.
    """
    decisao = default_chain().decide(_contexto(trend="SIDEWAYS", news_score=80.0))

    assert decisao.direction == SignalDirection.LONG
    assert decisao.source == SOURCE_SENTIMENT


# --- a cadeia --------------------------------------------------------------


def test_sentiment_wins_when_it_has_an_opinion() -> None:
    decisao = default_chain().decide(_contexto(trend="UP", news_score=15.0))

    assert decisao.direction == SignalDirection.SHORT
    assert decisao.source == SOURCE_SENTIMENT


def test_the_chain_falls_back_to_the_trend() -> None:
    decisao = default_chain().decide(_contexto(trend="DOWN", news_score=None))

    assert decisao.direction == SignalDirection.SHORT
    assert decisao.source == SOURCE_HEURISTIC


def test_a_silent_provider_does_not_block_the_next_one() -> None:
    """Provedor ausente, sem chave ou fora do ar se cala — nao derruba a
    cadeia nem consome o turno de quem vem depois."""

    class _Mudo:
        def decide(self, context):
            return None

    decisao = FallbackDirectionProvider(_Mudo(), TrendDirectionProvider()).decide(
        _contexto(trend="DOWN")
    )

    assert decisao.direction == SignalDirection.SHORT


def test_a_fully_silent_chain_returns_none() -> None:
    """Quando ninguem opina, a cadeia admite isso.

    Inventar um lado aqui seria pior que o `None`: o servico nao teria
    como distinguir "ha um setup" de "nao ha", e a tela mostraria uma
    zona de entrada apoiada em nada.
    """

    class _Mudo:
        def decide(self, context):
            return None

    assert FallbackDirectionProvider(_Mudo(), _Mudo()).decide(_contexto()) is None


def test_an_empty_chain_is_refused() -> None:
    """Falha na construcao, nao em producao."""
    import pytest

    with pytest.raises(ValueError):
        FallbackDirectionProvider()


# --- leitura do relatorio --------------------------------------------------


class _Fator:
    def __init__(self, name, raw_score, has_data):
        self.name = name
        self.raw_score = raw_score
        self.has_data = has_data


class _Relatorio:
    def __init__(self, *fatores):
        self.score = type("Score", (), {"factors": list(fatores)})()


def test_the_news_factor_is_read_from_the_report() -> None:
    """Zero chamada nova: o fator ja vem do `analyze_symbol`, atras do
    cache, do orcamento e do CoverageGuard. Consultar a AIsa de novo aqui
    repetiria o episodio que queimou a assinatura inteira."""
    relatorio = _Relatorio(
        _Fator("structure", 70.0, True), _Fator("news", 82.0, True)
    )

    assert news_score_from(relatorio) == 82.0


def test_a_factor_without_data_reads_as_none() -> None:
    relatorio = _Relatorio(_Fator("news", 50.0, False))

    assert news_score_from(relatorio) is None


def test_a_report_without_the_factor_reads_as_none() -> None:
    assert news_score_from(_Relatorio(_Fator("structure", 70.0, True))) is None
