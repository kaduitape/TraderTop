"""Ponto, tick, contrato e lote — os quatro que nao podem se misturar.

Estes testes nao protegem contra excecao: protegem contra o numero
PLAUSIVEL. Trocar tick por point num ativo em que eles diferem 25x produz
um stop que parece certo e esta 25 vezes fora de lugar, sem erro nenhum.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.hedge.units import (
    ConversionError,
    LevelUnit,
    level_to_price,
    money_per_price_unit,
    money_to_price_delta,
    price_delta_to_money,
    round_volume,
    to_decimal,
)

# MNQ: point 0.01, tick 0.25, tick_value 0.50 -> 2 dolares por ponto.
MNQ = {"tick_size": Decimal("0.25"), "tick_value": Decimal("0.50")}


def test_point_and_tick_are_not_interchangeable() -> None:
    """O bug de 25x que este projeto ja teve no indicador."""
    preco_por_tick = level_to_price(
        value=Decimal("20"),
        unit=LevelUnit.TICKS,
        entry=Decimal("20000"),
        is_profit_side=True,
        is_long=True,
        tick_size=Decimal("0.25"),
        point=Decimal("0.01"),
    )
    preco_por_ponto = level_to_price(
        value=Decimal("20"),
        unit=LevelUnit.POINTS,
        entry=Decimal("20000"),
        is_profit_side=True,
        is_long=True,
        tick_size=Decimal("0.25"),
        point=Decimal("0.01"),
    )

    assert preco_por_tick == Decimal("20005.00")
    assert preco_por_ponto == Decimal("20000.20")
    assert preco_por_tick != preco_por_ponto


def test_money_per_price_unit_does_not_double_count_contract_size() -> None:
    """`tick_value` ja embute o contract_size. Dividir por ele de novo
    daria um numero que continua "parecendo" dinheiro."""
    assert money_per_price_unit(**MNQ) == Decimal("2")


def test_price_and_money_round_trip() -> None:
    dinheiro = price_delta_to_money(
        delta=Decimal("10"), volume=Decimal("3"), **MNQ
    )
    assert dinheiro == Decimal("60")

    volta = money_to_price_delta(money=dinheiro, volume=Decimal("3"), **MNQ)
    assert volta == Decimal("10")


def test_money_with_no_position_has_no_price_answer() -> None:
    """Nenhuma distancia de preco produz lucro sem posicao. Devolver zero
    aqui daria um nivel igual a entrada, que pareceria configurado."""
    with pytest.raises(ConversionError):
        money_to_price_delta(money=Decimal("100"), volume=Decimal("0"), **MNQ)


# --- as quatro combinacoes de lado -----------------------------------------


@pytest.mark.parametrize(
    ("is_long", "is_profit_side", "esperado"),
    [
        (True, True, Decimal("20005.00")),    # comprado, alvo: acima
        (True, False, Decimal("19995.00")),   # comprado, stop: abaixo
        (False, True, Decimal("19995.00")),   # vendido, alvo: abaixo
        (False, False, Decimal("20005.00")),  # vendido, stop: acima
    ],
    ids=["compra/alvo", "compra/stop", "venda/alvo", "venda/stop"],
)
def test_the_side_decides_the_direction(is_long, is_profit_side, esperado) -> None:
    """Quatro combinacoes. Errar uma poe o stop onde deveria estar o alvo
    — com o numero certo, no lugar errado."""
    assert (
        level_to_price(
            value=Decimal("20"),
            unit=LevelUnit.TICKS,
            entry=Decimal("20000"),
            is_profit_side=is_profit_side,
            is_long=is_long,
            tick_size=Decimal("0.25"),
            point=Decimal("0.01"),
        )
        == esperado
    )


def test_a_negative_distance_still_lands_on_the_right_side() -> None:
    """O operador digita "-20 ticks" querendo dizer distancia. O sinal do
    que ele digitou nao pode mover o nivel para o lado errado."""
    assert level_to_price(
        value=Decimal("-20"),
        unit=LevelUnit.TICKS,
        entry=Decimal("20000"),
        is_profit_side=False,
        is_long=True,
        tick_size=Decimal("0.25"),
        point=Decimal("0.01"),
    ) == Decimal("19995.00")


def test_a_money_level_needs_the_position_size() -> None:
    """"Stop de 100 dolares" nao e um preco ate sabermos o tamanho."""
    with pytest.raises(ConversionError):
        level_to_price(
            value=Decimal("100"),
            unit=LevelUnit.MONEY,
            entry=Decimal("20000"),
            is_profit_side=False,
            is_long=True,
            tick_size=Decimal("0.25"),
            point=Decimal("0.01"),
        )


def test_a_money_level_converts_with_the_position_size() -> None:
    preco = level_to_price(
        value=Decimal("100"),
        unit=LevelUnit.MONEY,
        entry=Decimal("20000"),
        is_profit_side=False,
        is_long=True,
        tick_size=Decimal("0.25"),
        point=Decimal("0.01"),
        tick_value=Decimal("0.50"),
        volume=Decimal("1"),
    )
    # 100 dolares / (2 por ponto * 1 lote) = 50 pontos abaixo.
    assert preco == Decimal("19950")


def test_a_price_level_is_untouched() -> None:
    assert level_to_price(
        value=Decimal("20123.45"),
        unit=LevelUnit.PRICE,
        entry=Decimal("20000"),
        is_profit_side=True,
        is_long=True,
        tick_size=Decimal("0.25"),
        point=Decimal("0.01"),
    ) == Decimal("20123.45")


# --- lotes ------------------------------------------------------------------


def test_volume_rounds_down_never_up() -> None:
    """Arredondar para cima cobriria mais do que foi pedido — uma posicao
    direcional que ninguem decidiu tomar."""
    assert round_volume(
        volume=Decimal("1.37"),
        step=Decimal("0.1"),
        minimum=Decimal("0.1"),
        maximum=Decimal("100"),
    ) == Decimal("1.3")


def test_below_the_minimum_is_zero_not_the_minimum() -> None:
    """Entregar volume_min quando o calculo pediu metade dele dobraria a
    exposicao em silencio. Zero e visivel."""
    assert round_volume(
        volume=Decimal("0.004"),
        step=Decimal("0.01"),
        minimum=Decimal("0.01"),
        maximum=Decimal("100"),
    ) == Decimal("0")


def test_the_maximum_caps_the_volume() -> None:
    assert round_volume(
        volume=Decimal("500"),
        step=Decimal("0.01"),
        minimum=Decimal("0.01"),
        maximum=Decimal("100"),
    ) == Decimal("100")


def test_an_exact_multiple_survives_rounding() -> None:
    assert round_volume(
        volume=Decimal("2.00"),
        step=Decimal("0.01"),
        minimum=Decimal("0.01"),
        maximum=Decimal("100"),
    ) == Decimal("2.00")


def test_a_zero_step_is_refused() -> None:
    with pytest.raises(ConversionError):
        round_volume(
            volume=Decimal("1"),
            step=Decimal("0"),
            minimum=Decimal("0.01"),
            maximum=Decimal("100"),
        )


# --- Decimal ----------------------------------------------------------------


def test_decimal_comes_from_the_text_not_the_float() -> None:
    """`Decimal(0.1)` carrega o erro do binario; `Decimal("0.1")` nao. O
    sintoma seria um nivel um tick fora de lugar."""
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")


def test_a_non_numeric_value_is_refused_with_the_field_name() -> None:
    with pytest.raises(ConversionError, match="tick_value"):
        to_decimal("abc", field="tick_value")
