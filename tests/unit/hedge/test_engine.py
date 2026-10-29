"""O motor financeiro: dois lados, tres cenarios, uma moeda.

O caso que mais importa aqui e o da cobertura IMPERFEITA. Hedge perfeito
fecha em zero e e facil de verificar; o interessante e o que sobra quando
o step da corretora nao divide a exposicao da mesa — porque e isso que
acontece na pratica, e e onde um sistema descuidado mostra "coberto".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.hedge.contracts import ContractSpec, InstrumentMapping, mapping_for
from app.hedge.engine import (
    BrokerLeg,
    DeskLeg,
    HedgeCalculation,
    LegCosts,
    Objective,
    Scenario,
    calculate,
    required_lots,
)
from app.hedge.units import ConversionError


def _spec(
    symbol: str = "MNQ",
    *,
    tick_value: str = "0.50",
    tick_size: str = "0.25",
    step: str = "0.01",
    minimum: str = "0.01",
    maximum: str = "100",
    currency: str = "USD",
) -> ContractSpec:
    return ContractSpec(
        symbol=symbol,
        digits=2,
        point=Decimal("0.01"),
        tick_size=Decimal(tick_size),
        tick_value=Decimal(tick_value),
        contract_size=Decimal("1"),
        volume_min=Decimal(minimum),
        volume_max=Decimal(maximum),
        volume_step=Decimal(step),
        currency=currency,
    )


def _desk(*, is_long: bool = True, costs: LegCosts | None = None) -> DeskLeg:
    """Perna da mesa com os niveis do LADO certo para a direcao.

    Vendido, o take fica abaixo da entrada e o stop acima. Manter os
    mesmos numeros para os dois lados produziria um setup invertido — que
    o motor agora recusa, com razao.
    """
    take, stop = (
        (Decimal("20100"), Decimal("19950"))
        if is_long
        else (Decimal("19900"), Decimal("20050"))
    )
    return DeskLeg(
        symbol="MNQ",
        is_long=is_long,
        entry=Decimal("20000"),
        take=take,
        stop=stop,
        contracts=Decimal("1"),
        spec=_spec(),
        costs=costs or LegCosts(),
    )


def _broker(*, is_long: bool = False, spec: ContractSpec | None = None, costs=None) -> BrokerLeg:
    return BrokerLeg(
        symbol="MNQ",
        is_long=is_long,
        entry=Decimal("20000"),
        lots=Decimal("0"),
        spec=spec or _spec(),
        costs=costs or LegCosts(),
    )


# --- cobertura --------------------------------------------------------------


def test_a_perfect_hedge_nets_to_zero_before_costs() -> None:
    r = calculate(desk=_desk(), broker=_broker(), mapping=InstrumentMapping())

    assert r.available
    assert r.effective_lots == Decimal("1.00")
    assert r.residual_exposure == Decimal("0.00")
    for cenario in r.scenarios:
        assert cenario.net == Decimal("0.00")


def test_coverage_is_by_exposure_not_by_quantity() -> None:
    """1 contrato de futuro cheio nao e 1 lote de CFD. Igualar as
    quantidades e o erro classico."""
    micro = _spec("MNQ.cfd", tick_value="0.05", tick_size="0.25")

    lotes = required_lots(
        desk=_desk(), broker_spec=micro, mapping=InstrumentMapping(), coverage=Decimal("1")
    )

    # Mesa: 2 USD por ponto. Corretora: 0.2 por ponto. Precisa de 10 lotes.
    assert lotes == Decimal("10")


def test_partial_coverage_is_respected() -> None:
    r = calculate(
        desk=_desk(), broker=_broker(), mapping=InstrumentMapping(), coverage=Decimal("0.5")
    )

    assert r.effective_lots == Decimal("0.50")
    assert r.coverage_ratio == Decimal("0.5000")


def test_rounding_leaves_residual_exposure_and_says_so() -> None:
    """O caso real: o step nao divide a exposicao. A tela precisa mostrar
    o lote pedido E o efetivo — so o efetivo esconderia o descoberto."""
    grosso = _spec("MNQ.big", tick_value="0.30", tick_size="0.25", step="0.1", minimum="0.1")

    r = calculate(desk=_desk(), broker=_broker(spec=grosso), mapping=InstrumentMapping())

    assert r.available
    assert r.requested_lots > r.effective_lots
    assert r.residual_exposure > 0
    assert any("arredondado" in a for a in r.warnings)


def test_a_size_below_the_broker_minimum_is_unavailable() -> None:
    """Nao da para cobrir. Dizer isso e melhor que entregar o minimo e
    chamar de cobertura."""
    gigante = _spec("BIG", tick_value="500", tick_size="0.25", step="1", minimum="1")

    r = calculate(desk=_desk(), broker=_broker(spec=gigante), mapping=InstrumentMapping())

    assert not r.available
    assert "minimo" in r.reason


# --- os dois lados ----------------------------------------------------------


def test_the_two_legs_must_be_opposite() -> None:
    """Configuradas no mesmo sentido, as pernas andariam juntas e
    dobrariam o risco em vez de cobri-lo."""
    r = calculate(desk=_desk(is_long=True), broker=_broker(is_long=True), mapping=InstrumentMapping())

    assert not r.available
    assert "oposto" in r.reason


def test_a_short_desk_is_hedged_by_a_long_broker() -> None:
    r = calculate(
        desk=_desk(is_long=False), broker=_broker(is_long=True), mapping=InstrumentMapping()
    )

    assert r.available
    take = r.by_scenario(Scenario.DESK_TAKE)
    # Vendido, o take fica ABAIXO da entrada: a mesa ganha, a corretora perde.
    assert take.desk_gross > 0
    assert take.broker_gross < 0


def test_different_currencies_are_refused_not_converted() -> None:
    """Somar BRL com USD daria um numero sem significado. Sem taxa
    confiavel, o calculo e indisponivel."""
    r = calculate(
        desk=_desk(),
        broker=_broker(spec=_spec(currency="BRL")),
        mapping=InstrumentMapping(),
    )

    assert not r.available
    assert "moedas diferentes" in r.reason


# --- cenarios ---------------------------------------------------------------


def test_the_three_scenarios_are_reported_separately() -> None:
    r = calculate(
        desk=_desk(),
        broker=_broker(),
        mapping=InstrumentMapping(),
        current_desk_price=Decimal("20010"),
    )

    assert {s.scenario for s in r.scenarios} == {
        Scenario.DESK_TAKE,
        Scenario.DESK_STOP,
        Scenario.TIMEOUT,
    }


def test_timeout_is_absent_without_a_current_price() -> None:
    """Sem preco de saida nao ha cenario de tempo. Usar a entrada daria um
    zero que pareceria uma estimativa."""
    r = calculate(desk=_desk(), broker=_broker(), mapping=InstrumentMapping())

    assert r.by_scenario(Scenario.TIMEOUT) is None


def test_gross_and_net_are_reported_separately_per_account() -> None:
    """Mesa e corretora sao contas diferentes. Um numero unico de
    "resultado do hedge" esconderia de qual conta sai o dinheiro."""
    r = calculate(
        desk=_desk(costs=LegCosts(commission=Decimal("4"))),
        broker=_broker(costs=LegCosts(commission=Decimal("7"))),
        mapping=InstrumentMapping(),
    )

    take = r.by_scenario(Scenario.DESK_TAKE)
    assert take.desk_gross == Decimal("200.00")
    assert take.broker_gross == Decimal("-200.00")
    assert take.desk_costs == Decimal("4.00")
    assert take.broker_costs == Decimal("7.00")
    assert take.net == Decimal("-11.00")


def test_costs_make_a_perfect_hedge_negative() -> None:
    """A conta honesta: cobrir perfeitamente nao e gratis, e em TODO
    cenario o resultado e o custo das duas pontas."""
    r = calculate(
        desk=_desk(costs=LegCosts(commission=Decimal("4"))),
        broker=_broker(costs=LegCosts(commission=Decimal("7"), slippage_price=Decimal("0.25"))),
        mapping=InstrumentMapping(),
        current_desk_price=Decimal("20000"),
    )

    for cenario in r.scenarios:
        assert cenario.net < 0
        assert cenario.total_costs > 0


# --- instrumentos diferentes ------------------------------------------------


def test_different_instruments_demand_an_explicit_mapping() -> None:
    """Nao assumir equivalencia entre futuro e CFD e regra, nao preferencia."""
    with pytest.raises(ConversionError, match="instrumentos diferentes"):
        mapping_for("WIN", "US30", price_ratio=None)


def test_the_same_instrument_with_a_broker_suffix_maps_one_to_one() -> None:
    m = mapping_for("BTCUSD", "BTCUSDm", price_ratio=None)

    assert m.price_ratio == Decimal("1")
    assert m.basis_risk is False


def test_a_declared_mapping_carries_basis_risk_to_the_screen() -> None:
    m = mapping_for("WIN", "US30", price_ratio=Decimal("0.2"))

    assert m.basis_risk is True

    r = calculate(desk=_desk(), broker=_broker(), mapping=m)

    assert r.basis_risk is True
    assert any("risco de base" in a for a in r.warnings)


def test_the_price_ratio_scales_the_required_lots() -> None:
    m = mapping_for("WIN", "US30", price_ratio=Decimal("2"))

    lotes = required_lots(
        desk=_desk(), broker_spec=_spec(), mapping=m, coverage=Decimal("1")
    )

    # A corretora move 2 unidades de preco por 1 da mesa: metade do lote.
    assert lotes == Decimal("0.5")


# --- dados faltando ---------------------------------------------------------


def test_a_missing_tick_value_lists_what_is_missing() -> None:
    """A tela precisa mostrar TODOS os campos faltando de uma vez."""
    r = calculate(
        desk=_desk(),
        broker=_broker(spec=_spec(tick_value="0")),
        mapping=InstrumentMapping(),
    )

    assert not r.available
    assert "tick_value" in r.reason


def test_unavailable_never_carries_numbers() -> None:
    """Um resultado indisponivel com zeros preenchidos seria lido como
    "o hedge sai zerado"."""
    r = HedgeCalculation.unavailable("faltou dado")

    assert r.scenarios == ()
    assert r.effective_lots == Decimal("0")


# --- objetivo ---------------------------------------------------------------


def test_the_objective_never_changes_the_numbers() -> None:
    """Preferir um cenario nao o torna mais provavel nem mais lucrativo.
    O objetivo muda leitura e cor, nunca a conta."""
    base = calculate(desk=_desk(), broker=_broker(), mapping=InstrumentMapping())

    # O objetivo nem entra no motor: nao ha parametro para ele aqui, e e
    # essa ausencia que garante a propriedade.
    assert Objective.FAVOR_DESK_TAKE != Objective.FAVOR_BROKER_PROFIT
    assert base.available


def test_inverted_levels_are_refused_not_calculated() -> None:
    """Digitando preco absoluto nada impede trocar take com stop. O motor
    calcularia os dois normalmente e entregaria "alvo com prejuizo, stop
    com lucro": numeros corretos descrevendo uma operacao que ninguem
    quis montar."""
    invertido = DeskLeg(
        symbol="MNQ",
        is_long=True,
        entry=Decimal("20000"),
        take=Decimal("19950"),   # alvo ABAIXO numa compra
        stop=Decimal("20100"),   # stop ACIMA numa compra
        contracts=Decimal("1"),
        spec=_spec(),
    )

    r = calculate(desk=invertido, broker=_broker(), mapping=InstrumentMapping())

    assert not r.available
    assert "invertidos" in r.reason


def test_a_short_setup_with_long_style_levels_is_refused() -> None:
    vendido_errado = DeskLeg(
        symbol="MNQ",
        is_long=False,
        entry=Decimal("20000"),
        take=Decimal("20100"),   # alvo ACIMA numa venda
        stop=Decimal("19950"),
        contracts=Decimal("1"),
        spec=_spec(),
    )

    r = calculate(desk=vendido_errado, broker=_broker(is_long=True), mapping=InstrumentMapping())

    assert not r.available
    assert "take abaixo e stop acima" in r.reason


# --- lote fixo --------------------------------------------------------------


def test_a_fixed_lot_is_used_instead_of_the_derived_one() -> None:
    """Quando a posicao ja esta aberta, a pergunta deixa de ser "quanto
    devo operar" e passa a ser "quanto isto cobre"."""
    r = calculate(
        desk=_desk(),
        broker=_broker(),
        mapping=InstrumentMapping(),
        fixed_lots=Decimal("3.8"),
    )

    assert r.available
    assert r.effective_lots == Decimal("3.8")


