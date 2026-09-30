"""FotoAnaliseService — orquestra, nunca analisa.

Todo fato aqui vem de `analyze_symbol` e do snapshot multi-timeframe que
ele ja usa. Este servico so:

1. traduz o `AnalysisReport` em entradas geometricas (`HeatmapInputs`);
2. chama os dois motores visuais;
3. monta o resultado que a tela consome.

Se em algum momento este arquivo comecar a calcular indicador, detectar
padrao ou classificar tendencia, a separacao foi quebrada — e passa a
existir uma segunda opiniao sobre o mesmo mercado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy.orm import Session

from app.foto_analise.direction import (
    SOURCE_MANUAL,
    DirectionContext,
    DirectionDecision,
    FallbackDirectionProvider,
    default_chain,
    news_score_from,
)
from app.foto_analise.entry_zone import EntryStatus, EntryZone, EntryZoneEngine
from app.foto_analise.heatmap import (
    HeatmapBand,
    HeatmapDetail,
    HeatmapInputs,
    OpportunityHeatmapEngine,
)
from app.jev.snapshot import TechnicalSnapshot
from app.market.multi_timeframe import build_multi_timeframe_snapshot
from app.market.regimes import Trend
from app.market.smc import (
    compute_premium_discount,
    detect_liquidity_sweeps,
    detect_order_blocks,
)
from app.market.structure import (
    SRLevel,
    cluster_swing_levels,
    detect_structure_events,
    detect_swings,
    label_swing_structure,
)
from app.mt5.market_data import Timeframe
from app.services.analysis_service import AnalysisReport, analyze_symbol
from app.strategies.base import SignalDirection

DIRECTION_AUTO = "AUTO"
MAX_REASONS = 5
"""Cinco motivos e o limite pedido, e ele e uma decisao de leitura: uma
lista de quinze fatores nao e mais informativa, e menos — ninguem lê
quinze antes de operar."""


STATUS_LABELS: dict[str, str] = {
    "READY": "ENTRADA AGORA",
    "CONFIRMATION_REQUIRED": "AGUARDANDO CONFIRMACAO",
    EntryStatus.WAIT_PULLBACK.value: "AGUARDAR PULLBACK",
    EntryStatus.MISSED.value: "PRECO JA PASSOU",
    EntryStatus.NO_SETUP.value: "SEM ENTRADA BOA AGORA",
    "NO_TREND": "SEM DIRECAO DEFINIDA",
}
"""Rotulo de tela para cada `FotoAnalise.status`.

Uma tabela so, usada pela pagina e pelo SVG. Quando cada um tinha a sua,
renomear um estado deixava a outra para tras — foi exatamente o que
aconteceu com `READY`, e o sintoma foi a pagina inteira em erro 500.

