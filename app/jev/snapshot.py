"""O resumo tecnico que sai deste processo — e nada alem dele.

## Uma lista fechada, nao um dump

`TechnicalSnapshot` existe para ser a UNICA coisa que o Jev enxerga. Os
campos sao escritos um a um, com tipos primitivos, e `as_state()` monta o
dicionario a partir dessa lista fechada.

A alternativa obvia — serializar o `FotoAnalise` ou o `AnalysisReport`
inteiro — seria mais curta e estaria errada. Aqueles objetos crescem: basta
alguem adicionar um campo com dado de conta, credencial de corretora ou
identificador de usuario para que ele passe a ser enviado a um terceiro, em
silencio, sem ninguem ter decidido isso. Com uma lista fechada, incluir algo
novo e um ato deliberado que aparece no diff.

Por isso tambem nao ha `**kwargs`, nem `dict[str, Any]` de passagem, nem
`dataclasses.asdict`.

## O fingerprint e o que faz o cache valer alguma coisa

`fingerprint()` resume o estado em algo que so muda quando a DECISAO
mudaria. Preco atual fica de fora e o score e arredondado: se entrassem
crus, a impressao digital mudaria a cada tick, o cache nunca acertaria e
cada refresh do indicador viraria uma chamada paga.

Isso nao e hipotetico neste projeto — foi assim que a cota da AIsa foi
esgotada uma vez, com uma consulta por ciclo que nao virou nenhuma entrada.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

MAX_REASON_CODES = 3
"""Quantos codigos o grafico recebe. O limite e de legibilidade: a legenda
divide espaco com o preco, e uma lista que cobre candles deixa de ser
informacao."""


@dataclass(frozen=True, slots=True)
class TechnicalSnapshot:
    """Tudo que o Jev recebe. Nenhum segredo cabe aqui por construcao."""

    symbol: str
    timeframe: str
    direction: str
    """LONG, SHORT ou NONE. Ja decidido pelo motor local — o Jev nao escolhe
    lado."""

    score: float
    recommendation: str
    """ENTER ou DO_NOT_ENTER, do motor tecnico."""

    technical_status: str
    """READY, WAIT_PULLBACK, MISSED, NO_SETUP, CONFIRMATION_REQUIRED,
    NO_TREND."""

    trend: str
    timeframe_trends: dict[str, str] = field(default_factory=dict)
    volume_score: float | None = None
    liquidity_score: float | None = None
    spread_ticks: float | None = None
    data_age_minutes: float | None = None
    is_stale: bool = False
    has_blockers: bool = False
    blocker_count: int = 0
    entry_to_stop_pct: float | None = None
    entry_to_target_pct: float | None = None
    risk_reward: float | None = None
    candle_open_time: str = ""
    """Abertura da candle mais recente, ISO-8601. Entra na chave do cache:
    enquanto a candle nao vira, nao ha o que reavaliar."""

    def as_state(self) -> dict[str, object]:
        """O `state` enviado ao Jev, campo a campo.

        Valores ausentes viram `None` e permanecem `None` — nao sao
        convertidos em zero. "Sem tick, entao sem spread" e "spread zero"
        levam a vereditos diferentes, e achatar os dois faria o modelo
        classificar com uma certeza que ninguem tem.
        """
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "technical_direction": self.direction,
            "technical_score_0_100": round(self.score, 1),
            "technical_recommendation": self.recommendation,
            "technical_status": self.technical_status,
            "trend": self.trend,
            "trend_by_timeframe": dict(self.timeframe_trends),
            "volume_score_0_100": _round_or_none(self.volume_score, 1),
            "liquidity_score_0_100": _round_or_none(self.liquidity_score, 1),
            "spread_ticks": _round_or_none(self.spread_ticks, 2),
            "data_age_minutes": _round_or_none(self.data_age_minutes, 1),
            "data_is_stale": self.is_stale,
            "has_blockers": self.has_blockers,
            "blocker_count": self.blocker_count,
            "entry_to_stop_pct": _round_or_none(self.entry_to_stop_pct, 3),
            "entry_to_target_pct": _round_or_none(self.entry_to_target_pct, 3),
            "risk_reward": _round_or_none(self.risk_reward, 2),
        }

    def fingerprint(self) -> str:
        """Identidade do cenario para efeito de cache.

        Deriva de `as_state()` com os numeros arredondados de novo, mais
        grosso: o objetivo e ignorar oscilacao que nao muda classificacao. O
        preco atual nunca entrou em `as_state()`, entao tambem nao esta
        aqui.
        """
        material = {
            "candle": self.candle_open_time,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "score": round(self.score),
            "recommendation": self.recommendation,
            "status": self.technical_status,
            "trend": self.trend,
            "trends": sorted(self.timeframe_trends.items()),
            "volume": _round_or_none(self.volume_score, 0),
            "liquidity": _round_or_none(self.liquidity_score, 0),
            "stale": self.is_stale,
            "blockers": self.blocker_count,
            "stop_pct": _round_or_none(self.entry_to_stop_pct, 1),
            "target_pct": _round_or_none(self.entry_to_target_pct, 1),
        }
        serial = json.dumps(material, sort_keys=True, default=str)
        return hashlib.sha256(serial.encode("utf-8")).hexdigest()[:32]


def _round_or_none(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits) if digits > 0 else float(round(value))


# --- codigos de motivo -------------------------------------------------------
#
# Derivados AQUI, da analise local, e nunca do texto do Jev. Dois motivos:
#
# 1. O Jev nao produz texto livre — ele escolhe entre rotulos que nos
#    definimos. Pedir uma justificativa a ele seria pedir algo que o modelo
#    nao faz, e qualquer coisa parecida com isso teria de ser inventada.
# 2. Um codigo derivado localmente e VERIFICAVEL: `MTF_ALIGNED` pode ser
#    conferido contra o alinhamento que esta no relatorio. Um codigo vindo
#    de fora seria uma afirmacao sem lastro exibida sobre o grafico.

REASON_LABELS = {
    "MTF_ALIGNED": "timeframes alinhados",
    "MTF_CONFLICT": "timeframes em conflito",
    "VOLUME_FAVORABLE": "volume favoravel",
    "VOLUME_WEAK": "volume fraco",
    "LIQUIDITY_FAVORABLE": "liquidez favoravel",
    "SPREAD_ACCEPTABLE": "spread aceitavel",
    "SPREAD_WIDE": "spread alargado",
    "DATA_STALE": "dados atrasados",
    "BLOCKERS_PRESENT": "bloqueios ativos",
    "NO_DIRECTION": "sem direcao definida",
    "RR_FAVORABLE": "retorno/risco favoravel",
    "RR_POOR": "retorno/risco ruim",
}
"""Rotulo curto de cada codigo, para o grafico. O EA carrega a sua propria
copia desta tabela porque nao pode receber texto livre do servidor."""

_WIDE_SPREAD_TICKS = 20.0
_GOOD_VOLUME = 60.0
_GOOD_LIQUIDITY = 60.0
_GOOD_RR = 1.5


def reason_codes(snapshot: TechnicalSnapshot) -> list[str]:
    """Os codigos que explicam o cenario, em ordem de importancia.

    A ordem nao e alfabetica nem arbitraria: o que IMPEDE vem antes do que
    confirma, porque a legenda e truncada em tres e o operador precisa ver
    primeiro o que o faria desistir.
    """
    impeditivos: list[str] = []
    confirmatorios: list[str] = []

    if snapshot.is_stale:
        impeditivos.append("DATA_STALE")
    if snapshot.has_blockers:
        impeditivos.append("BLOCKERS_PRESENT")
    if snapshot.direction == "NONE":
        impeditivos.append("NO_DIRECTION")

    if snapshot.spread_ticks is not None:
        if snapshot.spread_ticks > _WIDE_SPREAD_TICKS:
            impeditivos.append("SPREAD_WIDE")
        else:
            confirmatorios.append("SPREAD_ACCEPTABLE")

    if snapshot.volume_score is not None:
        if snapshot.volume_score >= _GOOD_VOLUME:
            confirmatorios.append("VOLUME_FAVORABLE")
        else:
            impeditivos.append("VOLUME_WEAK")

    alinhamento = _alignment(snapshot)
    if alinhamento is True:
        confirmatorios.append("MTF_ALIGNED")
    elif alinhamento is False:
        impeditivos.append("MTF_CONFLICT")

    if snapshot.liquidity_score is not None and snapshot.liquidity_score >= _GOOD_LIQUIDITY:
        confirmatorios.append("LIQUIDITY_FAVORABLE")

    if snapshot.risk_reward is not None:
        if snapshot.risk_reward >= _GOOD_RR:
            confirmatorios.append("RR_FAVORABLE")
        else:
            impeditivos.append("RR_POOR")

    return [*impeditivos, *confirmatorios]


def _alignment(snapshot: TechnicalSnapshot) -> bool | None:
    """Os timeframes concordam com o lado escolhido?

    `None` quando nao da para dizer — sem direcao, ou sem timeframe algum
    com dado. Devolver False nesse caso viraria "conflito" na tela, que e
    uma afirmacao diferente de "nao sei" e mais forte do que os dados
    sustentam.
    """
    if snapshot.direction not in ("LONG", "SHORT"):
        return None

    esperado = "UP" if snapshot.direction == "LONG" else "DOWN"
    conhecidos = [
        valor.upper()
        for valor in snapshot.timeframe_trends.values()
        if valor and valor.upper() != "SEM_DADOS"
    ]
    if not conhecidos:
        return None

    contrario = "DOWN" if esperado == "UP" else "UP"
    if any(valor == contrario for valor in conhecidos):
        return False
    return any(valor == esperado for valor in conhecidos)
