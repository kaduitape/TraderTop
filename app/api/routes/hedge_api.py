"""Contrato HTTPS do Hedge AI — o que o EA conector consome.

## Por que `/api/v1/` quando o resto do projeto nao versiona

O Pulso usa `contract_version` no corpo porque nasceu antes de haver mais
de um cliente. Aqui o cliente e um arquivo `.mq5` COMPILADO que vive na
maquina do operador: atualizar o servidor nao atualiza o terminal dele.
O caminho versionado permite servir o contrato antigo enquanto o novo
existe, o que o campo no corpo nao resolve — ele avisa da quebra depois
que ela aconteceu.

## Formato: aninhado aqui, raso no Pulso

O Pulso e lido por busca de string em MQL5, e por isso e raso. Este
contrato tem um parser dedicado (`HedgeJson.mqh`), entao pode ser
aninhado — e aninhar e melhor: `forecast` e `financial` degradam
independentemente, e dois blocos com `available` proprio dizem isso sem
ambiguidade.

## Probabilidade ausente e `null`, nunca zero

Zero significa "estimamos que nao acontece". A ausencia de estimativa e
outra coisa, e confundir as duas na tela de uma operacao com dinheiro
real e o erro que este contrato existe para impedir.

## O que esta rota nao faz

Nao envia, altera nem cancela ordem. Nao importa `app.execution` nem
`app.mt5.orders`, e um teste estrutural cobra isso.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies.api_token import (
    SCOPE_ANALYSIS,
    SCOPE_SETUP_READ,
    SCOPE_TELEMETRY,
    require_scopes,
)
from app.database.models.api_token import ApiToken
from app.database.repositories.hedge_repository import HedgeRepository
from app.database.session import get_db
from app.hedge.engine import Scenario
from app.hedge.ratelimit import snapshot_limiter
from app.hedge.service import (
    SCHEMA_VERSION,
    AnalysisOut,
    HedgeService,
    SnapshotIn,
    objective_colors,
)

router = APIRouter(prefix="/api/v1/hedge", tags=["hedge"])


class SnapshotBody(BaseModel):
    """O corpo do snapshot. Validado antes de tocar o banco."""

    setup_id: int = Field(ge=1)
    terminal_id: str = Field(min_length=1, max_length=64)
    chart_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=40)
    sequence: int = Field(ge=0)
    captured_at: datetime
    bid: Decimal
    ask: Decimal

    connector_version: str = Field(default="", max_length=32)
    setup_version: int | None = None
    schema_version: int = SCHEMA_VERSION

    tick_value: Decimal | None = None
    tick_size: Decimal | None = None
    point: Decimal | None = None
    volume_min: Decimal | None = None
    volume_max: Decimal | None = None
    volume_step: Decimal | None = None
    contract_size: Decimal | None = None
    account_currency: str = Field(default="", max_length=8)

    atr: float | None = None
    atr_period_seconds: float | None = None
    spread: float | None = None
    tick_volume: int | None = None
    real_volume: int | None = None
    server_time: datetime | None = None

    def to_snapshot(self) -> SnapshotIn:
        return SnapshotIn(
            setup_id=self.setup_id,
            terminal_id=self.terminal_id,
            chart_id=self.chart_id,
            symbol=self.symbol,
            sequence=self.sequence,
            captured_at=_aware(self.captured_at),
            bid=self.bid,
            ask=self.ask,
            connector_version=self.connector_version,
            setup_version=self.setup_version,
            tick_value=self.tick_value,
            tick_size=self.tick_size,
            point=self.point,
            volume_min=self.volume_min,
            volume_max=self.volume_max,
            volume_step=self.volume_step,
            contract_size=self.contract_size,
            account_currency=self.account_currency,
            atr=self.atr,
            atr_period_seconds=self.atr_period_seconds,
            spread=self.spread,
            tick_volume=self.tick_volume,
            real_volume=self.real_volume,
            server_time=_aware(self.server_time) if self.server_time else None,
        )


def _aware(momento: datetime) -> datetime:
    """Tudo interno em UTC. Horario sem fuso e assumido UTC e dito assim
    na documentacao — adivinhar o fuso do terminal produziria uma idade de
    dado errada por horas."""
    return momento if momento.tzinfo is not None else momento.replace(tzinfo=UTC)


def _forecast_payload(saida: AnalysisOut) -> dict:
    previsao = saida.forecast
    if previsao is None:
        return {
            "available": False,
            "reason": "NO_MODEL",
            "model_kind": "",
            "model_version": "",
            "take_first": None,
            "stop_first": None,
            "no_barrier": None,
        }

    percentuais = previsao.percentages()
    return {
        "available": previsao.available,
        "reason": str(previsao.unavailable_reason) if previsao.unavailable_reason else "",
        "model_kind": str(previsao.model_kind),
        "model_version": previsao.model_version,
        "feed": previsao.feed,
        # `null` quando nao ha previsao. Zero seria lido como estimativa.
        "take_first": previsao.take_first,
        "stop_first": previsao.stop_first,
        "no_barrier": previsao.no_barrier,
        # Inteiros que somam 100, prontos para a tela — formatar no MQL5
        # exigiria replicar o maior-resto, e duas implementacoes divergem.
        "take_first_pct": percentuais[0] if percentuais else None,
        "stop_first_pct": percentuais[1] if percentuais else None,
        "no_barrier_pct": percentuais[2] if percentuais else None,
        "confidence": previsao.confidence,
        "valid_until": previsao.valid_until.isoformat() if previsao.valid_until else "",
        "assumptions": list(previsao.assumptions),
        "notes": list(previsao.notes),
    }


def _financial_payload(saida: AnalysisOut) -> dict:
    calculo = saida.financial
    if calculo is None:
        return {"available": False, "reason": "Nao calculado."}
    if not calculo.available:
        return {"available": False, "reason": calculo.reason, "warnings": list(calculo.warnings)}

    return {
        "available": True,
        "currency": calculo.currency,
        "requested_lots": str(calculo.requested_lots),
        "effective_lots": str(calculo.effective_lots),
        "desk_exposure": str(calculo.desk_exposure),
        "broker_exposure": str(calculo.broker_exposure),
        "residual_exposure": str(calculo.residual_exposure),
        "coverage_ratio": str(calculo.coverage_ratio),
        "basis_risk": calculo.basis_risk,
        "mapping_note": calculo.mapping_note,
        "scenarios": {
            str(cenario.scenario): {
                "desk_price": str(cenario.desk_price),
                "broker_price": str(cenario.broker_price),
                "desk_gross": str(cenario.desk_gross),
                "broker_gross": str(cenario.broker_gross),
                "desk_costs": str(cenario.desk_costs),
                "broker_costs": str(cenario.broker_costs),
                "net": str(cenario.net),
            }
            for cenario in calculo.scenarios
        },
        "warnings": list(calculo.warnings),
    }


def analysis_payload(saida: AnalysisOut) -> dict:
    """A resposta completa, usada pela rota e pelo painel."""
    return {
        "schema_version": saida.schema_version,
        "setup_id": saida.setup_id,
        "setup_version": saida.setup_version,
        "session_key": saida.session_key,
        "sequence": saida.sequence,
        "state": str(saida.state),
        "state_reason": saida.state_reason,
        "closed_reason": saida.closed_reason,
        "objective": saida.objective,
        "mode": saida.mode,
        "levels": {
            "is_long": saida.is_long,
            "entry": str(saida.entry),
            "take": str(saida.take),
            "stop": str(saida.stop),
            "unit": "PRICE",
        },
        "price": {
            "current": str(saida.current_price),
            "bid": str(saida.bid),
            "ask": str(saida.ask),
            "feed": saida.feed,
        },
        "deadline": {
            "seconds_remaining": saida.seconds_remaining,
            "deadline_utc": saida.deadline_utc.isoformat() if saida.deadline_utc else "",
            "display_timezone": saida.display_timezone,
        },
        "colors": objective_colors(saida.objective, is_long=saida.is_long),
        "forecast": _forecast_payload(saida),
        "financial": _financial_payload(saida),
        # Separado da previsao de proposito: e classificacao de CONTEXTO
        # (titulos de evento economico), em modo observacao, e nao entra
        # nas probabilidades. `available=false` com `reason` distingue
        # "desligado" de "sem evento" de "falhou" — os tres desenham a
        # mesma coisa e pedem acoes diferentes.
        "jev": {
            "available": saida.jev_available,
            "category": saida.jev_state,
            "confidence": saida.jev_confidence,
            "needs_review": saida.jev_needs_review,
            "reason": saida.jev_reason,
            "model": saida.jev_model,
            "observation_only": saida.jev_observation_only,
        },
        "analysis_valid_until": (
            saida.analysis_valid_until.isoformat() if saida.analysis_valid_until else ""
        ),
        "warnings": list(saida.warnings),
        "disclaimer": (
            "Classificacao e probabilidades sao estimativas do modelo declarado, "
            "nao garantia de resultado. Este conector nao envia ordens."
        ),
    }


@router.get("/ping")
def ping(
    auth: tuple[ApiToken, int] = Depends(require_scopes(SCOPE_TELEMETRY)),
) -> dict:
    """Valida a conexao e a chave.

    Devolve o nome e o prefixo da chave para o operador confirmar na tela
    do MetaTrader QUAL chave esta em uso — com varias emitidas, "conectado"
    sozinho nao diz se e a certa. O segredo nao volta, nunca.
    """
    token, user_id = auth
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "token_name": token.name,
        "token_prefix": token.prefix,
        "scopes": sorted(s.strip() for s in token.scopes.split(",") if s.strip()),
        "user_id": user_id,
        "server_time": datetime.now(UTC).isoformat(),
    }


@router.get("/setups")
def list_setups(
    include_closed: bool = False,
    db: Session = Depends(get_db),
    auth: tuple[ApiToken, int] = Depends(require_scopes(SCOPE_SETUP_READ)),
) -> dict:
    """Os setups DO DONO desta chave. Nunca de outro usuario."""
    _token, user_id = auth
    repo = HedgeRepository(db)

    setups = []
    for setup in repo.list_setups(user_id=user_id, include_closed=include_closed):
        versao = repo.current_version(setup)
        if versao is None:
            continue
        setups.append(
            {
                "setup_id": setup.id,
                "name": setup.name,
                "version": setup.current_version,
                "mode": setup.mode,
                "objective": setup.objective,
                "closed": setup.closed_at is not None,
                "closed_reason": setup.closed_reason or "",
                "desk_symbol": versao.desk_symbol,
                "broker_symbol": versao.broker_symbol,
                "is_long": bool(versao.desk_is_long),
                "entry": str(versao.desk_entry),
                "take": str(versao.desk_take),
                "stop": str(versao.desk_stop),
                "data_max_age_seconds": versao.data_max_age_seconds,
            }
        )

    return {"schema_version": SCHEMA_VERSION, "setups": setups}


@router.post("/snapshot")
def post_snapshot(
    body: SnapshotBody,
    db: Session = Depends(get_db),
    auth: tuple[ApiToken, int] = Depends(
        require_scopes(SCOPE_TELEMETRY, SCOPE_ANALYSIS)
    ),
) -> dict:
    """Recebe a cotacao e devolve a analise.

    Um endpoint em vez de dois (enviar + consultar) porque o conector
    precisa de resposta no mesmo ciclo: dois saltos dobrariam a latencia
    entre a cotacao e o que aparece no grafico.
    """
    token, user_id = auth

    if not snapshot_limiter.allow(f"token:{token.id}"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Limite de snapshots por segundo atingido para esta chave.",
        )

    if body.schema_version != SCHEMA_VERSION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Conector fala o schema {body.schema_version}; este servidor "
                f"fala {SCHEMA_VERSION}. Atualize o EA conector."
            ),
        )

    if body.bid <= 0 or body.ask <= 0 or body.ask < body.bid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bid e ask precisam ser positivos, com ask >= bid.",
        )

    try:
        saida = HedgeService(db).analyze(
            body.to_snapshot(), user_id=user_id, api_token_id=token.id
        )
    except PermissionError as exc:
        # 404 e nao 403: dizer "existe mas nao e seu" contaria a uma chave
        # vazada quais ids existem.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Setup nao encontrado."
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    db.commit()
    return analysis_payload(saida)


@router.get("/analysis/{setup_id}")
def latest_analysis(
    setup_id: int,
    db: Session = Depends(get_db),
    auth: tuple[ApiToken, int] = Depends(require_scopes(SCOPE_ANALYSIS)),
) -> dict:
    """A ultima analise gravada deste setup.

    Util para o painel e para o EA se reanexar sem esperar o proximo
    ciclo. Vem com a propria idade: um numero sem idade na tela parece
    atual mesmo quando nao e.
    """
    _token, user_id = auth
    repo = HedgeRepository(db)

    setup = repo.get_setup(setup_id, user_id=user_id)
    if setup is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Setup nao encontrado."
        )

    registro = repo.latest_analysis(setup.id)
    if registro is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "setup_id": setup.id,
            "state": "WAITING_DATA",
            "available": False,
        }

    agora = datetime.now(UTC)
    criado = registro.created_at
    if criado is not None and criado.tzinfo is None:
        criado = criado.replace(tzinfo=UTC)

    return {
        "schema_version": SCHEMA_VERSION,
        "setup_id": setup.id,
        "setup_version": registro.setup_version,
        "available": True,
        "sequence": registro.sequence,
        "captured_at": registro.captured_at.isoformat() if registro.captured_at else "",
        "age_seconds": int((agora - criado).total_seconds()) if criado else None,
        "current_price": str(registro.current_price),
        "model_kind": registro.model_kind,
        "model_version": registro.model_version,
        "forecast_available": registro.forecast_available,
        "unavailable_reason": registro.unavailable_reason or "",
        "take_first": float(registro.take_first) if registro.take_first is not None else None,
        "stop_first": float(registro.stop_first) if registro.stop_first is not None else None,
        "no_barrier": float(registro.no_barrier) if registro.no_barrier is not None else None,
        "closed": setup.closed_at is not None,
        "closed_reason": setup.closed_reason or "",
    }


@router.get("/history/{setup_id}")
def history(
    setup_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    auth: tuple[ApiToken, int] = Depends(require_scopes(SCOPE_ANALYSIS)),
) -> dict:
    _token, user_id = auth
    repo = HedgeRepository(db)

    setup = repo.get_setup(setup_id, user_id=user_id)
    if setup is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Setup nao encontrado."
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "setup_id": setup.id,
        "closed_reason": setup.closed_reason or "",
        "scenarios": [str(s) for s in Scenario],
        "items": [
            {
                "id": item.id,
                "setup_version": item.setup_version,
                "sequence": item.sequence,
                "captured_at": item.captured_at.isoformat() if item.captured_at else "",
                "current_price": str(item.current_price),
                "model_kind": item.model_kind,
                "model_version": item.model_version,
                "forecast_available": item.forecast_available,
                "unavailable_reason": item.unavailable_reason or "",
                "take_first": (
                    float(item.take_first) if item.take_first is not None else None
                ),
                "stop_first": (
                    float(item.stop_first) if item.stop_first is not None else None
                ),
                "no_barrier": (
                    float(item.no_barrier) if item.no_barrier is not None else None
                ),
                "financial_available": item.financial_available,
            }
            for item in repo.history(setup_id=setup.id, limit=limit)
        ],
    }
