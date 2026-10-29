"""Motor financeiro do Hedge AI: quanto sai de cada lado, em cada cenario.

## O que este modulo NAO faz

Nao prevê mercado. Ele responde "SE o preco chegar aqui, quanto da?" —
uma conta, nao uma opiniao. A probabilidade de chegar vive em
`app/hedge/forecast/` e e mantida separada de proposito: misturar as duas
produziria um numero que parece esperanca matematica e nao e, porque a
probabilidade deste sistema ainda nao foi validada.

## As duas pernas somam, nao se cancelam no papel

Mesa e corretora sao contas diferentes, com moedas, custos e regras
diferentes. O resultado liquido combinado e a soma dos dois brutos menos
os dois custos — nunca um "resultado do hedge" calculado de uma vez. Ver
`CombinedResult`.

## Cobertura e por exposicao, nao por quantidade

Dois instrumentos lineares estao cobertos quando movem o mesmo dinheiro
para o mesmo movimento de preco. Igualar numero de contratos a numero de
lotes e o erro classico: 1 contrato de futuro cheio nao e 1 lote de CFD.

## Indisponivel e um resultado

Faltando tick_value, moeda ou mapeamento, o motor devolve
`HedgeCalculation(available=False, reason=...)`. Nao estima, nao
aproxima, nao preenche com zero. Um numero inventado na tela de uma
operacao com dinheiro real e pior que um espaco em branco.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.hedge.contracts import ContractSpec, InstrumentMapping
from app.hedge.units import ConversionError, round_volume

CENTS = Decimal("0.01")


class Objective(enum.StrEnum):
    """Qual desfecho o operador PREFERE.

    Muda a leitura e as cores da tela. Nunca muda a previsao nem o
    resultado financeiro: preferir um cenario nao o torna mais provavel,
    e um sistema que deixasse a preferencia mexer no numero estaria
    mentindo para quem o configurou.
    """

    FAVOR_DESK_TAKE = "FAVOR_DESK_TAKE"
    """Quer o alvo da mesa. A cobertura e protecao contra o stop."""

    FAVOR_BROKER_PROFIT = "FAVOR_BROKER_PROFIT"
    """Quer o stop da mesa com lucro na corretora."""


class Mode(enum.StrEnum):
    SIMULATION = "SIMULATION"
    TRACKING = "TRACKING"
    """Operacao ja aberta. Entrada e niveis ficam congelados; mudar
    qualquer um gera nova versao auditavel em vez de reescrever o
    historico."""


class Scenario(enum.StrEnum):
    DESK_TAKE = "DESK_TAKE"
    DESK_STOP = "DESK_STOP"
    TIMEOUT = "TIMEOUT"
    """Prazo esgotado sem tocar barreira: fecha nos dois lados ao preco
    corrente. E um desfecho de verdade, nao um resto."""


@dataclass(frozen=True, slots=True)
class LegCosts:
    """Custos de UMA conta. Separados porque mesa e corretora cobram
    coisas diferentes e por regras diferentes."""

    commission: Decimal = Decimal("0")
    """Por lote/contrato, ida e volta ja somadas."""

    slippage_price: Decimal = Decimal("0")
    """Derrapagem esperada, em unidades de PRECO."""

    other: Decimal = Decimal("0")
    """Taxas fixas da operacao (registro, custodia rateada)."""

    def total(self, *, volume: Decimal, money_per_price_unit: Decimal) -> Decimal:
        derrapagem = self.slippage_price * money_per_price_unit * volume
        return (self.commission * volume) + abs(derrapagem) + self.other


@dataclass(frozen=True, slots=True)
class DeskLeg:
    """A operacao de referencia na mesa."""

    symbol: str
    is_long: bool
    entry: Decimal
    take: Decimal
    stop: Decimal
    contracts: Decimal
    spec: ContractSpec
    costs: LegCosts = field(default_factory=LegCosts)

    def gross_at(self, price: Decimal) -> Decimal:
        sentido = Decimal("1") if self.is_long else Decimal("-1")
        return (price - self.entry) * sentido * self.spec.money_per_price_unit * self.contracts

    def exposure(self) -> Decimal:
        return self.spec.exposure_per_lot() * self.contracts


@dataclass(frozen=True, slots=True)
class BrokerLeg:
    """A cobertura na corretora. Sempre no sentido oposto ao da mesa."""

    symbol: str
    is_long: bool
    entry: Decimal
    lots: Decimal
    spec: ContractSpec
    costs: LegCosts = field(default_factory=LegCosts)

    def gross_at(self, price: Decimal) -> Decimal:
        sentido = Decimal("1") if self.is_long else Decimal("-1")
        return (price - self.entry) * sentido * self.spec.money_per_price_unit * self.lots

    def exposure(self) -> Decimal:
        return self.spec.exposure_per_lot() * self.lots


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario: Scenario
    desk_price: Decimal
    broker_price: Decimal
    desk_gross: Decimal
    broker_gross: Decimal
    desk_costs: Decimal
    broker_costs: Decimal

    @property
    def net(self) -> Decimal:
        """Liquido combinado. Os dois brutos menos os dois custos — nunca
        um numero unico de "resultado do hedge", que esconderia de que
        conta sai o dinheiro."""
        return self.desk_gross + self.broker_gross - self.desk_costs - self.broker_costs

    @property
    def total_costs(self) -> Decimal:
        return self.desk_costs + self.broker_costs

    def rounded(self) -> ScenarioResult:
        q = lambda v: v.quantize(CENTS, rounding=ROUND_HALF_UP)  # noqa: E731
        return ScenarioResult(
            scenario=self.scenario,
            desk_price=self.desk_price,
            broker_price=self.broker_price,
            desk_gross=q(self.desk_gross),
            broker_gross=q(self.broker_gross),
            desk_costs=q(self.desk_costs),
            broker_costs=q(self.broker_costs),
        )


@dataclass(frozen=True, slots=True)
class HedgeCalculation:
    available: bool
    currency: str = ""
    reason: str = ""
    """Por que nao da para calcular. Preenchido so quando available e
    False, e sempre com algo que o operador possa corrigir."""

    requested_lots: Decimal = Decimal("0")
    effective_lots: Decimal = Decimal("0")
    """Depois de arredondar ao step da corretora. A diferenca entre os
    dois e o que gera exposicao residual, e por isso os dois aparecem."""

    desk_exposure: Decimal = Decimal("0")
    broker_exposure: Decimal = Decimal("0")
    residual_exposure: Decimal = Decimal("0")
    """Dinheiro por unidade de movimento que sobra descoberto. Zero so
    acontece quando o step da corretora divide exatamente a exposicao da
    mesa — na pratica, quase nunca."""

    coverage_ratio: Decimal = Decimal("0")
    basis_risk: bool = False
    mapping_note: str = ""
    scenarios: tuple[ScenarioResult, ...] = ()
    warnings: tuple[str, ...] = ()

    def by_scenario(self, scenario: Scenario) -> ScenarioResult | None:
        return next((s for s in self.scenarios if s.scenario is scenario), None)

    @classmethod
    def unavailable(cls, reason: str, *, warnings: tuple[str, ...] = ()) -> HedgeCalculation:
        return cls(available=False, reason=reason, warnings=warnings)


def required_lots(
    *, desk: DeskLeg, broker_spec: ContractSpec, mapping: InstrumentMapping, coverage: Decimal
) -> Decimal:
    """Quantos lotes igualam a exposicao da mesa, antes de arredondar.

    A conta e exposicao contra exposicao, nunca contrato contra lote. O
    `price_ratio` entra porque uma unidade de movimento na mesa pode
    valer outra quantidade de unidades na corretora.
    """
    exposicao_corretora_por_lote = broker_spec.exposure_per_lot() * mapping.price_ratio
    if exposicao_corretora_por_lote <= 0:
        raise ConversionError("exposicao por lote da corretora e zero")
    return (desk.exposure() * coverage) / exposicao_corretora_por_lote


def _broker_price_for(desk_price: Decimal, *, desk: DeskLeg, broker: BrokerLeg, mapping: InstrumentMapping) -> Decimal:
    """Onde a corretora estara quando a mesa chegar em `desk_price`.

    Sob o mapeamento declarado, o movimento se traduz proporcionalmente.
    Isso e uma HIPOTESE, nao um fato — e exatamente a hipotese que o
    `basis_risk` avisa que pode falhar.
    """
    return broker.entry + (desk_price - desk.entry) * mapping.price_ratio


def calculate(
    *,
    desk: DeskLeg,
    broker: BrokerLeg,
    mapping: InstrumentMapping,
    coverage: Decimal = Decimal("1"),
    current_desk_price: Decimal | None = None,
    fixed_lots: Decimal | None = None,
) -> HedgeCalculation:
    """Os tres cenarios, com os dois lados separados.

    `fixed_lots` fixa o lote da corretora em vez de deriva-lo da
    exposicao. Existe porque o operador frequentemente JA tem a posicao
    aberta com um tamanho que a mesa ou a corretora impos — e nesse caso
    a pergunta deixa de ser "quanto devo operar" e passa a ser "quanto
    isto cobre de fato".

    Com ele, `coverage_ratio` vira RESULTADO e nao entrada: o motor
    informa a cobertura que aquele lote produz, que raramente e 1. Fingir
    que e seria o erro — a parte descoberta e exposicao direcional real.

    `current_desk_price` alimenta o cenario de encerramento por tempo.
    Sem ele, o cenario de timeout nao e calculado — nao ha preco de
    saida, e inventar um (a entrada, por exemplo) daria um "zero" que
    pareceria uma estimativa.
    """
    problemas = [*desk.spec.validate(), *broker.spec.validate(), *mapping.validate()]
    if problemas:
        return HedgeCalculation.unavailable(
            "Dados insuficientes para calcular: " + "; ".join(problemas)
        )

    if desk.spec.currency != broker.spec.currency:
        return HedgeCalculation.unavailable(
            f"As duas pernas estao em moedas diferentes ({desk.spec.currency} e "
            f"{broker.spec.currency}) e nao ha conversao confiavel configurada."
        )

    if desk.is_long == broker.is_long:
        return HedgeCalculation.unavailable(
            "A cobertura precisa ser no sentido oposto ao da mesa. "
            "Do jeito configurado as duas pernas andariam juntas, "
            "dobrando o risco em vez de cobri-lo."
        )

    # Niveis invertidos. Quando o operador digita o nivel como DISTANCIA,
    # `level_to_price` ja resolve o lado; quando digita preco absoluto, nada
    # impede trocar take com stop. O motor calcularia os dois normalmente e
    # entregaria "alvo com prejuizo, stop com lucro" — numeros corretos
    # descrevendo uma operacao que ninguem quis montar.
    lado_ok = (
        (desk.take > desk.entry > desk.stop)
        if desk.is_long
        else (desk.take < desk.entry < desk.stop)
    )
    if not lado_ok:
        esperado = (
            "take acima e stop abaixo da entrada"
            if desk.is_long
            else "take abaixo e stop acima da entrada"
        )
        return HedgeCalculation.unavailable(
            f"Niveis invertidos para uma {'compra' if desk.is_long else 'venda'}: "
            f"esperado {esperado}, recebido entrada {desk.entry}, "
            f"take {desk.take}, stop {desk.stop}."
        )

    avisos: list[str] = []
    if mapping.basis_risk:
        avisos.append(
            f"Instrumentos diferentes ({desk.symbol} na mesa, {broker.symbol} na "
            "corretora): a cobertura vale enquanto os dois andarem juntos. "
            "O descolamento entre eles é risco de base, e ele é real."
        )

    lote_fixo = fixed_lots is not None and fixed_lots > 0
    pedidos = (
        fixed_lots
        if lote_fixo
        else required_lots(
            desk=desk, broker_spec=broker.spec, mapping=mapping, coverage=coverage
        )
    )
    efetivos = round_volume(
        volume=pedidos,
        step=broker.spec.volume_step,
        minimum=broker.spec.volume_min,
        maximum=broker.spec.volume_max,
    )

    if efetivos <= 0:
        motivo = (
            f"O lote fixo de {pedidos} nao e aceito em {broker.symbol} "
            f"(minimo {broker.spec.volume_min}, passo {broker.spec.volume_step})."
            if lote_fixo
            else (
                f"A cobertura exigiria {pedidos:.4f} lote(s), abaixo do minimo de "
                f"{broker.spec.volume_min} em {broker.symbol}. Nao da para cobrir "
                "esta operacao neste instrumento."
            )
        )
        return HedgeCalculation.unavailable(motivo, warnings=tuple(avisos))

    if efetivos < pedidos:
        avisos.append(
            f"Lote arredondado de {pedidos} para {efetivos} pelo passo de "
            f"{broker.spec.volume_step}"
            + (
                ": a corretora nao aceita o tamanho informado."
                if lote_fixo
                else ": a cobertura fica incompleta."
            )
        )

    # A perna da corretora e recriada com o lote efetivo. Calcular com o
    # lote pedido mostraria um resultado que a corretora nao executaria.
    perna = BrokerLeg(
        symbol=broker.symbol,
        is_long=broker.is_long,
        entry=broker.entry,
        lots=efetivos,
        spec=broker.spec,
        costs=broker.costs,
    )

    exposicao_mesa = desk.exposure()
    exposicao_corretora = perna.exposure() * mapping.price_ratio
    residual = exposicao_mesa - exposicao_corretora

    # Com lote fixo a cobertura e RESULTADO, nao escolha — e quase nunca
    # cai em 1. A diferenca e exposicao direcional de verdade, entao a
    # tela precisa dizer de que lado ela esta: descoberto (a mesa manda) ou
    # excedido (a corretora passou a mandar, e o "hedge" virou posicao).
    if lote_fixo and exposicao_mesa > 0:
        proporcao = exposicao_corretora / exposicao_mesa
        if abs(proporcao - 1) > Decimal("0.02"):
            avisos.append(
                f"Lote fixo de {efetivos} cobre {proporcao:.0%} da exposicao da "
                f"mesa — {'sobra descoberto' if proporcao < 1 else 'excede e vira posicao a favor da corretora'}."
            )

    custo_mesa = desk.costs.total(
        volume=desk.contracts, money_per_price_unit=desk.spec.money_per_price_unit
    )
    custo_corretora = perna.costs.total(
        volume=efetivos, money_per_price_unit=perna.spec.money_per_price_unit
    )

    cenarios: list[ScenarioResult] = []
    alvos = [(Scenario.DESK_TAKE, desk.take), (Scenario.DESK_STOP, desk.stop)]
    if current_desk_price is not None:
        alvos.append((Scenario.TIMEOUT, current_desk_price))

    for nome, preco_mesa in alvos:
        preco_corretora = _broker_price_for(
            preco_mesa, desk=desk, broker=perna, mapping=mapping
        )
        cenarios.append(
            ScenarioResult(
                scenario=nome,
                desk_price=preco_mesa,
                broker_price=preco_corretora,
                desk_gross=desk.gross_at(preco_mesa),
                broker_gross=perna.gross_at(preco_corretora),
                desk_costs=custo_mesa,
                broker_costs=custo_corretora,
            ).rounded()
        )

    return HedgeCalculation(
        available=True,
        currency=desk.spec.currency,
        requested_lots=pedidos.quantize(Decimal("0.0001")),
        effective_lots=efetivos,
        desk_exposure=exposicao_mesa.quantize(CENTS),
        broker_exposure=exposicao_corretora.quantize(CENTS),
        residual_exposure=residual.quantize(CENTS),
        coverage_ratio=(
            (exposicao_corretora / exposicao_mesa).quantize(Decimal("0.0001"))
            if exposicao_mesa > 0
            else Decimal("0")
        ),
        basis_risk=mapping.basis_risk,
        mapping_note=mapping.note,
        scenarios=tuple(cenarios),
        warnings=tuple(avisos),
    )