Note que as chaves NAO sao todas `EntryStatus`: `READY`,
`CONFIRMATION_REQUIRED` e `NO_TREND` nascem em `_decide`, que sabe coisas
que a zona nao sabe (a recomendacao da analise, a candle de confirmacao, a
ausencia de direcao). `IN_ZONE` nao aparece de proposito: estar na faixa
sempre vira `READY` ou `CONFIRMATION_REQUIRED` antes de chegar a tela."""


@dataclass(frozen=True, slots=True)
class Level:
    """Um nivel de referencia com rotulo pronto para a tela."""

    price: float
    label: str
    kind: str


@dataclass(frozen=True, slots=True)
class Candle:
    """Candle REAL, como veio do MetaTrader. Nada e sintetizado."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class FotoAnalise:
    symbol: str
    timeframe: Timeframe
    generated_at: datetime
    decision: str
    """COMPRA / VENDA / AGUARDAR / SEM_ENTRADA."""
    bias: str
    score: float
    current_price: float
    take_ticks: int
    tick_size: float
    entry_zone: EntryZone | None
    stop: float | None
    take: float | None
    status: str
    decision_level: float | None
    last_candle_at: datetime | None = None
    """Abertura da candle mais recente usada no desenho."""
    data_age_minutes: float | None = None
    price_source: str = "CANDLE"
    """CANDLE ou TICK. Um tick mais novo que a ultima candle fechada e o
    preco mais atual que este sistema tem — e a diferenca importa quando o
    operador compara a tela com o terminal."""
    direction_source: str = ""
    """Quem escolheu o lado: OPERADOR, AISA_SENTIMENTO ou TENDENCIA.

    Sai no resultado porque trocar o decisor sem registrar qual foi usado
    tornaria impossivel medir se a troca ajudou."""
    direction_rationale: str = ""
    is_stale: bool = False
    """Dados velhos nao viram desenho silencioso.

    Uma foto bonita sobre precos de ontem e pior que nenhuma foto: ela
    parece atual. Quando o coletor para, quem olha a tela precisa VER que
    parou, nao descobrir depois de operar em cima dela."""
    heatmap: list[HeatmapBand] = field(default_factory=list)
    candles: list[Candle] = field(default_factory=list)
    levels: list[Level] = field(default_factory=list)
    reasons_for: list[str] = field(default_factory=list)
    reasons_against: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    price_at: datetime | None = None
    """Horario da cotacao usada em ``current_price`` (tick ou candle)."""
    spread_ticks: float | None = None
    """Spread observado no tick, em ticks negociaveis; ausente sem tick."""
    risk_reward: float | None = None
    """Relacao bruta entre alvo e invalidacao, antes de comissoes."""
    setup_id: str = ""
    """Identifica um cenario para que o terminal nao repita alertas."""
    technical_snapshot: TechnicalSnapshot | None = None
    """Resumo tecnico fechado, para a camada opcional de classificacao visual.

    Vive aqui porque este e o unico ponto onde o `AnalysisReport` e a foto
    coexistem — montar o resumo depois obrigaria a rota a chamar
    `analyze_symbol` de novo, e essa chamada custa credito de API. Ver
    `app/jev/snapshot.py`."""


