"""Janela de bloqueio por evento economico.

Os dois primeiros testes existem porque o portao anterior falhava neles: um
cobre a comparacao contra o horario AGENDADO (o codigo antigo exigia data de
publicacao no futuro e por isso nunca disparou) e o outro cobre o filtro por
moeda (o campo existia e era ignorado).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.calendar_feed.blackout import (
    BlackoutWindow,
    currencies_for_symbol,
    describe,
    find_blocking_event,
)
from app.calendar_feed.provider import CalendarEvent

AGORA = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
JANELA = BlackoutWindow(minutes_before=30, minutes_after=15)


def evento(
    *,
    minutos: float,
    currency: str = "USD",
    impact: str = "HIGH",
    title: str = "Payroll",
) -> CalendarEvent:
    return CalendarEvent(
        title=title,
        scheduled_at=AGORA + timedelta(minutes=minutos),
        currency=currency,
        impact=impact,  # type: ignore[arg-type]
    )


# --- os dois defeitos que motivaram o conserto ---------------------------


def test_an_upcoming_high_impact_event_blocks() -> None:
    """O caso que o portao antigo deixava passar: evento daqui a 10 minutos.

    O codigo anterior comparava contra `published_at` de uma noticia — sempre
    no passado — entao esta condicao nunca era verdadeira.
    """
    encontrado = find_blocking_event(
        [evento(minutos=10)], symbol="EURUSD", now=AGORA, window=JANELA
    )

    assert encontrado is not None
    assert encontrado.title == "Payroll"


def test_an_event_of_another_currency_does_not_block() -> None:
    """O segundo defeito: a moeda era ignorada, entao um dado do iene
    barraria uma entrada em EUR/USD."""
    assert (
        find_blocking_event(
            [evento(minutos=10, currency="JPY")],
            symbol="EURUSD",
            now=AGORA,
            window=JANELA,
        )
        is None
    )


# --- janela ---------------------------------------------------------------


def test_the_window_also_covers_the_minutes_after_the_release() -> None:
    """O perigo nao acaba no instante da divulgacao: logo depois o spread
    abre e o preco chicoteia."""
    assert (
        find_blocking_event(
            [evento(minutos=-10)], symbol="EURUSD", now=AGORA, window=JANELA
        )
        is not None
    )


def test_an_event_far_ahead_does_not_block() -> None:
    assert (
        find_blocking_event(
            [evento(minutos=120)], symbol="EURUSD", now=AGORA, window=JANELA
        )
        is None
    )


def test_an_event_long_past_does_not_block() -> None:
    assert (
        find_blocking_event(
            [evento(minutos=-120)], symbol="EURUSD", now=AGORA, window=JANELA
        )
        is None
    )


def test_the_window_edges_are_inclusive() -> None:
    assert find_blocking_event(
        [evento(minutos=30)], symbol="EURUSD", now=AGORA, window=JANELA
    )
    assert find_blocking_event(
        [evento(minutos=-15)], symbol="EURUSD", now=AGORA, window=JANELA
    )


# --- impacto --------------------------------------------------------------


def test_medium_impact_does_not_block_by_default() -> None:
    assert (
        find_blocking_event(
            [evento(minutos=5, impact="MEDIUM")],
            symbol="EURUSD",
            now=AGORA,
            window=JANELA,
        )
        is None
    )


def test_the_minimum_impact_is_configurable() -> None:
    assert find_blocking_event(
        [evento(minutos=5, impact="MEDIUM")],
        symbol="EURUSD",
        now=AGORA,
        window=JANELA,
        min_impact="MEDIUM",
    )


def test_the_nearest_event_is_the_one_reported() -> None:
    """Com dois eventos barrando, o painel precisa citar o mais proximo."""
    encontrado = find_blocking_event(
        [evento(minutos=20, title="Longe"), evento(minutos=5, title="Perto")],
        symbol="EURUSD",
        now=AGORA,
        window=JANELA,
    )
    assert encontrado is not None
    assert encontrado.title == "Perto"


def test_an_event_without_currency_is_treated_as_global() -> None:
    """Feriado bancario ou decisao de organismo internacional nao tem moeda
    declarada: melhor barrar do que ignorar."""
    assert find_blocking_event(
        [evento(minutos=5, currency="")], symbol="EURUSD", now=AGORA, window=JANELA
    )


# --- moedas do instrumento ------------------------------------------------


def test_a_major_pair_maps_to_both_currencies() -> None:
    assert currencies_for_symbol("EURUSD") == frozenset({"EUR", "USD"})


def test_gold_maps_only_to_the_quote_currency() -> None:
    """XAU nao tem banco central; o que move ouro e a economia americana."""
    assert currencies_for_symbol("XAUUSD") == frozenset({"USD"})


def test_us_tech_index_maps_only_to_usd_events() -> None:
    """USTEC é um índice, não um par; eventos americanos são os relevantes."""
    assert currencies_for_symbol("USTEC") == frozenset({"USD"})


def test_a_broker_suffix_is_tolerated() -> None:
    assert currencies_for_symbol("EURUSD.r") == frozenset({"EUR", "USD"})
    assert currencies_for_symbol("XAUUSD_i") == frozenset({"USD"})


def test_an_unknown_six_letter_symbol_falls_back_to_splitting() -> None:
    assert currencies_for_symbol("NOKSEK") == frozenset({"NOK", "SEK"})


def test_an_undecipherable_symbol_returns_empty_instead_of_guessing() -> None:
    assert currencies_for_symbol("US30") == frozenset()


def test_a_symbol_without_known_currencies_never_filters_events_out() -> None:
    """Sem saber filtrar, o portao continua barrando — deixar passar seria
    silenciar a protecao justamente onde ela e menos confiavel."""
    assert find_blocking_event(
        [evento(minutos=5, currency="USD")], symbol="US30", now=AGORA, window=JANELA
    )


# --- mensagem -------------------------------------------------------------


def test_the_reason_says_which_event_and_when() -> None:
    texto = describe(evento(minutos=12), now=AGORA)
    assert "Payroll" in texto
    assert "em 12 min" in texto
    assert "USD" in texto


def test_the_reason_uses_past_tense_after_the_release() -> None:
    assert "ha 8 min" in describe(evento(minutos=-8), now=AGORA)


def test_a_non_forex_six_letter_name_is_not_split_into_fake_currencies() -> None:
    """"COFFEE" nao e um par: dividir em "COF"/"FEE" faria o filtro
    descartar eventos legitimos com base em siglas inventadas.

    O exemplo era "USTECH" ate o indice USTEC entrar no catalogo: com
    cinco letras, ele passou a casar como prefixo de qualquer simbolo
    comecado por elas. O caso vale a pena ser conhecido e esta coberto
    logo abaixo; aqui o que se protege e o caminho de divisao em seis
    letras, que precisa de um nome que nao colida com o catalogo.
    """
    assert currencies_for_symbol("COFFEE") == frozenset()


def test_a_catalog_prefix_wins_over_the_six_letter_split() -> None:
    """Efeito colateral de USTEC ter cinco letras: "USTECH" e lido como o
    indice com sufixo de corretora, nao como nome desconhecido.

    E o mesmo mecanismo que faz "EURUSD.r" funcionar, e aqui ele acerta —
    USTECH de fato e o mesmo indice em outras corretoras. Fica registrado
    porque a leitura nao e obvia: o simbolo nao esta no catalogo, e mesmo
    assim resolve.
    """
    assert currencies_for_symbol("USTECH") == frozenset({"USD"})


def test_such_a_symbol_keeps_the_gate_conservative() -> None:
    assert find_blocking_event(
        [evento(minutos=5, currency="USD")], symbol="COFFEE", now=AGORA, window=JANELA
    )
