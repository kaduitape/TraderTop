"""Painel do Hedge AI: as seis areas, e as acoes que mudam estado.

Router proprio em vez de crescer `dashboard.py` (que ja passa de 2400
linhas): as rotas daqui compartilham o mesmo contexto de setup e usuario,
e mistura-las com o resto tornaria as duas partes mais dificeis de ler.

## O segredo aparece uma vez

A chave em claro existe na resposta que a cria e nunca mais. Ela nao vai
para `system_settings`, nao volta em listagem e nao entra em log. Perdeu,
gera outra — e isso e proposital: se o sistema conseguisse mostrar de
novo, um vazamento do banco tambem conseguiria.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.dependencies.api_token import (
    SCOPE_ANALYSIS,
    SCOPE_SETUP_READ,
    SCOPE_TELEMETRY,
)
from app.api.dependencies.auth import get_current_user_for_web
from app.api.templates_engine import templates
from app.core.config import get_settings
from app.database.models.user import User
from app.database.repositories.api_token_repository import ApiTokenRepository
from app.database.repositories.audit_log_repository import AuditLogRepository
from app.database.repositories.hedge_repository import HedgeRepository
from app.database.repositories.symbol_repository import SymbolRepository
from app.database.session import get_db
from app.hedge.engine import Mode, Objective
from app.hedge.forecast.registry import available_versions, default_engine
from app.hedge.sessions import evaluate_freshness
from app.hedge.units import LevelUnit, level_to_price

router = APIRouter(tags=["hedge-dashboard"])

HEDGE_SCOPE_SET = f"{SCOPE_TELEMETRY},{SCOPE_SETUP_READ},{SCOPE_ANALYSIS}"


def _dec(valor: str, *, default: str = "0") -> Decimal:
    try:
        return Decimal((valor or "").strip().replace(",", ".") or default)
    except InvalidOperation:
        return Decimal(default)


def _voltar(mensagem: str = "", erro: str = "", ancora: str = "") -> RedirectResponse:
    partes = []
    if mensagem:
        partes.append(f"saved={quote(mensagem)}")
    if erro:
        partes.append(f"error={quote(erro)}")
    query = ("?" + "&".join(partes)) if partes else ""
    return RedirectResponse(url=f"/dashboard/hedge{query}{ancora}", status_code=303)


@router.get("/dashboard/hedge", response_class=HTMLResponse)
def dashboard_hedge(
    request: Request,
    saved: str | None = None,
    error: str | None = None,
    new_key: str | None = None,
    setup: int | None = None,
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """As seis areas numa pagina so.

    `new_key` chega pela URL de um redirect imediatamente apos a criacao e
    some na proxima navegacao. E a unica vez que o segredo aparece.
    """
    repo = HedgeRepository(db)
    settings = get_settings()
    agora = datetime.now(UTC)

    setups = repo.list_setups(user_id=user.id)
    selecionado = None
    if setup is not None:
        selecionado = repo.get_setup(setup, user_id=user.id)
    if selecionado is None and setups:
        selecionado = setups[0]

    linhas = []
    for item in setups:
        versao = repo.current_version(item)
        ultima = repo.latest_analysis(item.id)
        linhas.append({"setup": item, "version": versao, "latest": ultima})

    sessoes = []
    for sessao in repo.list_sessions(setup_ids=[s.id for s in setups]):
        frescor = evaluate_freshness(
            last_seen_at=sessao.last_seen_at, now=agora, max_age_seconds=30
        )
        sessoes.append({"session": sessao, "freshness": frescor})

    historico = repo.history(setup_id=selecionado.id, limit=50) if selecionado else []
    versoes = repo.list_versions(selecionado.id) if selecionado else []

    # So as chaves DESTE usuario: a lista de chaves de outro operador nao
    # tem por que aparecer aqui.
    chaves = [
        chave
        for chave in ApiTokenRepository(db).list_all()
        if chave.created_by_user_id == user.id
    ]

    motor = default_engine()

    return templates.TemplateResponse(
        request,
        "dashboard/hedge.html",
        {
            "user": user,
            "saved": saved,
            "error": error,
            "new_key": new_key,
            "rows": linhas,
            "selected": selecionado,
            "selected_version": repo.current_version(selecionado) if selecionado else None,
            "versions": versoes,
            "history": historico,
            "sessions": sessoes,
            "keys": chaves,
            "symbols": SymbolRepository(db).list_active(),
            "objectives": list(Objective),
            "modes": list(Mode),
            "level_units": list(LevelUnit),
            "engine_version": motor.version,
            "engine_kind": str(motor.kind),
            "engines": available_versions(),
            "jev_enabled": settings.jev_enabled,
            "jev_configured": bool(settings.jev_api_key),
            "jev_model": settings.jev_model,
            "jev_timeout": settings.jev_timeout_seconds,
            "jev_cache_ttl": settings.jev_cache_ttl_seconds,
            "api_base": str(request.base_url).rstrip("/"),
            "now": agora,
        },
    )


@router.post("/dashboard/hedge/setup")
def hedge_setup_save(
    name: str = Form(...),
    desk_symbol: str = Form(...),
    desk_direction: str = Form("LONG"),
    desk_entry: str = Form(...),
    desk_take: str = Form(...),
    desk_stop: str = Form(...),
    desk_contracts: str = Form("1"),
    level_unit: str = Form("PRICE"),
    desk_tick_size: str = Form("0.25"),
    desk_tick_value: str = Form("0.50"),
    desk_point: str = Form("0.01"),
    desk_commission: str = Form("0"),
    desk_slippage: str = Form("0"),
    broker_symbol: str = Form(...),
    coverage_ratio: str = Form("1"),
    price_ratio: str = Form(""),
    broker_commission: str = Form("0"),
    broker_slippage: str = Form("0"),
    account_currency: str = Form("USD"),
    horizon_minutes: str = Form("60"),
    display_timezone: str = Form("UTC"),
    data_max_age_seconds: str = Form("30"),
    objective: str = Form("FAVOR_DESK_TAKE"),
    mode: str = Form("SIMULATION"),
    mt5_position_ticket: str = Form(""),
    desk_reference: str = Form(""),
    setup_id: str = Form(""),
    change_note: str = Form(""),
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Cria ou versiona um setup.

    Os niveis podem chegar em qualquer unidade e sao convertidos para
    PRECO aqui, UMA vez. A unidade original fica gravada: so o preco final
    nao permite reconstruir o que o operador quis dizer.
    """
    try:
        unidade = LevelUnit(level_unit.upper())
    except ValueError:
        return _voltar(erro=f"unidade invalida: {level_unit}")

    comprado = desk_direction.upper() in {"LONG", "COMPRA", "BUY"}
    entrada = _dec(desk_entry)
    tick = _dec(desk_tick_size, default="0.25")
    ponto = _dec(desk_point, default="0.01")
    valor_tick = _dec(desk_tick_value, default="0.5")
    contratos = _dec(desk_contracts, default="1")

    if entrada <= 0 or tick <= 0 or ponto <= 0:
        return _voltar(erro="entrada, tick size e point precisam ser maiores que zero.")

    try:
        take = level_to_price(
            value=_dec(desk_take),
            unit=unidade,
            entry=entrada,
            is_profit_side=True,
            is_long=comprado,
            tick_size=tick,
            point=ponto,
            tick_value=valor_tick,
            volume=contratos,
        )
        stop = level_to_price(
            value=_dec(desk_stop),
            unit=unidade,
            entry=entrada,
            is_profit_side=False,
            is_long=comprado,
            tick_size=tick,
            point=ponto,
            tick_value=valor_tick,
            volume=contratos,
        )
    except Exception as exc:  # ConversionError e afins
        return _voltar(erro=f"nao foi possivel converter os niveis: {exc}")

    # Em TICKS, POINTS e MONEY o lado e deduzido da direcao e sai sempre
    # coerente. Em PRICE o operador digita dois numeros livres, e nada
    # impede trocar take com stop. Recusar AQUI, antes de gravar: o motor
    # tambem recusa, mas ai o setup invalido ja estaria no banco, visivel
    # na lista e pronto para ser anexado a um grafico.
    if not ((take > entrada > stop) if comprado else (take < entrada < stop)):
        esperado = (
            "take acima e stop abaixo da entrada"
            if comprado
            else "take abaixo e stop acima da entrada"
        )
        return _voltar(
            erro=(
                f"Niveis invertidos para uma {'compra' if comprado else 'venda'}: "
                f"esperado {esperado}. Entrada {entrada}, take {take}, stop {stop}."
            )
        )

    horizonte = max(60, int(_dec(horizon_minutes, default="60") * 60))

    params = {
        "desk_symbol": desk_symbol.strip().upper()[:40],
        "desk_is_long": comprado,
        "desk_entry": entrada,
        "desk_take": take,
        "desk_stop": stop,
        "desk_contracts": contratos,
        "level_unit": unidade.value,
        "desk_tick_size": tick,
        "desk_tick_value": valor_tick,
        "desk_point": ponto,
        "desk_commission": _dec(desk_commission),
        "desk_slippage": _dec(desk_slippage),
        "broker_symbol": broker_symbol.strip().upper()[:40],
        "coverage_ratio": _dec(coverage_ratio, default="1"),
        "price_ratio": _dec(price_ratio) if price_ratio.strip() else None,
        "broker_commission": _dec(broker_commission),
        "broker_slippage": _dec(broker_slippage),
        "account_currency": account_currency.strip().upper()[:8] or "USD",
        "horizon_seconds": horizonte,
        "deadline_utc": datetime.now(UTC) + timedelta(seconds=horizonte),
        "display_timezone": display_timezone.strip()[:64] or "UTC",
        "data_max_age_seconds": max(5, int(_dec(data_max_age_seconds, default="30"))),
        "mt5_position_ticket": (
            int(_dec(mt5_position_ticket)) if mt5_position_ticket.strip() else None
        ),
        "desk_reference": desk_reference.strip()[:120] or None,
    }

    repo = HedgeRepository(db)

    if setup_id.strip():
        existente = repo.get_setup(int(setup_id), user_id=user.id)
        if existente is None:
            return _voltar(erro="setup nao encontrado.")
        if existente.closed_at is not None:
            return _voltar(
                erro="setup encerrado nao recebe alteracao. Crie um novo.",
            )
        versao = repo.update_setup(
            existente,
            params=params,
            note=change_note.strip() or "alteracao pelo painel",
            user_id=user.id,
            mode=mode,
            objective=objective,
        )
        AuditLogRepository(db).record(
            action="hedge_setup_version",
            entity="hedge_setup",
            detail=f"setup {existente.id} -> versao {versao.version} por {user.username}",
            user_id=user.id,
        )
        db.commit()
        return _voltar(f"Setup atualizado (versao {versao.version}).", ancora="#setups")

    novo, _versao = repo.create_setup(
        user_id=user.id,
        name=name,
        mode=mode,
        objective=objective,
        params=params,
    )
    AuditLogRepository(db).record(
        action="hedge_setup_create",
        entity="hedge_setup",
        detail=f"setup {novo.id} '{novo.name}' por {user.username}",
        user_id=user.id,
    )
    db.commit()
    return _voltar(f"Setup criado (id {novo.id}).", ancora="#setups")