class FotoAnaliseService:
    def __init__(
        self,
        session: Session,
        *,
        detail: HeatmapDetail = HeatmapDetail.NORMAL,
        candles_shown: int = 60,
        directions: FallbackDirectionProvider | None = None,
    ) -> None:
        self._session = session
        self._detail = detail
        self._candles_shown = candles_shown
        # Injetavel para o teste trocar o decisor sem rede nem banco.
        self._directions = directions or default_chain()

    def build(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        take_ticks: int,
        direction: str = DIRECTION_AUTO,
        now: datetime | None = None,
    ) -> FotoAnalise:
        # `enforce_gates=False`: os portoes de noticias/calendario decidem se
        # o ROBO opera. Esta tela e consultiva — bloquear o desenho porque ha
        # evento economico esconderia justamente o mapa que ajuda a decidir
        # esperar. Os bloqueios continuam visiveis, como aviso.
        report = analyze_symbol(
            self._session,
            symbol=symbol,
            primary_timeframe=timeframe,
            now=now,
            enforce_gates=False,
        )

        snapshot = build_multi_timeframe_snapshot(
            self._session,
            symbol=symbol,
            now=now or datetime.now().astimezone(),
        )
        primary = snapshot.get(timeframe)

        tick_size = self._tick_size(symbol)
        candles = self._recent_candles(primary)
        if not candles:
            return self._empty(
                symbol, timeframe, report, take_ticks, tick_size,
                "Sem candles suficientes neste timeframe — colete dados antes de analisar.",
            )

        agora = now or datetime.now().astimezone()
        current_price, fonte_preco, preco_em, spread_ticks = self._latest_price(
            symbol, candles, agora, tick_size
        )
        idade, velho = self._data_age(preco_em, timeframe, agora)
        features = primary.features if primary is not None else None
        atr = _last_float(features, "atr_14") or (current_price * 0.002)

        suportes, resistencias = self._sr_levels(primary)
        entradas = HeatmapInputs(
            current_price=current_price,
            atr=atr,
            trend=report.trend,
            tick_size=tick_size,
            take_ticks=take_ticks,
            supports=suportes,
            resistances=resistencias,
            vwap=_last_float(features, "vwap_20"),
            emas={
                periodo: valor
                for periodo in (9, 21, 50, 200)
                if (valor := _last_float(features, f"ema_{periodo}")) is not None
            },
            order_blocks=self._order_blocks(primary),
            sweeps=self._sweeps(primary, suportes + resistencias),
            premium_discount=self._premium_discount(primary),
        )

        heatmap = OpportunityHeatmapEngine(detail=self._detail).build(entradas)
        decisao_direcao = self._resolve_bias(direction, report, symbol)
        vies = decisao_direcao.direction if decisao_direcao else None
        zona = (
            EntryZoneEngine().build(
                heatmap,
                direction=vies,
                current_price=current_price,
                tick_size=tick_size,
            )
            if vies is not None
            else None
        )

        nivel_decisao = self._decision_level(heatmap, current_price)
        stop, take = self._stop_and_take(zona, vies, take_ticks, tick_size, report)
        decisao, status = self._decide(zona, vies, report, candles)
        retorno_risco = self._risk_reward(zona, stop, take)
        favor, contra = self._reasons(report, entradas, vies)
        setup_id = self._setup_id(symbol, timeframe, vies, zona, candles[-1].time)

        return FotoAnalise(
            symbol=symbol,
            timeframe=timeframe,
            generated_at=report.generated_at,
            decision=decisao,
            bias=(
                "LONG"
                if vies == SignalDirection.LONG
                else "SHORT" if vies == SignalDirection.SHORT else "NEUTRAL"
            ),
            score=round(report.score.total_score, 1),
            current_price=current_price,
            take_ticks=take_ticks,
            tick_size=tick_size,
            entry_zone=zona,
            stop=stop,
            take=take,
            status=status,
            decision_level=nivel_decisao,
            heatmap=heatmap,
            candles=candles,
            levels=self._levels(zona, stop, take, suportes, resistencias, nivel_decisao),
            reasons_for=favor,
            reasons_against=contra,
            warnings=self._warnings(report, velho, idade, spread_ticks, take_ticks, retorno_risco),
            last_candle_at=candles[-1].time,
            data_age_minutes=idade,
            price_source=fonte_preco,
            direction_source=decisao_direcao.source if decisao_direcao else "",
            direction_rationale=decisao_direcao.rationale if decisao_direcao else "",
            technical_snapshot=self._technical_snapshot(
                report=report,
                symbol=symbol,
                timeframe=timeframe,
                vies=vies,
                status=status,
                zona=zona,
                stop=stop,
                take=take,
                idade=idade,
                velho=velho,
                spread_ticks=spread_ticks,
                retorno_risco=retorno_risco,
                candle_at=candles[-1].time,
            ),
            is_stale=velho,
            price_at=preco_em,
            spread_ticks=spread_ticks,
            risk_reward=retorno_risco,
            setup_id=setup_id if status == "READY" else "",
        )

    # --- leitura do que ja existe ----------------------------------------

    def _tick_size(self, symbol: str) -> float:
        """O tick real do simbolo, do catalogo ja coletado do MetaTrader.

        Sem ele, "20 ticks" nao tem significado: 20 ticks no MNQ e 5.00
        pontos, no EURUSD e 0.0020.
        """
        from app.database.repositories.symbol_repository import SymbolRepository

        registro = SymbolRepository(self._session).get_by_name(symbol)
        if registro is None or registro.point is None:
            return 0.0
        return float(registro.trade_tick_size or registro.point)

    def _latest_price(
        self, symbol: str, candles: list[Candle], agora: datetime, tick_size: float
    ) -> tuple[float, str, datetime | None, float | None]:
        """O preco mais atual que o sistema tem.

        A coleta so grava candles FECHADAS — por desenho, a ultima candle ja
        nasce ate um timeframe atrasada. Quando ha tick mais novo que ela,
        ele e o preco de verdade, e usa-lo evita que a tela discorde do
        terminal por um motivo que ninguem consegue explicar.

        Nao inventa nada: sem tick mais novo, continua o fechamento da
        ultima candle.
        """
        from app.database.repositories.symbol_repository import SymbolRepository
        from app.database.repositories.tick_repository import TickRepository

        fechamento = candles[-1].close
        horario_candle = candles[-1].time
        registro = SymbolRepository(self._session).get_by_name(symbol)
        if registro is None:
            return fechamento, "CANDLE", horario_candle, None

        ticks = TickRepository(self._session).get_recent(registro.id, 1)
        if not ticks:
            return fechamento, "CANDLE", horario_candle, None

        tick = ticks[-1]
        marca = _aware(tick.timestamp)
        ultima_candle = _aware(candles[-1].time)
        tempo_atual = _aware(agora)
        # Um tick antigo nao e uma cotacao viva so por ser mais novo que a
        # ultima candle. Tambem rejeitamos relogios no futuro: um timestamp
        # adiantado faria uma base parada parecer saudavel indefinidamente.
        if (
            marca is None
            or ultima_candle is None
            or tempo_atual is None
            or marca <= ultima_candle
            or (tempo_atual - marca).total_seconds() < -5
            or (tempo_atual - marca).total_seconds() > 120
        ):
            return fechamento, "CANDLE", horario_candle, None

        # Mid entre bid e ask: usar so o bid enviesaria toda a geometria da
        # zona pelo lado comprado do spread.
        bid, ask = float(tick.bid), float(tick.ask)
        preco = (bid + ask) / 2 if ask > 0 else bid
        if preco <= 0:
            return fechamento, "CANDLE", horario_candle, None
        spread = (ask - bid) / tick_size if ask >= bid and tick_size > 0 else None
        return preco, "TICK", marca, round(spread, 2) if spread is not None else None

    def _data_age(
        self, ultima: datetime | None, timeframe: Timeframe, agora: datetime
    ) -> tuple[float | None, bool]:
        """Idade do dado e se ela ja compromete o desenho.

        O limite e relativo ao timeframe, nao um numero fixo: 30 minutos de
        atraso e irrelevante no D1 e inaceitavel no M1. Tres barras de
        atraso significa que o coletor perdeu o passo.
        """
        from app.mt5.market_data import TIMEFRAME_SECONDS

        marca = _aware(ultima)
        if marca is None:
            return None, True

        idade = (_aware(agora) - marca).total_seconds()
        limite = TIMEFRAME_SECONDS.get(timeframe, 900) * 3
        return round(idade / 60, 1), idade > limite

    def _warnings(
        self,
        report: AnalysisReport,
        velho: bool,
        idade: float | None,
        spread_ticks: float | None,
        take_ticks: int,
        risk_reward: float | None,
    ) -> list[str]:
        avisos: list[str] = []
        if velho:
            quanto = f"{idade:.0f} min" if idade is not None else "tempo desconhecido"
            avisos.append(
                f"DADOS DESATUALIZADOS: a candle mais recente tem {quanto}. "
                "O coletor MT5 provavelmente esta parado — esta foto retrata o "
                "passado, nao o mercado agora."
            )
        avisos.extend(report.rejection_reasons)
        if spread_ticks is not None and spread_ticks >= take_ticks * 0.25:
            avisos.append(
                f"Spread observado de {spread_ticks:.1f} ticks consome uma parte relevante "
                f"do alvo de {take_ticks} ticks."
            )
        if risk_reward is not None and risk_reward < 1.0:
            avisos.append(
                f"Retorno/risco bruto de {risk_reward:.2f} esta abaixo de 1.0."
            )
        return avisos[:MAX_REASONS]

    def _recent_candles(self, primary) -> list[Candle]:
        if primary is None or not primary.candles:
            return []
        return [
            Candle(
                time=getattr(c, "open_time", None) or getattr(c, "time", None),
                open=float(c.open),
                high=float(c.high),
                low=float(c.low),
                close=float(c.close),
                volume=float(getattr(c, "tick_volume", 0) or 0),
            )
            for c in primary.candles[-self._candles_shown :]
        ]

    def _sr_levels(self, primary) -> tuple[list[SRLevel], list[SRLevel]]:
        if primary is None or not primary.candles:
            return [], []
        swings = detect_swings(primary.candles)
        niveis = cluster_swing_levels(swings)
        return (
            [n for n in niveis if n.kind == "SUPPORT"],
            [n for n in niveis if n.kind == "RESISTANCE"],
        )

    def _order_blocks(self, primary) -> list:
        if primary is None or not primary.candles:
            return []
        swings = detect_swings(primary.candles)
        eventos = detect_structure_events(primary.candles, label_swing_structure(swings))
        return detect_order_blocks(primary.candles, eventos)

    def _sweeps(self, primary, levels: list[SRLevel]) -> list:
        if primary is None or not primary.candles or not levels:
            return []
        return detect_liquidity_sweeps(primary.candles, levels)

    def _premium_discount(self, primary):
        if primary is None or not primary.candles:
            return None
        swings = detect_swings(primary.candles)
        altos = [s for s in swings if s.kind.value == "HIGH"]
        baixos = [s for s in swings if s.kind.value == "LOW"]
        if not altos or not baixos:
            return None
        return compute_premium_discount(altos[-1], baixos[-1])

    # --- decisao ----------------------------------------------------------

    def _technical_snapshot(
        self,
        *,
        report: AnalysisReport,
        symbol: str,
        timeframe: Timeframe,
        vies: SignalDirection | None,
        status: str,
        zona: EntryZone | None,
        stop: float | None,
        take: float | None,
        idade: float | None,
        velho: bool,
        spread_ticks: float | None,
        retorno_risco: float | None,
        candle_at: datetime,
    ) -> TechnicalSnapshot:
        """O resumo fechado que a camada de classificacao visual consome.

        Montado aqui porque este e o unico lugar onde o `AnalysisReport` e a
        foto existem juntos. Refaze-lo na rota custaria outra
        `analyze_symbol` — e essa chamada gasta credito de API externa.

        Distancias saem em PERCENTUAL do preco, nao em ticks. Um resumo que
        diz "stop a 40 ticks" nao e comparavel entre MNQ e EURUSD; "0.35%" e.
        """
        fatores = {fator.name: fator for fator in report.score.factors}
        referencia = zona.sweet_spot if zona else 0.0

        return TechnicalSnapshot(
            symbol=symbol,
            timeframe=timeframe.value,
            direction=vies.value if vies is not None else "NONE",
            score=report.score.total_score,
            recommendation=report.recommendation,
            technical_status=status,
            trend=report.trend.value,
            timeframe_trends={
                tf.value: rotulo for tf, rotulo in report.multi_timeframe_alignment.items()
            },
            volume_score=_factor_score(fatores, "volume"),
            liquidity_score=_factor_score(fatores, "liquidity"),
            spread_ticks=spread_ticks,
            data_age_minutes=idade,
            is_stale=velho,
            has_blockers=bool(report.rejection_reasons),
            blocker_count=len(report.rejection_reasons),
            entry_to_stop_pct=_distance_pct(referencia, stop),
            entry_to_target_pct=_distance_pct(referencia, take),
            risk_reward=retorno_risco,
            candle_open_time=candle_at.isoformat(),
        )

    def _resolve_bias(
        self, direction: str, report: AnalysisReport, symbol: str
    ) -> DirectionDecision | None:
        """Direcao forcada pelo usuario vence a automatica — de proposito.

        Quem pede "so compra" quer ver a melhor compra possivel, mesmo em
        tendencia de baixa; esconder isso seria responder outra pergunta. O
        contexto adverso aparece nos motivos e no score, nao como recusa.

        No modo AUTO a escolha passa pela cadeia de provedores: o
        sentimento da AIsa opina quando tem dados, a tendencia decide o
        resto. Devolve a DECISAO, nao so a direcao, para que a tela possa
        dizer quem escolheu — trocar o decisor sem registrar qual foi
        usado tornaria impossivel medir se a troca ajudou.
        """
        pedido = (direction or DIRECTION_AUTO).strip().upper()
        if pedido in {"COMPRA", "BUY", "LONG"}:
            return DirectionDecision(
                direction=SignalDirection.LONG,
                source=SOURCE_MANUAL,
                rationale="direcao escolhida pelo operador",
            )
        if pedido in {"VENDA", "SELL", "SHORT"}:
            return DirectionDecision(
                direction=SignalDirection.SHORT,
                source=SOURCE_MANUAL,
                rationale="direcao escolhida pelo operador",
            )

        return self._directions.decide(
            DirectionContext(
                symbol=symbol,
                trend=report.trend.value,
                news_score=news_score_from(report),
            )
        )

    def _decide(
        self,
        zona: EntryZone | None,
        vies: SignalDirection | None,
        report: AnalysisReport,
        candles: list[Candle],
    ) -> tuple[str, str]:
        if vies is None:
            return "SEM_ENTRADA", "NO_TREND"
        if zona is None or zona.status == EntryStatus.NO_SETUP:
            return "SEM_ENTRADA", EntryStatus.NO_SETUP.value
        if zona.status == EntryStatus.IN_ZONE:
            # Estar dentro da faixa e apenas localizacao. A entrada so fica
            # pronta quando a analise principal aprova o contexto E a ultima
            # candle fechada confirma o lado; assim "ENTRADA AGORA" nao e
            # sinonimo de "preco tocou a zona".
            if report.recommendation == "ENTER" and self._candle_confirms(candles, vies):
                return ("COMPRA" if vies == SignalDirection.LONG else "VENDA"), "READY"
            return "AGUARDAR", "CONFIRMATION_REQUIRED"
        return "AGUARDAR", zona.status.value

    @staticmethod
    def _candle_confirms(candles: list[Candle], direction: SignalDirection) -> bool:
        if not candles:
            return False
        ultima = candles[-1]
        return (
            ultima.close > ultima.open
            if direction == SignalDirection.LONG
            else ultima.close < ultima.open
        )

    def _stop_and_take(
        self,
        zona: EntryZone | None,
        vies: SignalDirection | None,
        take_ticks: int,
        tick_size: float,
        report: AnalysisReport,
    ) -> tuple[float | None, float | None]:
        """Take vem do que o usuario pediu; stop, da estrutura.

        Assimetria proposital. O alvo e escolha do operador — foi o que ele
        digitou. Ja a invalidacao e um fato do grafico: onde o cenario deixa
        de valer. Derivar o stop do take produziria uma invalidacao que o
        mercado nao respeita.
        """
        if zona is None or vies is None:
            return None, None

        distancia = take_ticks * tick_size
        if vies == SignalDirection.LONG:
            take = zona.sweet_spot + distancia
            stop = zona.min - max(distancia * 0.5, tick_size * 4)
        else:
            take = zona.sweet_spot - distancia
            stop = zona.max + max(distancia * 0.5, tick_size * 4)

        # Quando a analise ja produziu uma invalidacao estrutural, ela vale
        # mais que a derivada do take — e um nivel observado, nao calculado.
        niveis = report.trade_levels
        if niveis is not None:
            estrutural = niveis.stop_loss
            mais_distante = (
                estrutural < zona.min
                if vies == SignalDirection.LONG
                else estrutural > zona.max
            )
            if mais_distante:
                stop = estrutural

        return _round_to_tick(stop, tick_size), _round_to_tick(take, tick_size)

    @staticmethod
    def _risk_reward(
        zona: EntryZone | None, stop: float | None, take: float | None
    ) -> float | None:
        if zona is None or stop is None or take is None:
            return None
        risco = abs(zona.sweet_spot - stop)
        retorno = abs(take - zona.sweet_spot)
        if risco <= 0 or retorno <= 0:
            return None
        return round(retorno / risco, 2)

    @staticmethod
    def _setup_id(
        symbol: str,
        timeframe: Timeframe,
        direction: SignalDirection | None,
        zona: EntryZone | None,
        candle_at: datetime | None,
    ) -> str:
        if direction is None or zona is None or candle_at is None:
            return ""
        return ":".join(
            (
                symbol,
                timeframe.value,
                direction.value,
                f"{zona.min:.8f}",
                f"{zona.max:.8f}",
                _aware(candle_at).isoformat(),
            )
        )

    def _decision_level(
        self, heatmap: list[HeatmapBand], current_price: float
    ) -> float | None:
        """Onde compradores e vendedores se equivalem.

        E a faixa de menor diferenca entre os dois scores. O desempate pela
        PROXIMIDADE do preco atual nao e detalhe: num mapa plano dezenas de
        faixas empatam, e `min()` devolveria a primeira da lista — uma
        borda arbitraria. Uma linha de batalha desenhada na ponta do
        grafico e pior que nenhuma, porque parece informacao.
        """
        if not heatmap:
            return None
        menor = min(abs(b.buy_score - b.sell_score) for b in heatmap)
        empatadas = [b for b in heatmap if abs(b.buy_score - b.sell_score) == menor]
        return min(empatadas, key=lambda b: abs(b.price - current_price)).price

    def _levels(
        self,
        zona: EntryZone | None,
        stop: float | None,
        take: float | None,
        suportes: list[SRLevel],
        resistencias: list[SRLevel],
        decisao: float | None,
    ) -> list[Level]:
        niveis: list[Level] = []
        if resistencias:
            forte = max(resistencias, key=lambda n: n.touches)
            niveis.append(Level(forte.price, "Resistencia", "RESISTANCE"))
        if take is not None:
            niveis.append(Level(take, "Take", "TAKE"))
        if decisao is not None:
            niveis.append(Level(decisao, "Decision Level", "DECISION"))
        if zona is not None:
            niveis.append(Level(zona.sweet_spot, "Sweet Spot", "SWEET_SPOT"))
        if stop is not None:
            niveis.append(Level(stop, "Invalidacao", "STOP"))
        if suportes:
            forte = max(suportes, key=lambda n: n.touches)
            niveis.append(Level(forte.price, "Suporte", "SUPPORT"))
        return sorted(niveis, key=lambda n: n.price, reverse=True)

    def _reasons(
        self, report: AnalysisReport, entradas: HeatmapInputs, vies: SignalDirection | None
    ) -> tuple[list[str], list[str]]:
        """Motivos vindos da analise, nao inventados aqui."""
        favor = list(report.confluences)[:MAX_REASONS]

        contra: list[str] = []
        if vies is None:
            return favor, ["Mercado lateral — direcao automatica suspensa", *report.rejection_reasons][
                :MAX_REASONS
            ]
        comprando = vies == SignalDirection.LONG
        obstaculos = entradas.resistances if comprando else entradas.supports
        proximos = [
            n
            for n in obstaculos
            if abs(n.price - entradas.current_price) <= entradas.atr * 1.5
        ]
        if proximos:
            rotulo = "Resistencia" if comprando else "Suporte"
            contra.append(f"{rotulo} proxima em {proximos[0].price:.5g}")

        if entradas.trend == Trend.SIDEWAYS:
            contra.append("Mercado lateral — sem tendencia definida")
        elif (entradas.trend == Trend.DOWN) == comprando:
            contra.append("Operacao contra a tendencia do timeframe")

        contra.extend(report.rejection_reasons)
        return favor, contra[:MAX_REASONS]

    def _empty(
        self,
        symbol: str,
        timeframe: Timeframe,
        report: AnalysisReport,
        take_ticks: int,
        tick_size: float,
        aviso: str,
    ) -> FotoAnalise:
        return FotoAnalise(
            symbol=symbol,
            timeframe=timeframe,
            generated_at=report.generated_at,
            decision="SEM_ENTRADA",
            bias="NEUTRAL",
            score=round(report.score.total_score, 1),
            current_price=0.0,
            take_ticks=take_ticks,
            tick_size=tick_size,
            entry_zone=None,
            stop=None,
            take=None,
            status=EntryStatus.NO_SETUP.value,
            decision_level=None,
            warnings=[aviso],
        )


