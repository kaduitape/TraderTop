"""Especificacao de contrato das duas pernas, e o mapeamento entre elas.

## Por que a mesa tem spec propria

A operacao de referencia na mesa quase nunca e o mesmo instrumento que a
cobertura na corretora. Mesa opera futuro cheio; corretora oferece CFD ou
mini. Tratar os dois com a mesma spec produziria uma cobertura errada por
um fator inteiro — e o erro so apareceria no extrato.

## Risco de base

Quando os simbolos diferem, nao ha hedge perfeito: os dois instrumentos
podem descolar. O sistema exige um mapeamento EXPLICITO (`price_ratio`) e
marca `basis_risk`. Assumir equivalencia por os nomes se parecerem e o
tipo de atalho que funciona ate o dia em que nao funciona.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.hedge.units import ConversionError, money_per_price_unit, to_decimal


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """O que precisamos saber de um instrumento para converter preco em
    dinheiro. Campos ausentes viram `None` e param o calculo — nunca
    viram um default plausivel."""

    symbol: str
    digits: int
    point: Decimal
    tick_size: Decimal
    tick_value: Decimal
    """Dinheiro por tick, por 1.0 lote, JA na moeda da conta."""

    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    currency: str
    """Moeda em que `tick_value` esta expresso — a moeda da conta."""

    @property
    def money_per_price_unit(self) -> Decimal:
        return money_per_price_unit(tick_size=self.tick_size, tick_value=self.tick_value)

    def exposure_per_lot(self) -> Decimal:
        """Dinheiro por unidade de movimento de preco, por lote.

        Sinonimo de `money_per_price_unit`, nomeado pelo papel que
        cumpre no hedge: e a grandeza que se iguala entre as pernas.
        """
        return self.money_per_price_unit

    def validate(self) -> list[str]:
        """O que falta para este contrato ser utilizavel.

        Devolve lista de problemas em vez de levantar: a tela precisa
        mostrar TODOS os campos faltando de uma vez, e nao obrigar o
        operador a descobrir um por requisicao.
        """
        faltas: list[str] = []
        if self.tick_size <= 0:
            faltas.append(f"{self.symbol}: tick_size ausente ou zero")
        if self.tick_value <= 0:
            faltas.append(f"{self.symbol}: tick_value ausente ou zero")
        if self.volume_step <= 0:
            faltas.append(f"{self.symbol}: volume_step ausente ou zero")
        if not self.currency:
            faltas.append(f"{self.symbol}: moeda da conta nao informada")
        return faltas


def spec_from_symbol_row(row, *, tick_value: Decimal | None, currency: str) -> ContractSpec:
    """Monta a spec a partir da linha de `symbols` sincronizada do MT5.

    `tick_value` NAO vem do banco: a tabela `symbols` nao o guarda, porque
    ele depende da moeda da conta e do cambio do momento. Quem o fornece e
    o EA conector, que le `SYMBOL_TRADE_TICK_VALUE` no terminal onde a
    conta esta logada. Sem ele, a spec nasce invalida e o calculo e
    marcado como indisponivel — que e melhor que estimar.
    """
    ponto = to_decimal(row.point, field="point")
    tick = to_decimal(row.trade_tick_size or row.point, field="trade_tick_size")
    return ContractSpec(
        symbol=row.name,
        digits=int(row.digits),
        point=ponto,
        tick_size=tick if tick > 0 else ponto,
        tick_value=tick_value if tick_value is not None else Decimal("0"),
        contract_size=to_decimal(row.trade_contract_size, field="trade_contract_size"),
        volume_min=to_decimal(row.volume_min, field="volume_min"),
        volume_max=to_decimal(row.volume_max, field="volume_max"),
        volume_step=to_decimal(row.volume_step, field="volume_step"),
        currency=currency,
    )


@dataclass(frozen=True, slots=True)
class InstrumentMapping:
    """Como o preco de um instrumento se traduz no outro.

    `price_ratio` e quantas unidades de preco da CORRETORA correspondem a
    1 unidade de preco da MESA. Para o mesmo indice cotado igual nos dois
    lugares, vale 1.
    """

    price_ratio: Decimal = Decimal("1")
    same_instrument: bool = True
    note: str = ""

    @property
    def basis_risk(self) -> bool:
        """Instrumentos diferentes nunca andam exatamente juntos.

        A tela precisa dizer isso: um hedge "perfeito" no papel entre
        futuro e CFD vira exposicao residual real quando os dois
        descolam, e quem montou a operacao merece saber antes."""
        return not self.same_instrument

    def validate(self) -> list[str]:
        if self.price_ratio <= 0:
            return ["mapeamento: price_ratio precisa ser maior que zero"]
        return []


def mapping_for(desk_symbol: str, broker_symbol: str, *, price_ratio: Decimal | None) -> InstrumentMapping:
    """Decide o mapeamento, exigindo o explicito quando os nomes diferem.

    Nomes iguais (ignorando sufixo de corretora) assumem 1:1. Nomes
    diferentes SEM `price_ratio` informado sao um erro de configuracao,
    nao um caso para o default — e o `price_ratio=0` resultante faz a
    validacao reprovar, que e o comportamento desejado.
    """
    desk = desk_symbol.strip().upper()
    broker = broker_symbol.strip().upper()
    mesmo = bool(desk) and (desk == broker or broker.startswith(desk) or desk.startswith(broker))

    if mesmo:
        return InstrumentMapping(
            price_ratio=price_ratio if price_ratio and price_ratio > 0 else Decimal("1"),
            same_instrument=True,
        )

    if price_ratio is None or price_ratio <= 0:
        raise ConversionError(
            f"'{desk}' e '{broker}' sao instrumentos diferentes: informe a "
            "razao de preco entre eles. O sistema nao assume equivalencia "
            "entre futuro e CFD."
        )

    return InstrumentMapping(
        price_ratio=price_ratio,
        same_instrument=False,
        note=f"{broker} cobre {desk} a {price_ratio} de preco por unidade",
    )
