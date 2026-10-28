"""Ponto, tick, contrato e lote — quatro coisas diferentes.

Este modulo existe porque confundi-las e o jeito mais barato de perder
dinheiro neste sistema, e porque a confusao nao da erro: ela produz um
numero plausivel.

## As quatro grandezas

- **point** — o menor incremento REPRESENTAVEL do preco, `10^-digits`.
  E uma casa decimal, nao uma unidade negociavel.
- **tick_size** — o menor incremento NEGOCIAVEL. No MNQ vale 0.25
  enquanto o point vale 0.01: sao 25x de diferenca, e ja houve um bug
  neste projeto exatamente por trocar um pelo outro.
- **contract_size** — quantas unidades do ativo cabem em 1.0 lote.
- **lote / contrato** — "1 lote" no MT5 e "1 contrato" na mesa raramente
  sao a mesma quantidade. Nunca somamos os dois.

## A unica ponte entre preco e dinheiro

    dinheiro = (variacao_de_preco / tick_size) * tick_value * lotes

`tick_value` ja vem na moeda da conta e ja embute `contract_size`. Dividir
tambem por `contract_size` seria contar duas vezes — erro comum e
silencioso, porque o resultado continua "parecendo" dinheiro.

Por isso `money_per_price_unit` e derivado de `tick_value / tick_size`, e
nao de `contract_size`: o segundo caminho exige saber a moeda de cotacao
e a taxa de cambio, que nem sempre temos.

## Decimal, nao float

Preco e dinheiro usam `Decimal` em todo o modulo. `0.1 + 0.2` em float nao
da `0.3`, e o erro aparece como um nivel de stop um tick fora do lugar —
que e exatamente o tipo de divergencia que este sistema existe para
evitar.
"""

from __future__ import annotations

import enum
from decimal import Decimal, InvalidOperation


class LevelUnit(enum.StrEnum):
    """Em que unidade o operador digitou um nivel."""

    PRICE = "PRICE"
    """Preco absoluto. Nao precisa de conversao."""

    TICKS = "TICKS"
    """Multiplos de `tick_size` a partir da entrada."""

    POINTS = "POINTS"
    """Multiplos de `point`. Distinto de TICKS de proposito: num ativo em
    que tick_size != point os dois dao niveis diferentes, e deixar o
    operador escolher e mais honesto que adivinhar qual ele quis dizer."""

    MONEY = "MONEY"
    """Valor financeiro na moeda da conta. Exige `tick_value` e o tamanho
    da posicao para virar preco."""


class ConversionError(ValueError):
    """Faltou dado para converter. Nunca vira um numero aproximado.

    Preferir a excecao a um palpite e deliberado: um nivel de stop
    inventado parece igualzinho a um correto na tela.
    """


def to_decimal(value: object, *, field: str = "valor") -> Decimal:
    """Converte para Decimal pela representacao em texto.

    `Decimal(0.1)` carrega o erro do float junto; `Decimal(str(0.1))` nao.
    """
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ConversionError(f"{field} nao e numerico: {value!r}") from exc


def money_per_price_unit(*, tick_size: Decimal, tick_value: Decimal) -> Decimal:
    """Dinheiro por 1.0 de variacao de preco, por 1.0 lote.

    E a grandeza que torna dois instrumentos comparaveis: duas posicoes
    com a mesma exposicao por unidade de movimento ganham e perdem o mesmo
    quando o preco anda igual — que e a definicao operacional de hedge
    para instrumentos lineares.
    """
    if tick_size <= 0:
        raise ConversionError("tick_size precisa ser maior que zero")
    if tick_value <= 0:
        raise ConversionError("tick_value precisa ser maior que zero")
    return tick_value / tick_size


def price_delta_to_money(
    *, delta: Decimal, tick_size: Decimal, tick_value: Decimal, volume: Decimal
) -> Decimal:
    """Converte uma variacao de PRECO em dinheiro na moeda da conta."""
    return delta * money_per_price_unit(tick_size=tick_size, tick_value=tick_value) * volume


def money_to_price_delta(
    *, money: Decimal, tick_size: Decimal, tick_value: Decimal, volume: Decimal
) -> Decimal:
    """O caminho inverso. Volume zero nao tem resposta — nenhuma distancia
    de preco produz lucro sem posicao."""
    if volume <= 0:
        raise ConversionError(
            "sem volume nao da para converter dinheiro em distancia de preco"
        )
    return money / (money_per_price_unit(tick_size=tick_size, tick_value=tick_value) * volume)


def level_to_price(
    *,
    value: Decimal,
    unit: LevelUnit,
    entry: Decimal,
    is_profit_side: bool,
    is_long: bool,
    tick_size: Decimal,
    point: Decimal,
    tick_value: Decimal | None = None,
    volume: Decimal | None = None,
) -> Decimal:
    """Um nivel digitado em qualquer unidade vira preco absoluto.

    `is_profit_side` decide o SENTIDO: alvo e stop ficam em lados opostos
    da entrada, e o lado depende tambem de estar comprado ou vendido. Sao
    quatro combinacoes, e errar uma coloca o stop onde deveria estar o
    alvo — com o numero certo, no lugar errado.
    """
    if unit is LevelUnit.PRICE:
        return value

    if unit is LevelUnit.TICKS:
        distancia = value * tick_size
    elif unit is LevelUnit.POINTS:
        distancia = value * point
    elif unit is LevelUnit.MONEY:
        if tick_value is None or volume is None:
            raise ConversionError(
                "nivel em dinheiro exige tick_value e volume para virar preco"
            )
        distancia = money_to_price_delta(
            money=value, tick_size=tick_size, tick_value=tick_value, volume=volume
        )
    else:  # pragma: no cover - StrEnum fechado
        raise ConversionError(f"unidade desconhecida: {unit}")

    distancia = abs(distancia)
    sobe = is_long if is_profit_side else not is_long
    return entry + distancia if sobe else entry - distancia


def round_volume(
    *, volume: Decimal, step: Decimal, minimum: Decimal, maximum: Decimal
) -> Decimal:
    """Arredonda o lote para algo que a corretora aceita.

    Arredonda para BAIXO. Um lote arredondado para cima cobre mais do que
    foi pedido, e "mais hedge do que o planejado" e uma posicao
    direcional que ninguem decidiu tomar.

    Abaixo do minimo devolve zero, e nao o minimo: entregar volume_min
    quando o calculo pediu metade dele seria dobrar a exposicao em
    silencio. Zero e visivel, e o chamador trata como "nao da para
    cobrir este tamanho aqui".
    """
    if step <= 0:
        raise ConversionError("volume_step precisa ser maior que zero")
    if volume <= 0:
        return Decimal("0")

    passos = int((volume / step).to_integral_value(rounding="ROUND_FLOOR"))
    arredondado = step * passos

    if arredondado < minimum:
        return Decimal("0")
    return min(arredondado, maximum)