def _aware(momento: datetime | None) -> datetime | None:
    """Compara datas sem explodir por fuso.

    O SQLite devolve datetime ingenuo mesmo em coluna com timezone; o MySQL
    devolve consciente. Subtrair um do outro levanta TypeError, e a falha
    apareceria so em producao — nunca no teste.
    """
    if momento is None:
        return None
    return momento if momento.tzinfo is not None else momento.replace(tzinfo=UTC)


def _last_float(features: pd.DataFrame | None, coluna: str) -> float | None:
    """Ultimo valor de uma coluna ja calculada, ou None.

    None nunca vira zero: zero seria um valor de mercado plausivel e
    silenciosamente errado.
    """
    if features is None or features.empty or coluna not in features.columns:
        return None
    valor = features[coluna].iloc[-1]
    if pd.isna(valor):
        return None
    return float(valor)


def _round_to_tick(price: float, tick_size: float) -> float:
    """Mantem stop e alvo em um preco que o ativo realmente aceita."""
    if tick_size <= 0:
        return round(price, 8)
    return round(round(price / tick_size) * tick_size, 8)


def _factor_score(fatores: dict, nome: str) -> float | None:
    """O score bruto de um fator, ou None quando ele nao tem dados.

    Ausencia continua ausencia: um fator sem dados vale None, nunca 50.
    Cinquenta seria uma leitura neutra que ninguem emitiu, e ela chegaria
    ao classificador como se fosse medicao.
    """
    fator = fatores.get(nome)
    if fator is None or not getattr(fator, "has_data", True):
        return None
    return float(fator.raw_score)


def _distance_pct(referencia: float, alvo: float | None) -> float | None:
    """Distancia ate um nivel, em percentual do preco de referencia.

    Percentual e nao ticks porque o resumo atravessa ativos: 40 ticks sao
    10 pontos no MNQ e 0.0040 no EURUSD, e comparar os dois numeros crus
    nao significa nada.
    """
    if alvo is None or referencia <= 0:
        return None
    return abs(alvo - referencia) / referencia * 100.0