@router.post("/dashboard/hedge/setup/close")
def hedge_setup_close(
    setup_id: int = Form(...),
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    repo = HedgeRepository(db)
    alvo = repo.get_setup(setup_id, user_id=user.id)
    if alvo is None:
        return _voltar(erro="setup nao encontrado.")

    repo.close_setup(alvo, reason="MANUAL")
    AuditLogRepository(db).record(
        action="hedge_setup_close",
        entity="hedge_setup",
        detail=f"setup {alvo.id} encerrado a mao por {user.username}",
        user_id=user.id,
    )
    db.commit()
    return _voltar("Setup encerrado.", ancora="#setups")


@router.post("/dashboard/hedge/key")
def hedge_key_create(
    name: str = Form(...),
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Gera uma chave do conector.

    Os escopos sao FIXOS: telemetria, leitura de setup e analise. Nao ha
    campo para escolher porque nao ha caso de uso para uma chave de
    conector com mais do que isso, e um seletor so criaria a chance de
    alguem marcar algo a mais sem precisar.
    """
    repo = ApiTokenRepository(db)
    token, segredo = repo.create(name=name, user_id=user.id)
    token.scopes = HEDGE_SCOPE_SET

    AuditLogRepository(db).record(
        action="hedge_key_create",
        entity="api_token",
        # O prefixo identifica sem revelar. O segredo nao entra no log.
        detail=f"chave {token.prefix}... '{token.name}' criada por {user.username}",
        user_id=user.id,
    )
    db.commit()

    # O segredo viaja uma vez, na URL do redirect, e some na proxima
    # navegacao. Guarda-lo em sessao ou banco criaria um segundo lugar de
    # onde ele poderia vazar.
    return RedirectResponse(
        url=f"/dashboard/hedge?new_key={quote(segredo)}#chaves", status_code=303
    )


@router.post("/dashboard/hedge/key/revoke")
def hedge_key_revoke(
    token_id: int = Form(...),
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    repo = ApiTokenRepository(db)
    alvo = next(
        (
            chave
            for chave in repo.list_all()
            if chave.id == token_id and chave.created_by_user_id == user.id
        ),
        None,
    )
    if alvo is None:
        return _voltar(erro="chave nao encontrada.")

    repo.revoke(alvo.id)
    AuditLogRepository(db).record(
        action="hedge_key_revoke",
        entity="api_token",
        detail=f"chave {alvo.prefix}... revogada por {user.username}",
        user_id=user.id,
    )
    db.commit()
    return _voltar("Chave revogada. O conector para na proxima consulta.", ancora="#chaves")
