"""API do FotoAnalise.

Camada fina de proposito: valida entrada, chama o servico e serializa. Toda
decisao vive em `app.foto_analise`, e toda analise vive onde ja vivia.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies.auth import get_current_user
from app.database.models.user import User
from app.database.session import get_db
from app.foto_analise.heatmap import HeatmapDetail
from app.foto_analise.service import DIRECTION_AUTO, FotoAnalise, FotoAnaliseService
from app.market.multi_timeframe import ANALYSIS_TIMEFRAMES, SymbolNotFoundError
from app.mt5.market_data import Timeframe

router = APIRouter(prefix="/api/foto-analise", tags=["foto-analise"])

TAKE_MIN = 1
TAKE_MAX = 1000


class FotoAnaliseIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = "M15"
    take_ticks: int = Field(default=20, ge=TAKE_MIN, le=TAKE_MAX)
    direction: str = DIRECTION_AUTO
    detail: str = HeatmapDetail.NORMAL.value


def serialize(foto: FotoAnalise) -> dict:
    """Formato que a tela consome.

    `heatmap` sai com os fatores de cada faixa: um score sem origem
    rastreavel vira superticao, e a tela precisa poder mostrar o porque.
    """
    zona = foto.entry_zone
    return {
        "symbol": foto.symbol,
        "timeframe": foto.timeframe.value,
        "generated_at": foto.generated_at.isoformat(),
        "decision": foto.decision,
        "bias": foto.bias,
        "direction_source": foto.direction_source,
        "direction_rationale": foto.direction_rationale,
        "score": foto.score,
        "current_price": foto.current_price,
        "take_ticks": foto.take_ticks,
        "tick_size": foto.tick_size,
        "entry_zone": (
            None
            if zona is None
            else {
                "min": zona.min,
                "sweet_spot": zona.sweet_spot,
                "max": zona.max,
                "score": zona.score,
                "distance_ticks": zona.distance_ticks,
            }
        ),
        "stop": foto.stop,
        "take": foto.take,
        "status": foto.status,
        "decision_level": foto.decision_level,
        "last_candle_at": (
            foto.last_candle_at.isoformat() if foto.last_candle_at else None
        ),
        "data_age_minutes": foto.data_age_minutes,
        "price_source": foto.price_source,
        "is_stale": foto.is_stale,
        "heatmap": [
            {
                "price": faixa.price,
                "buy_score": faixa.buy_score,
                "sell_score": faixa.sell_score,
                "factors": faixa.factors,
            }
            for faixa in foto.heatmap
        ],
        "candles": [
            {
                "time": c.time.isoformat() if c.time else None,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in foto.candles
        ],
        "levels": [
            {"price": n.price, "label": n.label, "kind": n.kind} for n in foto.levels
        ],
        "reasons_for": foto.reasons_for,
        "reasons_against": foto.reasons_against,
        "warnings": foto.warnings,
    }


def build_foto(db: Session, payload: FotoAnaliseIn) -> FotoAnalise:
    """Validacao + execucao, compartilhada entre a API e a pagina."""
    try:
        timeframe = Timeframe(payload.timeframe.upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"timeframe invalido: {payload.timeframe}",
        ) from exc

    if timeframe not in ANALYSIS_TIMEFRAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"timeframe {timeframe.value} fora da matriz de analise "
                f"({', '.join(t.value for t in ANALYSIS_TIMEFRAMES)})."
            ),
        )

    try:
        detalhe = HeatmapDetail(payload.detail.upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"detalhe invalido: {payload.detail}",
        ) from exc

    try:
        return FotoAnaliseService(db, detail=detalhe).build(
            symbol=payload.symbol.upper(),
            timeframe=timeframe,
            take_ticks=payload.take_ticks,
            direction=payload.direction,
        )
    except SymbolNotFoundError as exc:
        # Simbolo nunca coletado nao e erro de servidor: e uma instrucao.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Simbolo {payload.symbol} nao tem candles coletadas. "
                "Colete dados em Dados de mercado antes de analisar."
            ),
        ) from exc


@router.post("")
def foto_analise(
    payload: FotoAnaliseIn,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    return serialize(build_foto(db, payload))
