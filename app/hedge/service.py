"""Orquestracao: snapshot entra, analise sai.

## A ordem importa, e ela e esta

1. **Setup existe, e deste usuario, e esta aberto?** Barato, e decide
   tudo. Encerrado nao recebe previsao.
2. **Sequencia aceita?** Fora de ordem para aqui, antes de calcular.
3. **Barreira tocada?** Se sim, encerra o setup e devolve o desfecho. Nao
   ha previsao a fazer sobre um evento que ja aconteceu.
4. **Motor financeiro.** Independe de previsao: "quanto da se chegar la"
   e uma conta, e ela vale mesmo sem modelo.
5. **Previsao.** Pode falhar sozinha sem derrubar o item 4.
6. **Jev.** Classificacao de contexto, em modo observacao. Falha dele nao
   afeta nada.
7. **Grava.** Inclusive quando nao houve previsao — a ausencia tambem e
   dado para avaliar o modelo depois.

As camadas 4, 5 e 6 degradam de forma independente de proposito. Um
sistema em que a falha do classificador de noticias apaga o calculo de
lote seria pior que a soma das partes.

## O que este modulo nunca faz

Nao envia, altera nem fecha ordem. Nao existe `app.execution` no import,
e um teste estrutural cobra isso: o conector e um canal de dados, e o
grafico do MetaTrader tem um botao que parece botao de robo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.database.models.hedge import HedgeAnalysis, HedgeSetup, HedgeSetupVersion
from app.database.repositories.hedge_repository import HedgeRepository
from app.hedge.contracts import ContractSpec, mapping_for
from app.hedge.engine import (
    BrokerLeg,
    DeskLeg,
    HedgeCalculation,
    LegCosts,
    Objective,
    Scenario,
    calculate,
)
from app.hedge.forecast.base import Forecast, ForecastInputs, Unavailable
from app.hedge.forecast.registry import default_engine
from app.hedge.sessions import SessionState, accepts_sequence, evaluate_freshness
from app.hedge.units import ConversionError, to_decimal

logger = get_logger(__name__)

SCHEMA_VERSION = 1
"""Versao do contrato trocado com o conector. O EA compara e avisa o
operador quando o servidor mudou — melhor uma mensagem no grafico do que
niveis desenhados a partir de um campo que mudou de nome."""


@dataclass(frozen=True, slots=True)
class SnapshotIn:
    """O que o terminal manda. Campos ausentes ficam `None`."""

    setup_id: int
    terminal_id: str
    chart_id: str
    symbol: str
    sequence: int
    captured_at: datetime
    bid: Decimal
    ask: Decimal
    connector_version: str = ""
    setup_version: int | None = None
    """Versao que o EA acredita estar desenhando. Divergente da vigente,
    a resposta e descartada pelo EA — ver `AnalysisOut.setup_version`."""

    tick_value: Decimal | None = None
    """Lido de `SYMBOL_TRADE_TICK_VALUE` no terminal, ja na moeda da
    conta. Nao vem do banco porque depende da conta logada e do cambio
    do momento."""

    tick_size: Decimal | None = None
    point: Decimal | None = None
    volume_min: Decimal | None = None
    volume_max: Decimal | None = None
    volume_step: Decimal | None = None
    contract_size: Decimal | None = None
    account_currency: str = ""
    atr: float | None = None
    atr_period_seconds: float | None = None
    spread: float | None = None
    tick_volume: int | None = None
    real_volume: int | None = None
    server_time: datetime | None = None

    @property
    def mid(self) -> Decimal:
        """Preco de referencia para a previsao.

        O meio entre bid e ask, nao o ultimo negocio: a barreira e tocada
        por preco executavel, e qual dos dois executa depende do lado.
        O meio e o compromisso honesto e a tela mostra os dois.
        """
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class AnalysisOut:
    """A resposta. Tudo que o EA e o painel precisam, de uma vez."""

    schema_version: int
    setup_id: int
    setup_version: int
    session_key: str
    sequence: int
    state: SessionState
    state_reason: str = ""

    objective: str = ""
    mode: str = ""
    is_long: bool = True
    entry: Decimal = Decimal("0")
    take: Decimal = Decimal("0")
    stop: Decimal = Decimal("0")
    current_price: Decimal = Decimal("0")
    bid: Decimal = Decimal("0")
    ask: Decimal = Decimal("0")
    feed: str = "MT5"

    seconds_remaining: int = 0
    deadline_utc: datetime | None = None
    display_timezone: str = "UTC"

    forecast: Forecast | None = None
    financial: HedgeCalculation | None = None
    jev_state: str = ""
    jev_confidence: float | None = None
    jev_observation_only: bool = True
    """O Jev entra em modo observacao: a classificacao e gravada e
    exibida, mas NAO altera as probabilidades. Permitir influencia exige
    avaliacao fora da amostra que ainda nao foi feita."""

    analysis_valid_until: datetime | None = None
    closed_reason: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _spec_from(snapshot: SnapshotIn, *, symbol: str) -> ContractSpec:
    """Spec da perna da corretora, montada do que o terminal mandou.

    Zeros nos campos ausentes sao intencionais: `ContractSpec.validate`
    os reprova e o calculo sai como indisponivel com a lista do que
    falta. Inventar um tick_value plausivel daria um lote errado com
    aparencia de certo.
    """
    return ContractSpec(
        symbol=symbol,
        digits=0,
        point=snapshot.point or Decimal("0"),
        tick_size=snapshot.tick_size or Decimal("0"),
        tick_value=snapshot.tick_value or Decimal("0"),
        contract_size=snapshot.contract_size or Decimal("0"),
        volume_min=snapshot.volume_min or Decimal("0"),
        volume_max=snapshot.volume_max or Decimal("0"),
        volume_step=snapshot.volume_step or Decimal("0"),
        currency=snapshot.account_currency or "",
    )


def _desk_spec(version: HedgeSetupVersion) -> ContractSpec:
    """Spec da mesa, vinda do cadastro.

    A mesa nao esta no MetaTrader: ninguem le o tick_value dela de uma
    API. O operador informa, e por isso ele e guardado na versao — muda-lo
    depois precisa gerar versao nova como qualquer outro parametro.
    """
    return ContractSpec(
        symbol=version.desk_symbol,
        digits=0,
        point=to_decimal(version.desk_point, field="desk_point"),
        tick_size=to_decimal(version.desk_tick_size, field="desk_tick_size"),
        tick_value=to_decimal(version.desk_tick_value, field="desk_tick_value"),
        contract_size=Decimal("1"),
        volume_min=Decimal("0"),
        volume_max=Decimal("1000000"),
        volume_step=Decimal("0.01"),
        currency=version.account_currency,
    )


def _barrier_touched(version: HedgeSetupVersion, snapshot: SnapshotIn) -> Scenario | None:
    """Alguma barreira foi tocada por preco EXECUTAVEL?

    Comprado sai no bid e e stopado no bid; vendido sai no ask. Usar o
    meio aqui declararia barreira tocada antes de o preco de saida ter
    chegado — meio tick de otimismo que, repetido, vira um historico que
    parece melhor do que foi.
    """
    take = to_decimal(version.desk_take)
    stop = to_decimal(version.desk_stop)

    if version.desk_is_long:
        if snapshot.bid >= take:
            return Scenario.DESK_TAKE
        if snapshot.bid <= stop:
            return Scenario.DESK_STOP
        return None

    if snapshot.ask <= take:
        return Scenario.DESK_TAKE
    if snapshot.ask >= stop:
        return Scenario.DESK_STOP
    return None


def _seconds_remaining(version: HedgeSetupVersion, now: datetime) -> int:
    """Segundos ate o prazo. Tudo em UTC.

    `deadline_utc` vence `horizon_seconds` quando existe: um horario
    limite e uma afirmacao sobre o calendario, enquanto o horizonte e
    uma duracao — e quando os dois discordam, quem escolheu o horario
    foi mais especifico.
    """
    if version.deadline_utc is not None:
        limite = version.deadline_utc
        if limite.tzinfo is None:
            limite = limite.replace(tzinfo=UTC)
        return max(0, int((limite - now).total_seconds()))

    criado = version.created_at
    if criado is None:
        return int(version.horizon_seconds)
    if criado.tzinfo is None:
        criado = criado.replace(tzinfo=UTC)
    fim = criado + timedelta(seconds=int(version.horizon_seconds))
    return max(0, int((fim - now).total_seconds()))


class HedgeService:
    """O caminho completo de um snapshot."""

    def __init__(self, session: Session, *, engine=None) -> None:
        self._session = session
        self._repo = HedgeRepository(session)
        self._engine = engine or default_engine()

    def analyze(
        self, snapshot: SnapshotIn, *, user_id: int, api_token_id: int | None, now: datetime | None = None
    ) -> AnalysisOut:
        agora = now or datetime.now(UTC)

        setup = self._repo.get_setup(snapshot.setup_id, user_id=user_id)
        if setup is None:
            # Mesma resposta para "nao existe" e "nao e seu": distinguir
            # os dois contaria a uma chave vazada quais ids existem.
            raise PermissionError("setup inexistente ou de outro usuario")

        versao = self._repo.current_version(setup)
        if versao is None:
            raise ValueError(f"setup {setup.id} sem versao gravada")

        from app.hedge.sessions import session_key as build_key

        chave = build_key(
            terminal_id=snapshot.terminal_id,
            chart_id=snapshot.chart_id,
            symbol=snapshot.symbol,
            setup_id=setup.id,
        )
        sessao = self._repo.get_or_create_session(
            session_key=chave,
            setup_id=setup.id,
            terminal_id=snapshot.terminal_id,
            chart_id=snapshot.chart_id,
            symbol=snapshot.symbol,
            api_token_id=api_token_id,
            connector_version=snapshot.connector_version,
        )

        base = {
            "schema_version": SCHEMA_VERSION,
            "setup_id": setup.id,
            "setup_version": versao.version,
            "session_key": chave,
            "sequence": snapshot.sequence,
            "objective": setup.objective,
            "mode": setup.mode,
            "is_long": bool(versao.desk_is_long),
            "entry": to_decimal(versao.desk_entry),
            "take": to_decimal(versao.desk_take),
            "stop": to_decimal(versao.desk_stop),
            "current_price": snapshot.mid,
            "bid": snapshot.bid,
            "ask": snapshot.ask,
            "display_timezone": versao.display_timezone,
            "deadline_utc": versao.deadline_utc,
        }

        # --- 1. encerrado nao recebe previsao -----------------------------
        if setup.closed_at is not None:
            sessao.state = SessionState.CLOSED.value
            return AnalysisOut(
                **base,
                state=SessionState.CLOSED,
                state_reason="Operacao encerrada.",
                closed_reason=setup.closed_reason or "",
            )

        # --- 2. fora de ordem --------------------------------------------
        if not accepts_sequence(incoming=snapshot.sequence, last_accepted=sessao.last_sequence):
            frescor = evaluate_freshness(
                last_seen_at=sessao.last_seen_at,
                now=agora,
                max_age_seconds=versao.data_max_age_seconds,
            )
            return AnalysisOut(
                **base,
                state=frescor.state,
                state_reason=(
                    f"Snapshot {snapshot.sequence} chegou fora de ordem "
                    f"(ultimo aceito: {sessao.last_sequence}) e foi descartado."
                ),
            )

        sessao.last_sequence = snapshot.sequence
        sessao.last_seen_at = agora

        restantes = _seconds_remaining(versao, agora)

        # --- 3. barreira tocada encerra a previsao ------------------------
        tocada = _barrier_touched(versao, snapshot)
        if tocada is not None:
            self._repo.close_setup(setup, reason=tocada.value)
            sessao.state = SessionState.CLOSED.value
            financeiro = self._financial(versao, snapshot, setup)
            saida = AnalysisOut(
                **base,
                state=SessionState.CLOSED,
                state_reason=f"Barreira atingida: {tocada.value}.",
                closed_reason=tocada.value,
                seconds_remaining=0,
                financial=financeiro,
                forecast=Forecast.unavailable(
                    Unavailable.BARRIER_HIT,
                    model_kind=self._engine.kind,
                    model_version=self._engine.version,
                ),
            )
            self._persist(saida, setup=setup, snapshot=snapshot, session_id=sessao.id)
            return saida

        if restantes <= 0:
            self._repo.close_setup(setup, reason=Scenario.TIMEOUT.value)
            sessao.state = SessionState.CLOSED.value
            saida = AnalysisOut(
                **base,
                state=SessionState.CLOSED,
                state_reason="Prazo esgotado sem tocar barreira.",
                closed_reason=Scenario.TIMEOUT.value,
                seconds_remaining=0,
                financial=self._financial(versao, snapshot, setup),
                forecast=Forecast.unavailable(
                    Unavailable.EXPIRED,
                    model_kind=self._engine.kind,
                    model_version=self._engine.version,
                ),
            )
            self._persist(saida, setup=setup, snapshot=snapshot, session_id=sessao.id)
            return saida

        # --- 4. financeiro, independente de previsao ----------------------
        financeiro = self._financial(versao, snapshot, setup)

        # --- 5. previsao, que pode faltar sozinha -------------------------
        previsao = self._engine.predict(
            ForecastInputs(
                symbol=snapshot.symbol,
                is_long=bool(versao.desk_is_long),
                entry=float(versao.desk_entry),
                take=float(versao.desk_take),
                stop=float(versao.desk_stop),
                current_price=float(snapshot.mid),
                seconds_remaining=float(restantes),
                feed="MT5",
                atr=snapshot.atr,
                atr_period_seconds=snapshot.atr_period_seconds,
                spread=snapshot.spread,
                tick_volume=snapshot.tick_volume,
                real_volume=snapshot.real_volume,
                server_time=snapshot.server_time,
            )
        )

        estado = SessionState.CONNECTED if previsao.available else SessionState.MODEL_UNAVAILABLE
        sessao.state = estado.value

        avisos = list(financeiro.warnings if financeiro else ())
        if versao.mt5_position_ticket is None and setup.mode == "TRACKING":
            avisos.append(
                "Acompanhamento sem ticket do MT5: a referencia da mesa foi "
                "informada a mao e o sistema nao verifica se a posicao existe."
            )

        saida = AnalysisOut(
            **base,
            state=estado,
            state_reason="" if previsao.available else "Previsao indisponivel.",
            seconds_remaining=restantes,
            forecast=previsao,
            financial=financeiro,
            analysis_valid_until=previsao.valid_until,
            warnings=tuple(avisos),
        )
        self._persist(saida, setup=setup, snapshot=snapshot, session_id=sessao.id)
        return saida

    # --- partes -----------------------------------------------------------

    def _financial(
        self, versao: HedgeSetupVersion, snapshot: SnapshotIn, setup: HedgeSetup
    ) -> HedgeCalculation:
        """O motor financeiro, com as falhas de configuracao viradas em
        `unavailable` legivel em vez de excecao."""
        try:
            spec_mesa = _desk_spec(versao)
            spec_corretora = _spec_from(snapshot, symbol=versao.broker_symbol)
            mapeamento = mapping_for(
                versao.desk_symbol,
                versao.broker_symbol,
                price_ratio=(
                    to_decimal(versao.price_ratio) if versao.price_ratio is not None else None
                ),
            )
        except ConversionError as exc:
            return HedgeCalculation.unavailable(str(exc))

        mesa = DeskLeg(
            symbol=versao.desk_symbol,
            is_long=bool(versao.desk_is_long),
            entry=to_decimal(versao.desk_entry),
            take=to_decimal(versao.desk_take),
            stop=to_decimal(versao.desk_stop),
            contracts=to_decimal(versao.desk_contracts),
            spec=spec_mesa,
            costs=LegCosts(
                commission=to_decimal(versao.desk_commission),
                slippage_price=to_decimal(versao.desk_slippage),
            ),
        )
        corretora = BrokerLeg(
            symbol=versao.broker_symbol,
            is_long=not bool(versao.desk_is_long),
            entry=snapshot.mid,
            lots=Decimal("0"),
            spec=spec_corretora,
            costs=LegCosts(
                commission=to_decimal(versao.broker_commission),
                slippage_price=to_decimal(versao.broker_slippage),
            ),
        )
        try:
            return calculate(
                desk=mesa,
                broker=corretora,
                mapping=mapeamento,
                coverage=to_decimal(versao.coverage_ratio),
                current_desk_price=snapshot.mid,
            )
        except ConversionError as exc:
            return HedgeCalculation.unavailable(str(exc))

    def _persist(
        self,
        saida: AnalysisOut,
        *,
        setup: HedgeSetup,
        snapshot: SnapshotIn,
        session_id: int | None,
    ) -> None:
        """Grava inclusive a ausencia de previsao.

        Um historico que so guarda os acertos nao serve para medir
        calibracao: faltariam justamente as vezes em que o modelo se
        calou, e a taxa de cobertura ficaria invisivel.
        """
        import json

        previsao = saida.forecast
        financeiro = saida.financial

        registro = HedgeAnalysis(
            setup_id=setup.id,
            setup_version=saida.setup_version,
            session_id=session_id,
            sequence=snapshot.sequence,
            captured_at=snapshot.captured_at,
            current_price=snapshot.mid,
            bid=snapshot.bid,
            ask=snapshot.ask,
            feed=saida.feed,
            model_kind=str(previsao.model_kind) if previsao else "",
            model_version=previsao.model_version if previsao else "",
            forecast_available=bool(previsao and previsao.available),
            unavailable_reason=(
                str(previsao.unavailable_reason)
                if previsao and previsao.unavailable_reason
                else None
            ),
            take_first=(
                Decimal(str(previsao.take_first))
                if previsao and previsao.available and previsao.take_first is not None
                else None
            ),
            stop_first=(
                Decimal(str(previsao.stop_first))
                if previsao and previsao.available and previsao.stop_first is not None
                else None
            ),
            no_barrier=(
                Decimal(str(previsao.no_barrier))
                if previsao and previsao.available and previsao.no_barrier is not None
                else None
            ),
            financial_available=bool(financeiro and financeiro.available),
            financial_json=(
                json.dumps(
                    {
                        "currency": financeiro.currency,
                        "effective_lots": str(financeiro.effective_lots),
                        "residual_exposure": str(financeiro.residual_exposure),
                        "scenarios": {
                            str(s.scenario): {
                                "desk_gross": str(s.desk_gross),
                                "broker_gross": str(s.broker_gross),
                                "net": str(s.net),
                            }
                            for s in financeiro.scenarios
                        },
                    }
                )
                if financeiro and financeiro.available
                else None
            ),
            jev_state=saida.jev_state or None,
        )
        self._repo.record_analysis(registro)


def objective_colors(objective: str, *, is_long: bool) -> dict[str, str]:
    """Qual cor vai em qual regiao, dado o objetivo e o lado.

    Vive no servidor porque indicador e painel precisam concordar: duas
    implementacoes da mesma regra divergem, e aqui a divergencia pintaria
    de verde a regiao que o operador quer evitar.

    O NOME do nivel nunca muda — o take da mesa continua sendo o take da
    mesa. Muda so a cor, porque o que e bom depende de qual lado se
    prefere.
    """
    favorece_corretora = objective == Objective.FAVOR_BROKER_PROFIT

    # Quem favorece a corretora quer o stop da mesa: essa regiao vira
    # verde. Quem favorece a mesa quer o take: inverte.
    cor_stop = "GREEN" if favorece_corretora else "RED"
    cor_take = "RED" if favorece_corretora else "GREEN"

    # O lado decide onde cada regiao FICA, nao de que cor ela e. Fixar
    # verde em cima poria a cor certa no nivel errado em toda venda.
    return {
        "take_region": cor_take,
        "stop_region": cor_stop,
        "take_above": "true" if is_long else "false",
    }