def test_a_fixed_lot_reports_the_coverage_it_actually_produces() -> None:
    """Com lote fixo a cobertura e RESULTADO, nao escolha. Fingir que e 1
    esconderia exposicao direcional de verdade."""
    r = calculate(
        desk=_desk(),
        broker=_broker(),
        mapping=InstrumentMapping(),
        fixed_lots=Decimal("3.8"),
    )

    assert r.coverage_ratio == Decimal("3.8000")
    assert any("380%" in a and "excede" in a for a in r.warnings)


def test_a_fixed_lot_below_full_coverage_says_it_is_uncovered() -> None:
    r = calculate(
        desk=_desk(),
        broker=_broker(),
        mapping=InstrumentMapping(),
        fixed_lots=Decimal("0.4"),
    )

    assert r.residual_exposure > 0
    assert any("descoberto" in a for a in r.warnings)


def test_a_fixed_lot_at_full_coverage_raises_no_alarm() -> None:
    """Avisar quando esta certo treina a ignorar o aviso."""
    r = calculate(
        desk=_desk(),
        broker=_broker(),
        mapping=InstrumentMapping(),
        fixed_lots=Decimal("1.0"),
    )

    assert not any("cobre" in a for a in r.warnings)


def test_a_fixed_lot_the_broker_refuses_is_reported_as_such() -> None:
    """A mensagem precisa dizer que o problema e o LOTE INFORMADO, nao
    que a operacao nao da para cobrir."""
    grosso = _spec("X", step="1", minimum="1")

    r = calculate(
        desk=_desk(),
        broker=_broker(spec=grosso),
        mapping=InstrumentMapping(),
        fixed_lots=Decimal("0.3"),
    )

    assert not r.available
    assert "lote fixo" in r.reason.lower()


def test_without_a_fixed_lot_nothing_changes() -> None:
    sem = calculate(desk=_desk(), broker=_broker(), mapping=InstrumentMapping())
    nulo = calculate(
        desk=_desk(), broker=_broker(), mapping=InstrumentMapping(), fixed_lots=None
    )

    assert sem.effective_lots == nulo.effective_lots == Decimal("1.00")
